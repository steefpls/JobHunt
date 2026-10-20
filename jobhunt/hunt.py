"""The hunt: find postings, judge them, build packages, optionally apply.

One job at a time, start to finish (Hyde's main rule): find one match ->
build its package -> (auto-submit mode) apply -> only then look for the next.

Model calls per matched job, each a fresh short request:
  filter  (cheap)  one per posting examined (repeat postings are never re-judged)
  tailor           one per matched job: resume + cover letter + match report as JSON
  company  (cheap)  one per package: the company summary, from the pages read about the company
  apply            only in auto-submit mode or when you press "Approve & apply"

Everything runs on one background worker thread (`runner`), one task at a
time, so the UI stays responsive and Stop works between steps.
"""
import json
import re
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import (adcheck, company, db, marketpay, profile, prompts, refresh, render, secrets_store,
               settings as settings_mod, sites, statuscheck, tuning)
from .applyagent import ApplyAgent
from .browser import BrowserBusy, BrowserSession
from .extract import Posting
from .llm import BudgetExceeded, LLMClient, LLMError, Meter, RateLimited, cost_of
from .sites import SiteBlocked, SiteContext, apply_site, get_site

# Sites where applying needs a government login (Singpass) the app must not touch. Checked against
# apply_site(), so an Indeed job whose Apply button goes to MyCareersFuture counts too.
HUMAN_APPLY_SITES = {"mycareersfuture"}
# The apply agent only starts on a job in one of these states (never on one already Applied or Applying).
APPLY_FROM_STATES = {"Ready", "Needs you"}

# Starting guesses for the estimate; replaced by your own averages once you have history.
DEFAULT_TOKENS = {"filter": (2600, 160), "tailor": (7500, 2600), "apply": (5200, 260), "company": (4800, 550)}
DEFAULT_SECONDS = {"filter": 4.0, "tailor": 30.0, "apply_step": 5.0, "pdf": 3.0, "company": 10.0}
COMPANY_PAGE_LOADS = 2.5  # pages a company summary loads, on average (board page, website, its contact page)
DEFAULT_APPLY_STEPS = 14
DEFAULT_CANDIDATES_PER_MATCH = 3.0


class StopHunt(Exception):
    pass


# ── factories (tests swap these) ──────────────────────────────────────

def make_llm(settings: dict, meter: Meter, recorder, session_prefix: str = "") -> LLMClient:
    key = secrets_store.get_secret(secrets_store.api_key_name(settings["provider"])) or ""
    return LLMClient(settings, key, meter=meter, recorder=recorder, session_prefix=session_prefix)


def make_browser(headless: bool = True) -> BrowserSession:
    return BrowserSession(headless=headless, minimized=not headless)


# ── the task context ──────────────────────────────────────────────────

class Task:
    """Shared plumbing for one background task: log, spend meter, stop flag."""

    def __init__(self, kind: str, params: dict, stop_event: threading.Event):
        self.kind = kind
        self.params = params
        self.stop_event = stop_event
        self.settings = settings_mod.load()
        self.redact = secrets_store.Redactor.from_store(self.settings, profile.load_contact())
        self.meter = Meter.from_settings(self.settings)
        self.run_id = db.start_run(kind, json.dumps(params))
        self.current_job = None
        self.llm = None
        self.listing_calls: dict = {}  # "listing:<url>" -> llm_calls row ids

    def log(self, message: str, level: str = "info", job_id=None) -> None:
        db.log_event(self.run_id, self.redact(message), level, job_id if job_id is not None else self.current_job)

    def record(self, stage: str, job_id, resp) -> None:
        call_id = db.record_llm_call(run_id=self.run_id, job_id=job_id if isinstance(job_id, int) else None,
                                     stage=stage, model=resp.model, tokens_in=resp.tokens_in,
                                     tokens_out=resp.tokens_out, cost=resp.cost, seconds=resp.seconds)
        if isinstance(job_id, str):  # a listing not yet in the tracker: remember the call for build_package
            self.listing_calls.setdefault(job_id, []).append(call_id)

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopHunt()

    def get_llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = make_llm(self.settings, self.meter, self.record, f"run{self.run_id}")
        return self.llm


# ── building one package ──────────────────────────────────────────────

def judge(task: Task, posting: Posting, scope_text: str, profile_text: str, expected_salary: str = "",
          dislikes: str = "") -> dict:
    system, user = prompts.filter_prompt(profile_text, scope_text, posting, expected_salary, dislikes)
    data, _ = task.get_llm().chat_json(stage="filter", system=system, user=user, max_tokens=400,
                                       job_id=f"listing:{posting.url}")
    decision = str(data.get("decision", "")).lower()
    return {"apply": decision == "apply", "score": data.get("score"), "reasons": data.get("reasons") or [],
            "doubts": data.get("doubts") or ""}


def build_package(task: Task, browser: BrowserSession, posting: Posting, verdict: dict,
                  ctx: SiteContext | None = None) -> int:
    """Folder + tracker row + posting.pdf + tailored resume/cover letter + match report, then the company
    summary (which can't fail the package: see company.py)."""
    t0 = time.monotonic()
    contact = profile.load_contact()
    profile_text = profile.load_profile()
    folder = render.make_job_folder(posting.company, posting.title)
    doubts = f"Doubts: {verdict['doubts']}" if verdict.get("doubts") else ""
    job_id = db.add_job(source=posting.source, url=posting.url, company=posting.company, role=posting.title,
                        location=posting.location, apply_url=posting.apply_url or posting.url,
                        posting_text=posting.text, folder=folder.name, history=doubts, salary=posting.salary,
                        match_score=_num(verdict.get("score")), run_id=task.run_id, posted_at=posting.posted)
    task.current_job = job_id
    listing_key = f"listing:{posting.url}"
    for call_id in task.listing_calls.pop(listing_key, []):
        db.assign_call_to_job(call_id, job_id)  # the filter call counts towards this job too
    task.meter.move_job(listing_key, job_id)
    task.log(f"Building package for {posting.company} - {posting.title}")
    try:
        browser.render_pdf(render.posting_html(posting), folder / "posting.pdf")
        system, user = prompts.tailor_prompt(profile_text, contact, posting)
        for attempt in (1, 2):  # an unreadable or incomplete reply gets one more try, then the job is an Error
            try:
                data, _ = task.get_llm().chat_json(stage="tailor", system=system, user=user, max_tokens=6000,
                                                   job_id=job_id)
            except RateLimited:
                raise
            except LLMError as e:
                if attempt == 2:
                    raise
                task.log(f"The AI's package could not be read ({e}); asking again.", "warn")
                continue
            data = render.normalise_tailor(data)
            problems = render.package_problems(data)
            if not problems:
                break
            task.log(f"The AI's package was incomplete ({'; '.join(problems)})"
                     + ("; asking again." if attempt == 1 else "."), "warn")
        if problems:
            db.update_job(job_id, state="Error")
            db.add_history(job_id, f"The AI returned an incomplete package twice ({'; '.join(problems)}), "
                                   "so nothing was built to send.")
            return job_id
        resume = data["resume"]
        warnings = render.honesty_warnings(profile_text, resume)
        r_html = render.resume_html(contact, resume)
        c_html = render.cover_letter_html(contact, posting.company, posting.title, data.get("cover_letter") or {})
        (folder / "resume.html").write_text(r_html, encoding="utf-8")
        (folder / "cover_letter.html").write_text(c_html, encoding="utf-8")
        browser.render_pdf(r_html, folder / "resume.pdf")
        browser.render_pdf(c_html, folder / "cover_letter.pdf")
        (folder / "match_report.md").write_text(
            render.match_report_md(posting.company, posting.title, data.get("match_report") or {}, warnings,
                                   verdict.get("reasons")), encoding="utf-8")
        for w in warnings:
            task.log(w, "warn")
        note = "Package ready, waiting for your approval." if not warnings else "Package ready - see warnings in the match report."
        db.update_job(job_id, state="Ready")
        db.add_history(job_id, note)
        company_summary(task, ctx or SiteContext(browser, task.settings["pacing"], task.stop_event, task.log),
                        job_id, posting.company_info)
        pay = marketpay.check(job_id)
        if pay and pay.get("status") == "ok":
            task.log(f"Market pay for {posting.title}: {pay['text']}, from {pay['ads']} similar ads.", job_id=job_id)
        db.record_timing(task.run_id, job_id, "draft", time.monotonic() - t0)
        task.log(f"Package ready: {folder.name}")
    except BudgetExceeded as e:
        db.update_job(job_id, state="Error")
        db.add_history(job_id, f"Stopped while building: {e}")
        raise
    except RateLimited:
        db.update_job(job_id, state="Error")
        db.add_history(job_id, "Stopped while building: the AI provider's rate limit was reached.")
        raise
    except LLMError as e:
        db.update_job(job_id, state="Error")
        db.add_history(job_id, f"Model error while tailoring: {e}")
        task.log(f"Model error while tailoring: {e}", "error")
    except Exception as e:  # a PDF or file problem with this job shouldn't end the whole hunt
        db.update_job(job_id, state="Error")
        db.add_history(job_id, f"Building the package failed ({type(e).__name__}).")
        task.log(f"Building the package failed: {type(e).__name__}: {task.redact(str(e))[:200]}", "error")
    return job_id


def company_summary(task: Task, ctx: SiteContext, job_id: int, info: dict | None = None) -> dict | None:
    """Look up the job's company and save the summary on it. Never raises."""
    try:
        return company.summarise(task, ctx, job_id, info, practice_base=task.settings.get("demo_board_url") or "")
    except Exception as e:  # belt and braces: company.summarise already catches its own problems
        task.log(f"Couldn't make the company summary ({type(e).__name__}).", "warn", job_id=job_id)
        return None


def apply_job(task: Task, browser: BrowserSession, job_id: int) -> str:
    """Run the apply agent on one prepared job. Returns the new state."""
    job = db.get_job(job_id)
    if not job:
        raise LLMError(f"There is no job {job_id}.")
    if job["state"] not in APPLY_FROM_STATES:  # e.g. a second click or a stale tab: never apply twice
        task.log(f"Not applying to {job['company']} - {job['role']}: it is {job['state']}.", "warn")
        return job["state"]
    task.current_job = job_id
    if apply_site(job["source"], job["apply_url"]) in HUMAN_APPLY_SITES:
        db.needs_you(job_id, "singpass", "This site needs Singpass to apply, so you apply yourself.")
        task.log("This site needs Singpass to apply, so it is handed to you (no tokens spent).")
        return "Needs you"
    t0 = time.monotonic()
    folder = _folder(job)
    db.update_job(job_id, state="Applying")
    task.log(f"Applying: {job['company']} - {job['role']}")
    # The per-job caps count everything this job ever used (filter, tailor, earlier
    # apply attempts), not just this run, so pressing Approve & apply again can't reset them.
    task.meter.seed_job(job_id, job["cost_usd"] or 0.0, (job["tokens_in"] or 0) + (job["tokens_out"] or 0))
    hunt_mode = (browser.headless, browser.minimized)
    needs_window = job["source"] in settings_mod.SITES and getattr(get_site(job["source"], task.settings), "needs_window", False)
    browser.set_headless(not (task.settings["apply"].get("show_browser", True) or needs_window))
    date_letter_today(task, browser, folder, job_id)
    agent = ApplyAgent(browser=browser, llm=task.get_llm(), settings=task.settings, contact=profile.load_contact(),
                       profile_text=profile.load_profile(), screening=profile.load_screening(), job=job,
                       folder=folder, redactor=task.redact,
                       stop_event=task.stop_event, log=lambda m: task.log(m, job_id=job_id))
    try:
        result = agent.run(job["apply_url"] or job["url"])
    except Exception as e:  # browser trouble etc. — hand it to the person, never guess
        if browser.is_closed_error(e):  # you closed the window: it may or may not have been sent
            browser.reset()  # the next job (or the rest of the hunt) gets a fresh browser
            db.needs_you(job_id, "interrupted", "The browser window was closed before the application finished. "
                                                "Check the site to see whether it was sent.")
            task.log("The browser window was closed before the application finished.", "warn", job_id=job_id)
        else:
            db.needs_you(job_id, "broke", f"The apply attempt broke ({type(e).__name__}) before it finished.")
            task.log(f"Apply attempt broke: {type(e).__name__}: {str(e)[:200]}", "error")
        return "Needs you"
    finally:
        browser.set_headless(*hunt_mode)  # never raises, even with the window already closed
    state = {"submitted": "Applied", "failed": "Error", "stopped": "Ready"}.get(result.status, "Needs you")
    if state == "Needs you":
        db.needs_you(job_id, result.kind or needs_you_kind(result.summary), result.summary)
        db.add_history(job_id, f"Stopped at {result.url}")
    else:
        db.update_job(job_id, state=state)
        db.add_history(job_id, f"Applied: {result.summary}" if state == "Applied" else f"{result.summary} (at {result.url})")
    db.record_timing(task.run_id, job_id, "apply", time.monotonic() - t0)
    task.log(f"Apply result: {state} - {result.summary}", "info" if state == "Applied" else "warn")
    return state


def date_letter_today(task: Task, browser: BrowserSession, folder, job_id: int) -> None:
    """A cover letter made on an earlier day still says that day: re-make it dated today before the agent can
    attach it. Only the date line changes (no AI); if that fails, the letter goes as it is."""
    html_path, pdf = folder / "cover_letter.html", folder / "cover_letter.pdf"
    tmp = folder / "cover_letter.new.pdf"
    try:
        if not html_path.is_file() or not pdf.is_file():
            return
        html = html_path.read_text(encoding="utf-8")
        new = refresh.redate(html, refresh.today())
        if new == html:
            return
        browser.render_pdf(new, tmp)
        tmp.replace(pdf)  # the old PDF stays whole until the new one is
        html_path.write_text(new, encoding="utf-8")
        task.log(f"Dated the cover letter today ({refresh.today().strftime(render.LETTER_DATE)}); it said "
                 f"{refresh.letter_date(html).strftime(render.LETTER_DATE)}.", job_id=job_id)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        task.log(f"Couldn't date the cover letter today ({type(e).__name__}), so it goes with the date it has.",
                 "warn", job_id=job_id)


# ── why a job needs you, and what to do about it ─────────────────────

def needs_you_kind(summary: str) -> str:
    """Sort the apply agent's own words into a kind of stop."""
    s = (summary or "").lower()
    if re.search(r"captcha|robot|verification code|one[- ]time|\botp\b|2fa|two[- ]factor|verify (that )?you|human check", s):
        return "verify"
    if re.search(r"log ?in|sign ?in|password|create an account|an account|register", s):
        return "login"
    return "other"


def needs_you_fix(job: dict) -> dict:
    """For the job page: {"fix": (label, link) or None, "retry": may JobHunt try again, "advice": what else to do}."""
    kind = job.get("reason_kind") or ""
    yourself = ("Or finish it yourself: open the apply page, attach the resume and cover letter from Files below, "
                "then press I applied.")
    out = {"fix": None, "retry": True, "advice": yourself}
    if kind.startswith("screening:"):
        topic = kind.split(":", 1)[1]
        label = profile.SCREENING_FIELDS.get(topic, ("that answer",))[0].split(" (")[0].lower()
        out["fix"] = (f"Add your {label} on the Profile page", f"/profile#s_{topic}")
    elif kind == "login":
        out["fix"] = ("Save a login for this site on Accounts", "/accounts#logins")
    elif kind == "budget":
        out["fix"] = ("Raise the per-job cap in Settings", "/settings")
    elif kind == "singpass":
        out.update(retry=False, advice="Open the apply page, log in with Singpass, attach the resume and cover letter "
                                       "from Files below, then press I applied.")
    elif kind == "verify" and sites.INDEED_CHECK_STEPS in (job.get("reason") or ""):
        # the apply agent stopped on Indeed's own human check: passing it once in the Accounts login window
        # lets the next try through (a captcha or code on an employer's site is for you, below)
        out["fix"] = ("Pass Indeed's human check on Accounts", "/accounts#sitelogins")
    elif kind == "verify":
        out.update(retry=False, advice="It needs a human check (a captcha or a code). Open the apply page and finish "
                                       "it yourself, then press I applied.")
    elif kind in ("interrupted", "unconfirmed"):
        out.update(retry=False, advice="Open the apply page and check whether it went through. If it did, press the "
                                       "I applied button; if not, finish it there yourself.")
    if apply_site(job.get("source") or "", job.get("apply_url") or "") in HUMAN_APPLY_SITES:
        out["retry"] = False
    return out


def _folder(job):
    from . import paths
    return paths.jobs_dir() / job["folder"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── the hunt loop ─────────────────────────────────────────────────────

def _below_floor(top_monthly: float, floor: float) -> str:
    """Why a stated salary rules a job out ('' = it doesn't, or no salary was stated)."""
    if floor and top_monthly and top_monthly < floor:
        return f"salary tops out at SGD {top_monthly:,.0f}/month, below your SGD {floor:,.0f}"
    return ""


def _avoid_hit(words: list[str], *texts: str) -> str | None:
    hay = " ".join(t or "" for t in texts).lower()
    for w in words:
        w = w.strip().lower()
        if w and re.search(r"\b" + re.escape(w) + r"\b", hay):
            return w
    return None


class SiteStats:
    """What a hunt did with each site's results, per site and per keyword (shown on the Hunt page when it
    ends), so a site or keyword that finds nothing new is easy to spot."""
    COUNTS = ("found", "known", "avoid", "low_pay", "not_fit", "unreadable", "other", "matched")
    NAMES = {"known": "already known", "avoid": "avoid word", "low_pay": "pay too low", "not_fit": "not a fit",
             "unreadable": "couldn't be read", "other": "same company or over the hunt's limit", "matched": "match"}

    def __init__(self):
        self.sites: dict = {}

    def site(self, site) -> dict:
        return self.sites.setdefault(site.key, {"label": site.label, "pages": 0, "stopped_early": 0, "keywords": {},
                                                **{c: 0 for c in self.COUNTS}})

    def add(self, site, listing, what: str) -> None:
        """Count one listing as `what` (one of COUNTS: "found" once for every listing, then what became of it)."""
        row = self.site(site)
        row[what] += 1
        kw = row["keywords"].setdefault(listing.extra.get("keyword") or "", {"found": 0, "new": 0, "matched": 0})
        if what == "found":
            kw["found"] += 1
        elif what != "known":
            kw["new"] += 1
            kw["matched"] += what == "matched"

    def lines(self) -> list[str]:
        """'JobStreet: 40 found: 25 already known, 3 pay too low, 9 not a fit, 2 matches.'"""
        out = []
        for row in self.sites.values():
            parts = [f"{row[k]} {name}{'es' if k == 'matched' and row[k] != 1 else ''}"
                     for k, name in self.NAMES.items() if row[k]]
            line = f"{row['label']}: {row['found']} found" + (": " + ", ".join(parts) if parts else "") + "."
            if row["stopped_early"]:
                line += (f" {row['stopped_early']} keyword{'s' if row['stopped_early'] != 1 else ''} stopped early, on a"
                         " page holding only jobs already known.")
            out.append(line)
        return out


def _known(listing, seen_here: set) -> bool:
    """Already judged or tracked (the hunt loop's own test, nothing opened), or listed earlier this run."""
    return (listing.url in seen_here or db.was_seen(listing.url)
            or db.is_duplicate(listing.url, listing.company, listing.title, listing.salary))


def candidates(task: Task, ctx: SiteContext, site_keys: list[str], keywords: list[str], location: str,
               max_pages: int = 5, blocked: dict | None = None, stats: SiteStats | None = None):
    """Round-robin over sites, then keywords, page by page; yields (site, listing).

    `blocked` maps a site's key to why it was dropped for this run. A site that
    lands in it (a posting page hit a wall) is dropped at once, so its search
    pages stop loading too."""
    blocked = blocked if blocked is not None else {}
    gens = []
    for key in site_keys:
        site = get_site(key, task.settings)
        gens.append((site, _site_listings(task, ctx, site, keywords, location, max_pages, stats)))
    while gens:
        for site, gen in list(gens):
            if site.key in blocked:
                gens.remove((site, gen))
                continue
            try:
                yield site, next(gen)
            except StopIteration:
                gens.remove((site, gen))
            except SiteBlocked as e:
                task.log(f"{site.label}: blocked or needs login ({e}); skipping this site for this run.", "warn")
                blocked[site.key] = f"{site.label} (blocked or needs login)"
                gens.remove((site, gen))
            except StopHunt:
                raise
            except Exception as e:  # one site's outage shouldn't end the hunt on the others
                task.log(f"{site.label}: search failed ({type(e).__name__}: {str(e)[:120]}); "
                         "skipping this site for this run.", "warn")
                blocked[site.key] = f"{site.label} (search failed)"
                gens.remove((site, gen))


def _site_listings(task, ctx, site, keywords, location, max_pages, stats: SiteStats | None = None):
    """One failed search page skips to the next keyword. Two failures in a row, or no search page
    working at all, give up on the site (the error goes up to `candidates`).

    On a site that lists the newest jobs first, a page where every job is already known means the
    pages after it are older still: that keyword stops there and the next one starts."""
    failures, worked, last_error = 0, False, None
    seen_here: set = set()
    for kw in keywords:
        for page_no in range(max_pages):
            task.check_stop()
            if page_no >= getattr(site, "page_limit", max_pages):
                break  # the site shows us no more pages (Indeed when logged out)
            try:
                listings = site.search(ctx, kw, location, page_no)
            except (SiteBlocked, StopHunt):
                raise
            except Exception as e:
                failures, last_error = failures + 1, e
                if failures >= 2:
                    raise
                task.log(f"{site.label}: '{kw}' page {page_no + 1} failed ({type(e).__name__}: {str(e)[:120]}); "
                         "trying the next keyword.", "warn")
                break
            failures, worked = 0, True
            if stats is not None:
                stats.site(site)["pages"] += 1
            all_known = bool(listings) and getattr(site, "newest_first", False) and all(_known(l, seen_here) for l in listings)
            more = page_no + 1 < min(max_pages, getattr(site, "page_limit", max_pages))
            if all_known and more:
                task.log(f"{site.label}: '{kw}' page {page_no + 1}: all {len(listings)} already known, so the older "
                         "pages are skipped; moving on.")
                if stats is not None:
                    stats.site(site)["stopped_early"] += 1
            else:
                task.log(f"{site.label}: '{kw}' page {page_no + 1}: {len(listings)} results")
            if not listings:
                break
            for listing in listings:
                listing.extra.setdefault("keyword", kw)
                seen_here.add(listing.url)
            yield from listings
            if all_known:
                break
    if last_error is not None and not worked:
        raise last_error


def wait_for_rate_limit(task: Task, source: str) -> None:
    cap = int(task.settings["pacing"].get("max_apps_per_hour_per_site") or 0)
    if cap <= 0:
        return
    announced = False
    while True:
        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        if db.applied_since(source, since) < cap:
            return
        if not announced:  # say until when, so a long wait doesn't look like a hang
            first = db.first_applied_since(source, since)
            try:
                until = (datetime.fromisoformat(first) + timedelta(hours=1)).strftime("%H:%M")
            except ValueError:
                until = ""
            task.log(f"Pacing: {cap} applications in the last hour on {source}; waiting "
                     + (f"until about {until} " if until else "") + "before the next one (Stop ends the hunt).")
            announced = True
        for _ in range(60):
            task.check_stop()
            time.sleep(1)


def readiness(s: dict | None = None) -> list[tuple[str, str]]:
    """What still stops a hunt from running, as (what to do, the page to do it on). [] = ready.

    The Hunt page greys out Start and lists these; the Dashboard's Get started ticks come from it too."""
    s = s or settings_mod.load()
    missing = []
    if not secrets_store.has_secret(secrets_store.api_key_name(s["provider"])):
        missing.append(("Add your AI key", "/accounts"))
    contact = profile.load_contact()
    if not (contact.get("name") and contact.get("email")):
        missing.append(("Add your name and email", "/profile#contact"))
    if not profile.load_profile().strip():
        missing.append(("Build or write your profile", "/profile#profiletext"))
    if not [k for k in s["scope"].get("keywords", []) if k.strip()]:
        missing.append(("Add at least one search keyword", "/scope"))
    if not profile.scope_ready(profile.load_scope_text()):
        missing.append(("Describe the jobs you want", "/scope#describe"))
    return missing


# Where a first real hunt looks: boards JobHunt can apply on (MyCareersFuture needs Singpass, so
# every job from there comes back to you).
FIRST_REAL_SITES = ["jobstreet", "linkedin"]


def suggested_sites(s: dict) -> list[str]:
    """The sites ticked on the Hunt page: yours once you've picked a real one; before that the
    Practice board, and after a practice hunt JobStreet and LinkedIn."""
    if s["hunt"].get("sites_chosen"):
        return s["hunt"]["sites"]
    return FIRST_REAL_SITES if db.practice_hunt_done() else ["demo"]


def readiness_text(missing: list[tuple[str, str]]) -> str:
    """'Before a hunt can start: add your AI key; describe the jobs you want.'"""
    return "Before a hunt can start: " + "; ".join(what[0].lower() + what[1:] for what, _ in missing) + "."


def stop_reason(matched: int, target: int, examined: int, max_candidates: int) -> str:
    """Why the hunt ended, so fewer packages than asked for never looks like a crash."""
    if matched >= target:
        return f"Stopped at the {target} you asked for."
    if examined >= max_candidates:
        return (f"Stopped after reading {max_candidates} new postings, the most allowed in Settings "
                "(Most postings to read per hunt). Raise it to read further.")
    return "Stopped because the searches ran out of new postings."


def run_hunt(task: Task) -> str:
    p = task.params
    s = task.settings
    target = int(p.get("jobs") or s["hunt"]["jobs"])
    auto_submit = bool(p.get("auto_submit"))
    site_keys = [k for k in (p.get("sites") or s["hunt"]["sites"]) if k in settings_mod.SITES]
    keywords = [k for k in s["scope"].get("keywords", []) if k.strip()]
    missing = readiness(s)
    if missing:  # the Hunt page greys out Start until this is empty; this is the backstop
        raise LLMError(readiness_text(missing))
    scope_text = profile.load_scope_text()
    task.get_llm()
    expected_salary = profile.load_screening()["expected_salary"]
    floor = profile.salary_floor()
    profile_text = profile.load_profile()
    avoid = s["scope"].get("avoid_words", [])
    dislikes = tuning.dislikes_text()  # why you turned recent packages down
    task.log(f"Hunt started: up to {target} jobs from {', '.join(site_keys)}; "
             f"{'AUTO-SUBMIT' if auto_submit else 'draft mode (nothing is submitted)'}; caps: {caps_text(s)}.")
    if s["hunt"].get("check_replies", True) and statuscheck.due():
        # first, what Indeed and JobStreet say about jobs he applied for (its own browser, closed before the hunt's)
        try:
            statuscheck.run(make_browser, log=task.log, stop_event=task.stop_event)
        except BrowserBusy:
            raise
        except Exception as e:  # never worth losing the hunt over
            task.log(f"Replies: the check broke ({type(e).__name__}); carrying on with the hunt.", "warn")
        task.check_stop()
    # Some sites (Indeed) show their human check to a browser without a window: with one of those
    # ticked, the hunt's browser is a real window, started minimized.
    window = any(getattr(get_site(k, s), "needs_window", False) for k in site_keys)
    browser = make_browser(headless=not window)
    ctx = SiteContext(browser, s["pacing"], task.stop_event, lambda m: task.log(m))
    matched, ready, examined, skipped_known = 0, 0, 0, 0
    companies, tried, blocked = set(), set(), {}
    stats = SiteStats()
    max_candidates = int(s["hunt"].get("max_candidates") or 60)
    try:
        try:  # first, whether the ads waiting for you are still open (JobStreet and Indeed need this browser)
            adcheck.run(ctx, log=task.log, stop_event=task.stop_event)
        except Exception as e:  # never worth losing the hunt over
            task.log(f"Ads: the check broke ({type(e).__name__}); carrying on with the hunt.", "warn")
        task.check_stop()
        for site, listing in candidates(task, ctx, site_keys, keywords, s["scope"].get("location", ""), blocked=blocked,
                                        stats=stats):
            task.check_stop()
            task.current_job = None
            if matched >= target or examined >= max_candidates:
                break
            stats.add(site, listing, "found")
            if site.key in blocked or listing.url in tried:
                stats.add(site, listing, "known")
                continue  # tried: the same posting can come up again under another keyword
            tried.add(listing.url)
            if db.was_seen(listing.url) or db.is_duplicate(listing.url, listing.company, listing.title, listing.salary):
                skipped_known += 1
                stats.add(site, listing, "known")
                continue
            hit = _avoid_hit(avoid, listing.title, listing.company)
            if hit:
                db.mark_seen(listing.url, listing.source, listing.company, listing.title, "skip", f"avoid word: {hit}", salary=listing.salary)
                task.log(f"Skip (avoid word '{hit}'): {listing.company} - {listing.title}")
                stats.add(site, listing, "avoid")
                continue
            if listing.company and db.company_key(listing.company) in companies:
                stats.add(site, listing, "other")
                continue  # one application per company per run
            reason = _below_floor(listing.salary_max_monthly, floor)
            if reason:  # the search card already shows the pay: no need to open the posting
                db.mark_seen(listing.url, listing.source, listing.company, listing.title, "skip", reason, salary=listing.salary)
                task.log(f"Skip ({reason}): {listing.company} - {listing.title}")
                stats.add(site, listing, "low_pay")
                continue
            examined += 1
            try:
                posting = site.fetch(ctx, listing)
            except SiteBlocked as e:
                blocked[site.key] = f"{site.label} (blocked or needs login)"
                task.log(f"{site.label}: blocked or needs login ({e}); skipping this site for this run.", "warn")
                stats.add(site, listing, "unreadable")
                continue
            except Exception as e:
                db.mark_seen(listing.url, listing.source, listing.company, listing.title, "error", str(e)[:200], salary=listing.salary)
                task.log(f"Could not read posting {listing.url}: {type(e).__name__}", "warn")
                stats.add(site, listing, "unreadable")
                continue
            dup = db.duplicate_of(listing.url, posting.company, posting.title, posting.salary, posting.apply_url)
            if dup:  # the posting's own company and title, and its pay, can say more than the search card did
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "skip",
                             f"same job as one already seen on {dup['source']}", salary=posting.salary,
                         apply_url=posting.apply_url)
                task.log(f"Skip (same job already seen on {dup['source']}): {posting.company} - {posting.title}")
                stats.add(site, listing, "known")
                continue
            if len(posting.text) < 80:
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "error", "posting text too short", salary=posting.salary,
                         apply_url=posting.apply_url)
                task.log(f"Posting had no readable text, skipped: {listing.url}", "warn")
                stats.add(site, listing, "unreadable")
                continue
            reason = _below_floor(posting.salary_max_monthly, floor)
            if reason:
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "skip", reason, salary=posting.salary,
                         apply_url=posting.apply_url)
                task.log(f"Skip ({reason}): {posting.company} - {posting.title}")
                stats.add(site, listing, "low_pay")
                continue
            try:
                verdict = judge(task, posting, scope_text, profile_text, expected_salary, dislikes)
            except RateLimited:
                raise
            except LLMError as e:  # one unreadable answer costs one posting, not the whole run
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "error", f"filter: {e}"[:200], salary=posting.salary,
                         apply_url=posting.apply_url)
                task.log(f"Could not judge {posting.company} - {posting.title}: {e}", "warn")
                stats.add(site, listing, "unreadable")
                continue
            if not verdict["apply"]:
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "skip",
                             "; ".join(map(str, verdict["reasons"])), salary=posting.salary,
                         apply_url=posting.apply_url)
                task.log(f"Not a fit: {posting.company} - {posting.title} ({'; '.join(map(str, verdict['reasons']))[:160]})")
                stats.add(site, listing, "not_fit")
                continue
            db.mark_seen(listing.url, listing.source, posting.company, posting.title, "match",
                         "; ".join(map(str, verdict["reasons"])), salary=posting.salary,
                         apply_url=posting.apply_url)
            task.log(f"Match ({verdict.get('score')}/10): {posting.company} - {posting.title}")
            stats.add(site, listing, "matched")
            job_id = build_package(task, browser, posting, verdict, ctx)
            companies.add(db.company_key(posting.company))
            matched += 1
            if db.get_job(job_id)["state"] == "Ready":
                ready += 1
                if auto_submit:
                    if apply_site(posting.source, posting.apply_url) not in HUMAN_APPLY_SITES:
                        wait_for_rate_limit(task, posting.source)  # a job handed to you doesn't wait
                    apply_job(task, browser, job_id)
        task.current_job = None
        summary = f"{ready} package(s) built from {examined} posting(s) examined ({skipped_known} already known)."
        if matched > ready:
            summary += f" {matched - ready} match(es) failed while building: see Error on the Dashboard."
        summary += " " + stop_reason(matched, target, examined, max_candidates)
        if blocked:
            summary += " Skipped for this run: " + ", ".join(blocked.values()) + "."
        for line in stats.lines():
            task.log(line)
        task.log("Hunt finished: " + summary)
        return summary
    finally:
        if stats.sites:  # kept for the Hunt page, also when the hunt was stopped
            db.set_site_stats(task.run_id, json.dumps(stats.sites))
        browser.close()


def run_apply(task: Task) -> str:
    job_id = int(task.params["job_id"])
    task.get_llm()
    browser = make_browser(headless=True)
    try:
        state = apply_job(task, browser, job_id)
    finally:
        browser.close()
    return f"Job {job_id}: {state}"


def run_profile(task: Task) -> str:
    texts = profile.all_cv_texts()
    if not texts:
        raise LLMError("Upload at least one CV first.")
    task.log(f"Reading {len(texts)} document(s) to build your profile...")
    system, user = prompts.profile_prompt(texts)
    data, _ = task.get_llm().chat_json(stage="profile", system=system, user=user, max_tokens=8000)
    md = str(data.get("profile_markdown") or "").strip()
    if not md:
        raise LLMError("The model returned an empty profile.")
    old = profile.load_profile()
    if old.strip():
        (profile.paths.data_dir() / "profile.previous.md").write_text(old, encoding="utf-8")
    profile.save_profile(md)
    contact = profile.load_contact()
    for k, v in (data.get("contact") or {}).items():
        if k in profile.CONTACT_FIELDS and v and not contact.get(k):
            contact[k] = v
    profile.save_contact(contact)
    answers = profile.load_screening()  # fill only empty ones; never overwrite what you typed
    found = data.get("screening") if isinstance(data.get("screening"), dict) else {}
    for k in profile.SCREENING_FIELDS:
        if not answers[k] and str(found.get(k) or "").strip():
            answers[k] = str(found[k]).strip()
    profile.save_screening(answers)
    gaps = data.get("gaps") or []
    for g in gaps:
        task.log(f"Profile gap: {g}", "warn")
    return (f"Profile built from {len(texts)} document(s). Review it on the Profile page, "
            "including the screening answers.")


def run_scope(task: Task) -> str:
    """Draft the Scope from the profile. Keywords and avoid words you already set are kept."""
    from . import paths
    profile_text = profile.load_profile()
    if not profile_text.strip():
        raise LLMError("Build your Profile first: the scope is drafted from it.")
    template = profile.scope_guide_text()
    task.log("Drafting your scope from your profile...")
    system, user = prompts.scope_prompt(profile_text, template, profile.load_screening())
    data, _ = task.get_llm().chat_json(stage="profile", system=system, user=user, max_tokens=3000)
    md = str(data.get("scope_markdown") or "").strip()
    if not md or profile.scope_is_example(md):
        raise LLMError("The model returned an empty scope; try again.")
    old = profile.load_scope_text()
    if old.strip() and not profile.scope_is_example(old):
        (paths.data_dir() / "scope.previous.md").write_text(old, encoding="utf-8")
    profile.save_scope_text(md)
    scope = task.settings["scope"]
    suggested = {k: [str(w).strip() for w in data.get(k) or [] if str(w).strip()][:10] for k in ("keywords", "avoid_words")}
    for key, label in (("keywords", "search keywords"), ("avoid_words", "avoid words")):
        if not suggested[key]:
            continue
        if scope.get(key):
            task.log(f"Suggested {label} (yours were kept): {', '.join(suggested[key])}")
        else:
            settings_mod.update({"scope": {key: suggested[key]}})
    return "Scope drafted from your profile. Review it on the Scope page before you hunt."


def run_rebuild_pdfs(task: Task) -> str:
    """Re-make resume/cover letter PDFs from their (hand-edited) HTML, with the facts from your Profile
    as it is now: name, contact details, employers, titles and dates (see refresh.py), and the cover letter
    dated today. No AI call."""
    from .browser import render_pdfs
    job = db.get_job(int(task.params["job_id"]))
    folder = _folder(job)
    docs, changes, unmatched = refresh.refresh_package(folder, job["company"], job["role"], profile.load_contact(),
                                                       profile.load_profile())
    pairs, redated = [], None
    for name, html, new in docs:
        if (name == "cover_letter" and job["state"] not in refresh.SENT_STATES  # a sent letter keeps its date
                and (dated := refresh.redate(new, refresh.today())) != new):
            new, redated = dated, refresh.letter_date(dated)
        if new != html:
            (folder / f"{name}.html").write_text(new, encoding="utf-8")  # the HTML stays the source you can edit
        pairs.append((new, folder / f"{name}.pdf"))
    render_pdfs(pairs)
    if changes:
        db.add_history(job["id"], "Rebuilt the PDFs with your Profile: " + "; ".join(changes))
    msg = f"Rebuilt {len(pairs)} PDF(s). " + ("Updated from your Profile: " + "; ".join(changes) + "."
                                             if changes else "They already matched your Profile.")
    if redated:
        msg += f" The cover letter is now dated {redated.strftime(render.LETTER_DATE)}."
    if unmatched:
        msg += (f" Couldn't find {', '.join(dict.fromkeys(unmatched))} in your Profile's Experience or Education, "
                "so check those by hand (edit resume.html, then Rebuild).")
    return msg


def run_company(task: Task) -> str:
    """The job page's "Look up this company" / "Refresh": the company summary again, from fresh pages."""
    job_id = int(task.params["job_id"])
    job = db.get_job(job_id)
    if not job:
        raise LLMError(f"There is no job {job_id}.")
    task.current_job = job_id
    # the per-job caps count everything this job ever used, as for Approve & apply
    task.meter.seed_job(job_id, job["cost_usd"] or 0.0, (job["tokens_in"] or 0) + (job["tokens_out"] or 0))
    task.get_llm()  # no AI key: say so before loading any page
    task.log(f"Looking up {job['company']}...", job_id=job_id)
    browser = make_browser(headless=True)  # never an Indeed page: see company.py
    ctx = SiteContext(browser, task.settings["pacing"], task.stop_event, lambda m: task.log(m, job_id=job_id))
    try:
        result = company_summary(task, ctx, job_id)
    finally:
        browser.close()
    task.check_stop()
    if result is None:
        raise LLMError("The company summary couldn't be made.")
    if result.get("refresh_error"):
        raise LLMError(f"Couldn't refresh the company summary ({result['refresh_error']}). The one from "
                       f"{result.get('made_at', 'before')} is still shown.")
    if result["status"] != "ok":
        raise LLMError(f"Couldn't look up {job['company']}: {result.get('error', 'unknown problem')}")
    found = sum(1 for v in result["fields"].values() if v)
    return (f"Company summary updated: {found} of {len(company.FIELDS)} facts found in {len(result['sources'])} "
            "page(s). Anything the pages didn't say shows as Not found.")


def caps_text(s: dict) -> str:
    c = s["caps"]
    parts = []
    if s["provider"] not in settings_mod.SUBSCRIPTION_PROVIDERS:
        parts.append(f"${c['per_job_usd']:.2f}/job, ${c['per_run_usd']:.2f}/run")
    if c.get("per_job_tokens"):
        parts.append(f"{int(c['per_job_tokens']):,} tokens/job")
    if c.get("per_run_tokens"):
        parts.append(f"{int(c['per_run_tokens']):,} tokens/run")
    return ", ".join(parts) or "none"


def run_status_check(task: Task) -> str:
    """Applications' "Check for replies": what Indeed and JobStreet say about jobs he applied for. No AI call."""
    state = statuscheck.run(make_browser, log=task.log, stop_event=task.stop_event)
    task.check_stop()
    return statuscheck.summary(state)


TASKS = {"hunt": run_hunt, "apply": run_apply, "profile": run_profile, "scope": run_scope,
         "rebuild": run_rebuild_pdfs, "company": run_company, "replies": run_status_check}


# ── the background runner ─────────────────────────────────────────────

class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self.thread = None
        self.task = None
        self.stop_event = threading.Event()
        self.last_result = ""
        self.last_kind = ""  # what finished last ("profile", "hunt"...) and how ("done", "error"...)
        self.last_status = ""

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, kind: str, params: dict | None = None) -> int:
        with self._lock:
            if self.busy:
                raise RuntimeError("Something is already running. Stop it or wait for it to finish.")
            self.stop_event = threading.Event()
            self.task = Task(kind, params or {}, self.stop_event)
            self.thread = threading.Thread(target=self._run, args=(self.task,), name=f"jobhunt-{kind}", daemon=True)
            self.thread.start()
            return self.task.run_id

    def _run(self, task: Task) -> None:
        status, summary = "done", ""
        try:
            summary = TASKS[task.kind](task)
        except StopHunt:
            status, summary = "stopped", "Stopped by you."
            task.log("Stopped by you.", "warn")
        except BudgetExceeded as e:
            status, summary = "capped", f"Stopped cleanly: {e}."
            task.log(summary + " Raise the cap in Settings to continue.", "warn")
        except (LLMError, BrowserBusy) as e:
            status, summary = "error", str(e)
            task.log(str(e), "error")
        except Exception as e:  # keep the app alive and say what happened
            status, summary = "error", f"{type(e).__name__}: {e}"
            task.log("Unexpected error: " + task.redact(traceback.format_exc()[-1500:]), "error")
        finally:
            if task.current_job and task.kind != "company":  # a lookup never changes a job's state
                job = db.get_job(task.current_job)
                if job and job["state"] in ("Found", "Applying"):
                    # an interrupted apply may already have submitted: a person has to look, not a re-run
                    if job["state"] == "Found":
                        db.update_job(task.current_job, state="Error")
                        db.add_history(task.current_job, f"Interrupted: {summary}")
                    else:
                        db.needs_you(task.current_job, "interrupted", f"Applying was interrupted ({summary}): check on "
                                                                      "the site whether it was submitted.")
            db.finish_run(task.run_id, status, summary)
            self.last_result, self.last_kind, self.last_status = summary, task.kind, status

    def stop(self) -> None:
        self.stop_event.set()

    def wait(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout)

    def status(self) -> dict:
        t = self.task
        meter = t.meter.snapshot() if t else None
        return {"busy": self.busy, "kind": t.kind if t else None, "run_id": t.run_id if t else None,
                "current_job": t.current_job if t else None, "job_id": t.params.get("job_id") if t else None,
                "meter": meter, "last_result": self.last_result,
                "last_kind": self.last_kind, "last_status": self.last_status}


runner = Runner()


# ── pre-run estimate ──────────────────────────────────────────────────

def estimate(settings: dict, jobs: int, sites: list[str], auto_submit: bool) -> dict:
    hist = db.stage_averages()

    def tokens(stage):
        h = hist.get(stage)
        return (h["tin"], h["tout"]) if h and h["n"] >= 3 else DEFAULT_TOKENS[stage]

    models = settings["models"]
    seen = db.seen_stats()
    ratio = DEFAULT_CANDIDATES_PER_MATCH
    if seen.get("match", 0) >= 3:
        ratio = max(1.0, (seen.get("match", 0) + seen.get("skip", 0)) / seen["match"])
    f_in, f_out = tokens("filter")
    t_in, t_out = tokens("tailor")
    a_in, a_out = tokens("apply")
    c_in, c_out = tokens("company")
    subscription = settings["provider"] in settings_mod.SUBSCRIPTION_PROVIDERS
    apply_sites = [x for x in sites if x not in HUMAN_APPLY_SITES]
    apply_share = (len(apply_sites) / len(sites)) if sites else 0
    apply_steps = DEFAULT_APPLY_STEPS * apply_share if auto_submit else 0.0
    per_job_tokens = (f_in + f_out) * ratio + (t_in + t_out) + (c_in + c_out) + (a_in + a_out) * apply_steps
    # on a flat plan this is the pay-per-use equivalent (nothing is billed per call)
    per_job = (cost_of(models["filter"], f_in, f_out, settings) * ratio
               + cost_of(models["tailor"], t_in, t_out, settings)
               + cost_of(models.get("company") or models["filter"], c_in, c_out, settings)
               + cost_of(models["apply"], a_in, a_out, settings) * apply_steps)
    caps = settings["caps"]

    pacing = settings["pacing"]
    page_wait = (float(pacing["min_delay_s"]) + float(pacing["max_delay_s"])) / 2
    cand_seconds = page_wait * 1.1 + DEFAULT_SECONDS["filter"]  # posting load (+ a share of search pages) + judge
    draft_seconds = db.average_timing("draft") or (DEFAULT_SECONDS["tailor"] + DEFAULT_SECONDS["pdf"]
                                                   + DEFAULT_SECONDS["company"] + COMPANY_PAGE_LOADS * page_wait)
    apply_seconds = db.average_timing("apply") or DEFAULT_APPLY_STEPS * (DEFAULT_SECONDS["apply_step"] + float(pacing["action_delay_s"]))
    per_draft = ratio * cand_seconds + draft_seconds
    drafts_per_hour = 3600 / per_draft
    applies_per_hour = None
    if auto_submit and apply_sites:
        cap = int(pacing.get("max_apps_per_hour_per_site") or 0)
        raw = 3600 / (per_draft + apply_seconds * apply_share)
        applies_per_hour = min(raw, cap * len(apply_sites)) if cap else raw
    rate = applies_per_hour or drafts_per_hour
    return {
        "subscription": subscription, "calls_per_job": ratio + 2 + apply_steps,
        "per_job_usd": per_job, "total_usd": per_job * jobs, "candidates_per_match": ratio,
        "per_job_tokens": per_job_tokens, "total_tokens": per_job_tokens * jobs,
        "per_job_cap": caps["per_job_usd"], "run_cap": caps["per_run_usd"],
        "per_job_token_cap": caps.get("per_job_tokens", 0), "run_token_cap": caps.get("per_run_tokens", 0),
        "over_job_cap": (not subscription and per_job > caps["per_job_usd"])
                        or bool(caps.get("per_job_tokens") and per_job_tokens > caps["per_job_tokens"]),
        "over_run_cap": (not subscription and per_job * jobs > caps["per_run_usd"])
                        or bool(caps.get("per_run_tokens") and per_job_tokens * jobs > caps["per_run_tokens"]),
        "drafts_per_hour": drafts_per_hour, "applies_per_hour": applies_per_hour,
        "hours": jobs / rate if rate else None,
        "from_history": any(hist.get(st, {}).get("n", 0) >= 3 for st in ("filter", "tailor")),
    }
