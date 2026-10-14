"""What the app shows about updates: its version, "JobHunt was updated: what's new",
and "a newer version is out" (with a one-click restart that installs it).

The updating itself is update.py's job (run.bat runs it before every start).
It writes .jobhunt-update.json next to run.bat; this module reads that file,
and while JobHunt runs it checks GitHub every few hours so a version pushed
after you started it doesn't go unnoticed.
"""
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths

FIRST_CHECK_S = 30 * 60  # run.bat checked at start-up; look again after half an hour...
CHECK_EVERY_S = 4 * 60 * 60  # ...then every 4 hours

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
    """Off for a developer's git checkout and when JOBHUNT_NO_UPDATE is set (as in update.py)."""
    return not os.environ.get("JOBHUNT_NO_UPDATE") and not (paths.APP_DIR / ".git").exists()


def current_version() -> str:
    """The commit this copy was last updated to ('' for a git checkout or a copy never updated)."""
    try:
        return (paths.APP_DIR / updater().VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
