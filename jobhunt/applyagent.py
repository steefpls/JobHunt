"""The apply agent: fills in one application in the browser, step by step.

How it keeps tokens and risk down:
- Each step is ONE fresh model request: the task, a short code-written log of
  what was done so far, and a condensed view of the current page (form
  elements with short ids, plus capped visible text). No growing chat history,
  no raw page snapshots.
- The model acts only through the small tool set below. It cannot run code,
  read or write files, or upload anything except this job's resume/cover letter.
- Passwords: the model calls type_secret(login, field, element). Code reads the
  value from Windows Credential Manager and types it. A password can only go
  into a real password box, and only on the sites listed for that login.
  Anything on the page that echoes a secret is replaced with [secret:name]
  before the model sees it.
- Screening questions (work authorisation, salary, notice period...) are
  answered only from the saved screening answers on the Profile page. If a form
  asks one that has no saved answer, code stops the run as "Needs you"; the
  model's judgement is not trusted with this.
- Where it can go: `goto` only reaches the site the application started on (and
  sites a click led to). JobHunt's own pages and other local addresses are
  blocked in this browser, except the practice board under /demo.
- "Applied" is decided by code, not by the model's say-so: the confirmation
  text the model quotes must be on the page now, and must have appeared after
  a button click (not been there all along).
- A human check (Cloudflare's "Just a moment...", Indeed's security check, a
  captcha page) is spotted by code before the model is asked anything: it gets
  a short wait to clear by itself, then the run stops as "Needs you" without
  spending tokens on it.
"""
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

from . import prompts, secrets_store, sites
from .llm import BudgetExceeded, LLMError
from .sites import is_mcf_url

MAX_ELEMENTS = 120
MAX_LINKS = 40
HUMAN_CHECK_WAIT_S = 15  # a check in a real window often clears by itself in a few seconds

# Screening topics, matched against a form field's label, its group question and its name.
SCREENING_PATTERNS = {
    "work_auth": r"\b(legal(ly)?|authori[sz]ed|eligible|entitled|right|permitted|allowed)\W+(\w+\W+){0,4}work\b|"
                 r"work\W*(pass|permit|visa|authori[sz]ation|eligibility)|work_?auth|citizen|permanent resident|"
                 r"\bPR\b|residency|nationality|immigration|visa status|employment pass",
    "sponsorship": r"sponsor",
    "notice_period": r"notice\W*period|earliest\W+(\w+\W+){0,2}start|"
                     r"(available|able|when can you)\W+(\w+\W+){0,2}(start|join)\b",
    "current_salary": r"\b(current|present|last|previous)\W+(\w+\W+){0,2}(salary|compensation|pay|ctc|remuneration)",
    "expected_salary": r"salary|compensation|remuneration|\bctc\b|pay expectation|expected\W+(pay|package)",
    "years_experience": r"years?\W+(of\W+)?(\w+\W+){0,2}experience|how many years|years_?exp",
    "relocate": r"relocat",
}

# Never reachable from the apply browser, whatever a page or the model says.
BLOCKED_HOSTS = {"mail.google.com", "drive.google.com", "docs.google.com", "myaccount.google.com",
                 "outlook.live.com", "outlook.office.com", "outlook.office365.com"}
LOCAL_NAMES = {"localhost", "0.0.0.0", "::1", "[::1]"}
# Second-level labels under which a registrable domain has three parts (sg.jobstreet.com is fine as is).
_SECOND_LEVEL = {"com", "co", "gov", "edu", "org", "net", "ac"}


def screening_topic(*texts: str) -> str | None:
    hay = " ".join(t for t in texts if t)
    for topic, pattern in SCREENING_PATTERNS.items():
        if re.search(pattern, hay, re.I):
            return topic
    return None


def site_of(host: str) -> str:
    """Rough registrable domain: careers.acme.com -> acme.com, x.acme.com.sg -> acme.com.sg."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if _is_ip(host) or len(parts) <= 2:
        return host
    if parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_local_host(host: str) -> bool:
    host = (host or "").lower()
    if host in LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def request_allowed(url: str) -> bool:
    """The apply browser's network rule: no JobHunt UI, no local network, no mailbox."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return True  # data:, blob: and the like stay inside the page
    host = (u.hostname or "").lower()
    if host in BLOCKED_HOSTS:
        return False
    if is_local_host(host):
        return u.path == "/demo" or u.path.startswith("/demo/")  # the practice board only
    return True

PAGE_SCAN_JS = r"""
(start) => {
  const out = []; let n = start;
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  const labelOf = el => {
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = l.innerText; }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t && el.getAttribute('aria-labelledby')) { const l = document.getElementById(el.getAttribute('aria-labelledby')); if (l) t = l.innerText; }
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('title') || '';
    if (!t && (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role'))) t = el.innerText;
    if (!t && el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) t = el.value;
    if (!t) t = el.getAttribute('name') || '';
    return clean(t);
  };
  // The question a radio/checkbox/field belongs to: fieldset legend, ARIA group, or short nearby text.
  const groupOf = el => {
    const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend');
    if (lg) return clean(lg.innerText);
    const g = el.closest('[role=radiogroup],[role=group]');
    if (g) { const by = g.getAttribute('aria-labelledby'); const l = by && document.getElementById(by);
      const t = g.getAttribute('aria-label') || (l ? l.innerText : ''); if (t) return clean(t); }
    const box = el.parentElement && el.parentElement.closest('div,li,p,td,section');
    const t = box ? (box.innerText || '') : '';
    return t.length <= 300 ? clean(t) : '';
  };
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox], [role=radio], [role=combobox], [contenteditable=true]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden') continue;
    if (!visible(el) && el.type !== 'file') continue;
    n++; const ref = 'e' + n; el.setAttribute('data-jh-ref', ref);
    const tag = el.tagName.toLowerCase();
    let kind = el.getAttribute('role') || tag;
    if (tag === 'input') kind = (el.type || 'text');
    if (tag === 'a') kind = 'link';
    const item = {ref, kind, label: labelOf(el), required: !!(el.required || el.getAttribute('aria-required') === 'true'), disabled: !!el.disabled};
    if (!['a', 'button'].includes(tag) && !['submit', 'button', 'image', 'reset'].includes(el.type)) {
      item.group = groupOf(el); item.name = (el.getAttribute('name') || el.id || '').slice(0, 60);
      if (item.group === item.label) item.group = '';
    }
    if (tag === 'input' && !['checkbox','radio','file','submit','button','password','image','reset'].includes(el.type)) item.value = (el.value || '').slice(0, 80);
    if (tag === 'textarea') item.value = (el.value || '').slice(0, 80);
    if (tag === 'input' && el.type === 'password') item.value = el.value ? '(filled)' : '';
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = el.checked;
    if (tag === 'select') { item.value = (el.options[el.selectedIndex] || {}).text || ''; item.options = Array.from(el.options).slice(0, 30).map(o => clean(o.text)); }
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 120);
    if (el.type === 'file') item.files = el.files ? el.files.length : 0;
    out.push(item);
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n');
  return {elements: out, text, next: n, title: document.title};
}
"""

TOOLS = [
    {"name": "click", "description": "Click a button, link, radio button or other element.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string", "description": "element id like e12"}},
                    "required": ["element"]}},
    {"name": "fill", "description": "Type text into a text box or text area (replaces what is there). Not for passwords.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["element", "text"]}},
    {"name": "select_option", "description": "Choose an option in a dropdown by its visible text.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"}, "option": {"type": "string"}},
                    "required": ["element", "option"]}},
    {"name": "set_checkbox", "description": "Tick or untick a checkbox.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"}, "checked": {"type": "boolean"}},
                    "required": ["element", "checked"]}},
    {"name": "type_secret", "description": "Type a saved login's username or password into a field. You only give the "
     "login name; the value is typed by the app and you never see it.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"}, "login": {"type": "string"},
                                                     "field": {"type": "string", "enum": ["username", "password"]}},
                    "required": ["element", "login", "field"]}},
    {"name": "upload_file", "description": "Attach the tailored resume or cover letter PDF to a file input.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"},
                                                     "file": {"type": "string", "enum": ["resume", "cover_letter"]}},
                    "required": ["element", "file"]}},
    {"name": "goto", "description": "Open a web address (http or https only).",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "wait", "description": "Wait a few seconds for the page to finish loading.",
     "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
    {"name": "finish", "description": "Stop working on this application and report the outcome.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["submitted", "needs_you", "failed"],
                    "description": "submitted = confirmation seen; needs_you = a person must finish it; failed = cannot be done"},
         "summary": {"type": "string", "description": "one or two sentences for the candidate"},
         "confirmation_text": {"type": "string", "description": "exact confirmation wording on the page (submitted only)"},
         "missing_info": {"type": "string", "description": "what the candidate must provide, if anything"}},
         "required": ["status", "summary"]}},
]


@dataclass
class ApplyResult:
    status: str  # 'submitted' (verified), 'needs_you', 'failed', 'stopped'
    summary: str
    url: str = ""
    steps: int = 0
    confirmation_pdf: Path | None = None
    log: list = field(default_factory=list)
    kind: str = ""  # for needs_you: what kind of stop (hunt.needs_you_fix says what you can do about it)


class ApplyAgent:
    def __init__(self, *, browser, llm, settings: dict, contact: dict, profile_text: str, job: dict, folder: Path,
                 screening: dict | None = None, redactor=None, stop_event=None, log=None, secret_getter=None,
                 sleep=time.sleep):
        self.browser = browser
        self.llm = llm
        self.settings = settings
        self.contact = contact
        self.profile_text = profile_text
        self.screening = {k: v for k, v in (screening or {}).items() if v}  # topic -> saved answer
        self.job = job
        self.folder = Path(folder)
        self.redact = redactor or (lambda t: t)
        self.stop_event = stop_event
        self.log = log or (lambda m: None)
        self.get_secret = secret_getter or secrets_store.get_secret
        self.sleep = sleep
        self.logins = settings.get("logins", [])
        self.refs: dict = {}
        self.action_log: list[str] = []
        self.clicked_since_fill = False
        self.texts_before_clicks: list[str] = []  # page text just before each button click
        self.allowed_sites: set[str] = set()  # where goto may go: the start site + sites clicks led to

    # ── page view ──────────────────────────────────────────────────────
    def _page(self):
        page = self.browser.page
        pages = [p for p in page.context.pages if not p.is_closed()]
        if pages and pages[-1] is not page:  # a click opened a new tab: follow it
            self.browser._page = pages[-1]
            page = pages[-1]
        return page

    def scan(self) -> str:
        page = self._page()
        self.refs = {}
        elements, texts, n = [], [], 0
        for frame in page.frames:
            try:
                res = frame.evaluate(PAGE_SCAN_JS, n)
            except PlaywrightError:
                continue
            n = res["next"]
            for el in res["elements"]:
                self.refs[el["ref"]] = (frame, el)
            elements += res["elements"]
            if res["text"].strip():
                texts.append(res["text"])
        links = [e for e in elements if e["kind"] == "link"]
        others = [e for e in elements if e["kind"] != "link"]
        shown = others[:MAX_ELEMENTS - min(len(links), MAX_LINKS)] + links[:MAX_LINKS]
        lines = [self._describe(e) for e in shown]
        if len(elements) > len(shown):
            lines.append(f"(+{len(elements) - len(shown)} more elements not shown)")
        text = "\n".join(texts)
        cap = prompts.CAPS["page_text"]
        if len(text) > cap:
            text = text[:cap] + "\n[...more text below, cut...]"
        try:
            title = page.title()
        except PlaywrightError as e:  # mid-navigation; but a closed window ends the run
            if self.browser.is_closed_error(e):
                raise
            title = ""
        view = (f"URL: {page.url}\nTitle: {title}\nElements:\n" + ("\n".join(lines) or "(none)") +
                "\n\nVisible text:\n" + text)
        return prompts.untrusted("web_page", self.redact(view))

    @staticmethod
    def _describe(e: dict) -> str:
        s = f"[{e['ref']}] {e['kind']} \"{e['label']}\""
        if e.get("group"):
            s += f" in \"{e['group'][:160]}\""
        if e.get("required"):
            s += " (required)"
        if e.get("disabled"):
            s += " (disabled)"
        if "value" in e:
            s += f" value=\"{e['value']}\""
        if "checked" in e:
            s += f" checked={str(e['checked']).lower()}"
        if e.get("options"):
            s += " options: " + " | ".join(e["options"])
        if e.get("href"):
            s += f" -> {e['href']}"
        if "files" in e:
            s += f" files_attached={e['files']}"
        return s

    def page_text(self) -> str:
        parts = []
        for frame in self._page().frames:
            try:
                parts.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except PlaywrightError:
                continue
        return "\n".join(parts)

    # ── tools ──────────────────────────────────────────────────────────
    def _locator(self, ref: str):
        if ref not in self.refs:
            raise ToolError(f"no element {ref} on this page (the page may have changed)")
        frame, el = self.refs[ref]
        return frame.locator(f'[data-jh-ref="{ref}"]').first, el

    def _check_screening(self, el: dict) -> None:
        """Code, not the model, decides whether a screening question may be answered."""
        if el["kind"] in BUTTONISH or el["kind"] in ("link", "file", "password"):
            return
        topic = screening_topic(el.get("label", ""), el.get("group", ""), el.get("name", ""))
        if topic and topic not in self.screening:
            from .profile import SCREENING_FIELDS
            question = el.get("group") or el.get("label") or el.get("name") or "a screening question"
            what = SCREENING_FIELDS[topic][0].split(" (")[0].lower()
            raise NeedsHuman(f"The form asks \"{question[:120]}\" ({what}), and your Profile page has no "
                             "screening answer for that. Add it there, or finish this application yourself.", topic)

    def run_tool(self, name: str, args: dict) -> str:
        """Do one action. Returns a short result line (goes into the action log)."""
        if name == "click":
            loc, el = self._locator(args.get("element", ""))
            self._check_screening(el)  # clicking a radio button or checkbox answers a question too
            if el["kind"] in BUTTONISH:
                self.texts_before_clicks.append(_norm(self.page_text()))
            loc.click(timeout=10000)
            self.clicked_since_fill = True
            return f"clicked {el['kind']} \"{el['label']}\""
        if name == "fill":
            loc, el = self._locator(args.get("element", ""))
            if el["kind"] == "password":
                raise ToolError("use type_secret for password fields")
            self._check_screening(el)
            loc.fill(str(args.get("text", ""))[:4000], timeout=10000)
            self.clicked_since_fill = False
            return f"filled \"{el['label']}\""
        if name == "select_option":
            loc, el = self._locator(args.get("element", ""))
            self._check_screening(el)
            loc.select_option(label=str(args.get("option", "")), timeout=10000)
            return f"selected \"{args.get('option')}\" in \"{el['label']}\""
        if name == "set_checkbox":
            loc, el = self._locator(args.get("element", ""))
            self._check_screening(el)
            loc.set_checked(bool(args.get("checked")), timeout=10000)
            return f"set \"{el['label']}\" to {bool(args.get('checked'))}"
        if name == "type_secret":
            return self._type_secret(args)
        if name == "upload_file":
            loc, el = self._locator(args.get("element", ""))
            which = args.get("file")
            path = self.folder / {"resume": "resume.pdf", "cover_letter": "cover_letter.pdf"}.get(which, "")
            if which not in ("resume", "cover_letter") or not path.is_file():
                raise ToolError("file must be 'resume' or 'cover_letter'")
            loc.set_input_files(str(path), timeout=10000)
            return f"attached {path.name} to \"{el['label']}\""
        if name == "goto":
            url = str(args.get("url", ""))
            if urlparse(url).scheme not in ("http", "https"):
                raise ToolError("only http(s) addresses are allowed")
            if site_of(urlparse(url).hostname or "") not in self.allowed_sites or not request_allowed(url):
                raise ToolError("goto can only open pages on the application's own site; "
                                "if you need another site, finish with needs_you")
            self._page().goto(url, wait_until="domcontentloaded", timeout=45000)
            return f"opened {url[:120]}"
        if name == "wait":
            self.sleep(min(5.0, max(0.0, float(args.get("seconds") or 1))))
            return "waited"
        raise ToolError(f"unknown tool {name}")

    def _type_secret(self, args: dict) -> str:
        login_name, fld = args.get("login", ""), args.get("field", "")
        login = next((l for l in self.logins if l["name"] == login_name), None)
        if not login:
            raise ToolError(f"no saved login called {login_name!r}")
        if fld not in ("username", "password"):
            raise ToolError("field must be username or password")
        host = (urlparse(self._page().url).hostname or "").lower()
        domains = [d.lower().lstrip(".") for d in login.get("domains") or []]
        if not domains:
            raise ToolError(f"login {login_name!r} has no site list yet, so it can't be used anywhere")
        if not any(host == d or host.endswith("." + d) for d in domains):
            raise ToolError(f"login {login_name!r} is not allowed on {host}")
        loc, el = self._locator(args.get("element", ""))
        if fld == "password" and el["kind"] != "password":
            raise ToolError("a password can only be typed into a password box")
        if fld == "username" and el["kind"] not in ("text", "email", "textbox", "input", "tel"):
            raise ToolError("a username can only be typed into a text or email box")
        value = self.get_secret(secrets_store.login_secret_name(login_name, fld))
        if not value:
            raise ToolError(f"login {login_name!r} has no saved {fld}")
        loc.fill(value, timeout=10000)
        self.clicked_since_fill = False
        return f"typed saved {fld} of login '{login_name}' into \"{el['label']}\""

    def _settle(self) -> None:
        page = self._page()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightError:
            pass
        self.sleep(float(self.settings.get("pacing", {}).get("action_delay_s", 0.8)))

    # ── finishing ──────────────────────────────────────────────────────
    def _verify_confirmation(self, quoted: str) -> bool:
        """The quoted confirmation is on the page now AND was not there before some button click."""
        q = _norm(quoted)
        if len(q) < 8 or q not in _norm(self.page_text()):
            return False
        return any(q not in before for before in self.texts_before_clicks)

    def _finish(self, args: dict, steps: int) -> ApplyResult:
        status = args.get("status")
        summary = str(args.get("summary", ""))[:500]
        if args.get("missing_info"):
            summary += f" Missing: {str(args['missing_info'])[:300]}"
        url = self._page().url
        if status == "submitted":
            if self._verify_confirmation(str(args.get("confirmation_text", ""))):
                pdf = self.folder / "apply_confirmation.pdf"
                try:
                    self.browser.page_to_pdf(pdf, f"Application confirmation - {self.job['company']} - {self.job['role']}")
                except Exception as e:  # proof is nice to have; the submission already happened
                    self.log(f"Could not save confirmation PDF: {type(e).__name__}")
                    pdf = None
                return ApplyResult("submitted", summary, url, steps, pdf, self.action_log)
            return ApplyResult("needs_you", "The agent said it submitted, but no confirmation was found on the page. "
                               "Please check this application yourself. " + summary, url, steps, None, self.action_log,
                               kind="unconfirmed")
        if status == "failed":
            return ApplyResult("failed", summary, url, steps, None, self.action_log)
        return ApplyResult("needs_you", summary or "Needs you to finish.", url, steps, None, self.action_log)

    # ── human checks ───────────────────────────────────────────────────
    def _human_check_title(self) -> str | None:
        """The page's title if it is a human check or block page (sites.is_human_check), '' if it isn't,
        None if the page couldn't be read just now (a check page redirecting as it clears)."""
        try:
            title = self._page().title()
        except PlaywrightError as e:
            if self.browser.is_closed_error(e):
                raise
            return None
        return title if sites.is_human_check(title) else ""

    def _wait_out_human_check(self, step: int) -> ApplyResult | None:
        """None when the page is not a human check, or one cleared within HUMAN_CHECK_WAIT_S;
        otherwise the result to stop with. Never asks the model."""
        title = self._human_check_title()
        if not title:
            return None
        self.log(f"The site is showing a human check; waiting up to {HUMAN_CHECK_WAIT_S} seconds for it to clear.")
        for _ in range(HUMAN_CHECK_WAIT_S):
            if self.stop_event is not None and self.stop_event.is_set():
                return ApplyResult("stopped", "Stopped by you.", self._page().url, step - 1, None, self.action_log)
            self.sleep(1)
            if self._human_check_title() == "":  # read, and not a check any more (None: look again)
                self.log("The human check cleared by itself.")
                self._settle()
                return None
        url = self._page().url
        done = "before filling anything in" if not self.action_log else "there"
        summary = (f"The site showed a human check (\"{self.redact(title.strip())[:80]}\") that didn't clear by "
                   f"itself, so JobHunt stopped {done}.")
        if site_of(urlparse(url).hostname or "").startswith("indeed."):
            summary += f" {sites.INDEED_CHECK_STEPS}, then press Try again."
        else:
            summary += " Open the apply page and finish it yourself, then press I applied."
        self.action_log.append(f"step {step}: STOPPED: human check page")
        return ApplyResult("needs_you", summary, url, step - 1, None, self.action_log, kind="verify")

    # ── main loop ──────────────────────────────────────────────────────
    def run(self, start_url: str) -> ApplyResult:
        max_steps = int(self.settings.get("apply", {}).get("max_steps", 30))
        system = prompts.apply_system_prompt(bool(self.settings.get("apply", {}).get("allow_register")))
        files = [n for n in ("resume", "cover_letter") if (self.folder / f"{n}.pdf").is_file()]
        self._guard_network()
        self.allowed_sites.add(site_of(urlparse(start_url).hostname or ""))
        self._page().goto(start_url, wait_until="domcontentloaded", timeout=45000)
        self._settle()
        landed = self._page().url  # a job board's apply link can redirect to the company's own site
        self.allowed_sites.add(site_of(urlparse(landed).hostname or ""))
        if is_mcf_url(landed):  # e.g. an Indeed job whose Apply button leads there
            return ApplyResult("needs_you", "This site needs Singpass to apply, so you apply yourself.", landed, 0,
                               None, self.action_log, kind="singpass")
        no_tool_turns = 0
        last_sig, repeats = None, 0
        for step in range(1, max_steps + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                return ApplyResult("stopped", "Stopped by you.", self._page().url, step - 1, None, self.action_log)
            stopped = self._wait_out_human_check(step)  # code, not the model, deals with a human check
            if stopped:
                return stopped
            view = self.scan()
            user = prompts.apply_step_prompt(contact=self.contact, profile_text=self.profile_text, job=self.job,
                                             logins=self.logins, files=files, action_log=self.action_log,
                                             page_view=view, step=step, max_steps=max_steps,
                                             screening=self.screening)
            try:
                resp = self.llm.chat(stage="apply", system=system, user=user, tools=TOOLS, max_tokens=1200,
                                     job_id=self.job.get("id"))
            except BudgetExceeded as e:
                return ApplyResult("needs_you", f"Stopped: {e}. Finish this one yourself.", self._page().url, step - 1,
                                   None, self.action_log, kind="budget")
            except LLMError as e:
                return ApplyResult("failed", f"Model error: {e}", self._page().url, step - 1, None, self.action_log)
            if not resp.tool_calls:
                no_tool_turns += 1
                self.action_log.append(f"step {step}: (no action taken; you must call a tool)")
                if no_tool_turns >= 2:
                    return ApplyResult("needs_you", "The agent stopped acting. " + self.redact(resp.text)[:300],
                                       self._page().url, step, None, self.action_log, kind="stuck")
                continue
            no_tool_turns = 0
            sig = json.dumps([(c.name, c.args) for c in resp.tool_calls], sort_keys=True) + self._page().url
            repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= 2:
                return ApplyResult("needs_you", "The agent kept repeating the same action, so it was stopped.",
                                   self._page().url, step, None, self.action_log, kind="stuck")
            for call in resp.tool_calls[:12]:
                if call.name == "finish":
                    return self._finish(call.args, step)
                url_before = self._page().url
                try:
                    result = self.run_tool(call.name, call.args)
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> {result}"
                except NeedsHuman as e:
                    self.action_log.append(f"step {step}: {call.name} {call.args.get('element', '')} -> "
                                           "STOPPED: screening question with no saved answer")
                    self.log(f"Stopped: {e}")
                    return ApplyResult("needs_you", str(e), self._page().url, step, None, self.action_log,
                                       kind=f"screening:{e.topic}")
                except ToolError as e:
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> REFUSED: {e}"
                except PlaywrightError as e:
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> FAILED: {str(e).splitlines()[0][:160]}"
                line = self.redact(line)
                self.action_log.append(line)
                self.log(line)
                self._settle()
                if self._page().url != url_before:
                    # a click or redirect led here (goto can't reach new sites), so goto may come back to it
                    self.allowed_sites.add(site_of(urlparse(self._page().url).hostname or ""))
                    break  # page changed: look again before doing anything else
        return ApplyResult("needs_you", f"Gave up after {max_steps} steps without finishing.", self._page().url,
                           max_steps, None, self.action_log, kind="stuck")


    def _guard_network(self) -> None:
        """Abort any request this browser makes to JobHunt itself, the local network or a mailbox."""
        def handler(route):
            if request_allowed(route.request.url):
                route.continue_()
            else:
                route.abort("blockedbyclient")
        self._page().context.route("**/*", handler)


BUTTONISH = ("button", "submit", "image")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class ToolError(Exception):
    pass


class NeedsHuman(Exception):
    """A form needs something only the candidate can answer: the run stops as Needs you."""

    def __init__(self, message: str, topic: str = ""):
        super().__init__(message)
        self.topic = topic  # the screening answer it needs (a key of profile.SCREENING_FIELDS)
