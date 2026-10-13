"""Job-site crawlers: plain code that finds postings and pulls out their text.

Each site has two jobs:
  search(ctx, keyword, location, page_no) -> list[Listing]
  fetch(ctx, listing)                     -> Posting (clean text, no HTML)

`ctx` is a SiteContext: it loads pages in the shared browser (or calls a JSON
API) and waits a human-ish random delay before every page load.

Status of each site (also shown in the UI):
  demo            the built-in practice board; fully tested
  mycareersfuture public JSON API; checked against the live API on 2026-10-13
  linkedin        public "guest" job pages; written against known markup. Checked live on 2026-10-13
                  from the build server: LinkedIn answered 429 and its sign-in wall (now detected)
  jobstreet       SEEK data-automation markup; checked live on 2026-10-13 (search + postings,
                  headless bundled Chromium and real Chrome both fine)
  indeed          known markup; Indeed often blocks automated browsers (experimental)
"""
import random
import re
import time
from urllib.parse import quote, quote_plus, urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from .extract import Listing, Posting, clean_whitespace, generic_posting, html_to_text, select_text

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")


class SiteBlocked(Exception):
    """The site showed a captcha, a block page or a login wall; stop using it this run."""


class SiteContext:
    def __init__(self, browser=None, pacing: dict | None = None, stop_event=None, log=None, sleep=time.sleep):
        self.browser = browser
        self.pacing = pacing or {}
        self.stop_event = stop_event
        self.log = log or (lambda msg: None)
        self._sleep = sleep
        self._last_load = 0.0

    def wait_turn(self) -> None:
        lo = float(self.pacing.get("min_delay_s", 4))
        hi = max(lo, float(self.pacing.get("max_delay_s", 9)))
        target = self._last_load + random.uniform(lo, hi)
        while (remaining := target - time.monotonic()) > 0:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            self._sleep(min(0.25, remaining))
        self._last_load = time.monotonic()

    def get_html(self, url: str) -> str:
        self.wait_turn()
        html = self.browser.load_html(url)
        check_blocked(html, getattr(self.browser, "last_url", "") or url)
        return html

    def get_json(self, url: str, method: str = "GET", body: dict | None = None) -> dict:
        self.wait_turn()
        with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30) as c:
            r = c.post(url, json=body) if method == "POST" else c.get(url)
        if r.status_code in (403, 429):
            raise SiteBlocked(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()


_BLOCK_MARKERS = ["just a moment...", "verify you are human", "captcha", "access denied", "unusual traffic",
                  "are you a robot"]
# Sign-in walls: where a site sends you when it wants a login before showing jobs.
_LOGIN_URL_RE = re.compile(r"/(login|signin|sign-in|authwall|uas/login|checkpoint|oauth)\b", re.I)
_LOGIN_TITLE_MARKERS = ["log in or sign up", "sign in |", "sign in to", "log in to", "login |"]


def check_blocked(html: str, final_url: str = "") -> None:
    head = (html or "")[:6000].lower()
    soup_title = re.search(r"<title[^>]*>(.*?)</title>", head, re.S)
    title = soup_title.group(1).strip() if soup_title else ""
    if any(m in title for m in _BLOCK_MARKERS) or ("captcha" in head and "g-recaptcha" in head and len(html) < 20000):
        raise SiteBlocked(f"block page: {title[:80]!r}")
    path = urlparse(final_url or "").path
    if _LOGIN_URL_RE.search(path) or any(title.startswith(m) or m in title for m in _LOGIN_TITLE_MARKERS):
        raise SiteBlocked(f"sign-in wall: {title[:80]!r} at {path[:80]}")


def _abs(base: str, href: str) -> str:
    return urljoin(base, href or "")


# ── practice board (served by this app at /demo) ──────────────────────

class DemoSite:
    key = "demo"
    label = "Practice board"

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def search(self, ctx, keyword, location, page_no):
        url = f"{self.base}/search?q={quote_plus(keyword)}&l={quote_plus(location or '')}&page={page_no}"
        soup = BeautifulSoup(ctx.get_html(url), "html.parser")
        out = []
        for card in soup.select(".job-card"):
            a = card.select_one("a.job-link")
            if not a:
                continue
            out.append(Listing(source=self.key, url=_abs(url, a.get("href")), title=clean_whitespace(a.get_text(" ")),
                               company=select_text(str(card), ".company"), location=select_text(str(card), ".location")))
        return out

    def fetch(self, ctx, listing):
        html = ctx.get_html(listing.url)
        p = generic_posting(html, self.key, listing.url, body_selectors=[".job-description"])
        apply = BeautifulSoup(html, "html.parser").select_one("a#apply")
        p.apply_url = _abs(listing.url, apply.get("href")) if apply else listing.url
        return _fill_from_listing(p, listing)


# ── MyCareersFuture (public JSON API) ─────────────────────────────────

class MyCareersFutureSite:
    key = "mycareersfuture"
    label = "MyCareersFuture"
    API = "https://api.mycareersfuture.gov.sg/v2"

    def search(self, ctx, keyword, location, page_no):
        data = ctx.get_json(f"{self.API}/search?limit=20&page={page_no}", method="POST",
                            body={"search": keyword, "sortBy": ["new_posting_date"]})
        return [self.listing_from_result(r) for r in data.get("results", [])]

    def listing_from_result(self, r: dict) -> Listing:
        meta = r.get("metadata") or {}
        company = (r.get("hiringCompany") or {}).get("name") or (r.get("postedCompany") or {}).get("name", "")
        districts = (r.get("address") or {}).get("districts") or []
        return Listing(source=self.key, url=meta.get("jobDetailsUrl") or f"https://www.mycareersfuture.gov.sg/job/{r['uuid']}",
                       title=r.get("title", ""), company=company,
                       location=", ".join(d.get("location", "") for d in districts) or "Singapore",
                       extra={"uuid": r["uuid"]})

    def fetch(self, ctx, listing):
        r = ctx.get_json(f"{self.API}/jobs/{listing.extra['uuid']}")
        return self.posting_from_job(r, listing)

    def posting_from_job(self, r: dict, listing: Listing) -> Posting:
        sal = r.get("salary") or {}
        salary = ""
        if sal.get("minimum"):
            salary = f"SGD {sal['minimum']:,}-{sal.get('maximum') or sal['minimum']:,} {(sal.get('type') or {}).get('salaryType', '')}".strip()
        parts = [html_to_text(r.get("description", ""))]
        if r.get("minimumYearsExperience"):
            parts.append(f"Minimum years of experience: {r['minimumYearsExperience']}")
        skills = [s.get("skill") for s in r.get("skills") or [] if s.get("skill")]
        if skills:
            parts.append("Skills listed: " + ", ".join(skills))
        if salary:
            parts.append("Salary: " + salary)
        p = Posting(source=self.key, url=listing.url, title=r.get("title") or listing.title,
                    company=(r.get("hiringCompany") or {}).get("name") or (r.get("postedCompany") or {}).get("name") or listing.company,
                    location=listing.location, text="\n\n".join(parts), apply_url=listing.url, salary=salary)
        return p


# ── LinkedIn (public guest job pages) ─────────────────────────────────

class LinkedInSite:
    key = "linkedin"
    label = "LinkedIn"

    def search(self, ctx, keyword, location, page_no):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
               f"keywords={quote_plus(keyword)}&location={quote_plus(location or '')}&start={page_no * 10}")
        return self.parse_search(ctx.get_html(url))

    def parse_search(self, html: str) -> list:
        out = []
        for card in BeautifulSoup(html, "html.parser").select("div.base-card, li div.job-search-card"):
            urn = card.get("data-entity-urn", "")
            m = re.search(r"(\d{6,})", urn)
            link = card.select_one("a.base-card__full-link")
            if not m and link:
                m = re.search(r"-(\d{6,})", link.get("href", ""))
            if not m:
                continue
            job_id = m.group(1)
            out.append(Listing(source=self.key, url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                               title=select_text(str(card), ".base-search-card__title"),
                               company=select_text(str(card), ".base-search-card__subtitle"),
                               location=select_text(str(card), ".job-search-card__location"),
                               extra={"job_id": job_id}))
        return out

    def fetch(self, ctx, listing):
        html = ctx.get_html(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{listing.extra['job_id']}")
        return self.parse_posting(html, listing)

    def parse_posting(self, html: str, listing: Listing) -> Posting:
        body = BeautifulSoup(html, "html.parser").select_one(".show-more-less-html__markup, .description__text")
        p = Posting(source=self.key, url=listing.url,
                    title=select_text(html, ".top-card-layout__title") or listing.title,
                    company=select_text(html, ".topcard__org-name-link") or listing.company,
                    location=select_text(html, ".topcard__flavor--bullet") or listing.location,
                    text=html_to_text(str(body)) if body else "", apply_url=listing.url)
        return p


# ── JobStreet (SEEK markup) ───────────────────────────────────────────

class JobStreetSite:
    key = "jobstreet"
    label = "JobStreet"
    BASE = "https://sg.jobstreet.com"

    def search(self, ctx, keyword, location, page_no):
        slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
        url = f"{self.BASE}/{quote(slug)}-jobs"
        if location:
            url += f"/in-{quote(re.sub(r'[^A-Za-z0-9]+', '-', location).strip('-'))}"
        url += f"?page={page_no + 1}"
        return self.parse_search(ctx.get_html(url))

    def parse_search(self, html: str) -> list:
        out = []
        for card in BeautifulSoup(html, "html.parser").select("article[data-automation='normalJob'], article[data-card-type='JobCard']"):
            a = card.select_one("a[data-automation='jobTitle']")
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"/job/(\d+)", href)
            url = f"{self.BASE}/job/{m.group(1)}" if m else _abs(self.BASE, href.split("?")[0])
            out.append(Listing(source=self.key, url=url, title=clean_whitespace(a.get_text(" ")),
                               company=select_text(str(card), "[data-automation='jobCompany']"),
                               location=select_text(str(card), "[data-automation='jobLocation']")))
        return out

    def fetch(self, ctx, listing):
        return self.parse_posting(ctx.get_html(listing.url), listing)

    def parse_posting(self, html, listing):
        p = generic_posting(html, self.key, listing.url, body_selectors=["[data-automation='jobAdDetails']"],
                            title_sel="[data-automation='job-detail-title']",
                            company_sel="[data-automation='advertiser-name']",
                            location_sel="[data-automation='job-detail-location']")
        p.apply_url = listing.url
        return _fill_from_listing(p, listing)


# ── Indeed (experimental) ─────────────────────────────────────────────

class IndeedSite:
    key = "indeed"
    label = "Indeed (experimental)"
    BASE = "https://sg.indeed.com"

    def search(self, ctx, keyword, location, page_no):
        url = f"{self.BASE}/jobs?q={quote_plus(keyword)}&l={quote_plus(location or '')}&start={page_no * 10}"
        return self.parse_search(ctx.get_html(url))

    def parse_search(self, html):
        out = []
        for a in BeautifulSoup(html, "html.parser").select("a.jcs-JobTitle, h2.jobTitle a"):
            jk = a.get("data-jk") or (parse_qs(urlparse(a.get("href", "")).query).get("jk") or [""])[0]
            if not jk:
                continue
            card = a.find_parent(class_=re.compile("job_seen_beacon|cardOutline|result")) or a.parent
            out.append(Listing(source=self.key, url=f"{self.BASE}/viewjob?jk={jk}", title=clean_whitespace(a.get_text(" ")),
                               company=select_text(str(card), "[data-testid='company-name']"),
                               location=select_text(str(card), "[data-testid='text-location']")))
        return out

    def fetch(self, ctx, listing):
        html = ctx.get_html(listing.url)
        p = generic_posting(html, self.key, listing.url, body_selectors=["#jobDescriptionText"],
                            title_sel="h1", company_sel="[data-testid='inlineHeader-companyName']",
                            location_sel="[data-testid='inlineHeader-companyLocation']")
        p.apply_url = listing.url
        return _fill_from_listing(p, listing)


def _fill_from_listing(p: Posting, listing: Listing) -> Posting:
    p.title = p.title or listing.title
    p.company = p.company or listing.company
    p.location = p.location or listing.location
    return p


def get_site(key: str, settings: dict):
    if key == "demo":
        return DemoSite(settings.get("demo_board_url") or "http://127.0.0.1:8765/demo")
    return {"mycareersfuture": MyCareersFutureSite, "linkedin": LinkedInSite, "jobstreet": JobStreetSite,
            "indeed": IndeedSite}[key]()


SITE_STATUS = {
    "demo": "Built-in fake jobs for practice. Nothing leaves your PC.",
    "mycareersfuture": "Uses MyCareersFuture's public job search. Applying needs Singpass, so these jobs are handed to you.",
    "linkedin": "Reads LinkedIn's public job pages. LinkedIn rate-limits hard; if it shows its sign-in wall the hunt skips it.",
    "jobstreet": "Reads JobStreet search pages in the browser. Checked against the live site in Oct 2026.",
    "indeed": "Experimental: Indeed often blocks automated browsers.",
}
