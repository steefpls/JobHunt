"""The Changelog page: every change from GitHub, grouped by day, with a copy kept for offline."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from jobhunt import updates
from jobhunt.web.app import create_app

REAL_FETCH = updates.fetch_history
ANSWER = {}  # what the fake GitHub answers: a list of commits, or an error to raise
A, B, C = "a" * 40, "b" * 40, "c" * 40
COMMITS = [  # newest first, as GitHub lists them
    {"sha": C, "at": "2026-10-18T09:00:00Z", "note": "Newest change <b>not</b> bold"},
    {"sha": B, "at": "2026-10-17T10:00:00Z", "note": "Middle change"},
    {"sha": A, "at": "2026-10-17T08:00:00Z", "note": "First change"},
]


@pytest.fixture(autouse=True)
def github(monkeypatch):
    """Never the real GitHub: each test says what it answers."""
    calls = []

    def fake(timeout=6):
        calls.append(1)
        if isinstance(ANSWER["now"], Exception):
            raise ANSWER["now"]
        return ANSWER["now"]
    ANSWER["now"] = COMMITS
    monkeypatch.setattr(updates, "fetch_history", fake)
    monkeypatch.setattr(updates, "current_version", lambda: B)
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    return calls


def test_grouped_by_day_with_your_version_and_whats_not_installed():
    log = updates.changelog()
    assert log["ok"] and log["version"] == B[:7] and log["coming"] == 1
    changes = [ch for d in log["days"] for ch in d["changes"]]
    assert [ch["note"] for ch in changes] == ["Newest change <b>not</b> bold", "Middle change", "First change"]
    assert [ch["installed"] for ch in changes] == [False, True, True]
    assert [ch["current"] for ch in changes] == [False, True, False]
    assert sum(len(d["changes"]) for d in log["days"]) == 3 and len(log["days"]) >= 2


def test_a_version_github_doesnt_list_counts_everything_as_installed(monkeypatch):
    monkeypatch.setattr(updates, "current_version", lambda: "d" * 40)
    log = updates.changelog()
    assert log["coming"] == 0 and not any(ch["current"] for d in log["days"] for ch in d["changes"])


def test_github_is_asked_at_most_every_half_hour(github):
    updates.changelog()
    updates.changelog()
    assert len(github) == 1


def test_offline_shows_the_last_copy(github, data_dir):
    updates.changelog()
    cache = json.loads((data_dir / "changelog.json").read_text(encoding="utf-8"))
    cache["fetched_at"] = "2026-01-01T00:00:00"  # long ago: it asks again
    (data_dir / "changelog.json").write_text(json.dumps(cache), encoding="utf-8")
    ANSWER["now"] = OSError("offline")
    log = updates.changelog()
    assert not log["ok"] and log["fetched_at"] == "2026-01-01 00:00"
    assert [ch["note"] for d in log["days"] for ch in d["changes"]][1] == "Middle change"


def test_offline_with_no_copy_says_so():
    ANSWER["now"] = OSError("offline")
    log = updates.changelog()
    assert not log["ok"] and log["days"] == []


def test_reading_githubs_answer_leaves_out_merges(monkeypatch):
    answer = [
        {"sha": C, "parents": [{}, {}], "commit": {"message": "Merge branch x", "committer": {"date": "2026-10-18T00:00:00Z"}}},
        {"sha": B, "parents": [{}], "commit": {"message": "A change\n\nmore detail", "committer": {"date": "2026-10-17T00:00:00Z"}}},
    ]
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps(answer).encode())
    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    assert REAL_FETCH() == [{"sha": B, "at": "2026-10-17T00:00:00Z", "note": "A change"}]
    assert len(seen) == 1 and "steefpls/JobHunt/commits?sha=main" in seen[0]


def test_page_renders_in_the_menu_and_escapes_notes(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as client:
        r = client.get("/changelog")
        assert r.status_code == 200
        assert 'href="/changelog"' in client.get("/").text
    assert "Middle change" in r.text and "your version" in r.text and "not installed yet" in r.text
    assert "&lt;b&gt;not&lt;/b&gt;" in r.text and "<b>not</b>" not in r.text
    assert "1 newer change is out" in r.text


def test_page_offline_with_nothing_yet(data_dir):
    ANSWER["now"] = OSError("offline")
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as client:
        r = client.get("/changelog")
    assert r.status_code == 200 and "Couldn't reach GitHub" in r.text and "No list of changes yet" in r.text
