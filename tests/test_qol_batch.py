"""Posted dates and pay on the cards, must-haves, closed ads, "No reply yet", stopping early on known pages,
what each site found, and notifications."""
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from jobhunt import adcheck, db, hunt, paths, render, settings, sites
from jobhunt.extract import Listing, posted_date
from jobhunt.web.app import create_app, no_reply_days, pay_text, posted_text

FIX = Path(__file__).resolve().parent / "fixtures"
SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}
TODAY = date(2026, 10, 20)


# ── when a job was posted, and its pay, on the cards ──────────────────

def test_posted_date_reads_every_boards_way_of_saying_it():
    assert posted_date("2026-10-18") == "2026-10-18"
    assert posted_date("2026-10-18T06:49:33.162Z") == "2026-10-18"
    assert posted_date(1760918400000) == "2025-10-20"  # Indeed's pubDate, in milliseconds
    assert posted_date("19h ago", TODAY) == "2026-10-20"
    assert posted_date("45m ago", TODAY) == "2026-10-20"
    assert posted_date("7d ago", TODAY) == "2026-10-13"
    assert posted_date("30d+ ago", TODAY) == "2026-09-20"
    assert posted_date("3 days ago", TODAY) == "2026-10-17"
    assert posted_date("Posted 2 weeks ago", TODAY) == "2026-10-06"
    assert posted_date("1 month ago", TODAY) == "2026-09-20"
    assert posted_date("Just posted", TODAY) == "2026-10-20"
    assert posted_date("Yesterday", TODAY) == "2026-10-19"
    for nothing in ("", None, "Urgent", "2026-13-45", 0):
        assert posted_date(nothing, TODAY) == "", nothing


def test_search_cards_carry_the_posted_day():
    js = sites.JobStreetSite().parse_search((FIX / "jobstreet_search.html").read_text(encoding="utf-8"))
    assert js and all(l.posted == date.today().isoformat() for l in js[:3])  # "1h ago", "10h ago", "2h ago"
    mcf = sites.MyCareersFutureSite().listing_from_result(
        {"uuid": "u1", "title": "Dev", "metadata": {"newPostingDate": "2026-10-18", "jobDetailsUrl": "https://x/job/u1"}})
    assert mcf.posted == "2026-10-18"
    li = sites.LinkedInSite().parse_search('<div class="base-card" data-entity-urn="urn:li:jobPosting:4300000001">'
                                           '<h3 class="base-search-card__title">Dev</h3>'
                                           '<time class="job-search-card__listdate" datetime="2026-10-13"></time></div>')
    assert li[0].posted == "2026-10-13"
    indeed = sites.IndeedSite().listing_from_result({"jobkey": "0123456789abcdee", "title": "Dev", "pubDate": 1760918400000})
    assert indeed.posted == "2025-10-20"


def test_posted_and_pay_in_words():
    assert posted_text("2026-10-20", TODAY) == "posted today"
    assert posted_text("2026-10-19", TODAY) == "posted yesterday"
    assert posted_text("2026-10-14", TODAY) == "posted 6 days ago"
    assert posted_text("2026-08-01", TODAY) == "posted 30+ days ago"
    assert posted_text("", TODAY) == ""
    assert pay_text("SGD 5,500-8,000 Monthly") == "$5.5k-8k a month"
    assert pay_text("$84,000 – $120,000 per year") == "$7k-10k a month"
    assert pay_text("$6,000 per month") == "$6k a month"
    assert pay_text("$25 - $30 per hour") == "$25 - $30 per hour"  # not monthly: as the posting says it
    assert pay_text("") == ""


def _ready(company="Lumen Analytics", role="Backend Engineer", **kw):
    jid = db.add_job(source=kw.pop("source", "demo"), url=kw.pop("url", f"https://example.com/{company}/{role}"),
                     company=company, role=role, state="Ready", **kw)
    return jid


def test_to_review_cards_show_pay_and_posted_day():
    _ready(salary="SGD 7,000 - 9,500 / month", posted_at=(date.today() - timedelta(days=3)).isoformat())
    _ready("Quiet Co", "Analyst")  # says neither: no empty line
    html = TestClient(create_app()).get("/").text
    assert "$7k-9.5k a month" in html and "posted 3 days ago" in html
    assert html.count('class="facts-line"') == 2 and html.count('" hidden></div>') == 1  # kept for a closed-ad tag


# ── must-haves, from the match report ─────────────────────────────────

def test_requirements_read_from_the_match_report():
    md = render.match_report_md("Acme", "Dev", {"requirements": [
        {"requirement": "Nice: Go", "must_have": False, "addressed_by": "-", "strength": "gap"},
        {"requirement": "3 years of Python", "must_have": True, "addressed_by": "Lumen: 4 years", "strength": "strong"},
        {"requirement": "Kubernetes", "must_have": True, "addressed_by": "Docker only", "strength": "partial"}]}, [])
    reqs = render.requirements(md)
    assert reqs["marked"]
    assert [(r["text"], r["strength"], r["must"]) for r in reqs["items"]] == [
        ("3 years of Python", "strong", True), ("Kubernetes", "partial", True), ("Nice: Go", "gap", False)]
    assert reqs["items"][2]["by"] == ""
    old = "## Key requirements (from the posting)\n1. SQL  → addressed by: Lumen bullet 2  [strong]\n\n## Gaps\n- none\n"
    assert render.requirements(old) == {"items": [{"text": "SQL", "by": "Lumen bullet 2", "strength": "strong",
                                                   "must": False}], "marked": False}
    assert render.requirements("") == {"items": [], "marked": False}


def test_the_tailor_prompt_asks_which_are_must_haves():
    from jobhunt import prompts
    system, user = prompts.tailor_prompt("profile", {"name": "A"}, sites.Posting("demo", "u", "Dev", "Acme", "SG", "text"))
    assert '"must_have": true' in user and "must_have is true" in system


def test_job_page_ticks_the_must_haves():
    jid = _ready()
    folder = paths.jobs_dir() / "acme"
    folder.mkdir(parents=True)
    db.update_job(jid, folder="acme")
    (folder / "match_report.md").write_text(render.match_report_md("Acme", "Dev", {"requirements": [
        {"requirement": "3 years of Python", "must_have": True, "addressed_by": "Lumen: 4 years", "strength": "strong"},
        {"requirement": "AWS certification", "must_have": True, "addressed_by": "not in the profile", "strength": "gap"},
        {"requirement": "Go", "must_have": False, "addressed_by": "-", "strength": "gap"}]}, []), encoding="utf-8")
    html = TestClient(create_app()).get(f"/jobs/{jid}").text
    assert "Must haves" in html and "1 of 2 met from your resume" in html
    assert "Missing: not in the profile" in html and "Nice to have (0 of 1 met)" in html


# ── closed ads ────────────────────────────────────────────────────────

def test_each_sites_answer_about_its_ad():
    assert adcheck.mcf_state(200, {"status": {"jobStatus": "Open"}, "metadata": {"expiryDate": "2026-11-01"}}, TODAY)[0] == "open"
    assert adcheck.mcf_state(200, {"status": {"jobStatus": "Closed"}}, TODAY) == ("closed", "MyCareersFuture marks it Closed")
    assert adcheck.mcf_state(200, {"status": {"jobStatus": "Open"}, "metadata": {"expiryDate": "2026-10-01"}}, TODAY)[0] == "closed"
    assert adcheck.mcf_state(200, {"status": {"id": 103, "jobStatus": "Re-open"}, "metadata": {"expiryDate": "2026-11-19"}}, TODAY)[0] == "open"
    old = {"ad_status": '{"state": "closed", "site": "mycareersfuture", "why": "MyCareersFuture marks it Re-open"}'}
    assert adcheck.load(old) is None and adcheck.closed(old) is None and adcheck.due(old)
    assert adcheck.mcf_state(404, None)[0] == "closed"
    assert adcheck.mcf_state(503, None)[0] == "unknown"
    assert adcheck.linkedin_state(404, "")[0] == "closed"
    assert adcheck.linkedin_state(200, "<span>No longer accepting applications</span>")[0] == "closed"
    assert adcheck.linkedin_state(200, "<h2 class='top-card-layout__title'>Dev</h2>")[0] == "open"
    assert adcheck.linkedin_state(429, "")[0] == "unknown"  # rate-limited is not closed
    assert adcheck.jobstreet_state('{"isExpired":true,"status":"Expired"}')[0] == "closed"
    assert adcheck.jobstreet_state((FIX / "jobstreet_posting.html").read_text(encoding="utf-8"))[0] == "open"


def test_which_site_answers_for_a_job():
    assert adcheck.where({"source": "indeed", "url": "https://sg.indeed.com/viewjob?jk=1",
                          "apply_url": "https://www.mycareersfuture.gov.sg/job/it/dev-0123456789abcdef0123456789abcdef"}) == "mycareersfuture"
    assert adcheck.where({"source": "indeed", "url": "https://sg.indeed.com/viewjob?jk=1", "apply_url": ""}) == ""
    assert adcheck.where({"source": "linkedin", "url": "https://www.linkedin.com/jobs/view/4300000001/"}) == "linkedin"
    assert adcheck.where({"source": "jobstreet", "url": "https://sg.jobstreet.com/job/1"}) == "jobstreet"


def test_to_review_asks_about_its_ads_and_shows_the_closed_one(monkeypatch):
    asked = []

    def fake_get(url, timeout=15):
        asked.append(url)
        if "0123456789abcdef0123456789abcdef" in url:
            return httpx.Response(200, json={"status": {"jobStatus": "Closed"}}, request=httpx.Request("GET", url))
        return httpx.Response(200, text="<h2>Still here</h2>", request=httpx.Request("GET", url))
    monkeypatch.setattr(adcheck, "http_get", fake_get)
    gone = _ready("Gone Co", "Dev", source="mycareersfuture",
                  url="https://www.mycareersfuture.gov.sg/job/it/dev-0123456789abcdef0123456789abcdef")
    live = _ready("Live Co", "Dev", source="linkedin", url="https://www.linkedin.com/jobs/view/4300000001/")
    _ready("Street Co", "Dev", source="jobstreet", url="https://sg.jobstreet.com/job/1")  # needs a browser: not here
    client = TestClient(create_app())
    got = client.post("/api/ads/check", headers=SAME_ORIGIN).json()
    assert got[str(gone)]["state"] == "closed" and got[str(live)]["state"] == "open"
    assert len(asked) == 2
    assert client.post("/api/ads/check", headers=SAME_ORIGIN).json() == {}  # checked lately: not asked again
    assert len(asked) == 2
    html = client.get("/").text
    assert "Ad closed" in html
    page = client.get(f"/jobs/{gone}").text
    assert "This ad has closed." in page and "MyCareersFuture marks it Closed" in page
    assert db.get_job(gone)["state"] == "Ready"  # shown, never moved


def test_a_hunt_checks_jobstreet_ads_with_its_browser():
    jid = _ready("Street Co", "Dev", source="jobstreet", url="https://sg.jobstreet.com/job/1")
    _ready("Other Co", "Dev", source="jobstreet", url="https://sg.jobstreet.com/job/2")
    db.update_job(jid, state="Applied")  # not waiting for you: not checked

    class Ctx:
        urls = []

        def get_html(self, url):
            self.urls.append(url)
            return '{"isExpired":true}'
    logs, ctx = [], Ctx()
    assert adcheck.run(ctx, log=logs.append) == 1
    assert ctx.urls == ["https://sg.jobstreet.com/job/2"]
    assert any("Other Co - Dev has closed" in l for l in logs)


# ── No reply yet ──────────────────────────────────────────────────────

def test_no_reply_yet_after_two_weeks():
    old = (TODAY - timedelta(days=16)).isoformat()
    assert no_reply_days({"state": "Applied", "date_applied": old}, TODAY) == 16
    assert no_reply_days({"state": "Applied", "date_applied": (TODAY - timedelta(days=13)).isoformat()}, TODAY) == 0
    assert no_reply_days({"state": "Interview", "date_applied": old}, TODAY) == 0
    assert no_reply_days({"state": "Applied", "date_applied": ""}, TODAY) == 0


def test_applications_tags_and_filters_the_quiet_ones():
    quiet = _ready("Quiet Co", "Dev")
    db.update_job(quiet, state="Applied", date_applied=(date.today() - timedelta(days=20)).isoformat())
    fresh = _ready("Fresh Co", "Dev")
    db.update_job(fresh, state="Applied")
    client = TestClient(create_app())
    html = client.get("/applications").text
    assert "No reply yet · 20 days" in html and "No reply yet <b>1</b>" in html
    only = client.get("/applications?noreply=1").text
    assert "Quiet Co" in only and "Fresh Co" not in only


# ── newest first: stop a keyword at a page of known jobs ──────────────

class PagedSite:
    key, label, newest_first = "fake", "Fake board", True

    def __init__(self, pages):
        self.pages, self.loaded = pages, []

    def search(self, ctx, keyword, location, page_no):
        self.loaded.append((keyword, page_no))
        return [Listing(source="fake", url=u, title=f"Job {u}", company=f"Co {u}") for u in self.pages.get((keyword, page_no), [])]


def _task():
    return hunt.Task("hunt", {}, threading.Event())


def test_a_page_of_known_jobs_ends_that_keyword():
    for u in ("a1", "a2"):
        db.mark_seen(u, "fake", f"Co {u}", f"Job {u}", "skip")
    site = PagedSite({("python", 0): ["n1", "n2"], ("python", 1): ["a1", "a2"], ("python", 2): ["old"],
                      ("java", 0): ["a1", "j1"], ("java", 1): ["j2"]})
    stats = hunt.SiteStats()
    got = [l.url for l in hunt._site_listings(_task(), None, site, ["python", "java"], "", 5, stats)]
    assert site.loaded == [("python", 0), ("python", 1), ("java", 0), ("java", 1), ("java", 2)]
    assert got == ["n1", "n2", "a1", "a2", "a1", "j1", "j2"]
    assert stats.sites["fake"]["stopped_early"] == 1 and stats.sites["fake"]["pages"] == 5
    assert any("all 2 already known, so the older pages are skipped" in e["message"] for e in db.recent_events())


def test_a_site_not_sorted_by_date_reads_every_page():
    db.mark_seen("a1", "fake", "Co a1", "Job a1", "skip")
    site = PagedSite({("python", 0): ["a1"], ("python", 1): ["n1"]})
    site.newest_first = False
    list(hunt._site_listings(_task(), None, site, ["python"], "", 5))
    assert site.loaded == [("python", 0), ("python", 1), ("python", 2)]


def test_sites_say_whether_they_list_newest_first():
    assert sites.JobStreetSite.newest_first and sites.MyCareersFutureSite.newest_first and sites.IndeedSite.newest_first
    assert not getattr(sites.LinkedInSite, "newest_first", False)  # its public search ignores the sort

    class Ctx:
        def get_html(self, url):
            self.url = url
            return ""
    ctx = Ctx()
    sites.JobStreetSite().search(ctx, "python developer", "", 1)
    assert ctx.url.endswith("python-developer-jobs?page=2&sortmode=ListedDate")


# ── what each site found ──────────────────────────────────────────────

def test_site_stats_count_per_site_and_keyword():
    site = PagedSite({})
    st = hunt.SiteStats()
    for url, kw, what in (("a", "python", "known"), ("b", "python", "matched"), ("c", "java", "not_fit"),
                          ("d", "java", "low_pay")):
        listing = Listing(source="fake", url=url, title="t", extra={"keyword": kw})
        st.add(site, listing, "found")
        st.add(site, listing, what)
    row = st.sites["fake"]
    assert (row["found"], row["known"], row["matched"], row["not_fit"], row["low_pay"]) == (4, 1, 1, 1, 1)
    assert row["keywords"] == {"python": {"found": 2, "new": 1, "matched": 1}, "java": {"found": 2, "new": 2, "matched": 0}}
    assert st.lines() == ["Fake board: 4 found: 1 already known, 1 pay too low, 1 not a fit, 1 match."]


def test_hunt_page_shows_what_each_site_found():
    run = db.start_run("hunt", "{}")
    db.finish_run(run, "done", "0 package(s) built from 3 posting(s) examined (5 already known).")
    db.set_site_stats(run, json.dumps({"jobstreet": {
        "label": "JobStreet", "pages": 3, "stopped_early": 1, "found": 8, "known": 5, "avoid": 0, "low_pay": 1,
        "not_fit": 2, "unreadable": 0, "other": 0, "matched": 0,
        "keywords": {"python developer": {"found": 6, "new": 3, "matched": 0}, "rust": {"found": 2, "new": 0, "matched": 0}}}}))
    html = TestClient(create_app()).get("/hunt").text
    assert "What each site found" in html and "JobStreet" in html
    assert "5 already known, 1 pay too low, 2 not a fit" in html
    assert "1 keyword stopped early" in html
    assert "Nothing new for “rust” this time" in html


def test_a_practice_hunt_keeps_its_counts_and_posted_days(demo_server, secrets, candidate, fake_llm):
    settings.update({"demo_board_url": demo_server, "scope": {"keywords": ["engineer"], "location": "Singapore",
                                                              "avoid_words": ["intern"]},
                     "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0}, "apply": {"show_browser": False}})
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"], "auto_submit": False})
    runner.wait(180)
    assert db.get_run(run_id)["status"] == "done"
    stats = json.loads(db.get_run(run_id)["site_stats"])
    assert stats["demo"]["matched"] == 1 and stats["demo"]["keywords"]["engineer"]["matched"] == 1
    job = db.list_jobs()[0]
    assert job["posted_at"] == (date.today() - timedelta(days=1)).isoformat()  # the practice board's "1d ago"
    assert any(e["message"].startswith("Practice board: ") for e in db.recent_events())


# ── notifications ─────────────────────────────────────────────────────

def test_notifications_can_be_turned_off():
    client = TestClient(create_app())
    assert 'data-notify="1"' in client.get("/").text
    assert 'id="notifyBox" checked' in client.get("/settings").text
    client.post("/settings", data={"provider": settings.load()["provider"]}, headers=SAME_ORIGIN)
    assert settings.load()["notify"] is False
    assert 'data-notify="0"' in client.get("/").text
    js = (Path(paths.APP_DIR) / "jobhunt" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "new Notification(" in js and "requestPermission" in js
