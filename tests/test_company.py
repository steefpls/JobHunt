"""The company summary on every package: read from pages JobHunt fetched, every fact checked against the
page it quotes, never able to fail a package, and on the job page and the Dashboard."""
import json
import socket
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from jobhunt import company, db, demo_board, hunt, llm, settings, sites
from jobhunt.web.app import create_app

from conftest import FIXTURES

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def sources(*texts):
    return [company.Source(f"S{i + 1}", f"page {i + 1}", f"https://example.com/{i + 1}", t) for i, t in enumerate(texts)]


POSTING = "Title: Electrical Engineer\nCompany: HOPE Technik Pte Ltd\nLocation: Teban Gardens, West Region\n\nBuild robots."
BOARD = ("Company size: 51-100\nPrimary location: 6 Penjuru Close Singapore 608614\n"
         "HOPE Technik, a Singapore-proud engineering company, has been evolving the industry landscape since 2006.")


# ── no made-up facts ──────────────────────────────────────────────────

def test_facts_without_a_real_quote_are_dropped():
    """Zech must never be told a location, size or headquarters the pages don't state."""
    srcs = sources(POSTING, BOARD)
    data = {
        "what_they_do": {"text": "A Singapore engineering company.", "short": "Engineering company", "source": "S2",
                         "quote": "a Singapore-proud engineering company"},
        "job_location": {"text": "Teban Gardens, West Region", "source": "S1", "quote": "Teban Gardens, West Region"},
        # the quote is on the page, but the place isn't in it: worked out from 'Pte Ltd'
        "hq_country": {"text": "Singapore", "source": "S1", "quote": "HOPE Technik Pte Ltd"},
        # a number that isn't in the quote
        "size": {"text": "500 employees", "source": "S2", "quote": "Company size: 51-100"},
        # a quote that isn't on the page it names
        "company_location": {"text": "Jurong East", "source": "S2", "quote": "office in Jurong East"},
        "industry": {"text": "Robotics", "source": "S9", "quote": "Build robots"},  # no such source
        "notable": [{"text": "Founded in 2006", "source": "S2", "quote": "since 2006"},
                    {"text": "Listed on SGX", "source": "S2", "quote": "listed on the SGX"}],
    }
    out = company.verify(data, srcs)
    f = out["fields"]
    assert f["what_they_do"]["text"] == "A Singapore engineering company." and f["what_they_do"]["short"]
    assert f["job_location"]["text"] == "Teban Gardens, West Region"
    assert f["hq_country"] is None and f["size"] is None and f["company_location"] is None and f["industry"] is None
    assert [n["text"] for n in out["notable"]] == ["Founded in 2006"]
    assert out["dropped"] == 5


def test_a_quote_may_differ_in_spacing_case_and_punctuation():
    srcs = sources(POSTING, BOARD)
    fact = company.check_fact({"text": "51-100 employees", "source": "s2", "quote": "company SIZE:  51 – 100"},
                              {s.id: s for s in srcs})
    assert fact and fact["text"] == "51-100 employees"
    assert company.check_fact({"text": "6 Penjuru Close, Singapore", "source": "S2", "quote": "6 Penjuru Close Singapore"},
                              {s.id: s for s in srcs}, place=True)


def test_not_found_answers_stay_not_found():
    out = company.verify({"size": {"text": "not found", "source": "", "quote": ""},
                          "hq_country": {"text": "Not found in the sources", "source": "S1", "quote": "Teban"}},
                         sources(POSTING))
    assert out["fields"]["size"] is None and out["fields"]["hq_country"] is None and out["dropped"] == 0


def test_a_recruiter_posting_is_flagged_even_when_the_ai_misses_it():
    posting = POSTING + "\nOur client, a leading bank, is hiring.\nEA Licence No. 12C3456"
    out = company.verify({"agency": False, "notable": []}, sources(posting))
    assert out["agency"] is True
    assert "recruitment agency" in out["notable"][0]["text"] and "Our client" in out["notable"][0]["quote"]
    info = {"status": "ok", **out}
    assert company.one_line(info).startswith("Recruiter posting")
    # the AI saying "agency" with nothing to back it doesn't count
    assert company.verify({"agency": True, "notable": []}, sources(POSTING))["agency"] is False


def test_one_line_is_made_from_checked_facts_only():
    info = {"status": "ok", "agency": False, "fields": {
        "what_they_do": {"text": "Builds robots for hospitals.", "short": "Engineering & robotics firm", "source": "S2"},
        "job_location": {"text": "Teban Gardens, West Region", "source": "S1"},
        "size": {"text": "51-100", "source": "S2"}}}
    assert company.one_line(info) == "Engineering & robotics firm · Teban Gardens, West Region · 51-100 staff"
    assert company.one_line({"status": "failed", "error": "x"}) == ""
    assert company.line_for({"company_info": ""}) == "" and company.line_for({"company_info": "not json"}) == ""


# ── reading the boards ────────────────────────────────────────────────

def test_jobstreet_company_page_as_seen_live():
    """A JobStreet company page, captured from the live site on 2026-10-15 (logged out, headless)."""
    text, website = company.parse_board_company_page(read("jobstreet_company_live.html"),
                                                     "https://sg.jobstreet.com/companies/hope-technik-pte-ltd-168550780952919")
    assert website == "http://www.hopetechnik.com"
    assert "Company size: 51-100" in text and "Primary location: 6 Penjuru Close Singapore 608614" in text
    assert "Singapore-proud engineering company" in text
    assert "Recent reviews" not in text and "Technician (Electrical)" not in text  # reviews and job cards aren't facts


def test_jobstreet_posting_names_its_company_page():
    listing = sites.Listing("jobstreet", "https://sg.jobstreet.com/job/95236588", "t", "c", "l")
    p = sites.JobStreetSite().parse_posting(read("jobstreet_posting.html"), listing)
    assert p.company_info == {"board_page": "https://sg.jobstreet.com/companies/hope-technik-pte-ltd-168550780952919"}


def test_mycareersfuture_record_as_seen_live():
    """MyCareersFuture's job record (live API, 2026-10-15): the company's own description, its registered
    business, and the workplace address, with no extra page load."""
    r = json.loads(read("mcf_job_company_live.json"))
    listing = sites.Listing("mycareersfuture", "https://www.mycareersfuture.gov.sg/job/x-61662a68782966e3ae83fe0aab0c36fc",
                            "t", "c", extra={"uuid": r["uuid"]})
    info = sites.MyCareersFutureSite().posting_from_job(r, listing).company_info
    assert info["label"].startswith("MyCareersFuture")
    assert "Registered business activity: Universities" in info["text"]
    assert "Workplace address: 50 NANYANG AVENUE, NANYANG TECHNOLOGICAL UNIVERSITY, Singapore 639798" in info["text"]
    assert "research-intensive public university" in info["text"]
    # posted by an agency for an employer: both are said
    r["hiringCompany"] = {"name": "ACME BANK", "description": "<p>A bank.</p>"}
    r["postedCompany"]["name"] = "BEST RECRUITERS PTE. LTD."
    text = company.mcf_record_text(r)[0]
    assert "Posted by: BEST RECRUITERS PTE. LTD." in text
    assert "Hiring company (the employer this was posted for): ACME BANK" in text


def test_company_website_named_in_the_posting_must_carry_the_name():
    assert company.domain_matches_name("www.hopetechnik.com", "HOPE Technik Pte Ltd")
    assert company.domain_matches_name("www.ntu.edu.sg", "NANYANG TECHNOLOGICAL UNIVERSITY")
    assert not company.domain_matches_name("careers.example.com", "HOPE Technik Pte Ltd")
    assert company.not_company_site("myworkdayjobs.com") and company.not_company_site("sg.linkedin.com")
    assert company.links_in_text("Visit www.hopetechnik.com or write to hr@hopetechnik.com.") == ["https://www.hopetechnik.com"]


def test_only_ordinary_public_websites_are_opened(monkeypatch):
    """A posting could name JobHunt's own address or a home router: never opened."""
    answers = {"public.example": "93.184.216.34", "sneaky.example": "192.168.1.1", "me.example": "127.0.0.1"}
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", (answers[host], port))])
    assert company.public_address("https://public.example/about") == "https://public.example/about"
    for url in ("http://127.0.0.1:8765/files/1/resume.pdf", "http://localhost/", "https://sneaky.example/",
                "https://me.example/", "file:///C:/Windows/win.ini", "javascript:alert(1)", "http://router.local/"):
        assert company.public_address(url) == "", url


# ── which pages a lookup loads ────────────────────────────────────────

class _Browser:
    """Pages by URL. `isolated` records what was opened in the throwaway browser."""

    def __init__(self, pages):
        self.pages, self.loaded, self.isolated, self.last_url, self.last_status = pages, [], [], "", 200

    def _get(self, url):
        self.last_url = url
        if url not in self.pages:
            self.last_status = 404
            return "<html><title>Not found</title></html>"
        self.last_status = 200
        return self.pages[url]

    def load_html(self, url):
        self.loaded.append(url)
        return self._get(url)

    def load_isolated_html(self, url, allow=None):
        """Like BrowserSession.load_isolated_html: a page value ('redirect', target) is a redirect, refused
        (PermissionError) when `allow` says no."""
        self.isolated.append(url)
        while isinstance(self.pages.get(url), tuple):
            url = self.pages[url][1]
            if allow is not None and not allow(url):
                raise PermissionError(f"it sent JobHunt to {url}")
        return self._get(url)


class _PacedContext(sites.SiteContext):
    """Counts the pauses: every page load must wait its turn, as in a hunt."""

    def __init__(self, browser):
        super().__init__(browser, {"min_delay_s": 0, "max_delay_s": 0})
        self.turns = 0

    def wait_turn(self):
        self.turns += 1


def _job(**kw):
    return {"id": 1, "source": "jobstreet", "url": "https://sg.jobstreet.com/job/1", "company": "HOPE Technik Pte Ltd",
            "role": "Electrical Engineer", "location": "Teban Gardens, West Region", "posting_text": "Build robots. " * 10,
            "salary": "", **kw}


def test_a_jobstreet_lookup_reads_the_company_page_and_website_with_pacing(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    board = "https://sg.jobstreet.com/companies/hope-technik-pte-ltd-168550780952919"
    home = ("<html><title>HOPE Technik</title><body><main><p>HOPE Technik builds robots.</p>"
            "<a href='/contact-us'>Contact</a></main><footer>HOPE Technik Pte Ltd, 6 Penjuru Close, Singapore</footer></body></html>")
    browser = _Browser({board: read("jobstreet_company_live.html"), "http://www.hopetechnik.com": home,
                        "http://www.hopetechnik.com/contact-us": "<html><body><main>Call us at 6262 0000</main></body></html>"})
    ctx = _PacedContext(browser)
    srcs, skipped = company.gather(ctx, _job(), {"board_page": board})
    assert [s.label for s in srcs] == ["The job posting", "JobStreet's company page",
                                       "The company's website (named by the job board)",
                                       "The company's website (named by the job board), /contact-us"]
    assert "6 Penjuru Close, Singapore" in srcs[2].text  # the footer is read: that's where addresses are
    assert browser.loaded == [board]  # the job board, in the job-site browser
    assert browser.isolated == ["http://www.hopetechnik.com", "http://www.hopetechnik.com/contact-us"]  # no logins
    assert ctx.turns == 3  # every load waited its turn
    # nothing skipped but what tests can't reach (the registry and Wikipedia; no search: the board named the site)
    assert [s.split(":")[0] for s in skipped] == ["Singapore's company registry (ACRA)", "Wikipedia"]


def test_indeed_and_linkedin_company_pages_are_never_loaded(monkeypatch):
    """Indeed is only read in a window (its human check), LinkedIn's company pages need a login: a lookup
    reads neither, only the posting and, at most, the company's own website."""
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    for source, url in (("indeed", "https://sg.indeed.com/viewjob?jk=0123456789abcdee"),
                        ("linkedin", "https://www.linkedin.com/jobs/view/4457908204/")):
        browser = _Browser({})
        srcs, skipped = company.gather(_PacedContext(browser), _job(source=source, url=url), None)
        assert browser.loaded == []  # nothing in the job-site browser at all
        assert not any(h in u for u in browser.isolated for h in ("indeed.", "linkedin."))
        assert [s.label for s in srcs] == ["The job posting"]


def test_a_guessed_website_must_name_the_company_and_singapore(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    other = "<html><title>Hope Technik GmbH</title><body><main>HOPE Technik builds cars in Munich.</main></body></html>"
    ours = "<html><title>HOPE Technik</title><body><main>HOPE Technik, engineering in Singapore.</main></body></html>"
    browser = _Browser({"https://www.hopetechnik.com.sg": other, "https://www.hopetechnik.sg": ours})
    srcs, _ = company.gather(_PacedContext(browser), _job(), {})
    assert browser.isolated == ["https://www.hopetechnik.com.sg", "https://www.hopetechnik.sg"]
    assert srcs[-1].url == "https://www.hopetechnik.sg" and "check it is theirs" in srcs[-1].label


def test_a_long_legal_name_is_also_guessed_without_its_region_words(monkeypatch):
    """Seen live: 'Applied Materials South East Asia Pte Ltd' has no www.appliedmaterialssoutheast.com.sg;
    its site is appliedmaterials.com. Addresses that don't exist cost no page load."""
    assert company.short_names("Applied Materials South East Asia Pte Ltd") == ["appliedmaterialssoutheast",
                                                                              "appliedmaterials"]
    assert company.short_names("ALBERT HOO ELECTRICAL PTE LTD") == ["alberthooelectrical", "alberthoo"]
    assert company.short_names("HOPE Technik Pte Ltd") == ["hopetechnik"]
    assert company.short_names("Bolt Services") == ["boltservices"]  # 'bolt' alone is too short to be safe
    exists = {"www.appliedmaterials.com"}

    def resolve(host, port):
        if host not in exists:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(company, "resolve", resolve)
    page = "<html><body><main>Applied Materials Singapore: materials engineering for chips.</main></body></html>"
    browser = _Browser({"https://www.appliedmaterials.com": page})
    job = _job() | {"company": "Applied Materials South East Asia Pte Ltd"}
    srcs, _ = company.gather(_PacedContext(browser), job, {})
    assert browser.isolated == ["https://www.appliedmaterials.com"]
    assert srcs[-1].url == "https://www.appliedmaterials.com" and "check it is theirs" in srcs[-1].label


def test_a_robot_check_is_skipped_not_worked_around(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    board = "https://sg.jobstreet.com/companies/x-1"
    browser = _Browser({board: "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"})
    srcs, skipped = company.gather(_PacedContext(browser), _job(), {"board_page": board})
    assert browser.loaded == [board]  # tried once, never again
    assert [s.label for s in srcs][0] == "The job posting" and "robot check" in skipped[0]


def test_practice_board_jobs_open_nothing_outside_the_practice_board(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    browser = _Browser({})
    job = _job(source="demo", url="http://127.0.0.1:9/demo/job/1", company="Lumen Analytics",
               posting_text="See https://www.lumenanalytics.com and http://127.0.0.1:9/files/1/resume.pdf " * 3)
    company.gather(_PacedContext(browser), job, {}, practice_base="http://127.0.0.1:9/demo")
    assert browser.isolated == [] and browser.loaded == []


def test_at_most_four_pages_per_lookup(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    browser = _Browser({})  # every page 404s, so a lookup would keep trying
    posting = " ".join(f"https://www.hopetechnik{i}.com" for i in range(10))
    company.gather(_PacedContext(browser), _job(posting_text=posting), {"board_page": "https://sg.jobstreet.com/companies/x"})
    assert len(browser.loaded) + len(browser.isolated) <= company.MAX_PAGE_LOADS


# ── part of every package, never a reason it fails ───────────────────

def _prepare(demo_server):
    settings.update({"demo_board_url": demo_server,
                     "scope": {"keywords": ["engineer"], "location": "Singapore", "avoid_words": ["intern"]},
                     "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0}})


def test_a_failed_lookup_never_fails_the_package(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    fake_llm.company_brain = lambda user: "I'd rather not"  # not a JSON object: the summary can't be read
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"]})
    runner.wait(180)
    assert db.get_run(run_id)["status"] == "done"
    [job] = db.list_jobs()
    assert job["state"] == "Ready"
    info = company.load(job)
    assert info["status"] == "failed" and "valid JSON" in info["error"]
    assert any("Couldn't make the company summary" in e["message"] for e in db.recent_events())


def test_the_company_summary_never_spends_past_the_caps(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    runner = hunt.Runner()
    runner.start("hunt", {"jobs": 1, "sites": ["demo"]})
    runner.wait(180)
    [job] = db.list_jobs()
    used = job["tokens_in"] + job["tokens_out"]
    settings.update({"caps": {"per_job_tokens": used + 100}})  # no room for another summary
    runner.start("company", {"job_id": job["id"]})
    runner.wait(120)
    assert runner.last_status == "error" and "token cap" in runner.last_result
    assert company.load(db.get_job(job["id"]))["status"] == "ok"  # the one it had is kept
    assert db.get_job(job["id"])["state"] == "Ready"


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_look_up_button_on_the_job_page_and_the_dashboard_line(client, demo_server, secrets, candidate, fake_llm):
    """An older package has no summary: the job page offers the button, and the summary then shows at the top
    of the job page, with its sources, and as one line on the Dashboard."""
    _prepare(demo_server)
    j = demo_board.JOBS[0]
    jid = db.add_job(source="demo", url=f"{demo_server}/job/1", company=j["company"], role=j["title"],
                     location=j["location"], posting_text="Lumen Analytics builds data products. " * 5, state="Ready")
    page = client.get(f"/jobs/{jid}").text
    assert "Look up this company" in page and "No company summary yet" in page

    r = client.post(f"/jobs/{jid}/company", headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Looking%20up%20Lumen%20Analytics" in r.headers["location"]
    hunt.runner.wait(120)
    assert hunt.runner.last_status == "done", hunt.runner.last_result
    assert "Company summary updated" in hunt.runner.last_result

    page = client.get(f"/jobs/{jid}").text
    assert page.index('id="company"') < page.index("What next")  # the first thing on the page
    assert "51-200 employees" in page and "18 Cross Street, #07-01, Singapore 048423" in page
    assert "Not found" in page  # what the pages don't say (the industry) says so
    assert "Sources" in page and "/demo/companies/lumen-analytics" in page
    assert ">Refresh<" in page
    assert db.get_job(jid)["state"] == "Ready"  # a lookup changes nothing else
    assert [c["stage"] for c in db.job_calls(jid)] == ["company"]  # counted in the job's AI use

    dash = client.get("/").text
    assert "Software company · Singapore (Hybrid) · 51-200 employees" in dash


def test_look_up_refuses_while_something_runs(client, monkeypatch):
    jid = db.add_job(source="demo", url="http://x/1", company="Acme", role="Engineer", state="Ready")
    monkeypatch.setattr(hunt.runner, "start", lambda kind, params=None: (_ for _ in ()).throw(
        RuntimeError("Something is already running. Stop it or wait for it to finish.")))
    r = client.post(f"/jobs/{jid}/company", headers=SAME_ORIGIN, follow_redirects=False)
    assert "err=Something%20is%20already%20running" in r.headers["location"]


# ── cost ──────────────────────────────────────────────────────────────

def test_the_company_step_uses_the_cheap_filter_model_and_counts_in_the_estimate():
    s = settings.load()
    client = llm.LLMClient(s | {"models": {**s["models"], "filter": "cheap-model", "tailor": "big-model"}}, "sk-x")
    assert client.model_for("company") == "cheap-model"
    est = hunt.estimate(s, 1, ["demo"], auto_submit=False)
    f_in, f_out = hunt.DEFAULT_TOKENS["filter"]
    t_in, t_out = hunt.DEFAULT_TOKENS["tailor"]
    c_in, c_out = hunt.DEFAULT_TOKENS["company"]
    assert est["per_job_tokens"] == pytest.approx((f_in + f_out) * est["candidates_per_match"] + t_in + t_out + c_in + c_out)
    assert est["calls_per_job"] == pytest.approx(est["candidates_per_match"] + 2)


# ── what the review found (2026-10-15) ───────────────────────────────

def _local_server(handler_map, hits):
    """A tiny web server on 127.0.0.1: {path: (status, headers, body)}; every path asked for goes in `hits`."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, headers, body = handler_map.get(self.path, (404, {}, b"no"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_website_cannot_redirect_or_reach_into_this_pc():
    """The real throwaway browser: a redirect to a refused address is never followed (the request is never
    made), nor is any request the page itself makes to one. Here 127.0.0.1 plays the internet and
    localhost plays this PC."""
    from jobhunt.browser import BrowserSession
    secret_hits, public_hits = [], []
    secret = _local_server({"/secret": (200, {"Content-Type": "text/html"}, b"<p>router password</p>")}, secret_hits)
    s_port = secret.server_address[1]
    page = (f"<html><body><main>Acme Robotics, Singapore.<img src='http://localhost:{s_port}/secret'>"
            f"<script>fetch('http://localhost:{s_port}/secret')</script></main></body></html>").encode()
    public = _local_server({"/": (200, {"Content-Type": "text/html"}, page),
                            "/go": (302, {"Location": f"http://localhost:{s_port}/secret"}, b"")}, public_hits)
    p_port = public.server_address[1]
    allow = lambda u: (urlparse(u).hostname or "") == "127.0.0.1"
    b = BrowserSession(headless=True)
    try:
        with pytest.raises(PermissionError, match="localhost"):
            b.load_isolated_html(f"http://127.0.0.1:{p_port}/go", allow=allow)
        html = b.load_isolated_html(f"http://127.0.0.1:{p_port}/", allow=allow)
        assert "Acme Robotics" in html
    finally:
        b.close()
        secret.shutdown()
        public.shutdown()
    assert "/go" in public_hits and secret_hits == []


def test_a_website_that_redirects_home_is_skipped_and_said_so(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    browser = _Browser({"https://hopetechnik.com": ("redirect", "http://192.168.1.1/admin"),
                        "http://192.168.1.1/admin": "<html><main>HOPE Technik router admin, Singapore</main></html>"})
    srcs, skipped = company.gather(_PacedContext(browser), _job(), {"website": "https://hopetechnik.com"})
    assert not any("website" in s.label for s in srcs)
    assert any("home network" in s and "192.168.1.1" in s for s in skipped)


def test_the_board_company_page_must_be_on_the_board_itself(monkeypatch):
    """A /companies/ link inside the job description, or one on another site, is never opened in the
    logged-in board browser; nor is an Indeed or LinkedIn company page."""
    desc = ("<html><body><div data-automation='jobAdDetails'><p>Apply via "
            "<a href='http://127.0.0.1:8765/companies/x'>us</a></p></div></body></html>")
    assert company.board_company_info(desc, "https://sg.jobstreet.com/job/1") == {}
    own = "<html><body><a href='/companies/hope-technik-1?x=1'>HOPE Technik</a></body></html>"
    assert company.board_company_info(own, "https://sg.jobstreet.com/job/1") == {
        "board_page": "https://sg.jobstreet.com/companies/hope-technik-1"}
    browser = _Browser({})
    for page in ("http://127.0.0.1:8765/companies/x", "https://www.indeed.com/cmp/hope-technik",
                 "https://evil.example/companies/x"):
        srcs, skipped = company.gather(_PacedContext(browser), _job(), {"board_page": page})
        assert len(srcs) == 1 and any("not on the job board's own site" in s for s in skipped)
    assert browser.loaded == [] and browser.isolated == []


def test_a_malformed_web_address_never_breaks_a_posting_or_a_lookup():
    bad = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "x", '
           '"hiringOrganization": {"name": "HOPE", "sameAs": "http://[bad"}}</script></head>'
           '<body><a href="http://[oops/companies/x">c</a></body></html>')
    assert company.board_company_info(bad, "https://sg.jobstreet.com/job/1") == {}
    assert company.host_of("http://[abc") == "" and company.public_address("http://[abc") == ""
    srcs, skipped = company.gather(_PacedContext(_Browser({})), _job(posting_text="See http://[abc for more. " * 3), {})
    assert srcs[0].label == "The job posting"


def test_a_quote_must_back_its_fact_not_just_be_on_the_page():
    posting = "Location: Singapore\nSalary: $50 per hour\nWe make robots for hospitals and ports."
    board = "Company size: 51-100\nHeadquartered in Munich, Germany.\nWe are a leading robotics maker."
    srcs = sources(posting, board)
    out = company.verify({
        "hq_country": {"text": "Singapore", "source": "S1", "quote": "Singapore"},  # where the job is
        "size": {"text": "50 employees", "source": "S1", "quote": "50 per hour"},    # that's pay
        "industry": {"text": "Robotics", "source": "S1", "quote": "bots for hosp"},  # not whole words
        "what_they_do": {"text": "Makes robots for hospitals and ports.", "source": "S1",
                         "quote": "We make robots for hospitals and ports", "short": "Global bank leader"},
    }, srcs)
    f = out["fields"]
    assert f["hq_country"] is None and f["size"] is None and f["industry"] is None
    assert f["what_they_do"] and "short" not in f["what_they_do"]  # the Dashboard won't show an unbacked label
    out = company.verify({
        "hq_country": {"text": "Germany", "source": "S2", "quote": "Headquartered in Munich, Germany"},
        "size": {"text": "51-100 employees", "source": "S2", "quote": "51-100"},  # its label is just before it
        "what_they_do": {"text": "A robotics maker.", "source": "S2", "quote": "a leading robotics maker",
                         "short": "Robotics maker"},
    }, srcs)
    f = out["fields"]
    assert f["hq_country"]["text"] == "Germany" and f["size"]["text"] == "51-100 employees"
    assert f["what_they_do"]["short"] == "Robotics maker"


@pytest.mark.parametrize("text,agency", [
    ("Our client, a leading bank, is hiring.", True),
    ("EA Licence No. 12C3456", True),
    ("We are hiring on behalf of a client in logistics.", True),
    ("You will support our clients in banking and insurance.", False),
    ("Our clients include DBS and OCBC.", False),
    ("Req ID R0123456. Contact our recruitment consultant for details.", False),
])
def test_only_strong_signs_make_a_recruiter_posting(text, agency):
    out = company.verify({"agency": "false"}, sources("Title: Analyst\n" + text))
    assert out["agency"] is agency
    assert bool(out["notable"]) is agency


def test_a_board_that_showed_a_robot_check_is_left_alone_for_the_rest_of_the_hunt(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    board = "https://sg.jobstreet.com/companies/hope-technik-1"
    browser = _Browser({board: "<html><title>Just a moment...</title><body>Verify you are human</body></html>"})
    ctx = _PacedContext(browser)
    _, skipped = company.gather(ctx, _job(), {"board_page": board})
    assert any("robot check" in s for s in skipped) and browser.loaded == [board]
    _, skipped = company.gather(ctx, _job(url="https://sg.jobstreet.com/job/2"), {"board_page": board + "0"})
    assert browser.loaded == [board]  # not asked again
    assert any("earlier" in s for s in skipped)


class _Task:
    def __init__(self, reply):
        self.reply, self.logs = reply, []

    def get_llm(self):
        task = self

        class _LLM:
            def chat_json(self, **kw):
                return task.reply, None
        return _LLM()

    def log(self, msg, level="info", job_id=None):
        self.logs.append((level, msg))


def test_a_refresh_that_finds_less_keeps_the_fuller_summary(data_dir):
    jid = db.add_job(source="demo", url="http://x/1", company="Acme", role="Engineer", state="Applied",
                     posting_text="Acme builds robots in Singapore. Company size: 51-100. " * 3)
    before = db.get_job(jid)["updated_at"]
    full = {"what_they_do": {"text": "Builds robots.", "source": "S1", "quote": "Acme builds robots in Singapore"},
            "size": {"text": "51-100 staff", "source": "S2", "quote": "Employees: 51-100"}}
    board = {"label": "The board's record", "url": "http://x/1", "text": "Acme Pte Ltd\nEmployees: 51-100\nRobots."}
    ctx = _PacedContext(_Browser({}))
    first = company.summarise(_Task(full), ctx, jid, info=board)
    assert first["status"] == "ok" and company._found(first) == 2 and len(first["sources"]) == 2
    # the board's record couldn't be read this time: the fuller summary stays
    again = company.summarise(_Task({"what_they_do": full["what_they_do"]}), ctx, jid, info={})
    assert "fewer pages could be read" in again["refresh_error"]
    assert company.load(db.get_job(jid))["fields"]["size"]["text"] == "51-100 staff"  # still the full one
    assert db.get_job(jid)["updated_at"] == before  # an Applied job's date (the apply cap counts it) stays
    # the same pages, the AI just found less: the new one is what it found
    again = company.summarise(_Task({"what_they_do": full["what_they_do"]}), ctx, jid, info=board)
    assert "refresh_error" not in again and company.load(db.get_job(jid))["fields"]["size"] is None


def test_the_job_page_knows_a_lookup_is_running_from_the_first_moment(data_dir):
    import threading
    t = hunt.Task("company", {"job_id": 7}, threading.Event())
    r = hunt.Runner()
    r.task = t
    assert r.status()["job_id"] == 7 and r.status()["current_job"] is None


def test_a_guessed_site_may_show_singapore_on_its_contact_page_only(monkeypatch):
    """Seen live: alberthoo.com.sg names the company on its home page but its Singapore address only on
    /contact. A home page that doesn't name the company is still never used."""
    exists = {"www.alberthoo.com.sg"}

    def resolve(host, port):
        if host not in exists:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(company, "resolve", resolve)
    home = ("<html><body><main>Lighting for every project.<a href='/contact'>Contact Us</a></main>"
            "<footer>Albert Hoo Electrical Pte Ltd 2026</footer></body></html>")
    contact = "<html><body><main>Albert Hoo Electrical Pte Ltd, #01-05 Wave 9, Singapore 757048</main></body></html>"
    browser = _Browser({"https://www.alberthoo.com.sg": home, "https://www.alberthoo.com.sg/contact": contact})
    job = _job(company="ALBERT HOO ELECTRICAL PTE LTD")
    srcs, _ = company.gather(_PacedContext(browser), job, {})
    assert [s.url for s in srcs[1:]] == ["https://www.alberthoo.com.sg", "https://www.alberthoo.com.sg/contact"]
    assert "Singapore 757048" in srcs[-1].text

    browser = _Browser({"https://www.alberthoo.com.sg": home.replace("Albert Hoo Electrical", "Someone Else"),
                        "https://www.alberthoo.com.sg/contact": contact})
    srcs, _ = company.gather(_PacedContext(browser), job, {})
    assert len(srcs) == 1 and browser.isolated == ["https://www.alberthoo.com.sg"]
