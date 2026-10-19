"""Is the ad still open? Checked for the jobs waiting for you, so a package whose ad has closed says so
before you spend time on it. No AI and no tokens.

  mycareersfuture  its public API says whether the job is Open (and when it expires); checked live 2026-10-20
  linkedin         the public job page: gone (404), or "No longer accepting applications"; checked live 2026-10-20
  jobstreet        the job page says "isExpired":true / "status":"Expired" in its data (checked live 2026-10-20).
                   Plain HTTP is turned away there, so this needs the hunt's browser: checked as a hunt starts
  indeed           not checked: logged out, its job pages go to the sign-in page and set off its robot check
An Indeed job that applies on MyCareersFuture is checked on MyCareersFuture.

Nothing here moves a job: a closed ad is shown, and you decide.
"""
import json
import re
from datetime import date, datetime, timedelta

import httpx

from . import db, sites

WAITING = ("Ready", "Needs you")
QUICK = {"mycareersfuture", "linkedin"}  # checked with one plain request, from the To review and job pages
BROWSER = {"jobstreet"}  # checked with the hunt's browser as a hunt starts
LABELS = {"mycareersfuture": "MyCareersFuture", "linkedin": "LinkedIn", "jobstreet": "JobStreet", "indeed": "Indeed"}
KEEP = timedelta(hours=6)  # an answer is good for this long
UA = sites.UA
MCF_ID_RE = re.compile(r"([0-9a-f]{32})(?:[/?#]|$)")
LINKEDIN_ID_RE = re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{6,})")
LINKEDIN_CLOSED_RE = re.compile(r"no longer accepting applications", re.I)
JOBSTREET_EXPIRED_RE = re.compile(r'"isExpired"\s*:\s*true|"status"\s*:\s*"Expired"|"jobStatus"\s*:\s*"expired"')


def load(job: dict) -> dict | None:
    """The last answer: {"state": "open"|"closed"|"unknown", "site", "checked_at", "why"}; None = never checked."""
    try:
        data = json.loads(job.get("ad_status") or "")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def closed(job: dict) -> dict | None:
    """The answer when the ad has closed, for the To review cards and the job page."""
    st = load(job)
    return st if st and st["state"] == "closed" else None


def where(job: dict) -> str:
    """The site that says whether this job's ad is open ('' = none JobHunt can ask)."""
    if sites.is_mcf_url(job.get("apply_url") or "") or sites.is_mcf_url(job.get("url") or ""):
        return "mycareersfuture"
    return job.get("source") if job.get("source") in QUICK | BROWSER else ""


def due(job: dict, now: datetime | None = None) -> bool:
    st = load(job)
    if not st:
        return True
    try:
        return (now or datetime.now()) - datetime.fromisoformat(st["checked_at"]) >= KEEP
    except (KeyError, TypeError, ValueError):
        return True


def save(job_id: int, site: str, state: str, why: str = "") -> dict:
    result = {"state": state, "site": LABELS.get(site, site), "why": why,
              "checked_at": datetime.now().isoformat(timespec="seconds")}
    db.set_ad_status(job_id, json.dumps(result))
    return result


def http_get(url: str, timeout: float = 15) -> httpx.Response:
    """One plain request. The tests swap this."""
    with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json, text/html"}, timeout=timeout,
                      follow_redirects=True) as c:
        return c.get(url)


# ── reading each site's answer ────────────────────────────────────────

def mcf_state(status_code: int, data: dict | None, today: date | None = None) -> tuple[str, str]:
    """MyCareersFuture's /jobs/<id> answer -> (state, why)."""
    if status_code in (404, 410):
        return "closed", "MyCareersFuture no longer lists it"
    if status_code != 200 or not isinstance(data, dict):
        return "unknown", f"MyCareersFuture didn't answer (HTTP {status_code})"
    status = str(((data.get("status") or {}).get("jobStatus")) or "")
    if status and status.lower() != "open":
        return "closed", f"MyCareersFuture marks it {status}"
    expiry = str((data.get("metadata") or {}).get("expiryDate") or "")[:10]
    try:
        if expiry and date.fromisoformat(expiry) < (today or date.today()):
            return "closed", f"its ad expired on {expiry}"
    except ValueError:
        pass
    return "open", ""


def linkedin_state(status_code: int, html: str) -> tuple[str, str]:
    if status_code in (404, 410):
        return "closed", "LinkedIn no longer has it"
    if status_code != 200:
        return "unknown", f"LinkedIn didn't answer (HTTP {status_code})"
    if LINKEDIN_CLOSED_RE.search(html or ""):
        return "closed", "LinkedIn says it's no longer accepting applications"
    return "open", ""


def jobstreet_state(html: str) -> tuple[str, str]:
    if JOBSTREET_EXPIRED_RE.search(html or ""):
        return "closed", "JobStreet says the ad has expired"
    if "data-automation=\"job-detail-title\"" in (html or "") or "jobAdDetails" in (html or ""):
        return "open", ""
    return "unknown", "JobStreet's page didn't say"


# ── checking ──────────────────────────────────────────────────────────

def check_quick(job: dict) -> dict | None:
    """Check a MyCareersFuture or LinkedIn job with one plain request; None when its site can't be asked that way."""
    site = where(job)
    try:
        if site == "mycareersfuture":
            m = MCF_ID_RE.search(job.get("apply_url") or "") or MCF_ID_RE.search(job.get("url") or "")
            if not m:
                return save(job["id"], site, "unknown", "no MyCareersFuture job id in its link")
            r = http_get(f"{sites.MyCareersFutureSite.API}/jobs/{m.group(1)}")
            try:
                data = r.json() if r.status_code == 200 else None
            except ValueError:
                data = None
            return save(job["id"], site, *mcf_state(r.status_code, data))
        if site == "linkedin":
            m = LINKEDIN_ID_RE.search(job.get("url") or "")
            if not m:
                return save(job["id"], site, "unknown", "no LinkedIn job id in its link")
            r = http_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
            return save(job["id"], site, *linkedin_state(r.status_code, r.text))
    except httpx.HTTPError as e:
        return save(job["id"], site, "unknown", f"couldn't reach {LABELS[site]} ({type(e).__name__})")
    return None


def check_due_quick(limit: int = 8, job_ids: list[int] | None = None) -> dict:
    """The jobs waiting for you (or just `job_ids`) whose ad can be checked with a plain request and wasn't
    checked lately: {job_id: answer}. At most `limit` requests, so a page that asks stays quick."""
    out = {}
    for job in waiting(job_ids):
        if len(out) >= limit:
            break
        if where(job) in QUICK and due(job):
            out[job["id"]] = check_quick(job)
    return out


def waiting(job_ids: list[int] | None = None) -> list[dict]:
    jobs = [j for state in WAITING for j in db.list_jobs(state)]
    return [j for j in jobs if job_ids is None or j["id"] in job_ids]


def run(ctx, log=lambda m: None, stop_event=None) -> int:
    """As a hunt starts: every job waiting for you whose ad wasn't checked lately, JobStreet's with the hunt's
    browser (paced like any page). Logs the ads that have closed; returns how many were checked."""
    checked, skip = 0, set()
    for job in waiting():
        if stop_event is not None and stop_event.is_set():
            break
        site = where(job)
        if not site or site in skip or not due(job):
            continue
        if site in QUICK:
            result = check_quick(job)
        else:
            try:
                result = save(job["id"], site, *jobstreet_state(ctx.get_html(job["url"])))
            except sites.SiteBlocked as e:
                log(f"Ads: {LABELS[site]} turned the check away ({e}); its ads are checked next hunt.")
                skip.add(site)
                continue
            except Exception as e:  # one page that won't load: that job is asked again next hunt
                save(job["id"], site, "unknown", f"its page didn't load ({type(e).__name__})")
                continue
        checked += 1
        if result and result["state"] == "closed":
            log(f"Ads: {job['company']} - {job['role']} has closed ({result['why']}).")
    return checked
