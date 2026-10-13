"""The browser: one persistent Chromium profile (keeps your job-site logins)
plus a separate throwaway headless Chromium that only turns HTML into PDFs.

Uses Playwright's sync API, so a BrowserSession must stay on the thread that
created it (the hunt worker thread).
"""
import base64
import html as htmlmod
import json
from datetime import datetime
from pathlib import Path

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


class BrowserSession:
    def __init__(self, headless: bool = True, profile_dir: Path | None = None):
        self.headless = headless
        self.profile_dir = profile_dir or paths.browser_profile_dir()
        self._pw = None
        self._ctx = None
        self._page = None
        self._pdf_browser = None
        self.last_url = ""  # where the last load_html ended up (after redirects)
        self.last_status = 0  # its HTTP status (0 = unknown)

    # profile browser ---------------------------------------------------
    def _ensure(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._ctx is None:
            try:
                prepare_profile(self.profile_dir)
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless, user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900}, accept_downloads=False,
                    service_workers="block",  # so the apply agent's network rules see every request
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except PlaywrightError as e:
                if "user data directory is already in use" in str(e).lower() or "lock" in str(e).lower():
                    raise BrowserBusy("The job-site browser is already open (close the login window first).") from None
                raise
            self._ctx.set_default_timeout(15000)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    @property
    def page(self):
        self._ensure()
        if self._page.is_closed():
            self._page = self._ctx.new_page()
        return self._page

    def set_headless(self, headless: bool) -> None:
        if headless != self.headless:
            self.headless = headless
            if self._ctx is not None:
                self._ctx.close()
                self._ctx = None
                self._page = None

    def load_html(self, url: str) -> str:
        page = self.page
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightError:
            pass  # busy pages never go idle; what we have is enough
        self.last_url = page.url
        self.last_status = response.status if response else 0
        return page.content()

    # PDFs ----------------------------------------------------------------
    def render_pdf(self, html: str, out_path: Path) -> Path:
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._pdf_browser is None:
            self._pdf_browser = self._pw.chromium.launch(headless=True)
        page = self._pdf_browser.new_page()
        try:
            page.set_content(html, wait_until="load")
            page.pdf(path=str(out_path), format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            page.close()
        return out_path

    def page_to_pdf(self, out_path: Path, heading: str) -> Path:
        """Proof of what the browser shows right now (works headed or headless)."""
        page = self.page
        png = page.screenshot(full_page=True)
        doc = (f"<html><head><meta charset='utf-8'><style>body{{font-family:Arial;margin:0}}"
               f"header{{padding:10px 14px;font-size:11px;color:#333;border-bottom:1px solid #ccc}}"
               f"img{{width:100%}}</style></head><body><header><b>{htmlmod.escape(heading)}</b><br>"
               f"{htmlmod.escape(page.url)}<br>Captured {datetime.now():%Y-%m-%d %H:%M}</header>"
               f"<img src='data:image/png;base64,{base64.b64encode(png).decode()}'></body></html>")
        return self.render_pdf(doc, out_path)

    def close(self) -> None:
        for thing in (self._ctx, self._pdf_browser):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass
        self._ctx = self._page = self._pdf_browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None


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
