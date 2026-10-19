"""Why auto-apply kept waiting, retrying and stopping, and the fixes (each found by running the agent on a
JavaScript-built application form like Workday's, and on real Greenhouse and Lever forms, up to their final
button).

- The apply browser's network rule sent every request through Python. Playwright's sync API only answers such a
  request while Python is inside a browser call, so while the agent slept or the model thought, a form built
  by JavaScript stopped loading: the agent saw "Loading...", waited (which waited without loading), and was
  stopped for repeating itself. Now only requests that might be blocked are checked, and pauses keep the
  browser going.
- Ids from an earlier look stayed on fields a form step had hidden, so a click on the next step's "Yes" could
  land on an invisible field and time out, three times over.
- A click that changes the form without a new address left the rest of that turn's actions working from the
  old view; now the agent looks again first.
- Type-to-search dropdowns (Greenhouse), list buttons (Workday), boxes that suggest as you type (Lever's
  location) and long dropdowns (a list of universities) couldn't be answered; now select_option and fill can.
- Lever puts a question beside its field, so its Yes/No choices showed no question (and the screening rule
  couldn't see what they answered).
- A captcha box stops the run as Needs you (verify), and so do turns that leave the page exactly as it was.
- An AI provider failure hands the job to Needs you (not Error), a long "retry after" stops at once, and
  pacing says how long it waits.

Uses a real headless Chromium and a local test site under /demo (the apply browser lets that through).
"""
import functools
import json
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest

from jobhunt import applyagent, db, hunt, llm, secrets_store, settings
from jobhunt.applyagent import ApplyAgent, NeedsHuman, ToolError, request_allowed
from jobhunt.browser import BrowserSession
from jobhunt.llm import LLMClient, Meter, RateLimited

from conftest import FakeLLM, find_ref, free_port, page_elements

PDF = b"%PDF-1.4\n% test file\n"

# ── a little test site ────────────────────────────────────────────────

LOADING = """<!DOCTYPE html><html><head><title>Careers - Apply</title>
<style>.step{display:none}.step.on{display:block}</style></head><body><div id="app">Loading...</div><script>
async function boot() {
  await new Promise(r => setTimeout(r, 800));  // quiet first, so a short settle can't wait it all out
  for (let i = 0; i < 6; i++) await (await fetch('/demo/t/api?n=' + i)).text();  // one request after another
  document.getElementById('app').innerHTML =
    '<div class="step on" id="s1"><label for="fn">First name</label><input id="fn">' +
    '<label for="ln">Last name</label><input id="ln"><button type="button" id="n1">Continue</button></div>' +
    '<div class="step" id="s2"><fieldset><legend>Are you legally authorised to work in Singapore?</legend>' +
    '<label><input type="radio" name="wa" value="yes"> Yes</label><label><input type="radio" name="wa" value="no"> No</label>' +
    '</fieldset><button type="button" id="n2">Continue</button></div>' +
    '<div class="step" id="s3"><button type="button" id="sub">Submit application</button></div>' +
    '<div class="step" id="s4"><h2>Application received</h2><p>Thank you for applying.</p></div>';
  const show = n => { document.querySelectorAll('.step').forEach(s => s.classList.remove('on'));
                      document.getElementById('s' + n).classList.add('on'); };
  document.getElementById('n1').onclick = async () => { await fetch('/demo/t/api?n=save'); show(2); };
  document.getElementById('n2').onclick = () => { if (document.querySelector('input[name=wa]:checked')) show(3); };
  document.getElementById('sub').onclick = async () => { await fetch('/demo/t/api?n=submit'); show(4); };
}
boot();
</script></body></html>"""

# Greenhouse's type-to-search dropdown, as react-select draws it: what you type is thrown away unless you pick
# one of the choices; the chosen one shows beside the box, not in it.
COMBO = """<!DOCTYPE html><html><head><title>Job Application</title></head><body>
<label id="q-label" for="q">Are you eligible to work in the location this job post is posted in?*</label>
<div class="select__control"><div class="select__value-container"><div class="select__placeholder" id="ph">Select...</div>
<div class="select__single-value" id="sv"></div><div class="select__input-container">
<input id="q" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-labelledby="q-label"></div></div></div>
<input required tabindex="-1" aria-hidden="true" value="" style="opacity:0;width:4px;height:4px" name="q_req">
<div id="menu" role="listbox"></div>
<script>
const q = document.getElementById('q'), menu = document.getElementById('menu');
const all = ['Yes', 'No'];
const draw = () => { const t = q.value.toLowerCase();
  menu.innerHTML = all.filter(o => o.toLowerCase().includes(t)).map(o => '<div role="option">' + o + '</div>').join(''); };
q.addEventListener('focus', draw); q.addEventListener('input', draw);
menu.addEventListener('mousedown', e => { if (e.target.getAttribute('role') !== 'option') return; e.preventDefault();
  document.getElementById('sv').textContent = e.target.textContent; document.getElementById('ph').textContent = '';
  q.value = ''; menu.innerHTML = ''; });
q.addEventListener('blur', () => { q.value = ''; menu.innerHTML = ''; });
</script></body></html>"""

# Lever: the location box only searches on a key press and lists plain <div>s; the question beside each field;
# a long list of schools.
LEVER = """<!DOCTYPE html><html><head><title>Lever application</title></head><body><ul>
<li class="application-question"><label><div class="application-label">Current location</div>
<div class="application-field"><input class="location-input" id="location-input" name="location" type="text">
<div class="dropdown-results"></div></div></label></li>
<li class="application-question"><div class="application-label"><div class="text">Are you legally authorized to work in
the country for which you are applying?</div></div><div class="application-field"><ul>
<li><label><input type="radio" name="cards[a][field0]" value="Yes">Yes</label></li>
<li><label><input type="radio" name="cards[a][field0]" value="No">No</label></li></ul></div></li>
<li class="application-question"><div class="application-label"><div class="text">Why do you want to join us?</div></div>
<div class="application-field"><textarea name="cards[b][field0]"></textarea></div></li>
<li class="application-question"><div class="application-label"><div class="text">University</div></div>
<div class="application-field"><select name="cards[c][field0]"><option>Click here</option>SCHOOLS</select></div></li>
</ul><script>
const box = document.getElementById('location-input'), res = document.querySelector('.dropdown-results');
const places = ['Singapore, SGP', 'Singapore', 'Singapore, MI, USA'];
box.addEventListener('keyup', () => { const t = box.value.toLowerCase();
  res.innerHTML = places.filter(p => t && p.toLowerCase().startsWith(t)).map(p => '<div class="dropdown-location">' + p + '</div>').join(''); });
res.addEventListener('click', e => { box.value = e.target.textContent; res.innerHTML = ''; });
</script></body></html>""".replace("SCHOOLS", "".join(f"<option>School number {i}</option>" for i in range(60))
                                   + "<option>National University of Singapore - NUS</option>")

# A box that throws away whatever is typed into it, and a form whose Next complains (a new complaint each time).
CLEARS = """<!DOCTYPE html><html><head><title>Stubborn form</title></head><body>
<label for="city">City</label><input id="city" type="text"><button type="button">Next</button>
<script>const c = document.getElementById('city');
c.addEventListener('input', () => setTimeout(() => { c.value = ''; }, 50));</script></body></html>"""
COMPLAINS = """<!DOCTYPE html><html><head><title>Picky form</title></head><body>
<label for="nm">Name</label><input id="nm" type="text"><div id="err" role="alert"></div>
<button type="button" id="nx">Next</button><div id="done"></div><script>
let tries = 0;
document.getElementById('nx').onclick = () => { tries++;
  if (!document.getElementById('nm').value) { document.getElementById('err').textContent = 'Name is required (try ' + tries + ')'; return; }
  document.getElementById('err').textContent = '';
  document.getElementById('done').innerHTML = '<h2>Application submitted</h2><p>We have your application.</p>'; };
</script></body></html>"""

POPUP_OPENER = """<!DOCTYPE html><html><head><title>Apply</title></head><body>
<a href="/demo/t/popup" target="_blank">Sign in with Example</a><label for="e">Email</label><input id="e"></body></html>"""
VERIFY = "<!DOCTYPE html><html><head><title>Almost done</title></head><body><button>Verify</button></body></html>"
POPUP = """<!DOCTYPE html><html><head><title>Signing in</title></head><body>One moment...
<script>setTimeout(() => window.close(), 300);</script></body></html>"""

PAGES = {"apply": LOADING, "combo": COMBO, "lever": LEVER, "clears": CLEARS, "complains": COMPLAINS,
         "opener": POPUP_OPENER, "popup": POPUP, "verify": VERIFY}
API_CALLS: list[str] = []


@pytest.fixture(scope="module")
def site():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, PlainTextResponse

    app = FastAPI()

    @app.get("/demo/t/api")
    async def api(n: str = ""):
        import asyncio
        await asyncio.sleep(0.05)
        API_CALLS.append(n)
        return PlainTextResponse("ok")

    @app.get("/demo/t/{name}")
    def page(name: str):
        return HTMLResponse(PAGES[name])

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/demo/t"
    server.should_exit = True
    t.join(5)


@pytest.fixture
def browser(data_dir):
    b = BrowserSession(headless=True)
    yield b
    b.close()


@pytest.fixture
def job_folder(data_dir):
    folder = data_dir / "jobs" / "2026-10-20_Acme_Engineer"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(PDF)
    return folder


def make_agent(browser, folder, fake=None, screening=None, **kw):
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    client = LLMClient(s, "sk-test", meter=Meter.from_settings(s), transport=(fake or FakeLLM()).transport)
    return ApplyAgent(browser=browser, llm=client, settings=s, contact={"name": "Alex Tan"}, profile_text="Python.",
                      screening={"work_auth": "Singapore Citizen"} if screening is None else screening,
                      job={"id": None, "role": "Engineer", "company": "Acme"}, folder=folder,
                      redactor=secrets_store.Redactor.from_store(s), **kw)


def view_of(agent) -> tuple[str, list]:
    """The page as the model sees it: (the view, [(ref, kind, label)])."""
    view = agent.scan()
    return view, page_elements("Current page:\n" + view)[1]


# ── the page keeps loading; ids stay unique; a click means look again ──

def test_a_javascript_form_loads_while_the_agent_waits_and_goes_through(site, browser, job_folder):
    """The whole run as on a Workday-like form: wait for it to load, fill, Continue, answer Yes, submit."""
    seen, turns = [], []

    def brain(user):
        title, els = page_elements(user)
        view = user.split("Current page:", 1)[1]
        turns.append(view)
        kinds = {k for _, k, _ in els}
        if "Thank you for applying" in view:
            return [("finish", {"status": "submitted", "summary": "Sent.", "confirmation_text": "Thank you for applying."})]
        if "Loading..." in view:
            return [("wait", {"seconds": 3})]
        if any(lbl == "First name" for _, _, lbl in els):
            fn, ln = find_ref(els, "text", "First name"), find_ref(els, "text", "Last name")
            # after Continue the form changes under the old ids: the last action must not run on the old view
            return [("fill", {"element": fn, "text": "Alex"}), ("fill", {"element": ln, "text": "Tan"}),
                    ("click", {"element": find_ref(els, "button", "Continue")}), ("fill", {"element": fn, "text": "X"})]
        if "radio" in kinds:
            seen.append(find_ref(els, "radio", "Yes"))
            return [("click", {"element": find_ref(els, "radio", "Yes")}),
                    ("click", {"element": find_ref(els, "button", "Continue")})]
        return [("click", {"element": find_ref(els, "button", "Submit application")})]

    agent = make_agent(browser, job_folder, FakeLLM(apply_brain=brain))  # the agent's own pauses, as in the app
    t0 = time.monotonic()
    result = agent.run(f"{site}/apply")
    assert result.status == "submitted", (result.summary, result.log)
    log = "\n".join(result.log)
    assert "FAILED" not in log and "REFUSED" not in log, log
    assert log.count("waited") <= 1  # one wait was enough: the page loaded during it
    assert 'clicked radio "Yes"' in log
    assert not any("-> filled" in ln and '"X"' in ln for ln in result.log)
    assert sum(1 for ln in result.log if ln.startswith("step") and "fill" in ln and "First name" in ln) == 1
    assert "submit" in API_CALLS
    assert time.monotonic() - t0 < 60  # no 10 s timeouts on hidden fields


def test_ids_from_an_earlier_look_do_not_stay_on_hidden_fields(site, browser, job_folder):
    agent = make_agent(browser, job_folder)
    browser.page.set_content("<div id=a><input id=one aria-label='Old field'></div><div id=b style='display:none'>"
                             "<input id=two aria-label='Yes field'></div>")
    _, els = view_of(agent)
    assert find_ref(els, "text", "Old field") == "e1"
    browser.page.evaluate("() => { a.style.display = 'none'; b.style.display = 'block'; }")
    _, els = view_of(agent)
    assert find_ref(els, "text", "Yes field") == "e1"
    assert browser.page.locator('[data-jh-ref="e1"]').count() == 1  # the hidden old field lost its id
    agent.run_tool("fill", {"element": "e1", "text": "yes"})
    assert browser.page.input_value("#two") == "yes"


def test_the_network_rule_only_checks_requests_it_might_block():
    """Everything request_allowed refuses is matched inside the browser and sent to Python; ordinary sites
    never are (so they keep loading while Python waits)."""
    refused = ["http://127.0.0.1:8765/accounts", "http://localhost:8765/settings", "http://[::1]:8765/",
               "http://10.0.0.5/admin", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data",
               "http://0.0.0.0:8765/", "http://foo.localhost/", "http://LOCALHOST/x", "http://user@127.0.0.1/",
               "https://mail.google.com/mail/u/0", "https://drive.google.com/x", "https://outlook.office.com/mail"]
    for url in refused:
        assert not request_allowed(url), url
        assert applyagent.GUARDED_URL.search(url), url
    for url in ["https://boards.greenhouse.io/acme/jobs/1", "https://acme.wd3.myworkdayjobs.com/x",
                "https://sg.jobstreet.com/job/1", "https://www.google.com/recaptcha/api.js",
                "https://localhostess.com/", "https://127.0.0.1.nip.io/"]:
        assert not applyagent.GUARDED_URL.search(url), url


def test_pausing_lets_the_page_load_where_sleeping_would_not(site, browser, job_folder):
    agent = make_agent(browser, job_folder)
    agent._guard_network()  # the local test site is checked by Python: the hardest case
    browser.page.goto(f"{site}/apply", wait_until="domcontentloaded")
    agent._pause(4)
    assert "Loading..." not in browser.page.inner_text("body")


# ── dropdowns that aren't plain selects ───────────────────────────────

def test_a_type_to_search_dropdown_gets_a_real_choice(site, browser, job_folder):
    agent = make_agent(browser, job_folder)
    browser.page.goto(f"{site}/combo")
    view, els = view_of(agent)
    ref = find_ref(els, "combobox", "Are you eligible")
    assert "q_req" not in view  # the dropdown's hidden stand-in box isn't shown as a field of its own
    # fill (what models do with a box) and select_option both pick the choice
    assert agent.run_tool("fill", {"element": ref, "text": "yes"}) == \
        'chose "Yes" in "Are you eligible to work in the location this job post is posted in?*"'
    assert browser.page.inner_text("#sv") == "Yes"
    view, els = view_of(agent)
    assert f'[{ref}] combobox "Are you eligible' in view and 'value="Yes"' in view  # it can see the choice took
    with pytest.raises(ToolError, match="the choices are: Yes | No"):
        agent.run_tool("select_option", {"element": find_ref(els, "combobox"), "option": "Maybe"})


def test_a_dropdown_answering_a_screening_question_still_needs_a_saved_answer(site, browser, job_folder):
    agent = make_agent(browser, job_folder, screening={})
    browser.page.goto(f"{site}/combo")
    _, els = view_of(agent)
    with pytest.raises(NeedsHuman) as stop:
        agent.run_tool("select_option", {"element": find_ref(els, "combobox"), "option": "Yes"})
    assert stop.value.topic == "work_auth"


def test_lever_style_suggestions_questions_and_long_lists(site, browser, job_folder):
    agent = make_agent(browser, job_folder)
    browser.page.goto(f"{site}/lever")
    view, els = view_of(agent)
    # the question shows with its choices, so the model (and the screening rule) know what Yes answers
    assert 'radio "Yes" in "Are you legally authorized to work in the country' in view
    assert 'textarea "cards[b][field0]" in "Why do you want to join us?"' in view
    assert "(+32 more: name the one you want)" in view
    loc_ref = find_ref(els, "text", "Current location")
    assert "suggests as you type" in view
    # the box searches on a key press and lists plain <div>s: JobHunt types and picks the matching one
    assert agent.run_tool("fill", {"element": loc_ref, "text": "Singapore"}) == 'chose "Singapore" in "Current location"'
    assert browser.page.input_value("#location-input") == "Singapore"
    school = next(r for r, k, _ in els if k == "select")
    assert agent.run_tool("select_option", {"element": school, "option": "National University of Singapore"}) == \
        'selected "National University of Singapore - NUS" in "cards[c][field0]"'
    t0 = time.monotonic()
    with pytest.raises(ToolError, match="some options: National University of Singapore - NUS"):
        agent.run_tool("select_option", {"element": school, "option": "Singapore Management University"})
    assert time.monotonic() - t0 < 3  # said at once, not after a 10 s wait


def test_best_match():
    m = applyagent._best_match
    assert m("yes", ["Yes", "No"]) == "Yes"
    assert m("No", ["None of the above", "No"]) == "No"
    assert m("Singapore", ["Singapore, SGP", "Singapore", "Singapore, MI, USA"]) == "Singapore"
    assert m("Singapore Citizen", ["Malaysia", "Singapore"]) == "Singapore"
    assert m("Not applicable", ["No", "Yes"]) is None
    assert m("", ["Yes"]) is None


# ── dead ends hand the job over with a reason ─────────────────────────

def test_a_box_that_takes_nothing_stops_as_stuck_with_a_reason(site, browser, job_folder):
    texts = iter(["Singapore", "Singapore ", "singapore", "SINGAPORE", "Singapore.", "Singapore,", "Singapore!"] * 3)

    def brain(user):
        _, els = page_elements(user)
        return [("fill", {"element": find_ref(els, "text", "City"), "text": next(texts)})]
    result = make_agent(browser, job_folder, FakeLLM(apply_brain=brain)).run(f"{site}/clears")
    assert result.status == "needs_you" and result.kind == "stuck"
    assert f"The last {applyagent.NO_CHANGE_LIMIT} rounds of actions left the page exactly as it was" in result.summary
    assert 'filled "City"' in result.summary


def test_clicking_next_again_after_the_form_complains_is_not_going_round_in_circles(site, browser, job_folder):
    turns = []

    def brain(user):
        _, els = page_elements(user)
        view = user.split("Current page:", 1)[1]
        turns.append(view)
        if "Application submitted" in view:
            return [("finish", {"status": "submitted", "summary": "ok", "confirmation_text": "Application submitted"})]
        if len(turns) <= 3:
            return [("click", {"element": find_ref(els, "button", "Next")})]  # three times the same, page changing
        return [("fill", {"element": find_ref(els, "text", "Name"), "text": "Alex Tan"}),
                ("click", {"element": find_ref(els, "button", "Next")})]
    result = make_agent(browser, job_folder, FakeLLM(apply_brain=brain)).run(f"{site}/complains")
    assert result.status == "submitted", result.summary
    assert "Messages on the page (errors, alerts):\n- Name is required (try 1)" in turns[1]


def test_the_same_actions_on_an_unchanged_page_still_stop(site, browser, job_folder):
    def brain(user):
        _, els = page_elements(user)
        return [("click", {"element": find_ref(els, "button", "Next")})]
    result = make_agent(browser, job_folder, FakeLLM(apply_brain=brain)).run(f"{site}/clears")
    assert result.status == "needs_you" and result.kind == "stuck"
    assert "tried the same thing 3 times (click e2)" in result.summary


def test_a_captcha_box_stops_as_needs_you_verify(browser, job_folder):
    agent = make_agent(browser, job_folder)
    agent.refs = {"e9": (None, {"ref": "e9", "kind": "checkbox", "label": "I am human", "captcha": True})}
    with pytest.raises(NeedsHuman) as stop:
        agent.run_tool("click", {"element": "e9"})
    assert stop.value.kind == "verify" and "captcha" in str(stop.value)
    assert "(captcha: only the candidate can do this)" in ApplyAgent._describe(agent.refs["e9"][1])
    for url in ["https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox",
                "https://www.google.com/recaptcha/api2/anchor?k=x", "https://challenges.cloudflare.com/cdn-cgi/x"]:
        assert applyagent.CAPTCHA_FRAME.search(url), url
    assert not applyagent.CAPTCHA_FRAME.search("https://jobs.lever.co/palantir/1/apply")


def test_a_captcha_met_mid_run_hands_the_job_over(site, browser, job_folder, monkeypatch):
    agent = make_agent(browser, job_folder, FakeLLM(apply_brain=lambda u: [("click", {"element": "e1"})]))
    real_scan = agent.scan

    def scan():
        view = real_scan()
        agent.refs["e1"][1]["captcha"] = True  # as if the button sat in an hCaptcha frame
        return view
    monkeypatch.setattr(agent, "scan", scan)
    result = agent.run(f"{site}/verify")
    assert result.status == "needs_you" and result.kind == "verify"
    assert result.log[-1].endswith("STOPPED: a captcha")


def test_a_popup_that_closes_itself_returns_to_the_page_before(site, browser, job_folder):
    agent = make_agent(browser, job_folder)
    browser.page.goto(f"{site}/opener")
    _, els = view_of(agent)
    agent.run_tool("click", {"element": find_ref(els, "link", "Sign in with")})
    agent._pause(0.3)
    assert agent._page().url.endswith("/popup")  # followed the new window
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len([p for p in browser._ctx.pages if not p.is_closed()]) > 1:
        agent._pause(0.1)
    assert agent._page().url.endswith("/opener")  # it closed: back on the form, not a blank page


# ── the AI provider failing ───────────────────────────────────────────

def test_an_ai_provider_failure_hands_the_job_over_not_error(browser, job_folder, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    down = httpx.MockTransport(lambda r: httpx.Response(503, text="overloaded"))
    agent = ApplyAgent(browser=browser, llm=LLMClient(s, "sk-test", transport=down), settings=s, contact={},
                       profile_text="", job={"id": None, "role": "R", "company": "C"}, folder=job_folder)
    browser.page.set_content("<button>Apply</button>")
    monkeypatch.setattr(agent, "_page", lambda: browser.page)
    result = agent.run("about:blank")
    assert result.status == "needs_you" and result.kind == "model"
    assert result.summary.startswith("The AI provider failed while applying (")
    assert "before filling anything in" in result.summary and "Try again" in result.summary
    assert hunt.needs_you_fix({"reason_kind": "model", "source": "demo", "apply_url": ""})["retry"] is True


def test_a_long_retry_after_stops_at_once(monkeypatch):
    calls, slept = [], []
    monkeypatch.setattr(llm.time, "sleep", slept.append)

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "5400"}, json={"error": "usage limit"})
    s = settings.load()
    client = LLMClient(s, "sk-test", transport=httpx.MockTransport(handle))
    with pytest.raises(RateLimited, match="try again in about 90 minutes"):
        client.chat(stage="apply", system="s", user="u")
    assert len(calls) == 1 and slept == []
    calls.clear()

    def short(request):  # a short one is still waited out, then tried again
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "3"}) if len(calls) == 1 else \
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    client = LLMClient(s, "sk-test", transport=httpx.MockTransport(short))
    assert client.chat(stage="apply", system="s", user="u").text == "ok"
    assert slept == [3.0]


def test_reasoning_models_get_room_to_think_before_acting(browser, job_folder):
    agent = make_agent(browser, job_folder)
    assert agent._max_tokens() == 1200
    agent.settings["reasoning"] = True
    assert agent._max_tokens() >= 4000


# ── the hunt carries on ───────────────────────────────────────────────

def test_pacing_says_until_when_it_waits(data_dir):
    settings.update({"pacing": {"max_apps_per_hour_per_site": 1}})
    jid = db.add_job(source="demo", url="https://x.example/1", company="A", role="R", folder="f", state="Ready")
    db.update_job(jid, state="Applied")
    first = datetime.fromisoformat(db.get_job(jid)["updated_at"])
    stop = threading.Event()
    task = hunt.Task("hunt", {}, stop)
    logged = []
    task.log = lambda m, *a, **k: (logged.append(m), stop.set())
    with pytest.raises(hunt.StopHunt):
        hunt.wait_for_rate_limit(task, "demo")
    assert f"waiting until about {(first + timedelta(hours=1)):%H:%M} before the next one" in logged[0]


def test_one_job_breaking_never_stops_the_rest_of_the_hunt(demo_server, secrets, candidate, fake_llm, data_dir,
                                                         monkeypatch):
    settings.update({"apply": {"show_browser": False}, "hunt": {"sites": ["demo"]},
                     "scope": {"keywords": ["engineer"]}, "demo_board_url": demo_server,
                     "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0}})
    monkeypatch.setattr(hunt, "ApplyAgent", functools.partial(ApplyAgent, sleep=lambda s: None))
    real_run, runs = ApplyAgent.run, []

    def run(self, url):
        runs.append(url)
        if len(runs) == 1:
            raise RuntimeError("the first job's page broke")
        return real_run(self, url)
    monkeypatch.setattr(ApplyAgent, "run", run)
    task = hunt.Task("hunt", {"auto_submit": True, "jobs": 3, "sites": ["demo"]}, threading.Event())
    hunt.run_hunt(task)
    states = [j["state"] for j in db.list_jobs()] if hasattr(db, "list_jobs") else \
        [r["state"] for r in db._query("SELECT state FROM jobs")]
    assert len(runs) >= 2, "the hunt stopped after the first job broke"
    assert "Needs you" in states and "Applied" in states, states
