"""The redesign: To review as the home page, Applications as its own page, Undo after a change,
CVs that come back, icons from one sprite, and no browser confirm() boxes left."""
import re

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, paths
from jobhunt.web.app import HERE, create_app, initials

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_home_is_the_review_queue_and_applications_has_the_table(client):
    ready = db.add_job(source="jobstreet", url="https://x/1", company="Lumen Analytics", role="Backend Engineer", state="Ready")
    db.add_job(source="jobstreet", url="https://x/2", company="Shopee", role="Platform Engineer", state="Applied")
    home = client.get("/").text
    assert "To review" in home and f'href="/jobs/{ready}"' in home and "Shopee" not in home  # only what needs you
    apps = client.get("/applications").text
    assert "Lumen Analytics" in apps and "Shopee" in apps and "<table" in apps
    filtered = client.get("/applications?state=Applied").text
    assert "Shopee" in filtered and "Lumen Analytics" not in filtered.split("<table", 1)[1]
    r = client.get("/?state=Applied", follow_redirects=False)  # an old filtered link still lands somewhere sensible
    assert r.status_code == 303 and r.headers["location"] == "/applications?state=Applied"


def test_nothing_to_review_says_so_and_offers_a_hunt(client):
    home = client.get("/").text
    assert "Nothing to review yet" in home and 'href="/hunt"' in home


def test_marking_applied_offers_undo_and_undo_puts_it_back(client):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="Acme", role="Engineer", state="Ready")
    r = client.post(f"/jobs/{jid}/state", data={"state": "Applied", "next": "/"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert f"undo={jid}:Ready" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert f'action="/jobs/{jid}/undo"' in page and 'name="state" value="Ready"' in page and ">Undo</button>" in page
    assert db.get_job(jid)["date_applied"]
    r = client.post(f"/jobs/{jid}/undo", data={"state": "Ready", "next": "/"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "msg=Undone" in r.headers["location"]
    job = db.get_job(jid)
    assert job["state"] == "Ready" and not job["date_applied"]


def test_undo_after_a_reject_clears_the_reasons(client):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="Acme", role="Engineer", state="Ready")
    r = client.post(f"/jobs/{jid}/reject", data={"reasons": ["Too senior"], "next": "/"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert f"undo={jid}:Ready" in r.headers["location"]
    client.post(f"/jobs/{jid}/undo", data={"state": "Ready"}, headers=SAME_ORIGIN)
    job = db.get_job(jid)
    assert job["state"] == "Ready" and job["reject_reason"] == "" and "You undid that" in job["history"]


def test_undo_refuses_a_state_it_cant_set(client):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="Acme", role="Engineer", state="Ready")
    for bad in ("Applying", "Whatever"):
        r = client.post(f"/jobs/{jid}/undo", data={"state": bad}, headers=SAME_ORIGIN, follow_redirects=False)
        assert "err=" in r.headers["location"]
    assert db.get_job(jid)["state"] == "Ready"


def test_a_removed_cv_can_be_brought_back(client):
    (paths.cv_dir() / "cv.pdf").write_bytes(b"%PDF-1.4 x")
    r = client.post("/profile/cv/delete", data={"name": "cv.pdf"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert not (paths.cv_dir() / "cv.pdf").exists() and "restore=cv.pdf" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert 'action="/profile/cv/restore"' in page and ">Undo</button>" in page
    assert ">cv.pdf<" not in page  # gone from the list meanwhile
    client.post("/profile/cv/restore", data={"name": "cv.pdf"}, headers=SAME_ORIGIN)
    assert (paths.cv_dir() / "cv.pdf").read_bytes() == b"%PDF-1.4 x"


def test_messages_are_toasts_with_their_tone(client):
    assert 'class="toast ok flash"' in client.get("/?msg=Saved.").text
    assert 'class="toast err flash"' in client.get("/?err=Nope.").text
    assert 'class="toast warn flash"' in client.get("/scope?warn=Saved%2C%20but").text
    r = client.post("/scope", data={"keywords": "engineer", "scope_text": ""}, headers=SAME_ORIGIN, follow_redirects=False)
    assert "warn=" in r.headers["location"]  # saved, but not finished: not red


def test_icons_come_from_one_versioned_sprite(client):
    sprite = (HERE / "static" / "icons.svg").read_text(encoding="utf-8")
    page = client.get("/").text
    used = set(re.findall(r'icons\.svg\?v=[0-9a-f]{10}#([\w-]+)"', page))
    assert used and all(f'<symbol id="{name}"' in sprite for name in used)
    for html in (client.get(u).text for u in ("/applications", "/hunt", "/profile", "/scope", "/accounts", "/settings")):
        assert all(f'<symbol id="{n}"' in sprite for n in re.findall(r'icons\.svg\?v=[0-9a-f]{10}#([\w-]+)"', html))
    assert client.get("/static/icons.svg").status_code == 200


def test_no_browser_confirm_boxes_are_left():
    js = (HERE / "static" / "app.js").read_text(encoding="utf-8")
    assert "showModal" in js and js.count("confirm(") == js.count("window.confirm(")  # only the old-browser fallback


def test_tiles_show_the_company_initials():
    assert initials("Lumen Analytics") == "LA" and initials("ALTAX") == "A" and initials("Acme Pte Ltd") == "A"
    assert initials("") == "?"


def test_the_menu_uses_plain_names_and_marks_the_page(client):
    page = client.get("/scope").text
    nav = page.split('aria-label="Main"', 1)[1].split("</nav>", 1)[0]
    for name in ("To review", "Applications", "Hunt", "Profile", "Preferences", "Connections", "Settings", "What's new"):
        assert name in nav
    assert 'href="/scope" class="on" aria-current=page' in nav
