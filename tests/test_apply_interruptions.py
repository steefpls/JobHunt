"""Two ways an apply can stop early that a person has to see plainly.

- You close the apply browser window while the agent is working: the job is
  "Needs you" (interrupted, check the site), never an "Unexpected error", and
  the next job in the same run gets a fresh browser.
- The site shows a human check (Cloudflare's "Just a moment...", Indeed's
  security check): code spots it, waits a little for it to clear, then stops
  as "Needs you" (verify) without asking the model anything.

Uses a real headless Chromium. Pages on made-up https sites are served by a
page route, so nothing here touches the internet.
"""
import functools
import threading

import pytest
from playwright._impl._errors import TargetClosedError

from jobhunt import applyagent, db, hunt, secrets_store, settings, sites
from jobhunt.applyagent import ApplyAgent
from jobhunt.browser import BrowserSession
from jobhunt.llm import LLMClient, Meter

from conftest import FakeLLM, scripted_apply_brain

PDF = b"%PDF-1.4\n% test file\n"
CHECK_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
INDEED_URL = "https://sg.indeed.com/viewjob?jk=0f1e2d3c4b5a6978"


@pytest.fixture
def browser(data_dir):
    b = BrowserSession(headless=True)
    yield b
    b.close()


@pytest.fixture
def job_folder(data_dir):
    folder = data_dir / "jobs" / "2026-10-16_Dell_Engineer"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(PDF)
    (folder / "cover_letter.pdf").write_bytes(PDF)
    return folder


def close_window(b: BrowserSession) -> None:
    """What happens when you close the browser's window: the whole browser goes away under the app."""
    try:
        b._ctx.new_cdp_session(b.page).send("Browser.close")
    except Exception:
        pass  # the connection can drop before the answer comes back


def serve(b: BrowserSession, url_glob: str, html_for) -> None:
    """Answer requests to a made-up site with html_for(url) (the page route runs before the agent's own)."""
    b.page.route(url_glob, lambda route: route.fulfill(status=200, content_type="text/html",
                                                       body=html_for(route.request.url)))


def make_agent(b, folder, fake, sleep, **kw):
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    client = LLMClient(s, "sk-test", meter=Meter.from_settings(s), transport=fake.transport)
    return ApplyAgent(browser=b, llm=client, settings=s, contact={"name": "Alex Tan"}, profile_text="Python.",
                      job={"id": None, "role": "Engineer", "company": "Dell", "source": "indeed"}, folder=folder,
                      redactor=secrets_store.Redactor.from_store(s), sleep=sleep, **kw)


def never_called(user):
    raise AssertionError("the model must not be asked about a human-check page")


# ── closing the window ────────────────────────────────────────────────

class _GoneContext:
    def close(self):
        raise TargetClosedError("Target page, context or browser has been closed")


def test_switching_window_mode_after_the_window_was_closed_does_not_raise():
    b = BrowserSession(headless=False, minimized=False)
    b._ctx, b._page, b._tabs = _GoneContext(), object(), {"indeed": object()}
    b.set_headless(False, True)  # what apply_job's finally does: it used to raise TargetClosedError here
    assert (b._ctx, b._page, b._tabs) == (None, None, {})
    assert (b.headless, b.minimized) == (False, True)


def test_a_really_closed_browser_is_recognised_and_replaced(browser, demo_server):
    browser.page.goto(f"{demo_server}/")
    close_window(browser)
    with pytest.raises(Exception) as caught:
        browser.page.goto(f"{demo_server}/")
    assert browser.is_closed_error(caught.value)
    assert not browser.is_closed_error(ValueError("something else"))
    browser.set_headless(True, True)  # a window-mode switch on a dead browser: no error
    browser.page.goto(f"{demo_server}/")  # and the next use starts a fresh one
    assert "/demo/" in browser.page.url


def _apply_ready_job(demo_server, data_dir, n):
    folder = f"2026-10-16_Job{n}"
    (data_dir / "jobs" / folder).mkdir(parents=True)
    for name in ("resume.pdf", "cover_letter.pdf"):
        (data_dir / "jobs" / folder / name).write_bytes(PDF)
    return db.add_job(source="demo", url=f"{demo_server}/job/{n}", apply_url=f"{demo_server}/ats/1/login",
                      company=f"Company {n}", role="Backend", folder=folder, state="Ready")


def test_closing_the_window_mid_apply_needs_you_and_the_next_job_gets_a_fresh_browser(
        demo_server, browser, secrets, candidate, fake_llm, data_dir, monkeypatch):
    settings.update({"apply": {"show_browser": False}})  # stays headless; set_headless changes nothing
    first, second = _apply_ready_job(demo_server, data_dir, 1), _apply_ready_job(demo_server, data_dir, 2)
    closed = []

    def sleep(seconds):  # the agent's `wait` (5 s; settling sleeps 0 s): you close the window while it waits
        if seconds >= 1 and not closed:
            closed.append(True)
            close_window(browser)

    def brain(user):  # waits on a page with nothing to click, as on Zech's Indeed page
        return [("wait", {"seconds": 5})] if not closed else scripted_apply_brain(user)
    fake_llm.apply_brain = brain
    monkeypatch.setattr(hunt, "ApplyAgent", functools.partial(ApplyAgent, sleep=sleep))
    task = hunt.Task("hunt", {}, threading.Event())

    assert hunt.apply_job(task, browser, first) == "Needs you"
    assert len(fake_llm.requests) == 1  # it closed during step 1's wait, not before
    job = db.get_job(first)
    assert job["reason_kind"] == "interrupted"
    assert job["reason"] == ("The browser window was closed before the application finished. "
                             "Check the site to see whether it was sent.")
    assert hunt.needs_you_fix(job)["retry"] is False  # it may have been sent: look first
    # the same run's next job opens a fresh browser and goes through
    assert hunt.apply_job(task, browser, second) == "Applied"
    # a window closed between jobs (say during the hunt's own browsing) is replaced too
    close_window(browser)
    third = _apply_ready_job(demo_server, data_dir, 3)
    assert hunt.apply_job(task, browser, third) == "Applied"
    logged = "\n".join(e["message"] for e in db.events_since(0) if e["run_id"] == task.run_id)
    assert "broke" not in logged and "Unexpected" not in logged


def test_other_apply_breakage_is_still_reported_as_broken(browser, secrets, fake_llm, data_dir, monkeypatch):
    settings.update({"apply": {"show_browser": False}})
    jid = db.add_job(source="demo", url="https://x.example/1", company="A", role="R", folder="f", state="Ready")

    class Agent:
        def __init__(self, **kw):
            pass

        def run(self, url):
            raise RuntimeError("disk full")
    monkeypatch.setattr(hunt, "ApplyAgent", Agent)
    assert hunt.apply_job(hunt.Task("apply", {}, threading.Event()), browser, jid) == "Needs you"
    assert db.get_job(jid)["reason_kind"] == "broke"


# ── human checks ──────────────────────────────────────────────────────

def test_an_indeed_human_check_stops_as_needs_you_without_model_tokens(browser, job_folder, secrets):
    serve(browser, "https://sg.indeed.com/**", lambda url: CHECK_PAGE)
    fake, slept = FakeLLM(apply_brain=never_called), []
    result = make_agent(browser, job_folder, fake, slept.append).run(INDEED_URL)
    assert result.status == "needs_you" and result.kind == "verify"
    assert fake.requests == []
    assert sum(slept) == applyagent.HUMAN_CHECK_WAIT_S  # it gave the check its chance to clear
    assert "human check" in result.summary and "Just a moment..." in result.summary
    assert sites.INDEED_CHECK_STEPS in result.summary and "Try again" in result.summary
    # and the job page offers the way out: pass the check on Accounts, then Try again
    fix = hunt.needs_you_fix({"reason_kind": "verify", "source": "indeed", "apply_url": INDEED_URL,
                              "reason": result.summary})
    assert fix["retry"] is True and fix["fix"][1] == "/accounts#sitelogins"
    # a captcha or code the model met on the employer's own site is still yours to finish
    fix = hunt.needs_you_fix({"reason_kind": "verify", "source": "indeed", "apply_url": INDEED_URL,
                              "reason": "The careers site sent a verification code to your email."})
    assert fix["retry"] is False and fix["fix"] is None


def test_a_check_on_another_site_says_to_finish_it_yourself(browser, job_folder, secrets):
    serve(browser, "https://careers.example.com/**",
          lambda url: "<html><head><title>Verify you are human</title></head><body>hi</body></html>")
    fake = FakeLLM(apply_brain=never_called)
    result = make_agent(browser, job_folder, fake, lambda s: None).run("https://careers.example.com/apply")
    assert result.status == "needs_you" and result.kind == "verify" and fake.requests == []
    assert "Indeed" not in result.summary and "finish it yourself" in result.summary
    fix = hunt.needs_you_fix({"reason_kind": "verify", "source": "indeed",
                              "apply_url": "https://careers.example.com/apply", "reason": result.summary})
    assert fix["retry"] is False


def test_a_check_that_clears_by_itself_lets_the_agent_carry_on(browser, job_folder, secrets):
    serve(browser, "https://sg.indeed.com/**", lambda url: CHECK_PAGE)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:  # the check passes and the real page shows
            browser.page.set_content("<html><head><title>Apply - Dell</title></head><body><button>Apply now</button>"
                                     "</body></html>")
    prompts = []

    def brain(user):
        prompts.append(user)
        return [("finish", {"status": "needs_you", "summary": "Saw the apply page."})]
    fake = FakeLLM(apply_brain=brain)
    result = make_agent(browser, job_folder, fake, sleep).run(INDEED_URL)
    assert result.summary == "Saw the apply page." and result.kind == ""
    assert len(prompts) == 1 and "Apply now" in prompts[0] and "Just a moment" not in prompts[0]


def test_stop_during_the_wait_stops_the_run(browser, job_folder, secrets):
    serve(browser, "https://sg.indeed.com/**", lambda url: CHECK_PAGE)
    stop = threading.Event()
    fake = FakeLLM(apply_brain=never_called)
    slept = []

    def sleep(seconds):  # you press Stop during the check's wait (settling after the first page sleeps 0 s)
        slept.append(seconds)
        if seconds == 1:
            stop.set()
    result = make_agent(browser, job_folder, fake, sleep, stop_event=stop).run(INDEED_URL)
    assert result.status == "stopped" and result.summary == "Stopped by you." and fake.requests == []
    assert slept.count(1) == 1  # stopped after one poll, not 15


@pytest.mark.parametrize("looks, outcome", [
    (["Just a moment...", None, None, ""], "cleared"),  # unreadable while it redirects, then the real page
    (["Just a moment..."] + [None] * 15, "verify"),  # never readable again: not taken as cleared
])
def test_an_unreadable_page_during_the_wait_is_not_taken_as_cleared(browser, job_folder, secrets, looks, outcome):
    browser.page.set_content("<title>Apply</title>")
    fake = FakeLLM(apply_brain=lambda user: [("finish", {"status": "needs_you", "summary": "Saw the page."})])
    agent = make_agent(browser, job_folder, fake, lambda s: None)
    seen = iter(looks)
    agent._human_check_title = lambda: next(seen, "")
    stopped = agent._wait_out_human_check(1)
    if outcome == "cleared":
        assert stopped is None
    else:
        assert stopped.kind == "verify" and "Just a moment..." in stopped.summary


def test_human_check_titles_come_from_the_site_block_list():
    assert sites.is_human_check("Just a moment...") and sites.is_human_check("Security Check - Indeed.com")
    assert not sites.is_human_check("Apply - Dell Technologies") and not sites.is_human_check("")
    with pytest.raises(sites.SiteBlocked):  # the hunt's own check still uses the same list
        sites.check_blocked(CHECK_PAGE)
