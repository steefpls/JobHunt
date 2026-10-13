"""The web UI: every page renders, local-only checks hold, secrets never come back out."""
import pytest
from fastapi.testclient import TestClient

from jobhunt import db, secrets_store, settings
from jobhunt.web.app import create_app

from conftest import API_KEY, LOGIN_PASSWORD, LOGIN_USER, assert_secret_free

PAGES = ["/", "/profile", "/scope", "/accounts", "/hunt", "/settings", "/demo/search?q=engineer", "/demo/job/1",
         "/demo/ats/1/login"]
SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.mark.parametrize("url", PAGES)
def test_pages_render(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_job_page_and_files(client, data_dir):
    folder = data_dir / "jobs" / "2026-10-13_Lumen_Backend"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(b"%PDF-1.4 x")
    (folder / "match_report.md").write_text("# Match report - Lumen\n## Gaps\n- k8s", encoding="utf-8")
    jid = db.add_job(source="demo", url="http://x/1", company="Lumen", role="Backend", folder=folder.name, state="Ready")
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 200 and "Match report" in r.text and "resume.pdf" in r.text
    assert client.get(f"/files/{jid}/resume.pdf").headers["content-type"] == "application/pdf"
    assert client.get(f"/files/{jid}/..%2F..%2Fjobhunt.db").status_code == 404
    assert client.get(f"/files/{jid}/missing.pdf").status_code == 404


def test_set_state_and_export(client):
    jid = db.add_job(source="demo", url="http://x/1", company="Lumen", role="Backend", state="Applied")
    r = client.post(f"/jobs/{jid}/state", data={"state": "Interview"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and db.get_job(jid)["state"] == "Interview"
    client.post(f"/jobs/{jid}/state", data={"state": "Applying"}, headers=SAME_ORIGIN)
    assert db.get_job(jid)["state"] == "Interview"  # not a hand-settable state
    csv = client.get("/export.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "Lumen" in csv.text
    assert client.get("/?state=Interview").status_code == 200


def test_foreign_host_and_cross_site_posts_are_refused(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/accounts/apikey", data={"provider": "opencode_go", "api_key": "x" * 20},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/accounts/apikey", data={"provider": "opencode_go", "api_key": "x" * 20},
                    headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert not secrets_store.has_secret(secrets_store.api_key_name("opencode_go"))


def test_saving_secrets_goes_to_keyring_and_never_back_to_the_page(client, memory_keyring, data_dir):
    r = client.post("/accounts/apikey", data={"provider": "opencode_go", "api_key": API_KEY}, headers=SAME_ORIGIN)
    assert r.status_code == 200
    r = client.post("/accounts/logins", data={"name": "default", "username": LOGIN_USER, "password": LOGIN_PASSWORD,
                                              "domains": "greenhouse.io, lever.co"}, headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert memory_keyring.store[("JobHunt", "api_key:opencode_go")] == API_KEY
    assert memory_keyring.store[("JobHunt", "login:default:password")] == LOGIN_PASSWORD
    assert settings.load()["logins"] == [{"name": "default", "domains": ["greenhouse.io", "lever.co"]}]
    for url in PAGES + ["/api/status", "/api/estimate?jobs=5&sites=demo"]:
        assert_secret_free(client.get(url).text, url)
    for f in data_dir.rglob("*"):
        if f.is_file():
            assert_secret_free(f.read_bytes(), str(f))
    client.post("/accounts/logins/delete", data={"name": "default"}, headers=SAME_ORIGIN)
    assert ("JobHunt", "login:default:password") not in memory_keyring.store


def test_bad_login_name_refused(client):
    r = client.post("/accounts/logins", data={"name": "../x", "password": "p"}, headers=SAME_ORIGIN)
    assert "letters, digits" in r.text
    assert settings.load()["logins"] == []


def test_settings_and_scope_round_trip(client):
    form = {"provider": "openrouter", "model_filter": "a/b", "model_tailor": "a/c", "model_apply": "a/c",
            "model_profile": "a/c", "per_job_usd": "0.2", "per_run_usd": "3", "per_job_tokens": "1000",
            "per_run_tokens": "9000", "min_delay_s": "5", "max_delay_s": "2", "max_apps_per_hour_per_site": "10",
            "action_delay_s": "1", "max_steps": "20", "max_candidates": "50"}
    client.post("/settings", data=form, headers=SAME_ORIGIN)
    s = settings.load()
    assert s["provider"] == "openrouter" and s["models"]["filter"] == "a/b"
    assert s["pacing"]["max_delay_s"] == 5  # never below the minimum
    client.post("/scope", data={"keywords": "backend engineer\nplatform engineer", "location": "Singapore",
                                "avoid_words": "intern, sales", "scope_text": "# Scope"}, headers=SAME_ORIGIN)
    s = settings.load()
    assert s["scope"]["keywords"] == ["backend engineer", "platform engineer"]
    assert s["scope"]["avoid_words"] == ["intern", "sales"]


def test_profile_upload_only_accepts_documents(client, data_dir):
    r = client.post("/profile/upload", files=[("files", ("cv.pdf", b"%PDF-1.4", "application/pdf")),
                                              ("files", ("evil.exe", b"MZ", "application/octet-stream"))],
                    headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert sorted(p.name for p in (data_dir / "cv").iterdir()) == ["cv.pdf"]


def test_auto_submit_is_off_by_default():
    assert settings.DEFAULTS["hunt"]["auto_submit"] is False
