"""Indeed: search from the page's own card data, descriptions by clicking a card (no network).

indeed_search_live.html and indeed_viewjob.json are cut from real sg.indeed.com responses (2026-10-15,
logged out); a recruiter's email address in the description was replaced.
"""
import copy
import json
from pathlib import Path

import pytest

from jobhunt import hunt, sites
from jobhunt.extract import Listing

FIX = Path(__file__).resolve().parent / "fixtures"
SEARCH = (FIX / "indeed_search_live.html").read_text(encoding="utf-8")
VIEWJOB = json.loads((FIX / "indeed_viewjob.json").read_text(encoding="utf-8"))
TRAP = "789abcdef0123456"  # the hidden trap card on that page


class IndeedCtx:
    """Stands in for SiteContext: serves the saved search page and the job a card click fetches."""

    def __init__(self, page=SEARCH, job=VIEWJOB, blocked=False):
        self.page, self.job, self.blocked = page, job, blocked
        self.loads, self.clicks = [], []

    def get_html(self, url, tab=None):
        self.loads.append((url, tab))
        if self.blocked:
            raise sites.SiteBlocked("block page: 'just a moment...'")
        return self.page

    def click_json(self, selector, wanted, tab=None):
        self.clicks.append((selector, wanted, tab))
        return self.job


def test_search_reads_the_card_data_and_skips_the_trap_card():
    listings = sites.IndeedSite().search(IndeedCtx(), "data analyst", "Singapore", 0)
    assert [l.url for l in listings] == ["https://sg.indeed.com/viewjob?jk=ad5444e9460a3553",
                                         "https://sg.indeed.com/viewjob?jk=c716dea90f0a01f4",
                                         "https://sg.indeed.com/viewjob?jk=889d3ac98e00e706"]
    assert TRAP in SEARCH and not any(TRAP in l.url for l in listings)
    first, second = listings[0], listings[1]
    assert (first.title, first.company, first.location) == ("Data Analyst", "YipitData (Alternative)", "Remote")
    assert first.salary == "$72,000 - $96,000 a year" and first.salary_max_monthly == 8000
    assert second.salary_max_monthly == 6000


def test_without_card_data_the_hidden_trap_card_is_still_skipped():
    page = SEARCH.replace('window.mosaic.providerData["mosaic-provider-jobcards"]', "window.somethingElse")
    listings = sites.IndeedSite().parse_search(page)
    assert len(listings) == 3
    assert not any(TRAP in l.url for l in listings)
    assert listings[1].company == "CORVANTA TRADING PTE. LTD."


@pytest.mark.parametrize("jk", ["abcdef0123456789", "789abcdef0123456", "a1b2c3d4e5f67890", "0123456789abcdef",
                                "ad5444e9460a355", "AD5444E9460A3553", "x' or '1", ""])
def test_trap_and_malformed_job_keys_are_refused(jk):
    assert sites.indeed_job_key(jk) == ""


def test_real_job_keys_pass():
    assert sites.indeed_job_key("ad5444e9460a3553") == "ad5444e9460a3553"


def test_search_loads_newest_first_in_its_own_tab():
    ctx = IndeedCtx()
    sites.IndeedSite().search(ctx, "data analyst", "Singapore", 0)
    url, tab = ctx.loads[0]
    assert url == "https://sg.indeed.com/jobs?q=data+analyst&l=Singapore&sort=date" and tab == "indeed"


def test_fetch_clicks_the_card_and_reads_the_job_it_loads():
    ctx = IndeedCtx()
    listing = Listing("indeed", "https://sg.indeed.com/viewjob?jk=5a42582d6016c4d2", "fallback", "fallback co", "SG")
    p = sites.IndeedSite().fetch(ctx, listing)
    assert ctx.clicks == [("a[data-jk='5a42582d6016c4d2']",
                           ["/viewjob?jk=5a42582d6016c4d2", ('"operationName":"Viewjob"', "5a42582d6016c4d2")], "indeed")]
    assert ctx.loads == []  # no page load: the open search page fetches it
    assert p.title == "Software Engineer (Fresh Graduate)"
    assert p.company == "BLACKSCORE PTE. LTD." and p.location == "River Valley"
    assert p.text.startswith("At BlackScore, we build AI-native") and len(p.text) > 1500
    assert "<br>" not in p.text and p.text.endswith("Job type: Full-time")
    assert p.salary == "$4,500 - $6,000 a month" and p.salary_max_monthly == 6000
    assert p.apply_url == listing.url


def test_the_other_version_of_the_page_graphql_is_read_too():
    # some browser profiles get a page that asks apis.indeed.com/graphql for the job instead
    job = json.loads((FIX / "indeed_viewjob_graphql.json").read_text(encoding="utf-8"))
    listing = Listing("indeed", "https://sg.indeed.com/viewjob?jk=172c202acb267581", "fallback", "fallback co", "SG")
    p = sites.IndeedSite().fetch(IndeedCtx(job=job), listing)
    assert p.title == "Fund Accounting & Administration, AVP"
    assert p.company == "STATE STREET FUND SERVICES (SINGAPORE) PTE. LIMITED" and p.location == "Outram"
    assert p.text.startswith("Why this role is important to us") and len(p.text) > 1500
    assert "<br>" not in p.text and p.text.endswith("Job type: Full-time")
    assert p.salary == "$9,000 - $13,000 a month" and p.salary_max_monthly == 13000
    other = Listing("indeed", "https://sg.indeed.com/viewjob?jk=ad5444e9460a3553", "t", "c", "")
    with pytest.raises(sites.PageUnavailable):
        sites.IndeedSite().posting_from_job(job, other)


def test_fetch_refuses_a_link_that_is_not_an_indeed_job():
    listing = Listing("indeed", f"https://sg.indeed.com/viewjob?jk={TRAP}", "t", "c", "")
    with pytest.raises(sites.PageUnavailable):
        sites.IndeedSite().fetch(IndeedCtx(), listing)


def test_a_card_no_longer_on_the_page_skips_just_that_job():
    class Ctx(IndeedCtx):
        def click_json(self, selector, wanted, tab=None):
            raise LookupError("nothing to click")
    listing = Listing("indeed", "https://sg.indeed.com/viewjob?jk=5a42582d6016c4d2", "t", "c", "")
    with pytest.raises(sites.PageUnavailable):
        sites.IndeedSite().fetch(Ctx(), listing)


def test_logged_out_one_page_per_keyword_logged_in_more():
    site = sites.IndeedSite()
    site.search(IndeedCtx(), "data analyst", "", 0)
    assert site.page_limit == 1  # page 2 would be Indeed's sign-in page
    ctx = IndeedCtx(page=SEARCH.replace('"loggedIn": false', '"loggedIn": true'))
    site.search(ctx, "data analyst", "", 0)
    assert site.page_limit > 1
    site.search(ctx, "data analyst", "", 1)
    assert ctx.loads[-1][0].endswith("&sort=date&start=10")


def test_the_human_check_tells_you_what_to_do():
    with pytest.raises(sites.SiteBlocked, match="Accounts page.*Verify you are human"):
        sites.IndeedSite().search(IndeedCtx(blocked=True), "data analyst", "", 0)


def test_indeed_block_page_titles_are_recognised():
    for title in ("Security Check - Indeed.com", "Just a moment...", "Additional Verification Required"):
        with pytest.raises(sites.SiteBlocked):
            sites.check_blocked(f"<html><head><title>{title}</title></head><body>x</body></html>", "https://sg.indeed.com/jobs")
    sites.check_blocked("<title>Security Checker jobs in Singapore | JobStreet</title>", "https://sg.jobstreet.com/x")


# ── SiteContext.click_json ────────────────────────────────────────────

class _ClickBrowser:
    def __init__(self, text, status, url="https://sg.indeed.com/viewjob?jk=5a42582d6016c4d2"):
        self.text, self.status, self.url = text, status, url
        self.calls = []

    def click_for_response(self, selector, wanted, tab=None):
        self.calls.append((selector, wanted, tab))
        self.last_status, self.last_url = self.status, self.url
        return self.text


def _ctx(browser):
    return sites.SiteContext(browser, {"min_delay_s": 0, "max_delay_s": 0})


def test_click_json_returns_the_data():
    b = _ClickBrowser(json.dumps(VIEWJOB), 200)
    assert _ctx(b).click_json("a#x", ["/viewjob?jk="], tab="indeed")["body"]["jobKey"] == "5a42582d6016c4d2"
    assert b.calls == [("a#x", ["/viewjob?jk="], "indeed")]


@pytest.mark.parametrize("text, status, error", [
    ("<html><head><title>Just a moment...</title></head><body></body></html>", 0, sites.SiteBlocked),
    ("<html><head><title>Data Analyst Jobs | Indeed</title></head><body>cards</body></html>", 0, sites.PageUnavailable),
    ('{"status": "error"}', 403, sites.SiteBlocked),
    ("Not found", 404, sites.PageUnavailable),
    ("not json", 200, sites.PageUnavailable),
])
def test_click_json_failures(text, status, error):
    with pytest.raises(error):
        _ctx(_ClickBrowser(text, status)).click_json("a#x", ["/viewjob?jk="])


def test_click_json_sign_in_redirect_is_a_wall():
    b = _ClickBrowser("<html><head><title>Sign In | Indeed Accounts</title></head></html>", 200,
                      url="https://secure.indeed.com/auth?continue=x")
    with pytest.raises(sites.SiteBlocked):
        _ctx(b).click_json("a#x", ["/viewjob?jk="])


def test_posting_from_unexpected_data_is_unavailable():
    with pytest.raises(sites.PageUnavailable):
        sites.IndeedSite().posting_from_job({"status": "success"}, Listing("indeed", "u", "t", "c", ""))
    other_job = Listing("indeed", "https://sg.indeed.com/viewjob?jk=ad5444e9460a3553", "t", "c", "")
    with pytest.raises(sites.PageUnavailable):  # another card's job must never be read as this one
        sites.IndeedSite().posting_from_job(VIEWJOB, other_job)


# ── in the hunt ───────────────────────────────────────────────────────

class _Task:
    settings: dict = {}

    def __init__(self):
        self.lines = []

    def log(self, message, level="info"):
        self.lines.append((level, message))

    def check_stop(self):
        pass


def test_hunt_reads_page_one_of_each_keyword_when_logged_out(monkeypatch):
    site = sites.IndeedSite()
    monkeypatch.setattr(hunt, "get_site", lambda key, s: site)
    task, blocked, ctx = _Task(), {}, IndeedCtx()
    got = list(hunt.candidates(task, ctx, ["indeed"], ["data analyst", "accountant"], "Singapore", blocked=blocked))
    assert len(got) == 6 and blocked == {}
    assert [u for u, _ in ctx.loads] == ["https://sg.indeed.com/jobs?q=data+analyst&l=Singapore&sort=date",
                                         "https://sg.indeed.com/jobs?q=accountant&l=Singapore&sort=date"]


class _NoBrowser:
    def close(self):
        pass


@pytest.mark.parametrize("sites_ticked, headless", [(["indeed", "jobstreet"], False), (["jobstreet"], True)])
def test_with_indeed_ticked_the_hunt_browser_is_a_window(sites_ticked, headless, monkeypatch):
    made = []
    monkeypatch.setattr(hunt, "make_browser", lambda headless=True: made.append(headless) or _NoBrowser())
    monkeypatch.setattr(hunt, "candidates", lambda *a, **k: iter(()))
    monkeypatch.setattr(hunt, "readiness", lambda s: [])
    monkeypatch.setattr(hunt.profile, "load_scope_text", lambda: "")
    monkeypatch.setattr(hunt.profile, "load_screening", lambda: {"expected_salary": ""})
    monkeypatch.setattr(hunt.profile, "salary_floor", lambda: 0)
    monkeypatch.setattr(hunt.profile, "load_profile", lambda: "")
    monkeypatch.setattr(hunt.tuning, "dislikes_text", lambda: "")

    class Task(_Task):
        params = {"jobs": 1, "sites": sites_ticked}
        settings = {**copy.deepcopy(hunt.settings_mod.DEFAULTS), "hunt": {"jobs": 1, "sites": sites_ticked},
                    "scope": {"keywords": ["kw"]}}
        stop_event = None

        def get_llm(self):
            return None
    hunt.run_hunt(Task())
    assert made == [headless]


def test_a_hunt_window_starts_minimized_and_the_apply_window_does_not():
    from jobhunt.browser import BrowserSession
    assert hunt.make_browser(headless=False).minimized
    assert not hunt.make_browser(headless=True).minimized
    b = BrowserSession(headless=False, minimized=True)
    b.set_headless(False)  # the apply step shows its window normally
    assert (b.headless, b.minimized) == (False, False)


def test_hunt_drops_indeed_with_advice_when_it_shows_the_human_check(monkeypatch):
    site = sites.IndeedSite()
    monkeypatch.setattr(hunt, "get_site", lambda key, s: site)
    task, blocked = _Task(), {}
    assert list(hunt.candidates(task, IndeedCtx(blocked=True), ["indeed"], ["kw"], "", blocked=blocked)) == []
    assert "indeed" in blocked
    assert any(level == "warn" and "Accounts page" in m for level, m in task.lines)


# ── the browser's own tab and clicks (real Chromium, local pages) ─────

def test_a_site_tab_keeps_its_page_and_a_click_returns_what_it_fetched(demo_server, data_dir):
    from jobhunt.browser import BrowserSession
    b = BrowserSession(headless=True)
    try:
        b.load_html(f"{demo_server}/demo", tab="indeed")
        b.load_html(f"{demo_server}/job/1")  # another site, on the shared page
        assert b.tab("indeed").url == f"{demo_server}/demo"
        b.tab("indeed").set_content(f"<a id='go' href='{demo_server}/job/1'>Job</a>"
                                    "<a id='hidden' style='display:none' href='/x'>trap</a>")
        text = b.click_for_response("#go", ["/job/1"], tab="indeed")
        assert b.last_status == 200 and "<" in text
        with pytest.raises(LookupError):
            b.click_for_response("#missing", ["/job/1"], tab="indeed")
        # matched on what the request sent, not just its address (Indeed's graphql)
        b.load_html(f"{demo_server}/demo", tab="indeed")
        b.tab("indeed").set_content("""<button id='b' onclick="
            fetch('job/1?first', {method: 'POST', body: '{&quot;operationName&quot;:&quot;Save&quot;,&quot;input&quot;:&quot;k1&quot;}'})
              .then(() => fetch('job/1?second', {method: 'POST', body: '{&quot;operationName&quot;:&quot;Viewjob&quot;,&quot;input&quot;:&quot;k1&quot;}'}))
            ">Go</button>""")
        b.click_for_response("#b", [('"operationName":"Viewjob"', "k1")], tab="indeed")
        assert b.last_url.endswith("/job/1?second")
    finally:
        b.close()


@pytest.mark.parametrize("source, shown", [("indeed", True), ("jobstreet", False)])
def test_applying_on_indeed_uses_a_window_even_with_show_browser_off(source, shown, data_dir, monkeypatch):
    from jobhunt import db, settings
    from jobhunt.applyagent import ApplyResult
    settings.update({"apply": {"show_browser": False}})
    jid = db.add_job(source=source, url=f"https://{source}.example/1", company="A", role="R", state="Ready")
    seen = []

    class Browser:
        headless, minimized = True, False

        def set_headless(self, headless, minimized=False):
            self.headless, self.minimized = headless, minimized

    class Agent:
        def __init__(self, browser, **kw):
            self.browser = browser

        def run(self, url):
            seen.append(not self.browser.headless)
            return ApplyResult(status="stopped", summary="stopped")
    monkeypatch.setattr(hunt, "ApplyAgent", Agent)
    task = hunt.Task("apply", {"job_id": jid}, __import__("threading").Event())
    monkeypatch.setattr(task, "get_llm", lambda: None)
    browser = Browser()
    hunt.apply_job(task, browser, jid)
    assert seen == [shown]
    assert (browser.headless, browser.minimized) == (True, False)  # back as it was
