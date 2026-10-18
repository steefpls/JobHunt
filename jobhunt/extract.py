"""Turn job-posting web pages into clean text.

This is deterministic code, not the model: the model only ever sees the clean
text this produces (capped), never raw HTML or page snapshots.
"""
import html as htmlmod
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "button",
             "template", "aside"]
BLOCK_TAGS = {"p", "div", "section", "article", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol",
              "table", "main", "dd", "dt", "blockquote", "pre"}


@dataclass
class Listing:
    """One search result: enough to decide whether to open the posting."""
    source: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    salary: str = ""  # as the search card shows it, if it does
    salary_max_monthly: float = 0.0  # top of that range in SGD a month; 0 = not stated or not comparable
    extra: dict = field(default_factory=dict)


@dataclass
class Posting:
    source: str
    url: str
    title: str
    company: str
    location: str
    text: str
    apply_url: str = ""
    salary: str = ""
    salary_max_monthly: float = 0.0  # top of the stated range in SGD a month; 0 = not stated in a form code can read
    # what the board says about the company, kept while reading the posting so the company summary needn't
    # load the page again: {"board_page": url} or {"label", "url", "text", "website"} (see company.py)
    company_info: dict = field(default_factory=dict)


# ── salary ────────────────────────────────────────────────────────────

_PER_YEAR_RE = re.compile(r"year|annum|annual|yearly|\bp\.?\s?a\b|\byr\b", re.I)
_PER_MONTH_RE = re.compile(r"month|\bmth|\bp\.?\s?m\b", re.I)
_PER_HOUR_RE = re.compile(r"hour|\bhr\b", re.I)
_PER_DAY_OR_WEEK_RE = re.compile(r"\bday\b|daily|\bweek|per diem", re.I)
# Pay in another currency can't be compared with an SGD floor. A bare "$" on a Singapore board means SGD.
_FOREIGN_CURRENCY_RE = re.compile(r"\b(usd|eur|gbp|myr|rm|hkd|aud|inr|idr|php|thb|cny|rmb|jpy|vnd)\b|"
                                  r"\b(us|hk|a)\$|[€£₹¥]", re.I)
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", re.I)


def monthly(amount: float, period: str) -> float:
    """An amount per `period` (a free-text unit such as 'Monthly', 'YEAR', 'per annum') as a monthly figure."""
    if _PER_MONTH_RE.search(period or ""):  # first: "per month, 3 years' experience" is monthly
        return amount
    if _PER_YEAR_RE.search(period or ""):
        return amount / 12
    if _PER_HOUR_RE.search(period or "") or _PER_DAY_OR_WEEK_RE.search(period or ""):
        return 0.0  # hourly, daily or weekly pay can't be compared fairly with a monthly floor
    return amount


def salary_top_monthly(text: str) -> float:
    """Pay as a board shows it -> top of the range in SGD a month (0 = not stated or not comparable).

    '$5,500 – $8,000 per month' -> 8000, '$5500 - $8k p.m.' -> 8000, '$84,000 – $120,000 per year' -> 10000.
    No period means monthly (how Singapore boards quote pay); hourly, daily, weekly and foreign pay give 0.
    A misread here skips a job unseen, so doubt reads high: '$6,000 – $8,000 + annual bonus' -> 8000, and
    '$60,000 – $80,000 per annum + 13th month' (both periods named) -> 80000, which no floor rules out."""
    if not text or _FOREIGN_CURRENCY_RE.search(text):
        return 0.0
    amounts = [float(n.replace(",", "")) * (1000 if k else 1) for n, k in _AMOUNT_RE.findall(_spread_k(text))]
    amounts = [a for a in amounts if a >= 100]  # skip stray small numbers ("2 days WFH")
    if not amounts:
        return 0.0
    top = max(amounts)
    if _PER_YEAR_RE.search(text):
        if _PER_MONTH_RE.search(text):
            return top  # a month and a year both named: can't tell which the figure is
        return top / 12 if top >= _LOWEST_YEARLY_PAY else top  # "annual" may be about a bonus, not the pay
    return monthly(top, text)


def salary_range_monthly(text: str) -> tuple[float, float]:
    """Pay as a board shows it -> (bottom, top) in SGD a month; (0, 0) when not stated or not comparable.
    '$5,500 – $8,000 per month' -> (5500, 8000), '$84,000 – $120,000 per year' -> (7000, 10000), '$6k' -> (6000, 6000).
    Unlike salary_top_monthly, doubt gives (0, 0): this is only shown, it never skips a job."""
    if not text or _FOREIGN_CURRENCY_RE.search(text):
        return 0.0, 0.0
    amounts = [float(n.replace(",", "")) * (1000 if k else 1) for n, k in _AMOUNT_RE.findall(_spread_k(text))]
    amounts = [a for a in amounts if a >= 100]
    if not amounts or (_PER_YEAR_RE.search(text) and _PER_MONTH_RE.search(text)):
        return 0.0, 0.0
    lo, hi = min(amounts), max(amounts)
    if _PER_YEAR_RE.search(text):
        return (lo / 12, hi / 12) if lo >= _LOWEST_YEARLY_PAY else (0.0, 0.0)
    if _PER_HOUR_RE.search(text) or _PER_DAY_OR_WEEK_RE.search(text):
        return 0.0, 0.0
    return lo, hi


_LOWEST_YEARLY_PAY = 20_000  # a year's pay below this is really a month's pay with "annual bonus" beside it


_K_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)(\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*k\b)", re.I)


def _spread_k(text: str) -> str:
    """'5-6k' means 5k-6k."""
    return _K_RANGE_RE.sub(r"\1k\2", text or "")


def parse_salary_floor(text: str) -> float:
    """'15,000 per month', 'SGD 15k', '180k a year', '5-6k' -> monthly SGD figure (0 if no number).

    The period is read next to the number, so 'SGD 6,000 per month, 3 years' experience' stays monthly."""
    text = _spread_k(text)
    m = _AMOUNT_RE.search(text)
    if not m:
        return 0.0
    amount = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
    return monthly(amount, text[max(0, m.start() - 20):m.end() + 25])


def _jsonld_salary(job: dict) -> tuple[str, float]:
    """JSON-LD baseSalary -> (display text, top of range per month in SGD, or 0)."""
    base = job.get("baseSalary")
    if not isinstance(base, dict):
        return "", 0.0
    currency = str(base.get("currency") or "").upper()
    value = base.get("value")
    if isinstance(value, dict):
        lo, hi, unit = value.get("minValue"), value.get("maxValue") or value.get("value"), str(value.get("unitText") or "")
    else:
        lo, hi, unit = None, value, str(base.get("unitText") or "")
    try:
        hi = float(hi)
    except (TypeError, ValueError):
        return "", 0.0
    lo_txt = f"{float(lo):,.0f}-" if isinstance(lo, (int, float)) else ""
    text = f"{currency or 'SGD'} {lo_txt}{hi:,.0f} {unit.lower()}".strip()
    return text, (monthly(hi, unit) if currency in ("", "SGD") else 0.0)


def clean_whitespace(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(fragment: str) -> str:
    """HTML (a page or a fragment) -> readable plain text with bullets kept."""
    soup = BeautifulSoup(fragment or "", "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.insert_after("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return _join_bullets(clean_whitespace(htmlmod.unescape(soup.get_text())))


def _join_bullets(text: str) -> str:
    """A list item whose text sits in a <p> comes out as a '-' line followed by its text: join them.
    A '-' with no text of its own (an empty item, or one holding only a nested list) is dropped."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if line != "-":
            out.append(line)
            continue
        j = next((k for k in range(i + 1, len(lines)) if lines[k].strip()), None)
        if j is not None and not lines[j].startswith("- "):
            lines[j] = "- " + lines[j]
    return clean_whitespace("\n".join(out))


def find_jsonld_jobposting(page_html: str) -> dict | None:
    soup = BeautifulSoup(page_html or "", "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                return item
    return None


def _jsonld_location(job: dict) -> str:
    loc = job.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
            return ", ".join(p for p in parts if isinstance(p, str) and p)
    return ""


def posting_from_jsonld(job: dict, source: str, url: str) -> Posting:
    org = job.get("hiringOrganization") or {}
    company = org.get("name", "") if isinstance(org, dict) else str(org)
    salary, salary_max = _jsonld_salary(job)
    return Posting(
        source=source, url=url,
        title=str(job.get("title", "")).strip(),
        company=str(company).strip(),
        location=_jsonld_location(job),
        text=html_to_text(job.get("description", "")),
        apply_url=str(job.get("url") or job.get("directApplyUrl") or ""),
        salary=salary, salary_max_monthly=salary_max,
    )


def main_text(page_html: str, selectors: list[str] | None = None) -> str:
    """Best guess at the posting body: given selectors first, then <main>/<article>, then <body>."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    for sel in (selectors or []) + ["main", "article", "[role=main]", "body"]:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return html_to_text(str(node))
    return ""


def select_text(page, selector: str) -> str:
    """Text of the first match of `selector` in an HTML string or an already parsed BeautifulSoup node."""
    if not hasattr(page, "select_one"):
        page = BeautifulSoup(page or "", "html.parser")
    node = page.select_one(selector)
    return clean_whitespace(node.get_text(" ")) if node else ""


def generic_posting(page_html: str, source: str, url: str, body_selectors=None,
                    title_sel=None, company_sel=None, location_sel=None) -> Posting:
    """JSON-LD JobPosting if the page has one (most boards do), else CSS selectors."""
    job = find_jsonld_jobposting(page_html)
    if job and job.get("description"):
        p = posting_from_jsonld(job, source, url)
        if not p.text or len(p.text) < 80:
            p.text = main_text(page_html, body_selectors)
        return p
    soup = BeautifulSoup(page_html or "", "html.parser")
    title = select_text(page_html, title_sel) if title_sel else ""
    if not title:
        h1 = soup.find("h1")
        title = clean_whitespace(h1.get_text(" ")) if h1 else (soup.title.string.strip() if soup.title and soup.title.string else "")
    return Posting(
        source=source, url=url, title=title,
        company=select_text(page_html, company_sel) if company_sel else "",
        location=select_text(page_html, location_sel) if location_sel else "",
        text=main_text(page_html, body_selectors),
    )
