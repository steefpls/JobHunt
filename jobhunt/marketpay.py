"""Market pay: what similar jobs in Singapore pay, next to what this one says it pays.

Every MyCareersFuture ad has to state its pay, so the jobs board's public search is a fair
sample of the market. JobHunt searches it for the job's title, keeps the ads whose titles
really are the same job at the same level, and takes the middle of their ranges.
No AI and no tokens: one search per job title, kept for a week.
"""
import json
import re
import statistics
import time
from urllib.parse import quote

import httpx

from . import db, paths
from .extract import salary_range_monthly

SEARCH_URL = "https://api.mycareersfuture.gov.sg/v2/search"
SEARCH_PAGE = "https://www.mycareersfuture.gov.sg/search"
KEEP_S = 7 * 24 * 60 * 60  # a search is reused for a week
MIN_ADS = 5  # fewer similar ads than this: too few to call it the market
UA = "JobHunt (market pay; +https://github.com/steefpls/JobHunt)"

LEVELS = {  # words in a title that say how senior the job is
    "junior": "junior", "jr": "junior", "associate": "junior", "graduate": "junior", "entry": "junior",
    "trainee": "junior", "intern": "intern", "internship": "intern",
    "senior": "senior", "sr": "senior", "lead": "senior", "principal": "senior", "staff": "senior",
    "head": "senior", "chief": "senior", "director": "senior", "vp": "senior",
}
LEVEL_WORDS = {"junior": "junior", "intern": "intern", "senior": "senior", "": "mid-level"}
# words that say nothing about what the job is
NOISE = {"singapore", "sg", "urgent", "hiring", "immediate", "immediately", "contract", "permanent", "temp",
         "temporary", "full", "part", "time", "fulltime", "parttime", "month", "months", "year", "years", "yr",
         "mth", "up", "to", "sgd", "salary", "bonus", "aws", "hybrid", "remote", "wfh", "west", "east", "north",
         "south", "central", "new", "the", "and", "of", "for", "a", "an", "in", "at", "with", "i", "ii", "iii",
         "1", "2", "3", "x"}


def http_post(url: str, body: dict, timeout: float = 20) -> dict:
    """The jobs board's answer as JSON. The tests swap this."""
    with httpx.Client(headers={"User-Agent": UA}, timeout=timeout) as c:
        r = c.post(url, json=body)
        r.raise_for_status()
        return r.json()


def _words(title: str) -> list[str]:
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", title.lower())
    title = re.split(r"\s+[-–|/]\s+|\s*@\s*", title)[0]  # "Backend Engineer - TikTok Search": the job, not the team
    title = re.sub(r"\$?\d[\d,.]*k?", " ", title)
    return [w for w in re.findall(r"[a-z][a-z+#.]*", title.replace("&", " ")) if w.strip(".")]


def describe(title: str) -> tuple[frozenset, str, str]:
    """(what the job is, how senior, the words to search for).
    'Senior Backend Engineer (AWS) - Payments' -> ({'backend', 'engineer'}, 'senior', 'senior backend engineer')."""
    words = _words(title)
    level = next((LEVELS[w] for w in words if w in LEVELS), "")
    core = frozenset(w for w in words if w not in LEVELS and w not in NOISE)
    query = " ".join(w for w in words if w not in NOISE)
    return core, level, query


def same_job(core: frozenset, other: frozenset) -> bool:
    if not core or not other:
        return False
    return core <= other and len(other - core) <= 1 or len(core & other) / len(core | other) >= 0.6


def _cache_file():
    return paths.data_dir() / "marketpay.json"


def _search(query: str) -> list[dict]:
    """The ads MyCareersFuture finds for these words, most relevant first (kept a week)."""
    try:
        cache = json.loads(_cache_file().read_text(encoding="utf-8"))
        cache = cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(query)
    if hit and time.time() - hit.get("at", 0) < KEEP_S:
        return hit["ads"]
    data = http_post(f"{SEARCH_URL}?limit=100&page=0", {"search": query})
    ads = []
    for r in data.get("results") or []:
        sal = r.get("salary") or {}
        lo, hi = sal.get("minimum") or 0, sal.get("maximum") or sal.get("minimum") or 0
        period = ((sal.get("type") or {}).get("salaryType") or "")
        if not lo or "month" not in period.lower():
            continue  # hourly or yearly pay isn't comparable with a month's
        company = (r.get("hiringCompany") or {}).get("name") or (r.get("postedCompany") or {}).get("name") or ""
        ads.append({"title": r.get("title") or "", "company": company, "min": float(lo), "max": float(hi)})
    cache = {k: v for k, v in cache.items() if time.time() - v.get("at", 0) < KEEP_S}
    cache[query] = {"at": time.time(), "ads": ads}
    try:
        _cache_file().write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return ads


def _k(n: float) -> str:
    """5500 -> '$5.5k', 12000 -> '$12k'."""
    return f"${n / 1000:.1f}k".replace(".0k", "k") if n < 10_000 else f"${n / 1000:.0f}k"


def estimate(title: str, stated_pay: str = "") -> dict:
    """What similar jobs pay. {"status": "ok" | "few" | "error", ...}; see the job page for how it's shown."""
    core, level, query = describe(title)
    out = {"status": "few", "query": query, "level": LEVEL_WORDS[level], "checked": time.strftime("%Y-%m-%d"),
           "search_url": f"{SEARCH_PAGE}?search={quote(query)}"}
    if not core:
        return {**out, "why": "The job title is too vague to compare."}
    try:
        ads = _search(query)
        if len(ads) < MIN_ADS and level:  # "Senior X" found little: look for X at every level too
            ads += [a for a in _search(" ".join(sorted(core))) if a not in ads]
    except Exception as e:
        return {**out, "status": "error", "why": f"Couldn't reach MyCareersFuture ({type(e).__name__})."}
    similar, seen = [], set()
    for a in ads:
        a_core, a_level, _ = describe(a["title"])
        key = (a["company"].lower(), " ".join(sorted(a_core)), a["min"], a["max"])  # one firm's copies count once
        if same_job(core, a_core) and key not in seen:
            seen.add(key)
            similar.append((a, a_level))
    same_level = [a for a, lv in similar if lv == level]
    pool, all_levels = (same_level, False) if len(same_level) >= MIN_ADS else ([a for a, _ in similar], True)
    if len(pool) < MIN_ADS:
        return {**out, "ads": len(pool), "why": f"Only {len(pool)} similar ad{'s' if len(pool) != 1 else ''} on "
                                                 f"MyCareersFuture right now: too few to call it the market."}
    low = statistics.median(a["min"] for a in pool)
    high = statistics.median(a["max"] for a in pool)
    mid = statistics.median((a["min"] + a["max"]) / 2 for a in pool)
    out.update(status="ok", ads=len(pool), all_levels=all_levels, low=low, high=high, mid=mid,
               text=f"{_k(low)}–{_k(high)} a month (typical {_k(mid)})")
    lo, hi = salary_range_monthly(stated_pay)
    if lo:
        job_mid = (lo + hi) / 2
        out["job"] = f"{_k(lo)}–{_k(hi)}" if hi > lo else _k(lo)
        out["verdict"] = ("above" if job_mid > mid * 1.1 else "below" if job_mid < mid * 0.9 else "in line")
    return out


def check(job_id: int) -> dict | None:
    """Work out and save market pay for one job. Never raises."""
    job = db.get_job(job_id)
    if not job or not job.get("role"):
        return None
    try:
        result = estimate(job["role"], job.get("salary") or "")
    except Exception as e:  # a bit of extra information must never break a hunt or a page
        result = {"status": "error", "why": f"Couldn't work out market pay ({type(e).__name__})."}
    db.update_job(job_id, market_pay=json.dumps(result))
    return result


def load(job: dict) -> dict | None:
    try:
        data = json.loads(job.get("market_pay") or "")
        return data if isinstance(data, dict) else None
    except ValueError:
        return None
