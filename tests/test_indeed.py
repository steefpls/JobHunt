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


# ── where the Apply button goes ───────────────────────────────────────
# indeed_viewjob_apply_live.json: three jobs cut from real embedded viewjob responses (2026-10-16, logged
# out): one whose Apply button goes to the company's site, one with Indeed's own Easy Apply, and one Indeed
# got from MyCareersFuture. mcf_search_indeed_job_live.json is MyCareersFuture's public search answering
# for that last job's title and company. /applystart?jk=<key> was seen to 302 to the company's page.

APPLY_JOBS = json.loads((FIX / "indeed_viewjob_apply_live.json").read_text(encoding="utf-8"))
MCF_FOUND = json.loads((FIX / "mcf_search_indeed_job_live.json").read_text(encoding="utf-8"))
MCF_PAGE = ("https://www.mycareersfuture.gov.sg/job/others/"
            "data-developer-bcw-dlt-technologies-d4bd2d166c14cb46dc20d1c94ae2cf40")


class ApplyCtx(IndeedCtx):
    """IndeedCtx that also answers MyCareersFuture's search (or fails it with `mcf_error`)."""

    def __init__(self, job, mcf=MCF_FOUND, mcf_error=None):
        super().__init__(job=job)
        self.mcf, self.mcf_error, self.json_calls = mcf, mcf_error, []

    def get_json(self, url, method="GET", body=None, pace=True):
        self.json_calls.append((url, method, body, pace))
        if self.mcf_error:
            raise self.mcf_error
        return self.mcf


def apply_listing(kind, **extra):
    jk = APPLY_JOBS[kind]["body"]["jobKey"]
    return Listing("indeed", f"https://sg.indeed.com/viewjob?jk={jk}", "t", "c", "SG", extra=extra)


def test_a_company_site_job_applies_through_indeeds_redirect_and_keeps_its_posting():
    ctx = ApplyCtx(APPLY_JOBS["company_site"])
    p = sites.IndeedSite().fetch(ctx, apply_listing("company_site"))
    assert p.title == "Director of Product" and p.company == "Electronic Arts"
    assert p.url == "https://sg.indeed.com/viewjob?jk=7451975960105ec7"  # Open posting: where it was found
    # the button's own link carries tracking for one visit; the bare key goes straight to jobs.ea.com
    assert p.apply_url == "https://sg.indeed.com/applystart?jk=7451975960105ec7"
    assert ctx.json_calls == [] and ctx.loads == []  # nothing else fetched
    assert sites.apply_site("indeed", p.apply_url) == "indeed"


@pytest.mark.parametrize("card_says", [True, False])
def test_an_easy_apply_job_keeps_the_indeed_link(card_says):
    p = sites.IndeedSite().fetch(ApplyCtx(APPLY_JOBS["easy_apply"]), apply_listing("easy_apply", easy_apply=card_says))
    assert p.apply_url == p.url == "https://sg.indeed.com/viewjob?jk=ffdf80ff67885c9b"  # the job's own word wins


def test_a_job_from_mycareersfuture_applies_on_its_mycareersfuture_page():
    ctx = ApplyCtx(APPLY_JOBS["mycareersfuture"])
    p = sites.IndeedSite().fetch(ctx, apply_listing("mycareersfuture", easy_apply=False))
    assert p.url == "https://sg.indeed.com/viewjob?jk=0df04dc908d6a540"
    assert p.apply_url == MCF_PAGE
    # one quick call to MyCareersFuture's own API: no page-load pause, Indeed isn't asked for anything more
    assert ctx.json_calls == [("https://api.mycareersfuture.gov.sg/v2/search?limit=20&page=0", "POST",
                               {"search": "Data Developer BCW DLT TECHNOLOGIES PTE. LTD."}, False)]
    assert ctx.loads == [] and len(ctx.clicks) == 1
    assert sites.apply_site("indeed", p.apply_url) == "mycareersfuture"


@pytest.mark.parametrize("mcf, error", [
    (None, sites.PageUnavailable("HTTP 500")),
    ({"results": []}, None),
    ({"results": [dict(MCF_FOUND["results"][0], title="Data Engineer")]}, None),  # not the same job
    ({"results": [MCF_FOUND["results"][0], dict(MCF_FOUND["results"][0], uuid="0" * 32,
                                                metadata={"jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/x"})]},
     None),  # two jobs match: can't tell which
    ({"results": [dict(MCF_FOUND["results"][0], metadata={"jobDetailsUrl": "https://elsewhere.example/job"})]},
     None),  # a link off MyCareersFuture is never taken for it
    ({"results": "not a list"}, None),
])
def test_a_mycareersfuture_job_not_found_there_opens_its_search_and_still_needs_you(mcf, error):
    p = sites.IndeedSite().fetch(ApplyCtx(APPLY_JOBS["mycareersfuture"], mcf=mcf, mcf_error=error),
                                 apply_listing("mycareersfuture"))
    assert p.apply_url == ("https://www.mycareersfuture.gov.sg/search?search="
                           "Data%20Developer%20BCW%20DLT%20TECHNOLOGIES%20PTE.%20LTD.&sortBy=relevancy&page=0")
    assert sites.apply_site("indeed", p.apply_url) == "mycareersfuture"


def test_after_mycareersfuture_fails_once_the_hunt_stops_asking_it():
    site = sites.IndeedSite()
    ctx = ApplyCtx(APPLY_JOBS["mycareersfuture"], mcf_error=sites.SiteBlocked("HTTP 429"))
    for _ in range(2):
        assert site.fetch(ctx, apply_listing("mycareersfuture")).apply_url.startswith(sites.MCF_SEARCH)
    assert len(ctx.json_calls) == 1 and site.mcf_down


def test_the_agency_name_also_finds_the_mycareersfuture_job():
    hit = dict(MCF_FOUND["results"][0], hiringCompany={"name": "Client Co"})
    assert sites.mcf_job_url(ApplyCtx(None, mcf={"results": [hit]}), "Data Developer",
                             "bcw dlt technologies pte. ltd.") == MCF_PAGE


def test_search_cards_say_whether_indeed_takes_the_application_itself():
    listings = sites.IndeedSite().parse_search(SEARCH)
    assert [l.extra for l in listings] == [{"easy_apply": False}] * 3


@pytest.mark.parametrize("extra, apply_url", [
    ({"easy_apply": False}, "https://sg.indeed.com/applystart?jk=172c202acb267581"),
    ({"easy_apply": True}, "https://sg.indeed.com/viewjob?jk=172c202acb267581"),
    ({}, "https://sg.indeed.com/viewjob?jk=172c202acb267581"),  # not known: the posting
])
def test_the_graphql_version_goes_by_what_the_search_card_said(extra, apply_url):
    job = json.loads((FIX / "indeed_viewjob_graphql.json").read_text(encoding="utf-8"))
    listing = Listing("indeed", "https://sg.indeed.com/viewjob?jk=172c202acb267581", "t", "c", "", extra=extra)
    assert sites.IndeedSite().posting_from_job(job, listing).apply_url == apply_url


@pytest.mark.parametrize("href, apply_url", [
    ("https://careers.acme.example/jobs/7", "https://careers.acme.example/jobs/7"),  # the link itself
    ("/rc/clk?jk=7451975960105ec7&url=https%3A%2F%2Fcareers.acme.example%2Fjobs%2F7",
     "https://careers.acme.example/jobs/7"),  # a redirect that names where it goes
    ("/rc/clk?jk=7451975960105ec7&from=vj", "https://sg.indeed.com/applystart?jk=7451975960105ec7"),
    ("/applystart?jk=7451975960105ec7&ref=https%3A%2F%2Fwww.google.com",
     "https://sg.indeed.com/applystart?jk=7451975960105ec7"),  # any other address in it is tracking
    ("https://evilindeed.com/x", "https://evilindeed.com/x"),  # not Indeed's, whatever it's called
    ("javascript:alert(1)", "https://sg.indeed.com/applystart?jk=7451975960105ec7"),
])
def test_an_apply_link_that_shows_its_destination_is_stored_as_that(href, apply_url):
    job = copy.deepcopy(APPLY_JOBS["company_site"])
    job["body"]["viewJobButtonLinkContainerModel"]["viewJobButtonLinkModel"]["href"] = href
    assert sites.IndeedSite().posting_from_job(job, apply_listing("company_site")).apply_url == apply_url


def test_apply_site_only_moves_jobs_whose_link_goes_to_mycareersfuture():
    assert sites.apply_site("indeed", MCF_PAGE) == "mycareersfuture"
    assert sites.apply_site("indeed", "https://mycareersfuture.gov.sg/job/x") == "mycareersfuture"
    assert sites.apply_site("indeed", "https://notmycareersfuture.gov.sg/job/x") == "indeed"
    assert sites.apply_site("indeed", "https://sg.indeed.com/viewjob?jk=1") == "indeed"
    assert sites.apply_site("indeed", "") == "indeed"  # older jobs: the Indeed link, as before
    assert sites.apply_site("mycareersfuture", "") == "mycareersfuture"


MCF_INDEED_JOB = dict(source="indeed", url="https://sg.indeed.com/viewjob?jk=0df04dc908d6a540", company="BCW",
                      role="Data Developer", apply_url=MCF_PAGE, folder="f", state="Ready")


def test_an_indeed_job_that_applies_on_mycareersfuture_is_handed_over_without_tokens(secrets, fake_llm, data_dir,
                                                                                      monkeypatch):
    from jobhunt import db
    jid = db.add_job(**MCF_INDEED_JOB)
    monkeypatch.setattr(hunt, "ApplyAgent", lambda **kw: pytest.fail("the apply agent must not start"))
    task = hunt.Task("apply", {"job_id": jid}, __import__("threading").Event())
    assert hunt.apply_job(task, browser=None, job_id=jid) == "Needs you"
    job = db.get_job(jid)
    assert job["reason_kind"] == "singpass" and fake_llm.requests == []
    assert hunt.needs_you_fix(job)["retry"] is False


def test_the_job_page_opens_the_apply_destination_and_the_posting_where_it_was_found(data_dir):
    from fastapi.testclient import TestClient
    from jobhunt import db
    from jobhunt.web.app import create_app
    jid = db.add_job(**MCF_INDEED_JOB)
    other = db.add_job(**dict(MCF_INDEED_JOB, apply_url="https://sg.indeed.com/applystart?jk=7451975960105ec7",
                              url="https://sg.indeed.com/viewjob?jk=7451975960105ec7", company="EA"))
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        page = c.get(f"/jobs/{jid}").text
        assert f'href="{MCF_PAGE}"' in page and "Open apply page" in page
        assert 'href="https://sg.indeed.com/viewjob?jk=0df04dc908d6a540"' in page and "Open posting" in page
        assert f'action="/jobs/{jid}/apply"' not in page and "needs Singpass" in page
        assert f'action="/jobs/{other}/apply"' in c.get(f"/jobs/{other}").text
        card = c.get("/").text.split('id="readyToApprove"', 1)[1].split("</table>", 1)[0]
        assert card.count("Apply yourself") == 1 and card.count("Approve &amp; apply") == 1


class RedirectPage:
    """Just enough of a Playwright page for the apply agent's first load: goto follows `redirects`."""

    def __init__(self, redirects):
        self.redirects, self.url, self.context = redirects, "about:blank", self
        self.pages = [self]

    def is_closed(self):
        return False

    def route(self, pattern, handler):
        pass

    def goto(self, url, **kw):
        self.url = self.redirects.get(url, url)

    def wait_for_load_state(self, *a, **kw):
        pass


def redirect_agent(redirects, fake, stop=False):
    import threading
    from jobhunt import secrets_store, settings
    from jobhunt.applyagent import ApplyAgent
    from jobhunt.llm import LLMClient, Meter
    s = settings.load()
    s["pacing"]["action_delay_s"] = 0
    ev = threading.Event()
    if stop:
        ev.set()  # stop before the first look at the page: only the start is tested
    return ApplyAgent(browser=type("B", (), {"page": RedirectPage(redirects)})(),
                      llm=LLMClient(s, "sk-test", meter=Meter.from_settings(s), transport=fake.transport),
                      settings=s, contact={}, profile_text="", job={"id": None, "role": "R", "company": "C"},
                      folder=Path("."), redactor=secrets_store.Redactor.from_store(s), sleep=lambda s: None,
                      stop_event=ev)


def test_the_apply_agent_may_come_back_to_the_site_an_apply_link_led_to(secrets, fake_llm, data_dir):
    start = "https://sg.indeed.com/applystart?jk=7451975960105ec7"
    agent = redirect_agent({start: "https://jobs.ea.com/en_US/careers/JobDetail/Director-of-Product/213767"},
                           fake_llm, stop=True)
    assert agent.run(start).status == "stopped"
    assert agent.allowed_sites == {"indeed.com", "ea.com"}
    assert fake_llm.requests == []


def test_the_apply_agent_hands_over_when_an_apply_link_leads_to_mycareersfuture(secrets, fake_llm, data_dir):
    start = "https://sg.indeed.com/applystart?jk=0df04dc908d6a540"
    result = redirect_agent({start: "https://www.mycareersfuture.gov.sg/job/d4bd2d166c14cb46dc20d1c94ae2cf40"},
                            fake_llm).run(start)
    assert (result.status, result.kind, result.steps) == ("needs_you", "singpass", 0)
    assert "Singpass" in result.summary and fake_llm.requests == []
