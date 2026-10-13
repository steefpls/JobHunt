"""The local web UI (http://127.0.0.1:8765). Server-rendered pages + a little JS.

Only reachable from this PC: the server binds to 127.0.0.1, and every request
must carry a local Host header (stops DNS-rebinding tricks); every form post must
come from this app's own pages (stops other websites posting to it).
Secret values can be saved here but are never sent back to the browser.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, demo_board, hunt, llm, paths, profile, render, secrets_store
from .. import settings as settings_mod
from ..sites import SITE_STATUS

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def web_url(url) -> str:
    """Only http(s) links from postings become clickable (no javascript: and friends)."""
    url = str(url or "").strip()
    return url if urlparse(url).scheme in ("http", "https") else "#"


def local_path(url: str, default: str) -> str:
    """Redirect targets from forms: a path on this app only (stops open redirects)."""
    url = str(url or "")
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else default


templates.env.filters["web_url"] = web_url

LOGIN_SITES = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/login"),
    "jobstreet": ("JobStreet", "https://sg.jobstreet.com/oauth/login"),
    "mycareersfuture": ("MyCareersFuture (Singpass)", "https://www.mycareersfuture.gov.sg/"),
    "indeed": ("Indeed", "https://secure.indeed.com/account/login"),
}

_login_proc: subprocess.Popen | None = None


def login_browser_open() -> bool:
    return _login_proc is not None and _login_proc.poll() is None


def create_app(port: int = 8765) -> FastAPI:
    app = FastAPI(title="JobHunt", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.port = port
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
        ctx.update(request=request, nav=name.split(".")[0], status=hunt.runner.status(), settings=s,
                   msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
                   subscription=s["provider"] in settings_mod.SUBSCRIPTION_PROVIDERS,
                   key_saved=secrets_store.has_secret(secrets_store.api_key_name(s["provider"])))
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
        url, hash_, fragment = url.partition("#")  # the message goes before any #fragment, or it never shows
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url + hash_ + fragment, status_code=303)

    # ── dashboard & jobs ───────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, state: str = ""):
        s = settings_mod.load()
        checklist = [
            ("Add your AI key", secrets_store.has_secret(secrets_store.api_key_name(s["provider"])), "/accounts"),
            ("Build your profile", profile.is_ready(), "/profile"),
            ("Set your search scope", bool(s["scope"]["keywords"]) and bool(profile.load_scope_text().strip())
             and not profile.scope_is_example(profile.load_scope_text()), "/scope"),
        ]
        return page(request, "dashboard.html", jobs=db.list_jobs(state or None), counts=db.state_counts(),
                    states=db.STATES, user_states=db.USER_STATES, state=state, checklist=checklist,
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
        report_html = render.report_html(report.read_text(encoding="utf-8")) if report.exists() else ""
        return page(request, "job.html", job=job, files=render.list_files(folder), report_html=report_html,
                    calls=db.job_calls(job_id), user_states=db.USER_STATES,
                    human_apply=job["source"] in hunt.HUMAN_APPLY_SITES)

    @app.post("/jobs/{job_id}/state")
    def set_state(job_id: int, state: str = Form(...), next: str = Form("")):
        if state not in db.USER_STATES:
            return back(f"/jobs/{job_id}", err="Not a state you can set by hand.")
        db.update_job(job_id, state=state)
        return back(local_path(next, f"/jobs/{job_id}"), msg=f"Marked as {state}.")

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
            if login_browser_open():
                raise RuntimeError("Close the login browser window first.")
            hunt.runner.start("apply", {"job_id": job_id})
        except RuntimeError as e:
            return back(f"/jobs/{job_id}", err=str(e))
        return back("/hunt", msg="Applying - watch progress here.")

    @app.post("/jobs/{job_id}/rebuild")
    def rebuild(job_id: int):
        try:
            hunt.runner.start("rebuild", {"job_id": job_id})
            hunt.runner.wait(120)
        except RuntimeError as e:
            return back(f"/jobs/{job_id}", err=str(e))
        return back(f"/jobs/{job_id}", msg=hunt.runner.last_result)

    @app.post("/jobs/{job_id}/open-folder")
    def open_folder(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return back("/", err="No such job.")
        folder = paths.jobs_dir() / job["folder"]
        if folder.exists() and hasattr(os, "startfile"):
            os.startfile(str(folder))  # opens Explorer on this PC
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
        if target.suffix == ".html":  # show the source as text; it's for editing, not for running here
            return PlainTextResponse(target.read_text(encoding="utf-8"))
        media = {".pdf": "application/pdf", ".md": "text/plain; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        return FileResponse(str(target), media_type=media, content_disposition_type="inline")

    # ── profile ────────────────────────────────────────────────────────
    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        text = profile.load_profile()
        return page(request, "profile.html", contact=profile.load_contact(), profile_text=text or profile.PROFILE_TEMPLATE,
                    has_profile=bool(text.strip()), cvs=[p.name for p in profile.list_cvs()],
                    screening=profile.load_screening(), screening_fields=profile.SCREENING_FIELDS)

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
        target = (paths.cv_dir() / Path(name).name)
        if target.is_file():
            target.unlink()
        return back("/profile", msg="Removed.")

    @app.post("/profile/build")
    def build_profile():
        try:
            hunt.runner.start("profile")
        except RuntimeError as e:
            return back("/profile", err=str(e))
        return back("/hunt", msg="Building your profile - this takes about a minute.")

    # ── scope ──────────────────────────────────────────────────────────
    @app.get("/scope", response_class=HTMLResponse)
    def scope_page(request: Request):
        text = profile.load_scope_text()
        if not text.strip():
            example = paths.APP_DIR / "scope.example.md"
            text = example.read_text(encoding="utf-8") if example.exists() else ""
        return page(request, "scope.html", scope_text=text)

    @app.post("/scope")
    def save_scope(keywords: str = Form(""), location: str = Form(""), avoid_words: str = Form(""), scope_text: str = Form("")):
        settings_mod.update({"scope": {
            "keywords": [k.strip() for k in keywords.splitlines() if k.strip()],
            "location": location.strip(),
            "avoid_words": [w.strip() for w in avoid_words.replace(",", "\n").splitlines() if w.strip()],
        }})
        profile.save_scope_text(scope_text)
        if profile.scope_is_example(scope_text):
            return back("/scope", err="Saved, but this is still the template. Fill it in (or press Draft from my "
                                      "profile) and delete its first line: hunts won't start until you do.")
        return back("/scope", msg="Scope saved.")

    @app.post("/scope/draft")
    def draft_scope():
        if not profile.load_profile().strip():
            return back("/scope", err="Build your Profile first: the scope is drafted from it.")
        try:
            hunt.runner.start("scope")
        except RuntimeError as e:
            return back("/scope", err=str(e))
        return back("/hunt", msg="Drafting your scope - this takes under a minute. Then review it on the Scope page.")

    # ── accounts: AI key, site logins, browser sessions ───────────────
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        s = settings_mod.load()
        keys = {p: secrets_store.has_secret(secrets_store.api_key_name(p)) for p in settings_mod.PROVIDERS}
        logins = []
        for l in s.get("logins", []):
            logins.append({**l, "has_user": secrets_store.has_secret(secrets_store.login_secret_name(l["name"], "username")),
                           "has_pw": secrets_store.has_secret(secrets_store.login_secret_name(l["name"], "password"))})
        return page(request, "accounts.html", keys=keys, providers=settings_mod.PROVIDERS, logins=logins,
                    login_sites=LOGIN_SITES, browser_open=login_browser_open())

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
            return back("/accounts", err=f"Could not reach the provider ({type(e).__name__}).")
        except llm.LLMError as e:
            return back("/accounts", err=str(e))
        if r.status_code == 200:
            extra = ""
            if provider == "openrouter":
                d = r.json().get("data", {})
                if d.get("limit_remaining") is not None:
                    extra = f" Credit left on this key: ${d['limit_remaining']:.2f}."
            return back("/accounts", msg="The key works." + extra)
        return back("/accounts", err=f"The provider rejected the key (HTTP {r.status_code}).")

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
        global _login_proc
        if hunt.runner.busy:
            return back("/accounts", err="Wait for the hunt to finish (or stop it) first.")
        if login_browser_open():
            return back("/accounts", err="The login browser is already open.")
        urls = [LOGIN_SITES[site][1]] if site in LOGIN_SITES else [u for _, u in LOGIN_SITES.values()]
        _login_proc = subprocess.Popen([sys.executable, "-m", "jobhunt.login_browser", *urls], cwd=str(paths.APP_DIR))
        return back("/accounts", msg="A browser window opened. Log in, then close that window.")

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
        return JSONResponse({"open": login_browser_open()})

    # ── hunt ───────────────────────────────────────────────────────────
    @app.get("/hunt", response_class=HTMLResponse)
    def hunt_page(request: Request):
        s = settings_mod.load()
        ready = db.list_jobs("Ready")
        return page(request, "hunt.html", sites=settings_mod.SITES, site_status=SITE_STATUS, ready=ready,
                    events=db.recent_events(150), human_apply=hunt.HUMAN_APPLY_SITES)

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
        settings_mod.update({"hunt": {"jobs": jobs, "sites": sites}})  # the mode is never remembered: Draft each time
        if login_browser_open():
            return back("/hunt", err="Close the login browser window first.")
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
        keys = [k for k in sites.split(",") if k in settings_mod.SITES] or s["hunt"]["sites"]
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
            "hunt": {"max_candidates": int(num("max_candidates", 60, 1, 2000))},
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
            return back("/settings", err=f"Could not download prices ({type(e).__name__}).")
        return back("/settings", msg=f"Downloaded current prices for {n} OpenRouter models.")

    @app.post("/settings/forget-seen")
    def forget_seen():
        db.forget_seen()
        return back("/settings", msg="Forgot every posting seen so far; the next hunt will look at them again.")

    return app
