"""What your rejections teach the search.

Two ways, both in your hands:
  1. The reasons you gave for your last few rejections go into the AI's yes/no prompt as
     your stated dislikes (fenced as data, like every posting).
  2. Right after a reject with a reason, JobHunt offers ONE concrete change to your Scope
     (a never-apply word, a line in the Avoid list, or a higher minimum pay). Nothing changes
     unless you press Yes. Suggestions come from the reason you picked and the job itself:
     plain code, no AI call.
"""
import math
import re

from . import db, profile, settings as settings_mod

# Title words that mean a level, for "Too senior" / "Too junior" (most telling first).
SENIOR_WORDS = ["principal", "director", "head", "chief", "vp", "lead", "senior", "sr", "staff", "manager"]
JUNIOR_WORDS = ["intern", "internship", "trainee", "apprentice", "graduate", "junior", "jr", "entry level", "assistant"]
# Title words that name a field of work, for "Not my field".
FIELD_WORDS = ["sales", "telesales", "telemarketing", "business development", "marketing", "recruiter", "recruitment",
               "hr", "accountant", "accounting", "audit", "tax", "finance", "insurance", "real estate", "property",
               "customer service", "call centre", "call center", "receptionist", "admin", "administrative",
               "retail", "teacher", "tutor", "nurse", "driver", "technician", "mechanic", "electrician", "chef",
               "cook", "cashier", "warehouse", "logistics", "security", "cleaner", "legal", "lawyer", "pharmacist"]

PROMPT_CAP = 1500  # characters of past rejections in each yes/no prompt
PROMPT_ROWS = 10


# ── 1. into the AI's yes/no prompt ────────────────────────────────────

def dislikes_text() -> str:
    """Your recent rejections with reasons, one per line ('' when there are none)."""
    lines = [f"- {r['role']} at {r['company']}: {db.reject_text(r)}" for r in db.recent_rejections(PROMPT_ROWS)]
    return "\n".join(lines)[:PROMPT_CAP]


# ── 2. one suggested change to your Scope ─────────────────────────────

def _hit(word: str, *texts: str) -> bool:
    from .hunt import _avoid_hit  # the same matching the hunt uses to skip
    return _avoid_hit([word], *texts) is not None


def _first_word(words: list[str], title: str, keywords: list[str]) -> str:
    """The first of `words` in the title that none of your search keywords uses."""
    for w in words:
        if _hit(w, title) and not any(_hit(w, k) for k in keywords):
            return w
    return ""


def company_word(company: str) -> str:
    """'TECHCOM SOLUTIONS (SINGAPORE) PTE. LTD.' -> 'TECHCOM SOLUTIONS': what to skip it by."""
    name = re.sub(r"\([^)]*\)", " ", company or "")
    words = [w for w in re.findall(r"[\w&'+.-]+", name) if w.lower().strip(".") not in db._LEGAL_WORDS]
    short = " ".join(words).strip(" .,-")
    for candidate in (short, (company or "").strip()):
        if candidate and _hit(candidate, company):
            return candidate
    return ""


def _one_line(text: str, limit: int = 150) -> str:
    return " ".join((text or "").split()).lstrip("-#*> ")[:limit].strip()


def _avoid_word(job: dict, word: str, question: str) -> dict:
    return {"kind": "avoid_word", "value": word, "job_id": job["id"], "question": question,
            "detail": f'Adds "{word}" to the never-apply words on your Scope page: postings with it in the title or '
                      "company are skipped without spending any AI tokens.",
            "button": f'Yes, skip "{word}"', "done": f'Done: "{word}" is now a never-apply word. You can change it '
                                                    "on the Scope page."}


def _scope_line(job: dict, line: str) -> dict:
    return {"kind": "scope_avoid", "value": line, "job_id": job["id"],
            "question": f'Add "{line}" to the Avoid list in your scope?',
            "detail": "The AI reads your scope to say yes or no to every posting.",
            "button": "Yes, add it", "done": "Done: added to the Avoid list in your scope. You can change it on the "
                                             "Scope page."}


def suggest(job: dict | None) -> dict | None:
    """ONE change to your Scope that would have kept this job away, from the reason you gave.
    None when there's nothing useful to offer (or your Scope already does it)."""
    if not job or job.get("state") != "Not for me":
        return None
    reasons = db.taste_reasons(job.get("reject_reason"))
    if not reasons and db.AD_CLOSED in (job.get("reject_reason") or "").split("; "):
        return None  # turned down because the ad closed: nothing in your Scope to change, the note included
    note = _one_line(job.get("reject_note"))
    s = settings_mod.load()
    avoid = s["scope"].get("avoid_words", [])
    keywords = [k for k in s["scope"].get("keywords", []) if k.strip()]
    title, company = job.get("role") or "", job.get("company") or ""
    scope_text = profile.load_scope_text().lower()

    def new_word(w):
        return w and not any(_hit(a, w) or a.strip().lower() == w.lower() for a in avoid if a.strip())

    def new_line(line):
        return line and line.lower() not in scope_text

    for reason in reasons:
        if reason == "Don't like the company":
            w = company_word(company)
            if new_word(w):
                return _avoid_word(job, w, f"Skip every job at {company} from now on?")
        elif reason in ("Too senior", "Too junior"):
            w = _first_word(SENIOR_WORDS if reason == "Too senior" else JUNIOR_WORDS, title, keywords)
            if new_word(w):
                return _avoid_word(job, w, f'Skip jobs with "{w}" in the title?')
        elif reason == "Not my field":
            w = _first_word(FIELD_WORDS, title, keywords)
            if new_word(w):
                return _avoid_word(job, w, f'Skip jobs with "{w}" in the title?')
            line = _one_line(f"Roles like {title}")
            if title and new_line(line):
                return _scope_line(job, line)
        elif reason == "Pay too low":
            found = _pay_suggestion(job)
            if found:
                return found
        elif reason == "Wrong location":
            line = _one_line(f"Jobs located in {job.get('location')}") if (job.get("location") or "").strip() else ""
            if new_line(line):
                return _scope_line(job, line)
    if note and new_line(note):  # your own words (or "Other" with a note)
        return _scope_line(job, note)
    return None


def _pay_suggestion(job: dict) -> dict | None:
    from .extract import salary_top_monthly
    floor = profile.salary_floor()
    top = salary_top_monthly(job.get("salary") or "")
    if top <= 0:
        if floor:
            return None
        return {"kind": "link", "href": "/profile#s_expected_salary", "job_id": job["id"],
                "question": "Set a minimum pay?",
                "detail": "Jobs whose pay tops out below your Expected salary (Profile page) are skipped.",
                "button": "Set it on the Profile page", "done": ""}
    new = int(math.floor(top / 500) + 1) * 500  # the next round figure above this job's top
    if new <= floor:
        return None
    return {"kind": "salary", "value": str(new), "job_id": job["id"],
            "question": f"Raise your minimum pay to SGD {new:,} a month?",
            "detail": f"This job paid up to SGD {top:,.0f}. Jobs that top out below your minimum are skipped. It is "
                      "your Expected salary on the Profile page, so application forms get this figure too"
                      + (f" (now: {profile.load_screening()['expected_salary']})." if floor else "."),
            "button": f"Yes, SGD {new:,}", "done": f"Done: your minimum pay (Expected salary) is now SGD {new:,} a month."}


def apply(sug: dict) -> None:
    """Make the change you said Yes to."""
    if sug["kind"] == "avoid_word":
        avoid = settings_mod.load()["scope"].get("avoid_words", [])
        settings_mod.update({"scope": {"avoid_words": avoid + [sug["value"]]}})
    elif sug["kind"] == "scope_avoid":
        profile.save_scope_text(add_avoid_line(profile.load_scope_text(), sug["value"]))
    elif sug["kind"] == "salary":
        answers = profile.load_screening()
        answers["expected_salary"] = f"SGD {int(sug['value']):,} per month"
        profile.save_screening(answers)


def add_avoid_line(scope_text: str, line: str) -> str:
    """The scope with '- line' at the end of its Avoid section (made if missing)."""
    lines = (scope_text or "").rstrip("\n").split("\n") if (scope_text or "").strip() else []
    start = next((i for i, l in enumerate(lines) if re.match(r"#+\s*avoid\b", l.strip(), re.I)), None)
    if start is None:
        return "\n".join(lines + ["", "## Avoid (do NOT apply)", f"- {line}"]).strip("\n") + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("#")), len(lines))
    section = [l for l in lines[start + 1:end] if l.strip() != "-"]  # drop the template's empty bullet
    while section and not section[-1].strip():
        section.pop()
    return "\n".join(lines[:start + 1] + section + [f"- {line}", ""] + lines[end:]).rstrip("\n") + "\n"
