"""Achievements and the other small surprises: worked out from real data, never near the AI or a document."""
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import achievements, db, paths, profile
from jobhunt.web.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update  # noqa: E402

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def job(n: int, source: str = "jobstreet", **kw) -> int:
    return db.add_job(source=source, url=f"https://x/{n}", company=f"Co{n}", role="Engineer", **kw)


def hunt_run(started: str, status: str = "done", sites: str = '["jobstreet"]') -> None:
    run = db.start_run("hunt", '{"sites": ' + sites + '}')
    db._exec("UPDATE runs SET started_at = ? WHERE id = ?", (started, run))
    db.finish_run(run, status, "")


def test_counts_come_from_the_tracker_and_the_state_history():
    a, b, c, d = job(1), job(2), job(3), job(4, source="linkedin")
    for j in (a, b, c, d):
        db.update_job(j, state="Ready")
    db.update_job(a, state="Applied")                    # JobHunt applied
    db.needs_you(b, "screening:notice_period", "asks notice period")
    db.update_job(b, state="Applied", by="you")          # you finished it yourself
    db.needs_you(c, "broke", "broke")
    db.update_job(c, state="Applied")                    # Try again worked
    db.update_job(c, state="Interview", by="you")
    db.update_job(c, state="Rejected", by="you")         # still counts as an interview
    db.update_job(d, state="Rejected", by="you")
    practice = job(5, source="demo")
    db.update_job(practice, state="Applied")             # practice board: pretend, doesn't count
    v = achievements.values()
    assert v["out_the_door"] == 3 and v["hands_on"] == 1 and v["second_wind"] == 1
    assert v["foot_in_door"] == 1 and v["thick_skin"] == 2 and v["globetrotter"] == 1
    assert v["big_day"] == 3 and v["same_day"] == 3


def test_ghost_town_and_comeback():
    old = job(1, state="Applied")
    db._exec("UPDATE jobs SET date_applied = ? WHERE id = ?", ((date.today() - timedelta(days=31)).isoformat(), old))
    for n in range(2, 13):
        db.update_job(job(n), state="Rejected", by="you")
    db.update_job(job(13), state="Interview", by="you")
    v = achievements.values()
    assert v["ghost_town"] == 1 and v["comeback"] == 11 and v["thick_skin"] == 11


def test_when_you_hunt():
    today = date.today()
    for i in range(4):  # four days in a row, ending today
        hunt_run(f"{(today - timedelta(days=3 - i)).isoformat()}T{['02', '06', '12', '20'][i]}:30:00")
    hunt_run((today - timedelta(days=40)).isoformat() + "T09:00:00")
    hunt_run(today.isoformat() + "T10:00:00", status="error")
    v = achievements.values()
    assert v["streak"] == 4 and v["tenure"] == 40 and v["the_hunt"] == 5
    assert v["night_owl"] == 1 and v["early_bird"] == 1 and v["lunch_break"] == 1


def test_tiers_and_lower_is_better():
    k = achievements.BY_KEY["thick_skin"]
    assert achievements.tiers_earned(k, 0) == 0 and achievements.tiers_earned(k, 5) == 2
    pig = achievements.BY_KEY["penny_pincher"]
    assert achievements.tiers_earned(pig, None) == 0 and achievements.tiers_earned(pig, 0.008) == 2
    assert achievements.milestone(pig, 1) == "$0.010 or less average per package (over 10+ packages)"
    assert achievements.milestone(achievements.BY_KEY["out_the_door"], 0) == "First one out the door!"
    assert achievements.milestone(achievements.BY_KEY["snoop"], 0) == "Found this page"
    assert achievements.fmt(achievements.BY_KEY["token_tycoon"], 10_000_000) == "10M"


def test_first_check_is_one_quiet_summary_then_each_new_tier_pops_up():
    for n in range(5):
        db.update_job(job(n), state="Rejected", by="you")
    first = achievements.check()
    assert len(first) == 1 and "already earned 2 achievements" in first[0]["text"]  # Thick Skin Bronze + Silver
    assert achievements.check() == []
    db.update_job(job(99), state="Applied", by="you")
    new = achievements.check()
    assert new == [{"icon": "📮", "title": "Out the Door · Bronze", "text": "First one out the door!"},
                   {"icon": "⚡", "title": "Same-Day Delivery · Bronze",
                    "text": "1 job applied to within a day of being found"}]
    assert achievements.check() == [] and achievements.earned_count() == 4


def test_the_page_is_secret_until_found(client):
    assert "/achievements" not in client.get("/").text  # nothing earned: no trophy in the sidebar
    page = client.get("/achievements").text
    assert "You found the secret page" in page and "Snoop · Unlocked" in page  # visiting earns Snoop
    assert page.count("???") == len([k for k in achievements.KINDS if k.group == "Hidden"]) - 1
    assert "A code older than you are." in page
    assert '🏆 1 achievement</a>' in client.get("/hunt").text


def test_flags_can_only_be_the_ones_the_page_sets(client):
    for name in ("snoop", "nonsense", "out_the_door"):
        assert client.post("/achievements/flag", data={"name": name}, headers=SAME_ORIGIN).status_code == 204
    assert achievements.load_state().get("flags", {}) == {}
    client.post("/achievements/flag", data={"name": "old_school"}, headers=SAME_ORIGIN)
    assert "old_school" in achievements.load_state()["flags"]
    assert client.post("/achievements/flag", data={"name": "old_school"}).status_code == 403  # not from the page


def test_an_offer_brings_confetti(client):
    jid = job(1, state="Applied")
    r = client.post(f"/jobs/{jid}/state", data={"state": "Offer"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert "party=offer" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert 'data-party="offer"' in page and "Congratulations!" in page
    assert 'data-party=""' in client.get("/?party=<script>").text  # only the two known parties


def test_rejection_decoder_only_on_rejected_jobs(client):
    jid = job(1, state="Applied")
    assert "Decode this rejection" not in client.get(f"/jobs/{jid}").text
    db.update_job(jid, state="Rejected", by="you")
    page = client.get(f"/jobs/{jid}").text
    assert "Decode this rejection" in page and "Someone&#39;s cousin applied." in page


def test_the_empty_log_has_a_dog(client):
    assert 'id="logArt"' in client.get("/hunt").text
    db.log_event(None, "Hunt started")
    assert 'id="logArt"' not in client.get("/hunt").text


def test_eggs_script_has_the_secret_ways_in(client):
    js = client.get("/static/eggs.js").text
    assert "'ArrowUp', 'ArrowUp', 'ArrowDown'" in js and "clicks >= 5" in js and "prefers-reduced-motion" in js
    assert "go to sleep" in js


def test_nothing_fun_reaches_the_ai_or_a_document():
    pkg = Path(achievements.__file__).parent
    for name in ("prompts.py", "llm.py", "render.py", "applyagent.py", "hunt.py", "extract.py", "sites.py"):
        assert not re.search(r"^\s*(from|import) .*\bachievements\b", (pkg / name).read_text(encoding="utf-8"), re.M), name


def test_an_older_tracker_gets_a_state_history(data_dir):
    import sqlite3
    a = job(1)
    b = job(2)
    con = sqlite3.connect(paths.db_path())
    con.execute("DELETE FROM job_states")
    con.execute("UPDATE jobs SET state = 'Rejected', date_applied = '2026-10-01', updated_at = '2026-10-05T10:00:00'"
                " WHERE id = ?", (a,))
    con.execute("UPDATE jobs SET state = 'Ready', updated_at = '2026-10-06T10:00:00' WHERE id = ?", (b,))
    con.commit()
    con.close()
    db.init()
    rows = db.query("SELECT job_id, ts, state, by FROM job_states ORDER BY job_id, ts")
    assert [(r["job_id"], r["state"], r["by"]) for r in rows] == [(a, "Applied", "earlier"), (a, "Rejected", "earlier"),
                                                                  (b, "Ready", "earlier")]
    assert rows[2]["ts"] == "2026-10-06T10:00:00"
    v = achievements.values()
    assert v["out_the_door"] == 1 and v["thick_skin"] == 1


def test_updates_are_counted(tmp_path, monkeypatch):
    d = tmp_path / "JobHunt"
    d.mkdir()
    (d / update.VERSION_FILE).write_text("1" * 40, encoding="utf-8")
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "port_in_use", lambda port: False)
    monkeypatch.setattr(update, "download", lambda sha: b"zip")
    monkeypatch.setattr(update, "apply_archive", lambda archive, app_dir: ["x"])
    monkeypatch.setattr(update, "_archive_digest", lambda a, r: "")
    monkeypatch.setattr(update, "changes", lambda old, new: [])
    update.write_status(d, updated={"from": "0" * 40, "to": "1" * 40})  # an update made by the previous updater
    for sha in ("2" * 40, "3" * 40):
        monkeypatch.setattr(update, "latest_commit", lambda sha=sha: sha)
        update.main(d)
    assert update.read_status(d)["updates"] == 3


def test_opening_jobhunt_twice_is_noticed(monkeypatch):
    from jobhunt import __main__ as launcher
    seen = []
    monkeypatch.setattr(launcher, "port_free", lambda port: False)
    monkeypatch.setattr(launcher, "jobhunt_running", lambda port: True)
    monkeypatch.setattr(launcher, "seen_again", lambda port: seen.append(port))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["jobhunt"])
    launcher.main()
    assert seen == [8765]


def test_every_kind_reads_right_for_one_and_many():
    for k in achievements.KINDS:
        assert achievements.unit(k, 2) == k.unit
        one = achievements.unit(k, 1)
        if k.key in achievements.ONE:
            assert one.split()[0].rstrip("s") == k.unit.split()[0].rstrip("s") or one.split()[0] in k.unit, k.key
    assert achievements.unit(achievements.BY_KEY["the_hunt"], 1) == "hunt finished"
    assert achievements.unit(achievements.BY_KEY["speed_reader"], 1) == "posting judged"
