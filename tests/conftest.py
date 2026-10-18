"""Shared test setup.

- Every test gets its own empty data folder (JOBHUNT_DATA_DIR).
- Windows Credential Manager is swapped for an in-memory keyring.
- The model is never called for real: tests use FakeLLM, an OpenAI-style
  endpoint simulated with httpx.MockTransport, which records every request.
- The practice board (/demo) is served by uvicorn on a free local port.
"""
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

APP_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(APP_DIR))
if (APP_DIR / ".browsers").is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(APP_DIR / ".browsers"))

# Values that must never show up in a prompt, a log line or a file on disk.
API_KEY = "sk-test-APIKEY-7f3e9d2c1b"
LOGIN_USER = "alex.login.secret@example.org"
LOGIN_PASSWORD = "Pw-Secret-Hunter2-!x9"


class MemoryKeyring(KeyringBackend):
    priority = 100

    def __init__(self):
        super().__init__()
        self.store = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(d))
    from jobhunt import db, llm
    llm._price_cache = None
    db.init()
    return d


@pytest.fixture(autouse=True)
def no_internet_lookups(monkeypatch):
    """The company summary checks a website's address with DNS before opening it: never for real in tests.
    A test that wants an address to "exist" sets company.resolve itself."""
    from jobhunt import company

    def offline(host, port):
        raise OSError("no DNS in tests")
    monkeypatch.setattr(company, "resolve", offline)
    # nor the registry, the web search or Wikipedia: a test that wants an answer sets company_sources.http_get
    import httpx
    from jobhunt import company_sources

    def no_http(url, params=None, headers=None, timeout=20):
        raise httpx.ConnectError("no internet in tests")
    monkeypatch.setattr(company_sources, "http_get", no_http)
    from jobhunt import marketpay  # nor MyCareersFuture's search: a market pay test sets marketpay.http_post

    def no_post(url, body, timeout=20):
        raise httpx.ConnectError("no internet in tests")
    monkeypatch.setattr(marketpay, "http_post", no_post)
    monkeypatch.setattr(company_sources, "GAP_S", 0)
    monkeypatch.setattr(company_sources, "RETRY_WAIT_S", 0)


@pytest.fixture(autouse=True)
def memory_keyring():
    old = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(old)


@pytest.fixture
def secrets(memory_keyring):
    """A saved API key and one saved site login, as the Accounts page would store them."""
    from jobhunt import secrets_store, settings
    secrets_store.set_secret(secrets_store.api_key_name("openai_compatible"), API_KEY)
    secrets_store.set_secret(secrets_store.api_key_name("opencode_go"), API_KEY)
    secrets_store.set_secret(secrets_store.login_secret_name("default", "username"), LOGIN_USER)
    secrets_store.set_secret(secrets_store.login_secret_name("default", "password"), LOGIN_PASSWORD)
    settings.update({"logins": [{"name": "default", "domains": ["127.0.0.1"]}]})
    return {"api_key": API_KEY, "user": LOGIN_USER, "password": LOGIN_PASSWORD}


@pytest.fixture
def candidate():
    """The fictional test candidate's contact details, profile and scope."""
    from jobhunt import paths, profile
    profile.save_contact(json.loads((FIXTURES / "contact.json").read_text(encoding="utf-8")))
    profile.save_profile((FIXTURES / "profile.md").read_text(encoding="utf-8"))
    profile.save_scope_text(CANDIDATE_SCOPE)
    profile.save_screening({"work_auth": "Singapore Citizen", "notice_period": "1 month",
                            "expected_salary": "SGD 8,500 per month"})
    return paths.data_dir()


CANDIDATE_SCOPE = """# What I'm looking for

## Target roles
- Backend Engineer (Python), Platform Engineer

## Avoid (do NOT apply)
- Sales, support and mostly on-call roles

## Notes for the filter
- When unsure, include it.
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def demo_server():
    """The practice board + fake application site on http://127.0.0.1:<port>/demo."""
    import uvicorn
    from fastapi import FastAPI
    from jobhunt import demo_board

    app = FastAPI()
    app.include_router(demo_board.router)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/demo"
    server.should_exit = True
    t.join(5)


# ── the fake model ────────────────────────────────────────────────────

FILTER_SKIP_WORDS = ("intern", ".net architect")

TAILOR_REPLY = {
    "resume": {
        "summary": "Backend engineer with 5 years building Python services on AWS.",
        "section_order": ["experience", "education", "projects", "skills"],
        "experience": [
            {"company": "Shipwise Pte Ltd", "location": "Singapore", "title": "Senior Backend Engineer",
             "dates": "Mar 2023 - Present",
             "bullets": ["Rebuilt the shipment-tracking pipeline to handle 1.8M events/day, cutting p95 latency from 4.2s to 600ms",
                         "Led a team of 3 engineers to launch a carrier-rates API used by 120 merchants"]},
            {"company": "CartNest Commerce", "location": "Singapore", "title": "Backend Engineer",
             "dates": "Jul 2020 - Feb 2023", "bullets": ["Built the order-fulfilment service that processed SGD 40M in annual GMV"]},
        ],
        "education": [{"school": "National University of Singapore", "location": "Singapore",
                       "degree": "B.Comp. in Computer Science", "dates": "2016 - 2020", "details": []}],
        "projects": [{"name": "tidewatch", "place": "Open source", "role": "Author", "dates": "",
                      "bullets": ["Python library for idempotent message handling; 300 GitHub stars"]}],
        "skills": {"technical": "Python, FastAPI, PostgreSQL, AWS, Docker, Terraform", "certifications": "AWS Certified Developer",
                   "languages": "English, Mandarin", "interests": ""},
    },
    "cover_letter": {"greeting": "Dear Hiring Team,",
                     "paragraphs": ["I am applying for the role because it matches my backend work.",
                                    "At Shipwise I rebuilt a pipeline handling 1.8M events a day.", "Thank you for your time."]},
    "match_report": {
        "requirements": [{"requirement": "3+ years Python", "addressed_by": "5 years at Shipwise and CartNest", "strength": "strong"}],
        "emphasized": ["AWS pipeline work"], "deemphasized": ["mentoring"], "gaps": ["Kubernetes administration"],
        "score": 8, "score_reason": "Strong backend overlap",
    },
}

SCOPE_DRAFT = ("# What I'm looking for\n\n## Target roles\n- Backend Engineer (Python)\n\n"
               "## Avoid (do NOT apply)\n- Sales roles\n\n## Notes for the filter\n- When unsure, skip.\n")

SOURCE_RE = re.compile(r"<untrusted_source>\nSource (S\d+): (.*?)\nAddress: .*?\n\n(.*?)\n</untrusted_source>", re.S)


def fact(text, sid, quote=None):
    return {"text": text, "source": sid, "quote": quote if quote is not None else text}


def company_brain(user_prompt: str) -> dict:
    """A careful company-summary writer: every fact is a line it read in one of the sources."""
    nf = {"text": "not found", "source": "", "quote": ""}
    out = {k: dict(nf) for k in ("what_they_do", "industry", "job_location", "company_location", "hq_country", "size")}
    out["notable"], out["agency"] = [], False
    for sid, label, text in SOURCE_RE.findall(user_prompt):
        m = re.search(r"^Location: (.+)$", text, re.M)
        if sid == "S1" and m:
            out["job_location"] = fact(m.group(1), sid)
        m = re.search(r"^Company size: (.+)$", text, re.M)
        if m:
            out["size"] = fact(m.group(1) + " employees", sid, m.group(1))
        m = re.search(r"^Primary location: (.+)$", text, re.M)
        if m:
            out["company_location"] = fact(m.group(1), sid)
        m = re.search(r"(?i:headquartered) in ([A-Z][a-z]+(?:, [A-Z][a-z]+)?)", text)
        if m:
            out["hq_country"] = fact(m.group(1), sid, m.group(0))
        if sid != "S1" and not out["what_they_do"]["source"]:
            m = re.search(r"[A-Z][^.\n]{30,}\.", text.split("\n", 1)[-1])
            if m:
                out["what_they_do"] = fact(m.group(0), sid) | {"short": "Software company"}
        if "recruitment agency" in text:
            out["agency"] = True
            out["notable"].append(fact("A recruitment agency", sid, "recruitment agency"))
    return out


ROW_RE = re.compile(r'\[(e\d+)\] (\S+) "([^"]*)"')


def page_elements(user_prompt: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(page title, [(ref, kind, label)]) from an apply-step prompt."""
    view = user_prompt.split("Current page:", 1)[1]
    title = re.search(r"Title: (.*)", view).group(1).strip()
    return title, ROW_RE.findall(view)


def find_ref(elements, kind=None, label_start=""):
    for ref, k, label in elements:
        if (kind is None or k == kind) and label.lower().startswith(label_start.lower()):
            return ref
    raise AssertionError(f"no {kind} element starting {label_start!r} in {elements}")


def scripted_apply_brain(user_prompt: str) -> list[tuple[str, dict]]:
    """A well-behaved apply agent for the practice application site."""
    title, els = page_elements(user_prompt)
    if title == "Sign in":
        return [("type_secret", {"element": find_ref(els, "email"), "login": "default", "field": "username"}),
                ("type_secret", {"element": find_ref(els, "password"), "login": "default", "field": "password"}),
                ("click", {"element": find_ref(els, "button", "Sign in")})]
    if title == "Application":
        return [("fill", {"element": find_ref(els, "text", "Full name"), "text": "Alex Tan"}),
                ("fill", {"element": find_ref(els, "email", "Email"), "text": "alex.tan.test@example.com"}),
                ("fill", {"element": find_ref(els, "tel", "Phone"), "text": "+65 9000 0000"}),
                ("select_option", {"element": find_ref(els, "select", "Are you legally"), "option": "Yes"}),
                ("upload_file", {"element": find_ref(els, "file", "Resume"), "file": "resume"}),
                ("upload_file", {"element": find_ref(els, "file", "Cover letter"), "file": "cover_letter"}),
                ("click", {"element": find_ref(els, "button", "Submit application")})]
    if title == "Application received":
        return [("finish", {"status": "submitted", "summary": "Submitted on the practice site.",
                            "confirmation_text": "Thank you for applying!"})]
    return [("finish", {"status": "needs_you", "summary": f"Unexpected page {title}"})]


class FakeLLM:
    """An OpenAI-compatible endpoint in-process. Records every request body."""

    def __init__(self, apply_brain=scripted_apply_brain, tailor_reply=None):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.apply_brain = apply_brain
        self.tailor_reply = tailor_reply or TAILOR_REPLY
        self.filter_override = None  # callable(user prompt) -> raw reply text, or None for the normal verdict
        self.company_brain = company_brain  # callable(user prompt) -> the company summary (a dict), or raises
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        message = {"role": "assistant", "content": ""}
        if "screen job postings" in system:
            posting = user.split("<untrusted_job_posting>", 1)[1].lower()
            skip = any(w in posting.split("\n", 2)[1] for w in FILTER_SKIP_WORDS)
            message["content"] = json.dumps({"decision": "skip" if skip else "apply", "score": 3 if skip else 8,
                                             "reasons": ["fake reason"], "doubts": ""})
            if self.filter_override and self.filter_override(user) is not None:
                message["content"] = self.filter_override(user)
        elif "tailor a Harvard-style resume" in system:
            message["content"] = "```json\n" + json.dumps(self.tailor_reply) + "\n```"
        elif "fill in one job application" in system:
            calls = self.apply_brain(user)
            message["tool_calls"] = [{"id": f"call{i}", "type": "function",
                                      "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
        elif "factual company summary" in system:
            message["content"] = json.dumps(self.company_brain(user))
        elif "draft a job-search scope" in system:
            message["content"] = json.dumps({"scope_markdown": SCOPE_DRAFT, "keywords": ["backend engineer", "python developer"],
                                             "avoid_words": ["intern", "sales"]})
        elif "turn a person's CVs" in system:
            message["content"] = json.dumps({"contact": {"name": "Alex Tan"}, "profile_markdown": "# CANDIDATE PROFILE\n## Summary\nTest",
                                             "gaps": ["salary expectation"]})
        else:
            message["content"] = "OK"
        return httpx.Response(200, json={"choices": [{"message": message}],
                                         "usage": {"prompt_tokens": len(system + user) // 4, "completion_tokens": 50}})

    def prompts_text(self) -> str:
        """Everything the model was ever sent (bodies only; the key belongs in a header)."""
        return json.dumps(self.requests)


@pytest.fixture
def fake_llm(monkeypatch):
    """Point the app at FakeLLM (provider openai_compatible) for every task."""
    from jobhunt import hunt, secrets_store, settings
    from jobhunt.llm import LLMClient

    fake = FakeLLM()
    settings.update({"provider": "openai_compatible", "base_url": "http://fake-llm.invalid/v1",
                     "models": {s: "fake-model" for s in ("filter", "tailor", "apply", "profile")},
                     "custom_prices": {"fake-model": {"in": 1.0, "out": 2.0}}})

    def make_llm(s, meter, recorder, session_prefix=""):
        key = secrets_store.get_secret(secrets_store.api_key_name(s["provider"])) or ""
        return LLMClient(s, key, meter=meter, recorder=recorder, session_prefix=session_prefix, transport=fake.transport)

    monkeypatch.setattr(hunt, "make_llm", make_llm)
    return fake


def files_under(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


def assert_secret_free(text, where: str) -> None:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    for value in (API_KEY, LOGIN_USER, LOGIN_PASSWORD):
        assert value not in text, f"secret value found in {where}"
