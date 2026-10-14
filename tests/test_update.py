"""The launch-time updater: copies the new version in, never touches personal files."""
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update  # noqa: E402

SHA = "a" * 40


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"JobHunt-{SHA}/", "")
        for name, text in files.items():
            z.writestr(f"JobHunt-{SHA}/{name}", text)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def nothing_running(monkeypatch):
    """No JobHunt is running on this PC during these tests (one that is, is covered in test_visibility)."""
    monkeypatch.setattr(update, "port_in_use", lambda port: False)


@pytest.fixture
def install(tmp_path):
    tmp_path = tmp_path / "JobHunt"  # tmp_path itself already holds the test data folder
    (tmp_path / "jobhunt").mkdir(parents=True)
    (tmp_path / "jobhunt" / "app.py").write_text("old", encoding="utf-8")
    (tmp_path / "run.bat").write_text("old run", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi==1", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "profile.md").write_text("me", encoding="utf-8")
    (tmp_path / "opencode_key.txt").write_text("key", encoding="utf-8")
    return tmp_path


def test_new_files_copied_and_personal_files_kept(install):
    update.apply_archive(make_zip({"jobhunt/app.py": "new", "jobhunt/extra.py": "x", "data/profile.md": "evil",
                                   "opencode_key.txt": "evil", "../escape.txt": "evil"}), install)
    assert (install / "jobhunt" / "app.py").read_text(encoding="utf-8") == "new"
    assert (install / "jobhunt" / "extra.py").exists()
    assert (install / "data" / "profile.md").read_text(encoding="utf-8") == "me"
    assert (install / "opencode_key.txt").read_text(encoding="utf-8") == "key"
    assert not (install.parent / "escape.txt").exists()


def test_run_bat_is_left_for_run_bat_to_swap(install):
    update.apply_archive(make_zip({"run.bat": "new run"}), install)
    assert (install / "run.bat").read_text(encoding="utf-8") == "old run"
    assert (install / "run.bat.new").read_text(encoding="utf-8") == "new run"
    (install / "run.bat.new").unlink()
    update.apply_archive(make_zip({"run.bat": "old run"}), install)  # unchanged: nothing to swap
    assert not (install / "run.bat.new").exists()


def test_only_files_an_update_put_there_are_deleted(install):
    (install / "mine.txt").write_text("keep", encoding="utf-8")
    update.apply_archive(make_zip({"jobhunt/app.py": "v1", "jobhunt/gone.py": "x"}), install)
    update.apply_archive(make_zip({"jobhunt/app.py": "v2"}), install)
    assert not (install / "jobhunt" / "gone.py").exists()
    assert (install / "mine.txt").exists() and (install / "data" / "profile.md").exists()


def test_main_updates_once_then_is_up_to_date(install, monkeypatch):
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "latest_commit", lambda: SHA)
    downloads = []
    monkeypatch.setattr(update, "download", lambda sha: downloads.append(sha) or make_zip({"jobhunt/app.py": "new"}))
    monkeypatch.setattr(update, "refresh_packages", lambda d: pytest.fail("requirements didn't change"))
    assert update.main(install) == 0 and update.main(install) == 0
    assert downloads == [SHA]
    assert (install / ".jobhunt-version").read_text(encoding="utf-8") == SHA


def test_changed_requirements_refresh_packages(install, monkeypatch):
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "latest_commit", lambda: SHA)
    monkeypatch.setattr(update, "download", lambda sha: make_zip({"requirements.txt": "fastapi==2"}))
    refreshed = []
    monkeypatch.setattr(update, "refresh_packages", refreshed.append)
    update.main(install)
    assert refreshed == [install]


def test_offline_or_git_checkout_starts_as_is(install, monkeypatch):
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)

    def offline():
        raise OSError("no network")
    monkeypatch.setattr(update, "latest_commit", offline)
    assert update.main(install) == 0
    assert (install / "jobhunt" / "app.py").read_text(encoding="utf-8") == "old"
    (install / ".git").mkdir()
    monkeypatch.setattr(update, "latest_commit", lambda: pytest.fail("a git checkout is never updated"))
    assert update.main(install) == 0


def test_bad_download_leaves_the_install_alone(install, monkeypatch):
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "latest_commit", lambda: SHA)
    monkeypatch.setattr(update, "download", lambda sha: b"not a zip")
    assert update.main(install) == 0
    assert not (install / ".jobhunt-version").exists()
    assert (install / "jobhunt" / "app.py").read_text(encoding="utf-8") == "old"


def test_a_failed_package_refresh_is_tried_again_next_start(install, monkeypatch):
    monkeypatch.delenv("JOBHUNT_NO_UPDATE", raising=False)
    monkeypatch.setattr(update, "latest_commit", lambda: SHA)
    monkeypatch.setattr(update, "download", lambda sha: make_zip({"requirements.txt": "fastapi==2"}))
    calls = []

    def flaky(d):
        calls.append(d)
        if len(calls) == 1:
            raise OSError("offline")
    monkeypatch.setattr(update, "refresh_packages", flaky)
    update.main(install)
    assert (install / update.STALE_FILE).exists()
    update.main(install)  # already on SHA, but the packages are still stale
    assert len(calls) == 2 and not (install / update.STALE_FILE).exists()
    update.main(install)
    assert len(calls) == 2
