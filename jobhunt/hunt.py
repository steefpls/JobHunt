"""The hunt: find postings, judge them, build packages, optionally apply.

One job at a time, start to finish (Hyde's main rule): find one match ->
build its package -> (auto-submit mode) apply -> only then look for the next.

Model calls per matched job, each a fresh short request:
  filter  (cheap)  one per posting examined (repeat postings are never re-judged)
  tailor           one per matched job: resume + cover letter + match report as JSON
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

from . import db, profile, prompts, render, secrets_store, settings as settings_mod
from .applyagent import ApplyAgent
from .browser import BrowserBusy, BrowserSession
from .extract import Posting
from .llm import BudgetExceeded, LLMClient, LLMError, Meter, cost_of
from .sites import SiteBlocked, SiteContext, get_site

# Sites where applying needs a government login (Singpass) the app must not touch.
HUMAN_APPLY_SITES = {"mycareersfuture"}

# Starting guesses for the estimate; replaced by your own averages once you have history.
DEFAULT_TOKENS = {"filter": (2600, 160), "tailor": (7500, 2600), "apply": (5200, 260)}
DEFAULT_SECONDS = {"filter": 4.0, "tailor": 30.0, "apply_step": 5.0, "pdf": 3.0}
DEFAULT_APPLY_STEPS = 14
DEFAULT_CANDIDATES_PER_MATCH = 3.0


class StopHunt(Exception):
    pass


# ── factories (tests swap these) ──────────────────────────────────────

def make_llm(settings: dict, meter: Meter, recorder, session_prefix: str = "") -> LLMClient:
    key = secrets_store.get_secret(secrets_store.api_key_name(settings["provider"])) or ""
    return LLMClient(settings, key, meter=meter, recorder=recorder, session_prefix=session_prefix)


def make_browser(headless: bool = True) -> BrowserSession:
    return BrowserSession(headless=headless)


# ── the task context ──────────────────────────────────────────────────

class Task:
    """Shared plumbing for one background task: log, spend meter, stop flag."""

    def __init__(self, kind: str, params: dict, stop_event: threading.Event):
        self.kind = kind
        self.params = params
        self.stop_event = stop_event
        self.settings = settings_mod.load()
        self.redact = secrets_store.Redactor.from_store(self.settings)
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

def judge(task: Task, posting: Posting, scope_text: str, profile_text: str) -> dict:
    system, user = prompts.filter_prompt(profile_text, scope_text, posting)
    data, _ = task.get_llm().chat_json(stage="filter", system=system, user=user, max_tokens=400,
                                       job_id=f"listing:{posting.url}")
    decision = str(data.get("decision", "")).lower()
    return {"apply": decision == "apply", "score": data.get("score"), "reasons": data.get("reasons") or [],
            "doubts": data.get("doubts") or ""}


def build_package(task: Task, browser: BrowserSession, posting: Posting, verdict: dict) -> int:
    """Folder + tracker row + posting.pdf + tailored resume/cover letter + match report."""
    t0 = time.monotonic()
    contact = profile.load_contact()
    profile_text = profile.load_profile()
    folder = render.make_job_folder(posting.company, posting.title)
    notes = f"Doubts: {verdict['doubts']}" if verdict.get("doubts") else ""
    job_id = db.add_job(source=posting.source, url=posting.url, company=posting.company, role=posting.title,
                        location=posting.location, apply_url=posting.apply_url or posting.url,
                        posting_text=posting.text, folder=folder.name, notes=notes,
                        match_score=_num(verdict.get("score")), run_id=task.run_id)
    task.current_job = job_id
    listing_key = f"listing:{posting.url}"
    for call_id in task.listing_calls.pop(listing_key, []):
        db.assign_call_to_job(call_id, job_id)  # the filter call counts towards this job too
    task.meter.move_job(listing_key, job_id)
    task.log(f"Building package for {posting.company} - {posting.title}")
    try:
        browser.render_pdf(render.posting_html(posting), folder / "posting.pdf")
        system, user = prompts.tailor_prompt(profile_text, contact, posting)
        data, _ = task.get_llm().chat_json(stage="tailor", system=system, user=user, max_tokens=6000, job_id=job_id)
        data = render.normalise_tailor(data)
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
        db.append_note(job_id, note)
        db.record_timing(task.run_id, job_id, "draft", time.monotonic() - t0)
        task.log(f"Package ready: {folder.name}")
    except BudgetExceeded as e:
        db.update_job(job_id, state="Error")
        db.append_note(job_id, f"Stopped while building: {e}")
        raise
    except LLMError as e:
        db.update_job(job_id, state="Error")
        db.append_note(job_id, f"Model error while tailoring: {e}")
        task.log(f"Model error while tailoring: {e}", "error")
    return job_id


def apply_job(task: Task, browser: BrowserSession, job_id: int) -> str:
    """Run the apply agent on one prepared job. Returns the new state."""
    job = db.get_job(job_id)
    task.current_job = job_id
    if job["source"] in HUMAN_APPLY_SITES:
        db.update_job(job_id, state="Needs you")
        db.append_note(job_id, f"Apply yourself (needs Singpass): {job['apply_url'] or job['url']}")
        task.log("This site needs Singpass to apply, so it is handed to you (no tokens spent).")
        return "Needs you"
    t0 = time.monotonic()
    folder = _folder(job)
    db.update_job(job_id, state="Applying")
    task.log(f"Applying: {job['company']} - {job['role']}")
    # The per-job caps count everything this job ever used (filter, tailor, earlier
    # apply attempts), not just this run, so pressing Approve & apply again can't reset them.
    task.meter.seed_job(job_id, job["cost_usd"] or 0.0, (job["tokens_in"] or 0) + (job["tokens_out"] or 0))
    browser.set_headless(not task.settings["apply"].get("show_browser", True))
    agent = ApplyAgent(browser=browser, llm=task.get_llm(), settings=task.settings, contact=profile.load_contact(),
                       profile_text=profile.load_profile(), screening=profile.load_screening(), job=job,
                       folder=folder, redactor=task.redact,
                       stop_event=task.stop_event, log=lambda m: task.log(m, job_id=job_id))
    try:
        result = agent.run(job["apply_url"] or job["url"])
    except Exception as e:  # browser trouble etc. — hand it to the person, never guess
        db.update_job(job_id, state="Needs you")
        db.append_note(job_id, f"Apply attempt broke ({type(e).__name__}); finish it here: {job['apply_url'] or job['url']}")
        task.log(f"Apply attempt broke: {type(e).__name__}: {str(e)[:200]}", "error")
        return "Needs you"
    finally:
        browser.set_headless(True)
    state = {"submitted": "Applied", "failed": "Error", "stopped": "Ready"}.get(result.status, "Needs you")
    db.update_job(job_id, state=state)
    db.append_note(job_id, f"{result.summary} (at {result.url})" if state != "Applied" else f"Applied: {result.summary}")
    db.record_timing(task.run_id, job_id, "apply", time.monotonic() - t0)
    task.log(f"Apply result: {state} - {result.summary}", "info" if state == "Applied" else "warn")
    return state


def _folder(job):
    from . import paths
    return paths.jobs_dir() / job["folder"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── the hunt loop ─────────────────────────────────────────────────────

def _avoid_hit(words: list[str], *texts: str) -> str | None:
    hay = " ".join(t or "" for t in texts).lower()
    for w in words:
        w = w.strip().lower()
        if w and re.search(r"\b" + re.escape(w) + r"\b", hay):
            return w
    return None


def candidates(task: Task, ctx: SiteContext, site_keys: list[str], keywords: list[str], location: str, max_pages: int = 5):
    """Round-robin over sites, then keywords, page by page; yields (site, listing)."""
    gens = []
    for key in site_keys:
        site = get_site(key, task.settings)
        gens.append((site, _site_listings(task, ctx, site, keywords, location, max_pages)))
    while gens:
        for site, gen in list(gens):
            try:
                yield site, next(gen)
            except StopIteration:
                gens.remove((site, gen))
            except SiteBlocked as e:
                task.log(f"{site.label}: blocked or needs login ({e}); skipping this site for this run.", "warn")
                gens.remove((site, gen))


def _site_listings(task, ctx, site, keywords, location, max_pages):
    for kw in keywords:
        for page_no in range(max_pages):
            task.check_stop()
            listings = site.search(ctx, kw, location, page_no)
            task.log(f"{site.label}: '{kw}' page {page_no + 1}: {len(listings)} results")
            if not listings:
                break
            yield from listings


def wait_for_rate_limit(task: Task, source: str) -> None:
    cap = int(task.settings["pacing"].get("max_apps_per_hour_per_site") or 0)
    if cap <= 0:
        return
    announced = False
    while True:
        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        if db.applied_since(source, since) < cap:
            return
        if not announced:
            task.log(f"Pacing: {cap} applications in the last hour on {source}; waiting before the next one.")
            announced = True
        for _ in range(60):
            task.check_stop()
            time.sleep(1)


def run_hunt(task: Task) -> str:
    p = task.params
    s = task.settings
    target = int(p.get("jobs") or s["hunt"]["jobs"])
    auto_submit = bool(p.get("auto_submit"))
    site_keys = [k for k in (p.get("sites") or s["hunt"]["sites"]) if k in settings_mod.SITES]
    keywords = [k for k in s["scope"].get("keywords", []) if k.strip()]
    if not keywords:
        raise LLMError("Add at least one search keyword on the Scope page first.")
    if not profile.is_ready():
        raise LLMError("Finish your Profile first (name, email and profile text).")
    task.get_llm()  # fail early on a missing key
    scope_text = profile.load_scope_text()
    profile_text = profile.load_profile()
    avoid = s["scope"].get("avoid_words", [])
    task.log(f"Hunt started: up to {target} jobs from {', '.join(site_keys)}; "
             f"{'AUTO-SUBMIT' if auto_submit else 'draft mode (nothing is submitted)'}; caps: {caps_text(s)}.")
    browser = make_browser(headless=True)
    ctx = SiteContext(browser, s["pacing"], task.stop_event, lambda m: task.log(m))
    matched, examined, skipped_known = 0, 0, 0
    companies, blocked = set(), set()
    max_candidates = int(s["hunt"].get("max_candidates") or 60)
    try:
        for site, listing in candidates(task, ctx, site_keys, keywords, s["scope"].get("location", "")):
            task.check_stop()
            task.current_job = None
            if matched >= target or examined >= max_candidates:
                break
            if db.was_seen(listing.url) or db.is_duplicate(listing.url, listing.company, listing.title):
                skipped_known += 1
                continue
            hit = _avoid_hit(avoid, listing.title, listing.company)
            if hit:
                db.mark_seen(listing.url, listing.source, listing.company, listing.title, "skip", f"avoid word: {hit}")
                task.log(f"Skip (avoid word '{hit}'): {listing.company} - {listing.title}")
                continue
            if listing.company and listing.company.lower() in companies:
                continue  # one application per company per run
            examined += 1
            if site.key in blocked:
                continue
            try:
                posting = site.fetch(ctx, listing)
            except SiteBlocked as e:
                blocked.add(site.key)
                task.log(f"{site.label}: blocked or needs login ({e}); skipping this site for this run.", "warn")
                continue
            except Exception as e:
                db.mark_seen(listing.url, listing.source, listing.company, listing.title, "error", str(e)[:200])
                task.log(f"Could not read posting {listing.url}: {type(e).__name__}", "warn")
                continue
            if len(posting.text) < 80:
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "error", "posting text too short")
                task.log(f"Posting had no readable text, skipped: {listing.url}", "warn")
                continue
            verdict = judge(task, posting, scope_text, profile_text)
            if not verdict["apply"]:
                db.mark_seen(listing.url, listing.source, posting.company, posting.title, "skip",
                             "; ".join(map(str, verdict["reasons"])))
                task.log(f"Not a fit: {posting.company} - {posting.title} ({'; '.join(map(str, verdict['reasons']))[:160]})")
                continue
            db.mark_seen(listing.url, listing.source, posting.company, posting.title, "match",
                         "; ".join(map(str, verdict["reasons"])))
            task.log(f"Match ({verdict.get('score')}/10): {posting.company} - {posting.title}")
            job_id = build_package(task, browser, posting, verdict)
            companies.add((posting.company or "").lower())
            matched += 1
            if auto_submit and db.get_job(job_id)["state"] == "Ready":
                wait_for_rate_limit(task, posting.source)
                apply_job(task, browser, job_id)
        task.current_job = None
        summary = f"{matched} package(s) built from {examined} posting(s) examined ({skipped_known} already known)."
        task.log("Hunt finished: " + summary)
        return summary
    finally:
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


def run_rebuild_pdfs(task: Task) -> str:
    """Re-make resume/cover letter PDFs from their (hand-edited) HTML."""
    from .browser import render_pdfs
    job = db.get_job(int(task.params["job_id"]))
    folder = _folder(job)
    pairs = [((folder / f"{n}.html").read_text(encoding="utf-8"), folder / f"{n}.pdf")
             for n in ("resume", "cover_letter") if (folder / f"{n}.html").exists()]
    render_pdfs(pairs)
    return f"Rebuilt {len(pairs)} PDF(s) for job {job['id']}."


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


TASKS = {"hunt": run_hunt, "apply": run_apply, "profile": run_profile, "rebuild": run_rebuild_pdfs}


# ── the background runner ─────────────────────────────────────────────

class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self.thread = None
        self.task = None
        self.stop_event = threading.Event()
        self.last_result = ""

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
            if task.current_job:
                job = db.get_job(task.current_job)
                if job and job["state"] in ("Found", "Applying"):
                    db.update_job(task.current_job, state="Error" if job["state"] == "Found" else "Ready")
                    db.append_note(task.current_job, f"Interrupted: {summary}")
            db.finish_run(task.run_id, status, summary)
            self.last_result = summary

    def stop(self) -> None:
        self.stop_event.set()

    def wait(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout)

    def status(self) -> dict:
        t = self.task
        meter = t.meter.snapshot() if t else None
        return {"busy": self.busy, "kind": t.kind if t else None, "run_id": t.run_id if t else None,
                "current_job": t.current_job if t else None, "meter": meter, "last_result": self.last_result}


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
    subscription = settings["provider"] in settings_mod.SUBSCRIPTION_PROVIDERS
    apply_sites = [x for x in sites if x not in HUMAN_APPLY_SITES]
    apply_share = (len(apply_sites) / len(sites)) if sites else 0
    apply_steps = DEFAULT_APPLY_STEPS * apply_share if auto_submit else 0.0
    per_job_tokens = (f_in + f_out) * ratio + (t_in + t_out) + (a_in + a_out) * apply_steps
    if subscription:
        per_job = 0.0
    else:
        per_job = (cost_of(models["filter"], f_in, f_out, settings) * ratio
                   + cost_of(models["tailor"], t_in, t_out, settings)
                   + cost_of(models["apply"], a_in, a_out, settings) * apply_steps)
    caps = settings["caps"]

    pacing = settings["pacing"]
    page_wait = (float(pacing["min_delay_s"]) + float(pacing["max_delay_s"])) / 2
    cand_seconds = page_wait * 1.1 + DEFAULT_SECONDS["filter"]  # posting load (+ a share of search pages) + judge
    draft_seconds = db.average_timing("draft") or (DEFAULT_SECONDS["tailor"] + DEFAULT_SECONDS["pdf"])
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
        "subscription": subscription, "calls_per_job": ratio + 1 + apply_steps,
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
