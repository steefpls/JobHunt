"""Rebuild PDFs: bring the facts in a job's resume.html / cover_letter.html up to date with your Profile.

A package's HTML is written once, when the AI tailors it, and you may edit it by hand
afterwards. So Rebuild doesn't re-make it: it only swaps the FACT slots for what your
Profile says now (your name and contact details; each employer, job title and dates;
each school and its dates) and leaves every other word as it is. No AI call.

A resume entry is matched to a line in your Profile's Experience or Education section
by its employer (or school), or else by its title and dates. One it can't match surely
is left alone and named in the result, so you know to check it.

profile_changes() is the job page's "Your Profile changed after these were made" notice: what a
Rebuild would change, when the Profile was saved after the PDFs were made.

The cover letter's date is the day it was made, read back from its date line (letter_date). Rebuild dates it
today, and so does Approve & apply just before the agent can attach it (hunt.date_letter_today); stale_letter_date()
is the job page's "The cover letter is dated ..." notice.
"""
import html as htmlmod
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import paths, profile, render

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s+)?(?:19|20)\d{{2}}|(?:0?[1-9]|1[0-2])/(?:19|20)\d{{2}}"
_END = rf"{_DATE}|present|current|now|today|ongoing"
DATES_RE = re.compile(rf"(?:{_DATE})(?:\s*(?:-|–|—|to|until|till)\s*(?:{_END}))?", re.I)
_TOKEN_RE = re.compile(rf"{_MONTH}|(?:19|20)\d{{2}}|present|current|now|today|ongoing", re.I)
_NOW = ("present", "current", "now", "today", "ongoing")
_SPLIT_RE = re.compile(r"\s+[-–—|@]\s+|\s*\|\s*|\s+at\s+")


@dataclass
class Line:
    """One role or school in your Profile: its text split into fields, and its dates."""
    fields: list[str]
    dates: str


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(dates: str) -> list[str]:
    """'Mar 2023 to present' -> ['mar', '2023', 'present'] (what the dates say, not how they're written)."""
    out = []
    for t in _TOKEN_RE.findall(dates or ""):
        t = t.lower().rstrip(".")
        out.append(t if t.isdigit() else "present" if t in _NOW else t[:3])
    return out


def profile_lines(profile_text: str) -> dict[str, list[Line]]:
    """{'experience': [...], 'education': [...]}: the headline of each role and school in the Profile."""
    out = {"experience": [], "education": []}
    section = None
    for raw in (profile_text or "").splitlines():
        s = raw.strip()
        if re.match(r"#{1,2}\s", s):
            name = s.lstrip("#").strip().lower()
            section = ("experience" if re.search(r"experience|employment|work history|career", name) else
                       "education" if "education" in name else None)
            continue
        if not section or not s or raw[:1] in (" ", "\t"):  # indented: a bullet under a role, not a role
            continue
        s = re.sub(r"^(?:[-*•]|#+)\s*", "", s).replace("**", "").strip()
        found = list(DATES_RE.finditer(s))
        if not found:
            continue
        m = found[-1]  # the dates are usually last
        rest = (s[:m.start()] + " - " + s[m.end():]).replace("()", " ").replace("( )", " ")
        fields = [f.strip(" ,;:()[]|-–—") for f in _SPLIT_RE.split(rest)]
        out[section].append(Line([f for f in fields if f], m.group(0).strip()))
    return out


def _same(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)))


def new_dates(old: str, profile_dates: str) -> str:
    """The resume's dates with what the Profile says now, kept in the resume's own format where possible:
    'Jan 2019 - Dec 2021' + Profile 'Jan 2018 to Dec 2021' -> 'Jan 2018 - Dec 2021'."""
    want, have = _tokens(profile_dates), _tokens(old)
    if not want or want == have:
        return old
    spans = list(_TOKEN_RE.finditer(old))
    if len(spans) != len(want) or [t.isdigit() for t in have] != [t.isdigit() for t in want]:
        return profile_dates  # shaped differently: the Profile's own words
    out, last = [], 0
    for m, old_t, new_t, new_m in zip(spans, have, want, _TOKEN_RE.finditer(profile_dates)):
        out.append(old[last:m.start()])
        out.append(m.group(0) if old_t == new_t else new_m.group(0))
        last = m.end()
    return "".join(out) + old[last:]


def _match(lines: list[Line], key: str, sub: str, dates: str) -> tuple[Line | None, int]:
    """The Profile line for a resume entry: by employer/school, else by title/degree AND dates.
    Returns (line, index of the field that is the employer/school, or -1)."""
    by_key = [(l, i) for l in lines for i, f in enumerate(l.fields) if _same(f, key)]
    if by_key:
        if len({id(l) for l, _ in by_key}) > 1:  # two roles at one employer: the title decides, then the dates
            by_key = ([(l, i) for l, i in by_key if any(_norm(f) == _norm(sub) for f in l.fields)]
                      or [(l, i) for l, i in by_key if any(_same(f, sub) for f in l.fields)]
                      or [(l, i) for l, i in by_key if _tokens(l.dates) == _tokens(dates)])
        return by_key[0] if len({id(l) for l, _ in by_key}) == 1 else (None, -1)
    same_dates = [l for l in lines if _tokens(l.dates) == _tokens(dates)]
    by_sub = ([l for l in same_dates if any(_norm(f) == _norm(sub) for f in l.fields)]
              or [l for l in same_dates if any(_same(f, sub) for f in l.fields)])
    return (by_sub[0], -1) if len(by_sub) == 1 else (None, -1)


_ENTRY_RE = re.compile(r'(<div class="entry">\s*<div class="row"><span class="left">)(.*?)(</span><span class="right">)'
                       r'(.*?)(</span></div>\s*<div class="row sub"><span class="left">)(.*?)(</span><span class="right">)'
                       r'(.*?)(</span></div>)', re.S)
_SECTION_RE = re.compile(r"(<section>\s*<h2>)(.*?)(</h2>.*?</section>)", re.S)


def _fix_entries(section_html: str, lines: list[Line], kind: str, changes: list[str], unmatched: list[str]) -> str:
    def fix(m):
        g = list(m.groups())
        key, sub, dates = (htmlmod.unescape(g[i]) for i in (1, 5, 7))
        line, ki = _match(lines, key, sub, dates)
        if line is None:
            unmatched.append(key or sub)
            return m.group(0)
        new = {"key": key, "sub": sub, "dates": new_dates(dates, line.dates)}
        f = line.fields
        # The app's own Profile shape: 'Title - Employer - Place - Dates' and 'School - Degree - Dates'.
        # (The place is left as written: "SG" and "Singapore" are both right.)
        if kind == "experience" and len(f) >= 2:
            if ki == 1 or (ki == -1 and _same(f[0], sub)):
                new["sub"], new["key"] = f[0], f[1]
        elif kind == "education" and ki == -1 and len(f) >= 2 and _same(f[1], sub):
            new["key"] = f[0]
        what = {"key": "employer" if kind == "experience" else "school", "sub": "title" if kind == "experience"
                else "degree", "dates": "dates"}
        for k, i in (("key", 1), ("sub", 5), ("dates", 7)):
            old = htmlmod.unescape(g[i])
            if _norm(new[k]) != _norm(old):  # not for a dot or a capital: "Pte. Ltd." is "Pte Ltd"
                changes.append(f"{key}: {what[k]} {old or '(none)'} → {new[k]}")
                g[i] = htmlmod.escape(new[k], quote=False)
        return "".join(g)
    return _ENTRY_RE.sub(fix, section_html)


def _block(html: str, pattern: str) -> re.Match | None:
    return re.search(pattern, html, re.S)


def refresh_resume(html: str, contact: dict, profile_text: str) -> tuple[str, list[str], list[str]]:
    """(new html, what changed, entries it couldn't match to the Profile)."""
    changes, unmatched = [], []
    fresh = render.resume_html(contact, {})
    html = _swap(html, fresh, r'<h1>.*?</h1>', changes, "your name")
    html = _swap(html, fresh, r'<div class="contact">.*?</div>', changes, "your contact details")
    html = _retitle(html, fresh, changes)
    lines = profile_lines(profile_text)

    def section(m):
        name = htmlmod.unescape(m.group(2)).strip().lower()
        kind = "experience" if name == "experience" else "education" if name == "education" else None
        if not kind:
            return m.group(0)
        return _fix_entries(m.group(0), lines[kind], kind, changes, unmatched)
    return _SECTION_RE.sub(section, html), changes, unmatched


def refresh_cover_letter(html: str, contact: dict, company: str, role: str) -> tuple[str, list[str]]:
    changes = []
    fresh = render.cover_letter_html(contact, company, role, {})
    for pattern, what in ((r'<div class="name">.*?</div>', "your name"),
                          (r'<div class="meta">.*?</div>', "your contact details"),
                          (r'<div class="sign">.*?</div>', "your name")):
        html = _swap(html, fresh, pattern, changes, what)
    return _retitle(html, fresh, changes), sorted(set(changes), key=changes.index)


_DATE_LINE_RE = re.compile(r'(<div class="date">)(.*?)(</div>)', re.S)


def today() -> date:
    return date.today()  # (tests move the clock here)


def letter_date(html: str) -> date | None:
    """The day a cover letter is dated, from its date line ("13 October 2026"). None when there's no line,
    or it was edited into something that isn't a date."""
    m = _DATE_LINE_RE.search(html or "")
    text = " ".join(htmlmod.unescape(m.group(2)).split()) if m else ""
    for fmt in (render.LETTER_DATE, "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def redate(html: str, on: date) -> str:
    """The cover letter dated `on` when it's dated before then; every other word as it was. A letter with no
    date line, one you edited into something that isn't a date, or one dated later is left as it is."""
    made = letter_date(html)
    if not made or made >= on:
        return html
    return _DATE_LINE_RE.sub(lambda m: m.group(1) + on.strftime(render.LETTER_DATE) + m.group(3), html, count=1)


def _swap(html: str, fresh: str, pattern: str, changes: list[str], what: str) -> str:
    """Put the freshly rendered block where the old one is (when the file still has it)."""
    old, new = _block(html, pattern), _block(fresh, pattern)
    if not old or not new or old.group(0) == new.group(0):
        return html
    changes.append(what)
    return html[:old.start()] + new.group(0) + html[old.end():]


def _retitle(html: str, fresh: str, changes: list[str]) -> str:
    """The <title> (the PDF's name in a viewer) starts with your name."""
    old, new = _block(html, r"<title>(.*?) - "), _block(fresh, r"<title>(.*?) - ")
    if old and new and old.group(1) != new.group(1):
        html = html[:old.start(1)] + new.group(1) + html[old.end(1):]
    return html


def refresh_package(folder: Path, company: str, role: str, contact: dict,
                    profile_text: str) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Each document in the folder as (name, html now, html with your Profile's facts), what would change,
    and the resume entries it couldn't match to the Profile. Writes nothing."""
    docs, changes, unmatched = [], [], []
    for name in ("resume", "cover_letter"):
        path = folder / f"{name}.html"
        if not path.exists():
            continue
        html = path.read_text(encoding="utf-8")
        if name == "resume":
            new, done, missed = refresh_resume(html, contact, profile_text)
            unmatched += missed
        else:
            new, done = refresh_cover_letter(html, contact, company, role)
        changes += [c for c in done if c not in changes]
        docs.append((name, html, new))
    return docs, changes, unmatched


# A job in one of these has had its documents sent (or is sending them): they're history, so no notice.
SENT_STATES = ("Applying", "Applied", "Interview", "Offer", "Rejected", "Not for me")


def profile_changes(job: dict, folder: Path, contact: dict | None = None, profile_text: str | None = None) -> list[str]:
    """What Rebuild PDFs would change in this job's PDFs because you changed your Profile after they were
    made ([] = nothing). Only the Profile text and contact details feed the documents (the screening
    answers don't), and a save that changed none of the facts Rebuild brings in counts for nothing.
    (It compares with the Profile as it is now, so a fact you edited by hand in resume.html shows here too
    once the Profile is saved again: Rebuild would put the Profile's version back, so that's worth knowing.)"""
    if not job.get("folder") or job.get("state") in SENT_STATES:
        return []
    try:
        built = [p.stat().st_mtime_ns for p in (folder / "resume.pdf", folder / "cover_letter.pdf") if p.is_file()]
        saved = [p.stat().st_mtime_ns for p in (paths.profile_path(), profile.contact_path()) if p.is_file()]
        if not built or not saved or max(saved) <= min(built):
            return []  # no documents yet, or nothing saved since they were made
        return refresh_package(folder, job["company"], job["role"],
                               profile.load_contact() if contact is None else contact,
                               profile.load_profile() if profile_text is None else profile_text)[1]
    except Exception:  # an unreadable file costs the notice, never the page
        return []


def stale_letter_date(job: dict, folder: Path) -> date | None:
    """The day this job's cover letter is dated, when that's before today and it hasn't been sent (else None)."""
    if not job.get("folder") or job.get("state") in SENT_STATES:
        return None
    try:
        if not (folder / "cover_letter.pdf").is_file():
            return None
        made = letter_date((folder / "cover_letter.html").read_text(encoding="utf-8"))
        return made if made and made < today() else None
    except Exception:  # as above: never the page
        return None
