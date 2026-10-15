"""Where a company summary reads about the company beyond the posting and the job board (see company.py):

  ACRA       Singapore's company registry, as open data on data.gov.sg (no key, no login): registered name, UEN,
             status, registered address, registered business activity (SSIC) and incorporation date. One dataset
             per first letter of the name; a name is looked up exactly (with its Pte. Ltd. / Private Limited
             spellings), then by its words, and only a single registered company with that very name counts.
  Web search DuckDuckGo's plain HTML results, once per company, to find the company's own website. A search that
             answers with a robot check is skipped, and not tried again for the rest of the hunt.
  Wikipedia  its API, for larger companies: the article's opening and, from Wikidata, the official website. Used
             only when the article is clearly this company (or the group a regional subsidiary belongs to).

Every call goes through http_get (the tests swap it, so they never reach the internet) and is kept polite: a short
gap between calls to the same site, and data.gov.sg's "try again in 10 seconds" is waited out once, never pushed.
"""
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .extract import clean_whitespace

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
# Wikimedia asks API users to say who they are
WIKI_UA = "JobHunt/1.0 (company summaries for a job seeker; https://github.com/steefpls/JobHunt)"
GAP_S = 2.0  # at least this long between two calls to the same site
RETRY_WAIT_S = 11  # data.gov.sg: "Rate limit exceeded. Please try again in 10 seconds"


def http_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 20) -> tuple[int, str]:
    """(HTTP status, body). The tests swap this."""
    with httpx.Client(headers={"User-Agent": UA, **(headers or {})}, timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, params=params)
        return r.status_code, r.text


_last_call: dict = {}
_last_lock = threading.Lock()


class Unavailable(Exception):
    """The source didn't answer usefully (an error, a robot check, too many requests): skipped, said why."""


def polite_get(url: str, params=None, headers=None, stop_event=None, sleep=time.sleep) -> tuple[int, str]:
    host = urlparse(url).hostname or ""
    with _last_lock:
        wait = _last_call.get(host, 0) + GAP_S - time.monotonic()
        _last_call[host] = time.monotonic() + max(0, wait)
    while wait > 0 and not (stop_event is not None and stop_event.is_set()):
        sleep(min(0.25, wait))
        wait -= 0.25
    if stop_event is not None and stop_event.is_set():
        raise Unavailable("stopped")
    try:
        return http_get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise Unavailable(f"couldn't be reached ({type(e).__name__})") from None


# ── ACRA, Singapore's company registry ────────────────────────────────

ACRA_COLLECTION = "https://data.gov.sg/collections/2/view"
ACRA_API = "https://data.gov.sg/api/action/datastore_search"
# "ACRA Information on Corporate Entities ('A')" ... ('Z'), ('Others'): data.gov.sg collection 2 (checked 2026-10-15)
ACRA_DATASETS = {
    "A": "d_8575e84912df3c28995b8e6e0e05205a", "B": "d_3a3807c023c61ddfba947dc069eb53f2",
    "C": "d_c0650f23e94c42e7a20921f4c5b75c24", "D": "d_acbc938ec77af18f94cecc4a7c9ec720",
    "E": "d_124a9bd407c7a25f8335b93b86e50fdd", "F": "d_4526d47d6714d3b052eed4a30b8b1ed6",
    "G": "d_b58303c68e9cf0d2ae93b73ffdbfbfa1", "H": "d_fa2ed456cf2b8597bb7e064b08fc3c7c",
    "I": "d_85518d970b8178975850457f60f1e738", "J": "d_478f45a9c541cbe679ca55d1cd2b970b",
    "K": "d_5573b0db0575db32190a2ad27919a7aa", "L": "d_a2141adf93ec2a3c2ec2837b78d6d46e",
    "M": "d_9af9317c646a1c881bb5591c91817cc6", "N": "d_67e99e6eabc4aad9b5d48663b579746a",
    "O": "d_5c4ef48b025fdfbc80056401f06e3df9", "P": "d_181005ca270b45408b4cdfc954980ca2",
    "Q": "d_4130f1d9d365d9f1633536e959f62bb7", "R": "d_2b8c54b2a490d2fa36b925289e5d9572",
    "S": "d_df7d2d661c0c11a7c367c9ee4bf896c1", "T": "d_72f37e5c5d192951ddc5513c2b134482",
    "U": "d_0cc5f52a1f298b916f317800251057f3", "V": "d_e97e8e7fc55b85a38babf66b0fa46b73",
    "W": "d_af2042c77ffaf0db5d75561ce9ef5688", "X": "d_1cd970d8351b42be4a308d628a6dd9d3",
    "Y": "d_31af23fdb79119ed185c256f03cb5773", "Z": "d_4e3db8955fdcda6f9944097bef3d2724",
    "Others": "d_300ddc8da4e8f7bdc1bfc62d0d99e2e7",
}
_SUFFIXES = ["PTE. LTD.", "PTE LTD", "PTE. LTD", "PTE LTD.", "PRIVATE LIMITED", "PTE. LIMITED", "PTE LIMITED",
             "(PTE.) LTD.", "(PTE) LTD", "(PTE) LTD.", "(PRIVATE) LIMITED", "(PTE.) LIMITED", "LTD.", "LTD", "LIMITED",
             "LLP", "L.L.P."]
_SUFFIX_RE = re.compile(r"[\s,.]*(?<![A-Z0-9])(?:\(?\s*(?:PTE|PRIVATE)\s*\.?\s*\)?\s*\.?\s*(?:LTD|LIMITED)|LTD|"
                        r"LIMITED|L\.?\s*L\.?\s*P)\s*\.?\s*$", re.I)
_COMPANY_END = re.compile(r"(?<![A-Z0-9])(?:PTE|PRIVATE|LTD|LIMITED)\b\.?\)?\s*\.?\s*$", re.I)
_LLP_END = re.compile(r"(?<![A-Z0-9])L\.?\s*L\.?\s*P\.?\s*$", re.I)
_LIVE = re.compile(r"^(live|registered)\b", re.I)


def registry_base(name: str) -> str:
    """'Applied Materials South East Asia Pte Ltd' -> 'APPLIED MATERIALS SOUTH EAST ASIA'."""
    base = re.sub(r"\s+", " ", (name or "").upper()).strip()
    for _ in range(2):  # 'X PTE. LTD. LLP' is rare; twice covers 'X (S) PTE LTD.'-like doubles
        base = _SUFFIX_RE.sub("", base).strip(" ,.-")
    return base


def name_key(name: str) -> str:
    """Two spellings of one registered name compare equal: no legal ending, '&' is AND, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", registry_base(name).replace("&", " AND "))


def name_variants(name: str) -> list[str]:
    """The ways the registry may spell this name, for one exact lookup."""
    base = registry_base(name)
    if not base:
        return []
    bases = [base]
    if "&" in base:
        bases.append(re.sub(r"\s*&\s*", " AND ", base))
    elif " AND " in base:
        bases.append(base.replace(" AND ", " & "))
    out = [re.sub(r"\s+", " ", (name or "").upper()).strip()]
    for b in bases:
        out += [b] + [f"{b} {s}" for s in _SUFFIXES]
    return list(dict.fromkeys(v for v in out if v))


def dataset_for(name: str) -> str:
    first = next((ch for ch in registry_base(name) if ch.isalnum()), "")
    return ACRA_DATASETS.get(first.upper() if first.isascii() and first.isalpha() else "Others", ACRA_DATASETS["Others"])


def _na(v) -> str:
    v = clean_whitespace(str(v or ""))
    return "" if v.lower() in ("na", "n/a", "-", "none", "null") else v


_ssic: dict | None = None


def ssic_title(code: str) -> str:
    """SSIC 5-digit code -> its title (SSIC 2025, else 2020: ssic.tsv, from the Department of Statistics)."""
    global _ssic
    if _ssic is None:
        table = {}  # filled first, then published: another thread never sees half of it
        try:
            for line in (Path(__file__).with_name("ssic.tsv")).read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#") and "\t" in line:
                    k, v = line.split("\t", 1)
                    table[k.strip()] = v.strip()
        except OSError:
            pass
        _ssic = table
    return _ssic.get(_na(code), "")


@dataclass
class Registry:
    """One company's registry record, as text a summary can quote."""
    name: str
    uen: str
    status: str
    text: str
    url: str
    postal_code: str = ""
    ssic_codes: list = field(default_factory=list)
    industry_line: str = ""  # the line of `text` that names its main registered business activity


def address_of(r: dict) -> str:
    if _na(r.get("address_type")).upper() not in ("", "LOCAL"):
        return ", ".join(filter(None, [_na(r.get("other_address_line1")), _na(r.get("other_address_line2"))]))
    unit = ""
    if _na(r.get("level_no")) or _na(r.get("unit_no")):
        unit = "#" + "-".join(filter(None, [_na(r.get("level_no")), _na(r.get("unit_no"))]))
    street = " ".join(filter(None, [_na(r.get("block")), _na(r.get("street_name")).title()]))
    postal = _na(r.get("postal_code"))
    return ", ".join(filter(None, [street, unit, _na(r.get("building_name")).title(),
                                   f"Singapore {postal}" if postal else ""]))


def registry_text(r: dict) -> Registry:
    name, uen, status = _na(r.get("entity_name")), _na(r.get("uen")), _na(r.get("entity_status_description"))
    date = _na(r.get("registration_incorporation_date"))
    head = f"{name}, UEN {uen}, {status or 'status not given'}" + (f", registered on {date}" if date else "")
    lines = [head, f"Registered name: {name}", f"UEN: {uen}", f"Status: {status}"]
    kind = " ".join(filter(None, [_na(r.get("entity_type_description")),
                                  f"({_na(r.get('company_type_description'))})" if _na(r.get("company_type_description")) else ""]))
    if kind:
        lines.append(f"Type: {kind}")
    if date:
        lines.append(f"Incorporated or registered: {date}")
    addr = address_of(r)
    if addr:
        lines.append(f"Registered office address: {addr}")
    industry_line, codes = "", []
    for which, key in (("Main", "primary"), ("Second", "secondary")):
        code = _na(r.get(f"{key}_ssic_code"))
        title = ssic_title(code) if code else ""
        own = _na(r.get(f"{key}_user_described_activity")) or _na(r.get(f"{key}_ssic_description"))
        if code:
            codes.append(code)
        if title or own:
            line = f"{which} registered business activity: " + (title or own.capitalize()) + (f" (SSIC {code})" if code else "")
            lines.append(line)
            if which == "Main":
                industry_line = line
            if own and title and own.lower() != title.lower():
                lines.append(f"{which} activity in the company's own words: {own.capitalize()}")
    return Registry(name=name, uen=uen, status=status, text="\n".join(lines), postal_code=_na(r.get("postal_code")),
                    url=ACRA_COLLECTION, ssic_codes=codes, industry_line=industry_line)


def _acra_call(params: dict, stop_event=None) -> list:
    for attempt in (1, 2):
        status, body = polite_get(ACRA_API, params=params, headers={"Accept": "application/json"}, stop_event=stop_event)
        if status == 429 and attempt == 1:  # "Please try again in 10 seconds": once, then leave it
            deadline = time.monotonic() + RETRY_WAIT_S
            while time.monotonic() < deadline and not (stop_event is not None and stop_event.is_set()):
                time.sleep(0.25)
            continue
        if status == 429:
            raise Unavailable("data.gov.sg said too many requests; try Refresh in a minute")
        if status >= 400:
            raise Unavailable(f"data.gov.sg answered HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError:
            raise Unavailable("data.gov.sg didn't send data") from None
        if not data.get("success"):
            raise Unavailable(f"data.gov.sg: {str(data.get('error') or data.get('errorMsg') or 'no result')[:80]}")
        return (data.get("result") or {}).get("records") or []
    return []


def legal_form(name_or_type: str) -> str:
    """'company', 'llp' or '' (a business, or a name without a legal ending)."""
    t = (name_or_type or "").strip()
    if _LLP_END.search(t) or "limited liability partnership" in t.lower():
        return "llp"
    if _COMPANY_END.search(t) or re.search(r"\bcompany\b", t, re.I):
        return "company"
    return ""


def _pick(records: list, name: str) -> tuple[dict | None, str]:
    """The one record for this name, or (None, why not). A name with a legal ending ('Pte Ltd', 'LLP') only
    matches a registered entity of that form: 'Acme Pte Ltd' is never the sole proprietorship 'ACME'."""
    want = legal_form(name)
    by_uen = {}
    for r in records:
        form = legal_form(_na(r.get("entity_type_description")))
        if name_key(r.get("entity_name")) == name_key(name) and r.get("uen") and (not want or form == want):
            by_uen.setdefault(r["uen"], r)
    if not by_uen:
        return None, "no company registered under this name"
    live = [r for r in by_uen.values() if _LIVE.match(_na(r.get("entity_status_description")))]
    if len(live) == 1:
        return live[0], ""
    if not live and len(by_uen) == 1:
        return next(iter(by_uen.values())), ""
    return None, f"{len(live) or len(by_uen)} registered entities share this name, so none was picked"


def acra_lookup(name: str, stop_event=None) -> Registry:
    """The registry record for exactly this company. Raises Unavailable (with the reason) when there isn't one
    clear match."""
    base = registry_base(name)
    if len(name_key(name)) < 2:
        raise Unavailable("no company name to look up")
    dataset = dataset_for(name)
    records = _acra_call({"resource_id": dataset, "limit": 20,
                          "filters": json.dumps({"entity_name": name_variants(name)})}, stop_event)
    rec, why = _pick(records, name)
    if rec is None and not records:  # spelt differently ('PTE.LTD.', 'A.B.C.'): by its words
        records = _acra_call({"resource_id": dataset, "limit": 100, "q": base}, stop_event)
        rec, why = _pick(records, name)
    if rec is None:
        raise Unavailable(why)
    return registry_text(rec)


# ── web search: the company's own website ─────────────────────────────

DDG_HTML = "https://html.duckduckgo.com/html/"
_CHALLENGE = re.compile(r"anomaly-modal|anomaly\.js|captcha|are you a robot|unusual traffic|challenge-form|"
                        r"bots use duckduckgo too", re.I)


def search_query(company: str) -> str:
    base = registry_base(company).title() or company
    return f'"{base}" Singapore'


def parse_ddg(html: str) -> list[tuple[str, str]]:
    """DuckDuckGo's HTML results -> [(url, title)], its redirect links unwrapped."""
    from bs4 import BeautifulSoup
    out = []
    for a in BeautifulSoup(html or "", "html.parser").select("a.result__a"):
        href = a.get("href") or ""
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href if "://" in href else "https:" + href).query).get("uddg", [""])[0])
        if href.startswith(("http://", "https://")):
            out.append((href, clean_whitespace(a.get_text(" "))))
    return out


def web_search(company: str, stop_event=None) -> list[tuple[str, str]]:
    status, body = polite_get(DDG_HTML, params={"q": search_query(company), "kl": "sg-en"},
                              headers={"Accept": "text/html", "Accept-Language": "en-SG,en;q=0.8"},
                              stop_event=stop_event)
    if status in (202, 403, 429) or (_CHALLENGE.search(body or "") and "result__a" not in (body or "")):
        raise Unavailable("the search showed a robot check")
    if status >= 400:
        raise Unavailable(f"the search answered HTTP {status}")
    return parse_ddg(body)


# ── Wikipedia ─────────────────────────────────────────────────────────

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_COMPANY_WORDS = re.compile(r"\b(company|companies|corporation|conglomerate|firm|manufacturer|multinational|"
                            r"business|enterprise|retailer|bank|airline|subsidiary|agency|provider|developer|"
                            r"publisher|studio|university|hospital|statutory board|group|maker|operator|"
                            r"supplier|chain|brand|startup|start-up|consultancy)\b", re.I)
# words a regional subsidiary adds to its group's name: 'Applied Materials South East Asia', 'X Singapore'
REGION_WORDS = {"south", "east", "southeast", "west", "north", "asia", "pacific", "apac", "sea", "singapore", "sg",
                "s"}


@dataclass
class Wiki:
    title: str
    url: str
    text: str
    website: str
    how: str  # why it is taken to be this company


_FOREIGN_ENDS = {"inc", "incorporated", "corp", "corporation", "co", "plc", "llc", "ag", "gmbh", "sa", "nv", "bv"}


def _words(name: str) -> list[str]:
    """The words of a name without its legal ending, Singapore's or another country's ('Applied Materials, Inc.')."""
    words = re.findall(r"[a-z0-9]+", registry_base(name).lower().replace("&", " and "))
    while len(words) > 1 and words[-1] in _FOREIGN_ENDS:
        words.pop()
    return words


def wiki_match(title: str, company: str) -> str:
    """'same' if the article's title is the company's name, 'group' if the company's name is the title plus region
    words (a regional subsidiary), else ''."""
    t = _words(re.sub(r"\s*\(.*?\)\s*", " ", title))
    c = _words(company)
    if not t or not c:
        return ""
    if t == c:
        return "same"
    if len(c) > len(t) and c[:len(t)] == t and all(w in REGION_WORDS for w in c[len(t):]):
        return "group"
    return ""


def wikipedia(company: str, known_sites: list[str], posting_text: str, stop_event=None) -> Wiki:
    """The Wikipedia article on this company, when it's clearly the one. Raises Unavailable otherwise."""
    base = registry_base(company)
    words = _words(company)
    short = " ".join(w for w in base.split() if w.lower() not in REGION_WORDS) or base
    if len("".join(words)) < 4:
        raise Unavailable("the name is too short to look up")
    status, body = polite_get(WIKI_API, params={
        "action": "query", "format": "json", "formatversion": "2", "generator": "search", "gsrsearch": short.title(),
        "gsrlimit": "5", "prop": "extracts|pageprops|info", "inprop": "url", "exintro": "1", "explaintext": "1",
        "exlimit": "5", "exchars": "1500", "ppprop": "wikibase_item|disambiguation", "redirects": "1"},
        headers={"User-Agent": WIKI_UA}, stop_event=stop_event)
    if status >= 400:
        raise Unavailable(f"Wikipedia answered HTTP {status}")
    try:
        pages = (json.loads(body).get("query") or {}).get("pages") or []
    except ValueError:
        raise Unavailable("Wikipedia didn't send data") from None
    for p in sorted(pages, key=lambda p: p.get("index", 99)):
        kind = wiki_match(p.get("title") or "", company)
        extract = clean_whitespace(p.get("extract") or "")
        if not kind or "disambiguation" in (p.get("pageprops") or {}) or not _COMPANY_WORDS.search(extract[:600]):
            continue
        website = _official_site((p.get("pageprops") or {}).get("wikibase_item") or "", stop_event)
        from .company import host_of  # (company imports this module)
        site_host = host_of(website).removeprefix("www.")
        same_site = bool(site_host) and any(host_of(u).removeprefix("www.") == site_host for u in known_sites)
        if kind == "group" and ("singapore" in extract.lower() or same_site or _says(posting_text, p.get("title") or "")):
            how = "group"  # 'X South East Asia Pte Ltd', and the article on X: always said to be the group's
        elif "singapore" in extract.lower() or same_site:
            how = "same company"
        elif kind == "same" and len(words) >= 2 and _says(posting_text, p.get("title") or "") \
                and re.search(r"\b(global|worldwide|multinational|listed|nasdaq|nyse|sgx|fortune)\b", posting_text or "", re.I):
            how = "same company"
        else:
            raise Unavailable(f"the article \"{p.get('title')}\" may be a different company with the same name, "
                              "so it wasn't used")
        return Wiki(title=p.get("title") or "", url=p.get("fullurl") or p.get("canonicalurl") or "",
                    text=extract, website=website, how=how)
    raise Unavailable("no article on this company")


def _says(text: str, title: str) -> bool:
    """Does the posting name the article's subject ('Applied Materials', without '(company)')?"""
    name = re.sub(r"\s*\(.*?\)\s*", " ", title).strip().lower()
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", (text or "").lower()) is not None


def _official_site(qid: str, stop_event=None) -> str:
    if not re.fullmatch(r"Q\d+", qid or ""):
        return ""
    try:
        status, body = polite_get(WIKIDATA_API, params={"action": "wbgetclaims", "entity": qid, "property": "P856",
                                                        "format": "json"},
                                  headers={"User-Agent": WIKI_UA}, stop_event=stop_event)
        claims = (json.loads(body).get("claims") or {}).get("P856") or [] if status < 400 else []
    except (Unavailable, ValueError, AttributeError):
        return ""
    for c in claims:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return ""
