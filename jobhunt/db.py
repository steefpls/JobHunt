"""The application tracker (SQLite). Code writes it; the model never does.

Tables
  jobs       one row per job we built a package for (the old applications.csv)
  seen       every posting we looked at, so we never pay to judge it twice
  runs       one row per hunt / apply / profile task, with its spend
  llm_calls  every model call: stage, model, tokens, cost
  events     the live progress log shown on the Hunt page
"""
import csv
import io
import sqlite3
import threading
from datetime import datetime

from . import paths

STATES = ["Found", "Ready", "Applying", "Applied", "Needs you", "Error", "Interview", "Offer", "Rejected"]
USER_STATES = ["Ready", "Applied", "Needs you", "Interview", "Offer", "Rejected"]  # settable from the UI

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
    run_id INTEGER
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


def init() -> None:
    with _lock, connect() as con:
        con.executescript(SCHEMA)


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
            folder="", notes="", match_score=None, run_id=None, state="Found") -> int:
    ts = now()
    cur = _exec(
        "INSERT INTO jobs (created_at, updated_at, source, url, company, role, location, apply_url,"
        " posting_text, state, folder, notes, match_score, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, ts, source, url, company, role, location, apply_url, posting_text, state, folder, notes,
         match_score, run_id),
    )
    return cur.lastrowid


_JOB_FIELDS = {"company", "role", "location", "apply_url", "posting_text", "state", "date_applied",
               "folder", "notes", "match_score"}


def update_job(job_id: int, **fields) -> None:
    bad = set(fields) - _JOB_FIELDS
    if bad:
        raise ValueError(f"unknown job fields: {bad}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"unknown state {fields['state']!r}")
    if fields.get("state") == "Applied" and "date_applied" not in fields:
        fields["date_applied"] = datetime.now().date().isoformat()
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def append_note(job_id: int, note: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    notes = (job["notes"] + "\n" if job["notes"] else "") + note
    update_job(job_id, notes=notes)


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


def is_duplicate(url: str, company: str, role: str) -> bool:
    """Same URL, or same company + role, already tracked."""
    rows = _query(
        "SELECT 1 FROM jobs WHERE url = ? OR (lower(company) = lower(?) AND lower(role) = lower(?) AND company != '')",
        (url, company, role),
    )
    return bool(rows)


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
                  "date_applied", "folder", "notes", "match_score", "cost_usd", "tokens_in", "tokens_out"]


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for j in reversed(list_jobs()):
        w.writerow([j[c] if j[c] is not None else "" for c in EXPORT_COLUMNS])
    return buf.getvalue()


# ── seen postings ─────────────────────────────────────────────────────

def was_seen(url: str) -> bool:
    return bool(_query("SELECT 1 FROM seen WHERE url = ?", (url,)))


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
