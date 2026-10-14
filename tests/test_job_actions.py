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
