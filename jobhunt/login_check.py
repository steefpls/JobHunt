"""Are you logged in to each job site in JobHunt's browser? (Shown on the Accounts page.)

Run by the app as a separate process after the login window closes, or when you
press Check again:  python -m jobhunt.login_check
It opens the browser profile without a window, looks at each site, and writes
data/login-status.json. Nothing is clicked or typed; no password is read.

LinkedIn is judged by its sign-in cookie alone (opening LinkedIn pages invites
its rate limiting). The others open a page only members can see: a site that
sends us to its sign-in page means you are logged out.
"""
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from . import paths

# site key -> how to tell. "cookie": (domain, name) of the cookie a logged-in session has.
# "url": a members-only page; "login_hosts": hosts that are a sign-in page wherever they land.
# Checked live while logged out (2026-10-14): JobStreet's saved jobs go to login.seek.com and Indeed's
# profile to secure.indeed.com/auth. (JobStreet's /profile is NOT one: logged out, it shows a sign-up page.)
CHECKS = {
    "linkedin": {"cookie": ("linkedin.com", "li_at")},
    "jobstreet": {"url": "https://sg.jobstreet.com/my-activity/saved-jobs", "login_hosts": ["login.seek.com"]},
    "indeed": {"url": "https://profile.indeed.com/", "login_hosts": ["secure.indeed.com"]},
}
# a final address with one of these as a path segment means the site wants you to sign in first
LOGIN_PATH_WORDS = ("login", "signin", "sign-in", "authwall", "oauth", "auth", "uas")

STATES = {"in": "Logged in", "out": "Not logged in", "unknown": "Couldn't tell"}


def status_path() -> Path:
    return paths.data_dir() / "login-status.json"


def load_status() -> dict:
    """{site: {"state": "in"|"out"|"unknown", "detail": str, "checked_at": iso}} ({} = never checked)."""
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(results: dict) -> None:
    tmp = status_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1), encoding="utf-8")
    tmp.replace(status_path())


def looks_like_login(url: str, login_hosts=()) -> bool:
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in login_hosts):
        return True
    return any(seg.lower() in LOGIN_PATH_WORDS for seg in u.path.split("/"))


def judge_cookie(cookies: list[dict], domain: str, name: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    for c in cookies:
        d = str(c.get("domain") or "").lstrip(".").lower()
        if c.get("name") == name and (d == domain or d.endswith("." + domain)) and c.get("value"):
            expires = c.get("expires", -1)
            if expires in (-1, None) or expires > now:
                return "in"
    return "out"


def check_site(ctx, key: str) -> dict:
    how = CHECKS[key]
    if "cookie" in how:
        state = judge_cookie(ctx.cookies(), *how["cookie"])
        return {"state": state, "detail": "" if state == "in" else "no LinkedIn sign-in in JobHunt's browser"}
    page = ctx.new_page()
    try:
        page.goto(how["url"], wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # busy pages never go idle
        final = page.url
        title = (page.title() or "").lower()
    except Exception as e:
        return {"state": "unknown", "detail": f"the site didn't load ({type(e).__name__})"}
    finally:
        page.close()
    if any(m in title for m in ("just a moment", "verify you are human", "captcha", "access denied")):
        return {"state": "unknown", "detail": "the site showed a robot check"}
    if looks_like_login(final, how.get("login_hosts", [])):
        return {"state": "out", "detail": "the site asked to sign in"}
    return {"state": "in", "detail": ""}


def run(sites=None) -> dict:
    from playwright.sync_api import sync_playwright
    from .browser import USER_AGENT, prepare_profile

    sites = [s for s in (sites or CHECKS) if s in CHECKS]
    results = load_status()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    prepare_profile(paths.browser_profile_dir())
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(paths.browser_profile_dir()), headless=True, user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            for key in sites:
                try:
                    results[key] = check_site(ctx, key)
                except Exception as e:
                    results[key] = {"state": "unknown", "detail": f"check failed ({type(e).__name__})"}
                results[key]["checked_at"] = stamp
                save_status(results)  # after each site, so the page fills in as it goes
        finally:
            ctx.close()
    return results


if __name__ == "__main__":
    import sys
    run(sys.argv[1:] or None)
