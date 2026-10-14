"""Job-site crawlers: plain code that finds postings and pulls out their text.

Each site has two jobs:
  search(ctx, keyword, location, page_no) -> list[Listing]
  fetch(ctx, listing)                     -> Posting (clean text, no HTML)

`ctx` is a SiteContext: it loads pages in the shared browser (or calls a JSON
API) and waits a human-ish random delay before every page load.

Status of each site (also shown in the UI):
  demo            the built-in practice board; fully tested
  mycareersfuture public JSON API; checked against the live API on 2026-10-13
  linkedin        public "guest" job pages; search and postings checked live on 2026-10-14 without a
                  login. It answers 429 often: SiteContext waits and retries once before giving up
  jobstreet       SEEK data-automation markup; checked live on 2026-10-13 and 2026-10-14 (search,
                  postings and salaries; headless bundled Chromium and real Chrome both fine)
  indeed          the search page's own job-card data, and each card's job as the page loads it when
                  clicked; checked live on 2026-10-15 logged out (see the Indeed section below)
"""
import json
import random
import re
import time
from urllib.parse import quote, quote_plus, urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from .extract import (Listing, Posting, clean_whitespace, generic_posting, html_to_text, monthly, salary_top_monthly,
                      select_text)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")


class SiteBlocked(Exception):
    """The site showed a captcha, a block page or a login wall; stop using it this run."""


class PageUnavailable(Exception):
    """The page answered with an HTTP error (404, 400, 500...): skip it, but keep using the site."""


class SiteContext:
    def __init__(self, browser=None, pacing: dict | None = None, stop_event=None, log=None, sleep=time.sleep):
        self.browser = browser
        self.pacing = pacing or {}
        self.stop_event = stop_event
        self.log = log or (lambda msg: None)
        self._sleep = sleep
        self._last_load = 0.0

    def _pause_until(self, target: float) -> None:
        while (remaining := target - time.monotonic()) > 0:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            self._sleep(min(0.25, remaining))

    def wait_turn(self) -> None:
        lo = float(self.pacing.get("min_delay_s", 4))
        hi = max(lo, float(self.pacing.get("max_delay_s", 9)))
        self._pause_until(self._last_load + random.uniform(lo, hi))
        self._last_load = time.monotonic()

    def _slow_down(self, url: str) -> None:
        """The site answered 429 (too many requests): wait a while before the one retry."""
        wait = float(self.pacing.get("rate_limit_wait_s", 45))
        self.log(f"{urlparse(url).hostname} asked us to slow down (HTTP 429); waiting {wait:.0f}s before trying once more.")
        self._pause_until(time.monotonic() + wait)

    def get_html(self, url: str, tab: str | None = None) -> str:
        """`tab`: load it in a tab of the site's own, so the page stays open while other sites load theirs."""
        for attempt in (1, 2):
            self.wait_turn()
            html = self.browser.load_html(url, tab=tab) if tab else self.browser.load_html(url)
            status = getattr(self.browser, "last_status", 0) or 0
            if status == 429 and attempt == 1:
                self._slow_down(url)
                continue
            check_blocked(html, getattr(self.browser, "last_url", "") or url)
            if status in (403, 429):
                raise SiteBlocked(f"HTTP {status}")
            if status >= 400:
                raise PageUnavailable(f"HTTP {status}")
            return html

    def click_json(self, selector: str, wanted: list, tab: str | None = None):
        """Click something on the open page (after the usual pause) and return the JSON it fetched
        (see BrowserSession.click_for_response for `wanted`)."""
        self.wait_turn()
        text = self.browser.click_for_response(selector, wanted, tab=tab)
        status = getattr(self.browser, "last_status", 0) or 0
        if text.lstrip().startswith("<"):  # a page, not data: a block page, or the click fetched nothing
            check_blocked(text, getattr(self.browser, "last_url", ""))
        if status in (401, 403, 429):
            raise SiteBlocked(f"HTTP {status}")
        if status == 0 or status >= 400:
            raise PageUnavailable(f"HTTP {status}" if status else "the page didn't load it")
        try:
            return json.loads(text)
        except ValueError:
            raise PageUnavailable("not the data we expected") from None

    def get_json(self, url: str, method: str = "GET", body: dict | None = None) -> dict:
        for attempt in (1, 2):
            self.wait_turn()
            with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30) as c:
                r = c.post(url, json=body) if method == "POST" else c.get(url)
            if r.status_code == 429 and attempt == 1:
                self._slow_down(url)
                continue
            if r.status_code in (403, 429):
                raise SiteBlocked(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise PageUnavailable(f"HTTP {r.status_code}")
            return r.json()


_BLOCK_MARKERS = ["just a moment...", "verify you are human", "captcha", "access denied", "unusual traffic",
                  "are you a robot", "security check - indeed", "additional verification required"]
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
                               company=select_text(card, ".company"), location=select_text(card, ".location")))
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
        salary, salary_max = self.salary(r)
        return Listing(source=self.key, url=meta.get("jobDetailsUrl") or f"https://www.mycareersfuture.gov.sg/job/{r['uuid']}",
                       title=r.get("title", ""), company=company,
                       location=", ".join(d.get("location", "") for d in districts) or "Singapore",
                       salary=salary, salary_max_monthly=salary_max, extra={"uuid": r["uuid"]})

    @staticmethod
    def salary(r: dict) -> tuple[str, float]:
        """The API's salary block -> (display text, top of the range in SGD a month)."""
        sal = r.get("salary") or {}
        if not sal.get("minimum"):
            return "", 0.0
        period = (sal.get("type") or {}).get("salaryType", "")
        top = sal.get("maximum") or sal["minimum"]
        return f"SGD {sal['minimum']:,}-{top:,} {period}".strip(), monthly(float(top), period)

    def fetch(self, ctx, listing):
        r = ctx.get_json(f"{self.API}/jobs/{listing.extra['uuid']}")
        return self.posting_from_job(r, listing)

    def posting_from_job(self, r: dict, listing: Listing) -> Posting:
        salary, salary_max = self.salary(r)
        parts = [html_to_text(r.get("description", ""))]
        if r.get("minimumYearsExperience"):
            parts.append(f"Minimum years of experience: {r['minimumYearsExperience']}")
        kinds = [e.get("employmentType") for e in r.get("employmentTypes") or [] if isinstance(e, dict)]
        if any(kinds):
            parts.append("Employment type: " + ", ".join(k for k in kinds if k))
        skills = [s.get("skill") for s in r.get("skills") or [] if s.get("skill")]
        if skills:
            parts.append("Skills listed: " + ", ".join(skills))
        if salary:
            parts.append("Salary: " + salary)
        p = Posting(source=self.key, url=listing.url, title=r.get("title") or listing.title,
                    company=(r.get("hiringCompany") or {}).get("name") or (r.get("postedCompany") or {}).get("name") or listing.company,
                    location=listing.location, text="\n\n".join(parts), apply_url=listing.url, salary=salary,
                    salary_max_monthly=salary_max)
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
            salary = select_text(card, ".job-search-card__salary-info")
            out.append(Listing(source=self.key, url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                               title=select_text(card, ".base-search-card__title"),
                               company=select_text(card, ".base-search-card__subtitle"),
                               location=select_text(card, ".job-search-card__location"),
                               salary=salary, salary_max_monthly=salary_top_monthly(salary),
                               extra={"job_id": job_id}))
        return out

    def fetch(self, ctx, listing):
        html = ctx.get_html(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{listing.extra['job_id']}")
        return self.parse_posting(html, listing)

    def parse_posting(self, html: str, listing: Listing) -> Posting:
        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one(".show-more-less-html__markup, .description__text")
        text = html_to_text(str(body)) if body else ""
        # "Seniority level: Associate", "Employment type: Full-time"...: useful to the filter
        criteria = [f"{select_text(li, '.description__job-criteria-subheader')}: "
                    f"{select_text(li, '.description__job-criteria-text')}"
                    for li in soup.select(".description__job-criteria-item")]
        if text and criteria:
            text += "\n\n" + "\n".join(criteria)
        salary = select_text(soup, ".compensation__salary")
        p = Posting(source=self.key, url=listing.url,
                    title=select_text(soup, ".top-card-layout__title") or listing.title,
                    company=select_text(soup, ".topcard__org-name-link") or listing.company,
                    location=select_text(soup, ".topcard__flavor--bullet") or listing.location,
                    text=text, apply_url=listing.url, salary=salary, salary_max_monthly=salary_top_monthly(salary))
        return _fill_from_listing(p, listing)


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
            salary = select_text(card, "[data-automation='jobSalary']")
            out.append(Listing(source=self.key, url=url, title=clean_whitespace(a.get_text(" ")),
                               company=select_text(card, "[data-automation='jobCompany']"),
                               location=select_text(card, "[data-automation='jobLocation']"),
                               salary=salary, salary_max_monthly=salary_top_monthly(salary)))
        return out

    def fetch(self, ctx, listing):
        return self.parse_posting(ctx.get_html(listing.url), listing)

    def parse_posting(self, html, listing):
        p = generic_posting(html, self.key, listing.url, body_selectors=["[data-automation='jobAdDetails']"],
                            title_sel="[data-automation='job-detail-title']",
                            company_sel="[data-automation='advertiser-name']",
                            location_sel="[data-automation='job-detail-location']")
        # The JSON-LD has no salary or work type and only gives the country; the page header has the
        # rest. Keep the country too: "Central Region" alone once read to the AI as Hong Kong.
        soup = BeautifulSoup(html, "html.parser")
        area, country = select_text(soup, "[data-automation='job-detail-location']"), p.location
        if area and country and country.lower() not in area.lower():
            area = f"{area}, {country}"
        p.location = area or country
        salary = select_text(soup, "[data-automation='job-detail-salary']")
        if salary:  # else keep a JSON-LD salary, should SEEK start sending one
            p.salary, p.salary_max_monthly = salary, salary_top_monthly(salary)
        work_type = select_text(soup, "[data-automation='job-detail-work-type']")
        if p.text and work_type:
            p.text += f"\n\nWork type: {work_type}"
        p.apply_url = listing.url
        return _fill_from_listing(p, listing)


# ── Indeed ────────────────────────────────────────────────────────────
# Checked live from Singapore on 2026-10-15, logged out. Indeed sits behind Cloudflare: plain HTTP gets
# a "Security Check". A browser without a window gets one search page, then a human check that sticks
# to the browser profile; a real window searches keyword after keyword. Logged out, page 2 of a search
# and a posting page (/viewjob) both go to the sign-in page. So a search loads in a tab of its own and
# each description is read the way a visitor reads it: click the card, and the page fetches that job
# into its right-hand pane. Indeed runs two versions of the page (which one depends on the browser
# profile): one fetches viewjob?jk=...&viewtype=embedded (JSON), the other asks apis.indeed.com/graphql
# for "Viewjob". Both are read.

INDEED_HELP = ("Indeed wants a human check. On the Accounts page press Log in next to Indeed, tick 'Verify you "
               "are human' in the Indeed tab yourself (logging in helps too), close the window, then hunt again")
# Each search page carries one hidden trap card for robots, with a made-up key. Seen live:
# abcdef0123456789, 789abcdef0123456 (runs of hex digits) and a1b2c3d4e5f67890.
_DECOY_RUN = "0123456789abcdef" * 2
_DECOY_RE = re.compile(r"a1b2c3d4e5f6")


def indeed_job_key(jk) -> str:
    """A real Indeed job key, or '' (16 hex digits, and not one of the trap cards' made-up keys)."""
    jk = str(jk or "")
    if not re.fullmatch(r"[0-9a-f]{16}", jk) or jk in _DECOY_RUN or _DECOY_RE.match(jk):
        return ""
    return jk


def _mosaic_results(html: str):
    """The job cards' own data the search page carries (None if it isn't there)."""
    m = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*', html or "")
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
        model = data["metaData"]["mosaicProviderJobCardsModel"]
        return model.get("results") or [], bool(model.get("loggedIn"))
    except (ValueError, KeyError, TypeError):
        return None


class IndeedSite:
    key = "indeed"
    label = "Indeed"
    BASE = "https://sg.indeed.com"
    TAB = "indeed"  # the search page stays open here while other sites load theirs
    needs_window = True  # see above: the hunt's browser runs as a real (minimized) window

    def __init__(self):
        self.logged_in = False

    @property
    def page_limit(self) -> int:
        """Search pages per keyword: logged out, page 2 on is behind Indeed's sign-in page."""
        return 99 if self.logged_in else 1

    def search(self, ctx, keyword, location, page_no):
        # Newest first: logged out a hunt sees one page per keyword, and the next hunt should find new jobs on it.
        url = f"{self.BASE}/jobs?q={quote_plus(keyword)}&l={quote_plus(location or '')}&sort=date"
        if page_no:
            url += f"&start={page_no * 10}"
        try:
            html = ctx.get_html(url, tab=self.TAB)
        except SiteBlocked as e:
            raise SiteBlocked(f"{e}. {INDEED_HELP}") from None
        found = _mosaic_results(html)
        if found:
            self.logged_in = found[1]
        return self.parse_search(html)

    def parse_search(self, html):
        found = _mosaic_results(html)
        if found is not None:
            return [self.listing_from_result(r) for r in found[0] if indeed_job_key(r.get("jobkey"))]
        out = []  # no card data: read the cards, skipping hidden ones (a visitor never sees those)
        for a in BeautifulSoup(html, "html.parser").select("a.jcs-JobTitle, h2.jobTitle a"):
            jk = indeed_job_key(a.get("data-jk") or (parse_qs(urlparse(a.get("href", "")).query).get("jk") or [""])[0])
            if not jk or a.find_parent(attrs={"aria-hidden": "true"}) or a.find_parent(style=re.compile(r"display:\s*none")):
                continue
            card = a.find_parent(class_=re.compile("job_seen_beacon|cardOutline|result")) or a.parent
            out.append(Listing(source=self.key, url=f"{self.BASE}/viewjob?jk={jk}", title=clean_whitespace(a.get_text(" ")),
                               company=select_text(card, "[data-testid='company-name']"),
                               location=select_text(card, "[data-testid='text-location']")))
        return out

    def listing_from_result(self, r: dict) -> Listing:
        salary = clean_whitespace((r.get("salarySnippet") or {}).get("text") or "")
        return Listing(source=self.key, url=f"{self.BASE}/viewjob?jk={r['jobkey']}",
                       title=clean_whitespace(r.get("displayTitle") or r.get("title") or ""),
                       company=clean_whitespace(r.get("company") or ""),
                       location=clean_whitespace(r.get("formattedLocation") or ""),
                       salary=salary, salary_max_monthly=salary_top_monthly(salary))

    def fetch(self, ctx, listing):
        jk = indeed_job_key((parse_qs(urlparse(listing.url).query).get("jk") or [""])[0])
        if not jk:
            raise PageUnavailable("not an Indeed job link")
        try:
            data = ctx.click_json(f"a[data-jk='{jk}']", [f"/viewjob?jk={jk}", ('"operationName":"Viewjob"', jk)],
                                  tab=self.TAB)
        except LookupError:
            raise PageUnavailable("no longer on the open search page") from None
        except SiteBlocked as e:
            raise SiteBlocked(f"{e}. {INDEED_HELP}") from None
        return self.posting_from_job(data, listing)

    def posting_from_job(self, data, listing: Listing) -> Posting:
        """The job a card click fetched, in either of Indeed's two shapes, as a Posting."""
        jk = (parse_qs(urlparse(listing.url).query).get("jk") or [""])[0]
        body = data.get("body") if isinstance(data, dict) else None
        graph = next((d["data"]["viewjob"] for d in (data if isinstance(data, list) else [])
                      if isinstance(d, dict) and isinstance(d.get("data"), dict)
                      and isinstance(d["data"].get("viewjob"), dict)), None)
        if isinstance(body, dict) and body.get("jobKey", jk) == jk:  # viewjob?...&viewtype=embedded
            info = (body.get("jobInfoWrapperModel") or {}).get("jobInfoModel") or {}
            head = info.get("jobInfoHeaderModel") or {}
            title, company, location = head.get("jobTitle"), head.get("companyName"), head.get("formattedLocation")
            html, job_type = info.get("sanitizedJobDescription"), (info.get("jobMetadataHeaderModel") or {}).get("jobType")
            salary = (body.get("salaryInfoModel") or {}).get("salaryText")
        elif graph is not None and graph.get("key") == jk:  # graphql "Viewjob"
            job, details = graph.get("job") or {}, graph.get("details") or {}
            title, company = job.get("title"), job.get("sourceEmployerName")
            location = ((job.get("location") or {}).get("formatted") or {}).get("long")
            html = details.get("sanitizedJobDescription") or (job.get("description") or {}).get("html")
            job_type = ", ".join((details.get("jobTypeAndShiftSchedule") or {}).get("jobTypes") or [])
            salary = (job.get("compensation") or {}).get("formattedText")
        else:  # not this card's job: never read another job as this one
            raise PageUnavailable("not the job data we expected")
        text = html_to_text(html or "")
        if text and job_type:
            text += f"\n\nJob type: {job_type}"
        salary = clean_whitespace(salary or "")
        p = Posting(source=self.key, url=listing.url, title=clean_whitespace(title or ""),
                    company=clean_whitespace(company or ""), location=clean_whitespace(location or ""), text=text,
                    apply_url=listing.url, salary=salary, salary_max_monthly=salary_top_monthly(salary))
        return _fill_from_listing(p, listing)


def _fill_from_listing(p: Posting, listing: Listing) -> Posting:
    p.title = p.title or listing.title
    p.company = p.company or listing.company
    p.location = p.location or listing.location
    if not p.salary and listing.salary:
        p.salary, p.salary_max_monthly = listing.salary, listing.salary_max_monthly
    return p


def get_site(key: str, settings: dict):
    if key == "demo":
        return DemoSite(settings.get("demo_board_url") or "http://127.0.0.1:8765/demo")
    return {"mycareersfuture": MyCareersFutureSite, "linkedin": LinkedInSite, "jobstreet": JobStreetSite,
            "indeed": IndeedSite}[key]()


SITE_STATUS = {
    "demo": "Built-in fake jobs for practice. Nothing leaves your PC.",
    "mycareersfuture": "Uses MyCareersFuture's public job search. Applying needs Singpass, so every job from here "
                       "comes back to you as Apply yourself.",
    "linkedin": "Reads LinkedIn's public job pages. LinkedIn rate-limits hard; if it shows its sign-in wall the hunt skips it.",
    "jobstreet": "Reads JobStreet search pages in the browser. Checked against the live site in Oct 2026.",
    "indeed": "Reads the newest 15 Indeed jobs for each keyword (more pages if you log in to Indeed). While it "
              "searches, JobHunt's browser opens as a window on your taskbar: Indeed turns away hidden browsers. If "
              "Indeed asks you to prove you're human, the hunt skips it and tells you how to tick the check yourself.",
}
