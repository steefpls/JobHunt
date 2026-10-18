"""The Dashboard's dates: the day a job was applied to (or rejected / turned down), and when its
state last changed, with a dash when nothing was recorded."""
import csv
import io
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.web.app import ago_text, create_app


def _job(**kw):
    base = dict(source="demo", url="http://x/1", company="Lumen Analytics", role="Backend Engineer", location="SG")
    base.update(kw)
    return db.add_job(**base)


def _row(jid: int) -> dict:
    return next(j for j in db.list_jobs() if j["id"] == jid)


def test_list_jobs_carries_the_last_state_change():
    jid = _job()
    db.update_job(jid, state="Ready")
    db.update_job(jid, state="Applied")
    job = _row(jid)
    assert job["state"] == job["last_state"] == "Applied"
    assert job["last_state_ts"].startswith(date.today().isoformat())


def test_applied_or_rejected_labels_every_date_it_knows():
    applied = _job(url="http://x/a")
    db.update_job(applied, state="Applied")  # fills in date_applied = today
    rejected = _job(url="http://x/b")
    db.update_job(rejected, state="Rejected", by="you")
    not_for_me = _job(url="http://x/c")
    db.reject(not_for_me, ["Too senior"])
    never = _job(url="http://x/d")
    today = date.today().isoformat()
    assert db.applied_or_rejected(_row(applied)) == ("Applied", today)
    assert db.applied_or_rejected(_row(rejected)) == ("Rejected", today)
    assert db.applied_or_rejected(_row(not_for_me)) == ("Not for me", today)
    assert db.applied_or_rejected(_row(never)) == ("", "")


def test_a_rejection_after_applying_keeps_the_applied_date():
    jid = _job()
    db.update_job(jid, state="Applied", date_applied="2026-10-01")
    db.update_job(jid, state="Rejected", by="you")
    assert db.applied_or_rejected(_row(jid)) == ("Applied", "2026-10-01")


def test_an_old_job_with_no_recorded_date_shows_nothing():
    # never a guessed date: no date_applied and no state log means no date
    assert db.applied_or_rejected({"state": "Rejected", "date_applied": "", "last_state": "Rejected",
                                   "last_state_ts": ""}) == ("", "")
    assert db.applied_or_rejected({"state": "Applied", "date_applied": ""}) == ("", "")


def test_ago_text():
    now = datetime(2026, 10, 15, 12, 0, 0)
    assert ago_text("2026-10-15T11:59:31", now) == "just now"
    assert ago_text("2026-10-15T11:30:00", now) == "30 minutes ago"
    assert ago_text("2026-10-15T09:00:00", now) == "3 hours ago"
    assert ago_text("2026-10-14T09:00:00", now) == "yesterday"
    assert ago_text("2026-10-13T12:00:00", now) == "2 days ago"
    assert ago_text("2026-10-05T12:00:00", now) == "1 week ago"
    assert ago_text("2026-08-15T12:00:00", now) == "2 months ago"
    assert ago_text("2025-10-15T12:00:00", now) == "1 year ago"
    assert ago_text("2024-10-15T12:00:00", now) == "2 years ago"
    assert ago_text("") == "" and ago_text("not a time") == ""


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_dashboard_shows_the_dates_and_a_dash(client):
    today = date.today().isoformat()
    applied = _job(url="http://x/a", company="Alpha")
    db.update_job(applied, state="Applied")
    declined = _job(url="http://x/b", company="Beta")
    db.reject(declined, ["Too senior"])
    _job(url="http://x/c", company="Gamma")  # Found: nothing was decided, no date to show
    page = client.get("/applications").text
    assert "<th>Applied / rejected</th>" in page and "<th>Last change</th>" in page
    assert f"Applied {today}" in page and f"Not interested {today}" in page
    assert "just now" in page and 'title="' in page  # the exact time is in the tooltip
    gamma_row = page.split(">Gamma<", 1)[1].split("</tr>", 1)[0]
    assert ">—</td>" in gamma_row  # a dash, never a wrong date


def test_export_csv_has_the_dates(client):
    jid = _job(url="http://x/a", company="Alpha")
    db.update_job(jid, state="Applied")
    rows = list(csv.reader(io.StringIO(db.export_csv())))
    header = rows[0]
    assert "applied_or_rejected_date" in header and "state_changed_at" in header
    row = dict(zip(header, rows[1]))
    assert row["applied_or_rejected_date"] == date.today().isoformat()
    assert row["state_changed_at"].startswith(date.today().isoformat())
