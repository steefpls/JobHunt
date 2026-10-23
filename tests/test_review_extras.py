"""Not interested in all closed (and "Ad closed" as a quick pick), the Hunt page's pay floor and keywords,
and the posted day on Applications."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from jobhunt import adcheck, db, profile, settings, tuning
from jobhunt.web.app import create_app, undo_pairs

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


def _job(company, state="Ready", **kw):
    return db.add_job(source="mycareersfuture", url=f"https://example.com/{company}", company=company, role="Dev",
                      state=state, **kw)


def _closed(job_id):
    adcheck.save(job_id, "mycareersfuture", "closed", "MyCareersFuture marks it Closed")


# ── Reject all closed ─────────────────────────────────────────────────

def test_reject_all_closed_shows_only_when_an_ad_has_closed():
    a = _job("Open Co")
    client = TestClient(create_app())
    html = client.get("/").text
    assert 'id="closedBar" hidden' in html  # kept for the page's ad check to reveal
    _closed(a)
    html = client.get("/").text
    assert 'id="closedBar" >' in html and "1 job here has a closed ad." in html and "Not interested in all closed" in html


def test_reject_all_closed_rejects_each_closed_ad_with_the_reason_and_one_undo():
    gone, gone2, live = _job("Gone Co"), _job("Gone Two", state="Needs you"), _job("Live Co")
    applied = _job("Sent Co", state="Applied")
    for j in (gone, gone2, applied):
        _closed(j)
    adcheck.save(live, "mycareersfuture", "open")
    client = TestClient(create_app())
    r = client.post("/jobs/reject-closed", headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Marked%202%20jobs" in r.headers["location"]
    for j in (gone, gone2):
        job = db.get_job(j)
        assert job["state"] == "Not for me" and job["reject_reason"] == db.AD_CLOSED
        assert db.reject_text(job) == "Ad closed."
    assert db.get_job(live)["state"] == "Ready" and db.get_job(applied)["state"] == "Applied"  # only waiting jobs
    page = client.get(r.headers["location"]).text
    assert 'action="/jobs/undo-all"' in page and f"{gone}:Ready,{gone2}:Needs you" in page
    # Undo puts every one back where it was, reason cleared
    r = client.post("/jobs/undo-all", data={"jobs": f"{gone}:Ready,{gone2}:Needs you", "next": "/"},
                    headers=SAME_ORIGIN, follow_redirects=False)
    assert "2%20jobs%20are%20back" in r.headers["location"]
    assert db.get_job(gone)["state"] == "Ready" and db.get_job(gone2)["state"] == "Needs you"
    assert db.get_job(gone)["reject_reason"] == ""


def test_reject_all_closed_with_nothing_closed_moves_nothing():
    j = _job("Live Co")
    r = TestClient(create_app()).post("/jobs/reject-closed", headers=SAME_ORIGIN, follow_redirects=False)
    assert "warn=" in r.headers["location"] and db.get_job(j)["state"] == "Ready"


def test_a_closed_ad_is_not_a_taste_the_ai_learns_from():
    j = _job("Gone Co")
    db.reject(j, [db.AD_CLOSED])
    k = _job("Far Co")
    db.reject(k, ["Wrong location"])
    assert [r["company"] for r in db.recent_rejections()] == ["Far Co"]
    assert tuning.suggest(db.get_job(j)) is None  # nothing to change in the Scope for a closed ad


# ── "Ad closed" as a quick pick ─────────────────────────────────────

def test_ad_closed_is_a_quick_pick_when_turning_one_job_down():
    assert db.AD_CLOSED in db.REJECT_REASONS
    jid = _job("Gone Co")
    client = TestClient(create_app())
    assert f'name="reasons" value="{db.AD_CLOSED}"' in client.get(f"/jobs/{jid}").text
    client.post(f"/jobs/{jid}/reject", data={"reasons": [db.AD_CLOSED], "next": "/"}, headers=SAME_ORIGIN)
    job = db.get_job(jid)
    assert job["state"] == "Not for me" and job["reject_reason"] == db.AD_CLOSED


def test_ad_closed_by_hand_is_not_a_taste_the_ai_learns_from_even_with_a_note():
    j = _job("Gone Co")
    db.reject(j, [db.AD_CLOSED], "Indeed says it expired")
    assert db.recent_rejections() == [] and tuning.dislikes_text() == ""
    assert tuning.suggest(db.get_job(j)) is None  # the note says why it closed: not a Scope line


def test_ad_closed_with_other_reasons_keeps_only_those_as_tastes():
    j = _job("Far Co")
    db.reject(j, ["Ad closed", "Too senior", "Wrong location"], "too far anyway")
    job = db.get_job(j)
    assert job["reject_reason"] == "Wrong location; Too senior; Ad closed"  # your own record keeps all of it
    assert db.reject_text(job) == 'Wrong location; Too senior; Ad closed. "too far anyway"'
    [row] = db.recent_rejections()
    assert row["reject_reason"] == "Wrong location; Too senior" and row["reject_note"] == "too far anyway"
    assert "Ad closed" not in tuning.dislikes_text() and "Too senior" in tuning.dislikes_text()
    db.reject(_job("Note Co"), ["Ad closed", "Other"], "agency")
    assert db.recent_rejections()[0]["reject_reason"] == "Other"


def test_undo_pairs_reads_only_real_jobs_and_states():
    assert undo_pairs("12:Ready,13:Needs you") == [(12, "Ready"), (13, "Needs you")]
    assert undo_pairs("x:Ready,14:Nope,,15:Applied") == [(15, "Applied")]
    assert undo_pairs("") == []


def test_one_job_undo_still_works():
    j = _job("Solo Co")
    client = TestClient(create_app())
    r = client.post(f"/jobs/{j}/reject", data={"reasons": ["Too senior"], "next": "/"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert f'action="/jobs/{j}/undo"' in page and 'action="/jobs/undo-all"' not in page


# ── Hunt page: what it will search for ────────────────────────────────

def test_hunt_page_shows_the_saved_pay_floor_and_keywords():
    answers = profile.load_screening()
    answers["expected_salary"] = "SGD 6,500 per month"
    profile.save_screening(answers)
    s = settings.load()
    s["scope"]["keywords"] = ["data analyst", "BI developer"]
    settings.save(s)
    html = TestClient(create_app()).get("/hunt").text
    assert "Pay floor" in html and "$6,500 a month" in html
    assert '<span class="kwtag">data analyst</span>' in html and '<span class="kwtag">BI developer</span>' in html
    assert 'href="/scope"' in html and "Change in Preferences" in html


def test_hunt_page_says_when_nothing_is_saved():
    html = TestClient(create_app()).get("/hunt").text
    assert "None saved: no job is skipped for its pay" in html and "None saved yet" in html


# ── Applications: when it was posted ──────────────────────────────────

def test_applications_rows_show_the_posted_day():
    _job("Old Co", state="Applied", posted_at=(date.today() - timedelta(days=2)).isoformat())
    _job("Undated Co", state="Applied")
    html = TestClient(create_app()).get("/applications").text
    assert "posted 2 days ago" in html
    assert html.count("posted ") == 1  # a job the board didn't date gets no line
