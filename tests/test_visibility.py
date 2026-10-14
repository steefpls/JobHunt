"""Can you tell what state JobHunt is in? Logins, updates, a second launch, tasks that need you."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, hunt, login_check, paths, profile, settings, updates
from jobhunt.web import app as webapp
from jobhunt.web.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update  # noqa: E402

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}
OLD, NEW = "1" * 40, "2" * 40


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An unzipped (non-git) copy of JobHunt, as Zech has it, at version OLD."""
    d = tmp_path / "JobHunt"
    d.mkdir()
    (d / update.VERSION_FILE).write_text(OLD, encoding="utf-8")
    monkeypatch.setattr(paths, "APP_DIR", d)
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "port_in_use", lambda port: False)
    return d


# ── logins ────────────────────────────────────────────────────────────

def test_sign_in_pages_are_recognised():
    assert login_check.looks_like_login("https://login.seek.com/login?state=x", ["login.seek.com"])
    assert login_check.looks_like_login("https://secure.indeed.com/auth?continue=x", ["secure.indeed.com"])
    assert login_check.looks_like_login("https://www.linkedin.com/login/?session_redirect=x")
    assert not login_check.looks_like_login("https://sg.jobstreet.com/my-activity/saved-jobs", ["login.seek.com"])
    assert not login_check.looks_like_login("https://example.com/authors/jane")


def test_linkedin_sign_in_cookie():
    now = 1_000_000.0
    signed_in = [{"name": "li_at", "domain": ".www.linkedin.com", "value": "x", "expires": now + 60}]
    assert login_check.judge_cookie(signed_in, "linkedin.com", "li_at", now) == "in"
    assert login_check.judge_cookie([{**signed_in[0], "expires": now - 60}], "linkedin.com", "li_at", now) == "out"
    assert login_check.judge_cookie([{"name": "JSESSIONID", "domain": ".linkedin.com", "value": "x"}],
                                    "linkedin.com", "li_at", now) == "out"


def test_which_account_you_are_logged_in_as():
    page = "Contact support@indeed.com\nJane Tan\njane.tan@gmail.com\n+65 9123 4567"
    assert login_check.email_in_text(page, ["indeed.com"]) == "jane.tan@gmail.com"
    assert login_check.email_in_text("Help: help@sg.jobstreet.com", ["jobstreet.com", "seek.com"]) == ""
    assert login_check.email_in_text("", ["indeed.com"]) == ""
    assert login_check.linkedin_name({"miniProfile": {"firstName": "Jane", "lastName": "Tan"}}) == "Jane Tan"
    assert login_check.linkedin_name({"data": {}, "included": [{"$type": "x"}, {"firstName": "Jane", "lastName": ""}]}) == "Jane"
    assert login_check.linkedin_name({"status": 401}) == "" and login_check.linkedin_name(None) == ""


def test_accounts_page_shows_each_sites_login_state(client):
    assert client.get("/accounts").text.count("Not checked yet") == len(webapp.LOGIN_SITES)
    login_check.save_status({"linkedin": {"state": "in", "detail": "", "account": "Jane Tan",
                                          "checked_at": "2026-10-14T10:00:00"},
                             "jobstreet": {"state": "out", "detail": "the site asked to sign in",
                                           "checked_at": "2026-10-14T10:00:00"}})
    page = client.get("/accounts").text
    assert "✓ Logged in" in page and "Not logged in" in page and "Not checked yet" in page
    assert "checked 2026-10-14 10:00" in page and "Log in again" in page
    assert "Logged in</span> as <b>Jane Tan</b>" in page
    assert "Session saved" not in client.get("/static/app.js").text  # no more "saved" without knowing


def test_closing_the_login_window_starts_a_check(client, monkeypatch):
    started = []

    class FakeProc:
        def __init__(self, args, **kw):
            started.append(args)
            self.returncode = None

        def poll(self):
            return self.returncode
    monkeypatch.setattr(webapp.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(webapp, "_check_pending", False)
    monkeypatch.setattr(webapp, "_check_proc", None)
    monkeypatch.setattr(webapp, "_login_proc", None)
    client.post("/accounts/browser", data={"site": "linkedin"}, headers=SAME_ORIGIN)
    assert started[-1][-2:] == ["jobhunt.login_browser", "https://www.linkedin.com/login"]
    assert client.get("/api/login-browser").json() == {"open": True, "checking": True}  # waiting for the window
    webapp._login_proc.returncode = 0  # you closed it
    assert client.get("/api/login-browser").json() == {"open": False, "checking": True}
    assert started[-1][2:] == ["jobhunt.login_check", *webapp.LOGIN_SITES]
    assert "Checking your logins" in client.get("/accounts").text
    r = client.post("/hunt/start", data={"sites": "demo", "jobs": "1"}, headers=SAME_ORIGIN)
    assert "checking your job-site logins" in r.text  # the browser profile is in use for a moment
    webapp._check_proc.returncode = 0
    assert client.get("/api/login-browser").json() == {"open": False, "checking": False}
    n = len(started)
    client.post("/accounts/check", headers=SAME_ORIGIN)
    assert len(started) == n + 1 and started[-1][2] == "jobhunt.login_check"
    webapp._check_proc.returncode = 0


@pytest.mark.parametrize("sites, headless", [(None, False), (["indeed"], False), (["jobstreet", "linkedin"], True)])
def test_the_login_check_opens_a_window_when_it_looks_at_indeed(sites, headless, data_dir, monkeypatch):
    # without a window, Indeed flags the whole browser profile and later hunts meet its human check
    launched = []

    class Ctx:
        def close(self):
            pass

    class PW:
        class chromium:
            @staticmethod
            def launch_persistent_context(path, **kw):
                launched.append(kw)
                return Ctx()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    import playwright.sync_api
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: PW())
    monkeypatch.setattr(login_check, "check_site", lambda ctx, key: {"state": "out", "detail": ""})
    login_check.run(sites)
    assert launched[0]["headless"] is headless
    assert ("--start-minimized" in launched[0]["args"]) is (not headless)


def test_indeed_log_in_also_opens_an_indeed_jobs_tab(client, monkeypatch):
    started = []

    class FakeProc:
        def __init__(self, args, **kw):
            started.append(args)

        def poll(self):
            return None
    monkeypatch.setattr(webapp.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(webapp, "_check_pending", False)
    monkeypatch.setattr(webapp, "_check_proc", None)
    monkeypatch.setattr(webapp, "_login_proc", None)
    client.post("/accounts/browser", data={"site": "indeed"}, headers=SAME_ORIGIN)
    # the human check shows on Indeed's job pages, so you can tick it there yourself
    assert started[-1][-3:] == ["jobhunt.login_browser", "https://secure.indeed.com/account/login", "https://sg.indeed.com/"]


# ── updates ───────────────────────────────────────────────────────────

def test_an_update_records_whats_new(install, monkeypatch):
    monkeypatch.setattr(update, "latest_commit", lambda: NEW)
    monkeypatch.setattr(update, "download", lambda sha: b"zip")
    monkeypatch.setattr(update, "apply_archive", lambda archive, d: ["x"])
    monkeypatch.setattr(update, "_archive_digest", lambda a, r: "")
    monkeypatch.setattr(update, "changes", lambda old, new: ["Accounts: shows whether each site is logged in"])
    update.main(install)
    st = update.read_status(install)
    assert st["check_ok"] is True and st["latest"] == NEW and st["checked_at"]
    assert st["updated"] == {"from": OLD, "to": NEW, "at": st["updated"]["at"],
                             "changes": ["Accounts: shows whether each site is logged in"], "seen": False}
    info = updates.info()
    assert info["version"] == NEW[:7] and not info["available"]
    assert info["news"]["changes"] == ["Accounts: shows whether each site is logged in"]
    updates.mark_news_seen()
    assert updates.info()["news"] is None


def test_a_failed_check_is_recorded_and_the_app_starts_as_is(install, monkeypatch):
    def offline():
        raise OSError("no network")
    monkeypatch.setattr(update, "latest_commit", offline)
    assert update.main(install) == 0
    st = update.read_status(install)
    assert st["check_ok"] is False and "latest" not in st
    assert updates.info()["check_ok"] is False


def test_a_running_jobhunt_is_never_updated_under_it(install, monkeypatch):
    monkeypatch.setattr(update, "port_in_use", lambda port: True)
    monkeypatch.setattr(update, "latest_commit", lambda: pytest.fail("checked while JobHunt was running"))
    assert update.main(install, ["--port", "8765"]) == 0


def test_restart_waits_for_the_old_jobhunt_then_updates(install, monkeypatch):
    waited = []
    monkeypatch.setattr(update, "port_in_use", lambda port: pytest.fail("a restart waits instead"))
    monkeypatch.setattr(update, "wait_for_port", lambda port, timeout=30: waited.append(port) or True)
    monkeypatch.setattr(update, "latest_commit", lambda: OLD)
    update.main(install, ["--restart", "--no-browser", "--port", "9999"])
    assert waited == [9999]


def test_a_copy_updated_by_the_old_updater_still_gets_whats_new(install, monkeypatch):
    """The first update to this version is done by the previous update.py, which kept no notes."""
    monkeypatch.setattr(update, "latest_commit", lambda: OLD)
    asked = []
    monkeypatch.setattr(update, "changes", lambda old, new: asked.append((old, new)) or ["new thing"])
    update.main(install)
    assert asked == [(update.NOTES_BASELINE, OLD)]
    assert updates.info()["news"]["changes"] == ["new thing"]
    update.main(install)  # only once
    assert len(asked) == 1


def test_a_newer_version_while_running_offers_a_restart(install, client, monkeypatch):
    update.write_status(install, checked_at="2026-10-14T09:00:00", check_ok=True, latest=NEW)
    page = client.get("/hunt").text
    assert "A newer version of JobHunt is out" in page and "Restart &amp; update" in page
    assert f"version <b>{OLD[:7]}</b>" in page and "Checked for updates 2026-10-14 09:00" in page
    called = []
    monkeypatch.setattr(updates, "restart", lambda port: called.append(("restart", port)))
    monkeypatch.setattr(updates, "exit_soon", lambda: called.append(("exit",)))
    monkeypatch.setattr(hunt.runner, "thread", type("T", (), {"is_alive": lambda self: True})())
    r = client.post("/update/restart", data={"next": "/hunt"}, headers=SAME_ORIGIN)
    assert "Wait for the current task" in r.text and not called
    monkeypatch.setattr(hunt.runner, "thread", None)
    r = client.post("/update/restart", data={"next": "/hunt"}, headers=SAME_ORIGIN)
    assert "Restarting JobHunt" in r.text and called == [("restart", 8765), ("exit",)]


def test_update_news_shows_until_got_it(install, client):
    update.write_status(install, updated={"from": "0" * 40, "to": OLD, "at": "2026-10-14T09:00:00",
                                          "changes": ["Hunt page lists the jobs that need you"], "seen": False})
    page = client.get("/").text
    assert "JobHunt was updated" in page and "Hunt page lists the jobs that need you" in page
    client.post("/update/seen", data={"next": "/scope"}, headers=SAME_ORIGIN)
    assert "JobHunt was updated" not in client.get("/").text


def test_api_version_identifies_jobhunt(client):
    v = client.get("/api/version").json()
    assert v["app"] == "JobHunt" and v["started"]


def test_restart_opens_run_bat_in_a_new_window(install, monkeypatch):
    seen = {}
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **kw: seen.update(args=args, **kw))
    updates.restart(8765)
    assert seen["args"] == [str(install / "run.bat"), "--restart", "--no-browser", "--port", "8765"]
    assert seen["cwd"] == str(install)


def test_run_bat_hands_its_arguments_to_the_updater():
    bat = (Path(__file__).resolve().parent.parent / "run.bat").read_bytes()
    assert b"update.py %*\r\n" in bat


# ── tasks that need you ──────────────────────────────────────────────

def test_needs_you_jobs_are_listed_on_the_hunt_page_and_dashboard(client):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="Lumen", role="Backend Engineer", state="Needs you")
    db.append_note(jid, "Package ready, waiting for your approval.")
    db.append_note(jid, "The form asks about notice period and your Profile has no answer.")
    hunt_page = client.get("/hunt").text
    assert "Needs you" in hunt_page and "Lumen" in hunt_page and "has no answer" in hunt_page
    assert "Needs you: 1" in client.get("/").text


def test_profile_and_scope_tasks_report_what_finished(fake_llm, secrets, candidate):
    hunt.runner.start("scope")
    hunt.runner.wait(30)
    st = hunt.runner.status()
    assert st["last_kind"] == "scope" and st["last_status"] == "done" and "Scope drafted" in st["last_result"]


def test_draft_from_my_profile_keeps_what_you_typed(client, fake_llm, secrets, candidate, monkeypatch):
    started = []
    monkeypatch.setattr(hunt.runner, "start", lambda kind, params=None: started.append(kind))
    client.post("/scope/draft", data={"keywords": "data engineer\nml engineer", "location": "Singapore",
                                      "avoid_words": "intern", "scope_text": "my own words"}, headers=SAME_ORIGIN)
    s = settings.load()["scope"]
    assert s["keywords"] == ["data engineer", "ml engineer"] and s["location"] == "Singapore"
    assert profile.load_scope_text().strip() == "my own words" and started == ["scope"]
