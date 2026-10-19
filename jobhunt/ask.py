"""Ask: a question about your own background, or a recruiter's whole message, answered from your
resume, Profile and saved screening answers. One AI call, made while you wait (no background task,
so it works during a hunt too). The last few answers are kept in data/asks.json for the Ask page.
"""
import json
import time
from datetime import date

from . import db, hunt, paths, profile, prompts, secrets_store, settings as settings_mod
from .llm import LLMError, Meter

KEEP = 20  # answers kept on the Ask page
MAX_QUESTION = 6000


def asks_path():
    return paths.data_dir() / "asks.json"


def history() -> list[dict]:
    """Newest first."""
    try:
        items = json.loads(asks_path().read_text(encoding="utf-8"))
        return [a for a in items if isinstance(a, dict) and a.get("id")]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def get(ask_id: str) -> dict | None:
    return next((a for a in history() if a["id"] == ask_id), None)


def forget(ask_id: str) -> None:
    asks_path().write_text(json.dumps([a for a in history() if a["id"] != ask_id], indent=2), encoding="utf-8")


def _clean(data: dict) -> dict:
    """Only the fields the page shows, as plain strings: whatever else the model sends is dropped."""
    fields = set(profile.SCREENING_FIELDS)
    answers = []
    for a in data.get("answers") or []:
        if not isinstance(a, dict) or not str(a.get("answer") or "").strip():
            continue
        sure = str(a.get("sure") or "").lower()
        field = str(a.get("profile_field") or "").strip()
        answers.append({"question": str(a.get("question") or "").strip()[:300],
                        "answer": str(a["answer"]).strip()[:2000],
                        "basis": str(a.get("basis") or "").strip()[:1000],
                        "sure": sure if sure in ("yes", "partly", "no") else "partly",
                        "field": field if field in fields else "",
                        "short": str(a.get("short") or "").strip()[:200]})
    return {"answers": answers, "reply": str(data.get("reply") or "").strip()[:6000],
            "missing": [str(m).strip()[:300] for m in data.get("missing") or [] if str(m).strip()][:10]}


def ask(question: str, job_id: int | None = None) -> dict:
    """Answer it and keep the answer. Raises LLMError (or BudgetExceeded) with words for the page."""
    question = (question or "").replace("\r\n", "\n").strip()[:MAX_QUESTION]
    if not question:
        raise LLMError("Type a question, or paste the recruiter's message.")
    profile_text, cvs = profile.load_profile(), profile.all_cv_texts()
    if not profile_text.strip() and not cvs:
        raise LLMError("Upload your CV on the Profile page first: answers come from it.")
    s = settings_mod.load()
    if not secrets_store.has_secret(secrets_store.api_key_name(s["provider"])):
        raise LLMError("Add your AI key on the Connections page first.")
    job = db.get_job(job_id) if job_id else None
    run_id = db.start_run("ask", json.dumps({"job_id": job["id"]} if job else {}))

    def record(stage, _job, resp):
        db.record_llm_call(run_id=run_id, job_id=None, stage=stage, model=resp.model, tokens_in=resp.tokens_in,
                           tokens_out=resp.tokens_out, cost=resp.cost, seconds=resp.seconds)

    status, summary = "done", "Answered."
    try:
        client = hunt.make_llm(s, Meter.from_settings(s), record, f"ask{run_id}")
        system, user = prompts.ask_prompt(question, profile_text, cvs, profile.load_screening(),
                                          date.today().strftime("%d %B %Y"), job)
        data, _ = client.chat_json(stage="profile", system=system, user=user, max_tokens=3000)
        result = _clean(data if isinstance(data, dict) else {})
        if not result["answers"] and not result["reply"]:
            raise LLMError("The AI didn't send an answer back. Try again, or ask it more simply.")
    except Exception as e:
        status, summary = "error", str(e)[:300]
        raise
    finally:
        db.finish_run(run_id, status, summary)
    entry = {"id": f"{time.time_ns():x}", "at": time.strftime("%Y-%m-%d %H:%M"), "question": question,
             "job_id": job["id"] if job else None,
             "job": f"{job['role']} at {job['company']}" if job else "", **result}
    asks_path().write_text(json.dumps([entry] + history()[:KEEP - 1], indent=2), encoding="utf-8")
    return entry
