"""Brings JobHunt up to date from GitHub. run.bat calls this before every start.

Checks the latest version on GitHub, and when it differs from this copy,
downloads it and copies the new app files over this folder. Personal things
are never touched: data\\, .venv\\, .browsers\\ and opencode_key.txt are not in
the download, and only files an earlier update put here are ever deleted.
When requirements.txt changes, the Python packages and Chromium are refreshed.

Offline, GitHub down, or anything unexpected: it says so and JobHunt starts
as it is. A copy cloned with git updates the same way (its .git folder is left
alone). Skipped when the environment variable JOBHUNT_NO_UPDATE is set, and
while JobHunt is already running: its files are never swapped under it (run.bat
then just brings the running one up in the browser).

What happened is written to .jobhunt-update.json, so the app can say "JobHunt
was updated: what's new", show its version and when it last checked, and
offer a restart when a newer version comes out while it runs.
Uses only the standard library, so it runs even when the packages are stale.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "steefpls/JobHunt"
BRANCH = "main"
APP_DIR = Path(__file__).resolve().parent
VERSION_FILE = ".jobhunt-version"  # the commit this copy was updated to
MANIFEST_FILE = ".jobhunt-files"  # the files that update put here
STALE_FILE = ".jobhunt-packages-stale"  # requirements.txt changed and the packages aren't refreshed yet
STATUS_FILE = ".jobhunt-update.json"  # last check, newest version seen, and what the last update brought
# never written or deleted by an update, even if the download had them
PROTECTED = {"data", ".venv", ".browsers", ".git", "opencode_key.txt", VERSION_FILE, MANIFEST_FILE, STALE_FILE,
             STATUS_FILE}
DEFAULT_PORT = 8765
MAX_NOTES = 15  # "what's new" lines kept from one update
# The newest version before updates kept notes: a copy updated past it by an older update.py
# (which wrote no status file) gets its "what's new" counted from here.
NOTES_BASELINE = "ff87949"
# run.bat is running while this runs; cmd reads it as it goes, so it is swapped in by run.bat itself
RUN_BAT = "run.bat"


def _is_sha(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def git_head(app_dir: Path) -> str:
    """The commit a git clone was checked out at ('' if it isn't one). Read from the files: git needn't be installed."""
    git = app_dir / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if _is_sha(head) else ""
        ref = head[5:].strip()
        if (git / ref).is_file():
            sha = (git / ref).read_text(encoding="utf-8").strip()
            return sha if _is_sha(sha) else ""
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _is_sha(sha):
                return sha
    except OSError:
        pass
    return ""


def latest_commit(timeout: float = 8) -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 headers={"Accept": "application/vnd.github.sha", "User-Agent": "JobHunt-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sha = r.read().decode().strip()
    if not _is_sha(sha):
        raise ValueError(f"unexpected answer from GitHub: {sha[:60]!r}")
    return sha


def changes(old: str, new: str, timeout: float = 8) -> list[str]:
    """The first line of every commit between two versions, oldest first (merges left out)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/compare/{old}...{new}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "JobHunt-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        commits = json.loads(r.read().decode()).get("commits") or []
    lines = [str((c.get("commit") or {}).get("message") or "").strip().splitlines()[:1] for c in commits]
    lines = [ln[0].strip() for ln in lines if ln]
    return [ln for ln in lines if ln and not ln.startswith("Merge ")][-MAX_NOTES:]


def read_status(app_dir: Path) -> dict:
    try:
        data = json.loads((app_dir / STATUS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(app_dir: Path, **changes) -> dict:
    """Merges `changes` into the status file (read by the app) and returns the result."""
    status = {**read_status(app_dir), **changes}
    tmp = app_dir / (STATUS_FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
        os.replace(tmp, app_dir / STATUS_FILE)
    except OSError:  # only the notices are lost; JobHunt itself is fine
        pass
    return status


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def port_in_use(port: int) -> bool:
    """Something (normally JobHunt itself) is answering on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, timeout: float = 30) -> bool:
    """For a restart from inside the app: wait until the old JobHunt has closed."""
    deadline = time.monotonic() + timeout
    while port_in_use(port):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def download(sha: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(f"https://codeload.github.com/{REPO}/zip/{sha}", headers={"User-Agent": "JobHunt-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _hash(data: bytes) -> str:
    """sha256 ignoring line endings: git on Windows checks files out with CRLF, GitHub's zip has LF."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _digest(path: Path) -> str:
    return _hash(path.read_bytes()) if path.exists() else ""


def _archive_digest(archive: bytes, rel: str) -> str:
    """sha256 of one file in GitHub's zip ("" if it isn't there)."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for info in z.infolist():
            if "/".join(info.filename.split("/")[1:]) == rel:
                return _hash(z.read(info))
    return ""


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
            if rel == RUN_BAT and _digest(app_dir / RUN_BAT) == _hash(data):
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


def main(app_dir: Path = APP_DIR, argv: list[str] = ()) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--restart", action="store_true")  # started by the app's "Restart to update" button
    args, _ = ap.parse_known_args(list(argv))
    if args.restart:
        print("Waiting for the old JobHunt to close...")
        if not wait_for_port(args.port):
            print("  It is still running, so JobHunt was not updated.")
            return 0
    elif port_in_use(args.port):
        print("JobHunt is already running, so it is not updated now (it updates the next time it starts).")
        return 0
    if os.environ.get("JOBHUNT_NO_UPDATE"):
        return 0
    print("Checking for JobHunt updates...")
    try:
        sha = latest_commit()
    except Exception as e:
        print(f"  Couldn't reach GitHub ({e.__class__.__name__}). Starting the version you have.")
        write_status(app_dir, checked_at=now(), check_ok=False)
        return 0
    write_status(app_dir, checked_at=now(), check_ok=True, latest=sha)
    version = app_dir / VERSION_FILE
    stale = app_dir / STALE_FILE
    # a git clone that was never updated this way is at the commit it was cloned (or last pulled) at
    old = version.read_text(encoding="utf-8").strip() if version.exists() else git_head(app_dir)
    if old == sha:
        print("  Up to date.")
        if old and "updated" not in read_status(app_dir) and not old.startswith(NOTES_BASELINE):
            try:  # an older update.py brought this version in: say what it brought
                write_status(app_dir, updated={"from": NOTES_BASELINE, "to": sha, "at": now(),
                                               "changes": changes(NOTES_BASELINE, sha), "seen": False})
            except Exception:
                pass
    else:
        try:
            archive = download(sha)
            if _archive_digest(archive, "requirements.txt") not in ("", _digest(app_dir / "requirements.txt")):
                # noted before copying, so an update that stops half-way still refreshes the packages next time
                stale.write_text(sha, encoding="utf-8")
            apply_archive(archive, app_dir)
            version.write_text(sha, encoding="utf-8")
        except Exception as e:
            print(f"  Update failed ({e}). Starting the version you have.")
            return 0
        print(f"  Updated to {sha[:7]}.")
        try:
            notes = changes(old, sha) if old else []
        except Exception:  # "what's new" is a nicety: the update itself already worked
            notes = []
        before = read_status(app_dir)
        count = int(before.get("updates") or (1 if before.get("updated") else 0)) + 1  # updates installed so far
        write_status(app_dir, updated={"from": old, "to": sha, "at": now(), "changes": notes, "seen": False},
                     updates=count)
    if stale.exists():  # also retries a package refresh that failed at an earlier start
        print("  Updating packages (this can take a few minutes)...")
        try:
            refresh_packages(app_dir)
        except Exception as e:
            print(f"  Updating packages failed ({e}). JobHunt will try again next time; "
                  "if it won't start, run setup.bat.")
        else:
            stale.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(APP_DIR, sys.argv[1:]))
