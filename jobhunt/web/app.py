"""The local web UI (http://127.0.0.1:8765). Server-rendered pages + a little JS.

Only reachable from this PC: the server binds to 127.0.0.1, and every request
must carry a local Host header (stops DNS-rebinding tricks); every form post must
come from this app's own pages (stops other websites posting to it).
Secret values can be saved here but are never sent back to the browser.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import (achievements, adcheck, company, db, demo_board, hunt, llm, login_check, marketpay, paths, profile,
               refresh, render, secrets_store, tuning, updates)
from .. import ask as ask_mod
from .. import settings as settings_mod
from .. import sites, statuscheck, workmode
from ..extract import salary_range_monthly
from ..sites import SITE_STATUS, apply_site

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


web_url = render.web_url


def local_path(url: str, default: str) -> str:
    """Redirect targets from forms: a path on this app only (stops open redirects)."""
    url = str(url or "")
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else default


def ago_text(ts: str, now: datetime | None = None) -> str:
    """A tracker timestamp in words: "just now", "30 minutes ago", "yesterday", "3 weeks ago".
    "" when it isn't a time; the Dashboard puts the exact time in the tooltip."""
    try:
        when = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    secs = max(0, int(((now or datetime.now()) - when).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        n, unit = secs // 60, "minute"
    elif secs < 86400:
        n, unit = secs // 3600, "hour"
    elif secs < 2 * 86400:
        return "yesterday"
    elif secs < 7 * 86400:
        n, unit = secs // 86400, "day"
    elif secs < 30 * 86400:
        n, unit = secs // (7 * 86400), "week"
    elif secs < 365 * 86400:
        n, unit = secs // (30 * 86400), "month"
    else:
        n, unit = secs // (365 * 86400), "year"
    return f"{n} {unit}{'s' if n != 1 else ''} ago"


def posted_text(day: str, today: date | None = None) -> str:
    """When the board says a job was posted, in words: "posted today", "posted 3 days ago" ('' = it didn't say)."""
    try:
        days = ((today or date.today()) - date.fromisoformat((day or "")[:10])).days
    except ValueError:
        return ""
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    if days < 30:
        return f"posted {days} days ago"
    return "posted 30+ days ago"


def pay_text(salary: str) -> str:
    """The pay on a card, short: '$5.5k-8k a month'. '' when it isn't stated; as written when it isn't monthly."""
    lo, hi = salary_range_monthly(salary or "")
    if not hi:
        text = " ".join((salary or "").split())
        return text if len(text) <= 28 else text[:27] + "…"

    def k(n):
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k" if n >= 1000 else f"{n:,.0f}"
    return f"${k(lo)} a month" if not lo or round(lo) == round(hi) else f"${k(lo)}-{k(hi)} a month"


NO_REPLY_DAYS = 14  # an Applied job this long without news gets the "No reply yet" tag


def no_reply_days(job: dict, today: date | None = None) -> int:
    """Days since an Applied job was applied for, once that's NO_REPLY_DAYS or more with no news (0 otherwise)."""
    if job.get("state") != "Applied":
        return 0
    try:
        days = ((today or date.today()) - date.fromisoformat((job.get("date_applied") or "")[:10])).days
    except ValueError:
        return 0
    return days if days >= NO_REPLY_DAYS else 0


_static_hashes: dict[str, str] = {}


def static_url(name: str) -> str:
    """/static/app.js?v=<its content hash>: after an update the browser fetches the new file at once
    instead of running its saved copy of the old one next to the new pages."""
    if name not in _static_hashes:
        try:
            _static_hashes[name] = hashlib.sha1((HERE / "static" / name).read_bytes()).hexdigest()[:10]
        except OSError:
            _static_hashes[name] = ""
    return f"/static/{name}?v={_static_hashes[name]}"


def icon(name: str, cls: str = "", label: str = "") -> Markup:
    """An icon from the sprite (static/icons.svg, Lucide): decorative unless it has a label."""
    a11y = f' role="img" aria-label="{escape(label)}"' if label else ' aria-hidden="true"'
    return Markup(f'<svg class="i{" " + escape(cls) if cls else ""}"{a11y} focusable="false">'
                  f'<use href="{static_url("icons.svg")}#{escape(name)}"></use></svg>')


# What a state is called on screen (the stored names stay as they are: the CSV export and old data use them)
STATE_LABELS = {"Not for me": "Not interested", "Rejected": "Employer said no", "Found": "Found", "Applying": "Applying now"}
STATE_ICONS = {"Ready": "circle-dot", "Needs you": "hand", "Applied": "send", "Interview": "calendar-check",
               "Offer": "party-popper", "Rejected": "circle-x", "Not for me": "circle-pause", "Error": "triangle-alert",
               "Applying": "loader-circle", "Found": "search"}


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def state_badge(state: str) -> Markup:
    """The coloured status chip, with its icon."""
    spin = " spin" if state == "Applying" else ""
    return Markup(f'<span class="badge {escape(state)}">{icon(STATE_ICONS.get(state, "circle-dot"), "sm" + spin)}'
                  f'{escape(state_label(state))}</span>')


def initials(name: str) -> str:
    """'Lumen Analytics' -> 'LA', 'ALTAX' -> 'A': the letters on a job's tile."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in (name or "")).split()
             if w.lower() not in ("pte", "ltd", "the", "sg", "singapore")]
    return "".join(w[0] for w in words[:2]).upper() or "?"


templates.env.filters["web_url"] = web_url
templates.env.globals["static_url"] = static_url
templates.env.globals["icon"] = icon
templates.env.globals["state_label"] = state_label
templates.env.globals["state_badge"] = state_badge
templates.env.globals["state_icons"] = STATE_ICONS
templates.env.globals["initials"] = initials
templates.env.filters["link_label"] = render.link_label
templates.env.globals["apply_site"] = apply_site  # MyCareersFuture for an Indeed job that applies there
templates.env.globals["company_line"] = company.line_for  # the one-line company summary on job lists
templates.env.globals["market_pay"] = marketpay.load  # what similar jobs pay, once checked
templates.env.globals["mcf_search"] = sites.MCF_SEARCH
templates.env.globals["work_mode"] = workmode.of  # remote / hybrid / on-site, when the posting says
templates.env.globals["why_needs_you"] = db.why_needs_you
templates.env.globals["reject_reasons"] = db.REJECT_REASONS
templates.env.globals["applied_or_rejected"] = db.applied_or_rejected  # the Dashboard's Applied / rejected column
templates.env.globals["ago_text"] = ago_text  # "2 days ago": when a job's state last changed
templates.env.globals["posted_text"] = posted_text  # "posted 3 days ago", as the board said
templates.env.globals["pay_text"] = pay_text  # "$5.5k-8k a month" on the cards
templates.env.globals["no_reply_days"] = no_reply_days  # the "No reply yet" tag on Applied jobs
templates.env.globals["ad_closed"] = adcheck.closed  # the ad has closed on its site

# MyCareersFuture isn't here: its applications need Singpass, so you finish them in your own browser
# and a login in JobHunt's browser would never be used.
LOGIN_SITES = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/login"),
    "jobstreet": ("JobStreet", "https://sg.jobstreet.com/oauth/login"),
    "indeed": ("Indeed", "https://secure.indeed.com/account/login"),
}
# Indeed's human check ("Verify you are human") shows on its job pages, so its Log in opens one of
# those too: you tick the check yourself, and later hunts can search again.
LOGIN_EXTRA_TABS = {"indeed": ["https://sg.indeed.com/"]}

_login_proc: subprocess.Popen | None = None
_check_proc: subprocess.Popen | None = None  # jobhunt.login_check, after the login window closes
_check_pending = False  # the login window closed (or Check again was pressed) and no check has run since


def login_browser_open() -> bool:
    return _login_proc is not None and _login_proc.poll() is None


def login_check_running() -> bool:
    return _check_proc is not None and _check_proc.poll() is None


def profile_busy_reason() -> str:
    """Why JobHunt's browser profile can't be used right now ('' = it can)."""
    if login_browser_open():
        return "Close the login browser window first."
    if login_check_running():
        return "JobHunt is checking your job-site logins (a few seconds). Try again in a moment."
    return ""


def start_pending_login_check() -> None:
    """Checks the logins once the browser profile is free (the login window closed, nothing running)."""
    global _check_proc, _check_pending
    if not _check_pending or hunt.runner.busy or profile_busy_reason():
        return
    _check_pending = False
    _check_proc = subprocess.Popen([sys.executable, "-m", "jobhunt.login_check", *LOGIN_SITES],
                                   cwd=str(paths.APP_DIR))


def bring_explorer_forward(folder: Path, wait: float = 3.0) -> None:
    """Restores and raises the Explorer window os.startfile just opened for `folder`.

    The click happens in the browser, so Windows' focus rules don't let JobHunt's own process hand
    the new window the foreground: Explorer then opens minimized or behind the browser. This finds
    the window by its title (the folder's name, or its full path with that Explorer option on),
    restores it, and raises it. Runs on a thread; does nothing off Windows or if no window turns up.
    """
    if sys.platform != "win32":
        return
    import threading
    threading.Thread(target=_raise_explorer, args=(folder, wait), daemon=True).start()


def _raise_explorer(folder: Path, wait: float) -> None:
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        titles = {folder.name.lower(), str(folder).lower()}
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "CabinetWClass":
                title = ctypes.create_unicode_buffer(1024)
                user32.GetWindowTextW(hwnd, title, 1024)
                if title.value.lower() in titles:
                    found.append(hwnd)
                    return False
            return True

        deadline = time.monotonic() + wait
        while not found and time.monotonic() < deadline:
            user32.EnumWindows(visit, 0)
            if not found:
                time.sleep(0.15)
        if not found:
            return
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: un-minimizes it, which the focus rules allow
        # A tap of Alt counts as fresh input to this process, which lets SetForegroundWindow raise
        # the window instead of only flashing it on the taskbar.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - raising the window is a nicety; the folder is already open
        pass


def create_app(port: int = 8765) -> FastAPI:
    app = FastAPI(title="JobHunt", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.port = port
    app.state.started = str(time.time_ns())  # tells the Restarting page that a new JobHunt has taken over
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(demo_board.router)
    db.init()
    if not hunt.runner.busy:
        db.recover_interrupted()
    s = settings_mod.load()
    if s["provider"] in settings_mod.SUBSCRIPTION_PROVIDERS:  # older flat-plan runs were recorded at $0
        db.backfill_costs(lambda model, tin, tout: llm.cost_of(model, tin, tout, s))
    demo_url = f"http://127.0.0.1:{port}/demo"
    if settings_mod.load().get("demo_board_url") != demo_url:  # follow the port we run on
        settings_mod.update({"demo_board_url": demo_url})

    own_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return PlainTextResponse("JobHunt only answers on this PC (127.0.0.1).", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # A change must come from JobHunt's own pages: same scheme, host AND port
            # (another local web app on another port is a different site). Browsers
            # always send Origin on form posts, so a post without one is refused.
            origin = request.headers.get("origin") or ""
            if not origin or origin == "null":
                ref = urlparse(request.headers.get("referer") or "")
                origin = f"{ref.scheme}://{ref.netloc}" if ref.scheme and ref.netloc else ""
            if origin.rstrip("/").lower() not in own_origins:
                return PlainTextResponse("Blocked: this request did not come from the JobHunt page.", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # always ask whether it changed (a quick 304 if not)
        if response.headers.get("content-type", "").startswith("text/html"):
            # Second line of defence: even if some web text slipped into a page as HTML,
            # no inline script, plugin or foreign resource would run.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    def page(request: Request, name: str, **ctx):
        s = settings_mod.load()
        try:
            toasts = achievements.check()
        except Exception:  # a bit of fun must never break a page
            toasts = []
        name_ = (profile.load_contact().get("name") or "").split()
        tune = request.query_params.get("tune", "")  # just rejected a job: offer one change to the Scope
        ctx.update(suggestion=tuning.suggest(db.get_job(int(tune))) if tune.isdigit() else None)
        undo = request.query_params.get("undo", "")  # "12:Ready": the job just changed, and the state to put back
        undo_id, _, undo_state = undo.partition(":")
        counts = db.state_counts()
        ctx.setdefault("nav", {"job": "applications"}.get(name.split(".")[0], name.split(".")[0]))
        ctx.update(request=request, status=hunt.runner.status(), settings=s,
                   msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
                   warn=request.query_params.get("warn", ""), nav_counts=counts,
                   undo={"job_id": int(undo_id), "state": undo_state}
                   if undo_id.isdigit() and undo_state in db.STATES else None,
                   subscription=s["provider"] in settings_mod.SUBSCRIPTION_PROVIDERS,
                   key_saved=secrets_store.has_secret(secrets_store.api_key_name(s["provider"])),
                   updates=updates.info(), here=request.url.path, waiting=counts.get("Ready", 0),
                   human_apply_sites=hunt.HUMAN_APPLY_SITES, toasts=toasts, trophies=achievements.earned_count(),
                   first_name=name_[0] if name_ else "", spend_total=db.total_spend(),
                   party=request.query_params.get("party", "") if request.query_params.get("party") in
                   ("offer", "interview") else "")
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str = "", err: str = "", party: str = "", warn: str = "",
             undo: tuple[int, str] | None = None) -> RedirectResponse:
        """Back to a page, with a message for its toast (the page's script takes it out of the address again).
        undo=(job_id, state) adds an Undo button to the toast that puts the job back in that state."""
        url, hash_, fragment = url.partition("#")  # the message goes before any #fragment, or it never shows
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        elif warn:
            url += f"{sep}warn={quote(warn)}"
        if undo:
            url += f"{'&' if '?' in url else '?'}undo={undo[0]}:{quote(undo[1])}"
        if party:
            url += f"{'&' if '?' in url else '?'}party={party}"
        return RedirectResponse(url + hash_ + fragment, status_code=303)

    # ── dashboard & jobs ───────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, state: str = ""):
        """Home: what needs you now (packages to approve, applications handed back), and the setup steps."""
        if state:  # an old link to a filtered list
            return RedirectResponse(f"/applications?state={quote(state)}", status_code=303)
        todo = {href.split("#")[0] for _, href in hunt.readiness()}  # the same test the Hunt page's Start uses
        checklist = [("Add your AI key", "/accounts" not in todo, "/accounts"),
                     ("Build your profile", "/profile" not in todo, "/profile"),
                     ("Set your search scope", "/scope" not in todo, "/scope")]
        return page(request, "dashboard.html", counts=db.state_counts(), checklist=checklist, replies=statuscheck.news(),
                    ready=db.list_jobs("Ready"), needs_you=db.list_jobs("Needs you"),
                    last_hunt=db.last_run("hunt"))

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, state: str = "", noreply: int = 0):
        """Every job JobHunt prepared, filterable by state (or by "No reply yet")."""
        everything = db.list_jobs()
        quiet = sum(1 for j in everything if no_reply_days(j))
        jobs = [j for j in everything if (not state or j["state"] == state) and (not noreply or no_reply_days(j))]
        contact, profile_text = profile.load_contact(), profile.load_profile()  # once, not per row
        profile_changed = {j["id"] for j in jobs
                           if refresh.profile_changes(j, paths.jobs_dir() / j["folder"], contact, profile_text)}
        return page(request, "applications.html", jobs=jobs, counts=db.state_counts(), profile_changed=profile_changed,
                    states=db.STATES, user_states=db.USER_STATES, state=state, replies=statuscheck.load_state(),
                    noreply=bool(noreply), quiet=quiet, no_reply_after=NO_REPLY_DAYS,
                    replies_todo=sum(len(v) for v in statuscheck.jobs_to_check().values()),
                    spend=db.total_spend(), runs=db.list_runs(5))

    @app.get("/export.csv")
    def export_csv():
        return Response(db.export_csv(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=applications.csv"})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        folder = paths.jobs_dir() / job["folder"]
        report = folder / "match_report.md"
        report_md = report.read_text(encoding="utf-8") if report.exists() else ""
        report_html = render.report_html(report_md) if report_md else ""
        has_package = (folder / "resume.pdf").is_file() if job["folder"] else False
        help_ = hunt.needs_you_fix(job)
        co = company.load(job)
        st = hunt.runner.status()
        dated = refresh.stale_letter_date(job, folder)  # "13 October" (with the year when it isn't this year's)
        letter_dated = (f"{dated.day} {dated:%B}" + (f" {dated.year}" if dated.year != refresh.today().year else "")
                        if dated else "")
        return page(request, "job.html", job=job, company=co, company_fields=company.FIELDS, pay=marketpay.load(job),
                    company_src={s_["id"]: s_ for s_ in (co or {}).get("sources", [])},
                    company_busy=st["busy"] and st["kind"] == "company" and job_id in (st["current_job"], st.get("job_id")), decoder=achievements.DECODER, files=render.list_files(folder) if job["folder"] else [],
                    report_html=report_html, calls=db.job_calls(job_id), user_states=db.USER_STATES,
                    human_apply=apply_site(job["source"], job["apply_url"]) in hunt.HUMAN_APPLY_SITES, has_package=has_package,
                    why=db.why_needs_you(job), help=help_, can_retry=help_["retry"] and has_package,
                    reject_why=db.reject_text(job), reqs=render.requirements(report_md), ad=adcheck.load(job),
                    ad_checkable=bool(adcheck.where(job)), ad_quick=adcheck.where(job) in adcheck.QUICK, ad_due=adcheck.due(job),
                    profile_changes=refresh.profile_changes(job, folder), letter_dated=letter_dated,
                    history=[l for l in (job.get("history") or "").splitlines() if l.strip()])

    @app.post("/jobs/{job_id}/state")
    def set_state(job_id: int, state: str = Form(...), next: str = Form("")):
        if state not in db.USER_STATES:
            return back(f"/jobs/{job_id}", err="Not a state you can set by hand.")
        before = db.get_job(job_id)
        db.update_job(job_id, state=state, by="you")
        cheer = {"Offer": " Congratulations! 🎉", "Interview": " Nice one. Go get it!"}.get(state, "")
        done = {"Not for me": "Rejected: JobHunt won't suggest this job again.",
                "Applied": "Marked as Applied today. Good luck!"}
        undo = (job_id, before["state"]) if before and before["state"] != state else None
        return back(local_path(next, f"/jobs/{job_id}"), msg=done.get(state, f"Marked as {state_label(state)}.{cheer}"),
                    party={"Offer": "offer", "Interview": "interview"}.get(state, ""), undo=undo)

    @app.post("/jobs/{job_id}/reject")
    def reject_job(job_id: int, reasons: list[str] = Form([]), note: str = Form(""), next: str = Form("")):
        before = db.get_job(job_id)
        if not before:
            return back("/", err="No such job.")
        db.reject(job_id, reasons, note)
        url, hash_, fragment = local_path(next, f"/jobs/{job_id}").partition("#")
        if tuning.suggest(db.get_job(job_id)):
            url += f"{'&' if '?' in url else '?'}tune={job_id}"
        return back(url + hash_ + fragment, msg="Rejected: JobHunt won't suggest this job again.",
                    undo=(job_id, before["state"]) if before["state"] != "Not for me" else None)

    @app.post("/jobs/{job_id}/undo")
    def undo_state(job_id: int, state: str = Form(...), next: str = Form("")):
        """The toast's Undo: puts a job back in the state it had before the last click."""
        job = db.get_job(job_id)
        if not job or state not in db.STATES or state == "Applying":
            return back(local_path(next, "/"), err="That can't be undone now.")
        fields = {"state": state}
        if job["state"] == "Not for me":
            fields.update(reject_reason="", reject_note="")
        if job["state"] == "Applied" and state != "Applied":
            fields["date_applied"] = ""
        db.update_job(job_id, by="you", **fields)
        db.add_history(job_id, f"You undid that: back to {state_label(state)}.")
        return back(local_path(next, f"/jobs/{job_id}"), msg=f"Undone: {job['company']} is back in {state_label(state)}.")

    @app.post("/api/ads/check")
    async def ads_check(request: Request):
        """Whether the ads waiting for you are still open (MyCareersFuture and LinkedIn: one plain request each,
        a few at a time; JobStreet's are checked as a hunt starts). ?job=<id> checks just that job, now."""
        one = request.query_params.get("job", "")
        if one.isdigit():
            job = db.get_job(int(one))
            if not job or adcheck.where(job) not in adcheck.QUICK:
                return JSONResponse({})
            return JSONResponse({job["id"]: adcheck.check_quick(job)})
        return JSONResponse(adcheck.check_due_quick())

    @app.post("/replies/check")
    def replies_check():
        """Applications' "Check for replies": read what Indeed and JobStreet say about jobs he applied for."""
        if not statuscheck.jobs_to_check():
            return back("/applications", msg="Nothing to check: no Applied jobs from Indeed or JobStreet.")
        try:
            if profile_busy_reason():
                raise RuntimeError(profile_busy_reason())
            hunt.runner.start("replies")
        except RuntimeError as e:
            return back("/applications", err=str(e))
        return back("/hunt", msg="Checking Indeed and JobStreet for replies: follow it here.")

    @app.post("/replies/seen")
    def replies_seen():
        statuscheck.dismiss_news()
        return back("/")

    @app.post("/tune")
    def tune_scope(job_id: int = Form(0), next: str = Form("")):
        """Yes to the one Scope change offered after a reject (worked out again here, never taken from the form)."""
        sug = tuning.suggest(db.get_job(job_id))
        if not sug or sug["kind"] == "link":
            return back(local_path(next, "/scope"), msg="Nothing to change: your Scope already covers that.")
        tuning.apply(sug)
        return back(local_path(next, "/scope"), msg=sug["done"])

    @app.post("/jobs/{job_id}/notes")
    def set_notes(job_id: int, notes: str = Form("")):
        db.update_job(job_id, notes=notes)
        return back(f"/jobs/{job_id}", msg="Notes saved.")

    @app.post("/jobs/{job_id}/apply")
    def approve_apply(job_id: int):
        job = db.get_job(job_id)
        if not job or job["state"] not in hunt.APPLY_FROM_STATES:
            return back(f"/jobs/{job_id}" if job else "/", err="This job can't be applied to now"
                        + (f" (it is {job['state']})." if job else ": it doesn't exist."))
        try:
            if profile_busy_reason():
                raise RuntimeError(profile_busy_reason())
            hunt.runner.start("apply", {"job_id": job_id})
        except RuntimeError as e:
            return back(f"/jobs/{job_id}", err=str(e))
        return back("/hunt", msg="Applying now: follow it here.")

    @app.post("/jobs/{job_id}/rebuild")
    def rebuild(job_id: int):
        try:
            hunt.runner.start("rebuild", {"job_id": job_id})
            hunt.runner.wait(120)
        except RuntimeError as e:
            return back(f"/jobs/{job_id}", err=str(e))
        if hunt.runner.last_kind == "rebuild" and hunt.runner.last_status not in ("", "done"):
            return back(f"/jobs/{job_id}", err=f"Rebuild didn't finish: {hunt.runner.last_result}")
        return back(f"/jobs/{job_id}", msg=hunt.runner.last_result)

    @app.post("/jobs/{job_id}/company")
    def company_lookup(job_id: int):
        """Look up this job's company (again): a background task, so the page stays usable meanwhile."""
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        try:
            if profile_busy_reason():
                raise RuntimeError(profile_busy_reason())
            hunt.runner.start("company", {"job_id": job_id})
        except RuntimeError as e:
            return back(f"/jobs/{job_id}#company", err=str(e))
        return back(f"/jobs/{job_id}#company", msg=f"Looking up {job['company']}: this takes under a minute.")

    @app.post("/jobs/{job_id}/market-pay")
    def market_pay(job_id: int):
        """Check what similar jobs pay: one search on MyCareersFuture, a few seconds, no AI."""
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        result = marketpay.check(job_id) or {}
        if result.get("status") == "ok":
            return back(f"/jobs/{job_id}#pay")
        return back(f"/jobs/{job_id}#pay", err=result.get("why") or "Couldn't work out market pay.")

    @app.get("/jobs/{job_id}/apply")
    def open_apply_page(job_id: int):
        """Open apply page. An Indeed job from MyCareersFuture whose own page the hunt couldn't pick out
        (its link is a MyCareersFuture search) gets one more look, with the pay to tell same-name ads apart."""
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        url = job["apply_url"] or job["url"]
        if url.startswith(sites.MCF_SEARCH):
            try:
                found = sites.mcf_job_url(_MCFSearch(), job["role"], job["company"], job["salary"])
            except (httpx.HTTPError, ValueError, sites.SiteBlocked, sites.PageUnavailable):
                found = ""  # offline or MyCareersFuture down: the search still finds it
            if found:
                db.update_job(job_id, apply_url=found)
                url = found
        return RedirectResponse(web_url(url) if web_url(url) != "#" else f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/open-folder")
    def open_folder(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        folder = paths.jobs_dir() / job["folder"]
        if folder.exists() and hasattr(os, "startfile"):
            os.startfile(str(folder))  # opens Explorer on this PC
            bring_explorer_forward(folder)
            return back(f"/jobs/{job_id}", msg="Opened in File Explorer.")
        return back(f"/jobs/{job_id}", err=f"Folder: {folder}")

    @app.get("/files/{job_id}/{name}")
    def job_file(job_id: int, name: str):
        job = db.get_job(job_id)
        if not job:
            return PlainTextResponse("not found", status_code=404)
        folder = (paths.jobs_dir() / job["folder"]).resolve()
        target = (folder / name).resolve()
        if target.parent != folder or not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        # Rebuild PDFs rewrites these files under the same name: the browser (and its PDF viewer) must
        # never show a copy it kept from before, so nothing here is cached.
        fresh = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}
        if target.suffix == ".html":  # show the source as text; it's for editing, not for running here
            return PlainTextResponse(target.read_text(encoding="utf-8"), headers=fresh)
        media = {".pdf": "application/pdf", ".md": "text/plain; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        return FileResponse(str(target), media_type=media, content_disposition_type="inline", headers=fresh)

    # ── profile ────────────────────────────────────────────────────────
    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        text = profile.load_profile()
        restore = Path(request.query_params.get("restore", "")).name
        return page(request, "profile.html", contact=profile.load_contact(), profile_text=text or profile.PROFILE_TEMPLATE,
                    restore=restore if restore and (paths.cv_dir() / ".removed" / restore).is_file() else "",
                    has_profile=bool(text.strip()), cvs=[p.name for p in profile.list_cvs()],
                    screening=profile.load_screening(), screening_fields=profile.SCREENING_FIELDS)

    @app.post("/profile/save")
    async def save_profile_page(request: Request):
        """Contact details, screening answers and profile text: one Save for the whole page."""
        f = await request.form()
        profile.save_contact({k: str(f.get(k, "")).strip() for k in ("name", "email", "phone", "location")}
                             | {"links": str(f.get("links", ""))})
        profile.save_screening({k: f.get(k, "") for k in profile.SCREENING_FIELDS})
        text = str(f.get("profile_text", ""))
        if text.replace("\r\n", "\n").strip() == profile.PROFILE_TEMPLATE.strip():
            text = ""  # the outline shown in an empty box isn't a profile
        profile.save_profile(text)
        return back("/profile", msg="Profile saved.")

    @app.post("/profile/screening")
    async def save_screening(request: Request):
        f = await request.form()
        profile.save_screening({k: f.get(k, "") for k in profile.SCREENING_FIELDS})
        return back("/profile#screening", msg="Screening answers saved.")

    @app.post("/profile/contact")
    def save_contact(name: str = Form(""), email: str = Form(""), phone: str = Form(""), location: str = Form(""),
                     links: str = Form("")):
        profile.save_contact({"name": name.strip(), "email": email.strip(), "phone": phone.strip(),
                              "location": location.strip(), "links": links})
        return back("/profile", msg="Contact details saved.")

    @app.post("/profile/text")
    def save_profile_text(profile_text: str = Form("")):
        profile.save_profile(profile_text)
        return back("/profile", msg="Profile saved.")

    @app.post("/profile/upload")
    async def upload_cvs(files: list[UploadFile] = File(...)):
        saved = 0
        for f in files:
            name = Path(f.filename or "").name
            if Path(name).suffix.lower() not in profile.CV_SUFFIXES:
                continue
            (paths.cv_dir() / name).write_bytes(await f.read())
            saved += 1
        return back("/profile", msg=f"Uploaded {saved} file(s).") if saved else back("/profile", err="Use PDF, Word (.docx), .txt or .md files.")

    @app.post("/profile/cv/delete")
    def delete_cv(name: str = Form(...)):
        """Moves the CV aside (cv/.removed), so the toast's Undo can put it back."""
        target = (paths.cv_dir() / Path(name).name)
        if target.is_file():
            bin_ = paths.cv_dir() / ".removed"
            bin_.mkdir(exist_ok=True)
            target.replace(bin_ / target.name)
            return back(f"/profile?restore={quote(target.name)}", msg=f"Removed {target.name}.")
        return back("/profile", msg="Removed.")

    @app.post("/profile/cv/restore")
    def restore_cv(name: str = Form(...)):
        kept = paths.cv_dir() / ".removed" / Path(name).name
        if not kept.is_file():
            return back("/profile", err="That file can't be brought back now.")
        kept.replace(paths.cv_dir() / kept.name)
        return back("/profile", msg=f"{kept.name} is back.")

    @app.post("/profile/build")
    def build_profile():
        try:
            hunt.runner.start("profile")
        except RuntimeError as e:
            return back("/profile", err=str(e))
        return back("/hunt", msg="Building your profile: about a minute. "
                                 "You'll be taken back to the Profile page when it's done.")

    # ── ask: a question, or a recruiter's message, answered from the resume ──
    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, id: str = "", job: int = 0):
        past = ask_mod.history()
        shown = ask_mod.get(id) if id else None
        about = db.get_job(job) if job else None
        return page(request, "ask.html", shown=shown, past=[a for a in past if not shown or a["id"] != shown["id"]],
                    about=about, screening=profile.load_screening(), screening_fields=profile.SCREENING_FIELDS,
                    has_cv=bool(profile.load_profile().strip() or profile.list_cvs()))

    @app.post("/ask")
    def ask_question(question: str = Form(""), job_id: int = Form(0)):
        """One AI call while you wait (about 10-30 s): it doesn't use the background task, so it works mid-hunt."""
        back_to = f"/ask?job={job_id}" if job_id else "/ask"
        try:
            entry = ask_mod.ask(question, job_id or None)
        except (llm.LLMError, llm.BudgetExceeded) as e:
            return back(back_to, err=str(e))
        except httpx.HTTPError:
            return back(back_to, err="Couldn't reach the AI provider. Check your internet connection and try again.")
        return back(f"/ask?id={entry['id']}#answer")

    @app.post("/ask/remember")
    def ask_remember(field: str = Form(...), value: str = Form(""), id: str = Form("")):
        """Save one answer as the Profile's screening answer, so forms and the next Ask say the same."""
        if field not in profile.SCREENING_FIELDS or not value.strip():
            return back(f"/ask?id={quote(id)}#answer", err="Nothing to save.")
        answers = profile.load_screening()
        answers[field] = value.strip()
        profile.save_screening(answers)
        return back(f"/ask?id={quote(id)}#answer", msg=f"Saved to your Profile: {profile.SCREENING_FIELDS[field][0]}.")

    @app.post("/ask/forget")
    def ask_forget(id: str = Form(...)):
        ask_mod.forget(id)
        return back("/ask", msg="Removed.")

    # ── scope ──────────────────────────────────────────────────────────
    @app.get("/scope", response_class=HTMLResponse)
    def scope_page(request: Request):
        text = profile.load_scope_text()
        return page(request, "scope.html", scope_text=text, guide=profile.scope_guide(),
                    old_example=profile.scope_is_example(text), has_profile=bool(profile.load_profile().strip()))

    def store_scope(keywords: str, location: str, avoid_words: str, scope_text: str) -> None:
        settings_mod.update({"scope": {
            "keywords": [k.strip() for k in keywords.splitlines() if k.strip()],
            "location": location.strip(),
            "avoid_words": [w.strip() for w in avoid_words.replace(",", "\n").splitlines() if w.strip()],
        }})
        profile.save_scope_text(scope_text)

    @app.post("/scope")
    def save_scope(keywords: str = Form(""), location: str = Form(""), avoid_words: str = Form(""), scope_text: str = Form("")):
        store_scope(keywords, location, avoid_words, scope_text)
        if profile.scope_is_example(scope_text):
            return back("/scope", warn="Saved, but this is still the example from the original JobHunt, not yours. "
                                      "Write your own (or press Draft from my profile): hunts won't start until you do.")
        if not profile.scope_ready(scope_text):
            return back("/scope", warn="Saved. Describe the jobs you want in the big box (or press Draft from my "
                                      "profile): hunts won't start until you do.")
        return back("/scope", msg="Preferences saved.")

    @app.post("/scope/draft")
    def draft_scope(keywords: str | None = Form(None), location: str = Form(""), avoid_words: str = Form(""),
                    scope_text: str = Form("")):
        if keywords is not None:  # the button sits in the Scope form: keep what was typed there first
            if hunt.runner.busy:
                return back("/scope", err="Something is already running. Stop it or wait for it to finish.")
            store_scope(keywords, location, avoid_words, scope_text)
        if not profile.load_profile().strip():
            return back("/scope", err="Build your Profile first: the scope is drafted from it.")
        try:
            hunt.runner.start("scope")
        except RuntimeError as e:
            return back("/scope", err=str(e))
        return back("/hunt", msg="Drafting your preferences: under a minute. "
                                 "You'll be taken back to review them when it's done.")

    # ── accounts: AI key, site logins, browser sessions ───────────────
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        start_pending_login_check()
        s = settings_mod.load()
        keys = {p: secrets_store.has_secret(secrets_store.api_key_name(p)) for p in settings_mod.PROVIDERS}
        logins = []
        for l in s.get("logins", []):
            logins.append({**l, "has_user": secrets_store.has_secret(secrets_store.login_secret_name(l["name"], "username")),
                           "has_pw": secrets_store.has_secret(secrets_store.login_secret_name(l["name"], "password"))})
        return page(request, "accounts.html", keys=keys, providers=settings_mod.PROVIDERS, logins=logins,
                    login_sites=LOGIN_SITES, browser_open=login_browser_open(), checking=login_check_running(),
                    check_waiting=_check_pending, login_status=login_check.load_status())

    @app.post("/accounts/apikey")
    def save_api_key(provider: str = Form(...), api_key: str = Form(""), next: str = Form("")):
        dest = next if next in ("/accounts", "/settings") else "/accounts"
        if provider not in settings_mod.PROVIDERS:
            return back(dest, err="Unknown provider.")
        api_key = api_key.strip()
        if len(api_key) < 8:
            return back(dest, err="That doesn't look like an API key.")
        secrets_store.set_secret(secrets_store.api_key_name(provider), api_key)
        return back(dest, msg="Key saved in Windows Credential Manager.")

    @app.post("/accounts/apikey/delete")
    def delete_api_key(provider: str = Form(...)):
        secrets_store.delete_secret(secrets_store.api_key_name(provider))
        return back("/accounts", msg="Key removed.")

    @app.post("/accounts/apikey/test")
    def test_api_key(provider: str = Form(...)):
        key = secrets_store.get_secret(secrets_store.api_key_name(provider))
        if not key:
            return back("/accounts", err="No key saved for that provider.")
        try:
            if provider == "opencode_go":
                s = settings_mod.load()
                probe = {**s, "provider": "opencode_go",
                         "models": {**s["models"], "filter": s["models"]["filter"] if s["provider"] == "opencode_go"
                                    else settings_mod.DEFAULT_MODEL}}
                resp = llm.LLMClient(probe, key, session_prefix="keytest").chat(
                    stage="filter", system="Reply with the single word OK.", user="Say OK.", max_tokens=5)
                return back("/accounts", msg=f"The key works ({resp.tokens_in + resp.tokens_out} tokens used for the test).")
            if provider == "openrouter":
                r = httpx.get(f"{llm.OPENROUTER_URL}/key", headers={"Authorization": f"Bearer {key}"}, timeout=20)
            elif provider == "anthropic":
                r = httpx.get("https://api.anthropic.com/v1/models", timeout=20,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            else:
                base = settings_mod.load().get("base_url", "").rstrip("/")
                r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        except httpx.HTTPError as e:
            return back("/accounts", err="Couldn't reach the AI provider. Check your internet connection and try again.")
        except llm.LLMError as e:
            return back("/accounts", err=str(e))
        if r.status_code == 200:
            extra = ""
            if provider == "openrouter":
                d = r.json().get("data", {})
                if d.get("limit_remaining") is not None:
                    extra = f" Credit left on this key: ${d['limit_remaining']:.2f}."
            return back("/accounts", msg="The key works." + extra)
        return back("/accounts", err="The provider turned this key down. Check you copied all of it, or make a new one." if r.status_code in (401, 403) else f"The provider didn't answer properly (error {r.status_code}). Try again in a minute.")

    @app.post("/accounts/logins")
    def save_login(name: str = Form(...), username: str = Form(""), password: str = Form(""), domains: str = Form("")):
        name = name.strip()
        if not secrets_store.valid_login_name(name):
            return back("/accounts", err="Login name: letters, digits, - and _ only (e.g. default, workday).")
        s = settings_mod.load()
        doms = [d.strip().lower().lstrip(".") for d in domains.replace(",", " ").split() if d.strip()]
        doms = [urlparse(d).hostname or d if "://" in d else d.split("/")[0] for d in doms]
        if not doms:
            return back("/accounts", err="List the sites this login is for (e.g. myworkdayjobs.com). "
                                         "A login is never used on any other site.")
        bad = [d for d in doms if "." not in d and d != "localhost"]
        if bad:
            return back("/accounts", err=f"'{bad[0]}' is too broad: use a site name like greenhouse.io.")
        logins = [l for l in s.get("logins", []) if l["name"] != name] + [{"name": name, "domains": doms}]
        settings_mod.update({"logins": logins})
        if username.strip():
            secrets_store.set_secret(secrets_store.login_secret_name(name, "username"), username.strip())
        if password:
            secrets_store.set_secret(secrets_store.login_secret_name(name, "password"), password)
        return back("/accounts", msg=f"Login '{name}' saved (values are in Windows Credential Manager).")

    @app.post("/accounts/logins/delete")
    def delete_login(name: str = Form(...)):
        s = settings_mod.load()
        settings_mod.update({"logins": [l for l in s.get("logins", []) if l["name"] != name]})
        for f in ("username", "password"):
            secrets_store.delete_secret(secrets_store.login_secret_name(name, f))
        return back("/accounts", msg=f"Login '{name}' deleted.")

    @app.post("/accounts/browser")
    def open_login_browser(site: str = Form(...)):
        global _login_proc, _check_pending
        if hunt.runner.busy:
            return back("/accounts", err="Wait for the hunt to finish (or stop it) first.")
        if login_browser_open():
            return back("/accounts", err="The login browser is already open.")
        if login_check_running():
            return back("/accounts", err=profile_busy_reason())
        urls = ([LOGIN_SITES[site][1], *LOGIN_EXTRA_TABS.get(site, [])] if site in LOGIN_SITES
                else [u for _, u in LOGIN_SITES.values()])
        _login_proc = subprocess.Popen([sys.executable, "-m", "jobhunt.login_browser", *urls], cwd=str(paths.APP_DIR))
        _check_pending = True  # checked as soon as the window is closed
        return back("/accounts", msg="A browser window opened. Log in, then close that window.")

    @app.post("/accounts/check")
    def check_logins():
        global _check_pending
        if login_browser_open() or login_check_running():
            return back("/accounts", err=profile_busy_reason())
        if hunt.runner.busy:
            return back("/accounts", err="Wait for the current task to finish (or stop it), then check again.")
        _check_pending = True
        start_pending_login_check()
        return back("/accounts")

    @app.post("/accounts/browser/close")
    def close_login_browser():
        # For a window that's hidden or won't close (a Singpass pop-up left behind, say):
        # ends the helper and every browser it started, so the profile is free again.
        if login_browser_open():
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(_login_proc.pid)], capture_output=True)
            else:
                _login_proc.kill()
            try:
                _login_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                return back("/accounts", err="The login browser didn't close. Restart JobHunt.")
        return back("/accounts", msg="Login browser closed.")

    @app.get("/api/login-browser")
    def api_login_browser():
        start_pending_login_check()
        return JSONResponse({"open": login_browser_open(), "checking": login_check_running() or _check_pending})

    # ── achievements (not in the menu) ────────────────────────────────
    @app.get("/achievements", response_class=HTMLResponse)
    def achievements_page(request: Request):
        achievements.set_flag("snoop")
        return page(request, "achievements.html", board=achievements.board())

    @app.post("/achievements/flag")
    def achievement_flag(name: str = Form("")):
        if name in achievements.SETTABLE_FLAGS:
            achievements.set_flag(name)
        return Response(status_code=204)

    # ── version & updates ──────────────────────────────────────────────
    @app.get("/api/version")
    def api_version():  # also how a second run.bat tells that JobHunt is the one on this port
        return JSONResponse({"app": "JobHunt", "version": updates.info()["version"], "started": app.state.started})

    @app.get("/changelog", response_class=HTMLResponse)
    def changelog_page(request: Request):
        return page(request, "changelog.html", log=updates.changelog())

    @app.post("/update/seen")
    def update_seen(next: str = Form("/")):
        updates.mark_news_seen()
        return back(local_path(next, "/"))

    @app.post("/update/restart")
    def update_restart(request: Request, next: str = Form("/")):
        if hunt.runner.busy:
            return back(local_path(next, "/"), err="Wait for the current task to finish (or stop it), then restart.")
        if profile_busy_reason():
            return back(local_path(next, "/"), err=profile_busy_reason())
        try:
            updates.restart(app.state.port)
        except OSError as e:
            return back(local_path(next, "/"), err=f"Couldn't restart JobHunt ({type(e).__name__}). Close the black "
                                                    "JobHunt window and double-click run.bat instead.")
        updates.exit_soon()
        return page(request, "restarting.html", started=app.state.started)

    # ── hunt ───────────────────────────────────────────────────────────
    @app.get("/hunt", response_class=HTMLResponse)
    def hunt_page(request: Request):
        s = settings_mod.load()
        ready = db.list_jobs("Ready")
        last_hunt = db.last_run("hunt")
        return page(request, "hunt.html", sites=settings_mod.SITES, site_status=SITE_STATUS, ready=ready,
                    needs_you=db.list_jobs("Needs you"), missing=hunt.readiness(s), ticked=hunt.suggested_sites(s),
                    practice_first=not s["hunt"].get("sites_chosen") and not db.practice_hunt_done(),
                    events=db.recent_events(150), last_hunt=last_hunt,
                    last_hunt_ready=db.count_jobs(last_hunt["id"], "Ready") if last_hunt else 0,
                    last_hunt_built=db.count_jobs(last_hunt["id"]) if last_hunt else 0,
                    site_stats=_site_stats(last_hunt), stat_names=hunt.SiteStats.NAMES)

    @app.post("/hunt/start")
    async def hunt_start(request: Request):
        form = await request.form()
        s = settings_mod.load()
        sites = [k for k in form.getlist("sites") if k in settings_mod.SITES]
        if not sites:
            return back("/hunt", err="Pick at least one site.")
        try:
            jobs = max(1, min(200, int(form.get("jobs") or 10)))
        except ValueError:
            jobs = 10
        auto = form.get("mode") == "auto"
        chosen = s["hunt"].get("sites_chosen") or any(k != "demo" for k in sites)
        settings_mod.update({"hunt": {"jobs": jobs, "sites": sites, "sites_chosen": chosen}})  # the mode is never remembered
        if profile_busy_reason():
            return back("/hunt", err=profile_busy_reason())
        missing = hunt.readiness(s)
        if missing:
            return back("/hunt", err=hunt.readiness_text(missing))
        try:
            hunt.runner.start("hunt", {"jobs": jobs, "sites": sites, "auto_submit": auto})
        except RuntimeError as e:
            return back("/hunt", err=str(e))
        return back("/hunt", msg="Hunt started.")

    @app.post("/hunt/stop")
    def hunt_stop():
        hunt.runner.stop()
        return back("/hunt", msg="Stopping after the current step...")

    @app.get("/api/status")
    def api_status(after: int = 0):
        st = hunt.runner.status()
        st["events"] = db.events_since(after)
        if st["run_id"]:
            st["run"] = db.get_run(st["run_id"])
        if st["current_job"]:
            j = db.get_job(st["current_job"])
            st["job"] = {k: j[k] for k in ("id", "company", "role", "state", "cost_usd", "tokens_in", "tokens_out")} if j else None
        st["counts"] = db.state_counts()
        return JSONResponse(st)

    @app.get("/api/estimate")
    def api_estimate(jobs: int = 10, sites: str = "", auto: int = 0):
        s = settings_mod.load()
        keys = [k for k in sites.split(",") if k in settings_mod.SITES] or hunt.suggested_sites(s)
        return JSONResponse(hunt.estimate(s, max(1, jobs), keys, bool(auto)))

    # ── settings ───────────────────────────────────────────────────────
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        s = settings_mod.load()
        prices = {stage: llm.price_for(m, s) for stage, m in s["models"].items()}
        return page(request, "settings.html", providers=settings_mod.PROVIDERS, prices=prices,
                    defaults=settings_mod.PROVIDER_DEFAULT_MODELS,
                    provider_short=settings_mod.PROVIDER_SHORT.get(s["provider"], s["provider"]))

    @app.post("/settings")
    async def save_settings(request: Request):
        f = await request.form()
        s = settings_mod.load()

        def num(name, default, lo=0.0, hi=1e9):
            try:
                return max(lo, min(hi, float(f.get(name, default))))
            except (TypeError, ValueError):
                return default

        provider = f.get("provider", s["provider"])
        if provider not in settings_mod.PROVIDERS:
            provider = s["provider"]
        models = {stage: (f.get(f"model_{stage}") or "").strip() for stage in ("filter", "tailor", "apply", "profile")}
        if provider != s["provider"] and f.get("reset_models"):
            models = dict(settings_mod.PROVIDER_DEFAULT_MODELS[provider])
        patch = {
            "provider": provider,
            # the endpoint URL is only kept for the custom provider, so no key can be sent to a leftover URL
            "base_url": (f.get("base_url") or "").strip() if provider == "openai_compatible" else "",
            "models": models,
            "reasoning": f.get("reasoning") == "on",
            "caps": {"per_job_usd": num("per_job_usd", s["caps"]["per_job_usd"], 0.0, 100),
                     "per_run_usd": num("per_run_usd", s["caps"]["per_run_usd"], 0.0, 1000),
                     "per_job_tokens": int(num("per_job_tokens", 150000, 0, 1e9)),
                     "per_run_tokens": int(num("per_run_tokens", 3000000, 0, 1e10))},
            "pacing": {"min_delay_s": num("min_delay_s", 4, 0, 600), "max_delay_s": num("max_delay_s", 9, 0, 600),
                       "max_apps_per_hour_per_site": int(num("max_apps_per_hour_per_site", 15, 0, 1000)),
                       "action_delay_s": num("action_delay_s", 0.8, 0, 30)},
            "apply": {"max_steps": int(num("max_steps", 30, 3, 200)), "show_browser": f.get("show_browser") == "on",
                      "allow_register": f.get("allow_register") == "on"},
            "hunt": {"max_candidates": int(num("max_candidates", 60, 1, 2000)), "check_replies": f.get("check_replies") == "on"},
            "notify": f.get("notify") == "on",
        }
        if patch["pacing"]["max_delay_s"] < patch["pacing"]["min_delay_s"]:
            patch["pacing"]["max_delay_s"] = patch["pacing"]["min_delay_s"]
        settings_mod.update(patch)
        return back("/settings", msg="Settings saved.")

    @app.post("/settings/prices")
    def refresh_prices():
        try:
            n = llm.refresh_openrouter_prices()
        except Exception as e:
            return back("/settings", err="Couldn't download prices. Check your internet connection and try again.")
        return back("/settings", msg=f"Downloaded current prices for {n} OpenRouter models.")

    @app.post("/settings/forget-seen")
    def forget_seen():
        db.forget_seen()
        return back("/settings", msg="Forgot every posting seen so far; the next hunt will look at them again.")

    return app


def _site_stats(run: dict | None) -> dict:
    """A hunt's counts per site and keyword (hunt.SiteStats), {} for a hunt from before they were kept."""
    try:
        data = json.loads((run or {}).get("site_stats") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class _MCFSearch:
    """Just enough of a hunt's SiteContext for sites.mcf_job_url, outside a hunt."""
    def get_json(self, url, method="GET", body=None, pace=True):
        with httpx.Client(headers={"User-Agent": marketpay.UA}, timeout=8) as c:
            r = c.request(method, url, json=body)
            r.raise_for_status()
            return r.json()
