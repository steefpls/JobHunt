"""Regression tests for what the first real hunt (2026-10-13) turned up.

- the expected salary on the Profile page never reached the filter (the example
  scope's SGD 8,000 floor did instead), and the example scope was accepted as-is
- an incomplete package from the AI was marked Ready
- one unreadable filter reply, one broken site or one broken PDF ended the whole hunt
- a blocked site kept being searched; HTTP error pages were judged as postings
- a flat plan showed "included" instead of a dollar figure
"""
import json

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, hunt, llm, profile, render, settings, sites
from jobhunt.extract import Listing, generic_posting, parse_salary_floor

from conftest import APP_DIR, TAILOR_REPLY

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


def _prepare(demo_server):
    settings.update({
        "demo_board_url": demo_server,
        "scope": {"keywords": ["engineer"], "location": "Singapore", "avoid_words": ["intern"]},
        "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0},
        "apply": {"show_browser": False},
    })


def _hunt(jobs: int) -> dict:
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": jobs, "sites": ["demo"]})
    runner.wait(180)
    return db.get_run(run_id)


def _calls(fake, marker: str) -> list:
    return [r for r in fake.requests if marker in r["messages"][0]["content"]]


def _seen(url: str) -> dict:
    return db._query("SELECT * FROM seen WHERE url = ?", (url,))[0]


# ── salary floor ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text, monthly", [
    ("15,000 per month", 15000), ("SGD 15k", 15000), ("180k a year", 15000),
    ("S$180,000 per annum", 15000), ("", 0), ("negotiable", 0),
])
def test_expected_salary_is_read_as_monthly(text, monthly):
    assert parse_salary_floor(text) == pytest.approx(monthly)


def test_jsonld_salary_is_read_and_made_monthly():
    ld = {"@type": "JobPosting", "title": "T", "description": "<p>" + "x" * 100 + "</p>",
          "hiringOrganization": {"name": "C"},
          "baseSalary": {"currency": "SGD", "value": {"minValue": 120000, "maxValue": 180000, "unitText": "YEAR"}}}
    p = generic_posting(f'<script type="application/ld+json">{json.dumps(ld)}</script>', "x", "u")
    assert p.salary_max_monthly == pytest.approx(15000) and "180,000" in p.salary
    ld["baseSalary"]["currency"] = "USD"  # a foreign currency is left to the model
    p = generic_posting(f'<script type="application/ld+json">{json.dumps(ld)}</script>', "x", "u")
    assert p.salary_max_monthly == 0


def test_postings_below_the_floor_are_skipped_without_a_model_call(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    profile.save_screening({**profile.load_screening(), "expected_salary": "10,000 per month"})
    run = _hunt(3)
    assert run["status"] == "done", run["summary"]
    # Lumen tops out at 9,500 and Kopi Labs at 8,500: code skips them; Harbourline (11,000) goes on
    assert "below your SGD 10,000" in _seen(f"{demo_server}/job/1")["reason"]
    assert _seen(f"{demo_server}/job/5")["decision"] == "skip"
    assert [j["company"] for j in db.list_jobs()] == ["Harbourline Payments"]
    filters = _calls(fake_llm, "screen job postings")
    assert len(filters) == 1
    assert "10,000 per month" in filters[0]["messages"][1]["content"]  # the floor reaches the filter too


# ── the example scope ─────────────────────────────────────────────────

def test_hunt_refuses_the_example_scope(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    profile.save_scope_text((APP_DIR / "scope.example.md").read_text(encoding="utf-8"))
    run = _hunt(1)
    assert run["status"] == "error" and "example" in run["summary"]
    assert fake_llm.requests == []


def test_saving_the_example_scope_warns_and_leaves_the_checklist_open(data_dir):
    from jobhunt.web.app import create_app
    example = (APP_DIR / "scope.example.md").read_text(encoding="utf-8")
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        r = c.post("/scope", data={"keywords": "engineer", "scope_text": example}, headers=SAME_ORIGIN,
                   follow_redirects=False)
        assert "err=" in r.headers["location"]
        assert "✓</span>Set your search scope" not in c.get("/").text
        mine = example.replace(profile.SCOPE_EXAMPLE_MARKER, "")
        r = c.post("/scope", data={"keywords": "engineer", "scope_text": mine}, headers=SAME_ORIGIN,
                   follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert "✓</span>Set your search scope" in c.get("/").text


# ── incomplete packages ───────────────────────────────────────────────

def test_package_problems():
    assert render.package_problems(render.normalise_tailor(dict(TAILOR_REPLY))) == []
    empty = render.normalise_tailor({"resume": {}, "cover_letter": {"paragraphs": ["", " "]}})
    assert len(render.package_problems(empty)) == 3


def test_incomplete_package_is_retried_then_marked_error(demo_server, secrets, candidate, fake_llm, data_dir):
    _prepare(demo_server)
    fake_llm.tailor_reply = {"resume": TAILOR_REPLY["resume"], "cover_letter": {"greeting": "Dear Hiring Team,"},
                             "match_report": {}}
    run = _hunt(1)
    assert run["status"] == "done", run["summary"]
    job = db.list_jobs()[0]
    assert job["state"] == "Error" and "incomplete" in job["notes"]
    assert len(_calls(fake_llm, "tailor a Harvard")) == 2  # one retry, then give up
    folder = data_dir / "jobs" / job["folder"]
    assert not (folder / "cover_letter.pdf").exists() and not (folder / "resume.pdf").exists()


# ── one failure costs one posting, not the run ────────────────────────

def test_unreadable_filter_reply_skips_only_that_posting(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    fake_llm.filter_override = lambda user: "Sorry, I can't help with that." if "Lumen Analytics" in user else None
    run = _hunt(1)
    assert run["status"] == "done", run["summary"]
    assert _seen(f"{demo_server}/job/1")["decision"] == "error"
    assert [j["company"] for j in db.list_jobs()] == ["Harbourline Payments"]


def test_rate_limit_still_stops_the_run(demo_server, secrets, candidate, fake_llm, monkeypatch):
    _prepare(demo_server)

    def judge(*a, **k):
        raise llm.RateLimited("slow down")
    monkeypatch.setattr(hunt, "judge", judge)
    run = _hunt(1)
    assert run["status"] == "error" and "slow down" in run["summary"]


def test_a_broken_pdf_marks_only_that_job(demo_server, secrets, candidate, fake_llm, monkeypatch):
    _prepare(demo_server)
    real, calls = render.cover_letter_html, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(render, "cover_letter_html", flaky)
    run = _hunt(2)
    assert run["status"] == "done", run["summary"]
    assert {j["company"]: j["state"] for j in db.list_jobs()} == {"Lumen Analytics": "Error",
                                                                  "Harbourline Payments": "Ready"}


class _Task:
    settings: dict = {}

    def __init__(self):
        self.lines = []

    def log(self, message, level="info"):
        self.lines.append(message)

    def check_stop(self):
        pass


class _Site:
    def __init__(self, key, fail=None):
        self.key = self.label = key
        self.fail, self.searches = fail, 0

    def search(self, ctx, keyword, location, page_no):
        self.searches += 1
        if self.fail:
            raise self.fail
        return [Listing(self.key, f"https://{self.key}.example/{page_no}/{i}", "t") for i in range(3)]


def test_a_blocked_site_stops_being_searched(monkeypatch):
    a, b = _Site("a"), _Site("b")
    monkeypatch.setattr(hunt, "get_site", lambda key, s: {"a": a, "b": b}[key])
    blocked = set()
    gen = hunt.candidates(_Task(), None, ["a", "b"], ["kw"], "", max_pages=5, blocked=blocked)
    assert next(gen)[0] is a
    blocked.add("a")  # opening a posting on site a hit a sign-in wall
    rest = list(gen)
    assert len(rest) == 15 and all(site is b for site, _ in rest)
    assert a.searches == 1  # no more search pages loaded on a


def test_one_broken_site_does_not_end_the_hunt(monkeypatch):
    a, b = _Site("a", fail=RuntimeError("HTTP 500")), _Site("b")
    monkeypatch.setattr(hunt, "get_site", lambda key, s: {"a": a, "b": b}[key])
    task, blocked = _Task(), set()
    got = list(hunt.candidates(task, None, ["a", "b"], ["kw"], "", max_pages=1, blocked=blocked))
    assert len(got) == 3 and all(site is b for site, _ in got)
    assert "a" in blocked and any("search failed" in line for line in task.lines)


# ── HTTP error pages ──────────────────────────────────────────────────

@pytest.mark.parametrize("status, error", [(400, sites.PageUnavailable), (404, sites.PageUnavailable),
                                           (503, sites.PageUnavailable), (403, sites.SiteBlocked),
                                           (429, sites.SiteBlocked)])
def test_http_error_pages_are_not_postings(status, error):
    class Browser:
        last_url = "https://sg.indeed.com/viewjob?jk=1"
        last_status = status

        def load_html(self, url):
            return "<title>Error 400 Bad Request</title><h1>Error</h1><p>" + "x" * 200 + "</p>"

    ctx = sites.SiteContext(Browser(), {"min_delay_s": 0, "max_delay_s": 0})
    with pytest.raises(error):
        ctx.get_html(Browser.last_url)


def test_browser_records_the_http_status(demo_server, data_dir):
    from jobhunt.browser import BrowserSession
    b = BrowserSession(headless=True)
    try:
        b.load_html(f"{demo_server}/job/1")
        assert b.last_status == 200
        b.load_html(f"{demo_server}/no-such-page")
        assert b.last_status == 404
    finally:
        b.close()


# ── dollars on a flat plan ────────────────────────────────────────────

def test_price_lookup_matches_vendor_prefixed_names():
    assert llm.price_for("deepseek-v4.1-flash") == llm.KNOWN_PRICES["deepseek/deepseek-v4.1-flash"]
    assert llm.price_for("no-such-model") == llm.FALLBACK_PRICE


def test_old_zero_cost_calls_are_backfilled(data_dir):
    run_id = db.start_run("hunt")
    job_id = db.add_job(source="demo", url="u", company="C", role="R")
    db.record_llm_call(run_id=run_id, job_id=job_id, stage="tailor", model="deepseek-v4.1-flash",
                       tokens_in=1_000_000, tokens_out=1_000_000, cost=0.0, seconds=1)
    price = lambda m, i, o: llm.cost_of(m, i, o)  # noqa: E731
    assert db.backfill_costs(price) == 1
    assert db.get_job(job_id)["cost_usd"] == pytest.approx(0.0441 + 0.63)
    assert db.get_run(run_id)["cost_usd"] == pytest.approx(0.0441 + 0.63)
    assert db.backfill_costs(price) == 0  # only once


def test_flat_plan_pages_show_dollars(data_dir):
    from jobhunt.web.app import create_app
    settings.update({"provider": "opencode_go"})
    run_id = db.start_run("hunt")
    job_id = db.add_job(source="demo", url="u", company="C", role="R", state="Ready")
    db.record_llm_call(run_id=run_id, job_id=job_id, stage="tailor", model="deepseek-v4.1-flash",
                       tokens_in=2000, tokens_out=1000, cost=0.0, seconds=1)
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:  # starting the app backfills
        dashboard, job, hunt_page = c.get("/").text, c.get(f"/jobs/{job_id}").text, c.get("/hunt").text
    assert "incl." not in dashboard and "$0.0007" in dashboard
    assert "included in your subscription" not in job and "at API prices" in job
    assert "cost at API prices" in hunt_page
