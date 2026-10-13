"""Regression tests for what the live run of 2026-10-14 turned up (JobStreet, MyCareersFuture and
LinkedIn read for real, the AI on OpenCode Go, the apply agent on the practice site).

- JobStreet salaries were never read, so the salary floor never applied there
- one HTTP 429 from LinkedIn dropped the site for the whole run
- a tailor reply that wasn't valid JSON made the job an Error without the retry incomplete ones get
- the cover letter could be signed twice ("Sincerely, Alex" in the AI's text and the template's own)
- the apply agent looped on a correctly filled Email box: the saved login's username was the same
  address, so it was shown to the AI as [secret:...]
- postings that failed once were never looked at again; the same job on two boards was built twice
- "/profile#screening?msg=..." put the message in the #fragment, so it never showed
"""
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunt import db, hunt, llm, render, secrets_store, settings, sites
from jobhunt.extract import Listing, html_to_text, parse_salary_floor, salary_top_monthly

from conftest import FIXTURES, TAILOR_REPLY

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── salary text as the boards show it ─────────────────────────────────

@pytest.mark.parametrize("text, top", [
    ("$5,500 – $8,000 per month", 8000), ("$5500 - $8k p.m.", 8000), ("$84,000 – $120,000 per year", 10000),
    ("$5-6k p.m.", 6000), ("SGD 10,000-15,000 Monthly", 15000), ("$25 – $30 per hour", 0), ("$300 per day", 0),
    ("RM 5,000 - 8,000", 0), ("US$ 9,000", 0), ("Competitive", 0), ("", 0),
])
def test_board_salary_text_to_monthly_top(text, top):
    assert salary_top_monthly(text) == pytest.approx(top)


@pytest.mark.parametrize("text, floor", [
    ("SGD 6,000 per month, min 3 years", 6000), ("5-6k", 5000), ("8k/mth", 8000), ("Annual: 120,000", 10000),
])
def test_expected_salary_period_is_read_next_to_the_number(text, floor):
    assert parse_salary_floor(text) == pytest.approx(floor)


# ── what each site gives us ───────────────────────────────────────────

def test_jobstreet_posting_salary_location_and_work_type():
    listing = Listing("jobstreet", "https://sg.jobstreet.com/job/95175476", "t", "c", "l")
    p = sites.JobStreetSite().parse_posting(read("jobstreet_posting_salary.html"), listing)
    assert p.title == "Python Django Developer (Preferred with banking experience)"
    assert p.company == "A-IT Software Services Pte Ltd"
    assert p.location == "Changi, East Region, Singapore"  # the page header plus the JSON-LD country
    assert p.salary == "$5,500 – $8,000 per month" and p.salary_max_monthly == 8000
    assert p.text.endswith("Work type: Contract/Temp")
    assert len(p.text) > 2000


def test_jobstreet_search_card_salary():
    listings = sites.JobStreetSite().parse_search(read("jobstreet_search.html"))
    paid = [l for l in listings if l.salary]
    assert paid and paid[0].salary == "$4,500 – $6,000 per month" and paid[0].salary_max_monthly == 6000


def test_listing_salary_carries_over_when_the_page_has_none():
    listing = Listing("jobstreet", "https://sg.jobstreet.com/job/95236588", "t", "c", "l",
                      salary="$4,500 – $6,000 per month", salary_max_monthly=6000)
    p = sites.JobStreetSite().parse_posting(read("jobstreet_posting.html"), listing)
    assert p.salary_max_monthly == 6000


def test_mycareersfuture_search_result_has_salary():
    search = json.loads(read("mcf_search.json"))
    search["results"][0]["salary"] = {"minimum": 3500, "maximum": 4500, "type": {"salaryType": "Monthly"}}
    listing = sites.MyCareersFutureSite().listing_from_result(search["results"][0])
    assert listing.salary == "SGD 3,500-4,500 Monthly" and listing.salary_max_monthly == 4500


def test_linkedin_live_posting_adds_job_criteria():
    listing = Listing("linkedin", "https://www.linkedin.com/jobs/view/4457908204/", "fallback", "fallback co", "SG",
                      extra={"job_id": "4457908204"})
    p = sites.LinkedInSite().parse_posting(read("linkedin_posting_live.html"), listing)
    assert p.title == "Python Developer" and p.company == "Capgemini" and p.location == "Singapore, Singapore"
    assert len(p.text) > 1500
    assert "Seniority level: Associate" in p.text and "Employment type: Full-time" in p.text


def test_linkedin_search_card_salary():
    html = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4012345678">'
            '<h3 class="base-search-card__title">Backend Engineer</h3><h4 class="base-search-card__subtitle">Acme</h4>'
            '<span class="job-search-card__location">Singapore</span>'
            '<span class="job-search-card__salary-info">$7,000 - $9,000</span></div></li>')
    [listing] = sites.LinkedInSite().parse_search(html)
    assert listing.salary_max_monthly == 9000


def test_list_items_wrapped_in_paragraphs_keep_their_text_on_the_bullet():
    text = html_to_text("<ul><li><p>Fast and curious</p></li><li> </li><li>Plain</li></ul>"
                        "<ul><li><ul><li>nested</li></ul></li></ul>")
    assert text.split("\n\n") == ["- Fast and curious", "- Plain", "- nested"]


# ── HTTP 429: wait once, then try again ───────────────────────────────

class _Browser:
    def __init__(self, statuses):
        self.statuses, self.loads = list(statuses), 0
        self.last_url, self.last_status = "", 0

    def load_html(self, url):
        self.loads += 1
        self.last_url, self.last_status = url, self.statuses.pop(0)
        return "<html><head><title>Jobs</title></head><body>ok</body></html>"


def _ctx(browser, logs=None):
    return sites.SiteContext(browser, {"min_delay_s": 0, "max_delay_s": 0, "rate_limit_wait_s": 0},
                             log=(logs.append if logs is not None else None))


def test_one_429_is_retried_after_a_pause():
    browser, logs = _Browser([429, 200]), []
    assert "ok" in _ctx(browser, logs).get_html("https://www.linkedin.com/jobs-guest/x")
    assert browser.loads == 2 and "HTTP 429" in logs[0]


def test_a_second_429_blocks_the_site():
    with pytest.raises(sites.SiteBlocked):
        _ctx(_Browser([429, 429])).get_html("https://www.linkedin.com/jobs-guest/x")


def test_the_429_pause_ends_when_stop_is_pressed():
    stop = threading.Event()
    stop.set()
    ctx = sites.SiteContext(_Browser([429, 200]), {"min_delay_s": 0, "max_delay_s": 0, "rate_limit_wait_s": 3600},
                            stop_event=stop)
    assert "ok" in ctx.get_html("https://x.example/")  # returns at once instead of waiting an hour


# ── the hunt loop ─────────────────────────────────────────────────────

class _Task:
    settings: dict = {}

    def __init__(self):
        self.lines = []

    def log(self, message, level="info"):
        self.lines.append(message)

    def check_stop(self):
        pass


class _FlakySite:
    key = label = "flaky"

    def __init__(self, failing_keywords):
        self.failing = set(failing_keywords)

    def search(self, ctx, keyword, location, page_no):
        if keyword in self.failing:
            raise RuntimeError("HTTP 500")
        return [] if page_no else [Listing("flaky", f"https://x.example/{keyword}", keyword)]


def test_one_failed_search_page_moves_on_to_the_next_keyword(monkeypatch):
    monkeypatch.setattr(hunt, "get_site", lambda key, s: _FlakySite({"kw1"}))
    task, blocked = _Task(), {}
    got = [l.title for _, l in hunt.candidates(task, None, ["flaky"], ["kw1", "kw2"], "", blocked=blocked)]
    assert got == ["kw2"] and not blocked
    assert any("trying the next keyword" in line for line in task.lines)


def test_two_failed_search_pages_in_a_row_drop_the_site(monkeypatch):
    monkeypatch.setattr(hunt, "get_site", lambda key, s: _FlakySite({"kw1", "kw2", "kw3"}))
    blocked = {}
    assert list(hunt.candidates(_Task(), None, ["flaky"], ["kw1", "kw2", "kw3"], "", blocked=blocked)) == []
    assert "search failed" in blocked["flaky"]


class _CardSite:
    """A board whose search cards show the pay; opening a posting would fail the test."""
    key, label = "demo", "Card board"

    def search(self, ctx, keyword, location, page_no):
        if page_no:
            return []
        return [Listing("demo", "https://cards.example/1", "Backend Engineer", "Lowpay Pte Ltd",
                        salary="$3,500 – $4,500 per month", salary_max_monthly=4500)]

    def fetch(self, ctx, listing):
        raise AssertionError("a posting whose card is below the floor must not be opened")


class _NoBrowser:
    def close(self):
        pass


def test_a_search_card_below_the_floor_is_skipped_without_opening_it(secrets, candidate, fake_llm, monkeypatch):
    settings.update({"scope": {"keywords": ["engineer"]}, "pacing": {"min_delay_s": 0, "max_delay_s": 0}})
    monkeypatch.setattr(hunt, "get_site", lambda key, s: _CardSite())
    monkeypatch.setattr(hunt, "make_browser", lambda headless=True: _NoBrowser())
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": 1, "sites": ["demo"]})
    runner.wait(60)
    assert db.get_run(run_id)["status"] == "done"
    seen = db._query("SELECT * FROM seen WHERE url = 'https://cards.example/1'")[0]
    assert seen["decision"] == "skip" and "4,500" in seen["reason"]
    assert not fake_llm.requests  # no tokens spent on it


def _prepare(demo_server):
    settings.update({"demo_board_url": demo_server,
                     "scope": {"keywords": ["engineer"], "location": "Singapore", "avoid_words": ["intern"]},
                     "pacing": {"min_delay_s": 0, "max_delay_s": 0, "action_delay_s": 0},
                     "apply": {"show_browser": False}})


def _hunt(jobs: int) -> dict:
    runner = hunt.Runner()
    run_id = runner.start("hunt", {"jobs": jobs, "sites": ["demo"]})
    runner.wait(180)
    return db.get_run(run_id)


def test_an_unreadable_tailor_reply_is_asked_again(demo_server, secrets, candidate, fake_llm):
    _prepare(demo_server)
    real, tailor_calls = fake_llm.transport.handler, []

    def handler(request):
        if "tailor a Harvard-style resume" in json.loads(request.content)["messages"][0]["content"]:
            tailor_calls.append(1)
            if len(tailor_calls) == 1:  # what OpenCode Go sent once in the live run
                return httpx.Response(200, json={"choices": [{"message": {"content": '{"resume": {"summary": "x"'}}],
                                                 "usage": {"prompt_tokens": 10, "completion_tokens": 10}})
        return real(request)
    fake_llm.transport.handler = handler
    run = _hunt(1)
    assert run["status"] == "done", run["summary"]
    [job] = db.list_jobs()
    assert job["state"] == "Ready" and len(tailor_calls) == 2
    assert run["summary"].startswith("1 package(s) built")


def test_a_cover_letter_is_signed_once(demo_server, secrets, candidate, fake_llm, data_dir):
    _prepare(demo_server)
    reply = json.loads(json.dumps(TAILOR_REPLY))
    reply["cover_letter"]["paragraphs"] += ["Sincerely, Alex Tan"]
    fake_llm.tailor_reply = reply
    assert _hunt(1)["status"] == "done"
    [job] = db.list_jobs()
    letter = (data_dir / "jobs" / job["folder"] / "cover_letter.html").read_text(encoding="utf-8")
    assert letter.count("Sincerely") == 1


def test_sign_off_and_repeated_greeting_are_dropped_but_real_closing_lines_kept():
    paras = render._letter_paragraphs({"greeting": "Dear Hiring Team,", "paragraphs": [
        "Dear Hiring Team,", "Body.", "Regards to the whole team, and thanks.", "Best regards,\nAlex Tan"]})
    assert paras == ["Body.", "Regards to the whole team, and thanks."]


def test_odd_tailor_shapes_are_coerced_not_crashed():
    data = render.normalise_tailor({"resume": {"experience": [{"company": "A", "bullets": "Did x"}],
                                               "skills": "Python, SQL"},
                                    "cover_letter": "Para one.\n\nPara two.", "match_report": "great"})
    assert data["cover_letter"]["paragraphs"] == ["Para one.", "Para two."]
    assert render.normalise_resume(data["resume"])["skills"] == {"technical": "Python, SQL"}
    assert render.package_problems(data) == ["the match report is empty"]


# ── the AI's replies ──────────────────────────────────────────────────

def test_json_with_raw_line_breaks_and_chatter_is_read():
    assert llm.extract_json('Here is {the result}: {"p": "line one\nline two"}') == {"p": "line one\nline two"}
    with pytest.raises(ValueError):
        llm.extract_json('{"a": {"b": 1}, oops}')  # never a nested object out of a broken one


@pytest.mark.parametrize("body", [b"<html>Bad gateway</html>", b"", b'{"error": "overloaded"}'])
def test_an_unreadable_provider_answer_is_an_llm_error(body):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    client = llm.LLMClient({"provider": "openai_compatible", "base_url": "http://x.invalid/v1",
                            "models": {"filter": "m"}}, "", transport=transport)
    with pytest.raises(llm.LLMError):
        client.chat(stage="filter", system="s", user="u")


# ── tracker ───────────────────────────────────────────────────────────

def test_the_same_job_on_two_boards_is_a_duplicate():
    db.add_job(source="jobstreet", url="https://sg.jobstreet.com/job/1", company="TechCom Solutions Pte Ltd",
               role="Python Developer")
    assert db.is_duplicate("https://mcf.example/2", "TECHCOM SOLUTIONS (SINGAPORE) PTE. LTD.", "Python Developer")
    assert not db.is_duplicate("https://mcf.example/3", "TECHCOM SOLUTIONS (SINGAPORE) PTE. LTD.", "Data Analyst")
    assert db.company_key("GIC") == "gic" and db.company_key("Singapore Pte Ltd") == "singapore pte ltd"


def test_a_posting_that_failed_is_tried_again_next_run():
    db.mark_seen("https://x.example/1", "demo", "C", "R", "error", "timeout")
    db.mark_seen("https://x.example/2", "demo", "C", "R", "skip", "not a fit")
    assert not db.was_seen("https://x.example/1") and db.was_seen("https://x.example/2")


def test_jobs_left_mid_task_are_tidied_at_start_up():
    applying = db.add_job(source="demo", url="https://x.example/a", company="A", role="R", state="Applying")
    found = db.add_job(source="demo", url="https://x.example/f", company="F", role="R", state="Found")
    run_id = db.start_run("hunt")
    db.recover_interrupted()
    assert db.get_job(applying)["state"] == "Needs you" and "check on the site" in db.get_job(applying)["notes"]
    assert db.get_job(found)["state"] == "Error"
    assert db.get_run(run_id)["status"] == "interrupted"


def test_an_applied_job_is_never_applied_to_again(data_dir):
    jid = db.add_job(source="demo", url="https://x.example/1", company="A", role="R", state="Applied")
    task = hunt.Task("apply", {"job_id": jid}, threading.Event())
    assert hunt.apply_job(task, browser=None, job_id=jid) == "Applied"
    with TestClient(__import__("jobhunt.web.app", fromlist=["create_app"]).create_app(8765),
                    base_url="http://127.0.0.1:8765") as c:
        r = c.post(f"/jobs/{jid}/apply", headers=SAME_ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"] and not hunt.runner.busy


# ── secrets and the web page ──────────────────────────────────────────

def test_a_login_username_that_is_your_contact_email_is_not_hidden(memory_keyring):
    secrets_store.set_secret(secrets_store.login_secret_name("ats", "username"), "Alex@Example.com")
    secrets_store.set_secret(secrets_store.login_secret_name("ats", "password"), "pw-very-secret")
    s = {"logins": [{"name": "ats", "domains": ["example.com"]}]}
    red = secrets_store.Redactor.from_store(s, {"email": "alex@example.com", "links": []})
    assert red("Email: Alex@Example.com, password pw-very-secret") == "Email: Alex@Example.com, password [secret:login:ats:password]"
    assert "[secret:login:ats:username]" in secrets_store.Redactor.from_store(s)("Alex@Example.com")


def test_a_flash_message_goes_before_the_fragment(data_dir):
    from jobhunt.web.app import create_app
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        r = c.post("/profile/screening", data={"expected_salary": "SGD 7,000"}, headers=SAME_ORIGIN,
                   follow_redirects=False)
    assert r.headers["location"] == "/profile?msg=Screening%20answers%20saved.#screening"


def test_a_broken_settings_file_is_kept_aside_not_silently_lost(data_dir):
    from jobhunt import paths
    paths.settings_path().write_text('{"provider": "anthropic", oops', encoding="utf-8")
    settings.update({"reasoning": True})
    assert (paths.settings_path().parent / "settings.broken.json").read_text(encoding="utf-8").startswith('{"provider"')
    assert settings.load()["reasoning"] is True
