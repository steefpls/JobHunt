"""End-to-end dry run: a draft-mode hunt on the practice board, then
"Approve & apply" on the fake application site. The model is FakeLLM.
"""
import json

from pypdf import PdfReader

from jobhunt import db, demo_board, hunt, paths, settings

from conftest import assert_secret_free, files_under


def _prepare(demo_server):
    settings.update({
        "demo_board_url": demo_server,
        "scope": {"keywords": ["engineer"], "location": "Singapore", "avoid_words": ["intern"]},
        "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0},
        "apply": {"show_browser": False},
    })


def test_draft_hunt_then_approve_and_apply(demo_server, secrets, candidate, fake_llm, data_dir):
    _prepare(demo_server)
    demo_board.SUBMISSIONS.clear()
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"], "auto_submit": False})
    runner.wait(180)
    run = db.get_run(run_id)
    assert run["status"] == "done", [e["message"] for e in db.recent_events()]

    # exactly one tracker row, ready for approval, nothing submitted
    jobs = db.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["state"] == "Ready"
    assert job["company"] == "Lumen Analytics"
    assert job["match_score"] == 8
    assert job["tokens_in"] > 0  # spend shown per job
    assert demo_board.SUBMISSIONS == []  # draft mode submits nothing

    # one folder with the package, clean names, no stray files
    folders = [p for p in paths.jobs_dir().iterdir() if p.is_dir()]
    assert len(folders) == 1 and folders[0].name == job["folder"]
    names = sorted(p.name for p in folders[0].iterdir())
    assert names == ["cover_letter.html", "cover_letter.pdf", "match_report.md", "posting.pdf", "resume.html", "resume.pdf"]
    for pdf in ("posting.pdf", "resume.pdf", "cover_letter.pdf"):
        text = "".join(p.extract_text() for p in PdfReader(str(folders[0] / pdf)).pages)
        assert len(text) > 100, pdf
    assert "ALEX TAN" in "".join(p.extract_text() for p in PdfReader(str(folders[0] / "resume.pdf")).pages).upper()
    report = (folders[0] / "match_report.md").read_text(encoding="utf-8")
    assert "Tailoring score: 8" in report

    # one fresh request per stage: one filter, one tailor; no apply in draft mode
    stages = [("screen job postings" in r["messages"][0]["content"], "tailor a Harvard" in r["messages"][0]["content"])
              for r in fake_llm.requests]
    assert stages.count((True, False)) == 1 and stages.count((False, True)) == 1
    assert len(fake_llm.requests) == 2

    # approve & apply (what the job page's button does)
    demo_board.SUBMISSIONS.clear()
    runner.start("apply", {"job_id": job["id"]})
    runner.wait(180)
    job = db.get_job(job["id"])
    assert job["state"] == "Applied", job["notes"]
    assert job["date_applied"]
    assert (folders[0] / "apply_confirmation.pdf").exists()
    assert demo_board.SUBMISSIONS[-1]["job_id"] == 1 and demo_board.SUBMISSIONS[-1]["resume_is_pdf"]
    assert db.average_timing("draft") is not None and db.average_timing("apply") is not None

    # no secret anywhere: prompts, event log, tracker, files on disk
    assert_secret_free(fake_llm.prompts_text(), "model requests")
    assert_secret_free(json.dumps(db.recent_events(1000)), "event log")
    assert_secret_free(db.export_csv(), "tracker")
    for f in files_under(data_dir):
        assert_secret_free(f.read_bytes(), str(f))


def test_hunt_skips_avoid_words_and_misfits(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    settings.update({"scope": {"keywords": ["intern", "architect"], "avoid_words": ["intern"]}})
    runner = hunt.Runner()
    runner.start("hunt", {"jobs": 3, "sites": ["demo"]})
    runner.wait(120)
    assert db.list_jobs() == []
    assert db.was_seen(f"{demo_server}/job/2")  # avoid word: skipped without a model call
    assert db.was_seen(f"{demo_server}/job/4")  # model said skip
    assert len(fake_llm.requests) == 1


def test_hunt_stops_cleanly_at_token_cap(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    settings.update({"caps": {"per_run_tokens": 1000}})
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 2, "sites": ["demo"]})
    runner.wait(120)
    run = db.get_run(run_id)
    assert run["status"] == "capped"
    assert "token cap" in run["summary"]
    assert fake_llm.requests == []


def test_missing_profile_is_a_clear_error(demo_server, secrets, fake_llm):
    _prepare(demo_server)
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"]})
    runner.wait(60)
    run = db.get_run(run_id)
    assert run["status"] == "error" and "Profile" in run["summary"]


def test_estimate_shape(demo_server):
    est = hunt.estimate(settings.load(), 10, ["demo", "mycareersfuture"], auto_submit=True)
    assert est["subscription"] is True and est["total_usd"] > 0  # API-price equivalent
    assert not est["over_run_cap"] and not est["over_job_cap"]  # USD caps don't apply to a flat plan
    assert est["total_tokens"] > 0 and est["drafts_per_hour"] > 0 and est["applies_per_hour"] > 0
