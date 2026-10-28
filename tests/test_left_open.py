"""An apply that stops as Needs you in a visible window leaves that window open where it stopped.

Zech lost half-filled applications: the apply browser closed as soon as the agent handed a job back, so he had
to start over. Now the window stays (JobHunt lets go of it), later work gets a fresh browser on a stand-in
profile with his logins while that window holds the real one, and it closes when he closes it or JobHunt quits.
A headless apply has nothing to leave open and closes as before.

Also: the agent following a site's popups and new tabs, and a popup that closes itself not counting as a failed
step. Real headless Chromium against the built-in practice site; nothing touches the internet.
"""
import functools
import os
import signal
import threading
import time

import pytest

from jobhunt import browser as browser_mod
from jobhunt import db, hunt, paths, settings
from jobhunt.applyagent import ApplyAgent
from jobhunt.browser import BrowserSession

from conftest import FakeLLM, find_ref, page_elements, scripted_apply_brain

PDF = b"%PDF-1.4\n% test file\n"


@pytest.fixture(autouse=True)
def stays_headless(monkeypatch):
    """The apply asks for a visible window; the tests' browsers stay headless (the logic is the same)."""
    monkeypatch.setattr(BrowserSession, "set_headless", lambda self, headless, minimized=False: None)
    yield
    for w in list(browser_mod._left_open):  # nothing a test left open outlives it
        if w["pid"]:
            try:
                os.kill(w["pid"], signal.SIGTERM)
            except OSError:
                pass
    wait_for(lambda: not browser_mod.windows_left_open())


def wait_for(cond, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def ready_job(demo_server, data_dir, n):
    folder = f"2026-10-28_Job{n}"
    (data_dir / "jobs" / folder).mkdir(parents=True)
    for name in ("resume.pdf", "cover_letter.pdf"):
        (data_dir / "jobs" / folder / name).write_bytes(PDF)
    return db.add_job(source="demo", url=f"{demo_server}/job/{n}", apply_url=f"{demo_server}/ats/1/login",
                      company=f"Company {n}", role="Backend", folder=folder, state="Ready")


def stops_at_sign_in(user):
    """An agent that hands the job back at the practice site's sign-in page."""
    if page_elements(user)[0] == "Sign in":
        return [("finish", {"status": "needs_you", "summary": "It needs you to sign in."})]
    return scripted_apply_brain(user)


def test_a_needs_you_in_a_window_stays_open_and_the_next_job_gets_a_fresh_browser(
        demo_server, secrets, candidate, fake_llm, data_dir):
    settings.update({"apply": {"show_browser": True}})
    first, second = ready_job(demo_server, data_dir, 1), ready_job(demo_server, data_dir, 2)
    b = BrowserSession(headless=True)
    try:
        b.page.context.add_cookies([{"name": "session", "value": "logged-in", "url": demo_server,
                                     "expires": time.time() + 86400}])  # a login that lasts
        fake_llm.apply_brain = stops_at_sign_in
        task = hunt.Task("hunt", {}, threading.Event())

        assert hunt.apply_job(task, b, first) == "Needs you"
        job = db.get_job(first)
        assert job["reason"] == f"It needs you to sign in. {hunt.LEFT_OPEN}"
        assert hunt.LEFT_OPEN == ("The window is left open where it stopped: finish it there, then press "
                                  "I applied.")
        assert browser_mod.windows_left_open() == [{"job_id": first}]
        main = paths.browser_profile_dir()
        assert browser_mod.held_by_left_open(main)["job_id"] == first
        left = browser_mod._left_open[0]["ctx"]
        page = [p for p in left.pages if not p.is_closed()][-1]
        assert page.url.endswith("/ats/1/login")  # where it stopped, form and all
        # none of the apply's rules stay on it: JobHunt's network guard would refuse this local address
        assert page.evaluate("() => fetch('/not-the-practice-site').then(r => r.status, () => 'blocked')") == 404

        # the same hunt's next job opens a fresh browser (a stand-in profile, with the logins) and goes through
        fake_llm.apply_brain = scripted_apply_brain
        assert hunt.apply_job(task, b, second) == "Applied"
        cookies = b.page.context.cookies()  # (relaunched if the hunt's own window mode differs)
        assert b._launched_on != main and b._standin is not None
        assert any(c["name"] == "session" and c["value"] == "logged-in" for c in cookies)
        assert hunt.LEFT_OPEN not in (db.get_job(second)["reason"] or "")
        assert not page.is_closed()  # the window left open was never touched

        # you close that window: it's forgotten, and the next browser is on your own profile again
        left.new_cdp_session(page).send("Browser.close")
        assert wait_for(lambda: not browser_mod.windows_left_open())
        standin = b._standin
        b.reset()
        b.page.goto(f"{demo_server}/")
        assert b._launched_on == main and not standin.exists()
    finally:
        b.close()


def test_a_headless_apply_closes_as_before(demo_server, secrets, candidate, fake_llm, data_dir):
    settings.update({"apply": {"show_browser": False}})
    jid = ready_job(demo_server, data_dir, 1)
    fake_llm.apply_brain = stops_at_sign_in
    b = BrowserSession(headless=True)
    try:
        assert hunt.apply_job(hunt.Task("apply", {}, threading.Event()), b, jid) == "Needs you"
    finally:
        b.close()
    assert db.get_job(jid)["reason"] == "It needs you to sign in."
    assert browser_mod.windows_left_open() == []


def test_the_window_outlives_its_task_and_closes_when_jobhunt_lets_its_driver_go(
        demo_server, secrets, candidate, fake_llm, data_dir, monkeypatch):
    """Approve & apply runs on a thread of its own that ends with the task: the window must stay, a later
    task (another thread) must still get a browser, and ending the window's Playwright driver (as when
    JobHunt quits) closes it."""
    settings.update({"apply": {"show_browser": True}})
    first, second = ready_job(demo_server, data_dir, 1), ready_job(demo_server, data_dir, 2)
    results = {}

    def apply(jid, brain):
        fake_llm.apply_brain = brain
        results[jid] = hunt.run_apply(hunt.Task("apply", {"job_id": jid}, threading.Event()))
    for jid, brain in ((first, stops_at_sign_in), (second, scripted_apply_brain)):
        t = threading.Thread(target=apply, args=(jid, brain))
        t.start()
        t.join(120)
    assert results == {first: f"Job {first}: Needs you", second: f"Job {second}: Applied"}
    assert browser_mod.windows_left_open() == [{"job_id": first}]
    left = browser_mod._left_open[0]
    assert browser_mod._running(left["watch"])

    os.kill(left["pid"], signal.SIGTERM)  # JobHunt quitting ends its Playwright driver...
    assert wait_for(lambda: not browser_mod._running(left["watch"]))  # ...and that ends the window's browser
    assert browser_mod.windows_left_open() == []


def test_accounts_and_restart_wait_for_a_window_left_open(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from jobhunt.web import app as web
    jid = db.add_job(source="demo", url="https://x.example/1", company="Acme", role="R", folder="f",
                     state="Needs you")
    monkeypatch.setattr(browser_mod, "held_by_left_open", lambda d: {"job_id": jid})
    client = TestClient(web.create_app())
    for url, data in (("/accounts/browser", {"site": "indeed"}), ("/accounts/check", {}),
                      ("/update/restart", {"next": "/"})):
        r = client.post(url, data=data, headers={"origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303, url
        assert "Finish%20the%20application%20in%20the%20window%20JobHunt%20left%20open%20for%20Acme" \
               in r.headers["location"], url
    assert web._login_proc is None


# ── popups and new tabs ───────────────────────────────────────────────

OPENER = """<!DOCTYPE html><html><head><title>Job</title></head><body>
<button onclick="window.open('/demo/popup-apply')">Apply now</button></body></html>"""
POPUP = """<!DOCTYPE html><html><head><title>Apply with Indeed</title></head><body>
<button>Apply with Indeed</button></body></html>"""


@pytest.fixture(scope="module")
def popup_site():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    from conftest import free_port
    app = FastAPI()
    app.get("/demo/opener")(lambda: HTMLResponse(OPENER))
    app.get("/demo/popup-apply")(lambda: HTMLResponse(POPUP))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/demo"
    server.should_exit = True


def test_the_agent_follows_a_new_apply_tab_and_a_popup_closing_itself_is_not_a_failure(
        popup_site, data_dir, secrets):
    from jobhunt.llm import LLMClient, Meter
    b = BrowserSession(headless=True)
    seen = []

    def brain(user):
        title, els = page_elements(user)
        seen.append(title)
        if title == "Apply with Indeed" and "Apply with Indeed" not in [s for s in seen[:-1]]:
            b._page.close()  # the site closes its popup just as the agent clicks in it (as on Indeed)
            return [("click", {"element": find_ref(els, "button", "Apply with Indeed")})]
        if title == "Job" and len(seen) == 1:
            return [("click", {"element": find_ref(els, "button", "Apply now")})]
        return [("finish", {"status": "needs_you", "summary": "Done looking."})]
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    fake = FakeLLM(apply_brain=brain)
    agent = ApplyAgent(browser=b, llm=LLMClient(s, "sk-test", meter=Meter.from_settings(s), transport=fake.transport),
                       settings=s, contact={"name": "Alex Tan"}, profile_text="Python.",
                       job={"id": None, "role": "Engineer", "company": "Acme", "source": "indeed"},
                       folder=data_dir, sleep=lambda s: None)
    try:
        result = agent.run(f"{popup_site}/opener")
        assert seen == ["Job", "Apply with Indeed", "Job"]  # into the new tab, then back when it closed
        log = "\n".join(result.log)
        assert "it opened a new tab: " + f"{popup_site}/popup-apply" in log
        assert "-> that window closed itself" in log and "FAILED" not in log
        assert f"that window closed; back in the one before it: {popup_site}/opener" in log
        # the run took its network rule and listeners off the browser when it ended
        ctx = b._ctx
        assert ctx._impl_obj.listeners("request") == [] and not ctx._impl_obj._routes
    finally:
        b.close()
