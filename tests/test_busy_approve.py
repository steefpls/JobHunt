"""While a hunt (or any task) runs, Approve & apply is disabled and says why. Zech saw only '"Waiting' as
its tooltip (the title attribute broke at its first space) and nothing on the page said what was running.
"""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, hunt
from jobhunt.web.app import create_app


@pytest.fixture
def client(data_dir):
    return TestClient(create_app())


class Tags(HTMLParser):
    """Every start tag with its attributes, as a browser would read them."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def job(company, source, apply_url, state="Ready", **fields):
    jid = db.add_job(source=source, url=f"https://{source}.example/{company}", company=company, role="Engineer",
                     apply_url=apply_url, state="Ready", folder=company)
    if state != "Ready" or fields:
        db.update_job(jid, state=state, **fields)
    return jid


# ── Approve & apply while a task runs ─────────────────────────────────

def busy(monkeypatch, kind):
    real = hunt.runner.status
    monkeypatch.setattr(hunt.runner, "status", lambda: {**real(), "busy": True, "kind": kind})


def approve_buttons(html):
    tags = Tags(html).tags
    return [a for t, a in tags if t == "button" and "data-wait-busy" in a]


def test_approve_while_a_hunt_runs_says_why_in_one_whole_tooltip_and_on_the_page(client, monkeypatch):
    jid = job("Acme", "jobstreet", "https://sg.jobstreet.com/job/1")
    busy(monkeypatch, "hunt")
    for url in ("/", "/hunt", f"/jobs/{jid}"):
        buttons = approve_buttons(client.get(url).text)
        assert buttons, url
        for b in buttons:
            assert "disabled" in b
            assert b["title"] == "A hunt is running. You can approve this when it finishes.", url
    for url in ("/", "/hunt"):  # said once above the list, not only on hover
        notes = [a for t, a in Tags(client.get(url).text).tags if t == "p" and "data-busy-note" in a]
        assert len(notes) == 1 and "hidden" not in notes[0], url
        assert "A hunt is running. You can approve these when it finishes." in client.get(url).text
    assert client.get("/api/status").json()["busy_text"] == "A hunt is running"


def test_other_tasks_are_named_too(client, monkeypatch):
    job("Acme", "jobstreet", "https://sg.jobstreet.com/job/1")
    busy(monkeypatch, "replies")
    (b,) = approve_buttons(client.get("/").text)
    assert b["title"] == "Indeed and JobStreet are being checked for replies. You can approve this when it finishes."


def test_idle_approve_is_enabled_with_no_note(client):
    job("Acme", "jobstreet", "https://sg.jobstreet.com/job/1")
    html = client.get("/").text
    (b,) = approve_buttons(html)
    assert "disabled" not in b and "title" not in b
    notes = [a for t, a in Tags(html).tags if t == "p" and "data-busy-note" in a]
    assert len(notes) == 1 and "hidden" in notes[0]
    assert client.get("/api/status").json()["busy_text"] == ""
