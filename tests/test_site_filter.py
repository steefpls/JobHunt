"""Applications can be filtered by where you apply (JobStreet, Indeed, LinkedIn, MyCareersFuture, a
company's own site...), together with the status and "No reply yet" filters, with a count on each.
Zech asked for it on 28 October 2026.
"""
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.sites import applied_on
from jobhunt.web.app import create_app

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(data_dir):
    return TestClient(create_app())


class Tags(HTMLParser):
    """Every start tag with its attributes, as a browser would read them."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def job(company, source, apply_url, state="Ready", **fields):
    jid = db.add_job(source=source, url=f"https://{source}.example/{company}", company=company, role="Engineer",
                     apply_url=apply_url, state="Ready", folder=company)
    if state != "Ready" or fields:
        db.update_job(jid, state=state, **fields)
    return jid


# ── where you apply ───────────────────────────────────────────────────

def test_where_an_application_is_made():
    assert applied_on("jobstreet", "https://sg.jobstreet.com/job/123") == "jobstreet"
    assert applied_on("jobstreet", "") == "jobstreet"
    assert applied_on("linkedin", "https://sg.linkedin.com/jobs/view/9") == "linkedin"
    assert applied_on("indeed", "https://sg.indeed.com/viewjob?jk=abc") == "indeed"  # Indeed's own apply
    assert applied_on("indeed", "https://sg.indeed.com/applystart?jk=abc") == "company"  # Apply on company site
    assert applied_on("indeed", "https://careers.acme.com/apply/1") == "company"
    assert applied_on("indeed", "https://www.mycareersfuture.gov.sg/job/x-123") == "mycareersfuture"
    assert applied_on("mycareersfuture", "https://www.mycareersfuture.gov.sg/job/x-123") == "mycareersfuture"
    assert applied_on("demo", "http://127.0.0.1:8765/demo/ats/1/login") == "demo"


def test_applications_filter_by_site_with_counts_and_the_other_filters(client):
    old = (date.today() - timedelta(days=30)).isoformat()
    job("Street1", "jobstreet", "https://sg.jobstreet.com/job/1", state="Applied", date_applied=old)
    job("Street2", "jobstreet", "https://sg.jobstreet.com/job/2")
    job("Acme", "indeed", "https://sg.indeed.com/applystart?jk=1", state="Applied", date_applied=old)
    job("Gov", "indeed", "https://www.mycareersfuture.gov.sg/job/gov-1")

    page = client.get("/applications").text
    for chip in ("Any site", "JobStreet <b>2</b>", "Company site <b>1</b>", "MyCareersFuture <b>1</b>"):
        assert chip in page
    assert "LinkedIn <b>" not in page  # only the sites your jobs are on

    # the sites count within the status picked, and every link keeps the other filters
    applied = client.get("/applications?state=Applied").text
    assert "JobStreet <b>1</b>" in applied and "Company site <b>1</b>" in applied
    assert "MyCareersFuture <b>" not in applied  # none of those is Applied: no chip, as with the statuses
    assert "MyCareersFuture <b>0</b>" in client.get("/applications?state=Applied&site=mycareersfuture").text
    assert 'href="/applications?state=Applied&amp;site=jobstreet"' in applied

    street = client.get("/applications?site=jobstreet").text
    assert "Street1" in street and "Street2" in street and "Acme" not in street and "Gov" not in street
    assert "All <b>2</b>" in street and "Applied <b>1</b>" in street and "Ready <b>1</b>" in street
    assert 'href="/applications?state=Applied&amp;site=jobstreet"' in street
    assert 'href="/applications?noreply=1&amp;site=jobstreet"' in street  # No reply yet keeps the site too
    assert 'href="/applications?site=jobstreet"' in street  # All keeps it

    both = client.get("/applications?state=Applied&site=jobstreet").text
    assert "Street1" in both and "Street2" not in both and "Acme" not in both
    assert 'href="/applications?state=Applied"' in both  # "Any site" drops only the site
    hidden = [a for t, a in Tags(both).tags if t == "input" and a.get("name") == "next"]
    assert hidden and all(a["value"] == "/applications?state=Applied&site=jobstreet" for a in hidden)

    quiet = client.get("/applications?noreply=1&site=company").text
    assert "Acme" in quiet and "Street1" not in quiet and "No reply yet <b>1</b>" in quiet

    none = client.get("/applications?state=Ready&site=company").text
    assert "Nothing on Company site marked Ready" in none and "Show every site" in none


def test_changing_a_status_from_a_filtered_list_comes_back_to_it(client):
    jid = job("Street1", "jobstreet", "https://sg.jobstreet.com/job/1", state="Applied")
    nxt = "/applications?state=Applied&site=jobstreet"
    r = client.post(f"/jobs/{jid}/state", data={"state": "Interview", "next": nxt}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(nxt)


def test_one_site_shows_no_site_row(client):
    job("Street1", "jobstreet", "https://sg.jobstreet.com/job/1")
    job("Street2", "jobstreet", "https://sg.jobstreet.com/job/2")
    assert "Where you apply" not in client.get("/applications").text
