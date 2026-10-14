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


# ── Scope: nothing to delete by hand ─────────────────────────────────

LEGACY_TEMPLATE = ("# What I'm looking for (template: fill this in, then delete this line)\n\n"
                   "The AI reads this whole page to decide yes or no on every posting, so be specific.\n"
                   "Short answers under each heading are enough; delete headings you don't care about.\n"
                   "Tip: \"Draft from my profile\" on the Scope page writes a first version for you.\n\n")


def legacy_scope() -> str:
    """What the Scope box held before this version: the intro + the guide."""
    from conftest import APP_DIR
    guide = (APP_DIR / "scope.example.md").read_text(encoding="utf-8").split("\n", 2)[2]
    return LEGACY_TEMPLATE + guide


def test_an_untouched_old_template_counts_as_empty(data_dir):
    (data_dir / "scope.md").write_text(legacy_scope(), encoding="utf-8")
    assert profile.load_scope_text() == "" and not profile.scope_ready(legacy_scope())


def test_writing_under_the_old_template_counts_even_with_its_first_line_left_in(data_dir):
    filled = legacy_scope().replace("(e.g. \"backend engineer - payments\", \"Unity developer - simulation, not mobile games\").\n-",
                                    "(e.g. \"backend engineer - payments\", \"Unity developer - simulation, not mobile games\").\n"
                                    "- Unity developer, simulation")
    assert "Unity developer, simulation" in filled
    (data_dir / "scope.md").write_text(filled, encoding="utf-8")
    text = profile.load_scope_text()
    assert profile.scope_ready(text) and "- Unity developer, simulation" in text and "## Target roles" in text
    assert "template" not in text and "Tip:" not in text and "Job titles or kinds of work" not in text
    assert text.startswith("# What I'm looking for\n")


def test_scope_page_starts_empty_with_the_guide_beside_it(client):
    page = client.get("/scope").text
    box = re.search(r'<textarea id="f_scope"[^>]*>(.*?)</textarea>', page, re.S).group(1)
    assert box == "" and "What helps the AI decide" in page and "<h3>Target roles</h3>" in page
    assert "Job titles or kinds of work you want" in page  # the hint, in the guide


def test_saving_an_empty_description_says_so(client):
    r = client.post("/scope", data={"keywords": "engineer", "scope_text": ""}, headers=SAME_ORIGIN)
    assert "Describe the jobs you want in the big box" in r.text


# ── Profile: one Save for the page ───────────────────────────────────

def test_one_save_keeps_contact_screening_and_profile_together(client):
    page = client.get("/profile").text
    form = page.split('<form method="post" action="/profile/save"', 1)[1].split("</form>", 1)[0]
    assert 'id="f_name"' in form and 'id="s_notice_period"' in form and 'id="f_profile_text"' in form
    assert form.count("Save profile") == 2 and "data-guard" in form
    for n in ("1. Upload your CVs", "2. Once a CV is uploaded", "3. Contact details", "4. Screening answers",
              "5. Check your profile"):
        assert n in page
    r = client.post("/profile/save", data={"name": "Alex Tan", "email": "alex@example.com", "links": "a\nb",
                                           "notice_period": "1 month", "profile_text": "# Alex\nBackend engineer"},
                    headers=SAME_ORIGIN)
    assert "Profile saved." in r.text
    assert profile.load_contact()["name"] == "Alex Tan" and profile.load_contact()["links"] == ["a", "b"]
    assert profile.load_screening()["notice_period"] == "1 month"
    assert profile.load_profile().startswith("# Alex")


def test_saving_the_empty_outline_is_not_a_profile(client):
    client.post("/profile/save", data={"name": "Alex", "email": "a@example.com",
                                       "profile_text": profile.PROFILE_TEMPLATE.replace("\n", "\r\n")}, headers=SAME_ORIGIN)
    assert profile.load_profile() == "" and ("Build or write your profile", "/profile#profiletext") in hunt.readiness()


def test_editable_forms_warn_before_leaving_unsaved(client):
    for url in ("/profile", "/scope", "/settings", "/accounts"):
        assert "data-guard" in client.get(url).text, url
    js = client.get("/static/app.js").text
    assert "beforeunload" in js and "form[data-guard]" in js


# ── a first hunt on sites JobHunt can apply on ───────────────────────

def ticked(page: str) -> list[str]:
    return re.findall(r'name="sites" value="(\w+)" checked', page)


def test_first_hunt_is_the_practice_board_then_real_boards(client):
    page = client.get("/hunt").text
    assert ticked(page) == ["demo"] and "start here" in page
    run = db.start_run("hunt", '{"jobs": 1, "sites": ["demo"]}')
    db.finish_run(run, "done", "1 package(s) built")
    page = client.get("/hunt").text
    assert ticked(page) == ["jobstreet", "linkedin"] and "start here" not in page


def test_the_old_default_counts_as_not_chosen(client):
    settings.update({"hunt": {"sites": ["mycareersfuture"]}})  # what earlier versions saved without asking
    assert ticked(client.get("/hunt").text) == ["demo"]


def test_your_own_choice_sticks(client, ready, monkeypatch):
    monkeypatch.setattr(hunt.runner, "start", lambda kind, params=None: 1)
    client.post("/hunt/start", data={"sites": ["indeed", "mycareersfuture"], "jobs": "3"}, headers=SAME_ORIGIN)
    assert ticked(client.get("/hunt").text) == ["mycareersfuture", "indeed"]
    client.post("/hunt/start", data={"sites": "demo", "jobs": "3"}, headers=SAME_ORIGIN)  # a practice run later
    assert ticked(client.get("/hunt").text) == ["demo"] and settings.load()["hunt"]["sites_chosen"] is True
