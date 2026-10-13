"""The SQLite tracker that replaced applications.csv."""
import csv
import io
from datetime import date

import pytest

from jobhunt import db


def _job(**kw):
    base = dict(source="demo", url="http://x/1", company="Lumen Analytics", role="Backend Engineer", location="SG")
    base.update(kw)
    return db.add_job(**base)


def test_add_and_get_job():
    jid = _job(match_score=8.0)
    job = db.get_job(jid)
    assert job["company"] == "Lumen Analytics"
    assert job["state"] == "Found"
    assert job["match_score"] == 8.0
    assert job["date_applied"] == ""


def test_update_state_and_applied_date():
    jid = _job()
    db.update_job(jid, state="Ready")
    assert db.get_job(jid)["state"] == "Ready"
    db.update_job(jid, state="Applied")
    job = db.get_job(jid)
    assert job["state"] == "Applied"
    assert job["date_applied"] == date.today().isoformat()


def test_rejects_unknown_state_and_fields():
    jid = _job()
    with pytest.raises(ValueError):
        db.update_job(jid, state="Hired!!")
    with pytest.raises(ValueError):
        db.update_job(jid, url="http://evil")  # the URL is the job's identity: not editable


def test_duplicates_by_url_or_company_and_role():
    _job()
    assert db.is_duplicate("http://x/1", "", "")
    assert db.is_duplicate("http://other", "lumen analytics", "backend engineer")
    assert not db.is_duplicate("http://other", "Lumen Analytics", "Data Engineer")


def test_filter_by_state_and_counts():
    a = _job(url="http://x/a")
    _job(url="http://x/b", company="B")
    db.update_job(a, state="Interview")
    assert [j["id"] for j in db.list_jobs("Interview")] == [a]
    assert db.state_counts() == {"Interview": 1, "Found": 1}


def test_notes_append():
    jid = _job()
    db.append_note(jid, "first")
    db.append_note(jid, "second")
    assert db.get_job(jid)["notes"] == "first\nsecond"


def test_spend_is_recorded_per_job_and_run():
    run = db.start_run("hunt")
    jid = _job()
    db.record_llm_call(run_id=run, job_id=jid, stage="tailor", model="m", tokens_in=1000, tokens_out=200, cost=0.01, seconds=2)
    db.record_llm_call(run_id=run, job_id=None, stage="filter", model="m", tokens_in=100, tokens_out=20, cost=0.001, seconds=1)
    job, r = db.get_job(jid), db.get_run(run)
    assert (job["tokens_in"], job["tokens_out"]) == (1000, 200)
    assert job["cost_usd"] == pytest.approx(0.01)
    assert (r["tokens_in"], r["tokens_out"]) == (1100, 220)
    assert db.stage_averages()["tailor"]["tin"] == 1000


def test_seen_postings():
    assert not db.was_seen("http://x/9")
    db.mark_seen("http://x/9", "demo", "C", "R", "skip", "too junior")
    assert db.was_seen("http://x/9")
    assert db.seen_stats() == {"skip": 1}
    db.forget_seen()
    assert not db.was_seen("http://x/9")


def test_csv_export_has_header_and_rows_oldest_first():
    _job(url="http://x/a", company="Alpha, Inc")  # comma must be quoted
    _job(url="http://x/b", company="Beta")
    rows = list(csv.reader(io.StringIO(db.export_csv())))
    assert rows[0] == db.EXPORT_COLUMNS
    assert [r[3] for r in rows[1:]] == ["Alpha, Inc", "Beta"]
    assert "posting_text" not in rows[0]


def test_applied_since_counts_for_pacing():
    jid = _job()
    db.update_job(jid, state="Applied")
    assert db.applied_since("demo", "2000-01-01T00:00:00") == 1
    assert db.applied_since("linkedin", "2000-01-01T00:00:00") == 0


def test_events_and_timings():
    run = db.start_run("hunt")
    first = db.log_event(run, "hello")
    db.log_event(run, "world", "warn")
    assert [e["message"] for e in db.events_since(first)] == ["world"]
    db.record_timing(run, None, "draft", 10)
    db.record_timing(run, None, "draft", 20)
    assert db.average_timing("draft") == 15
    assert db.average_timing("apply") is None
