# Extraction eval

Gold set: 12 hand-checked reports, 5 on the DR-1 template and 7 free-form (.txt, .eml, .pdf). Field-level micro-averaged scores; see `src/fieldreports/evaluate.py` for the matching rules.

LLM: `claude-sonnet-5-5`, mode `mock` (replaying responses recorded with `LLM_MODE=record`; nothing called the API).

## Overall

| Method | Reports | Precision | Recall | F1 |
|---|---|---|---|---|
| Rules | template (5) | 1.000 | 1.000 | 1.000 |
| Rules | free-form (7) | 0.000 | 0.000 | 0.000 |
| Pipeline (rules first, LLM fallback) | template (5) | 1.000 | 1.000 | 1.000 |
| Pipeline (rules first, LLM fallback) | free-form (7) | 0.991 | 0.993 | 0.992 |
| Pipeline (rules first, LLM fallback) | all (12) | 0.994 | 0.996 | 0.995 |

Rules return nothing for free-form reports by design (they only read the template), so their free-form row is 0. The pipeline routes template reports to the rules and everything else to the LLM, so its free-form row is the LLM's own score; the pipeline rows are what `ingest` actually does.

Not run here: the LLM on the template reports as well (the rules-vs-LLM comparison on identical inputs). It needs those responses recorded first: `fieldreports eval --record`.

## By section: Pipeline (rules first, LLM fallback), all 12 reports

| Section | Gold facts | Precision | Recall | F1 |
|---|---|---|---|---|
| header | 36 | 1.000 | 1.000 | 1.000 |
| weather | 48 | 1.000 | 1.000 | 1.000 |
| manpower | 396 | 1.000 | 1.000 | 1.000 |
| work_completed | 150 | 0.993 | 0.993 | 0.993 |
| delays | 26 | 1.000 | 1.000 | 1.000 |
| safety_events | 6 | 1.000 | 0.667 | 0.800 |
| inspections | 15 | 1.000 | 1.000 | 1.000 |
| deliveries | 15 | 0.833 | 1.000 | 0.909 |
| visitors | 1 | 1.000 | 1.000 | 1.000 |
| notes | 2 | 1.000 | 1.000 | 1.000 |

## Every Pipeline (rules first, LLM fallback) error

**Daily - Harbor Pt MOB - 9-4.eml**
- deliveries.notes: extra '14 reels'

**daily 9-11.txt**
- work_completed.area: 'L1 and L2', expected 'L1 east'
- deliveries.notes: extra '8 bunks'

**daily 9-25.txt**
- safety_events: missed item {'type': 'observation', 'description': 'Ladder missing a foot found on L2; tagged out and removed'}
- deliveries.notes: extra '8 bunks'


## Cost

7 LLM calls, 23,446 input tokens (including cached) and 6,237 output tokens: $0.0728, about $0.0104 per call.
