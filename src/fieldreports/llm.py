"""LLM extraction with Anthropic tool use, plus record/replay so tests never touch the network.

How one report is extracted:

1. One tool, ``record_daily_report``, whose ``input_schema`` is generated from the pydantic
   model. ``strict: true`` asks the API to keep the arguments schema-valid.
2. ``tool_choice`` is ``auto``, and the system prompt says to call the tool. Claude Sonnet 5.5
   (the default) rejects forced tool choice with a 400, so a response without the tool call
   counts as a failed attempt.
3. The tool input is validated by pydantic. On failure the error text goes back to the model as
   an ``is_error`` tool result and it gets **one** retry. Still invalid -> review queue.
4. A refusal or a truncated response is never parsed; it goes to the review queue.

Modes (``LLM_MODE``):

* ``mock``   replay responses recorded earlier; the default when no API key is set.
             A missing or stale fixture raises ``FixtureError``. Nothing is ever invented.
* ``record`` call the API and save every response to ``tests/fixtures/llm/<sha256>.json``.
* ``live``   call the API, save nothing.

The fixture key is the sha256 of the full request (model, system prompt, tool schema, messages),
so changing the prompt, the schema, the model or the report text all require a re-record.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from fieldreports.extract import LLMExtraction
from fieldreports.roster import Roster
from fieldreports.schema import ReportFields

DEFAULT_MODEL = "claude-sonnet-5-5"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"
PROMPT_VERSION = "extract-v1"
TOOL_NAME = "record_daily_report"
MAX_TOKENS = 16000
EFFORT = "low"  # narrow, well-specified task; checked against the eval

# Server-side refusal fallback ("default" routes by refusal category). Only sent to models
# that accept it; see https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

# USD per million tokens: input, output, cache read, cache write (5 min).
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

# JSON Schema keywords strict tool use does not support. Pydantic still enforces them
# client-side (e.g. headcount >= 0), and a violation becomes the retry message.
_UNSUPPORTED_KEYWORDS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "title", "default",
}  # fmt: skip


class LLMMode(StrEnum):
    MOCK = "mock"
    RECORD = "record"
    LIVE = "live"


class FixtureError(RuntimeError):
    """Mock mode has no recorded response for this exact request."""


# -- prompt and tool ---------------------------------------------------------------------------


def tool_schema() -> dict:
    """ReportFields' JSON schema with the keywords strict mode rejects removed."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return clean(ReportFields.model_json_schema())


def tool_definition() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Record the structured contents of one superintendent daily report.",
        "strict": True,
        "input_schema": tool_schema(),
    }


def system_prompt(roster: Roster) -> str:
    subs = "\n".join(f"- {s.company} ({s.trade})" for s in roster.subs)
    trades = ", ".join(sorted({s.trade for s in roster.subs}))
    return f"""\
You extract construction superintendent daily reports into structured data for a general \
contractor. Read the report and call the {TOOL_NAME} tool exactly once. Do not answer in text.

Project: {roster.project}
General contractor: {roster.general_contractor}
Superintendents: {", ".join(roster.superintendents)}
Subcontractors on this job (company, trade):
{subs}
Trades: {trades}

Rules:
- Record only what the report states. Never invent a value. If the project, report date or \
superintendent is not stated, use null.
- report_date is the day the work happened, as the superintendent wrote it (body or subject \
line). Two-digit years are 20xx. Never use an email's send time.
- Superintendent and companies: use the full names above when the text clearly refers to \
them by name, nickname or trade word ("elec", "framers", "summit roofers"). If a company is \
not on the list, copy its name as written. Trade must be one of the trades above.
- manpower.hours is hours worked PER WORKER, not crew total. If the report states a default \
shift ("all crews 8 hrs unless noted"), apply it to every crew without its own hours.
- delays: one entry per stoppage. hours = clock hours of work lost as stated. Causes: weather; \
rfi (waiting on an RFI answer; set rfi_no); material (late, short or wrong deliveries); \
inspection (waiting on or rescheduled inspections); owner_change (owner directives, ASIs, \
change orders); labor (crew shortages or crews pulled off); equipment (breakdowns); other.
- weather.weather_delay_hours = hours lost to weather (0 if none). When weather stopped work, \
also list it in delays with cause weather.
- safety_events types: near_miss (no injury, could have been one); first_aid (minor care on \
site); recordable (OSHA recordable: treatment beyond first aid, such as prescription \
medication, or lost time); observation (unsafe act or condition noted or corrected).
- inspections result: pass, fail or partial.
- Keep descriptions short and in the superintendent's words. notes = anything else worth \
keeping that fits no other field, otherwise ""."""


def user_message(text: str) -> dict:
    return {
        "role": "user",
        "content": f"<report>\n{text}</report>\n\nExtract this report with {TOOL_NAME}.",
    }


def request_key(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cost_usd(model: str, usage: dict) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out, p_read, p_write = PRICES[model]
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + (usage.get("cache_read_input_tokens") or 0) * p_read
        + (usage.get("cache_creation_input_tokens") or 0) * p_write
    ) / 1_000_000


# -- transports: where a response comes from ---------------------------------------------------


class Transport(Protocol):
    def create(self, params: dict, *, input_sha256: str) -> dict:
        """Return the Messages API response as a plain dict. ``input_sha256`` identifies the
        report text, so a missing fixture can be told apart from a stale one."""
        ...


class AnthropicTransport:
    """Calls the Messages API (beta namespace, for the server-side fallback parameter)."""

    def __init__(self, client: Any | None = None):
        if client is None:
            import anthropic  # imported lazily so mock mode never needs credentials

            client = anthropic.Anthropic()
        self.client = client

    def create(self, params: dict, *, input_sha256: str) -> dict:
        return self.client.beta.messages.create(**params).to_dict(mode="json")


@dataclass
class FixtureStore:
    directory: Path = FIXTURE_DIR

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def save(self, key: str, params: dict, response: dict, input_sha256: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fixture = {
            "request_sha256": key,
            "input_sha256": input_sha256,
            "prompt_version": PROMPT_VERSION,
            "model": params["model"],
            "served_by": response.get("model"),
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "usage": response.get("usage", {}),
            "request": params,
            "response": response,
        }
        self.path(key).write_text(
            json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )

    def load(self, key: str, params: dict, input_sha256: str) -> dict:
        path = self.path(key)
        if not path.exists():
            raise FixtureError(self._missing_message(params, input_sha256))
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture.get("request_sha256") != key or request_key(fixture["request"]) != key:
            raise FixtureError(f"fixture {path.name} does not match its request (edited by hand?). "
                               "Delete it and re-record with LLM_MODE=record.")  # fmt: skip
        return fixture["response"]

    def _missing_message(self, params: dict, input_sha256: str) -> str:
        stale = [f for f in self._all() if f.get("input_sha256") == input_sha256]
        hint = "No recorded LLM response for this request."
        if stale:
            old = stale[0]
            hint = (
                "Stale fixture: this report was recorded with model "
                f"{old.get('model')} and prompt {old.get('prompt_version')}, but the current "
                f"request uses model {params['model']} and prompt {PROMPT_VERSION} (or the "
                "prompt/schema text changed)."
            )
        return (
            f"{hint} Mock mode never invents a response. Set ANTHROPIC_API_KEY and re-record: "
            "LLM_MODE=record fieldreports ingest data/raw"
        )

    def _all(self) -> list[dict]:
        if not self.directory.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.directory.glob("*.json")]


class RecordingTransport:
    """``record`` mode: call the API, then save the response as a fixture."""

    def __init__(self, inner: Transport, store: FixtureStore):
        self.inner, self.store = inner, store

    def create(self, params: dict, *, input_sha256: str) -> dict:
        response = self.inner.create(params, input_sha256=input_sha256)
        self.store.save(request_key(params), params, response, input_sha256)
        return response


class ReplayTransport:
    """``mock`` mode: return the recorded response for this exact request, or fail."""

    def __init__(self, store: FixtureStore):
        self.store = store

    def create(self, params: dict, *, input_sha256: str) -> dict:
        return self.store.load(request_key(params), params, input_sha256)


# -- the extractor -----------------------------------------------------------------------------


class ClaudeExtractor:
    def __init__(self, transport: Transport, roster: Roster, model: str = DEFAULT_MODEL):
        self.transport, self.model = transport, model
        self.system = system_prompt(roster)
        self.tool = tool_definition()
        self.usage: list[dict] = []  # one entry per API call, for cost reporting

    def _params(self, messages: list[dict]) -> dict:
        params = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}
            ],
            "tools": [self.tool],
            "tool_choice": {"type": "auto"},
            "output_config": {"effort": EFFORT},
            "messages": messages,
        }
        if self.model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _call(self, params: dict, input_sha256: str) -> dict:
        response = self.transport.create(params, input_sha256=input_sha256)
        self.usage.append({"model": response.get("model", self.model), **response.get("usage", {})})
        return response

    def extract(self, text: str) -> LLMExtraction:
        input_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        messages = [user_message(text)]
        error = ""
        for attempt in (1, 2):
            response = self._call(self._params(messages), input_sha256)
            stop = response.get("stop_reason")
            if stop == "refusal":
                category = (response.get("stop_details") or {}).get("category")
                return LLMExtraction(None, attempt, f"model declined the request ({category})")
            if stop == "max_tokens":
                return LLMExtraction(None, attempt, "response was cut off at max_tokens")

            content = response.get("content", [])
            calls = [b for b in content if b.get("type") == "tool_use"]
            mine = [b for b in calls if b.get("name") == TOOL_NAME]
            if mine:
                try:
                    return LLMExtraction(ReportFields.model_validate(mine[0]["input"]), attempt)
                except ValidationError as exc:
                    error = "tool input failed validation: " + _format_errors(exc)
            else:
                error = f"model did not call {TOOL_NAME}"

            if attempt == 2:
                break
            # Append-only retry: the model's turn as returned, then feedback on it.
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                _feedback(calls, error),
            ]
        return LLMExtraction(None, 2, f"still failing after one retry: {error}")


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
    )


def _feedback(calls: list[dict], error: str) -> dict:
    if not calls:
        return {"role": "user", "content": f"{error}. Call {TOOL_NAME} with the report contents."}
    # Every tool_use block needs a tool_result in the next message.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "is_error": True,
                "content": f"{error}. Fix these fields and call {TOOL_NAME} again.",
            }
            for call in calls
        ],
    }


# -- wiring ------------------------------------------------------------------------------------


def resolve_mode(env: dict[str, str] | None = None) -> LLMMode:
    env = os.environ if env is None else env
    has_key = bool(env.get("ANTHROPIC_API_KEY"))
    raw = env.get("LLM_MODE", "").strip().lower()
    if not raw:
        return LLMMode.LIVE if has_key else LLMMode.MOCK
    try:
        mode = LLMMode(raw)
    except ValueError:
        raise ValueError(f"LLM_MODE must be mock, record or live, not {raw!r}") from None
    if mode is not LLMMode.MOCK and not has_key:
        raise ValueError(f"LLM_MODE={mode} calls the API and needs ANTHROPIC_API_KEY")
    return mode


def build_extractor(roster: Roster, mode: LLMMode, model: str | None = None) -> ClaudeExtractor:
    model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    store = FixtureStore()
    transport: Transport
    if mode is LLMMode.MOCK:
        transport = ReplayTransport(store)
    elif mode is LLMMode.RECORD:
        transport = RecordingTransport(AnthropicTransport(), store)
    else:
        transport = AnthropicTransport()
    return ClaudeExtractor(transport, roster, model)
