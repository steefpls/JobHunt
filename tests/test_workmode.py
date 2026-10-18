"""Work arrangement (remote / hybrid / on-site) from the posting, and Open apply page finding the exact
MyCareersFuture job when a company has several ads with the same title."""
import pytest
from fastapi.testclient import TestClient

from jobhunt import db, sites, workmode
from jobhunt.extract import Listing
from jobhunt.web.app import create_app


@pytest.mark.parametrize("text, kind", [
    ("This is a hybrid role based in Singapore.", "Hybrid"),
    ("Work arrangement: Hybrid (3 days in office)", "Hybrid"),
    ("2 days WFH per week", "Hybrid"),
    ("3 days a week in the office", "Hybrid"),
    ("Fully remote team across APAC.", "Remote"),
    ("You will work remotely with our US team.", "Remote"),
    ("- Working Arrangement: Fully on-site", "On-site"),
    ("Office-based role with standard working hours", "On-site"),
    ("Workplace type: On-site", "On-site"),
    # a work word has to be near: these say nothing about where the job is worked
    ("We run hybrid cloud on AWS.", ""),
    ("Experience with remote monitoring tools", ""),
    ("Provide on-site support to users.", ""),
    ("", ""),
])
def test_the_posting_says_where_the_job_is_worked(text, kind):
    got = workmode.read(f"Some intro.\n{text}\nMore duties.")
    assert got["kind"] == kind
    assert (text.strip(" -.") in got["quote"]) if kind else got["quote"] == ""


def test_hybrid_wins_over_the_remote_and_office_a_hybrid_posting_also_mentions():
    assert workmode.read("Fully remote days twice a week. This is a hybrid role.")["kind"] == "Hybrid"


def test_the_location_counts_and_mycareersfuture_ticks_show():
    assert workmode.read("", "Hybrid work in Singapore")["kind"] == "Hybrid"
    got = workmode.read("Duties.\n\nFlexible work arrangements: Telecommuting, Flexi-Hours")
    assert got["kind"] == "" and got["flex"] == ["Work from home possible", "Flexi-hours"]


def test_mycareersfuture_postings_carry_their_flexible_work_ticks():
    r = {"title": "Engineer", "description": "<p>Build things.</p>",
         "flexibleWorkArrangements": [{"id": 2, "flexibleWorkArrangement": "Telecommuting"}]}
    p = sites.MyCareersFutureSite().posting_from_job(r, Listing("mycareersfuture", "https://x/1", "Engineer", "Co", ""))
    assert "Flexible work arrangements: Telecommuting" in p.text
    assert workmode.read(p.text)["flex"] == ["Work from home possible"]


# ── Open apply page: two ads with one title, told apart by pay ───────────────

def _ad(uuid, lo, hi):
    return {"uuid": uuid, "title": "Software Engineer", "hiringCompany": {"name": "ALTAX PTE. LTD."},
            "salary": {"minimum": lo, "maximum": hi, "type": {"salaryType": "Monthly"}},
            "metadata": {"jobDetailsUrl": f"https://www.mycareersfuture.gov.sg/job/it/software-engineer-{uuid}"}}


TWO_ADS = {"results": [_ad("a" * 32, 4000, 6000), _ad("b" * 32, 7000, 10000)]}


class Search:
    def __init__(self, answer=TWO_ADS, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    def get_json(self, url, method="GET", body=None, pace=True):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


@pytest.mark.parametrize("salary, want", [
    ("$4,000 - $6,000 a month", "a" * 32),
    ("$7,000 - $10,000 a month", "b" * 32),
    ("$5,000 a month", None),  # neither: still the search
    ("", None),
])
def test_the_pay_picks_between_same_title_ads(salary, want):
    url = sites.mcf_job_url(Search(), "Software Engineer", "ALTAX PTE. LTD.", salary)
    assert url.endswith(want) if want else url == ""


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def _indeed_job(apply_url):
    return db.add_job(source="indeed", url="https://sg.indeed.com/viewjob?jk=1", company="ALTAX PTE. LTD.",
                      role="Software Engineer", apply_url=apply_url, salary="$4,000 - $6,000 a month", state="Ready",
                      posting_text="This is a hybrid role.")


SEARCH_LINK = sites.MCF_SEARCH + "Software%20Engineer%20ALTAX%20PTE.%20LTD.&sortBy=relevancy&page=0"


def test_open_apply_page_finds_the_job_a_hunt_left_as_a_search_and_keeps_it(client, monkeypatch):
    search = Search()
    monkeypatch.setattr("jobhunt.web.app._MCFSearch", lambda: search)
    jid = _indeed_job(SEARCH_LINK)
    page = client.get(f"/jobs/{jid}").text
    assert f'href="/jobs/{jid}/apply"' in page and ">Hybrid</span>" in page
    r = client.get(f"/jobs/{jid}/apply", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("a" * 32)
    assert db.get_job(jid)["apply_url"].endswith("a" * 32)
    client.get(f"/jobs/{jid}/apply", follow_redirects=False)
    assert search.calls == 1  # found once, kept


def test_open_apply_page_falls_back_to_the_search_offline(client, monkeypatch):
    import httpx
    monkeypatch.setattr("jobhunt.web.app._MCFSearch", lambda: Search(error=httpx.ConnectError("offline")))
    jid = _indeed_job(SEARCH_LINK)
    r = client.get(f"/jobs/{jid}/apply", follow_redirects=False)
    assert r.headers["location"] == SEARCH_LINK and db.get_job(jid)["apply_url"] == SEARCH_LINK


def test_open_apply_page_never_follows_a_non_web_link(client):
    jid = _indeed_job("javascript:alert(1)")
    r = client.get(f"/jobs/{jid}/apply", follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{jid}"
