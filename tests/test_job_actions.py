"""Buttons on a job: I applied, Reject (and why), and what a reason teaches the search."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.web.app import create_app

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def applied_form(page: str, jid: int) -> str:
    """The I applied form for one job ('' when the page has none)."""
    for form in re.findall(r'<form method="post" action="/jobs/%d/state".*?</form>' % jid, page, re.S):
        if "I applied" in form:
            return form
    return ""


# ── I applied ─────────────────────────────────────────────────────────

def test_i_applied_sits_next_to_apply_yourself(client):
    mcf = db.add_job(source="mycareersfuture", url="https://mcf/1", company="Lumen", role="Engineer", state="Ready")
    js = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    for path in ("/hunt", "/"):
        page = client.get(path).text
        form = applied_form(page, mcf)
        assert form and 'name="state" value="Applied"' in form and f'name="next" value="{path}"' in form
        assert page.index("Apply yourself") < page.index(form)
        assert not applied_form(page, js)  # JobHunt applies to that one: Approve & apply


def test_i_applied_marks_it_applied_today_and_goes_back(client):
    jid = db.add_job(source="mycareersfuture", url="https://mcf/1", company="Lumen", role="Engineer", state="Ready")
    r = client.post(f"/jobs/{jid}/state", data={"state": "Applied", "next": "/hunt"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert r.headers["location"].startswith("/hunt?msg=Marked%20as%20Applied%20today")
    job = db.get_job(jid)
    assert job["state"] == "Applied" and job["date_applied"] == date.today().isoformat()
    assert "Marked as Applied today" in client.get(r.headers["location"]).text


def test_needs_you_rows_and_the_job_page_have_i_applied(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    db.needs_you(jid, "verify", "The site asked for a captcha.")
    hunt_page = client.get("/hunt").text
    form = applied_form(hunt_page, jid)
    assert form and 'name="next" value="/hunt"' in form
    job_page = client.get(f"/jobs/{jid}").text
    form = applied_form(job_page, jid)
    assert form and f'name="next" value="/jobs/{jid}"' in form
    assert job_page.index("Open folder") < job_page.index(form)  # in the action row
    db.update_job(jid, state="Applied")
    assert not applied_form(client.get(f"/jobs/{jid}").text, jid)  # nothing left to mark


# ── Reject asks why ───────────────────────────────────────────────────

def reject_panel(page: str, jid: int) -> str:
    m = re.search(r'<details class="reject">\s*<summary[^>]*>Not interested</summary>\s*<form method="post" action="/jobs/%d/reject"'
                  r'.*?</details>' % jid, page, re.S)
    return m.group(0) if m else ""


def test_reject_opens_a_panel_with_reasons_instead_of_rejecting_at_once(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    for path in ("/hunt", "/", f"/jobs/{jid}"):
        panel = reject_panel(client.get(path).text, jid)
        assert panel, path
        for reason in db.REJECT_REASONS:
            assert f'name="reasons" value="{reason}"' in panel.replace("&#39;", "'")
        assert 'name="note"' in panel and f'name="next" value="{path}"' in panel
        assert "data-close-details" in panel and ">Cancel</button>" in panel and ">Not interested</button>" in panel
        assert "data-confirm" not in panel and "data-guard" not in panel  # no clash with those handlers
    assert db.get_job(jid)["state"] == "Ready"  # showing the panel changes nothing


def test_reject_with_reasons_saves_them_and_the_job_page_shows_them(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    r = client.post(f"/jobs/{jid}/reject", headers=SAME_ORIGIN, follow_redirects=False,
                    data={"reasons": ["Too senior", "Wrong location", "Made up"], "note": "  Too far   from home ",
                          "next": "/hunt"})
    assert r.status_code == 303 and r.headers["location"].startswith("/hunt?") and "msg=Marked" in r.headers["location"]
    job = db.get_job(jid)
    assert job["state"] == "Not for me"
    assert job["reject_reason"] == "Wrong location; Too senior"  # the quick picks only, in the usual order
    assert job["reject_note"] == "Too far from home"
    assert 'You marked it Not interested: Wrong location; Too senior. "Too far from home"' in job["history"]
    page = client.get(f"/jobs/{jid}").text
    assert "You turned this one down" in page and "Wrong location; Too senior." in page
    assert "Too far from home" in page and not reject_panel(page, jid)
    assert db.list_jobs("Not for me")[0]["id"] == jid and not db.list_jobs("Rejected")


def test_reject_with_no_reason_still_rejects(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    client.post(f"/jobs/{jid}/reject", data={"next": "/"}, headers=SAME_ORIGIN)
    job = db.get_job(jid)
    assert job["state"] == "Not for me" and job["reject_reason"] == "" and "(no reason given)" in job["history"]
    assert "No reason given." in client.get(f"/jobs/{jid}").text


def test_changing_your_mind_clears_the_reason(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    db.reject(jid, ["Pay too low"], "too little")
    client.post(f"/jobs/{jid}/state", data={"state": "Ready"}, headers=SAME_ORIGIN)
    job = db.get_job(jid)
    assert job["state"] == "Ready" and job["reject_reason"] == "" and job["reject_note"] == ""


def test_reject_refuses_other_sites_and_missing_jobs(client):
    jid = db.add_job(source="jobstreet", url="https://js/1", company="Acme", role="Engineer", state="Ready")
    r = client.post(f"/jobs/{jid}/reject", data={"next": "/"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and db.get_job(jid)["state"] == "Ready"
    r = client.post("/jobs/999/reject", data={"next": "https://evil.example/"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert r.headers["location"].startswith("/?err=")
