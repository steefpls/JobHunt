"""Are you logged in to each job site in JobHunt's browser? (Shown on the Accounts page.)

Run by the app as a separate process after the login window closes, or when you
press Check again:  python -m jobhunt.login_check
It opens the browser profile without a window, looks at each site, and writes
data/login-status.json. Nothing is clicked or typed; no password is read.

LinkedIn is judged by its sign-in cookie alone (opening LinkedIn pages invites
its rate limiting). The others open a page only members can see: a site that
sends us to its sign-in page means you are logged out. When you're in, it also
reads which account it is, for the Accounts page.
"""
import json
import re
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
    "jobstreet": {"url": "https://sg.jobstreet.com/my-activity/saved-jobs", "login_hosts": ["login.seek.com"],
                  "account_url": "https://sg.jobstreet.com/profile", "own_domains": ["jobstreet.com", "seek.com"]},
    "indeed": {"url": "https://profile.indeed.com/", "login_hosts": ["secure.indeed.com"],
               "own_domains": ["indeed.com"], "needs_window": True},
}
# needs_window: a browser without a window gets Indeed's robot flag on all of indeed.com, and the flag
# stays in the profile, so later hunts would meet its human check (seen live 2026-10-15). With Indeed
# to check, the browser opens as a minimized window for those few seconds.
# Which account you're logged in as, shown next to "Logged in". LinkedIn: one call to its own "who am I"
# endpoint (no page opened). The others: the email address on your profile page. Not checked while logged
# in (no test account), so anything unexpected just leaves it blank.
LINKEDIN_ME = "https://www.linkedin.com/voyager/api/me"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# a final address with one of these as a path segment means the site wants you to sign in first
LOGIN_PATH_WORDS = ("login", "signin", "sign-in", "authwall", "oauth", "auth", "uas")

STATES = {"in": "Logged in", "out": "Not logged in", "unknown": "Couldn't tell"}


def status_path() -> Path:
    return paths.data_dir() / "login-status.json"


def load_status() -> dict:
    """{site: {"state": "in"|"out"|"unknown", "detail": str, "account": str, "checked_at": iso}} ({} = never checked).

    "account" is who you're logged in as (a name or email), "" when the site didn't say."""
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


def email_in_text(text: str, own_domains=()) -> str:
    """The first email address on a page that isn't the site's own (support@indeed.com and the like)."""
    for m in EMAIL_RE.finditer(text or ""):
        email = m.group(0).rstrip(".")
        domain = email.rsplit("@", 1)[1].lower()
        if not any(domain == d or domain.endswith("." + d) for d in own_domains):
            return email
    return ""


def linkedin_name(data) -> str:
    """Your name from LinkedIn's "who am I" reply: plain ({"miniProfile": ...}) or normalized ({"included": [...]})."""
    if not isinstance(data, dict):
        return ""
    profiles = [data.get("miniProfile")] + [i for i in data.get("included") or [] if isinstance(i, dict)]
    for p in profiles:
        if isinstance(p, dict) and (p.get("firstName") or p.get("lastName")):
            return " ".join(str(p.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
    return ""


def linkedin_account(ctx) -> str:
    # LinkedIn wants its JSESSIONID cookie echoed back as csrf-token on its own API
    token = next((c["value"] for c in ctx.cookies("https://www.linkedin.com")
                  if c.get("name") == "JSESSIONID" and c.get("value")), "")
    if not token:
        return ""
    r = ctx.request.get(LINKEDIN_ME, timeout=15000, max_redirects=0,
                        headers={"csrf-token": token.strip('"'), "accept": "application/json"})
    return linkedin_name(r.json()) if r.ok else ""


def page_account(page, own_domains) -> str:
    return email_in_text(page.inner_text("body", timeout=5000), own_domains)


def check_site(ctx, key: str) -> dict:
    how = CHECKS[key]
    if "cookie" in how:
        state = judge_cookie(ctx.cookies(), *how["cookie"])
        if state != "in":
            return {"state": state, "detail": "no LinkedIn sign-in in JobHunt's browser"}
        try:
            account = linkedin_account(ctx)
        except Exception:
            account = ""
        return {"state": state, "detail": "", "account": account}
    page = ctx.new_page()
    account = ""
    try:
        page.goto(how["url"], wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # busy pages never go idle
        final = page.url
        title = (page.title() or "").lower()
        if not looks_like_login(final, how.get("login_hosts", [])) and "own_domains" in how:
            try:
                if how.get("account_url"):
                    page.goto(how["account_url"], wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                account = page_account(page, how["own_domains"])
            except Exception:
                account = ""  # the login answer above still stands
    except Exception as e:
        return {"state": "unknown", "detail": f"the site didn't load ({type(e).__name__})"}
    finally:
        page.close()
    if any(m in title for m in ("just a moment", "verify you are human", "captcha", "access denied")):
        return {"state": "unknown", "detail": "the site showed a robot check"}
    if looks_like_login(final, how.get("login_hosts", [])):
        return {"state": "out", "detail": "the site asked to sign in"}
    return {"state": "in", "detail": "", "account": account}


def run(sites=None) -> dict:
    from playwright.sync_api import sync_playwright
    from .browser import USER_AGENT, prepare_profile

    sites = [s for s in (sites or CHECKS) if s in CHECKS]
    results = load_status()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    window = any(CHECKS[k].get("needs_window") for k in sites)
    prepare_profile(paths.browser_profile_dir())
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(paths.browser_profile_dir()), headless=not window, user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled", *(["--start-minimized"] if window else [])])
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
