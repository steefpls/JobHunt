"""Start JobHunt:  python -m jobhunt [--port 8765] [--no-browser]"""
import argparse
import json
import socket
import threading
import urllib.request
import webbrowser

import uvicorn

from . import db, updates
from .first_run import import_bundled_key
from .web.app import create_app


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def jobhunt_running(port: int) -> bool:
    """Is the program on this port JobHunt (and not something else that took the port)?"""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this PC only, never via a proxy
    try:
        with direct.open(f"http://127.0.0.1:{port}/api/version", timeout=3) as r:
            return json.loads(r.read().decode()).get("app") == "JobHunt"
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(prog="jobhunt")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--restart", action="store_true", help=argparse.SUPPRESS)  # for update.py (Restart to update)
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    if not port_free(args.port):  # before create_app: its start-up tidy-up would upset the JobHunt already running
        if jobhunt_running(args.port):  # run.bat double-clicked again (the tab was closed, say): just show it
            webbrowser.open(url)
            print(f"\n  JobHunt is already running, so it was opened in your browser ({url}).\n"
                  "  You can close this window.", flush=True)
            return
        raise SystemExit(f"\n  Port {args.port} is in use by another program, so JobHunt can't start.")
    app = create_app(args.port)
    note = import_bundled_key()  # opencode_key.txt next to run.bat -> Credential Manager
    if note:
        print("\n  " + note, flush=True)
        db.log_event(None, note, "warn" if "could not" in note.lower() or "left alone" in note else "info")
    updates.start_checker()  # notices a newer version that comes out while JobHunt runs
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  JobHunt is running at {url}\n  Keep this window open while you use it. Close it to stop JobHunt.\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
