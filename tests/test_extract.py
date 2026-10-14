"""Crawler extraction against saved pages (no network).

jobstreet_*.html are cut from real sg.jobstreet.com pages (2026-10-13);
mcf_*.json are cut from the real MyCareersFuture API; the LinkedIn pages are
synthetic (LinkedIn rate-limited the build machine). Indeed has its own tests: test_indeed.py.
"""
import json
from pathlib import Path

import pytest

from jobhunt import extract, sites
from jobhunt.extract import Listing

FIX = Path(__file__).resolve().parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


class FakeCtx:
    """Stands in for SiteContext: serves fixture pages instead of loading URLs."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.urls = []

    def get_html(self, url):
        self.urls.append(url)
        for key, html in self.pages.items():
            if key in url:
                sites.check_blocked(html, url)
                return html
        raise AssertionError(f"unexpected url {url}")

    def get_json(self, url, method="GET", body=None):
        self.urls.append(url)
        for key, data in self.pages.items():
            if key in url:
                return data
        raise AssertionError(f"unexpected url {url}")


def test_jobstreet_search_live_fixture():
    ctx = FakeCtx({"/software-engineer-jobs/in-Singapore?page=1": read("jobstreet_search.html")})
    listings = sites.JobStreetSite().search(ctx, "Software Engineer", "Singapore", 0)
    assert len(listings) == 3
    first = listings[0]
    assert first.url == "https://sg.jobstreet.com/job/95236588"
    assert first.title == "Software Engineer (Full stack with Automation)"
    assert first.company == "HOPE Technik Pte Ltd"
    assert first.location
    assert all(l.url.startswith("https://sg.jobstreet.com/job/") for l in listings)


def test_jobstreet_posting_live_fixture():
    listing = Listing("jobstreet", "https://sg.jobstreet.com/job/95236588", "fallback title", "fallback co", "West")
    p = sites.JobStreetSite().fetch(FakeCtx({"/job/95236588": read("jobstreet_posting.html")}), listing)
    assert p.title == "Software Engineer (Full stack with Automation)"
    assert p.company == "HOPE Technik Pte Ltd"
    assert len(p.text) > 1000
    assert "<" not in p.text[:200]  # clean text, not HTML
    assert p.apply_url == listing.url


def test_mycareersfuture_api_fixtures():
    search, job = json.loads(read("mcf_search.json")), json.loads(read("mcf_job.json"))
    site = sites.MyCareersFutureSite()
    ctx = FakeCtx({"/search": search, f"/jobs/{job['uuid']}": job})
    listings = site.search(ctx, "data analyst", "Singapore", 0)
    assert len(listings) == 2
    first = listings[0]
    assert first.title == job["title"]
    assert first.url.startswith("https://www.mycareersfuture.gov.sg/job/")
    p = site.fetch(ctx, first)
    assert p.salary.startswith("SGD 5,000-6,900")
    assert p.salary_max_monthly == 6900
    assert "Skills listed:" in p.text and "Minimum years of experience: 3" in p.text
    assert "<p>" not in p.text


def test_linkedin_guest_markup():
    site = sites.LinkedInSite()
    listings = site.parse_search(read("linkedin_search.html"))
    assert [l.extra["job_id"] for l in listings] == ["4012345678", "4098765432"]
    assert listings[0].title == "Backend Engineer"
    assert listings[0].company == "Harbourline Payments"
    assert listings[1].url == "https://www.linkedin.com/jobs/view/4098765432/"
    p = site.parse_posting(read("linkedin_posting.html"), listings[0])
    assert p.title == "Backend Engineer"
    assert "- 3+ years backend experience" in p.text
    assert "Show more" not in p.text


def test_html_to_text_keeps_bullets_and_drops_scripts():
    text = extract.html_to_text("<div><p>Hello&nbsp;there</p><script>evil()</script><ul><li>One</li><li>Two</li></ul></div>")
    assert text == "Hello there\n\n- One\n\n- Two" or text.replace("\n\n", "\n") == "Hello there\n- One\n- Two"
    assert "evil" not in text


def test_jsonld_posting_preferred():
    html = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"T","description":"<p>' + "x" * 100 +
            '</p>","hiringOrganization":{"name":"Co"},"jobLocation":{"address":{"addressLocality":"Singapore"}}}</script>'
            '<body><h1>Other</h1></body></html>')
    p = extract.generic_posting(html, "demo", "http://x")
    assert (p.title, p.company, p.location) == ("T", "Co", "Singapore")


@pytest.mark.parametrize("html,url", [
    ("<title>Just a moment...</title>", "https://x/jobs"),
    ("<title>Log In or Sign Up | LinkedIn</title>", "https://www.linkedin.com/jobs/search"),
    ("<title>Jobs</title>", "https://www.linkedin.com/authwall?trk=x"),
    ("<title>Jobs</title>", "https://sg.jobstreet.com/oauth/login?returnUrl=/"),
])
def test_block_and_login_walls_detected(html, url):
    with pytest.raises(sites.SiteBlocked):
        sites.check_blocked(html, url)


def test_saved_linkedin_signin_wall_is_detected():
    """The page LinkedIn really served this machine (429 + sign-in wall), cut to its title."""
    with pytest.raises(sites.SiteBlocked):
        sites.check_blocked("<html><head><title>Log In or Sign Up | LinkedIn</title></head><body></body></html>",
                            "https://www.linkedin.com/")


def test_normal_pages_not_flagged():
    sites.check_blocked(read("jobstreet_search.html"), "https://sg.jobstreet.com/software-engineer-jobs/in-Singapore")
    sites.check_blocked(read("jobstreet_posting.html"), "https://sg.jobstreet.com/job/95236588")


def test_site_context_paces_page_loads_and_checks_walls():
    class FakeBrowser:
        last_url = ""

        def load_html(self, url):
            self.last_url = "https://www.linkedin.com/authwall"
            return "<title>LinkedIn</title>"

    slept = []
    ctx = sites.SiteContext(FakeBrowser(), {"min_delay_s": 0.2, "max_delay_s": 0.2}, sleep=lambda s: slept.append(s))
    ctx._last_load = __import__("time").monotonic()
    with pytest.raises(sites.SiteBlocked):
        ctx.get_html("https://www.linkedin.com/jobs/search")
    assert slept, "a page load must wait its turn"
