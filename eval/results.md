# Extraction eval

Gold set: 12 hand-checked reports, 5 on the DR-1 template and 7 free-form (.txt, .eml, .pdf). Field-level micro-averaged scores; see `src/fieldreports/evaluate.py` for the matching rules.

LLM: `claude-sonnet-5-5`, mode `mock` (replaying responses recorded with `LLM_MODE=record`; nothing called the API).

## Overall

| Method | Reports | Precision | Recall | F1 |
|---|---|---|---|---|
| Rules | template (5) | 1.000 | 1.000 | 1.000 |
| Rules | free-form (7) | 0.000 | 0.000 | 0.000 |
| LLM | template (5) | 1.000 | 1.000 | 1.000 |
| LLM | free-form (7) | 0.990 | 0.988 | 0.989 |
| LLM | all (12) | 0.994 | 0.993 | 0.994 |
| Pipeline (rules first, LLM fallback) | template (5) | 1.000 | 1.000 | 1.000 |
| Pipeline (rules first, LLM fallback) | free-form (7) | 0.990 | 0.988 | 0.989 |
| Pipeline (rules first, LLM fallback) | all (12) | 0.994 | 0.993 | 0.994 |

Rules return nothing for free-form reports by design (they only read the template), so their free-form row is 0. The pipeline routes template reports to the rules and everything else to the LLM, so its free-form row is the LLM's own score; the pipeline rows are what `ingest` actually does.

## By section: Pipeline (rules first, LLM fallback), all 12 reports

| Section | Gold facts | Precision | Recall | F1 |
|---|---|---|---|---|
| header | 36 | 1.000 | 1.000 | 1.000 |
| weather | 48 | 1.000 | 1.000 | 1.000 |
| manpower | 396 | 1.000 | 1.000 | 1.000 |
| work_completed | 150 | 0.980 | 0.980 | 0.980 |
| delays | 26 | 1.000 | 1.000 | 1.000 |
| safety_events | 6 | 1.000 | 0.667 | 0.800 |
| inspections | 15 | 1.000 | 1.000 | 1.000 |
| deliveries | 15 | 1.000 | 1.000 | 1.000 |
| visitors | 1 | 1.000 | 1.000 | 1.000 |
| notes | 2 | 0.667 | 1.000 | 0.800 |

## Rules vs LLM on the same 5 template reports

| Section | Rules F1 | LLM F1 |
|---|---|---|
| header | 1.000 | 1.000 |
| weather | 1.000 | 1.000 |
| manpower | 1.000 | 1.000 |
| work_completed | 1.000 | 1.000 |
| delays | 1.000 | 1.000 |
| safety_events | 1.000 | 1.000 |
| inspections | 1.000 | 1.000 |
| deliveries | 1.000 | 1.000 |
| visitors | n/a | n/a |
| notes | 1.000 | 1.000 |

## Every LLM error

**Daily - Harbor Pt MOB - 9-4.eml**
- work_completed.area: 'L1 and L2', expected 'L1 west'

**daily 9-11.txt**
- work_completed.area: 'L1 and L2', expected 'L1 east'
- safety_events: missed item {'type': 'first_aid', 'description': 'Keystone framer cut hand on stud edge; cleaned and bandaged on site, back to work'}

**daily 9-25.txt**
- notes: extra item {'notes': 'Glassline left at 1 PM.'}

**daily 9-29.txt**
- work_completed.area: 'All floors / L1 east dumpster', expected 'L1 east'


## Cost

12 LLM extraction calls (one per gold report the LLM read), 43,166 input tokens (including cached) and 10,854 output tokens: $0.1295, about $0.0108 per call.
