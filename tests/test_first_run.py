"""First-run import of a bundled opencode_key.txt into Credential Manager."""
import json

import keyring
import pytest
from fastapi.testclient import TestClient

from jobhunt import db, first_run, secrets_store, settings
from jobhunt.web.app import create_app

from conftest import API_KEY, assert_secret_free, files_under


@pytest.fixture
def key_file(tmp_path):
    folder = tmp_path / "JobHunt"
    folder.mkdir()
    f = folder / first_run.KEY_FILE
    f.write_text("﻿" + API_KEY + "\r\n", encoding="utf-8")  # Notepad-style: BOM and a newline
    return f


def test_import_moves_key_to_keyring_and_deletes_file(key_file, memory_keyring, data_dir, capsys):
    settings.update({"provider": "openrouter", "models": {"filter": "x/y"}})
    note = first_run.import_bundled_key(key_file.parent)
    assert "deleted" in note
    assert not key_file.exists()
    assert memory_keyring.store[("JobHunt", "api_key:opencode_go")] == API_KEY
    s = settings.load()
    assert s["provider"] == "opencode_go"
    assert set(s["models"].values()) == {"deepseek-v4.1-flash"}
    # the key went nowhere else: not the message, console, settings, event log, or any file
    assert_secret_free(note, "message")
    assert_secret_free(capsys.readouterr().out, "console")
    assert_secret_free(json.dumps(db.recent_events(100)), "event log")
    for f in files_under(data_dir):
        assert_secret_free(f.read_bytes(), str(f))
    assert first_run.import_bundled_key(key_file.parent) == ""  # nothing left to do


def test_no_file_means_nothing_happens(tmp_path, memory_keyring):
    assert first_run.import_bundled_key(tmp_path) == ""
    assert memory_keyring.store == {}


def test_malformed_file_is_left_alone(tmp_path, memory_keyring):
    f = tmp_path / first_run.KEY_FILE
    f.write_text("paste your key here", encoding="utf-8")
    assert "left alone" in first_run.import_bundled_key(tmp_path)
    assert f.exists() and memory_keyring.store == {}


def test_file_kept_when_credential_manager_fails(key_file, monkeypatch):
    def broken(*a):
        raise keyring.errors.KeyringError("vault locked")
    monkeypatch.setattr(secrets_store.keyring, "set_password", broken)
    note = first_run.import_bundled_key(key_file.parent)
    assert "was kept" in note and key_file.exists()
    assert_secret_free(note, "message")


def test_settings_page_shows_key_set_but_never_the_key(key_file, data_dir):
    first_run.import_bundled_key(key_file.parent)
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        html = c.get("/settings").text
        assert "OpenCode Go key:" in html and ">set<" in html and "Replace" in html
        assert_secret_free(html, "settings page")
        # Replace from the Settings page goes back to Settings
        r = c.post("/accounts/apikey", data={"provider": "opencode_go", "api_key": "sk-new-key-123456", "next": "/settings"},
                   headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False)
        assert r.headers["location"].startswith("/settings")
    assert secrets_store.get_secret("api_key:opencode_go") == "sk-new-key-123456"
