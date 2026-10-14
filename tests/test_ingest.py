import json
import shutil
from datetime import date

import pytest
from conftest import RAW

from fieldreports import db, queries
from fieldreports.ingest import ingest_folder
from fieldreports.pipeline import Status

TODAY = date(2026, 10, 14)
TABLES = [
    "files", "reports", "manpower", "work_items", "delays", "safety_events",
    "inspections", "deliveries", "visitors", "review_queue",
]  # fmt: skip


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_first_ingest_loads_templates_and_queues_broken_reports(conn, roster):
    result = ingest_folder(conn, RAW, roster, today=TODAY)
    assert (result.count(Status.LOADED), result.count(Status.REVIEW)) == (19, 2)
    assert result.count(Status.PENDING) == 11
    assert result.skipped == 0
    counts = _counts(conn)
    assert counts["reports"] == 19
    assert counts["files"] == 21  # pending files are deliberately not recorded


def test_reingest_is_a_no_op(conn, roster):
    ingest_folder(conn, RAW, roster, today=TODAY)
    before = _counts(conn)
    again = ingest_folder(conn, RAW, roster, today=TODAY)
    assert again.skipped == 21
    assert again.count(Status.LOADED) == again.count(Status.REVIEW) == 0
    assert _counts(conn) == before


def test_same_bytes_under_a_new_name_is_skipped(conn, roster, tmp_path):
    ingest_folder(conn, RAW, roster, today=TODAY)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(RAW / "DR_2026-09-22_HarborPoint.txt", inbox / "fwd- daily report.txt")
    assert ingest_folder(conn, inbox, roster, today=TODAY).skipped == 1


def test_revised_report_for_a_loaded_day_goes_to_review(conn, roster, tmp_path):
    ingest_folder(conn, RAW, roster, today=TODAY)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    text = (RAW / "DR_2026-09-22_HarborPoint.txt").read_text(encoding="utf-8")
    (inbox / "DR_2026-09-22_rev1.txt").write_text(text.replace("Pumped", "Pumped out"))

    result = ingest_folder(conn, inbox, roster, today=TODAY)
    [outcome] = result.outcomes
    assert outcome.status is Status.REVIEW
    assert "already loaded (DR_2026-09-22_HarborPoint.txt)" in outcome.reasons[0]
    assert _counts(conn)["reports"] == 19


def test_review_queue_lists_each_item_with_its_reason(conn, roster):
    ingest_folder(conn, RAW, roster, today=TODAY)
    queue = {row["path"].split("\\")[-1].split("/")[-1]: json.loads(row["reasons"])
             for row in queries.review_queue(conn)}  # fmt: skip
    assert queue["DR_HarborPoint_draft.txt"] == ["missing required field: report_date"]
    assert "garbled" in queue["SCAN0047.txt"][0]


def test_review_items_keep_the_extracted_payload_for_the_reviewer(conn, roster):
    ingest_folder(conn, RAW, roster, today=TODAY)
    payload = conn.execute(
        "SELECT payload_json FROM review_queue WHERE path LIKE '%draft%'"
    ).fetchone()[0]
    assert json.loads(payload)["superintendent"] == "Dana Okafor"


def test_rain_week_delays_from_the_real_data(conn, roster):
    """9/22 (8 hrs, rained out) and 9/23 (4 hrs, rain until 11) are both Dana's template
    reports, and ASI-07 on 9/24 held drywall 2 hrs. Free-form days are still pending."""
    ingest_folder(conn, RAW, roster, today=TODAY)
    rows = queries.delays(conn, "cause", date(2026, 9, 21))
    assert [(r["cause"], r["events"], r["hours"]) for r in rows] == [
        ("weather", 2, 12.0),
        ("owner_change", 1, 2.0),
    ]
