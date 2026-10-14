"""The UX review fixes: Start says what's missing, Scope without a hidden line, one Save on Profile,
a sensible first hunt, what a finished hunt found, and why a job needs you."""
import re

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, hunt, profile, settings
from jobhunt.web.app import create_app

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def ready(secrets, candidate):
    settings.update({"scope": {"keywords": ["backend engineer"]}})


def start_button(page: str) -> str:
    return re.search(r'<button[^>]*id="startBtn"[^>]*>', page).group(0)


# ── Start says what's missing ─────────────────────────────────────────

def test_start_is_greyed_out_with_whats_missing(client, monkeypatch):
    started = []
    monkeypatch.setattr(hunt.runner, "start", lambda kind, params=None: started.append(kind))
    page = client.get("/hunt").text
    assert "Before you can start" in page and "disabled" in start_button(page)
    for what, href in [("Add your AI key", "/accounts"), ("Add your name and email", "/profile#contact"),
                       ("Build or write your profile", "/profile#profiletext"),
                       ("Add at least one search keyword", "/scope"), ("Describe the jobs you want", "/scope#describe")]:
        assert f'<a href="{href}">{what}</a>' in page
    r = client.post("/hunt/start", data={"sites": "demo", "jobs": "1"}, headers=SAME_ORIGIN)
    assert "Before a hunt can start: add your AI key;" in r.text and not started


def test_start_is_enabled_once_everything_is_done(client, ready):
    page = client.get("/hunt").text
    assert "Before you can start" not in page and "disabled" not in start_button(page)
    assert page.count('class="tick done"') == 0  # (the Hunt page has no checklist)
    dash = client.get("/").text
    assert "Get started" not in dash  # all three ticked: the checklist goes away


def test_dashboard_checklist_ticks_follow_the_same_rules(client, secrets):
    profile.save_contact({"name": "Alex", "email": "a@example.com"})
    profile.save_profile("# Me")
    dash = client.get("/").text
    assert dash.count('class="tick done"') == 2  # key + profile; scope still to do


def test_a_hunt_started_anyway_stops_with_the_same_list(secrets, fake_llm):
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"]})
    runner.wait(30)
    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert run["summary"].startswith("Before a hunt can start: add your name and email;")


def test_draft_from_my_profile_is_greyed_out_without_a_profile(client, secrets):
    page = client.get("/scope").text
    assert re.search(r'<button[^>]*formaction="/scope/draft"[^>]*disabled', page)
    assert "needs a profile to draft from" in page
