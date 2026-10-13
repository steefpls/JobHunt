"""Brings JobHunt up to date from GitHub. run.bat calls this before every start.

Checks the latest version on GitHub, and when it differs from this copy,
downloads it and copies the new app files over this folder. Personal things
are never touched: data\\, .venv\\, .browsers\\ and opencode_key.txt are not in
the download, and only files an earlier update put here are ever deleted.
When requirements.txt changes, the Python packages and Chromium are refreshed.

Offline, GitHub down, or anything unexpected: it says so and JobHunt starts
as it is. Skipped for a git checkout (developers update with git) and when
the environment variable JOBHUNT_NO_UPDATE is set.
Uses only the standard library, so it runs even when the packages are stale.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "steefpls/JobHunt"
BRANCH = "main"
APP_DIR = Path(__file__).resolve().parent
VERSION_FILE = ".jobhunt-version"  # the commit this copy was updated to
MANIFEST_FILE = ".jobhunt-files"  # the files that update put here
# never written or deleted by an update, even if the download had them
PROTECTED = {"data", ".venv", ".browsers", ".git", "opencode_key.txt", VERSION_FILE, MANIFEST_FILE}
# run.bat is running while this runs; cmd reads it as it goes, so it is swapped in by run.bat itself
RUN_BAT = "run.bat"


def latest_commit(timeout: float = 8) -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 headers={"Accept": "application/vnd.github.sha", "User-Agent": "JobHunt-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sha = r.read().decode().strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"unexpected answer from GitHub: {sha[:60]!r}")
    return sha


def download(sha: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(f"https://codeload.github.com/{REPO}/zip/{sha}", headers={"User-Agent": "JobHunt-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def apply_archive(archive: bytes, app_dir: Path) -> list[str]:
    """Copies the archive's files over app_dir; returns the files it wrote (relative, forward slashes)."""
    written = []
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for info in z.infolist():
            parts = info.filename.split("/")[1:]  # drop GitHub's "JobHunt-<sha>/" top folder
            if not parts or info.is_dir() or not parts[-1]:
                continue
            if parts[0] in PROTECTED or any(p in ("", ".", "..") for p in parts) or ":" in info.filename:
                continue
            rel = "/".join(parts)
            target = app_dir.joinpath(*parts)
            if rel == RUN_BAT:
                target = app_dir / (RUN_BAT + ".new")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(info)
            if rel == RUN_BAT and _digest(app_dir / RUN_BAT) == hashlib.sha256(data).hexdigest():
                written.append(rel)
                continue
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            written.append(rel)
    if not written:
        raise ValueError("the download held no files")
    # files an earlier update put here that the new version no longer has
    manifest = app_dir / MANIFEST_FILE
    if manifest.exists():
        for rel in set(manifest.read_text(encoding="utf-8").split("\n")) - set(written):
            parts = rel.split("/")
            if not rel or parts[0] in PROTECTED or ".." in parts:
                continue
            try:
                app_dir.joinpath(*parts).unlink()
            except OSError:
                pass
    manifest.write_text("\n".join(sorted(written)), encoding="utf-8")
    return written


def refresh_packages(app_dir: Path) -> None:
    py = sys.executable
    subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q",
                    "-r", str(app_dir / "requirements.txt")], cwd=str(app_dir), check=True)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(app_dir / ".browsers"))
    subprocess.run([py, "-m", "playwright", "install", "chromium"], cwd=str(app_dir), env=env, check=True)


def main(app_dir: Path = APP_DIR) -> int:
    if os.environ.get("JOBHUNT_NO_UPDATE") or (app_dir / ".git").exists():
        return 0
    print("Checking for JobHunt updates...")
    try:
        sha = latest_commit()
    except Exception as e:
        print(f"  Couldn't reach GitHub ({e.__class__.__name__}). Starting the version you have.")
        return 0
    version = app_dir / VERSION_FILE
    if version.exists() and version.read_text(encoding="utf-8").strip() == sha:
        print("  Up to date.")
        return 0
    try:
        archive = download(sha)
        reqs_before = _digest(app_dir / "requirements.txt")
        apply_archive(archive, app_dir)
        version.write_text(sha, encoding="utf-8")
    except Exception as e:
        print(f"  Update failed ({e}). Starting the version you have.")
        return 0
    print(f"  Updated to {sha[:7]}.")
    if _digest(app_dir / "requirements.txt") != reqs_before:
        print("  Updating packages (this can take a few minutes)...")
        try:
            refresh_packages(app_dir)
        except Exception as e:
            print(f"  Updating packages failed ({e}). If JobHunt won't start, run setup.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
