"""Ask: a question, or a recruiter's whole message, answered from the resume and the saved answers."""
import json

import pytest
from fastapi.testclient import TestClient

from jobhunt import ask, db, hunt, profile, prompts, secrets_store
from jobhunt.web.app import create_app

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}

REPLY = {
    "answers": [
        {"question": "Singapore Citizen or PR?", "answer": "I'm a Singapore Citizen.", "basis": "Saved answer: work_auth",
         "sure": "yes", "profile_field": "work_auth", "short": "Singapore Citizen"},
        {"question": "Years in backend development?", "answer": "About 3 years.",
         "basis": "Backend Engineer, Lumen (2022-2025)", "sure": "partly", "profile_field": "years_experience",
         "short": "3", "extra": "dropped"},
        {"question": "Hands-on with GCP?", "answer": "Not on your resume.", "basis": "", "sure": "no",
         "profile_field": "not_a_field", "short": ""},
    ],
    "reply": "1. Singapore Citizen.\n2. About 3 years.",
    "missing": ["Current salary"],
}


class StubLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat_json(self, **kw):
        self.calls.append(kw)
        return self.reply, None


@pytest.fixture
def stub(monkeypatch):
    llm = StubLLM(REPLY)
    monkeypatch.setattr(hunt, "make_llm", lambda *a, **k: llm)
    return llm


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_a_recruiters_message_is_answered_kept_and_shown(client, secrets, candidate, stub):
    message = "Greetings!\n1. Are you a Singapore Citizen or PR?\n2. Years in backend?\n3. GCP?"
    r = client.post("/ask", data={"question": message}, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ask?id=")
    user = stub.calls[0]["user"]
    assert "<untrusted_question>" in user and "Years in backend?" in user  # pasted text is fenced as data
    assert "work_auth: Singapore Citizen" in user and "current_salary: (empty)" in user
    entry = ask.history()[0]
    assert entry["question"] == message and len(entry["answers"]) == 3
    assert "extra" not in entry["answers"][1] and entry["answers"][2]["field"] == ""  # only known fields survive
    page = client.get(r.headers["location"]).text
    assert "I&#39;m a Singapore Citizen." in page and ">Sure<" in page and "Draft reply" in page and "Copy reply" in page
    assert "Not on your resume" in page and "Current salary" in page
    # years_experience isn't saved yet: offered; work_auth already says the same: not offered again
    assert 'name="field" value="years_experience"' in page and 'name="field" value="work_auth"' not in page


def test_saving_an_answer_fills_the_profile(client, candidate):
    r = client.post("/ask/remember", data={"field": "years_experience", "value": "3", "id": "x"},
                    headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and profile.load_screening()["years_experience"] == "3"
    client.post("/ask/remember", data={"field": "nonsense", "value": "x"}, headers=SAME_ORIGIN)
    assert "nonsense" not in json.loads((profile.screening_path()).read_text(encoding="utf-8"))


def test_asking_about_a_job_includes_its_posting(client, secrets, candidate, stub):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="Paves", role="Backend Engineer",
                     posting_text="Needs REST APIs and AWS.", state="Ready")
    assert "Ask about this job" in client.get(f"/jobs/{jid}").text
    assert "About Backend Engineer at Paves" in client.get(f"/ask?job={jid}").text
    client.post("/ask", data={"question": "Do I meet this?", "job_id": jid}, headers=SAME_ORIGIN)
    assert "Needs REST APIs and AWS." in stub.calls[0]["user"] and ask.history()[0]["job_id"] == jid


def test_no_key_or_no_cv_says_what_to_do_first(client, candidate, stub):
    r = client.post("/ask", data={"question": "How many years?"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert "Connections" in r.headers["location"] and not stub.calls
    secrets_store.set_secret(secrets_store.api_key_name("opencode_go"), "k")
    profile.save_profile("")
    r = client.post("/ask", data={"question": "How many years?"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert "Upload%20your%20CV" in r.headers["location"] and not stub.calls


def test_an_empty_answer_is_an_error_and_is_logged(client, secrets, candidate, monkeypatch):
    monkeypatch.setattr(hunt, "make_llm", lambda *a, **k: StubLLM({"answers": []}))
    r = client.post("/ask", data={"question": "Hm?"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"] and not ask.history()
    run = db._query("SELECT * FROM runs WHERE kind = 'ask'")[0]
    assert run["status"] == "error"


def test_removing_an_earlier_answer(client, secrets, candidate, stub):
    client.post("/ask", data={"question": "One?"}, headers=SAME_ORIGIN)
    client.post("/ask", data={"question": "Two?"}, headers=SAME_ORIGIN)
    first, second = ask.history()[1], ask.history()[0]
    assert "One?" in client.get("/ask").text
    client.post("/ask/forget", data={"id": first["id"]}, headers=SAME_ORIGIN)
    assert [a["id"] for a in ask.history()] == [second["id"]]


def test_the_prompt_forbids_rounding_up_and_counts_from_today():
    system, user = prompts.ask_prompt("How many years of Python?", "## Experience\n- Dev - X - 2023-present",
                                      [("cv.pdf", "Python dev")], {"work_auth": ""}, "19 October 2026")
    assert "Never round up" in system and "19 October 2026" in system and "Not on your resume" in system
    assert "<untrusted_document>" in user and "work_auth: (empty)" in user
