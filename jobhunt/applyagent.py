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
- The page keeps loading while the agent waits or the model thinks: only
  requests that might be blocked (an IP address, localhost, a mailbox) are
  checked in Python, and every pause lets the browser carry on. (Playwright's
  sync API only answers a checked request while Python is inside a browser
  call, so checking every request froze pages built by JavaScript: the agent
  saw "Loading...", waited, and gave up.)
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
SETTLE_IDLE_S = 4  # after a click or page load: how long the next part of a JavaScript form may take to load
QUIET_S = 0.4  # ...and how long the page must stay still before the agent looks at it again
SAME_TURN_LIMIT = 3  # the same actions on an unchanged page this many turns in a row: stuck
NO_CHANGE_LIMIT = 5  # this many turns in a row whose actions left the page exactly as it was: stuck
# Captcha widgets live in frames from these sites (hCaptcha, Google's reCAPTCHA, Cloudflare Turnstile, Arkose).
CAPTCHA_FRAME = re.compile(r"^https://([a-z0-9-]+[.])*(hcaptcha[.]com|google[.]com/recaptcha|recaptcha[.]net|"
                           r"challenges[.]cloudflare[.]com|arkoselabs[.]com|funcaptcha[.]com)", re.I)
CHOICES_WAIT_S = 4  # how long a dropdown's list of choices may take to show

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


# The requests the network rule has to look at: an IP address, a *.localhost name or a mailbox host. Matched
# inside the browser, so every other request goes ahead without waiting for Python (see the module notes).
GUARDED_URL = re.compile(r"^https?://(?:[^/?#@]*@)?(?:\[[^\]/]*\]|[0-9.]+|(?:[^/?#:]*\.)?localhost\.?|"
                         + "|".join(re.escape(h) for h in sorted(BLOCKED_HOSTS)) + r")(?::\d*)?(?:[/?#]|$)", re.I)


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
  const groupOf = (el, label) => {
    const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend');
    if (lg) return clean(lg.innerText);
    const g = el.closest('[role=radiogroup],[role=group]');
    if (g) { const by = g.getAttribute('aria-labelledby'); const l = by && document.getElementById(by);
      const t = g.getAttribute('aria-label') || (l ? l.innerText : ''); if (t) return clean(t); }
    let box = el.parentElement && el.parentElement.closest('div,li,p,td,section');
    // A choice, or a box named only by its code name (cards[..][field0]): Lever puts the question beside the
    // field's own box, so look a little further out for words that aren't the choices' own labels.
    const own = b => { let t = b.innerText || ''; for (const l of b.querySelectorAll('label, option, button')) t = t.replace(l.innerText || '', ''); return t.trim(); };
    if (['radio', 'checkbox'].includes(el.type) || !label || (/^[\w\[\].:-]+$/.test(label) && /[\[\]_]/.test(label)))
      for (let up = 0; box && up < 4 && !own(box); up++) box = box.parentElement;
    const t = box ? (box.innerText || '') : '';
    return t.length <= 300 ? clean(t) : '';
  };
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox], [role=radio], [role=combobox], [role=option], [role=menuitem], [role=menuitemradio], [role=switch], [role=tab], [contenteditable=true]';
  // ids from an earlier look stay on elements a form step has since hidden: a new id must find only its own element
  for (const old of document.querySelectorAll('[data-jh-ref]')) old.removeAttribute('data-jh-ref');
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden') continue;
    if (!visible(el) && el.type !== 'file') continue;
    // a type-to-search dropdown's stand-in box that only carries "required" for the browser
    if (el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0 && !['file', 'radio', 'checkbox'].includes(el.type)) continue;
    n++; const ref = 'e' + n; el.setAttribute('data-jh-ref', ref);
    const tag = el.tagName.toLowerCase();
    let kind = el.getAttribute('role') || tag;
    if (tag === 'input') kind = (el.getAttribute('role') === 'combobox' ? 'combobox' : (el.type || 'text'));
    if (tag === 'button' && el.getAttribute('aria-haspopup') === 'listbox') kind = 'combobox';  // Workday's dropdowns
    if (tag === 'a') kind = 'link';
    const item = {ref, kind, label: labelOf(el), required: !!(el.required || el.getAttribute('aria-required') === 'true'), disabled: !!el.disabled};
    if (el.getAttribute('aria-invalid') === 'true') item.invalid = true;
    if (kind === 'combobox' || (!['a', 'button'].includes(tag) && !['submit', 'button', 'image', 'reset'].includes(el.type))) {
      item.group = groupOf(el, item.label); item.name = (el.getAttribute('name') || el.id || '').slice(0, 60);
      if (item.group === item.label) item.group = '';
    }
    if (tag === 'input' && !['checkbox','radio','file','submit','button','password','image','reset'].includes(el.type)) item.value = (el.value || '').slice(0, 80);
    if (tag === 'textarea') item.value = (el.value || '').slice(0, 80);
    if (tag === 'input' && el.type === 'password') item.value = el.value ? '(filled)' : '';
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = el.checked;
    if (tag === 'select') { item.value = (el.options[el.selectedIndex] || {}).text || ''; item.options = Array.from(el.options).slice(0, 30).map(o => clean(o.text));
      if (el.options.length > 30) item.more_options = el.options.length - 30; }
    // a box that offers suggestions as you type (a city, a school): what is typed only counts once one is picked
    if (kind !== 'combobox' && tag === 'input' && ['text', 'search', ''].includes(el.type) &&
        (el.getAttribute('aria-autocomplete') || el.getAttribute('list') ||
         /location|city|address|school|university|college|autocomplete|typeahead/i.test((el.name || '') + ' ' + (el.id || '') + ' ' + el.className)))
      item.suggests = true;
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 120);
    if (el.type === 'file') item.files = el.files ? el.files.length : 0;
    if (kind === 'combobox') {  // what it shows as chosen sits beside the box you type in, not in its value
      const box = el.parentElement && el.parentElement.closest('[class*=control], [class*=value-container], [class*=combobox], [class*=dropdown]');
      const shown = clean(tag === 'button' ? el.innerText : (box ? box.innerText : ''));
      item.value = clean(el.value || '') || (/^(select|choose|search)\b/i.test(shown) ? '' : shown);
    }
    if (el.getAttribute('aria-selected') === 'true') item.selected = true;
    out.push(item);
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n');
  // what the form says went wrong ("This field is required"): on a long page it can sit past the text cap
  const alerts = [];
  for (const el of document.querySelectorAll('[role=alert], [aria-live=assertive], [class*=error], [class*=invalid]')) {
    const t = clean(el.innerText);
    if (t && t.length <= 200 && visible(el) && !alerts.includes(t) && alerts.length < 10) alerts.push(t);
  }
  return {elements: out, text, alerts, next: n, title: document.title};
}
"""

# In every page the apply browser opens: when the page last changed (for ApplyAgent._wait_quiet).
DOM_CHANGES_JS = """(() => { window.__jhChanged = Date.now();
  new MutationObserver(() => { window.__jhChanged = Date.now(); })
    .observe(document, {subtree: true, childList: true, attributes: true, characterData: true}); })();"""

# Suggestions a box shows that aren't marked as choices (Lever's location: plain <div>s): short new lines of text
# just under the box. MARK_NEAR_JS tags what shows there before typing; SUGGESTIONS_JS finds what is new since,
# matching the typed text, tags each one data-jh-pick and returns their texts.
_NEAR_JS = r"""
const near = (el, c, r0) => {
  if (c === el || c.contains(el) || c.children.length > 3 || c.querySelector('input, select, textarea')) return false;
  const r = c.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(c).visibility !== 'hidden' &&
         r.top >= r0.top && r.top <= r0.bottom + 400 && r.right > r0.left && r.left < r0.right;
};
const zone = el => document.body.querySelectorAll('li, div, span, a, p, td');
"""
MARK_NEAR_JS = "e => {" + _NEAR_JS + r"""
  const r0 = e.getBoundingClientRect();
  for (const c of zone(e)) if (near(e, c, r0)) c.setAttribute('data-jh-before', '1');
}"""
SUGGESTIONS_JS = "(e, typed) => {" + _NEAR_JS + r"""
  for (const o of document.querySelectorAll('[data-jh-pick]')) o.removeAttribute('data-jh-pick');
  const word = (typed || '').trim().toLowerCase().split(/[\s,]+/)[0] || '';
  const r0 = e.getBoundingClientRect(); const found = [];
  for (const c of zone(e)) {
    if (c.hasAttribute('data-jh-before') || !near(e, c, r0)) continue;
    const t = (c.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 120 && t.toLowerCase().includes(word)) found.push(c);
    if (found.length >= 30) break;
  }
  const inner = found.filter(c => !found.some(d => d !== c && c.contains(d))).slice(0, 15);
  return inner.map((c, i) => { c.setAttribute('data-jh-pick', String(i)); return c.innerText.replace(/\s+/g, ' ').trim(); });
}"""

TOOLS = [
    {"name": "click", "description": "Click a button, link, radio button or other element.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string", "description": "element id like e12"}},
                    "required": ["element"]}},
    {"name": "fill", "description": "Type text into a text box or text area (replaces what is there). Not for passwords.",
     "parameters": {"type": "object", "properties": {"element": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["element", "text"]}},
    {"name": "select_option", "description": "Choose an option in a dropdown by its visible text: a select, or a "
     "combobox (a type-to-search or list dropdown: JobHunt opens it, types and clicks the matching choice).",
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
                 sleep=None):
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
        self.sleep = sleep or self._pause
        self.logins = settings.get("logins", [])
        self.refs: dict = {}
        self.action_log: list[str] = []
        self.clicked_since_fill = False
        self.texts_before_clicks: list[str] = []  # page text just before each button click
        self.allowed_sites: set[str] = set()  # where goto may go: the start site + sites clicks led to

    # ── page view ──────────────────────────────────────────────────────
    def _page(self):
        other_tabs = list(getattr(self.browser, "_tabs", {}).values())  # a hunt's own tabs (Indeed's search page)

        def open_pages(context):
            return [p for p in context.pages if not p.is_closed() and not any(p is t for t in other_tabs)]
        current = getattr(self.browser, "_page", None)
        if current is not None and current.is_closed() and open_pages(current.context):
            # a popup (a "sign in with..." window) closed itself: carry on in the page before it, not a blank one
            self.browser._page = open_pages(current.context)[-1]
        page = self.browser.page
        pages = open_pages(page.context)
        if pages and pages[-1] is not page:  # a click opened a new tab: follow it
            self.browser._page = pages[-1]
            page = pages[-1]
        return page

    def _pause(self, seconds: float) -> None:
        """Wait while the browser carries on loading (time.sleep would hold up a page's checked requests)."""
        seconds = max(0.0, float(seconds))
        try:
            self._page().wait_for_timeout(seconds * 1000)
        except PlaywrightError as e:
            if self.browser.is_closed_error(e):
                raise
            time.sleep(seconds)

    def scan(self) -> str:
        page = self._page()
        self.refs = {}
        elements, texts, alerts, n = [], [], [], 0
        for frame in page.frames:
            try:
                res = frame.evaluate(PAGE_SCAN_JS, n)
            except PlaywrightError:
                continue
            n = res["next"]
            captcha = bool(CAPTCHA_FRAME.search(frame.url or ""))
            for el in res["elements"]:
                if captcha:
                    el["captcha"] = True
                self.refs[el["ref"]] = (frame, el)
            elements += res["elements"]
            alerts += [a for a in res.get("alerts") or [] if a not in alerts]
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
                ("\n\nMessages on the page (errors, alerts):\n" + "\n".join(f"- {a}" for a in alerts[:10])
                 if alerts else "") + "\n\nVisible text:\n" + text)
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
        if e.get("invalid"):
            s += " (marked invalid)"
        if e.get("captcha"):
            s += " (captcha: only the candidate can do this)"
        if "value" in e:
            s += f" value=\"{e['value']}\""
        if "checked" in e:
            s += f" checked={str(e['checked']).lower()}"
        if e.get("options"):
            s += " options: " + " | ".join(e["options"])
            if e.get("more_options"):
                s += f" (+{e['more_options']} more: name the one you want)"
        if e.get("suggests"):
            s += " (suggests as you type: fill it and JobHunt picks the matching suggestion)"
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
        if name in ("click", "fill", "set_checkbox", "select_option") and args.get("element") in self.refs                 and self.refs[args["element"]][1].get("captcha"):
            raise NeedsHuman("The form has a captcha (an \"I am human\" check) that only you can do, so JobHunt "
                             "stopped there. Open the apply page, fill it in and tick the check yourself, then press "
                             "I applied.", kind="verify")
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
            if el["kind"] == "combobox" or el.get("suggests"):  # typed text alone doesn't stick in a dropdown
                return self._choose(loc, el, str(args.get("text", ""))[:200], typed_ok=True)
            loc.fill(str(args.get("text", ""))[:4000], timeout=10000)
            self.clicked_since_fill = False
            return f"filled \"{el['label']}\""
        if name == "select_option":
            loc, el = self._locator(args.get("element", ""))
            self._check_screening(el)
            option = str(args.get("option", ""))
            if el["kind"] != "select":
                return self._choose(loc, el, option)
            options = loc.evaluate("e => Array.from(e.options).map(o => o.text.replace(/\\s+/g, ' ').trim())")
            best = option if option in options else _best_match(option, options)  # "yes" for "Yes", a few words
            if best is None:  # say so at once (a wrong label used to mean a 10 s wait, three times over)
                near = [o for o in options if set(_norm(o).split()) & set(_norm(option).split())][:10] or options[:10]
                raise ToolError(f"no option {option!r} in \"{el['label']}\"; some options: {' | '.join(near)[:300]}")
            loc.select_option(index=options.index(best), timeout=10000)
            return f"selected \"{best}\" in \"{el['label']}\""
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

    def _choose(self, loc, el: dict, option: str, typed_ok: bool = False) -> str:
        """Pick a choice in a dropdown that isn't a plain <select> (Greenhouse's type-to-search boxes, Workday's
        list buttons): open it, type to narrow it down where it takes typing, then click the matching choice."""
        frame = self.refs[el["ref"]][0]
        editable = loc.evaluate("e => e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.isContentEditable")
        if editable:
            loc.evaluate(MARK_NEAR_JS)  # what already shows under the box isn't a suggestion
        loc.click(timeout=10000)
        if editable and option:
            # the last letter as a real key press: some boxes (Lever's location) only search on a key
            loc.fill(option[:-1], timeout=10000)
            loc.press_sequentially(option[-1:], timeout=10000)
        choices = frame.locator("[role=option]:visible")
        texts: list[str] = []
        for _ in range(CHOICES_WAIT_S * 4):  # a list filled by the site's search (a city) takes a moment
            texts = [t.strip() for t in choices.all_inner_texts()]
            if any(texts):
                break
            if editable:  # suggestions that aren't marked as choices: new short lines just under the box
                texts = loc.evaluate(SUGGESTIONS_JS, option)
                if texts:
                    choices = frame.locator("[data-jh-pick]")
                    break
            self.sleep(0.25)
        best = _best_match(option, texts)
        if best is None:
            if typed_ok and editable and (not any(texts) or el.get("suggests")):  # a box that merely suggests
                self.clicked_since_fill = False
                seen = " | ".join(t for t in texts if t)[:200]
                return f"filled \"{el['label']}\"" + (f" (no suggestion matched; it offered: {seen})" if seen else
                                                      " (no list of choices appeared)")
            if editable and option and not any(texts):  # nothing matched what was typed: show it all instead
                loc.fill("", timeout=10000)
                self.sleep(0.5)
                texts = [t.strip() for t in frame.locator("[role=option]:visible").all_inner_texts()]
            loc.press("Escape")
            seen = " | ".join(t for t in texts if t)[:300]
            raise ToolError(f"no choice matching {option!r} in \"{el['label']}\"" +
                            (f"; the choices are: {seen}" if seen else "; no choices appeared"))
        choices.nth(texts.index(best)).click(timeout=10000)
        self.clicked_since_fill = False
        return f"chose \"{best[:80]}\" in \"{el['label']}\""

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

    def _settle(self, busy: bool = False) -> None:
        """After an action. `busy`: a click or page load, after which a form built by JavaScript fetches its
        next part: give that a few seconds to arrive (a page that never goes quiet just costs the wait)."""
        page = self._page()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            if busy:
                self._wait_quiet()
        except PlaywrightError as e:
            if self.browser.is_closed_error(e):
                raise
        self.sleep(float(self.settings.get("pacing", {}).get("action_delay_s", 0.8)))

    def _watch_page_activity(self) -> None:
        """Count the requests a page has open and stamp its last DOM change, for _wait_quiet. (Playwright's
        "networkidle" only describes the first load, not what a click starts afterwards.)"""
        ctx = self._page().context
        if not hasattr(ctx, "on"):  # a stand-in browser (tests): nothing to watch
            return
        self._open_requests, self._last_request = {}, 0.0

        def started(req):
            if req.resource_type in ("document", "xhr", "fetch", "script"):
                self._open_requests[req] = self._last_request = time.monotonic()

        def ended(req):
            self._open_requests.pop(req, None)
            self._last_request = time.monotonic()
        ctx.on("request", started)
        ctx.on("requestfinished", ended)
        ctx.on("requestfailed", ended)
        ctx.add_init_script(DOM_CHANGES_JS)

    def _wait_quiet(self) -> None:
        """Until the page has no request open (one older than a few seconds is a long poll: not counted) and
        neither it nor the DOM has changed for a moment, at most SETTLE_IDLE_S."""
        if not hasattr(self, "_open_requests"):
            return
        page = self._page()
        end = time.monotonic() + SETTLE_IDLE_S
        while time.monotonic() < end:
            now = time.monotonic()
            busy = any(now - t < 3 for t in list(self._open_requests.values()))
            try:
                dom_age = page.evaluate("() => window.__jhChanged ? Date.now() - window.__jhChanged : 1e9")
            except PlaywrightError as e:  # a new page loading
                if self.browser.is_closed_error(e):
                    raise
                dom_age = 0
            if not busy and now - self._last_request >= QUIET_S and dom_age >= QUIET_S * 1000:
                return
            page.wait_for_timeout(100)

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
        self._watch_page_activity()
        self.allowed_sites.add(site_of(urlparse(start_url).hostname or ""))
        self._page().goto(start_url, wait_until="domcontentloaded", timeout=45000)
        self._settle(busy=True)
        landed = self._page().url  # a job board's apply link can redirect to the company's own site
        self.allowed_sites.add(site_of(urlparse(landed).hostname or ""))
        if is_mcf_url(landed):  # e.g. an Indeed job whose Apply button leads there
            return ApplyResult("needs_you", "This site needs Singpass to apply, so you apply yourself.", landed, 0,
                               None, self.action_log, kind="singpass")
        no_tool_turns = 0
        last_sig, repeats = None, 0
        last_view, unchanged = None, 0
        for step in range(1, max_steps + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                return ApplyResult("stopped", "Stopped by you.", self._page().url, step - 1, None, self.action_log)
            stopped = self._wait_out_human_check(step)  # code, not the model, deals with a human check
            if stopped:
                return stopped
            view = self.scan()
            # a form that takes none of what is typed into it (a box that clears itself) loops forever otherwise
            unchanged = unchanged + 1 if view == last_view else 0
            last_view = view
            if unchanged >= NO_CHANGE_LIMIT:
                what = "; ".join(ln.split(": ", 1)[-1] for ln in self.action_log[-3:])
                self.action_log.append(f"step {step}: STOPPED: the page stopped changing")
                return ApplyResult("needs_you", f"The last {NO_CHANGE_LIMIT} rounds of actions left the page exactly as "
                                   f"it was (the last ones: {self.redact(what)[:300]}), so JobHunt stopped there. "
                                   "Finish this one yourself.", self._page().url, step - 1, None, self.action_log,
                                   kind="stuck")
            user = prompts.apply_step_prompt(contact=self.contact, profile_text=self.profile_text, job=self.job,
                                             logins=self.logins, files=files, action_log=self.action_log,
                                             page_view=view, step=step, max_steps=max_steps,
                                             screening=self.screening)
            try:
                resp = self.llm.chat(stage="apply", system=system, user=user, tools=TOOLS, max_tokens=self._max_tokens(),
                                     job_id=self.job.get("id"))
            except BudgetExceeded as e:
                return ApplyResult("needs_you", f"Stopped: {e}. Finish this one yourself.", self._page().url, step - 1,
                                   None, self.action_log, kind="budget")
            except LLMError as e:  # the AI provider failed (rate limit, outage): the job goes back to you, not Error
                self.action_log.append(f"step {step}: STOPPED: the AI provider failed")
                return ApplyResult("needs_you", f"The AI provider failed while applying ({e}), so JobHunt stopped "
                                   f"{'before filling anything in' if step == 1 else 'there'}. Press Try again later, "
                                   "or finish it yourself.", self._page().url, step - 1, None, self.action_log,
                                   kind="model")
            if not resp.tool_calls:
                no_tool_turns += 1
                self.action_log.append(f"step {step}: (you replied without calling a tool: call one, or finish)")
                if no_tool_turns >= 2:
                    said = self.redact(resp.text).strip()[:300]
                    return ApplyResult("needs_you", "The AI stopped choosing actions on this page"
                                       + (f" (it said: {said})" if said else "") + ". Finish this one yourself.",
                                       self._page().url, step, None, self.action_log, kind="stuck")
                continue
            no_tool_turns = 0
            # the same actions again only count as going round in circles when the page hasn't changed either
            # (clicking Next again after fixing a field the form complained about is fine)
            sig = json.dumps([(c.name, c.args) for c in resp.tool_calls], sort_keys=True) + view
            repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= SAME_TURN_LIMIT - 1:
                what = "; ".join(self._call_text(c) for c in resp.tool_calls[:3])
                self.action_log.append(f"step {step}: STOPPED: the same actions on an unchanged page")
                return ApplyResult("needs_you", f"The agent tried the same thing {SAME_TURN_LIMIT} times ({what}) "
                                   "and the page didn't change, so it stopped there. Finish this one yourself.",
                                   self._page().url, step, None, self.action_log, kind="stuck")
            for call in resp.tool_calls[:12]:
                if call.name == "finish":
                    return self._finish(call.args, step)
                url_before = self._page().url
                clicked = call.name == "click" and self.refs.get(str(call.args.get("element", "")), (None, {}))[1] \
                    .get("kind") not in ("radio", "checkbox")
                try:
                    result = self.run_tool(call.name, call.args)
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> {result}"
                except NeedsHuman as e:
                    why = "a captcha" if e.kind == "verify" else "screening question with no saved answer"
                    self.action_log.append(f"step {step}: {call.name} {call.args.get('element', '')} -> STOPPED: {why}")
                    self.log(f"Stopped: {e}")
                    return ApplyResult("needs_you", str(e), self._page().url, step, None, self.action_log,
                                       kind=e.kind or f"screening:{e.topic}")
                except ToolError as e:
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> REFUSED: {e}"
                except PlaywrightError as e:
                    if self.browser.is_closed_error(e):
                        raise
                    line = f"step {step}: {call.name} {call.args.get('element', '')} -> FAILED: {_failure(e)}"
                line = self.redact(line)
                self.action_log.append(line)
                self.log(line)
                self._settle(busy=clicked or call.name == "goto")
                if self._page().url != url_before:
                    # a click or redirect led here (goto can't reach new sites), so goto may come back to it
                    self.allowed_sites.add(site_of(urlparse(self._page().url).hostname or ""))
                    break  # page changed: look again before doing anything else
                if clicked:
                    break  # a button or link click usually changes the form without a new address: look again too
        return ApplyResult("needs_you", f"Gave up after {max_steps} steps without finishing.", self._page().url,
                           max_steps, None, self.action_log, kind="stuck")

    def _max_tokens(self) -> int:
        """Room for the reply. A model that thinks first spends part of it thinking: with too little, the reply
        comes back with no action and the run stops as stuck."""
        return 4000 if self.settings.get("reasoning") else 1200

    @staticmethod
    def _call_text(call) -> str:
        bits = [str(call.args.get(k)) for k in ("element", "option", "seconds", "url") if call.args.get(k) is not None]
        return f"{call.name} {' '.join(bits)}".strip()

    def _guard_network(self) -> None:
        """Abort any request this browser makes to JobHunt itself, the local network or a mailbox. Only the
        requests GUARDED_URL matches come to Python; everything else loads without waiting for it."""
        def handler(route):
            if request_allowed(route.request.url):
                route.continue_()
            else:
                route.abort("blockedbyclient")
        self._page().context.route(GUARDED_URL, handler)


def _best_match(wanted: str, choices: list[str]) -> str | None:
    """The choice meaning `wanted`: the same words, else the only one starting with them, else the only one
    containing them (or contained in them: "Singapore" for "Singapore Citizen" in a country list)."""
    w = _norm(wanted)
    if not w:
        return None
    def has(hay: str, needle: str) -> bool:
        return bool(needle) and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay) is not None
    normed = [_norm(c) for c in choices]
    for test in (lambda c: c == w, lambda c: c.startswith(w), lambda c: has(c, w), lambda c: has(w, c)):
        hits = [choices[i] for i, c in enumerate(normed) if test(c)]
        if hits:
            return min(hits, key=len)  # "Singapore" before "Singapore - Central"
    return None


def _failure(e: Exception) -> str:
    """A Playwright error in one line, with the reason from its call log when there is one ("... intercepts
    pointer events" is a cookie banner or dialog in the way; "not visible" a field the form has hidden)."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    first = lines[0][:160] if lines else type(e).__name__
    why = next((ln for ln in reversed(lines[1:]) if re.search(
        r"intercepts pointer events|not visible|not enabled|not editable|detached|outside of the viewport|"
        r"did not find some options|not a select", ln)), "")
    return first + (f" ({why[:160]})" if why else "")


BUTTONISH = ("button", "submit", "image")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class ToolError(Exception):
    pass


class NeedsHuman(Exception):
    """A form needs something only the candidate can answer: the run stops as Needs you."""

    def __init__(self, message: str, topic: str = "", kind: str = ""):
        super().__init__(message)
        self.topic = topic  # the screening answer it needs (a key of profile.SCREENING_FIELDS)
        self.kind = kind  # what kind of stop when it isn't a screening answer ("verify": a captcha)
