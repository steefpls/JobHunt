"""Where the job is worked: remote, hybrid or on-site, when the posting says so. No AI: the posting's own
words (and MyCareersFuture's flexible-work ticks, which sites.py adds to the posting text), read with
patterns that need a work word nearby, so "hybrid cloud" or "remote monitoring" never count.
Works on jobs saved before this existed too: it only reads what the tracker already keeps."""
import re

# MyCareersFuture's own ticks, as sites.py writes them into the posting text
FLEX_LINE = "Flexible work arrangements: "
FLEX_LABELS = {"telecommuting": "Work from home possible", "flexi-hours": "Flexi-hours",
               "staggered time": "Staggered hours", "employees choice of days off": "Choose your days off",
               "creative scheduling": "Flexible schedule"}

_WORK = r"(?:work(?:ing)?|role|position|job|arrangement|set[- ]?up|model|environment|schedule|basis|opportunity)"
PATTERNS = [  # (kind, pattern), strongest first: a hybrid job often says "remote" and "office" too
    ("Hybrid", rf"\bhybrid\s+(?:{_WORK}|\(|-)|\b{_WORK}\s*(?:arrangement|mode|model|type)?\s*(?::|-|is|of)?\s*hybrid\b"
               r"|\b\d\s*(?:days?|x)\s*(?:a|per|/)?\s*week\s*(?:in|at|from)\s*(?:the\s+)?(?:office|home)"
               r"|\b\d\s*days?\s*(?:in|at)\s*(?:the\s+)?office\b|\b\d\s*days?\s*(?:wfh|work(?:ing)? from home)\b"
               r"|\bwfh\s*\d\s*days?\b"),
    ("Remote", rf"\b(?:fully|100%|completely|entirely|permanently)\s+remote\b|\bremote[- ](?:first|only)\b"
               rf"|\bremote\s+{_WORK}|\b{_WORK}\s+(?:is\s+)?(?:fully\s+)?remote(?:ly)?\b|\bwork\s+from\s+anywhere\b"
               r"|\bwork(?:ing)?\s+remotely\b"),
    ("On-site", rf"\b(?:fully|100%|5\s*days?)\s+(?:on[- ]?site|in[- ](?:the[- ])?office|office[- ]based)\b"
                rf"|\b(?:on[- ]?site|office[- ]based|in[- ]office)\s+{_WORK}"
                rf"|\b{_WORK}\s*(?:arrangement|mode|type)?\s*(?::|-|is)\s*(?:fully\s+)?(?:on[- ]?site|office[- ]based|in[- ]office)\b"
                r"|\bno\s+(?:remote|wfh|work from home)\b|\bnot\s+(?:a\s+)?remote\b"
                r"|\b(?:workplace|work\s+location)\s*(?:type)?\s*:\s*(?:on[- ]?site|in[- ]office|office)\b"),
]
_RE = [(kind, re.compile(p, re.I)) for kind, p in PATTERNS]
_LOCATION = [("Hybrid", re.compile(r"\bhybrid\b", re.I)), ("Remote", re.compile(r"\bremote\b", re.I))]


def _sentence(text: str, m: re.Match) -> str:
    """The words around a match, to show what the posting said."""
    start = max(text.rfind(c, 0, m.start()) for c in ".\n•;") + 1
    ends = [i for i in (text.find(c, m.end()) for c in ".\n•;") if i != -1]
    s = " ".join(text[start:min(ends) if ends else len(text)].split()).lstrip("-–*· ")
    return s if len(s) <= 160 else s[:157].rstrip() + "..."


def read(posting_text: str, location: str = "") -> dict:
    """{"kind": "Remote" | "Hybrid" | "On-site" | "", "quote": what the posting said, "flex": [MCF's ticks]}."""
    text = posting_text or ""
    flex = []
    for line in text.splitlines():
        if line.startswith(FLEX_LINE):
            flex = [FLEX_LABELS.get(a.strip().lower(), a.strip()) for a in line[len(FLEX_LINE):].split(",") if a.strip()]
    for kind, rx in _LOCATION:  # Indeed and LinkedIn put "Remote" or "Hybrid work in Singapore" in the location
        if rx.search(location or ""):
            return {"kind": kind, "quote": f"Location: {location}", "flex": flex}
    for kind, rx in _RE:
        m = rx.search(text)
        if m:
            return {"kind": kind, "quote": _sentence(text, m), "flex": flex}
    return {"kind": "", "quote": "", "flex": flex}


def of(job: dict) -> dict:
    """A tracker job's work arrangement, for the pages."""
    return read(job.get("posting_text") or "", job.get("location") or "")
