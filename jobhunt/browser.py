"""The browser: one persistent Chromium profile (keeps your job-site logins)
plus a separate throwaway headless Chromium that only turns HTML into PDFs.

Uses Playwright's sync API, so a BrowserSession must stay on the thread that
created it (the hunt worker thread). An apply that stops for you can leave its
window open where it stopped (BrowserSession.leave_open, below).
"""
import base64
import html as htmlmod
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import paths

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")


class BrowserBusy(Exception):
    """The browser profile is open elsewhere (usually the Accounts login window)."""


# Chromium's own password manager and autofill stay off in JobHunt's profile, so
# usernames and passwords typed into forms are never saved on disk by the browser.
NO_SAVING_PREFS = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"enabled": False, "profile_enabled": False, "credit_card_enabled": False},
}


def prepare_profile(profile_dir: Path) -> None:
    """Merge NO_SAVING_PREFS into the profile's Preferences file before Chromium starts."""
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except (OSError, ValueError):
        prefs = {}

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict):
                dst[k] = merge(dst.get(k) if isinstance(dst.get(k), dict) else {}, v)
            else:
                dst[k] = v
        return dst
    prefs_path.write_text(json.dumps(merge(prefs, NO_SAVING_PREFS)), encoding="utf-8")


# ── windows left open for you ─────────────────────────────────────────
# An apply that stops as Needs you in a visible window leaves that window where it stopped, so you can finish the
# form by hand (BrowserSession.leave_open). JobHunt lets go of it: none of its rules stay on the window, and the
# session carries on in a fresh browser. While such a window still holds the browser profile (where your logins
# live), later browsers start on a stand-in profile with a copy of its cookies and site storage. A window left open
# closes when you close it, or when JobHunt quits (the Playwright driver that started it ends with JobHunt).

STANDIN_PREFIX = "jobhunt-browser-"
_left_open: list[dict] = []  # {"job_id", "profile", "pw", "ctx", "pid", "watch", "state", "own_dir"}
_active_pws: list = []  # the Playwright instances BrowserSessions are using right now
_standins_in_use: set = set()  # stand-in profiles sessions are on right now
_lock = threading.Lock()


def profile_in_use(profile_dir) -> bool:
    """Is a Chromium running on this profile? (On Windows it keeps <profile>/lockfile open while it runs.)"""
    d = Path(profile_dir)
    if sys.platform != "win32":
        return os.path.lexists(d / "SingletonLock")
    try:
        fd = os.open(d / "lockfile", os.O_WRONLY)
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(fd)
    return False


def _watch(pid: int | None):
    """Something that tells whether process `pid` still runs: on Windows a handle, which a reused PID can't fool."""
    if not pid:
        return None
    if sys.platform != "win32":
        return pid
    import ctypes
    return ctypes.windll.kernel32.OpenProcess(0x00100000 | 0x1000, False, pid) or None  # SYNCHRONIZE | QUERY_LIMITED


def _running(watch) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(watch, 0)
            return True
        except OSError:
            return False
    import ctypes
    return ctypes.windll.kernel32.WaitForSingleObject(watch, 0) == 0x102  # WAIT_TIMEOUT: not ended yet


def _unwatch(watch) -> None:
    if watch and sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.CloseHandle(watch)


def _still_open(w: dict) -> bool:
    return _running(w["watch"]) if w["watch"] else profile_in_use(w["profile"])


def _same_dir(a, b) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _driver_pid(pw) -> int | None:
    try:
        return pw._impl_obj._connection._transport._proc.pid  # Playwright's driver process (not public API)
    except AttributeError:
        return None


def _reap_locked() -> None:
    """Forget the windows you have closed; end the Playwright driver behind them once nothing else uses it."""
    dead = [w for w in _left_open if not _still_open(w)]
    for w in dead:
        _left_open.remove(w)
        _unwatch(w["watch"])
    ended = set()
    for w in dead:
        pw = w["pw"]
        if (w["pid"] and w["pid"] not in ended and not any(o["pw"] is pw for o in _left_open)
                and not any(a is pw for a in _active_pws)):
            ended.add(w["pid"])
            try:
                os.kill(w["pid"], signal.SIGTERM)
            except OSError:
                pass
        if w["own_dir"]:
            shutil.rmtree(w["profile"], ignore_errors=True)


def windows_left_open() -> list[dict]:
    """The windows an apply left open that are still open: [{"job_id": ...}]."""
    with _lock:
        _reap_locked()
        return [{"job_id": w["job_id"]} for w in _left_open]


def held_by_left_open(profile_dir) -> dict | None:
    """The window left open that is running on this profile, if one is."""
    with _lock:
        _reap_locked()
        return next((w for w in _left_open if _same_dir(w["profile"], profile_dir)), None)


def clean_standins() -> None:
    """Delete stand-in profiles that an earlier JobHunt left behind."""
    for d in Path(tempfile.gettempdir()).glob(STANDIN_PREFIX + "*"):
        if (d.is_dir() and not profile_in_use(d) and not held_by_left_open(d)
                and not any(_same_dir(d, u) for u in _standins_in_use)):
            shutil.rmtree(d, ignore_errors=True)


# Copies a profile's site storage into a stand-in profile, where the stand-in has nothing of its own yet.
SEED_STORAGE_JS = """(() => { try { const items = (STORAGE)[location.origin]; if (!items) return;
  for (const [k, v] of Object.entries(items)) if (localStorage.getItem(k) === null) localStorage.setItem(k, v);
} catch (e) {} })();"""
SEED_STORAGE_MAX = 2_000_000  # characters: more than any login needs


class BrowserSession:
    def __init__(self, headless: bool = True, profile_dir: Path | None = None, minimized: bool = False):
        self.headless = headless
        self.minimized = minimized  # a window that starts on the taskbar, not over your work
        self.profile_dir = profile_dir or paths.browser_profile_dir()
        self._pw = None
        self._ctx = None
        self._page = None
        self._tabs = {}  # name -> page: a site that keeps its search page open between steps (Indeed)
        self._gone = None  # a profile browser context that has closed
        self._pdf_browser = None
        self._launched_on = None  # the profile the open browser runs on: yours, or a stand-in
        self._standin = None  # this session's stand-in profile, while a window left open holds yours
        self._seed = None  # what the stand-in starts with: the held profile's cookies and site storage
        self.last_url = ""  # where the last load_html ended up (after redirects)
        self.last_status = 0  # its HTTP status (0 = unknown)

    # profile browser ---------------------------------------------------
    def _playwright(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
            with _lock:
                _active_pws.append(self._pw)
        return self._pw

    def _profile_to_launch(self) -> tuple[Path, bool]:
        """Your profile, or a stand-in while a window left open holds yours: (the profile, is a new stand-in)."""
        held = held_by_left_open(self.profile_dir)
        if not held:
            self._drop_standin()  # yours is free again
            return self.profile_dir, False
        if self._standin is not None:
            return self._standin, False
        self._standin, self._seed = Path(tempfile.mkdtemp(prefix=STANDIN_PREFIX)), held["state"] or {}
        with _lock:
            _standins_in_use.add(self._standin)
        return self._standin, True

    def _drop_standin(self, delete: bool = True) -> None:
        if self._standin is not None:
            with _lock:
                _standins_in_use.discard(self._standin)
            if delete:
                shutil.rmtree(self._standin, ignore_errors=True)
            self._standin = self._seed = None

    def _seed_standin(self, ctx, new: bool) -> None:
        """A stand-in starts with your profile's logins: its cookies once, its site storage where it has none."""
        seed = self._seed or {}
        if new and seed.get("cookies"):
            try:
                ctx.add_cookies(seed["cookies"])
            except PlaywrightError:
                pass
        storage = {o["origin"]: {i["name"]: i["value"] for i in o["localStorage"]}
                   for o in seed.get("origins") or [] if o.get("localStorage")}
        script = json.dumps(storage)
        if storage and len(script) <= SEED_STORAGE_MAX:
            ctx.add_init_script(SEED_STORAGE_JS.replace("STORAGE", script))

    def _ensure(self):
        self._playwright()
        if self._ctx is None:
            profile, new_standin = self._profile_to_launch()
            try:
                prepare_profile(profile)
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(profile), headless=self.headless, user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900}, accept_downloads=False,
                    service_workers="block",  # so the apply agent's network rules see every request
                    args=["--disable-blink-features=AutomationControlled",
                          *(["--start-minimized"] if self.minimized and not self.headless else [])],
                )
            except PlaywrightError as e:
                if "user data directory is already in use" in str(e).lower() or "lock" in str(e).lower():
                    raise BrowserBusy("The job-site browser is already open (close the login window first).") from None
                raise
            self._launched_on = profile
            self._ctx.on("close", self._on_close)
            self._ctx.set_default_timeout(15000)
            if profile != self.profile_dir:
                self._seed_standin(self._ctx, new_standin)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _on_close(self, ctx) -> None:
        self._gone = ctx  # e.g. you closed its window. Not reopened by itself (an apply must not carry on in a
        # fresh window you didn't ask for): reset() or set_headless() starts the next one.

    @property
    def page(self):
        self._ensure()
        if self._page.is_closed():
            self._page = self._ctx.new_page()
        return self._page

    def set_headless(self, headless: bool, minimized: bool = False) -> None:
        """Switch window mode; also replaces a browser whose window you closed, so the next use gets a fresh one."""
        if (headless, minimized) != (self.headless, self.minimized):
            self.headless, self.minimized = headless, minimized
            self.reset()
        elif self.gone():
            self.reset()

    def reset(self) -> None:
        """Close the profile browser (if it isn't closed already: you may have closed its window yourself),
        so the next page use starts a fresh one."""
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        self._ctx = self._page = self._launched_on = None
        self._tabs = {}

    def leave_open(self, job_id: int | None = None) -> bool:
        """Let go of the browser with its window as it is, for you to finish what is on screen yourself. The
        session's next page use starts a fresh browser. False (nothing changes) if there's no browser to leave,
        e.g. you closed its window."""
        if self._ctx is None or self.gone():
            return False
        ctx, profile = self._ctx, self._launched_on
        try:
            ctx.unroute_all(behavior="ignoreErrors")  # no request in that window waits for JobHunt to answer it
            for t in self._tabs.values():  # the hunt's own tabs (Indeed's search page) aren't part of it
                if not t.is_closed():
                    t.close()
            if self._page is not None and not self._page.is_closed():
                self._page.bring_to_front()
            # while it holds your profile, later browsers start with a copy of its logins
            state = ctx.storage_state() if _same_dir(profile, self.profile_dir) else None
        except PlaywrightError:
            return False
        try:  # its browser process: when that has ended, you have closed the window
            cdp = ctx.browser.new_browser_cdp_session()
            pid = next((p["id"] for p in cdp.send("SystemInfo.getProcessInfo")["processInfo"] if p["type"] == "browser"),
                       None)
            cdp.detach()
        except (PlaywrightError, AttributeError, KeyError, TypeError):
            pid = None  # then the profile's lock file tells
        if self._pdf_browser is not None:  # it would stay behind with the window's Playwright
            try:
                self._pdf_browser.close()
            except Exception:
                pass
            self._pdf_browser = None
        with _lock:
            _left_open.append({"job_id": job_id, "profile": profile, "pw": self._pw, "ctx": ctx,
                               "pid": _driver_pid(self._pw), "watch": _watch(pid), "state": state,
                               "own_dir": not _same_dir(profile, self.profile_dir)})
        if self._standin is not None and _same_dir(profile, self._standin):
            self._drop_standin(delete=False)  # the window has that stand-in now: it goes when the window does
        self._ctx = self._page = self._launched_on = None
        self._tabs = {}
        return True

    def gone(self) -> bool:
        """The profile browser was started and has since closed (e.g. you closed its window)."""
        if self._ctx is None:
            return False
        if self._ctx is self._gone:
            return True
        try:
            self._ctx.cookies()  # a cheap round trip: fails once the browser has gone
            return False
        except Exception:
            return True

    def is_closed_error(self, e: BaseException) -> bool:
        """Did `e` happen because the browser window was closed under us? (A site closing one of its own
        popups raises "Target page ... has been closed" too, but leaves the browser running: not this.)"""
        return isinstance(e, PlaywrightError) and self.gone()

    def tab(self, name: str | None = None):
        """The shared page, or a named tab of its own that other sites' page loads don't move."""
        if not name:
            return self.page
        self._ensure()
        if name not in self._tabs or self._tabs[name].is_closed():
            self._tabs[name] = self._ctx.new_page()
        return self._tabs[name]

    def load_html(self, url: str, tab: str | None = None) -> str:
        page = self.tab(tab)
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightError:
            pass  # busy pages never go idle; what we have is enough
        self.last_url = page.url
        self.last_status = response.status if response else 0
        return page.content()

    def click_for_response(self, selector: str, wanted: list, tab: str | None = None, timeout: int = 20000) -> str:
        """Click something on the open page, as a visitor would, and return what the page fetched
        because of it: the first response matching one of `wanted` (a string, or a tuple of strings,
        that must all appear in the request's address or the data it sent). No response in time: the
        page itself, so the caller can see a block page. Sets last_status (0 = no response)."""
        page = self.tab(tab)
        target = page.locator(selector).first
        if not target.count():
            raise LookupError(f"nothing to click: {selector}")
        target.scroll_into_view_if_needed(timeout=5000)

        def matches(r):
            sent = f"{r.url}\n{r.request.post_data or ''}"
            return any(all(part in sent for part in ((w,) if isinstance(w, str) else w)) for w in wanted)
        try:
            with page.expect_response(matches, timeout=timeout) as info:
                target.click()
            response = info.value
            self.last_url, self.last_status = response.url, response.status
            return response.text()
        except PlaywrightError:
            self.last_url, self.last_status = page.url, 0
            return page.content()

    def load_isolated_html(self, url: str, timeout: int = 25000, allow=None) -> str:
        """A page that isn't a job board (a company's own website), in a throwaway headless browser: none of
        your job-site logins, no downloads. Sets last_url and last_status like load_html.
        `allow(url) -> bool`: every request the page makes, and every redirect, must pass it (so a website
        can't send JobHunt to this PC or the home network); anything else raises PermissionError."""
        if self._pdf_browser is None:
            self._pdf_browser = self._playwright().chromium.launch(headless=True)
        ctx = self._pdf_browser.new_context(user_agent=USER_AGENT, accept_downloads=False, service_workers="block",
                                            viewport={"width": 1280, "height": 900})
        refused = []
        if allow is not None:
            checked = {}

            def ok(u: str) -> bool:
                p = urlparse(u)
                if p.scheme in ("data", "blob", "about"):
                    return True
                key = f"{p.scheme}://{p.netloc}"
                if key not in checked:
                    checked[key] = p.scheme in ("http", "https") and bool(allow(u))
                return checked[key]

            def guard(route):
                req = route.request
                if req.resource_type in ("image", "media", "font", "stylesheet"):  # only the words are read
                    return route.abort()
                if not ok(req.url):
                    refused.append(req.url)
                    return route.abort()
                # A route sees only the first address of a redirect chain, so redirects are followed here,
                # one hop at a time, each checked before it is asked for.
                try:
                    resp = route.fetch(max_redirects=0, timeout=timeout)
                except PlaywrightError:
                    return route.abort()
                where = resp.headers.get("location")
                if 300 <= resp.status < 400 and where:
                    target = urljoin(req.url, where)
                    if not ok(target):
                        refused.append(target)
                        return route.abort()
                return route.fulfill(response=resp)
            ctx.route("**/*", guard)
        try:
            page = ctx.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except PlaywrightError:
                if refused:
                    raise PermissionError(f"it sent JobHunt to {refused[0][:80]}") from None
                raise
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightError:
                pass
            self.last_url = page.url
            self.last_status = response.status if response else 0
            if allow is not None and not ok(page.url):
                raise PermissionError(f"it sent JobHunt to {page.url[:80]}")
            return page.content()
        finally:
            ctx.close()

    # PDFs ----------------------------------------------------------------
    def render_pdf(self, html: str, out_path: Path) -> Path:
        if self._pdf_browser is None:
            self._pdf_browser = self._playwright().chromium.launch(headless=True)
        page = self._pdf_browser.new_page()
        try:
            page.set_content(html, wait_until="load")
            page.pdf(path=str(out_path), format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            page.close()
        return out_path

    def page_to_pdf(self, out_path: Path, heading: str) -> Path:
        """Proof of what the browser shows right now (works headed or headless)."""
        from .render import link_label, web_url
        page = self.page
        png = page.screenshot(full_page=True)
        url = web_url(page.url)
        link = (f"<a href='{htmlmod.escape(url, quote=True)}'>{htmlmod.escape(link_label(url))}</a>"
                if url != "#" else htmlmod.escape(page.url))  # a real link, so a click opens the whole URL
        doc = (f"<html><head><meta charset='utf-8'><style>body{{font-family:Arial;margin:0}}"
               f"header{{padding:10px 14px;font-size:11px;color:#333;border-bottom:1px solid #ccc}}"
               f"img{{width:100%}}</style></head><body><header><b>{htmlmod.escape(heading)}</b><br>"
               f"{link}<br>Captured {datetime.now():%Y-%m-%d %H:%M}</header>"
               f"<img src='data:image/png;base64,{base64.b64encode(png).decode()}'></body></html>")
        return self.render_pdf(doc, out_path)

    def close(self) -> None:
        for thing in (self._ctx, self._pdf_browser):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass
        self._ctx = self._page = self._pdf_browser = self._launched_on = None
        self._tabs = {}
        self._drop_standin()
        if self._pw is not None:
            pw, self._pw = self._pw, None
            with _lock:
                _active_pws[:] = [a for a in _active_pws if a is not pw]
                _reap_locked()
                kept = any(w["pw"] is pw for w in _left_open)  # a window left open still runs on it
            if not kept:
                try:
                    pw.stop()
                except Exception:
                    pass


def render_pdfs(pairs: list[tuple[str, Path]]) -> None:
    """Render HTML strings to PDF files without touching the profile browser."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for html, out in pairs:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
                page.close()
        finally:
            browser.close()
