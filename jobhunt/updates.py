"""What the app shows about updates: its version, "JobHunt was updated: what's new",
"a newer version is out" (with a one-click restart that installs it), and the
Changelog page.

The updating itself is update.py's job (run.bat runs it before every start).
It writes .jobhunt-update.json next to run.bat; this module reads that file,
and while JobHunt runs it checks GitHub every few hours so a version pushed
after you started it doesn't go unnoticed.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import paths

FIRST_CHECK_S = 30 * 60  # run.bat checked at start-up; look again after half an hour...
CHECK_EVERY_S = 4 * 60 * 60  # ...then every 4 hours
CHANGELOG_FRESH_S = 30 * 60  # the Changelog page asks GitHub again after this long
CHANGELOG_PAGES = 5  # at most 500 changes

_updater = None
_checker: threading.Thread | None = None


def updater():
    """update.py (next to run.bat), loaded by path: it is a script, not part of the package."""
    global _updater
    if _updater is None:
        script = Path(__file__).resolve().parent.parent / "update.py"
        spec = importlib.util.spec_from_file_location("jobhunt_update_script", script)
        _updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_updater)
    return _updater


def updates_on() -> bool:
    """Off only when JOBHUNT_NO_UPDATE is set (as in update.py). A git clone updates like any other copy."""
    return not os.environ.get("JOBHUNT_NO_UPDATE")


def current_version() -> str:
    """The commit this copy was last updated to (for a git clone not updated yet, the commit it's at)."""
    try:
        return (paths.APP_DIR / updater().VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return updater().git_head(paths.APP_DIR)


def info() -> dict:
    """Everything the pages show about versions and updates."""
    status = updater().read_status(paths.APP_DIR)
    version = current_version()
    latest = str(status.get("latest") or "")
    updated = status.get("updated") if isinstance(status.get("updated"), dict) else None
    return {
        "on": updates_on(),
        "version": version[:7] or "dev",
        "checked_at": str(status.get("checked_at") or "").replace("T", " ")[:16],
        "check_ok": bool(status.get("check_ok")),
        # a newer version is on GitHub than the one running (restart to get it)
        "available": updates_on() and bool(version) and bool(latest) and latest != version,
        "latest": latest[:7],
        # the update that run.bat installed at this start, until you press Got it
        "news": updated if updated and not updated.get("seen") and updated.get("to") == version else None,
    }


def mark_news_seen() -> None:
    status = updater().read_status(paths.APP_DIR)
    if isinstance(status.get("updated"), dict):
        updater().write_status(paths.APP_DIR, updated={**status["updated"], "seen": True})


def check_now() -> None:
    u = updater()
    try:
        sha = u.latest_commit()
    except Exception:
        u.write_status(paths.APP_DIR, checked_at=u.now(), check_ok=False)
        return
    u.write_status(paths.APP_DIR, checked_at=u.now(), check_ok=True, latest=sha)


def start_checker() -> None:
    """Background thread: look for a newer version now and then while JobHunt runs."""
    global _checker
    if not updates_on() or (_checker is not None and _checker.is_alive()):
        return

    def loop():
        time.sleep(FIRST_CHECK_S)
        while True:
            check_now()
            time.sleep(CHECK_EVERY_S)
    _checker = threading.Thread(target=loop, name="jobhunt-update-check", daemon=True)
    _checker.start()


def restart(port: int) -> None:
    """Opens a new JobHunt window that waits for this one to close, updates, and starts again.
    The caller must then end this process (see web.app)."""
    args = [str(paths.APP_DIR / "run.bat"), "--restart", "--no-browser", "--port", str(port)]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(args, cwd=str(paths.APP_DIR), creationflags=flags, close_fds=True)


def exit_soon(delay: float = 1.0) -> None:
    """Ends JobHunt shortly, once the page has its answer (the new window takes over)."""
    def bye():
        time.sleep(delay)
        sys.stdout.flush()
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()


# ── the Changelog page ─────────────────────────────────────────────────
# Every change ever pushed, from GitHub's list of commits (the same notes as "what's new").
# A copy is kept in data\changelog.json so the page still works offline.

def _changelog_file() -> Path:
    return paths.data_dir() / "changelog.json"


def fetch_history(timeout: float = 6) -> list[dict]:
    """Every commit on main, newest first: {"sha", "at" (ISO, UTC), "note" (its first line)}. Merges left out."""
    u = updater()
    out = []
    for page in range(1, CHANGELOG_PAGES + 1):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{u.REPO}/commits?sha={u.BRANCH}&per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "JobHunt-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            commits = json.loads(r.read().decode())
        if not isinstance(commits, list):
            raise ValueError("unexpected answer from GitHub")
        for c in commits:
            commit = c.get("commit") or {}
            lines = str(commit.get("message") or "").strip().splitlines()
            note = lines[0].strip() if lines else ""
            if note and not note.startswith("Merge ") and len(c.get("parents") or []) <= 1:
                at = (commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date") or ""
                out.append({"sha": str(c.get("sha") or ""), "at": str(at), "note": note})
        if len(commits) < 100:
            break
    return out


def _read_cache() -> dict:
    try:
        data = json.loads(_changelog_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("commits"), list) else {}
    except (OSError, ValueError):
        return {}


def history() -> tuple[list[dict], str, bool]:
    """(commits, when they were fetched, whether GitHub answered this time or recently).
    Asks GitHub at most every half hour; offline, the last copy is used."""
    cache = _read_cache()
    fetched = str(cache.get("fetched_at") or "")
    try:
        fresh = time.time() - time.mktime(time.strptime(fetched, "%Y-%m-%dT%H:%M:%S")) < CHANGELOG_FRESH_S
    except ValueError:
        fresh = False
    if fresh or not updates_on():
        return cache.get("commits") or [], fetched, True
    try:
        commits = fetch_history()
    except Exception:
        return cache.get("commits") or [], fetched, False
    fetched = updater().now()
    tmp = _changelog_file().with_name("changelog.json.tmp")
    try:
        tmp.write_text(json.dumps({"fetched_at": fetched, "commits": commits}, indent=1), encoding="utf-8")
        os.replace(tmp, _changelog_file())
    except OSError:  # only the offline copy is lost
        pass
    return commits, fetched, True


def _local_day(at: str) -> tuple[str, str]:
    """("2026-10-14", "14 October 2026") in this PC's time zone."""
    try:
        d = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "", "Date unknown"
    return d.strftime("%Y-%m-%d"), f"{d.day} {d.strftime('%B %Y')}"


def changelog() -> dict:
    """What the Changelog page shows: the changes grouped by day, newest first, each marked
    as in the version you have, the version you have, or not installed yet."""
    commits, fetched, ok = history()
    version = current_version()
    have = any(c.get("sha") == version for c in commits)
    installed = not have  # a version GitHub doesn't list (a local edit, say): everything counts as installed
    days: list[dict] = []
    for c in commits:
        sha = c.get("sha", "")
        if have and sha == version:
            installed = True
        key, label = _local_day(c.get("at", ""))
        if not days or days[-1]["key"] != key:
            days.append({"key": key, "label": label, "changes": []})
        days[-1]["changes"].append({"sha": sha[:7], "note": c.get("note", ""), "installed": installed,
                                    "current": have and sha == version})
    return {"days": days, "fetched_at": fetched.replace("T", " ")[:16], "ok": ok,
            "coming": sum(1 for d in days for ch in d["changes"] if not ch["installed"]),
            "version": version[:7] or "dev"}
