"""Opens the job-site browser (with a window) so you can log in yourself.

Run by the Accounts page as a separate process:  python -m jobhunt.login_browser <url> [<url> ...]
Log in (Singpass, 2FA, captchas all fine - it's you doing it), then close the
window. The session is saved in data/browser-profile and reused by hunts.
"""
import sys

from playwright.sync_api import sync_playwright

from . import paths
from .browser import USER_AGENT, prepare_profile


def main(urls: list[str]) -> None:
    prepare_profile(paths.browser_profile_dir())
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(paths.browser_profile_dir()), headless=False, user_agent=USER_AGENT, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(urls[0] if urls else "about:blank")
        for url in urls[1:]:
            ctx.new_page().goto(url)
        # wait until every tab is closed (closing the window closes them all)
        while ctx.pages:
            try:
                ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                break
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])
