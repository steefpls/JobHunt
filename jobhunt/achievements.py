"""Achievements: a little fun on top of the tracker. Worked out from real data only (jobs, their
state history, runs, spend), never shown to the AI and never put on a resume, letter or form.

Each kind has tiers (Bronze, Silver...). check() runs on every page: tiers earned since the last
page become pop-ups. The very first check only records what was already earned, with one pop-up
for the lot, so an existing tracker doesn't open with a flood of them.
"""
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import db, login_check, paths, profile, updates

TIER_NAMES = ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Legend", "Mythic"]
FLAGS = {"snoop", "old_school", "codebreaker", "party_time", "deja_vu"}  # hidden ones, set by what you do
SETTABLE_FLAGS = FLAGS - {"snoop"}  # snoop is set by visiting the page itself

GROUPS = ["Applying", "Outcomes", "Hunting", "When you hunt", "AI use", "Getting set up", "Hidden"]


@dataclass
class Kind:
    key: str
    name: str
    icon: str
    group: str
    unit: str  # "5 rejections": what the number counts
    tiers: list
    lower_better: bool = False  # Penny Pincher: a smaller number is better
    fmt: str = "n"  # n | tokens | usd | usd3
    hint: str = ""  # hidden ones: shown instead of the name until earned
    toasts: dict = field(default_factory=dict)  # tier index -> a special pop-up line


KINDS = [
    # Applying
    Kind("out_the_door", "Out the Door", "📮", "Applying", "applications sent", [1, 5, 10, 25, 50, 100, 250],
         toasts={0: "First one out the door!", 2: "Ten applications sent. Double digits!",
                 4: "Fifty applications. You're a machine."}),
    Kind("big_day", "Big Day", "📅", "Applying", "applications sent in one day", [3, 5, 10, 20]),
    Kind("same_day", "Same-Day Delivery", "⚡", "Applying", "jobs applied to within a day of being found", [1, 10, 50]),
    Kind("hands_on", "Hands-On", "🛠️", "Applying", "Needs-you jobs you finished yourself", [1, 5, 15, 40]),
    Kind("second_wind", "Second Wind", "🔁", "Applying", "Needs-you jobs JobHunt got through on Try again", [1, 5, 15]),
    Kind("globetrotter", "Globetrotter", "🧭", "Applying", "job sites you've applied on", [2, 3, 4]),
    # Outcomes
    Kind("foot_in_door", "Foot in the Door", "🚪", "Outcomes", "interviews", [1, 3, 5, 10, 20],
         toasts={0: "Your first interview! 🎉"}),
    Kind("options", "Options", "🤝", "Outcomes", "offers", [1, 2, 3, 5], toasts={0: "An offer! 🎉🎉"}),
    Kind("thick_skin", "Thick Skin", "🛡️", "Outcomes", "rejections", [1, 5, 10, 25, 50, 100],
         toasts={0: "First rejection. Everyone gets them; on to the next."}),
    Kind("ghost_town", "Ghost Town", "👻", "Outcomes", "applications with no reply after 30 days", [1, 10, 25, 50]),
    Kind("comeback", "Comeback", "📈", "Outcomes", "rejections before an interview", [10, 25, 50]),
    # Hunting
    Kind("the_hunt", "The Hunt Is On", "🎯", "Hunting", "hunts finished", [1, 5, 10, 25, 50, 100]),
    Kind("speed_reader", "Speed Reader", "📖", "Hunting", "postings judged", [10, 100, 500, 1000, 5000]),
    Kind("tailor_made", "Tailor Made", "🧵", "Hunting", "application packages built", [1, 10, 25, 50, 100, 250]),
    Kind("sharp_eye", "Sharp Eye", "🔍", "Hunting", "jobs rated 9/10 or better", [1, 5, 20, 50]),
    Kind("perfect_match", "Perfect Match", "💯", "Hunting", "jobs rated 10/10", [1, 3, 10]),
    Kind("streak", "Streak", "🔥", "Hunting", "days in a row with a hunt", [3, 7, 14, 30, 60]),
    Kind("tenure", "Tenure", "🗓️", "Hunting", "days since your first hunt", [7, 30, 90, 180, 365]),
    # When you hunt
    Kind("night_owl", "Night Owl", "🦉", "When you hunt", "hunts started between midnight and 5am", [1, 5, 20, 50]),
    Kind("early_bird", "Early Bird", "🐦", "When you hunt", "hunts started between 5am and 7am", [1, 5, 20]),
    Kind("weekend_warrior", "Weekend Warrior", "⚔️", "When you hunt", "hunts on a Saturday or Sunday", [1, 5, 20, 50]),
    Kind("lunch_break", "Lunch Break", "🥪", "When you hunt", "hunts started between 12pm and 2pm", [1, 5, 20]),
    # AI use
    Kind("token_tycoon", "Token Tycoon", "🪙", "AI use", "tokens used", [100_000, 1_000_000, 10_000_000, 50_000_000,
                                                                      100_000_000], fmt="tokens"),
    Kind("big_spender", "Big Spender", "💸", "AI use", "spent at API prices", [1, 5, 20, 50], fmt="usd"),
    Kind("penny_pincher", "Penny Pincher", "🐷", "AI use", "average per package (over 10+ packages)",
         [0.02, 0.01, 0.005], lower_better=True, fmt="usd3"),
    # Getting set up
    Kind("practice", "Practice Makes Perfect", "🧪", "Getting set up", "ran a practice-board hunt", [1]),
    Kind("prepared", "Prepared", "📋", "Getting set up", "every screening answer filled in", [1]),
    Kind("well_connected", "Well Connected", "🔗", "Getting set up", "logged in on every job site", [1]),
    Kind("early_adopter", "Early Adopter", "🆕", "Getting set up", "JobHunt updates installed", [1, 5, 10, 25]),
    # Hidden
    Kind("snoop", "Snoop", "🕵️", "Hidden", "found this page", [1], hint="Some pages aren't in the menu."),
    Kind("old_school", "Old School", "🎮", "Hidden", "entered the code", [1], hint="A code older than you are."),
    Kind("codebreaker", "Codebreaker", "🔐", "Hidden", "decoded a rejection", [1], hint="Every 'no' has a translation."),
    Kind("party_time", "Party Time", "🎉", "Hidden", "celebrated an offer", [1], hint="Good news deserves a party."),
    Kind("deja_vu", "Déjà Vu", "🌀", "Hidden", "opened JobHunt while it was already open", [1],
         hint="Haven't we met before?"),
]
BY_KEY = {k.key: k for k in KINDS}

# The unit for exactly 1: "1 rejection", not "1 rejections".
ONE = {
    "out_the_door": "application sent",
    "big_day": "application sent in one day",
    "same_day": "job applied to within a day of being found",
    "hands_on": "Needs-you job you finished yourself",
    "second_wind": "Needs-you job JobHunt got through on Try again",
    "globetrotter": "job site you've applied on",
    "foot_in_door": "interview",
    "options": "offer",
    "thick_skin": "rejection",
    "ghost_town": "application with no reply after 30 days",
    "comeback": "rejection before an interview",
    "the_hunt": "hunt finished",
    "speed_reader": "posting judged",
    "tailor_made": "application package built",
    "sharp_eye": "job rated 9/10 or better",
    "perfect_match": "job rated 10/10",
    "streak": "day in a row with a hunt",
    "tenure": "day since your first hunt",
    "night_owl": "hunt started between midnight and 5am",
    "early_bird": "hunt started between 5am and 7am",
    "weekend_warrior": "hunt on a Saturday or Sunday",
    "lunch_break": "hunt started between 12pm and 2pm",
    "early_adopter": "JobHunt update installed",
}

# The rejection decoder on a Rejected job's page. Affectionate, not accurate.
DECODER = [
    ("We've decided to move forward with other candidates.", "Someone's cousin applied."),
    ("Your profile is impressive, but...", "We stopped reading at the second bullet point."),
    ("We'll keep your resume on file.", "The file is the recycle bin."),
    ("The position has been filled internally.", "It was always Dave's job."),
    ("We're looking for someone with a slightly different background.", "We wanted 10 years of a 3-year-old framework."),
    ("We received an overwhelming number of applications.", "We used a keyword filter. It did not like you."),
    ("This was a very difficult decision.", "It took eleven seconds."),
    ("We encourage you to apply for future openings.", "Please don't, for at least six months."),
    ("You were a strong candidate.", "You were a candidate."),
    ("We're not hiring for this role at this time.", "The budget evaporated the day after we posted it."),
]

_lock = threading.Lock()


# ── where the numbers come from ──────────────────────────────────────

def _state_file():
    return paths.data_dir() / "achievements.json"


def load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_file()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def set_flag(name: str) -> None:
    if name not in FLAGS:
        return
    with _lock:
        state = load_state()
        state.setdefault("flags", {}).setdefault(name, datetime.now().isoformat(timespec="seconds"))
        _save_state(state)


def _dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _longest_streak(days: set[date]) -> int:
    best = 0
    for d in days:
        if d - timedelta(days=1) in days:
            continue  # not the start of a run
        n = 1
        while d + timedelta(days=n) in days:
            n += 1
        best = max(best, n)
    return best


def values(flags: dict | None = None) -> dict:
    """The number behind every kind (None = not measurable yet)."""
    flags = flags if flags is not None else load_state().get("flags", {})
    # practice-board jobs are pretend: they count for Practice Makes Perfect, not for the real thing
    jobs = {j["id"]: j for j in db.query("SELECT id, created_at, source, state, date_applied, folder, match_score, "
                                         "cost_usd FROM jobs WHERE source != 'demo'")}
    history: dict[int, list] = {}
    for h in db.query("SELECT job_id, ts, state, by FROM job_states ORDER BY ts, id"):
        history.setdefault(h["job_id"], []).append(h)

    def ever(jid, *states):
        j = jobs.get(jid)
        return (j and j["state"] in states) or any(h["state"] in states for h in history.get(jid, []))

    offered = db.OFFER_STATES  # an offer you accepted or turned down was still an offer (and an interview before it)
    applied = {jid for jid, j in jobs.items() if j["date_applied"] or ever(jid, "Applied", "Interview", *offered)}
    per_day: dict[date, int] = {}
    same_day = 0
    for jid in applied:
        found = _dt(jobs[jid]["created_at"])
        first = next((h for h in history.get(jid, []) if h["state"] == "Applied" and h["by"] != "earlier"), None)
        when = _dt(first["ts"]) if first else _dt(jobs[jid]["date_applied"])  # older jobs: only the day is known
        if not when:
            continue
        per_day[when.date()] = per_day.get(when.date(), 0) + 1
        if found and ((when - found) if first else (when.date() - found.date())) <= timedelta(days=1):
            same_day += 1

    def after_needs_you(by: str) -> int:
        n = 0
        for jid, hs in history.items():
            seen_needs = False
            for h in hs:
                if h["state"] == "Needs you":
                    seen_needs = True
                elif seen_needs and h["state"] == "Applied" and h["by"] == by:
                    n += 1
                    break
        return n

    rejected_at = sorted(h["ts"] for hs in history.values() for h in hs if h["state"] == "Rejected")
    first_interviews = [next(h["ts"] for h in hs if h["state"] in ("Interview", *offered))
                        for hs in history.values() if any(h["state"] in ("Interview", *offered) for h in hs)]
    comeback = max((sum(1 for r in rejected_at if r <= ts) for ts in first_interviews), default=0)
    month_ago = (date.today() - timedelta(days=30)).isoformat()

    hunts = db.query("SELECT started_at, status, params FROM runs WHERE kind = 'hunt'")
    starts = [d for d in (_dt(h["started_at"]) for h in hunts) if d]
    spend = db.total_spend()
    priced = [j["cost_usd"] for j in jobs.values() if j["folder"] and (j["cost_usd"] or 0) > 0]
    screening = profile.load_screening()
    logins = login_check.load_status()
    update_status = updates.updater().read_status(paths.APP_DIR)

    return {
        "out_the_door": len(applied),
        "big_day": max(per_day.values(), default=0),
        "same_day": same_day,
        "hands_on": after_needs_you("you"),
        "second_wind": after_needs_you("jobhunt"),
        "globetrotter": len({jobs[j]["source"] for j in applied}),
        "foot_in_door": sum(1 for jid in jobs if ever(jid, "Interview", *offered)),
        "options": sum(1 for jid in jobs if ever(jid, *offered)),
        "thick_skin": sum(1 for jid in jobs if ever(jid, "Rejected")),
        "ghost_town": sum(1 for j in jobs.values() if j["state"] == "Applied" and j["date_applied"]
                          and j["date_applied"] <= month_ago),
        "comeback": comeback,
        "the_hunt": sum(1 for h in hunts if h["status"] == "done"),
        "speed_reader": db.query("SELECT COUNT(*) AS n FROM seen WHERE decision != 'error'")[0]["n"],
        "tailor_made": sum(1 for j in jobs.values() if j["folder"] and j["state"] not in ("Found", "Error")),
        "sharp_eye": sum(1 for j in jobs.values() if (j["match_score"] or 0) >= 9),
        "perfect_match": sum(1 for j in jobs.values() if (j["match_score"] or 0) >= 10),
        "streak": _longest_streak({d.date() for d in starts}),
        "tenure": (date.today() - min(starts).date()).days if starts else 0,
        "night_owl": sum(1 for d in starts if d.hour < 5),
        "early_bird": sum(1 for d in starts if 5 <= d.hour < 7),
        "weekend_warrior": sum(1 for d in starts if d.weekday() >= 5),
        "lunch_break": sum(1 for d in starts if 12 <= d.hour < 14),
        "token_tycoon": int(spend["tin"] + spend["tout"]),
        "big_spender": float(spend["cost"]),
        "penny_pincher": sum(priced) / len(priced) if len(priced) >= 10 else None,
        "practice": 1 if db.practice_hunt_done() else 0,
        "prepared": 1 if all(v for k, v in screening.items() if k != "current_salary") else 0,
        "well_connected": 1 if logins and all((logins.get(s) or {}).get("state") == "in"
                                              for s in ("linkedin", "jobstreet", "indeed")) else 0,
        "early_adopter": int(update_status.get("updates") or (1 if update_status.get("updated") else 0)),
        **{f: 1 if f in flags else 0 for f in FLAGS},
    }


def tiers_earned(kind: Kind, value) -> int:
    if value is None:
        return 0
    if kind.lower_better:
        return sum(1 for t in kind.tiers if value <= t)
    return sum(1 for t in kind.tiers if value >= t)


def fmt(kind: Kind, n) -> str:
    if n is None:
        return "–"
    if kind.fmt == "tokens":
        return f"{n / 1e6:g}M" if n >= 1e6 else (f"{n / 1e3:g}k" if n >= 1e3 else f"{int(n)}")
    if kind.fmt == "usd":
        return f"${n:,.2f}".replace(".00", "")
    if kind.fmt == "usd3":
        return f"${n:.3f}"
    return f"{int(n):,}"


def tier_name(kind: Kind, i: int) -> str:
    return "Unlocked" if len(kind.tiers) == 1 else TIER_NAMES[i]


def milestone(kind: Kind, i: int) -> str:
    """What tier i stands for: '10 rejections', '$0.010 or less average per package', 'Found this page'."""
    if kind.toasts.get(i):
        return kind.toasts[i]
    if len(kind.tiers) == 1 and kind.tiers[0] == 1:
        return kind.unit[0].upper() + kind.unit[1:]
    return f"{fmt(kind, kind.tiers[i])}{' or less' if kind.lower_better else ''} {unit(kind, kind.tiers[i])}"


def unit(kind: Kind, n) -> str:
    return ONE.get(kind.key, kind.unit) if n == 1 else kind.unit


# ── the page and the pop-ups ─────────────────────────────────────────

def board() -> dict:
    """Everything the Achievements page shows."""
    state = load_state()
    vals = values(state.get("flags", {}))
    groups = {g: [] for g in GROUPS}
    earned_total = 0
    for k in KINDS:
        v = vals.get(k.key)
        n = tiers_earned(k, v)
        earned_total += n
        nxt = k.tiers[n] if n < len(k.tiers) else None
        if k.lower_better:
            progress = 1.0 if nxt is None else (0.0 if v is None else min(1.0, nxt / v))
        else:
            prev = k.tiers[n - 1] if n else 0
            progress = 1.0 if nxt is None else max(0.0, min(1.0, ((v or 0) - prev) / (nxt - prev)))
        groups[k.group].append({
            "kind": k, "value": fmt(k, v), "unit": unit(k, v), "earned": n, "tiers": [(tier_name(k, i), fmt(k, t), i < n)
                                                                  for i, t in enumerate(k.tiers)],
            "current": tier_name(k, n - 1) if n else "", "next": (tier_name(k, n), fmt(k, nxt)) if nxt is not None else None,
            "progress": round(progress * 100), "secret": k.group == "Hidden" and not n,
        })
    return {"groups": groups, "earned": earned_total, "possible": sum(len(k.tiers) for k in KINDS)}


def earned_count() -> int:
    return sum(load_state().get("earned", {}).values())


def check() -> list[dict]:
    """Tiers earned since the last check, as pop-ups [{icon, title, text}]. Saves what's earned."""
    with _lock:
        state = load_state()
        vals = values(state.get("flags", {}))
        now = {k.key: tiers_earned(k, vals.get(k.key)) for k in KINDS}
        first = "earned" not in state
        before = state.get("earned", {})
        toasts = []
        if first:
            total = sum(now.values())
            if total:
                toasts.append({"icon": "🏆", "title": "Achievements",
                               "text": f"You've already earned {total} achievement{'s' if total != 1 else ''}. Have a look!"})
        else:
            for k in KINDS:
                for i in range(int(before.get(k.key, 0)), now[k.key]):
                    toasts.append({"icon": k.icon, "title": f"{k.name} · {tier_name(k, i)}", "text": milestone(k, i)})
        if first or now != {k: int(before.get(k, 0)) for k in now}:
            state["earned"] = now
            _save_state(state)
        return toasts
