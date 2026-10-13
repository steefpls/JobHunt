"""The apply agent against the practice application site, with a scripted model.

Uses a real (headless) Chromium. Checks that passwords are typed by code and
never seen by the model, that type_secret's rules hold, and that "Applied"
needs a confirmation that is really on the page.
"""
import threading

import pytest

from jobhunt import demo_board, hunt, secrets_store, settings
from jobhunt.applyagent import ApplyAgent, ToolError
from jobhunt.browser import BrowserSession
from jobhunt.llm import LLMClient, Meter

from conftest import FakeLLM, LOGIN_PASSWORD, LOGIN_USER, assert_secret_free, find_ref, page_elements

PDF = b"%PDF-1.4\n% test file\n"


@pytest.fixture
def browser(data_dir):
    b = BrowserSession(headless=True)
    yield b
    b.close()


@pytest.fixture
def job_folder(data_dir):
    folder = data_dir / "jobs" / "2026-10-13_Lumen_Backend"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(PDF)
    (folder / "cover_letter.pdf").write_bytes(PDF)
    return folder


def make_agent(browser, folder, fake, **kw):
    kw.setdefault("screening", {"work_auth": "Singapore Citizen"})
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    client = LLMClient(s, "sk-test", meter=Meter.from_settings(s), transport=fake.transport)
    return ApplyAgent(browser=browser, llm=client, settings=s, contact={"name": "Alex Tan", "email": "alex.tan.test@example.com"},
                      profile_text="Singapore Citizen. 5 years Python.", job={"id": None, "role": "Backend", "company": "Lumen"},
                      folder=folder, redactor=secrets_store.Redactor.from_store(s), sleep=lambda s: None, **kw)


def test_full_apply_with_login(demo_server, browser, job_folder, secrets, fake_llm):
    demo_board.SUBMISSIONS.clear()
    demo_board.LOGINS.clear()
    agent = make_agent(browser, job_folder, fake_llm)
    result = agent.run(f"{demo_server}/ats/1/login")
    assert result.status == "submitted", result.summary
    assert (job_folder / "apply_confirmation.pdf").read_bytes()[:4] == b"%PDF"
    # the code typed the real secrets into the site...
    assert demo_board.LOGINS[-1]["email"] == LOGIN_USER
    assert demo_board.LOGINS[-1]["password"] == LOGIN_PASSWORD
    sub = demo_board.SUBMISSIONS[-1]
    assert sub["resume_is_pdf"] and sub["work_auth"] == "Yes" and sub["marketing"] == ""
    # ...but the model never saw them, and neither did the action log
    assert_secret_free(fake_llm.prompts_text(), "model requests")
    assert_secret_free("\n".join(result.log), "action log")
    assert any("typed saved password of login 'default'" in line for line in result.log)


def test_claimed_submit_without_confirmation_needs_you(demo_server, browser, job_folder, secrets):
    def liar(user):
        title, els = page_elements(user)
        if title == "Sign in":
            return [("type_secret", {"element": find_ref(els, "email"), "login": "default", "field": "username"}),
                    ("type_secret", {"element": find_ref(els, "password"), "login": "default", "field": "password"}),
                    ("click", {"element": find_ref(els, "button", "Sign in")})]
        return [("finish", {"status": "submitted", "summary": "done", "confirmation_text": "Thank you for applying!"})]

    result = make_agent(browser, job_folder, FakeLLM(apply_brain=liar)).run(f"{demo_server}/ats/1/login")
    assert result.status == "needs_you"
    assert "no confirmation was found" in result.summary
    assert not (job_folder / "apply_confirmation.pdf").exists()


def test_type_secret_rules(demo_server, browser, job_folder, secrets):
    agent = make_agent(browser, job_folder, FakeLLM())
    browser.page.goto(f"{demo_server}/ats/1/login")
    _, els = page_elements("Current page:\n" + agent.scan())
    email, pw = find_ref(els, "email"), find_ref(els, "password")
    with pytest.raises(ToolError, match="password box"):
        agent.run_tool("type_secret", {"element": email, "login": "default", "field": "password"})
    with pytest.raises(ToolError, match="no saved login"):
        agent.run_tool("type_secret", {"element": pw, "login": "nobody", "field": "password"})
    with pytest.raises(ToolError, match="use type_secret"):
        agent.run_tool("fill", {"element": pw, "text": "guess"})
    # a login limited to other sites can't be typed here
    agent.logins = [{"name": "default", "domains": ["greenhouse.io"]}]
    with pytest.raises(ToolError, match="not allowed on 127.0.0.1"):
        agent.run_tool("type_secret", {"element": pw, "login": "default", "field": "password"})
    with pytest.raises(ToolError):
        agent.run_tool("goto", {"url": "file:///C:/Windows/win.ini"})
    with pytest.raises(ToolError):
        agent.run_tool("upload_file", {"element": email, "file": "../../secrets.txt"})


def test_page_echoing_a_secret_is_redacted_before_the_model_sees_it(browser, job_folder, secrets):
    agent = make_agent(browser, job_folder, FakeLLM())
    browser.page.set_content(f"<h1>Welcome {LOGIN_USER}</h1><p>Your password is {LOGIN_PASSWORD}</p>"
                             f"<input type=text aria-label='Username' value='{LOGIN_USER}'>")
    view = agent.scan()
    assert_secret_free(view, "page view")
    assert "[secret:login:default:password]" in view
    assert view.startswith("<untrusted_web_page>")


def test_singpass_sites_are_handed_over_without_tokens(secrets, fake_llm, data_dir):
    from jobhunt import db
    jid = db.add_job(source="mycareersfuture", url="https://www.mycareersfuture.gov.sg/job/x", company="C", role="R",
                     apply_url="https://www.mycareersfuture.gov.sg/job/x", folder="f", state="Ready")
    task = hunt.Task("apply", {"job_id": jid}, threading.Event())
    assert hunt.apply_job(task, browser=None, job_id=jid) == "Needs you"
    assert "Singpass" in db.get_job(jid)["notes"]
    assert fake_llm.requests == []
