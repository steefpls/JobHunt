"""Replies from job sites: an Applied job that Indeed or JobStreet says was turned down moves to Employer said no."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, statuscheck
from jobhunt.browser import BrowserSession
from jobhunt.web.app import create_app

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}

# An applied list shaped like Indeed's: each card links its job (jk=...), the status sits above the title, and an
# "Update status" menu that isn't open (so not shown) lists every status, the employer's "no" included.
INDEED_PAGE = """<html><head><title>My jobs | Indeed</title></head><body>
<nav><a href="#">Saved</a> <a href="#">Applied</a> <a href="#">Interviews</a></nav>
<ul class="list">
 <li class="card"><div class="status">Not selected by employer</div>
   <a href="https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa">Software Engineer</a><div>Acme Pte Ltd</div>
   <div class="menu" style="display:none">Interviewing Offer received Not selected by employer</div></li>
 <li class="card"><div class="status">Applied</div>
   <a href="https://sg.indeed.com/viewjob?jk=bbbbbbbbbbbbbbbb">Backend Developer</a><div>Beta Labs</div>
   <div class="menu" style="display:none">Interviewing Offer received Not selected by employer</div></li>
 <li class="card"><div class="status">Response seems unlikely</div>
   <a href="https://sg.indeed.com/viewjob?jk=cccccccccccccccc">Data Engineer</a><div>Gamma Corp</div></li>
</ul></body></html>"""

# Shaped like nothing in particular: no job links at all, only words. The job is found by its title and company.
JOBSTREET_PAGE = """<html><head><title>Applied jobs | Jobstreet</title></head><body>
<section><h1>Applied jobs</h1>
 <div class="c1"><h3>QA Engineer</h3><span>Delta Systems</span><p>Applied 3 weeks ago</p><p>Not suitable</p></div>
 <div class="c1"><h3>Support Engineer</h3><span>Epsilon Pte Ltd</span><p>Applied 2 days ago</p><p>Viewed by employer</p></div>
</section></body></html>"""


def _job(source, url, company, role, state="Applied"):
    jid = db.add_job(source=source, url=url, company=company, role=role, state="Ready")
    db.update_job(jid, state=state)
    return jid


@pytest.fixture
def sites(tmp_path, monkeypatch):
    """Both lists served from files, and a browser without a window."""
    (tmp_path / "indeed.html").write_text(INDEED_PAGE, encoding="utf-8")
    (tmp_path / "jobstreet.html").write_text(JOBSTREET_PAGE, encoding="utf-8")
    monkeypatch.setitem(statuscheck.SITES, "indeed", dict(statuscheck.SITES["indeed"], url=(tmp_path / "indeed.html").as_uri()))
    monkeypatch.setitem(statuscheck.SITES, "jobstreet", dict(statuscheck.SITES["jobstreet"],
                                                             url=(tmp_path / "jobstreet.html").as_uri()))
    monkeypatch.setattr(statuscheck, "_load_everything", lambda page, stop_event=None: None)
    return lambda headless: BrowserSession(headless=True)


def test_only_the_employers_answer_counts():
    assert statuscheck.rejection_in("Not selected by employer\nSoftware Engineer") == "not selected by employer"
    assert statuscheck.rejection_in("Applied 2 days ago · Not suitable") == "not suitable"
    assert statuscheck.rejection_in("Your application is unlikely to progress") == "unlikely to progress"
    for text in ("Response seems unlikely", "Viewed by employer", "Applied", "No longer interested", "Job closed", ""):
        assert statuscheck.rejection_in(text) == "", text


def test_the_sites_own_job_id_from_the_tracker():
    assert statuscheck.site_job_id("indeed", "https://sg.indeed.com/viewjob?jk=0123456789abcdef") == "0123456789abcdef"
    assert statuscheck.site_job_id("jobstreet", "https://sg.jobstreet.com/job/87654321") == "87654321"
    assert statuscheck.site_job_id("jobstreet", "", "https://example.com/apply") == ""


def test_turned_down_jobs_move_and_nothing_else_does(sites):
    acme = _job("indeed", "https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa", "ACME PTE. LTD.", "Software Engineer")
    beta = _job("indeed", "https://sg.indeed.com/viewjob?jk=bbbbbbbbbbbbbbbb", "Beta Labs", "Backend Developer")
    gamma = _job("indeed", "https://sg.indeed.com/viewjob?jk=cccccccccccccccc", "Gamma Corp", "Data Engineer")
    delta = _job("jobstreet", "https://sg.jobstreet.com/job/11111111", "Delta Systems", "QA Engineer")
    eps = _job("jobstreet", "https://sg.jobstreet.com/job/22222222", "Epsilon Pte Ltd", "Support Engineer")
    interview = _job("jobstreet", "https://sg.jobstreet.com/job/33333333", "Delta Systems", "QA Engineer", "Interview")
    log = []
    state = statuscheck.run(sites, log=log.append)
    states = {j: db.get_job(j)["state"] for j in (acme, beta, gamma, delta, eps, interview)}
    assert states == {acme: "Rejected", beta: "Applied", gamma: "Applied", delta: "Rejected", eps: "Applied",
                      interview: "Interview"}  # only Applied jobs are looked at; nothing goes to Interview or Offer
    assert state["sites"]["indeed"] == dict(state["sites"]["indeed"], state="ok", checked=3, found=3, moved=1)
    assert state["sites"]["jobstreet"]["found"] == 2 and state["sites"]["jobstreet"]["moved"] == 1
    assert 'Indeed says the employer said no ("not selected by employer"' in db.get_job(acme)["history"]
    assert db.query("SELECT by FROM job_states WHERE job_id = ? ORDER BY id DESC LIMIT 1", (acme,))[0]["by"] == "site"
    assert any("Delta Systems said no" in m for m in log)
    assert "Indeed: found 3 of 3 applied job(s), 1 turned down." in statuscheck.summary(state)
    news = statuscheck.news()
    assert {j["id"] for j in news} == {acme, delta} and {j["site"] for j in news} == {"Indeed", "JobStreet"}


def test_a_job_put_back_after_a_site_moved_it_is_never_moved_again(sites):
    acme = _job("indeed", "https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa", "Acme", "Software Engineer")
    statuscheck.run(sites)
    db.update_job(acme, by="you", state="Applied")  # the Undo
    assert statuscheck.jobs_to_check() == {}
    statuscheck.run(sites)
    assert db.get_job(acme)["state"] == "Applied"
    assert statuscheck.news() == []


def test_a_list_it_cant_read_is_kept_and_said(sites, data_dir):
    _job("indeed", "https://sg.indeed.com/viewjob?jk=dddddddddddddddd", "Nowhere Ltd", "Product Manager")
    state = statuscheck.run(sites)
    assert state["sites"]["indeed"]["state"] == "unreadable"
    assert "couldn't find your jobs on Indeed's list" in statuscheck.summary(state)
    assert (data_dir / "status-pages" / "indeed.html").read_text(encoding="utf-8").count("Software Engineer") == 1


def test_logged_out_is_said_and_moves_nothing(sites, tmp_path, monkeypatch):
    acme = _job("indeed", "https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa", "Acme", "Software Engineer")
    login = tmp_path / "auth" / "login.html"
    login.parent.mkdir()
    login.write_text("<title>Sign in</title>Not selected by employer", encoding="utf-8")
    monkeypatch.setitem(statuscheck.SITES, "indeed", dict(statuscheck.SITES["indeed"], url=login.as_uri()))
    state = statuscheck.run(sites)
    assert state["sites"]["indeed"]["state"] == "out" and db.get_job(acme)["state"] == "Applied"


def test_a_hunt_checks_first_only_when_its_been_a_while():
    assert not statuscheck.due()  # nothing applied for on Indeed or JobStreet: nothing to check
    _job("jobstreet", "https://sg.jobstreet.com/job/11111111", "Delta", "QA Engineer")
    assert statuscheck.due()
    statuscheck.save_state({"checked_at": datetime.now().isoformat(timespec="seconds")})
    assert not statuscheck.due()
    assert statuscheck.due(datetime.now() + timedelta(hours=7))
    _job("mycareersfuture", "https://www.mycareersfuture.gov.sg/job/x", "Zeta", "Analyst")
    assert set(statuscheck.jobs_to_check()) == {"jobstreet"}  # Singpass: never checked


def test_hunt_runs_the_check_before_searching(monkeypatch):
    from jobhunt import hunt, settings
    calls = []
    monkeypatch.setattr(statuscheck, "due", lambda: True)
    monkeypatch.setattr(statuscheck, "run", lambda factory, log, stop_event: calls.append("check"))
    monkeypatch.setattr(hunt, "readiness", lambda s=None: [])
    monkeypatch.setattr(hunt.Task, "get_llm", lambda self: None)

    def no_search(*a, **kw):
        calls.append("search")
        return iter(())
    monkeypatch.setattr(hunt, "candidates", no_search)
    import threading
    hunt.run_hunt(hunt.Task("hunt", {"sites": ["demo"]}, threading.Event()))
    assert calls == ["check", "search"]
    settings.update({"hunt": {"check_replies": False}})
    calls.clear()
    hunt.run_hunt(hunt.Task("hunt", {"sites": ["demo"]}, threading.Event()))
    assert calls == ["search"]


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_to_review_lists_the_replies_with_undo_and_got_it(client):
    acme = _job("indeed", "https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa", "Acme", "Software Engineer")
    beta = _job("jobstreet", "https://sg.jobstreet.com/job/1", "Beta Labs", "QA Engineer")
    for jid in (acme, beta):
        db.update_job(jid, by="site", state="Rejected")
    html = client.get("/").text
    assert "Replies from job sites" in html and "Acme" in html and "Beta Labs" in html
    r = client.post(f"/jobs/{acme}/undo", data={"state": "Applied", "next": "/"}, headers=SAME_ORIGIN)
    assert db.get_job(acme)["state"] == "Applied" and "Undone" in str(r.url)
    html = client.get("/").text
    assert "Replies from job sites" in html and "Acme</a>" not in html
    client.post("/replies/seen", headers=SAME_ORIGIN)
    assert "Replies from job sites" not in client.get("/").text


def test_applications_offers_check_for_replies(client, monkeypatch):
    from jobhunt import hunt
    assert "Check for replies" not in client.get("/applications").text  # nothing to check yet
    _job("indeed", "https://sg.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa", "Acme", "Software Engineer")
    html = client.get("/applications").text
    assert "Check for replies" in html and "haven't been checked for replies yet" in html
    started = []
    monkeypatch.setattr(hunt.runner, "start", lambda kind, params=None: started.append(kind))
    r = client.post("/replies/check", headers=SAME_ORIGIN, follow_redirects=False)
    assert started == ["replies"] and r.headers["location"].startswith("/hunt")
    statuscheck.save_state({"checked_at": db.now(), "sites": {"indeed": {
        "state": "out", "detail": "not logged in to Indeed in JobHunt's browser", "checked_at": db.now()}}})
    html = client.get("/applications").text
    assert "last checked for replies just now" in html and "Not logged in to Indeed in JobHunt&#39;s browser." in html


def test_the_sidebar_shows_the_total_ai_cost(client):
    assert "spent on AI" not in client.get("/").text and "est. AI cost" not in client.get("/").text
    db.record_llm_call(run_id=None, job_id=None, stage="filter", model="m", tokens_in=1000, tokens_out=200,
                       cost=1.5, seconds=1)
    db.record_llm_call(run_id=None, job_id=None, stage="tailor", model="m", tokens_in=3000, tokens_out=800,
                       cost=0.75, seconds=1)
    html = client.get("/hunt").text
    assert "$2.25 est. AI cost" in html or "$2.25 spent on AI" in html
    assert "5,000 tokens" in html and 'href="/applications#aiuse"' in html


def test_the_setting_turns_the_hunts_check_off(client):
    from jobhunt import settings
    assert settings.load()["hunt"]["check_replies"] is True
    client.post("/settings", data={"max_candidates": "60"}, headers=SAME_ORIGIN)
    assert settings.load()["hunt"]["check_replies"] is False
    client.post("/settings", data={"max_candidates": "60", "check_replies": "on"}, headers=SAME_ORIGIN)
    assert settings.load()["hunt"]["check_replies"] is True
    assert json.loads(json.dumps(settings.load()["hunt"]))  # still plain JSON
