# PLAN — field-report-pipeline

> Turns superintendent daily reports into structured, queryable data and a weekly summary.

**Status: approved (2026-10-14).** Decisions made at approval:

1. **LLM fixtures are always real recorded responses.** `LLM_MODE=record|live|mock`.
   - `record` calls the API and saves `tests/fixtures/llm/<sha256(model+prompt+input)>.json`
     with the model id, a `recorded_at` timestamp and the token usage.
   - `mock` looks fixtures up by the same hash. A missing or stale fixture fails with a message
     saying to re-record. It never falls back to invented output.
   - No fixture is ever hand-written.
   - Build order: every milestone that doesn't need the LLM comes first (schema, synthetic data,
     rules parser, validation + review queue, SQLite + queries). Work stops before the LLM
     extractor milestone until an API key is set and record mode has been run.
2. **Git.** Repo-local identity only. Stay on `main`, commit after each milestone, never push.

Calendar update: 30 working days (29 weekdays plus the Saturday make-up on 9/26), so 30 good
reports plus 2 broken ones, **32 files**.

---

## 1. Domain decisions (these are what a construction hiring manager will check)

| Topic | Decision | Why |
|---|---|---|
| `manpower.hours` | Hours **per worker** for the day (the shift length), not the crew total. | The spec's "hours > 24 per worker" check only makes sense this way. |
| Man-hours | `headcount × hours`, computed **in SQL** and never stored. | One source of truth. A PM can't argue with a derived column that can't drift. |
| Delay `hours` | Clock hours of work stoppage **as the super reported them**. They are not crew-hours. | Reports rarely say which crew stood idle. Multiplying by headcount would invent numbers. This is listed as a limitation. |
| Weather delay | `weather.weather_delay_hours` is stored as written. The **delays table** feeds the lost-time queries. If the two disagree, a validation warning is logged. The report is still accepted. | Prevents double counting. Weather days still show up in the weekly summary because they matter for excusable-delay and time-extension requests. |
| Report date | Never guessed from the file mtime or the email `Date:` header. A missing date sends the report to review. | An email sent after midnight would land on the wrong day. Date errors break the delay record that claims depend on. |
| One report per day | `UNIQUE(project, report_date)`. A second report for a date already loaded goes to review with the reason "duplicate date (revised report?)". | Revised dailies are real. A human decides which version counts. |
| Sub roster | A `data/project.yaml` file lists the subs, their trades and aliases (`elec`, `sparky`, `Volt Electric` → *Volt Electric, Inc.*). Both extractors use it. | A GC always knows its subs. Without the roster, "elec 6 guys" can't be tied to a company, and man-hours by sub falls apart. |
| Week | Monday through Sunday. `--week` must be a Monday (2026-09-21 is one). | Saturday make-up days count in the week they happen. |

**Fictional project:** *Harbor Point Medical Office Building*, a 3-story steel-frame MOB. It is
between dry-in and interior rough-in. Trades on site: framing/drywall, electrical, plumbing,
HVAC, fire sprinkler, roofing, curtain wall, and sitework. Inspections: city building
(framing, above-ceiling), fire marshal (sprinkler hydro), and a third-party special inspector
(firestopping, concrete cylinders). Deliveries: drywall board, an RTU set by crane, switchgear.

**Calendar:** Mon 2026-08-31 to Fri 2026-10-09. Labor Day (Mon 9/7) is off, which leaves
**29 working days**. The **week of 2026-09-21 is the weather week**: two rain days that stop
roofing, curtain wall and sitework while interior trades keep going. A Saturday make-up day
(9/26) follows.

## 2. Architecture

```
data/raw/*.txt|*.eml|*.pdf
   │  ingest: sha256 per file → skip if already in `files` table (idempotent)
   ▼
text loader (.txt as-is · .eml → stdlib email, plain-text part · .pdf → pdfplumber)
   │
   ▼
extract ── template detected? ── yes ─► rules parser (regex, deterministic)
   │                                 └─ fails mid-parse ─┐
   └─────────────── no ─────────────────────────────────►├─► LLM extractor (tool use)
                                                         │     validate with pydantic,
                                                         │     retry once with the error
   ▼                                                     ▼
validate + score confidence ──► needs_review (reason)  |  SQLite facts tables
                                                               ▼
                                              queries · weekly summary · eval
```

The extractor is picked by **content, not file type**. A PDF printed from the template goes
through the rules parser.

## 3. Package layout

```
src/fieldreports/
  schema.py        pydantic v2 models (DailyReport + parts) and the LLM tool schema
  roster.py        loads project.yaml, normalizes company/trade aliases
  loaders.py       file → text (.txt/.eml/.pdf), sha256
  rules.py         template parser
  llm.py           Anthropic client wrapper, mock/live mode, fixture record/replay
  extract.py       routing: rules first, then LLM
  validate.py      hard checks, confidence score, review reasons
  db.py            DDL, connection, load/upsert
  queries.py       every SQL statement, named and commented
  weekly.py        weekly markdown (SQL numbers + LLM narrative + template fallback)
  evaluate.py      field-level P/R/F1
  cli.py           typer + rich: ingest, query, weekly, review, eval
scripts/generate_reports.py   synthetic data (seeded, reproducible)
data/raw/ data/gold/ data/project.yaml
tests/fixtures/llm/           recorded LLM responses, keyed by sha256(prompt_version + text)
eval/results.md   docs/sample_weekly_2026-09-21.md
```

## 4. Key technical choices

- **Schema.** `DailyReport` follows the spec exactly. Enums: `DelayCause`, `SafetyType`,
  `InspectionResult`, `ExtractionMethod`. `source_file`, `extraction_method` and `confidence`
  are set by the pipeline. They are left out of the schema the LLM sees.
- **LLM call.** Model comes from `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`, as you
  specified). It uses a single tool, `record_daily_report`, whose `input_schema` is generated
  from the pydantic model. Notes on the current API:
  - Claude Sonnet 5.5 **rejects forced `tool_choice` (`any`/`tool`) with a 400.** So the call
    uses `tool_choice: auto`, `strict: true` on the tool, and a system prompt that says to call
    the tool. "No tool call" counts as a failure and uses up the single retry.
  - The retry is append-only. It sends a `tool_result` with `is_error: true` that carries the
    pydantic error text.
  - `stop_reason == "refusal"` or `"max_tokens"` sends the report to review. It is never parsed.
  - Effort is set to `low` with adaptive thinking. Extraction is a narrow task, so cost and
    latency matter more than deep reasoning. I'll check this against the eval.
  - Server-side refusal fallback (`fallbacks: "default"`) is on by default, as recommended for
    Sonnet 5.5. It's one parameter, and I'll drop it if you'd rather not use a beta feature.
- **Mock vs live.** Mock mode runs if `LLM_MODE=mock` **or** no API key is set. Mock mode
  replays fixtures. A cache miss raises a clear error and never touches the network. A
  `conftest.py` guard makes any real `anthropic` call fail in tests. Live mode with
  `--record` writes fixtures, including `usage` token counts, which feed the measured cost per
  report.
- **Confidence.** This is a heuristic, not a calibrated probability, and the docs say so.
  It starts at 1.0 and loses points for:
  - **numbers the extractor produced that don't appear in the source text** (grounding check:
    headcounts, hours, RFI numbers, temperatures)
  - needing the retry
  - unknown companies not in the roster
  - empty core sections (no manpower on a working day)

  A score below **0.80** sends the report to review.
- **Hard failures go to review.** Missing project, date or superintendent. A date in the
  future (the clock is injected so tests are deterministic). Headcount over 500. Hours over 24
  per worker or below 0. Delay hours over 24. Unparseable or garbled text (detected by a
  low ratio of dictionary words to tokens).
- **SQLite tables:**
  - `files(sha256 PK, path, ingested_at, status)`
  - `reports`
  - `manpower`, `work_items`, `delays`, `safety_events`, `inspections`, `deliveries` (each
    with an FK to `reports` and `ON DELETE CASCADE`)
  - `visitors`
  - `review_queue(sha256, path, reason, payload_json)`

  Review items stay out of the facts tables, as the spec says. They share one DB file so
  `review` and `ingest` stay simple.
- **Weekly summary.** All tables come from `queries.py`: man-hours by sub, by day, delays by
  cause (with RFI numbers), safety, inspections (failures highlighted), deliveries, and
  weather days. The LLM sees **only the computed numbers**, never the raw reports. It writes
  one paragraph. If any number in that paragraph isn't in the facts, or the LLM is
  unavailable, a template paragraph is used instead.

## 5. Synthetic data (`scripts/generate_reports.py`, seeded)

| Writer | Style | Files |
|---|---|---|
| Superintendent *Dana Okafor* | Company template, fills every section | ~17 templated `.txt` + 2 templated `.pdf` |
| Asst. superintendent *Marcus Reyes* (Thu/Fri and the week Dana is out) | Messy free text: "elec 6 guys", "lost 2 hrs - waiting on RFI 14 response", "rain AM, roofers sent home 10:30" | ~5 `.txt` + ~5 `.eml` + 1 free-form `.pdf` |
| Broken | One with no date, one garbled (scanner OCR noise) | 2 `.txt`. **Both must end up in review.** |

That's 29 good reports plus 2 broken ones, **31 files**. The generator builds structured
"truth" first and then renders it in each style. That way the gold files and the expected
numbers in the query tests come from the same truth. They're then hand-checked against the
rendered text, because rendering can drop or garble facts, and the gold has to match what the
text actually says. PDFs are created with `fpdf2` (a dev-only dependency). The generated files
are committed, so a user only needs `pdfplumber`.

## 6. Evaluation

- **Gold set.** `data/gold/*.json` has 12 reports: 3 templated `.txt`, 3 free-form `.txt`,
  3 `.eml`, and 3 `.pdf`, including the weather week.
- **Facts.** Each report is flattened into a set of `(section, item_key, field, value)` facts.
  List items are matched by a key: manpower by canonical company, delays by
  cause + RFI number or ordinal, inspections by type, deliveries by supplier + material.
  Values are normalized (case, whitespace, aliases, number formatting). Free-text fields
  (`description`, `notes`) are scored with token-overlap ≥ 0.6 instead of exact match, and
  this is documented.
- **Metrics.** Per section: precision = TP / predicted, recall = TP / gold, and F1. Results are
  reported for **rules**, **LLM**, and **pipeline** (rules first, LLM fallback) on all 12
  reports, plus rules vs LLM on the templated subset for a fair comparison. The run writes
  `eval/results.md`.
- **Modes.** `fieldreports eval` uses fixtures and is deterministic, so it can run in CI.
  `fieldreports eval --live` calls the API.

## 7. CLI

```
fieldreports ingest data/raw [--db fieldreports.db]
fieldreports query manhours --by sub|trade|day [--week 2026-09-21]
fieldreports query delays   --by cause|day     [--week ...]
fieldreports weekly --week 2026-09-21 [--out docs/]
fieldreports review
fieldreports eval [--live] [--record]
```

## 8. Milestones (tests and one commit after each)

1. **Scaffold + schema.** pyproject, src layout, ruff, CI (3.11 and 3.12), MIT license,
   `.env.example`, and schema tests.
2. **Synthetic data.** Generator, `data/raw`, and `project.yaml`.
3. **Rules parser.** Parser tests on templated `.txt` and `.pdf`, plus a "free-form isn't
   detected as a template" test.
4. **LLM extractor + fixtures.** Mock/live client, retry logic tested with a fake client
   (bad, then good), refusal/no-tool handling, and recorded fixtures.
5. **Validation + review queue.** Tests for each hard check, the confidence math, and both
   broken reports going to review.
6. **SQLite + queries.** Idempotent re-ingest test (second run adds 0 rows), and query tests
   with hand-computed man-hours and delay totals.
7. **Weekly report.** Snapshot test of the tables, a test that a narrative with a made-up
   number gets rejected, and the template fallback.
8. **Eval.** Metric unit tests on toy facts, gold files, and a mock-mode eval test.
9. **Docs.** README (mermaid diagram, real CLI output, results table), the sample weekly,
   LEARNING.md, and the final full run plus 2 resume bullets using real numbers only.

Target: about 1,500 lines of source, no ORM, no framework beyond typer/rich.
Runtime dependencies: `anthropic`, `pydantic`, `typer`, `rich`, `pdfplumber`, `pyyaml`,
`python-dotenv`.

---

## Open questions (I need answers before starting)

1. **No `ANTHROPIC_API_KEY` is set on this machine.** "Recorded LLM responses" are only
   honest if they were actually recorded. The options:
   - **(a) Recommended:** you set a key, and I run `eval --live --record` once to record
     fixtures for about 25 reports. At Sonnet 5.5 rates ($2 in / $10 out per MTok), with about
     3k input and 1k output tokens per report, that's roughly **$0.02 per report, under $1 in
     total**. The exact number is measured from `usage` and goes in the README.
   - **(b)** No key: I write the fixtures by hand. The README and `eval/results.md` would then
     have to say the LLM numbers are **simulated, not measured**, and the LLM-F1 resume bullet
     would be dropped.
2. **Git identity.** This folder isn't a git repo yet. Your global git config says
   `Manohar Reddy <akb74639@gmail.com>`, so commits in your portfolio repo would show that
   name. Should I set a local identity for this repo, and if so, which name and email?
3. **Is OneDrive OK?** The project lives in a OneDrive-synced folder. That works, but OneDrive
   can lock the SQLite file during a sync. The DB is gitignored and rebuilt by `ingest`, so the
   risk is small. I'm only mentioning it.

---

## As built (2026-10-14)

All milestones are done. Where the build differs from the plan above:

- **Record mode fills gaps.** `LLM_MODE=record` replays any request that already has a
  fixture instead of paying again. `LLM_RECORD_REFRESH=1` re-records. This keeps verified
  recordings stable between record sessions.
- **Recorded so far:** the 11 free-form reports (11 calls, $0.1136). Not yet recorded: the
  LLM on the 5 template gold reports (`fieldreports eval --record`) and the weekly narrative.
  `eval/results.md` and the sample weekly page state both.
- **`constraints.txt`** pins pydantic and pdfplumber/pdfminer.six, because the fixture key
  hashes the request, which includes the pydantic schema and the PDF text.
- **Size.** About 2,600 lines of source and 1,300 of tests, more than the ~1,500 targeted;
  most of the extra is the weekly renderer, the eval report and the record/replay layer.
- **CI** is configured but hasn't been verified on GitHub's runners yet. Everything has been
  run locally on Windows with Python 3.13.
