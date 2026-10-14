"""Tests for the LLM extractor's control flow: retry, refusal, record/replay, mode selection.

``ScriptedTransport`` is a test double that returns hand-built response dicts so the
control flow can be checked without a network. Those dicts are not model output: they are
never written to tests/fixtures/llm, and no eval number is computed from them. Real fixtures
only come from ``LLM_MODE=record``.
"""

import json
from datetime import date

import pytest
from conftest import RAW, load_gold

from fieldreports import db, llm
from fieldreports.ingest import ingest_folder
from fieldreports.pipeline import Status
from fieldreports.schema import ExtractionMethod

TEXT = (RAW / "daily 9-25.txt").read_text(encoding="utf-8")
GOOD_INPUT = dict(load_gold())["daily 9-25.txt"].model_dump(mode="json")


def tool_call(tool_input, call_id="toolu_1", name=llm.TOOL_NAME):
    return {
        "model": "claude-sonnet-5-5",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
        "usage": {"input_tokens": 3000, "output_tokens": 900},
    }


def stop(reason, **extra):
    return {
        "model": "claude-sonnet-5-5",
        "stop_reason": reason,
        "content": [],
        "usage": {},
        **extra,
    }


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, params, *, input_sha256):
        self.requests.append(json.loads(json.dumps(params)))  # snapshot
        return self.responses.pop(0)


def extractor(*responses, model="claude-sonnet-5-5", roster=None):
    from fieldreports.roster import Roster

    transport = ScriptedTransport(*responses)
    return llm.ClaudeExtractor(transport, roster or Roster.load(), model), transport


def bad_input():
    data = json.loads(json.dumps(GOOD_INPUT))
    data["manpower"][0]["headcount"] = -3
    return data


# -- tool schema ---------------------------------------------------------------------------


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_tool_schema_only_uses_keywords_strict_mode_supports():
    schema = llm.tool_schema()
    for node in _walk(schema):
        assert not (set(node) & llm._UNSUPPORTED_KEYWORDS), node
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node


def test_tool_schema_has_no_pipeline_owned_fields_and_keeps_enums():
    schema = llm.tool_schema()
    assert "confidence" not in schema["properties"]
    assert set(schema["$defs"]["DelayCause"]["enum"]) >= {"weather", "rfi", "owner_change"}


def test_tool_is_strict_and_tool_choice_is_auto(roster):
    ex, transport = extractor(tool_call(GOOD_INPUT))
    ex.extract(TEXT)
    params = transport.requests[0]
    assert params["tools"][0]["strict"] is True
    assert params["tool_choice"] == {"type": "auto"}  # forced choice is a 400 on Sonnet 5.5
    assert params["fallbacks"] == "default"
    assert params["betas"] == [llm.FALLBACK_BETA]


def test_fallbacks_only_sent_to_models_that_accept_them():
    ex, transport = extractor(tool_call(GOOD_INPUT), model="claude-haiku-5-5")
    ex.extract(TEXT)
    assert "fallbacks" not in transport.requests[0]
    assert transport.requests[0]["model"] == "claude-haiku-5-5"


def test_system_prompt_lists_the_roster(roster):
    prompt = llm.system_prompt(roster)
    assert "Volt Line Electric, Inc. (electrical)" in prompt
    assert "PER WORKER" in prompt


# -- extraction control flow ---------------------------------------------------------------


def test_valid_first_answer_needs_one_call():
    ex, transport = extractor(tool_call(GOOD_INPUT))
    result = ex.extract(TEXT)
    assert result.error is None
    assert result.attempts == 1
    assert result.report.manpower[0].headcount == 3
    assert len(transport.requests) == 1


def test_invalid_answer_is_retried_once_with_the_validation_error():
    ex, transport = extractor(tool_call(bad_input()), tool_call(GOOD_INPUT, call_id="toolu_2"))
    result = ex.extract(TEXT)
    assert result.report is not None and result.attempts == 2

    first, second = transport.requests
    assert second["messages"][:1] == first["messages"]  # append-only history
    assert second["messages"][1]["role"] == "assistant"
    [feedback] = second["messages"][2]["content"]
    assert feedback["type"] == "tool_result"
    assert feedback["tool_use_id"] == "toolu_1"
    assert feedback["is_error"] is True
    assert "manpower.0.headcount" in feedback["content"]


def test_invalid_twice_gives_up_without_guessing():
    ex, transport = extractor(tool_call(bad_input()), tool_call(bad_input(), call_id="toolu_2"))
    result = ex.extract(TEXT)
    assert result.report is None
    assert result.error.startswith("still failing after one retry: tool input failed validation")
    assert len(transport.requests) == 2


def test_answer_without_a_tool_call_gets_a_text_nudge_then_retry():
    no_call = {"model": "m", "stop_reason": "end_turn", "usage": {},
               "content": [{"type": "text", "text": "Here is the report..."}]}  # fmt: skip
    ex, transport = extractor(no_call, tool_call(GOOD_INPUT))
    assert ex.extract(TEXT).report is not None
    nudge = transport.requests[1]["messages"][2]
    assert nudge["role"] == "user" and llm.TOOL_NAME in nudge["content"]


def test_every_tool_call_gets_a_tool_result_on_retry():
    two_calls = tool_call(bad_input())
    two_calls["content"].append(
        {"type": "tool_use", "id": "toolu_x", "name": llm.TOOL_NAME, "input": bad_input()}
    )
    ex, transport = extractor(two_calls, tool_call(GOOD_INPUT, call_id="toolu_2"))
    ex.extract(TEXT)
    ids = [b["tool_use_id"] for b in transport.requests[1]["messages"][2]["content"]]
    assert ids == ["toolu_1", "toolu_x"]


def test_refusal_is_not_parsed_or_retried():
    ex, transport = extractor(stop("refusal", stop_details={"category": "general_harms"}))
    result = ex.extract(TEXT)
    assert result.report is None
    assert result.error == "model declined the request (general_harms)"
    assert len(transport.requests) == 1


def test_truncated_response_is_not_parsed():
    ex, _ = extractor(stop("max_tokens"))
    assert ex.extract(TEXT).error == "response was cut off at max_tokens"


def test_usage_is_collected_per_call():
    ex, _ = extractor(tool_call(bad_input()), tool_call(GOOD_INPUT))
    ex.extract(TEXT)
    assert [u["output_tokens"] for u in ex.usage] == [900, 900]


# -- record / replay -----------------------------------------------------------------------


def test_record_then_replay_gives_the_same_extraction_without_calling_the_api(tmp_path, roster):
    store = llm.FixtureStore(tmp_path)
    inner = ScriptedTransport(tool_call(bad_input()), tool_call(GOOD_INPUT, call_id="toolu_2"))
    recorded = llm.ClaudeExtractor(llm.RecordingTransport(inner, store), roster).extract(TEXT)

    fixtures = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert len(fixtures) == 2  # one per API call, including the retry
    for f in fixtures:
        assert f["model"] == "claude-sonnet-5-5"
        assert f["recorded_at"] and f["usage"]["input_tokens"] == 3000
        assert f["prompt_version"] == llm.PROMPT_VERSION
        assert llm.request_key(f["request"]) == f["request_sha256"]

    replayed = llm.ClaudeExtractor(llm.ReplayTransport(store), roster).extract(TEXT)
    assert replayed == recorded


def test_mock_mode_fails_loudly_when_nothing_was_recorded(tmp_path, roster):
    ex = llm.ClaudeExtractor(llm.ReplayTransport(llm.FixtureStore(tmp_path)), roster)
    with pytest.raises(llm.FixtureError, match=r"never invents a response.*LLM_MODE=record"):
        ex.extract(TEXT)


def test_changed_model_is_reported_as_a_stale_fixture(tmp_path, roster):
    store = llm.FixtureStore(tmp_path)
    inner = ScriptedTransport(tool_call(GOOD_INPUT))
    llm.ClaudeExtractor(llm.RecordingTransport(inner, store), roster).extract(TEXT)

    other_model = llm.ClaudeExtractor(llm.ReplayTransport(store), roster, "claude-opus-5-5")
    with pytest.raises(llm.FixtureError, match=r"Stale fixture.*claude-sonnet-5-5"):
        other_model.extract(TEXT)


def test_hand_edited_fixture_is_rejected(tmp_path, roster):
    store = llm.FixtureStore(tmp_path)
    inner = ScriptedTransport(tool_call(GOOD_INPUT))
    llm.ClaudeExtractor(llm.RecordingTransport(inner, store), roster).extract(TEXT)
    [path] = tmp_path.glob("*.json")
    fixture = json.loads(path.read_text())
    fixture["request"]["max_tokens"] = 1
    path.write_text(json.dumps(fixture))
    with pytest.raises(llm.FixtureError, match="does not match its request"):
        llm.ClaudeExtractor(llm.ReplayTransport(store), roster).extract(TEXT)


def test_ingest_uses_the_llm_only_for_free_form_reports(tmp_path, roster):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("daily 9-25.txt", "DR_2026-09-22_HarborPoint.txt"):
        (inbox / name).write_bytes((RAW / name).read_bytes())
    ex, transport = extractor(tool_call(GOOD_INPUT))
    conn = db.connect(tmp_path / "t.db")
    result = ingest_folder(conn, inbox, roster, today=date(2026, 10, 14), llm=ex)
    methods = {o.doc.path.name: o.report.extraction_method for o in result.outcomes}
    assert methods == {
        "DR_2026-09-22_HarborPoint.txt": ExtractionMethod.RULES,
        "daily 9-25.txt": ExtractionMethod.LLM,
    }
    assert result.count(Status.LOADED) == 2
    assert len(transport.requests) == 1


# -- modes and cost ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, llm.LLMMode.MOCK),
        ({"ANTHROPIC_API_KEY": "k"}, llm.LLMMode.LIVE),
        ({"ANTHROPIC_API_KEY": "k", "LLM_MODE": "record"}, llm.LLMMode.RECORD),
        ({"ANTHROPIC_API_KEY": "k", "LLM_MODE": "mock"}, llm.LLMMode.MOCK),
    ],
)
def test_mode_resolution(env, expected):
    assert llm.resolve_mode(env) is expected


@pytest.mark.parametrize("env", [{"LLM_MODE": "record"}, {"LLM_MODE": "live"}, {"LLM_MODE": "x"}])
def test_bad_mode_settings_fail_instead_of_silently_mocking(env):
    with pytest.raises(ValueError):
        llm.resolve_mode(env)


def test_cost_uses_per_million_token_prices():
    usage = {"input_tokens": 3000, "output_tokens": 900, "cache_read_input_tokens": 2000}
    # 3000*$2 + 900*$10 + 2000*$0.20, per million tokens
    assert llm.cost_usd("claude-sonnet-5-5", usage) == pytest.approx(0.0154)
    assert llm.cost_usd("some-unpriced-model", usage) is None
