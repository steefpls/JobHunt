"""Replies from job sites: an application a site says the employer turned down moves to "Employer said no".

Indeed and JobStreet each list the jobs you applied for, and mark the ones an employer turned down
("Not selected by employer" on Indeed; JobStreet passes on SEEK's "Not suitable" as "unlikely to
progress"). This opens those lists in JobHunt's browser, with your saved logins, finds each of your
Applied jobs from that site on it, and reads the words on its card. No AI call.

What it will and won't do:
  - It only ever moves an Applied job to Rejected ("Employer said no"). A vague status ("Viewed",
    "Response seems unlikely", "Job closed") changes nothing, and nothing is ever moved to Interview or Offer.
    A job past Applied (Interview, Offer, an offer accepted or turned down) is never looked at.
  - A job you moved yourself after the site moved it (the Undo on the To review page) is never moved again.
  - MyCareersFuture needs Singpass, and LinkedIn's limits make reading it risky: neither is checked.

Neither list was seen logged in while this was built (there was no account to test with), so a card is
found by the job's own link on it where there is one, else by its title and company, and the list is
read by its visible words. When a list loads but none of your jobs can be found on it, a copy of the page
is kept in data/status-pages/ so it can be looked at.
"""
import json
import re
from datetime import datetime, timedelta

from . import db, paths
from .login_check import looks_like_login
from .sites import is_human_check

# site key -> its applied-jobs list. id_re: a job's own id in a link or attribute (group 1); the same pattern
# reads it from the job's URL in the tracker.
SITES = {
    "indeed": {"label": "Indeed", "url": "https://myjobs.indeed.com/applied", "login_hosts": ["secure.indeed.com"],
               "id_re": r"(?:\bjk=|jobKey=|data-jk=\"?)([0-9a-f]{16})\b", "needs_window": True},
    "jobstreet": {"label": "JobStreet", "url": "https://sg.jobstreet.com/my-activity/applied-jobs",
                  "login_hosts": ["login.seek.com"], "id_re": r"/job/(\d{6,})\b"},
}
# needs_window: see login_check.CHECKS: a browser without a window gets Indeed's robot flag.

# Words on a card that mean the employer said no. Indeed: "Not selected by employer" (its help pages, 2026-10-20).
# SEEK/JobStreet: the employer marks you "Not suitable" and you're told it's "unlikely to progress".
REJECTED_PHRASES = ("not selected by employer", "not selected", "unlikely to progress", "not progressing",
                    "not suitable", "unsuccessful", "no longer under consideration", "not moving forward",
                    "rejected by employer", "application rejected", "application was rejected")
# ...and words that sound close but aren't the employer's answer: never a reason to move a job.
NOT_AN_ANSWER = ("response seems unlikely", "no longer accepting", "job closed", "has closed", "expired",
                 "no longer interested")

AUTO_EVERY = timedelta(hours=6)  # a hunt checks first when the last check is older than this


def state_path():
    return paths.data_dir() / "status-check.json"


def pages_dir():
    return paths.data_dir() / "status-pages"


def load_state() -> dict:
    """{"checked_at": iso, "sites": {key: {"state", "detail", "checked", "found", "moved"}}, "news_seen_at": iso}"""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(state_path())


def rejection_in(text: str) -> str:
    """The words on a card that say the employer turned you down ('' when it says no such thing)."""
    low = " ".join((text or "").lower().split())
    for phrase in NOT_AN_ANSWER:
        low = low.replace(phrase, " ")
    return next((p for p in REJECTED_PHRASES if p in low), "")


def site_job_id(site: str, *urls: str) -> str:
    """The site's own id for a job from its links in the tracker ('' when none of them carry one)."""
    rx = re.compile(SITES[site]["id_re"])
    for u in urls:
        m = rx.search(u or "")
        if m:
            return m.group(1)
    return ""


def jobs_to_check(sites=None) -> dict[str, list[dict]]:
    """{site: [Applied jobs found on that site that no site has moved before]}."""
    touched = {r["job_id"] for r in db.query("SELECT DISTINCT job_id FROM job_states WHERE by = 'site'")}
    out = {}
    for key in sites or SITES:
        if key in SITES:
            out[key] = [j for j in db.list_jobs("Applied") if j["source"] == key and j["id"] not in touched]
    return {k: v for k, v in out.items() if v}


def due(now: datetime | None = None) -> bool:
    """Should a hunt check first? Only when there's something to check and the last check is old."""
    if not jobs_to_check():
        return False
    last = load_state().get("checked_at", "")
    try:
        return (now or datetime.now()) - datetime.fromisoformat(last) >= AUTO_EVERY
    except ValueError:
        return True


# Run in the page: for each job, the card it sits on and that card's visible words.
# A card is found from the job's own link (an element with its id in an attribute), else from an element whose
# words are the job's title. From there it climbs to the list item: an element holding the title (and company)
# whose parent holds another element just like it (same tag and class, as every card of a list is), and never
# an element that holds another job's link.
FIND_CARDS_JS = r"""
(args) => {
  const rx = new RegExp(args.idRe);
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const idEls = [];
  for (const el of document.body.querySelectorAll("*")) {
    for (const a of el.attributes) {
      const m = rx.exec(a.value);
      if (m) { idEls.push([el, m[1]]); break; }
    }
  }
  const idsIn = el => { const s = new Set(); for (const [e, k] of idEls) if (el.contains(e)) s.add(k); return s; };
  const text = el => (el.innerText || "");
  const card = (start, job) => {
    const title = norm(job.title), company = norm(job.company);
    const holds = el => { const t = norm(text(el)); return t.includes(title) && (!company || t.includes(company)); };
    let el = start, best = null, cap = 3000;
    while (el && el !== document.body) {
      const parent = el.parentElement;
      if (holds(el) && idsIn(el).size <= 1 && text(el).length <= cap) {
        if (!best) cap = Math.max(400, text(el).length * 3);  // a card, not the page around a lone card
        best = el;
        if (parent && Array.from(parent.children).some(c => c !== el && c.tagName === el.tagName
                                                          && c.className === el.className)) return el;
      } else if (best) {
        return best;
      }
      el = parent;
    }
    return best;
  };
  const out = [];
  for (const job of args.jobs) {
    let start = null;
    if (job.key) { const hit = idEls.find(([e, k]) => k === job.key); if (hit) start = hit[0]; }
    if (!start && job.title) {
      const want = norm(job.title);
      for (const el of document.body.querySelectorAll("a, h1, h2, h3, h4, span, div, p, button")) {
        if (norm(text(el)) === want) { start = el; break; }
      }
    }
    const c = start ? card(start, job) : null;
    out.push({id: job.id, found: !!c, text: c ? text(c).slice(0, 1500) : ""});
  }
  return {jobs: out, links: idEls.length};
}
"""


def _load_everything(page, stop_event=None) -> None:
    """Scroll to the end of the list, pressing Load more / Show more, until nothing new comes in."""
    last = -1
    for _ in range(12):
        if stop_event is not None and stop_event.is_set():
            return
        size = page.evaluate("document.body.innerText.length")
        if size == last:
            return
        last = size
        page.mouse.wheel(0, 20000)
        more = page.locator("button, a[role=button]").filter(has_text=re.compile(r"^\s*(load|show|see) more", re.I))
        try:
            if more.count():
                more.first.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(1500)


def check_site(browser, key: str, jobs: list[dict], log=lambda m: None, stop_event=None) -> tuple[dict, list[dict]]:
    """Read one site's applied list. Returns (how it went, [{"job", "said"}] for each job it says was turned down)."""
    how = SITES[key]
    page = browser.tab(f"status-{key}")
    try:
        page.goto(how["url"], wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # busy pages never go idle
        title, final = page.title() or "", page.url
        if is_human_check(title):
            return {"state": "blocked", "detail": f"{how['label']} showed a robot check"}, []
        if looks_like_login(final, how["login_hosts"]):
            return {"state": "out", "detail": f"not logged in to {how['label']} in JobHunt's browser"}, []
        _load_everything(page, stop_event)
        found = page.evaluate(FIND_CARDS_JS, {"idRe": how["id_re"], "jobs": [
            {"id": j["id"], "key": site_job_id(key, j["url"], j["apply_url"]), "title": j["role"], "company": j["company"]}
            for j in jobs]})
        html = page.content()
    finally:
        try:
            page.close()
        except Exception:
            pass
    by_id = {j["id"]: j for j in jobs}
    seen = [r for r in found["jobs"] if r["found"]]
    said = [{"job": by_id[r["id"]], "said": rejection_in(r["text"])} for r in seen]
    said = [s for s in said if s["said"]]
    result = {"state": "ok", "detail": "", "checked": len(jobs), "found": len(seen), "moved": len(said)}
    if not seen:
        # the list loaded but none of his jobs could be found on it: keep the page, so it can be looked at
        pages_dir().mkdir(parents=True, exist_ok=True)
        (pages_dir() / f"{key}.html").write_text(html, encoding="utf-8")
        result.update(state="unreadable", detail=f"couldn't find your jobs on {how['label']}'s list "
                                                 f"(a copy of the page is in data/status-pages/{key}.html)")
    return result, said


def run(browser_factory, sites=None, log=lambda m: None, stop_event=None) -> dict:
    """Check each site that has Applied jobs to check, and move the ones it says were turned down.
    `browser_factory(headless)` makes the browser (with your logins). Returns the saved state."""
    todo = jobs_to_check(sites)
    state = load_state()
    state.setdefault("sites", {})
    state["checked_at"] = datetime.now().isoformat(timespec="seconds")
    if not todo:
        log("Replies: no Applied jobs from Indeed or JobStreet to check.")
        save_state(state)
        return state
    window = any(SITES[k].get("needs_window") for k in todo)
    browser = browser_factory(not window)
    try:
        for key, jobs in todo.items():
            if stop_event is not None and stop_event.is_set():
                break
            label = SITES[key]["label"]
            log(f"Replies: reading your applied jobs on {label} ({len(jobs)} to check)...")
            try:
                result, said = check_site(browser, key, jobs, log, stop_event)
            except Exception as e:
                result, said = {"state": "error", "detail": f"{label} didn't load ({type(e).__name__})"}, []
            for s in said:
                job = s["job"]
                current = db.get_job(job["id"])
                if not current or current["state"] != "Applied":  # changed while we looked: yours wins
                    continue
                db.update_job(job["id"], by="site", state="Rejected")
                db.add_history(job["id"], f'{label} says the employer said no ("{s["said"]}" on its applied jobs list).')
                log(f"Replies: {label} says {job['company']} said no to {job['role']}.")
            result["checked_at"] = state["checked_at"]
            state["sites"][key] = result
            if result["state"] != "ok":
                log(f"Replies: {result['detail']}.")
            save_state(state)
    finally:
        browser.close()
    return state


def news(since: str | None = None) -> list[dict]:
    """Jobs a site moved since you last dismissed the notice, newest first (each a job with "moved_at")."""
    since = since if since is not None else load_state().get("news_seen_at", "")
    rows = db.query("SELECT job_id, MAX(ts) AS ts FROM job_states WHERE by = 'site' AND state = 'Rejected' AND ts > ?"
                    " GROUP BY job_id ORDER BY ts DESC", (since,))
    out = []
    for r in rows:
        job = db.get_job(r["job_id"])
        if job and job["state"] == "Rejected":  # undone since: no longer news
            out.append(dict(job, moved_at=r["ts"], site=SITES.get(job["source"], {}).get("label", job["source"])))
    return out


def dismiss_news() -> None:
    state = load_state()
    state["news_seen_at"] = db.now()
    save_state(state)


def summary(state: dict) -> str:
    """'Indeed: 1 of 4 said no. JobStreet: not logged in.' for the run's result."""
    parts = []
    for key, r in (state.get("sites") or {}).items():
        if r.get("checked_at") != state.get("checked_at"):
            continue
        label = SITES.get(key, {}).get("label", key)
        if r["state"] == "ok":
            parts.append(f"{label}: found {r['found']} of {r['checked']} applied job(s), "
                         + (f"{r['moved']} turned down." if r["moved"] else "no news."))
        else:
            parts.append(f"{label}: {r['detail']}.")
    return " ".join(parts) or "No Applied jobs from Indeed or JobStreet to check."

