"""The application tracker (SQLite). Code writes it; the model never does.

Tables
  jobs       one row per job we built a package for (the old applications.csv)
  seen       every posting we looked at, so we never pay to judge it twice
  runs       one row per hunt / apply / profile task, with its spend
  llm_calls  every model call: stage, model, tokens, cost
  events     the live progress log shown on the Hunt page
  job_states every change of a job's state, when, and whether you or JobHunt made it
"""
import csv
import io
import re
import sqlite3
import threading
from datetime import datetime

from . import paths

# "Rejected" is the employer saying no (it counts toward achievements); "Not for me" is you turning a package down.
STATES = ["Found", "Ready", "Applying", "Applied", "Needs you", "Error", "Interview", "Offer", "Rejected", "Not for me"]
USER_STATES = ["Ready", "Applied", "Needs you", "Interview", "Offer", "Rejected", "Not for me"]  # settable from the UI
# The quick picks when you reject a package (all optional). Saved on the job and read by the next hunts.
REJECT_REASONS = ["Pay too low", "Wrong location", "Too senior", "Too junior", "Not my field", "Don't like the company",
                  "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    posting_text TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'Found',
    date_applied TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    match_score REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',       -- why it is Needs you, in words
    reason_kind TEXT NOT NULL DEFAULT '',  -- what kind of stop (see hunt.needs_you_fix)
    history TEXT NOT NULL DEFAULT '',      -- what JobHunt did with it, one line each (notes are yours)
    reject_reason TEXT NOT NULL DEFAULT '', -- why you rejected it: the quick picks, joined with '; '
    reject_note TEXT NOT NULL DEFAULT '',   -- ... and anything you typed
    salary TEXT NOT NULL DEFAULT '',        -- the pay as the posting stated it
    company_info TEXT NOT NULL DEFAULT ''   -- the company summary as JSON (company.py), '' = none yet
);
CREATE TABLE IF NOT EXISTS seen (
    url TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    params TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id INTEGER,
    job_id INTEGER,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id INTEGER,
    job_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    state TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT 'jobhunt'  -- 'jobhunt', 'you', or 'earlier' (rebuilt from a tracker made before this table)
);
CREATE TABLE IF NOT EXISTS timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    job_id INTEGER,
    kind TEXT NOT NULL,          -- 'draft' or 'apply'
    seconds REAL NOT NULL,
    ts TEXT NOT NULL
);
"""

_lock = threading.RLock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(paths.db_path(), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


# Columns added after the first release: a tracker made before them gets them on start-up.
_ADDED_COLUMNS = {"jobs": {"reason": "TEXT NOT NULL DEFAULT ''", "reason_kind": "TEXT NOT NULL DEFAULT ''",
                           "history": "TEXT NOT NULL DEFAULT ''", "reject_reason": "TEXT NOT NULL DEFAULT ''",
                           "reject_note": "TEXT NOT NULL DEFAULT ''", "salary": "TEXT NOT NULL DEFAULT ''",
                           "company_info": "TEXT NOT NULL DEFAULT ''"}}


def init() -> None:
    with _lock, connect() as con:
        con.executescript(SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        if not con.execute("SELECT 1 FROM job_states LIMIT 1").fetchone():
            # A tracker from before job_states: start each job's history from what is known about it.
            for j in con.execute("SELECT id, created_at, updated_at, state, date_applied FROM jobs").fetchall():
                if j["date_applied"]:
                    con.execute("INSERT INTO job_states (job_id, ts, state, by) VALUES (?,?,?,'earlier')",
                                (j["id"], j["date_applied"] + "T12:00:00", "Applied"))
                if j["state"] != "Applied" or not j["date_applied"]:
                    con.execute("INSERT INTO job_states (job_id, ts, state, by) VALUES (?,?,?,'earlier')",
                                (j["id"], max(j["updated_at"], j["date_applied"] + "T12:00:01") if j["date_applied"]
                                 else j["updated_at"], j["state"]))


def _exec(sql: str, args=()) -> sqlite3.Cursor:
    with _lock:
        con = connect()
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur
        finally:
            con.close()


def _query(sql: str, args=()) -> list[dict]:
    with _lock:
        con = connect()
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()


# ── jobs ──────────────────────────────────────────────────────────────

def add_job(*, source, url, company, role, location="", apply_url="", posting_text="",
            folder="", notes="", history="", match_score=None, run_id=None, state="Found", salary="") -> int:
    ts = now()
    cur = _exec(
        "INSERT INTO jobs (created_at, updated_at, source, url, company, role, location, apply_url,"
        " posting_text, state, folder, notes, history, match_score, run_id, salary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, ts, source, url, company, role, location, apply_url, posting_text, state, folder, notes,
         _stamp(history) if history else "", match_score, run_id, salary or ""),
    )
    _exec("INSERT INTO job_states (job_id, ts, state, by) VALUES (?,?,?,'jobhunt')", (cur.lastrowid, ts, state))
    return cur.lastrowid


_JOB_FIELDS = {"company", "role", "location", "apply_url", "posting_text", "state", "date_applied",
               "folder", "notes", "match_score", "reason", "reason_kind", "history", "reject_reason", "reject_note",
               "company_info"}


def update_job(job_id: int, by: str = "jobhunt", **fields) -> None:
    """Change a job. `by` says who changed its state: "jobhunt", or "you" from the job pages."""
    bad = set(fields) - _JOB_FIELDS
    if bad:
        raise ValueError(f"unknown job fields: {bad}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"unknown state {fields['state']!r}")
    if fields.get("state") == "Applied" and "date_applied" not in fields:
        fields["date_applied"] = datetime.now().date().isoformat()
    if "state" in fields and fields["state"] != "Needs you":  # the reason only belongs to Needs you
        fields.setdefault("reason", "")
        fields.setdefault("reason_kind", "")
    if "state" in fields and fields["state"] != "Not for me":  # changed your mind: the reject reason goes too
        fields.setdefault("reject_reason", "")
        fields.setdefault("reject_note", "")
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        old = _query("SELECT state FROM jobs WHERE id = ?", (job_id,)) if "state" in fields else []
        _exec(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        if old and old[0]["state"] != fields["state"]:
            _exec("INSERT INTO job_states (job_id, ts, state, by) VALUES (?,?,?,?)",
                  (job_id, fields["updated_at"], fields["state"], by))


def set_company_info(job_id: int, info_json: str) -> None:
    """Save a job's company summary. Leaves updated_at alone: the per-site apply cap counts Applied jobs by
    it, so refreshing a summary must not make an old application look like one made today."""
    with _lock:
        _exec("UPDATE jobs SET company_info = ? WHERE id = ?", (info_json, job_id))


def append_note(job_id: int, note: str) -> None:
    """Add a line to your own notes on a job."""
    job = get_job(job_id)
    if not job:
        return
    notes = (job["notes"] + "\n" if job["notes"] else "") + note
    update_job(job_id, notes=notes)


def _stamp(line: str) -> str:
    return f"{now()[:16].replace('T', ' ')}  {line}"


def add_history(job_id: int, line: str) -> None:
    """Record something JobHunt did with a job (shown read-only on the job page, kept out of your notes)."""
    job = get_job(job_id)
    if job:
        update_job(job_id, history=(job["history"] + "\n" if job["history"] else "") + _stamp(line))


def needs_you(job_id: int, kind: str, reason: str) -> None:
    """Hand a job to you, saying why (shown on the job page with what to do about it)."""
    update_job(job_id, state="Needs you", reason=reason, reason_kind=kind)
    add_history(job_id, "Needs you: " + reason)


def reject(job_id: int, reasons: list[str], note: str = "") -> None:
    """You turned a package down ('Not for me'), saying why if you like."""
    reasons = [r for r in REJECT_REASONS if r in reasons]  # only the quick picks, in their usual order
    note = " ".join((note or "").split())[:300]
    update_job(job_id, state="Not for me", by="you", reject_reason="; ".join(reasons), reject_note=note)
    add_history(job_id, "You rejected it" + (": " + reject_text(get_job(job_id) or {}) if reasons or note
                                             else " (no reason given)."))


def reject_text(job: dict) -> str:
    """'Too senior; Wrong location. "Too far from home"' ('' when no reason was given)."""
    reason, note = job.get("reject_reason") or "", job.get("reject_note") or ""
    return (reason + ("." if reason else "") + (f' "{note}"' if note else "")).strip()


def recent_rejections(limit: int = 10) -> list[dict]:
    """Your latest rejections that came with a reason, newest first."""
    return _query("SELECT company, role, reject_reason, reject_note FROM jobs WHERE state = 'Not for me'"
                  " AND (reject_reason != '' OR reject_note != '') ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))


def why_needs_you(job: dict) -> str:
    """The reason, or for a job handed over before reasons were kept, its last note."""
    if job.get("reason"):
        return job["reason"]
    for text in (job.get("history") or "", job.get("notes") or ""):
        lines = [l for l in text.strip().splitlines() if l.strip()]
        if lines:
            return lines[-1]
    return ""


def get_job(job_id: int) -> dict | None:
    rows = _query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return rows[0] if rows else None


def list_jobs(state: str | None = None) -> list[dict]:
    if state:
        return _query("SELECT * FROM jobs WHERE state = ? ORDER BY id DESC", (state,))
    return _query("SELECT * FROM jobs ORDER BY id DESC")


def state_counts() -> dict:
    rows = _query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
    return {r["state"]: r["n"] for r in rows}


_LEGAL_WORDS = {"pte", "ltd", "limited", "private", "inc", "llc", "llp", "plc", "co", "corp", "corporation",
                "company", "singapore", "sg", "the"}


def company_key(name: str) -> str:
    """A company name the same way every board writes it: 'TECHCOM SOLUTIONS (SINGAPORE) PTE. LTD.' and
    'TechCom Solutions Pte Ltd' are both 'techcom solutions'."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [w for w in words if w not in _LEGAL_WORDS]
    return " ".join(kept or words)


def is_duplicate(url: str, company: str, role: str) -> bool:
    """Same URL, or the same company + role already tracked (even from another job site)."""
    if _query("SELECT 1 FROM jobs WHERE url = ?", (url,)):
        return True
    key = company_key(company)
    if not key:
        return False
    rows = _query("SELECT company FROM jobs WHERE lower(trim(role)) = lower(trim(?)) AND company != ''", (role,))
    return any(company_key(r["company"]) == key for r in rows)


def applied_since(source: str, since_iso: str) -> int:
    rows = _query("SELECT COUNT(*) AS n FROM jobs WHERE source = ? AND state = 'Applied' AND updated_at >= ?",
                  (source, since_iso))
    return rows[0]["n"]


def seen_stats() -> dict:
    rows = _query("SELECT decision, COUNT(*) AS n FROM seen GROUP BY decision")
    return {r["decision"]: r["n"] for r in rows}


def add_job_spend(job_id: int, tokens_in: int, tokens_out: int, cost: float) -> None:
    _exec("UPDATE jobs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, cost_usd = cost_usd + ? WHERE id = ?",
          (tokens_in, tokens_out, cost, job_id))


EXPORT_COLUMNS = ["id", "created_at", "source", "company", "role", "location", "url", "apply_url", "state",
                  "date_applied", "folder", "notes", "reason", "reject_reason", "reject_note", "history", "match_score",
                  "cost_usd", "tokens_in", "tokens_out"]


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for j in reversed(list_jobs()):
        w.writerow([j[c] if j[c] is not None else "" for c in EXPORT_COLUMNS])
    return buf.getvalue()


# ── seen postings ─────────────────────────────────────────────────────

def was_seen(url: str) -> bool:
    """Already judged (match or skip). A posting that failed to load or be judged is tried again next run."""
    return bool(_query("SELECT 1 FROM seen WHERE url = ? AND decision != 'error'", (url,)))


def mark_seen(url: str, source: str, company: str, role: str, decision: str, reason: str = "") -> None:
    _exec("INSERT OR REPLACE INTO seen (url, source, company, role, decision, reason, seen_at) VALUES (?,?,?,?,?,?,?)",
          (url, source, company, role, decision, reason[:500], now()))


def forget_seen() -> None:
    _exec("DELETE FROM seen")


# ── runs, spend and events ────────────────────────────────────────────

def start_run(kind: str, params: str = "") -> int:
    return _exec("INSERT INTO runs (kind, started_at, params) VALUES (?,?,?)", (kind, now(), params)).lastrowid


def finish_run(run_id: int, status: str, summary: str = "") -> None:
    _exec("UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?", (now(), status, summary, run_id))


def recover_interrupted() -> None:
    """At start-up: tidy up after JobHunt was closed mid-task (window closed, PC restarted).

    Nothing can be running yet, so a run still marked running was cut off. A job left Applying may
    already have been submitted, so it goes to you (Needs you), never back to Ready; a job left
    Found never got its package."""
    _exec("UPDATE runs SET status = 'interrupted', finished_at = ?, summary = 'JobHunt was closed while this ran.' "
          "WHERE status = 'running'", (now(),))
    for job in _query("SELECT id, state FROM jobs WHERE state IN ('Applying', 'Found')"):
        applying = job["state"] == "Applying"
        if applying:
            needs_you(job["id"], "interrupted", "JobHunt was closed while applying: check on the site whether it "
                                                "was submitted.")
        else:
            update_job(job["id"], state="Error")
            add_history(job["id"], "JobHunt was closed while building this package.")


def query(sql: str, args=()) -> list[dict]:
    """A read-only query for code that only reports (achievements)."""
    return _query(sql, args)


def last_run(kind: str) -> dict | None:
    """The latest finished run of this kind (a running one is shown live instead)."""
    rows = _query("SELECT * FROM runs WHERE kind = ? AND status != 'running' ORDER BY id DESC LIMIT 1", (kind,))
    return rows[0] if rows else None


def count_jobs(run_id: int, state: str = "") -> int:
    """Jobs from this run in this state; with no state, the packages it built (any state but Found / Error)."""
    if state:
        return _query("SELECT COUNT(*) AS n FROM jobs WHERE run_id = ? AND state = ?", (run_id, state))[0]["n"]
    return _query("SELECT COUNT(*) AS n FROM jobs WHERE run_id = ? AND state NOT IN ('Found', 'Error')", (run_id,))[0]["n"]


def practice_hunt_done() -> bool:
    """Has a hunt on the practice board finished?"""
    return bool(_query("SELECT 1 FROM runs WHERE kind = 'hunt' AND status = 'done' AND params LIKE '%\"demo\"%' LIMIT 1"))


def get_run(run_id: int) -> dict | None:
    rows = _query("SELECT * FROM runs WHERE id = ?", (run_id,))
    return rows[0] if rows else None


def list_runs(limit: int = 20) -> list[dict]:
    return _query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))


def record_llm_call(*, run_id, job_id, stage, model, tokens_in, tokens_out, cost, seconds) -> int:
    cur = _exec("INSERT INTO llm_calls (ts, run_id, job_id, stage, model, tokens_in, tokens_out, cost_usd, seconds)"
                " VALUES (?,?,?,?,?,?,?,?,?)", (now(), run_id, job_id, stage, model, tokens_in, tokens_out, cost, seconds))
    if run_id:
        _exec("UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, cost_usd = cost_usd + ?"
              " WHERE id = ?", (tokens_in, tokens_out, cost, run_id))
    if job_id:
        add_job_spend(job_id, tokens_in, tokens_out, cost)
    return cur.lastrowid


def assign_call_to_job(call_id: int, job_id: int) -> None:
    """A call made before the job existed (the filter call) now belongs to that job."""
    rows = _query("SELECT tokens_in, tokens_out, cost_usd FROM llm_calls WHERE id = ? AND job_id IS NULL", (call_id,))
    if rows:
        _exec("UPDATE llm_calls SET job_id = ? WHERE id = ?", (job_id, call_id))
        add_job_spend(job_id, rows[0]["tokens_in"], rows[0]["tokens_out"], rows[0]["cost_usd"])


def backfill_costs(cost_fn) -> int:
    """Price calls recorded at $0 (older flat-plan runs) with cost_fn(model, tokens_in, tokens_out),
    then re-total the jobs and runs that showed $0. Returns how many calls were priced."""
    rows = _query("SELECT id, model, tokens_in, tokens_out FROM llm_calls WHERE cost_usd = 0 AND tokens_in + tokens_out > 0")
    for r in rows:
        _exec("UPDATE llm_calls SET cost_usd = ? WHERE id = ?", (cost_fn(r["model"], r["tokens_in"], r["tokens_out"]), r["id"]))
    if rows:
        for table, col in (("jobs", "job_id"), ("runs", "run_id")):
            _exec(f"UPDATE {table} SET cost_usd = (SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls"
                  f" WHERE {col} = {table}.id) WHERE cost_usd = 0")
    return len(rows)


def job_calls(job_id: int) -> list[dict]:
    return _query("SELECT * FROM llm_calls WHERE job_id = ? ORDER BY id", (job_id,))


def total_spend() -> dict:
    rows = _query("SELECT COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(tokens_in),0) AS tin,"
                  " COALESCE(SUM(tokens_out),0) AS tout FROM llm_calls")
    return rows[0]


def stage_averages() -> dict:
    """Average tokens per call by stage, from real history (feeds the estimate)."""
    rows = _query("SELECT stage, AVG(tokens_in) AS tin, AVG(tokens_out) AS tout, COUNT(*) AS n FROM llm_calls GROUP BY stage")
    return {r["stage"]: r for r in rows}


def log_event(run_id, message: str, level: str = "info", job_id=None) -> int:
    return _exec("INSERT INTO events (ts, run_id, job_id, level, message) VALUES (?,?,?,?,?)",
                 (now(), run_id, job_id, level, message)).lastrowid


def events_since(after_id: int = 0, limit: int = 300) -> list[dict]:
    return _query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))


def recent_events(limit: int = 200) -> list[dict]:
    return list(reversed(_query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))))


def record_timing(run_id, job_id, kind: str, seconds: float) -> None:
    _exec("INSERT INTO timings (run_id, job_id, kind, seconds, ts) VALUES (?,?,?,?,?)",
          (run_id, job_id, kind, seconds, now()))


def average_timing(kind: str) -> float | None:
    rows = _query("SELECT AVG(seconds) AS s, COUNT(*) AS n FROM timings WHERE kind = ?", (kind,))
    return rows[0]["s"] if rows and rows[0]["n"] else None
