"""Start JobHunt:  python -m jobhunt [--port 8765] [--no-browser]"""
import argparse
import threading
import webbrowser

import uvicorn

from . import db
from .first_run import import_bundled_key
from .web.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="jobhunt")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    app = create_app(args.port)
    note = import_bundled_key()  # opencode_key.txt next to run.bat -> Credential Manager
    if note:
        print("\n  " + note, flush=True)
        db.log_event(None, note, "warn" if "could not" in note.lower() or "left alone" in note else "info")
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  JobHunt is running at {url}\n  Keep this window open while you use it. Close it to stop JobHunt.\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
