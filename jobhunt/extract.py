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
    return clean_whitespace(htmlmod.unescape(soup.get_text()))


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
    return Posting(
        source=source, url=url,
        title=str(job.get("title", "")).strip(),
        company=str(company).strip(),
        location=_jsonld_location(job),
        text=html_to_text(job.get("description", "")),
        apply_url=str(job.get("url") or job.get("directApplyUrl") or ""),
    )


def main_text(page_html: str, selectors: list[str] | None = None) -> str:
    """Best guess at the posting body: given selectors first, then <main>/<article>, then <body>."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    for sel in (selectors or []) + ["main", "article", "[role=main]", "body"]:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return html_to_text(str(node))
    return ""


def select_text(page_html: str, selector: str) -> str:
    node = BeautifulSoup(page_html or "", "html.parser").select_one(selector)
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
