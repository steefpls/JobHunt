"""Build the files in a job folder from the model's structured output.

The model returns JSON (bullets, paragraphs, a requirement audit); this code
pours it into fixed templates. So every resume has the same Harvard layout,
the contact details are always exactly what's on the Profile page, and the
model never writes files itself.

Folder layout (data/jobs/<date>_<Company>_<Role>/):
  posting.pdf        the job posting as captured
  resume.pdf         tailored resume          (+ resume.html, editable source)
  cover_letter.pdf   tailored cover letter    (+ cover_letter.html)
  match_report.md    requirement-by-requirement audit, gaps, score
  apply_confirmation.pdf   only after a confirmed submission
"""
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import paths

_env = Environment(loader=FileSystemLoader(str(paths.TEMPLATES_DIR)), autoescape=select_autoescape(["j2", "html"]))


def web_url(url) -> str:
    """Only http(s) links from postings become clickable (no javascript: and friends)."""
    url = str(url or "").strip()
    return url if urlparse(url).scheme in ("http", "https") else "#"


def link_label(url, max_len: int = 48) -> str:
    """A short, one-line name for a link: 'sg.jobstreet.com/job/12345…'. The link itself keeps the full URL."""
    u = urlparse(str(url or "").strip())
    text = (u.netloc.removeprefix("www.") + u.path).rstrip("/") or str(url or "")
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


_env.filters["web_url"] = web_url
_env.filters["link_label"] = link_label

PACKAGE_FILES = ["posting.pdf", "resume.pdf", "cover_letter.pdf", "match_report.md"]


def safe_name(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return (text[:max_len].rstrip("-")) or "Unknown"


def make_job_folder(company: str, role: str) -> Path:
    base = f"{date.today().isoformat()}_{safe_name(company, 30)}_{safe_name(role, 40)}"
    folder = paths.jobs_dir() / base
    n = 2
    while folder.exists():
        folder = paths.jobs_dir() / f"{base}_{n}"
        n += 1
    folder.mkdir(parents=True)
    return folder


def _as_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return [x for x in v if x]
    return [v]


def normalise_resume(r: dict) -> dict:
    r = dict(r) if isinstance(r, dict) else {}
    order = [s for s in _as_list(r.get("section_order")) if s in ("experience", "education", "projects", "skills")]
    for s in ("experience", "education", "projects", "skills"):
        if s not in order:
            order.append(s)
    r["section_order"] = order
    for key in ("experience", "education", "projects"):
        items = []
        for e in _as_list(r.get(key)):
            if isinstance(e, dict):
                e = dict(e)
                e["bullets"] = _as_list(e.get("bullets"))
                e["details"] = _as_list(e.get("details"))
                items.append(e)
        r[key] = items
    skills = r.get("skills") or {}
    if isinstance(skills, list):
        skills = {"technical": ", ".join(map(str, skills))}
    elif not isinstance(skills, dict):
        skills = {"technical": str(skills)}
    r["skills"] = {k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in skills.items() if v}
    return r


MATCH_KEYS = ("requirements", "emphasized", "deemphasized", "gaps", "score", "score_reason")


def normalise_tailor(data: dict) -> dict:
    """Smooth over common model slips: match-report fields placed at the top
    level instead of inside match_report, and scores given out of 100."""
    data = dict(data or {})
    mr = data.get("match_report")
    mr = dict(mr) if isinstance(mr, dict) else {}
    for k in MATCH_KEYS:
        if k in data and not mr.get(k):
            mr[k] = data.pop(k)
    score = mr.get("score")
    try:
        score = float(score)
        if 10 < score <= 100:
            score = score / 10
        mr["score"] = round(max(0.0, min(10.0, score)), 1)
    except (TypeError, ValueError):
        mr["score"] = "?"
    data["match_report"] = mr
    data["resume"] = data["resume"] if isinstance(data.get("resume"), dict) else {}
    cl = data.get("cover_letter") or {}
    if isinstance(cl, (str, list)):  # just the paragraphs
        cl = {"paragraphs": cl.split("\n\n") if isinstance(cl, str) else cl}
    data["cover_letter"] = cl if isinstance(cl, dict) else {}
    return data


def package_problems(data: dict) -> list[str]:
    """What's missing from a (normalised) tailor reply. Empty list = complete enough to send."""
    problems = []
    r = normalise_resume(data.get("resume"))
    if not any(e.get("bullets") for e in r["experience"]):
        problems.append("the resume has no experience bullets")
    if not any(str(p).strip() for p in _as_list((data.get("cover_letter") or {}).get("paragraphs"))):
        problems.append("the cover letter has no text")
    mr = data.get("match_report") or {}
    if not _as_list(mr.get("requirements")) and mr.get("score") == "?":
        problems.append("the match report is empty")
    return problems


def resume_html(contact: dict, resume: dict) -> str:
    return _env.get_template("resume.html.j2").render(contact=contact, r=normalise_resume(resume))


# A paragraph that is only a sign-off ("Sincerely, Alex Lim"): the template signs the letter itself.
_SIGN_OFF_RE = re.compile(r"^(?i:(yours\s+)?(sincerely|faithfully|truly|respectfully|(best|kind|warm(est)?)\s+regards|"
                          r"regards|best wishes))[\s,.!]*([A-Z(][\w.'()-]*\s*){0,5}$")


def _letter_paragraphs(cl: dict) -> list[str]:
    """The model's paragraphs without a repeated greeting or a sign-off the template already adds."""
    paras = [str(p).strip() for p in _as_list(cl.get("paragraphs")) if str(p).strip()]
    greeting = str(cl.get("greeting") or "").strip().lower()
    if paras and greeting and paras[0].lower() == greeting:
        paras = paras[1:]
    while paras and len(paras[-1]) <= 80 and _SIGN_OFF_RE.match(paras[-1]):
        paras.pop()
    return paras


def cover_letter_html(contact: dict, company: str, role: str, cl: dict) -> str:
    cl = dict(cl or {})
    cl["paragraphs"] = _letter_paragraphs(cl)
    return _env.get_template("cover_letter.html.j2").render(
        contact=contact, company=company, role=role, cl=cl, date=datetime.now().strftime("%d %B %Y"))


def posting_html(posting) -> str:
    return _env.get_template("posting.html.j2").render(p=posting, captured=datetime.now().strftime("%Y-%m-%d %H:%M"))


def match_report_md(company: str, role: str, mr: dict, warnings: list[str], filter_reasons: list[str] | None = None) -> str:
    mr = mr or {}
    lines = [f"# Match report - {company} · {role}", ""]
    if warnings:
        lines += ["> **Check before sending:**"] + [f"> - {w}" for w in warnings] + [""]
    lines += ["## Key requirements (from the posting)"]
    for i, req in enumerate(_as_list(mr.get("requirements")), 1):
        if isinstance(req, dict):
            lines.append(f"{i}. {req.get('requirement', '')}  → addressed by: {req.get('addressed_by', '-')}  "
                         f"[{req.get('strength', '?')}]")
    for title, key in (("What this version emphasizes", "emphasized"), ("De-emphasized / omitted", "deemphasized"),
                       ("Gaps (requirements you don't fully meet - NOT invented in the resume)", "gaps")):
        lines += ["", f"## {title}"] + [f"- {x}" for x in _as_list(mr.get(key))] + ([] if mr.get(key) else ["- (none)"])
    if filter_reasons:
        lines += ["", "## Why it was picked"] + [f"- {x}" for x in filter_reasons]
    score = mr.get("score", "?")
    lines += ["", f"## Tailoring score: {score}/10 - {mr.get('score_reason', '')}", ""]
    return "\n".join(lines)


def report_html(md_text: str) -> str:
    """match_report.md -> HTML for the job page.

    The report quotes the posting (company, role, requirements) and the model, so
    it is untrusted text: every '<' is escaped before Markdown sees it, so no raw
    HTML can get through, and links and images are not turned into tags at all.
    """
    import markdown
    m = markdown.Markdown()
    for name in ("html", "link", "reference", "short_reference", "image_link", "image_reference",
                 "short_image_ref", "autolink", "automail"):
        if name in m.inlinePatterns:
            m.inlinePatterns.deregister(name)
    if "html_block" in m.preprocessors:
        m.preprocessors.deregister("html_block")
    return m.convert((md_text or "").replace("&", "&amp;").replace("<", "&lt;"))


def honesty_warnings(profile_text: str, resume: dict) -> list[str]:
    """Cheap code check: every employer/school on the resume must appear in the profile."""
    hay = re.sub(r"\s+", " ", (profile_text or "").lower())
    warnings = []
    r = normalise_resume(resume)
    for e in r["experience"]:
        name = (e.get("company") or "").strip()
        if name and _norm(name) not in _norm(hay):
            warnings.append(f"Employer '{name}' is not in your profile - check it was not invented.")
    for e in r["education"]:
        name = (e.get("school") or "").strip()
        if name and _norm(name) not in _norm(hay):
            warnings.append(f"School '{name}' is not in your profile - check it was not invented.")
    return warnings


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def list_files(folder: Path) -> list[dict]:
    if not folder or not folder.exists():
        return []
    order = {n: i for i, n in enumerate(PACKAGE_FILES + ["apply_confirmation.pdf", "resume.html", "cover_letter.html"])}
    files = [f for f in folder.iterdir() if f.is_file()]
    files.sort(key=lambda f: (order.get(f.name, 99), f.name))
    return [{"name": f.name, "size": f.stat().st_size} for f in files]
