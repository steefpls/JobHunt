"""The company summary on every package: what the company does, where it and the job are, how big it is,
and anything notable (a recruiter posting for a client it doesn't name, say).

Every fact comes from a page JobHunt read for this job, never from the AI's own knowledge:
  S1  the posting itself (already in the tracker; no page load)
  S2  the board's own record of the company, where it can be read without logging in:
        MyCareersFuture  the job's API record: company description, industry, staff count, website, job address
        JobStreet        its company page (/companies/<name>-<id>): website, size, address, overview
        Practice board   its company page, laid out like JobStreet's
        LinkedIn         not read: its company pages send a visitor who isn't logged in to a sign-in wall
                         (seen live from Singapore on 2026-10-15)
        Indeed           not read: its company pages sit behind the human check, and Indeed is only ever
                         read in a window (sites.IndeedSite), never in the hidden browser a lookup uses
  S3+ Singapore's company registry (ACRA open data on data.gov.sg): registered name, UEN, status, registered
      address, registered business activity, incorporation date; only for one clear match (company_sources.py)
      the company's own website: its home page and its contact (or about) page. The address comes from the
      board, from a link or email address in the posting that carries the company's name, from Wikidata, from
      one web search (a result whose address carries the company's name), or else from the name itself
      (<name>.com.sg, .sg, .com); a found or guessed site must name the company and Singapore (or its
      registered postal code or UEN)
      Wikipedia, for larger companies: only an article that is clearly this company, or the group a regional
      subsidiary ('X South East Asia Pte Ltd') belongs to

A page with a robot check or a sign-in wall is skipped, never worked around. Job boards are paced like a hunt
(SiteContext.wait_turn); company websites load in a separate throwaway browser with no saved logins.

One cheap AI call (the filter model) writes the summary. For each fact it must name the source and quote its
exact words; code then checks every quote against that page and drops a fact whose quote isn't there, or whose
numbers or place names aren't in the quote, so a location, size or headquarters can't be made up: it shows
"Not found". Nothing here can fail or hold up a package: every problem is caught and shown on the job page.

The summary is about the company, from the company's own sources first: "What they do" from the posting is shown
as "From the job posting only". On a recruiter posting the company is the agency ("Posted by"), and what the
posting says about the client it hires for is kept apart ("Hiring for"), so the client is never described as the
agency. "Where the job is" is the board's own location field, credited to the posting.
"""
import ipaddress
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import company_sources as more, db, paths, prompts
from .extract import clean_whitespace, find_jsonld_jobposting, html_to_text, main_text

# What the summary holds, in the order the job page shows it.
FIELDS = [("what_they_do", "What they do"), ("industry", "Industry"), ("job_location", "Where the job is"),
          ("company_location", "Company's office"), ("hq_country", "Headquarters"), ("size", "Size"),
          ("registered", "Registered as")]
PLACE_FIELDS = {"job_location", "company_location", "hq_country"}
CODE_FIELDS = {"registered"}  # made by code from the registry record, never by the AI
REST_S = 120  # a source that said "too many requests" is left alone this long
MAX_PAGE_LOADS = 5  # pages one lookup may load (board pages and website pages together)
CAPS = {"posting": 3500, "board": 3000, "web": 2500, "registry": 1200, "wiki": 1500}
NOT_FOUND = "Not found"

# Never a company's own website: job boards, application systems, social sites, link shorteners, webmail.
NOT_COMPANY_SITES = (
    "linkedin.com", "jobstreet.com", "jobsdb.com", "indeed.com", "mycareersfuture.gov.sg", "glassdoor.com",
    "jobscentral.com.sg", "foundit.sg", "monster.com", "careerjet", "jora.com", "seek.com",
    "myworkdayjobs.com", "workday.com", "greenhouse.io", "lever.co", "smartrecruiters.com", "taleo.net",
    "successfactors.com", "icims.com", "bamboohr.com", "recruitee.com", "workable.com", "ashbyhq.com",
    "breezy.hr", "jobvite.com", "zohorecruit.com", "applytojob.com", "freshteam.com", "hire.lever.co",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com", "wa.me", "t.me",
    "bit.ly", "tinyurl.com", "forms.gle", "google.com", "goo.gl", "gmail.com", "googlemail.com", "yahoo.com",
    "hotmail.com", "outlook.com", "live.com", "icloud.com", "singnet.com.sg", "gov.sg/singpass",
    "tafep.sg", "mom.gov.sg", "wsg.gov.sg", "schema.org", "w3.org",
)
_LEGAL_WORDS = {"pte", "ltd", "limited", "private", "llp", "llc", "inc", "incorporated", "corp", "corporation", "co",
                "company", "plc", "gmbh", "bhd", "sdn", "pty", "the", "sg", "singapore", "asia", "pacific",
                "holdings", "group", "international", "and"}
# A recruiter writing for an employer it doesn't name. EA = Singapore's employment agency licence.
# Only strong signs: "our clients" (plural) is how any consultancy talks about its customers, and a bare
# R1234567 is as likely a requisition number as an EA personnel number.
_AGENCY_RE = re.compile(r"\b(our (?:esteemed |valued )?client\b|on behalf of (?:our|a|the) client\b|"
                        r"EA\s*(?:licen[cs]e\b|reg(?:istration)?\b\.?|no\b\.?|personnel\b)|"
                        r"recruitment (?:agency|firm|consultancy)\b|employment agency\b|\b\d{2}C\d{4}\b)", re.I)
_NOT_BEFORE = re.compile(r"\b(?:not|never|nor)\s+(?:an?\s+)?$", re.I)


def agency_sign(text: str):
    """The first strong sign that a recruiter wrote this posting ("We are not a recruitment agency" isn't one)."""
    for m in _AGENCY_RE.finditer(text or ""):
        if not _NOT_BEFORE.search(text[max(0, m.start() - 20):m.start()]):
            return m
    return None


@dataclass
class Source:
    id: str
    label: str
    url: str
    text: str
    kind: str = ""  # posting, board, registry, website, wikipedia
    meta: dict = field(default_factory=dict)


# ── reading the pages ─────────────────────────────────────────────────

def posting_source_text(job: dict) -> str:
    head = f"Title: {job.get('role', '')}\nCompany: {job.get('company', '')}\nLocation: {job.get('location', '')}\n"
    if job.get("salary"):
        head += f"Salary: {job['salary']}\n"
    return head + "\n" + (job.get("posting_text") or "")


def name_core(company: str) -> list[str]:
    """'HOPE Technik Pte. Ltd.' -> ['hope', 'technik']: the words that make the name, without the legal bits."""
    words = re.findall(r"[a-z0-9]+", (company or "").lower().replace("&", " and "))
    core = [w for w in words if w not in _LEGAL_WORDS]
    return core or words


# Words a long legal name often ends with that the company's web address leaves out:
# 'Applied Materials South East Asia' is appliedmaterials.com, 'Albert Hoo Electrical' alberthoo.com.sg.
_TAIL_WORDS = {"south", "east", "southeast", "west", "north", "electrical", "engineering", "services", "service",
               "technologies", "technology", "solutions", "enterprise", "enterprises", "trading", "industries",
               "industrial", "consultancy", "consulting", "systems", "manufacturing", "logistics", "construction"}


def short_names(company: str) -> list[str]:
    """The joined name, then the shorter one without those tail words (if it is still at least 6 letters)."""
    core = name_core(company)
    names = ["".join(core)]
    while len(core) > 1 and core[-1] in _TAIL_WORDS:
        core = core[:-1]
    if len("".join(core)) >= 6 and "".join(core) not in names:
        names.append("".join(core))
    return names


def domain_matches_name(host: str, company: str) -> bool:
    """Does a web address look like this company's own? hopetechnik.com for HOPE Technik, ntu.edu.sg for
    Nanyang Technological University."""
    labels = [l for l in (host or "").lower().removeprefix("www.").split(".") if l]
    if len(labels) < 2:
        return False
    core = name_core(company)
    joined = "".join(core)
    initials = "".join(w[0] for w in core) if len(core) >= 3 else ""
    for label in labels[:-1]:  # every part but the country/top-level ending
        label = label.replace("-", "")
        if len(label) < 3 or label in ("com", "edu", "gov", "org", "net", "co"):
            continue
        if (len(joined) >= 4 and (joined in label or (len(label) >= 4 and label in joined))) or label == initials \
                or (core and len(core[0]) >= 4 and label.startswith(core[0])):
            return True
    return False


def not_company_site(host: str) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in NOT_COMPANY_SITES)


def host_of(url: str) -> str:
    """The host name of a web address ('' if it has none or is malformed: 'http://[abc' raises in urlparse)."""
    url = (url or "").strip()
    try:
        return (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return ""


def public_address(url: str) -> str:
    """The URL if it's an ordinary website on the internet, else ''. Never this PC, the home network or
    JobHunt itself (a posting could name http://127.0.0.1:8765/...)."""
    url = (url or "").strip()
    if url.lower().startswith("www."):
        url = "http://" + url
    try:
        u = urlparse(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:  # 'http://[abc', 'http://x:99999'
        return ""
    if u.scheme not in ("http", "https") or not host:
        return ""
    if host == "localhost" or host.endswith((".local", ".internal", ".lan", ".home", ".localhost")):
        return ""
    try:  # an address written as numbers: no lookup needed
        return url if ipaddress.ip_address(host.strip("[]")).is_global else ""
    except ValueError:
        pass
    try:
        infos = resolve(host, port or (443 if u.scheme == "https" else 80))
    except (OSError, UnicodeError, ValueError):
        return ""
    addresses = {i[4][0] for i in infos}
    try:
        if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
            return ""
    except ValueError:
        return ""
    return url


def resolve(host: str, port: int, timeout: float = 5.0) -> list:
    """DNS lookup, given up after `timeout` seconds (the tests swap this, so they never reach the internet)."""
    box: list = []

    def look():
        try:
            box.append(socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP))
        except Exception as e:
            box.append(e)
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    if not box:
        raise OSError(f"no answer for {host} within {timeout:.0f}s")
    if isinstance(box[0], Exception):
        raise box[0]
    return box[0]


def links_in_text(text: str) -> list[str]:
    """Web addresses and email domains a posting mentions, in order."""
    out = []
    for m in re.finditer(r"(https?://[^\s<>\"')\]]+|\bwww\.[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>\"')\]]*|"
                         r"[\w.+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+))", text or "", re.I):
        if m.group(2):  # an email address: its domain
            out.append("https://www." + m.group(2).lower().removeprefix("www."))
        else:
            link = m.group(1).rstrip(".,;:")
            out.append("https://" + link if link.lower().startswith("www.") else link)
    return list(dict.fromkeys(out))


_FACT_HEADINGS = re.compile(r"(website|company size|size|primary location|location|industry|industries|"
                            r"specialities|specialties|headquarters|founded|company type)", re.I)


def _labelled_values(soup) -> dict:
    """'Website' / 'Company size' / 'Primary location' headings on a JobStreet company page -> their values.
    (The page's other headings are job cards and reviews: not facts about the company.)"""
    out = {}
    for h in soup.find_all("h3"):
        label = clean_whitespace(h.get_text(" "))
        if not _FACT_HEADINGS.fullmatch(label):
            continue
        box = h.parent.find_next_sibling() if h.parent else None
        value = clean_whitespace(box.get_text(" ")) if box else ""
        if label and value and label not in out:
            out[label] = value[:300]
    return out


def parse_board_company_page(html: str, base_url: str) -> tuple[str, str]:
    """A JobStreet (or practice board) company page -> (its facts as text, the company's website or '')."""
    soup = BeautifulSoup(html or "", "html.parser")
    site = soup.select_one("a#website-value, a.website")
    try:
        website = urljoin(base_url, site.get("href", "")) if site and site.get("href") else ""
    except ValueError:
        website = ""
    facts = _labelled_values(soup)
    lines = [f"{k}: {v}" for k, v in facts.items() if k.lower() != "website"]
    if website:
        lines.append(f"Website: {website}")
    about = soup.select_one("[data-testid='readmore-measure-full'], [data-automation='company-overview'], .company-overview")
    text = html_to_text(str(about)) if about else ""
    if not text:  # the longest run of plain paragraphs on the page
        paras = sorted((clean_whitespace(p.get_text(" ")) for p in soup.find_all("p")), key=len, reverse=True)
        text = "\n".join(p for p in paras[:3] if len(p) > 80)
    title = clean_whitespace(soup.title.get_text(" ")) if soup.title else ""
    return clean_whitespace("\n".join(filter(None, [title, *lines, "", text]))), website


def mcf_record_text(r: dict) -> tuple[str, str]:
    """MyCareersFuture's job record -> (what it says about the company and the workplace, the website or '')."""
    posted, hiring = r.get("postedCompany") or {}, r.get("hiringCompany") or {}
    lines = []
    if posted.get("name"):
        lines.append(f"Posted by: {posted['name']}")
    if hiring.get("name") and hiring.get("name") != posted.get("name"):
        lines.append(f"Hiring company (the employer this was posted for): {hiring['name']}")
    company = hiring if hiring.get("name") else posted
    industry = company.get("ssicDescription2020") or company.get("ssicDescription") or ""
    if industry:
        lines.append(f"Registered business activity: {industry.title()}")
    if company.get("employeeCount"):
        lines.append(f"Employees: {company['employeeCount']}")
    website = str(company.get("companyUrl") or posted.get("companyUrl") or "").strip()
    if website:
        lines.append(f"Website: {website}")
    addr = r.get("address") or {}
    if addr.get("isOverseas"):
        place = ", ".join(filter(None, [addr.get("foreignAddress1"), addr.get("foreignAddress2"), addr.get("overseasCountry")]))
    else:
        street = " ".join(filter(None, [addr.get("block"), addr.get("street")]))
        place = ", ".join(filter(None, [street, addr.get("building"),
                                        f"Singapore {addr['postalCode']}" if addr.get("postalCode") else ""]))
    if place:
        lines.append(f"Workplace address: {place}")
    districts = [d.get("location") for d in addr.get("districts") or [] if d.get("location")]
    if districts:
        lines.append("District: " + "; ".join(districts))
    for c in (posted, hiring):
        if c.get("description"):
            lines.append(f"\nAbout {c.get('name') or 'the company'}, in its own words:\n" + html_to_text(c["description"]))
    return "\n".join(lines), website


def website_text(html: str) -> str:
    """A company web page as text, footer included (that's where the address usually is)."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = clean_whitespace(soup.title.get_text(" ")) if soup.title else ""
    feet = "\n".join(html_to_text(f.decode_contents()) for f in soup.find_all("footer"))
    return clean_whitespace("\n".join(filter(None, [title, main_text(html), feet])))


def _page_name(url: str) -> str:
    """'/contact', or '/?page_id=136' for a site whose pages are numbered."""
    p = urlparse(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def _same_page(a: str, b: str) -> bool:
    def key(u):
        p = urlparse(u)
        return ((p.hostname or "").removeprefix("www."), p.path.rstrip("/"), p.query)
    try:
        return key(a) == key(b)
    except ValueError:
        return False


def subpage_link(html: str, base_url: str) -> str:
    """The site's contact page (where the address is), else its about page; same site only."""
    host = host_of(base_url).removeprefix("www.")
    found = {}
    for a in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
        try:
            url = urljoin(base_url, a["href"]).split("#")[0]
            path = urlparse(url).path
        except ValueError:
            continue
        if host_of(url).removeprefix("www.") != host or not (path.strip("/") or urlparse(url).query) \
                or _same_page(url, base_url):
            continue
        words = f"{a.get_text(' ')} {path}".lower()
        for kind in ("contact", "about"):
            if kind in words and kind not in found:
                found[kind] = url
    return found.get("contact") or found.get("about") or ""


def looks_like(html_text: str, company: str, guessed: bool, confirm: tuple = ()) -> bool:
    """Is this page the company's own? A guessed (or searched-for) address must name the company and Singapore,
    or the company's registered postal code or UEN (`confirm`)."""
    flat = re.sub(r"[^a-z0-9]+", "", (html_text or "").lower())
    names = [n for n in short_names(company) if n]
    if not names or not any(n in flat for n in names):
        return False
    low = (html_text or "").lower()
    if re.search(r"domain (is|may be) for sale|buy this domain|parked free|this domain has expired", low):
        return False
    return not guessed or "singapore" in low or any(c and re.search(rf"\b{re.escape(c.lower())}\b", low) for c in confirm)


class Reader:
    """Loads at most MAX_PAGE_LOADS pages for one lookup, and notes every page it had to skip."""

    def __init__(self, ctx, practice_base: str = ""):
        self.ctx, self.loads, self.skipped = ctx, 0, []
        self.blocked_sites: set = set()  # websites that showed a robot check in this lookup: not asked again
        # boards that showed a robot check: kept on the hunt's SiteContext, so later packages leave them alone
        self.blocked = getattr(ctx, "blocked_hosts", None)
        if self.blocked is None:
            self.blocked = set()
            try:
                ctx.blocked_hosts = self.blocked
            except AttributeError:
                pass
        # the registry, search or Wikipedia that said "too many requests": when to ask it again (kept on the
        # hunt's SiteContext, like `blocked`)
        self.resting = getattr(ctx, "company_resting", None)
        if self.resting is None:
            self.resting = {}
            try:
                ctx.company_resting = self.resting
            except AttributeError:
                pass
        # a practice-board job: only the practice board's own pages are opened (nothing leaves the PC)
        self.practice_base = practice_base.rstrip("/") + "/" if practice_base else ""

    def address(self, url: str) -> str:
        if self.practice_base:
            return url if url.startswith(self.practice_base) else ""
        return public_address(url)

    def stopped(self) -> bool:
        ev = getattr(self.ctx, "stop_event", None)
        return bool(ev is not None and ev.is_set())

    def page(self, url: str, what: str, board: bool) -> str | None:
        from .sites import PageUnavailable, SiteBlocked
        if self.stopped() or self.loads >= MAX_PAGE_LOADS:
            return None
        if board and host_of(url) in self.blocked:
            self.skipped.append(f"{what}: {host_of(url)} showed a robot check earlier, so it was left alone")
            return None
        self.loads += 1
        try:
            if board:
                return self.ctx.get_html(url)
            return self.ctx.get_html(url, isolated=True, allow=lambda u: bool(self.address(u)))
        except PermissionError as e:
            self.skipped.append(f"{what}: {e}, which is on this PC or the home network, so it wasn't read")
        except SiteBlocked as e:
            if board:  # once a board shows a robot check, no more of its company pages this hunt
                self.blocked.add(host_of(url))
            else:
                self.blocked_sites.add(host_of(url).removeprefix("www."))
            self.skipped.append(f"{what}: it showed a robot check or a sign-in page, so it was skipped ({str(e)[:80]})")
        except PageUnavailable as e:
            self.skipped.append(f"{what}: the page didn't open ({e})")
        except Exception as e:  # a slow or broken site
            self.skipped.append(f"{what}: couldn't be read ({type(e).__name__})")
        return None

    def lookup(self, fn, what: str, *args, once: str = ""):
        """A call to the registry, the web search or Wikipedia (company_sources.py): its answer, or None with the
        reason noted. `once`: a source that showed a robot check is left alone for the rest of the hunt."""
        if self.stopped():
            return None
        if once and once in self.blocked:
            self.skipped.append(f"{what}: it showed a robot check earlier, so it was left alone")
            return None
        rest = self.resting.get(what, 0) - time.monotonic()
        if rest > 0:
            self.skipped.append(f"{what}: it asked JobHunt to slow down, so it is left alone for {rest:.0f} more seconds")
            return None
        try:
            return fn(*args, stop_event=getattr(self.ctx, "stop_event", None))
        except more.Unavailable as e:
            if once and "robot check" in str(e):
                self.blocked.add(once)
            if re.search(r"too many requests|HTTP 429", str(e), re.I):
                self.resting[what] = time.monotonic() + REST_S
            self.skipped.append(f"{what}: {e}")
        except Exception as e:  # a slow or broken service never costs the summary
            self.skipped.append(f"{what}: couldn't be read ({type(e).__name__})")
        return None

    def json(self, url: str, what: str):
        from .sites import PageUnavailable, SiteBlocked
        if self.stopped() or self.loads >= MAX_PAGE_LOADS:
            return None
        self.loads += 1
        try:
            return self.ctx.get_json(url)
        except (SiteBlocked, PageUnavailable) as e:
            self.skipped.append(f"{what}: not available ({e})")
        except Exception as e:
            self.skipped.append(f"{what}: couldn't be read ({type(e).__name__})")
        return None


def board_info(reader: Reader, job: dict) -> dict:
    """For a job page's Refresh: what a hunt keeps from the board while it reads the posting (see
    Posting.company_info), read again. Never Indeed or LinkedIn (see the top of this file)."""
    source, url = job.get("source"), job.get("url") or ""
    if source == "mycareersfuture":
        m = re.search(r"([0-9a-f]{32})", url)
        if m:
            from .sites import MyCareersFutureSite
            r = reader.json(f"{MyCareersFutureSite.API}/jobs/{m.group(1)}", "MyCareersFuture's record of this job")
            if isinstance(r, dict):
                return mcf_info(r, url)
    elif source in ("jobstreet", "demo"):
        html = reader.page(url, "The posting page", board=True)
        if html:
            return board_company_info(html, url)
    return {}


def mcf_info(r: dict, url: str) -> dict:
    try:
        text, website = mcf_record_text(r)
    except Exception:  # never costs the hunt its posting
        return {}
    return {"label": "MyCareersFuture's record of this job and company", "url": url, "text": text, "website": website}


def board_company_info(html: str, page_url: str) -> dict:
    """What a JobStreet (or practice board) posting page says about where to read about the company:
    {"board_page": the board's company page, "website": the company's own site}, either may be missing.
    JobStreet names its company page in the posting's JSON-LD (hiringOrganization.sameAs)."""
    try:
        return _board_company_info(html, page_url)
    except Exception:  # the company summary must never cost the hunt its posting
        return {}


def _board_company_info(html: str, page_url: str) -> dict:
    out = {}
    job = find_jsonld_jobposting(html) or {}
    org = job.get("hiringOrganization") if isinstance(job.get("hiringOrganization"), dict) else {}
    board_host = host_of(page_url)
    for key in ("sameAs", "url"):
        links = org.get(key) if isinstance(org.get(key), list) else [org.get(key)]
        for link in links:
            if not isinstance(link, str) or not link.startswith(("http://", "https://")) or not host_of(link):
                continue
            kind = "board_page" if host_of(link) == board_host else "website"
            out.setdefault(kind, link.split("?")[0] if kind == "board_page" else link)
    if "board_page" not in out:  # a link the board's own page makes, never one inside the job description
        for a in BeautifulSoup(html or "", "html.parser").select("a[href*='/companies/'], a.company-link"):
            if a.find_parent(attrs={"data-automation": "jobAdDetails"}) or a.find_parent(class_="job-description"):
                continue
            try:
                link = urljoin(page_url, a.get("href") or "").split("?")[0]
            except ValueError:
                continue
            if board_host and host_of(link) == board_host:
                out["board_page"] = link
                break
    return out


def board_page_ok(link: str, job: dict) -> bool:
    """The board's company page is opened in the browser with the board's login, so only on the posting's
    own site, and never LinkedIn or Indeed (see the top of this file)."""
    host = host_of(link)
    return bool(host) and host == host_of(job.get("url") or "") and link.startswith(("http://", "https://")) \
        and not any(host == d or host.endswith("." + d) for d in ("linkedin.com", "indeed.com"))


def gather(ctx, job: dict, info: dict | None = None, practice_base: str = "") -> tuple[list[Source], list[str]]:
    """The pages a summary is written from, and what was skipped. `info`: what the hunt kept from the board
    while reading the posting (None: read it again now). `practice_base`: the practice board's address."""
    practice = job.get("source") == "demo"
    reader = Reader(ctx, (practice_base or "http://practice-board.invalid") if practice else "")
    sources: list[Source] = []

    def add(label, url, text, cap, kind="", **meta):
        text = clean_whitespace(text or "")
        if len(text) >= 20:
            sources.append(Source(f"S{len(sources) + 1}", label, url, text[:cap], kind, meta))
            return sources[-1]
        return None

    add("The job posting", job.get("url") or "", posting_source_text(job), CAPS["posting"], "posting")
    info = dict(info) if info is not None else board_info(reader, job)
    if info.get("text"):
        add(info.get("label") or "The job board's company record", info.get("url") or job.get("url") or "",
            info["text"], CAPS["board"], "board")
    websites = []  # (url, how it was found, guessed)
    if info.get("website"):
        websites.append((info["website"], "named by the job board", False))
    if info.get("board_page") and not board_page_ok(str(info["board_page"]), job):
        reader.skipped.append(f"{str(info['board_page'])[:100]}: not on the job board's own site, so it wasn't opened")
    elif info.get("board_page") and reader.address(info["board_page"]):
        label = "JobStreet's company page" if job.get("source") == "jobstreet" else "The job board's company page"
        html = reader.page(info["board_page"], label, board=True)
        if html:
            text, website = parse_board_company_page(html, info["board_page"])
            add(label, info["board_page"], text, CAPS["board"], "board")
            if website:
                websites.append((website, "named by the job board", False))
    company = job.get("company") or ""
    for url in links_in_text("\n".join([job.get("posting_text") or ""] + [s.text for s in sources[1:]])):
        if domain_matches_name(host_of(url), company):
            websites.append((url, "linked from the posting or the board", False))
    confirm: tuple = ()
    if not practice and company:  # nothing leaves the PC for a practice-board job
        reg = reader.lookup(more.acra_lookup, "Singapore's company registry (ACRA)", company)
        if reg:
            add("Singapore's company registry (ACRA), via data.gov.sg", reg.url, reg.text, CAPS["registry"], "registry",
                name=reg.name, uen=reg.uen, status=reg.status, industry_line=reg.industry_line,
                ssic=reg.ssic_codes, head=reg.text.split("\n", 1)[0])
            confirm = tuple(c for c in (reg.postal_code, reg.uen) if c)
        if not websites:  # one search, only when neither the board nor the posting names the website
            found = reader.lookup(more.web_search, "Web search", company, once="web search")
            hits = [(u, "found by a web search: check it is theirs", True) for u, _ in (found or [])
                    if host_of(u) and not not_company_site(host_of(u)) and domain_matches_name(host_of(u), company)]
            if found is not None and not hits:
                reader.skipped.append("Web search: no result on a website that carries the company's name")
            websites += hits[:2]
        wiki = reader.lookup(more.wikipedia, "Wikipedia", company, [w[0] for w in websites],
                             job.get("posting_text") or "")
        if wiki:
            label = (f"Wikipedia: {wiki.title}" if wiki.how != "group" else
                     f"Wikipedia: {wiki.title} (the group this company appears to belong to, matched by name)")
            add(label, wiki.url, wiki.text, CAPS["wiki"], "wikipedia", group=wiki.how == "group")
            if wiki.website:
                websites.insert(0, (wiki.website, "named by Wikipedia", False))
    _read_website(reader, websites, company, add, confirm=confirm)
    # no guessing when the company's own site was found but turned JobHunt away
    if not any(s.kind == "website" for s in sources) and not reader.blocked_sites and reader.loads < MAX_PAGE_LOADS:
        names = [n for n in short_names(company) if len(n) >= 4]
        if names and not practice:
            # an address that doesn't exist costs no page load (see Reader.address), only a name lookup
            guesses = [(f"https://www.{name}{end}", "found from the company's name: check it is theirs", True)
                       for name in names for end in (".com.sg", ".sg", ".com")]
            _read_website(reader, guesses, company, add, max_tries=2, confirm=confirm)
    return sources, reader.skipped


def _read_website(reader: Reader, candidates: list, company: str, add, max_tries: int = 3, confirm: tuple = ()) -> None:
    tries, unique = 0, {}
    for c in candidates:  # the same site found twice keeps how it was found first
        unique.setdefault(c[0].rstrip("/").lower(), c)
    for url, how, guessed in unique.values():
        if tries >= max_tries or reader.loads >= MAX_PAGE_LOADS or reader.stopped():
            return
        host = host_of(url)
        if not host or not_company_site(host) or host.removeprefix("www.") in reader.blocked_sites:
            continue
        safe = reader.address(url)
        if not safe:
            if not guessed:
                reader.skipped.append(f"{url}: not an ordinary public website, so it wasn't opened")
            continue
        tries += 1
        html = reader.page(safe, f"The company's website ({host})", board=False)
        if not html:
            continue
        text = website_text(html)
        final = getattr(reader.ctx.browser, "last_url", "") or safe
        if not reader.address(final):
            final = safe
        sub = subpage_link(html, final)
        if not looks_like(text, company, guessed, confirm):
            # a guessed site whose home page names the company but not Singapore: its contact page may
            # (seen live: alberthoo.com.sg has the Singapore address only on /contact)
            if guessed and looks_like(text, company, False) and sub and reader.address(sub):
                sub_html = reader.page(sub, f"The company's website ({host}, second page)", board=False)
                sub_text = website_text(sub_html) if sub_html else ""
                if looks_like(sub_text, company, True, confirm):
                    add(f"The company's website ({how})", final, text, CAPS["web"], "website")
                    add(f"The company's website ({how}), {_page_name(sub)}", sub, sub_text, CAPS["web"],
                        "website")
                    return
            if not guessed:
                reader.skipped.append(f"{host}: the page doesn't name {company}, so it wasn't used")
            elif how.startswith("found by a web search"):
                reader.skipped.append(f"{host} (from the web search): the page doesn't name {company} and "
                                      "Singapore, so it wasn't used")
            continue
        add(f"The company's website ({how})", final, text, CAPS["web"], "website")
        if sub and reader.address(sub):
            sub_html = reader.page(sub, f"The company's website ({host}, second page)", board=False)
            if sub_html:
                add(f"The company's website ({how}), {_page_name(sub)}", sub, website_text(sub_html),
                    CAPS["web"], "website")
        return


# ── checking what the AI wrote ────────────────────────────────────────

def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


_PLACE_STOP = {"the", "and", "office", "offices", "headquarters", "headquartered", "hq", "based", "in", "at", "of",
               "near", "region", "area", "district", "job", "main", "company", "its", "is", "located", "work", "remote",
               "hybrid", "on", "site", "onsite", "from", "with", "a", "an", "head", "registered"}


# A headquarters or size quote must say that's what it is (in its own words or the label just before it on
# the page): 'Singapore' from 'Location: Singapore' is where the job is, '50' from '$50 per hour' is pay.
CUES = {"hq_country": re.compile(r"headquarter|head office|\bhq\b|based in|based out of|registered in|"
                                 r"(?:home|parent) (?:country|company)|incorporated in", re.I),
        "size": re.compile(r"employee|staff|people|headcount|workforce|team of|company size|\bsize\b|employs",
                           re.I)}


def _before(quote_norm: str, source_norm: str, chars: int = 40) -> str:
    i = f" {source_norm} ".find(f" {quote_norm} ")
    return source_norm[max(0, i - chars):i] if i > 0 else ""


def _backs(short: str, text: str, quote: str) -> bool:
    """Is the Dashboard's short label made of the fact's own words (at least half of them)?"""
    words = [w for w in _norm(short).split() if len(w) >= 4 and w not in _PLACE_STOP]
    have = {w[:4] for w in _norm(text + " " + quote).split()}
    return not words or sum(w[:4] in have for w in words) * 2 >= len(words)


def check_fact(item, by_id: dict, place: bool = False, key: str = "") -> dict | None:
    """One fact from the AI, kept only if its quote is really on the page it names (whole words), every number
    (and, for a place, every place word) in it is in that quote, and a headquarters or size quote says so."""
    if not isinstance(item, dict):
        return None
    text = clean_whitespace(str(item.get("text") or ""))[:400]
    if not text or _norm(text).startswith("not found") or _norm(text) in ("unknown", "none", "n a", "not stated"):
        return None
    src = by_id.get(str(item.get("source") or "").strip().upper())
    quote = clean_whitespace(str(item.get("quote") or ""))[:400]
    q = _norm(quote)
    if not src or len(q) < 6 or f" {q} " not in f" {_norm(src.text)} ":
        return None
    if key in CUES and not CUES[key].search(quote + " " + _before(q, _norm(src.text))):
        return None
    q_words = set(q.split())
    if any(n not in q_words for n in re.findall(r"\d+", text)):
        return None
    if place and any(w not in q_words for w in _norm(text).split() if w not in _PLACE_STOP and not w.isdigit()):
        return None
    out = {"text": text, "source": src.id, "quote": quote}
    short = clean_whitespace(str(item.get("short") or ""))[:80]
    if short and not any(n not in q_words for n in re.findall(r"\d+", short)) and _backs(short, text, quote):
        out["short"] = short
    return out


def verify(data: dict, sources: list[Source], posting_text: str = "", job: dict | None = None) -> dict:
    by_id = {s.id: s for s in sources}
    fields, dropped = {}, 0
    for key, _ in FIELDS:
        raw = data.get(key) if isinstance(data, dict) and key not in CODE_FIELDS else None
        fact = check_fact(raw, by_id, place=key in PLACE_FIELDS, key=key)
        if fact is None and isinstance(raw, dict) and raw.get("text") and not _norm(str(raw["text"])).startswith("not found"):
            dropped += 1
        fields[key] = fact
    notable = []
    for raw in (data.get("notable") or [] if isinstance(data, dict) else [])[:4]:
        fact = check_fact(raw, by_id)
        if fact:
            notable.append(fact)
        elif isinstance(raw, dict) and raw.get("text"):
            dropped += 1
    agency = isinstance(data, dict) and str(data.get("agency")).strip().lower() == "true"
    # the posting as the AI saw it (S1), so the quote below is really in S1
    posting_text = sources[0].text if sources else posting_text
    m = agency_sign(posting_text)
    if m:  # seen by code, so it's said even when the AI missed it
        agency = True
        if not any(re.search(r"recruit|agency|client", n["text"], re.I) for n in notable) and sources:
            start = posting_text.rfind("\n", 0, m.start()) + 1
            end = posting_text.find("\n", m.end())
            line = clean_whitespace(posting_text[start:end if end > 0 else None])[:200]
            notable.insert(0, {"text": "Posted by a recruitment agency: the real employer may be a client it doesn't name.",
                               "source": "S1", "quote": line})
    elif agency and not any(re.search(r"recruit|agency|client", n["text"], re.I) for n in notable):
        reg = next((s for s in sources if s.kind == "registry"), None)
        if reg and str((reg.meta.get("ssic") or [""])[0]).startswith("78") and reg.meta.get("industry_line"):
            # registered as an employment or manpower agency (SSIC 78): that backs what the AI saw
            notable.insert(0, {"text": "Posted by a recruitment agency: the real employer may be a client it doesn't name.",
                               "source": reg.id, "quote": reg.meta["industry_line"]})
        else:
            agency = False  # the AI said so but backed it with nothing that survived the check
    out = {"fields": fields, "notable": notable[:4], "agency": agency, "dropped": dropped, "client": None}
    _company_first(out, data if isinstance(data, dict) else {}, sources, by_id, job or {})
    return out


def _company_first(out: dict, data: dict, sources: list[Source], by_id: dict, job: dict) -> None:
    """The summary is about the company, from its own sources first (Zech, 2026-10-15: "a literal summary of the
    job posting and not so much the company"):
      - a recruiter posting: the facts are about the agency, so none of them may come from the posting (which
        describes the client); what the posting says about the client is kept apart, as `client`
      - otherwise 'What they do' from the posting says so (from_posting)
      - 'Where the job is' is the board's own location field, credited to the posting
      - 'Registered as' (and the industry, when nothing else gave one) comes from the registry record"""
    f = out["fields"]
    posting = sources[0].id if sources else "S1"
    hired = {}  # the board record of an agency posting that names its client (MyCareersFuture's hiringCompany)
    for s in sources:
        m = re.search(r"^Hiring company \(the employer this was posted for\): (.+)$", s.text, re.M) \
            if s.kind == "board" else None
        p = re.search(r"^Posted by: (.+)$", s.text, re.M) if m else None
        if m:
            hired = {"source": s, "client": m.group(1).strip()[:120], "agency": p.group(1).strip()[:120] if p else ""}
            break
    if hired and more.name_key(hired["client"]) == more.name_key(job.get("company") or ""):
        # the job's company IS the employer the agency names: the summary is about that employer
        out["agency"] = False
        out["notable"] = [n for n in out["notable"] if not n["text"].startswith("Posted by a recruitment agency")]
        if hired["agency"]:
            out["posted_by"] = hired["agency"]
            out["notable"].insert(0, {"text": f"Posted by {hired['agency']}, a recruitment agency, for this employer.",
                                      "source": hired["source"].id, "quote": f"Posted by: {hired['agency']}"})
    if out["agency"]:
        client_pages = {posting} | ({hired["source"].id} if hired else set())
        for key in ("what_they_do", "industry", "company_location", "hq_country", "size"):
            if f.get(key) and f[key]["source"] in client_pages:
                f[key] = None
        if hired:
            out["client_name"] = hired["client"]
        client = check_fact(data.get("client"), by_id)
        if client and (by_id[client["source"]].kind in ("posting", "board") or client["source"] == posting):
            out["client"] = client
    elif f.get("what_they_do") and f["what_they_do"]["source"] == posting:
        f["what_they_do"]["from_posting"] = True
        # "only" when no page about the company itself (its website, the board's company page, Wikipedia) was read
        f["what_they_do"]["posting_only"] = not any(s.kind in ("board", "website", "wikipedia") for s in sources)
    loc = clean_whitespace(job.get("location") or "")
    head = sources[0].text if sources else ""
    if loc and f"Location: {loc}" in head:
        ai = f.get("job_location")
        if not (ai and _norm(loc) in ("singapore", "remote", "") and len(_norm(ai["text"])) > len(_norm(loc))
                and by_id[ai["source"]].kind in ("posting", "board", "")):
            f["job_location"] = {"text": loc, "source": posting, "quote": f"Location: {loc}", "board_field": True}
    reg = next((s for s in sources if s.kind == "registry"), None)
    if reg and reg.meta.get("head") and reg.meta["head"] in reg.text:
        f["registered"] = {"text": reg.meta["head"], "source": reg.id, "quote": reg.meta["head"]}
        line = reg.meta.get("industry_line") or ""
        if not f.get("industry") and line and line in reg.text:
            f["industry"] = {"text": line.split(": ", 1)[-1], "source": reg.id, "quote": line}
        m = re.search(r"^Registered office address: (.+)$", reg.text, re.M)
        if not f.get("company_location") and m:
            f["company_location"] = {"text": f"{m.group(1)} (registered office)", "source": reg.id, "quote": m.group(0)}


def one_line(info: dict | None) -> str:
    """'Engineering & robotics firm · Penjuru Close, Singapore · 51-100 staff', for the Dashboard. Built by
    code from the checked facts only."""
    if not info or info.get("status") != "ok":
        return ""
    f = info.get("fields") or {}
    what = ((f.get("what_they_do") or {}).get("short") or (f.get("industry") or {}).get("text")
            or (f.get("what_they_do") or {}).get("text") or "")
    if len(what) > 70:
        what = what[:69].rsplit(" ", 1)[0] + "…"
    where = (f.get("job_location") or f.get("company_location") or {}).get("text", "")
    if len(where) > 50:
        where = where[:49].rsplit(" ", 1)[0] + "…"
    size = (f.get("size") or {}).get("text", "")
    if size and not re.search(r"staff|employee|people|person", size, re.I) and len(size) < 20:
        size += " staff"
    if info.get("agency"):  # what the agency does isn't the job: say who it's for, and where
        client = info.get("client") or {}
        hiring = client.get("short") or (client.get("text") if len(client.get("text") or "") <= 40 else "")
        return " · ".join(p for p in ("Recruiter posting", f"for: {hiring}" if hiring else "for an unnamed client",
                                      (f.get("job_location") or {}).get("text", "")[:50]) if p)
    parts = [p for p in ("Via a recruiter" if info.get("posted_by") else "", what, where, size[:40]) if p]
    return " · ".join(parts)


def load(job: dict | None) -> dict | None:
    try:
        info = json.loads((job or {}).get("company_info") or "")
    except (TypeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def line_for(job: dict) -> str:
    return one_line(load(job))


# ── making it ─────────────────────────────────────────────────────────

class _FoundLess(Exception):
    pass


def _found(info: dict) -> int:
    return sum(1 for v in (info.get("fields") or {}).values() if v)


def summarise(task, ctx, job_id: int, info: dict | None = None, practice_base: str = "") -> dict | None:
    """Look the company up and save the summary on the job. Never raises: a problem is saved and shown
    instead (a refresh that fails keeps the summary it had). Returns what was saved, or None if stopped."""
    from .hunt import StopHunt
    job = db.get_job(job_id)
    if not job:
        return None
    old = load(job)
    sources, skipped = [], []
    try:
        sources, skipped = gather(ctx, job, info, practice_base)
        if getattr(ctx, "stop_event", None) is not None and ctx.stop_event.is_set():
            return None
        system, user = prompts.company_prompt(job.get("company") or "", job.get("role") or "", sources)
        data, _ = task.get_llm().chat_json(stage="company", system=system, user=user, max_tokens=1200, job_id=job_id)
        checked = verify(data, sources, job.get("posting_text") or "", job)
        result = {"status": "ok", **checked}
        if old and old.get("status") == "ok" and len(sources) < len(old.get("sources") or []) \
                and _found(result) < _found(old):
            # a page blocked or gone this time: the fuller summary stays (its sources are its own). Read from
            # as many pages, the new one wins even with fewer facts: the AI varies, and the pages may have changed.
            raise _FoundLess(f"fewer pages could be read this time ({len(sources)}, before "
                             f"{len(old.get('sources') or [])})")
    except StopHunt:
        return None
    except Exception as e:  # the AI, the cap, a page: none of it may touch the package
        reason = str(e) if str(e) and type(e).__name__ in ("LLMError", "RateLimited", "BudgetExceeded",
                                                           "_FoundLess") else type(e).__name__
        if old and old.get("status") == "ok":
            task.log(f"Couldn't refresh the company summary for {job['company']} ({reason[:160]}); kept the one from "
                     f"{old.get('made_at', 'before')}.", "warn", job_id=job_id)
            return {**old, "refresh_error": reason[:200]}
        result = {"status": "failed", "error": reason[:300]}
    result.update(made_at=datetime.now().isoformat(timespec="minutes"), company=job.get("company") or "",
                  sources=[{"id": s.id, "label": s.label, "url": s.url, "kind": s.kind} for s in sources], skipped=skipped)
    db.set_company_info(job_id, json.dumps(result, ensure_ascii=False))
    _write_file(job, result)
    if result["status"] == "ok":
        found = sum(1 for v in result["fields"].values() if v)
        task.log(f"Company summary for {job['company']}: {found} of {len(FIELDS)} facts found in "
                 f"{len(sources)} page(s)" + (f"; {result['dropped']} unbacked fact(s) left out" if result["dropped"] else "")
                 + ".", job_id=job_id)
    else:
        task.log(f"Couldn't make the company summary for {job['company']}: {result['error'][:160]}", "warn",
                 job_id=job_id)
    return result


def summary_md(info: dict) -> str:
    agency = info.get("agency")
    lines = [f"# Posted by: {info.get('company', '')} (a recruitment agency)" if agency
             else f"# About {info.get('company', '')}", ""]
    if info.get("status") != "ok":
        return "\n".join(lines + [f"Couldn't look this company up: {info.get('error', '')}", ""])
    f = info.get("fields") or {}
    for key, label in FIELDS:
        if agency and key == "job_location":
            continue
        fact = f.get(key)
        tag = ((" (from the job posting only)" if fact.get("posting_only") else " (from the job posting)")
               if fact and fact.get("from_posting") else "")
        lines.append(f"- **{label}:** " + (f"{fact['text']}{tag} [{fact['source']}]" if fact else NOT_FOUND))
    if agency:
        client, where = info.get("client"), f.get("job_location")
        lines += ["", f"## Hiring for: {info.get('client_name') or 'an unnamed client'}", "",
                  "- **What the posting says about the client:** "
                  + (f"{client['text']} [{client['source']}]" if client else NOT_FOUND),
                  "- **Where the job is:** " + (f"{where['text']} [{where['source']}]" if where else NOT_FOUND)]
    if info.get("notable"):
        lines += ["", "## Notable"] + [f"- {n['text']} [{n['source']}]" for n in info["notable"]]
    lines += ["", "## Sources"] + [f"- {s['id']}: {s['label']}: {s['url']}" for s in info.get("sources", [])]
    if info.get("skipped"):
        lines += ["", "## Not read"] + [f"- {s}" for s in info["skipped"]]
    lines += ["", f"Made {info.get('made_at', '')}. Every fact above is quoted from the page it cites; anything the "
                  "pages didn't say is 'Not found'.", ""]
    return "\n".join(lines)


def _write_file(job: dict, info: dict) -> None:
    if not job.get("folder"):
        return
    folder = paths.jobs_dir() / job["folder"]
    try:
        if folder.is_dir():
            (folder / "company.md").write_text(summary_md(info), encoding="utf-8")
    except OSError:
        pass
