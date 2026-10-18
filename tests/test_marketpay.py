"""Market pay: what similar jobs on MyCareersFuture pay, next to what this one says."""
import json

import pytest
from fastapi.testclient import TestClient

from jobhunt import db, marketpay
from jobhunt.extract import salary_range_monthly
from jobhunt.web.app import create_app


def ad(title, lo, hi, company="Firm", period="Monthly"):
    return {"title": title, "hiringCompany": {"name": company},
            "salary": {"minimum": lo, "maximum": hi, "type": {"salaryType": period}}}


ADS = ([ad("Backend Engineer", 6000 + i * 500, 8000 + i * 500, f"Firm {i}") for i in range(5)]
       + [ad("Senior Backend Engineer", 9000, 12000, f"Big {i}") for i in range(2)]
       + [ad("Backend Engineer", 6000, 8000, "Firm 0")] * 3  # one firm's copies of one ad count once
       + [ad("Sales Executive", 2000, 2500), ad("Backend Engineer", 30, 40, "Hourly", "Hourly")])


@pytest.fixture
def mcf(monkeypatch):
    asked = []

    def post(url, body, timeout=20):
        asked.append(body["search"])
        return {"results": ADS}
    monkeypatch.setattr(marketpay, "http_post", post)
    return asked


def test_stated_pay_ranges():
    assert salary_range_monthly("$5,500 – $8,000 per month") == (5500, 8000)
    assert salary_range_monthly("SGD 84,000 - 120,000 per annum") == (7000, 10000)
    assert salary_range_monthly("$6k") == (6000, 6000)
    assert salary_range_monthly("5-6k") == (5000, 6000)
    assert salary_range_monthly("$25 per hour") == (0, 0)
    assert salary_range_monthly("USD 5,000 - 7,000") == (0, 0)
    assert salary_range_monthly("") == (0, 0)


def test_what_a_title_says():
    core, level, query = marketpay.describe("Senior Backend Engineer (AWS) - Payments team")
    assert core == {"backend", "engineer"} and level == "senior" and query == "senior backend engineer"
    assert marketpay.describe("Junior Data Analyst, Singapore")[1] == "junior"
    assert marketpay.same_job(frozenset({"backend", "engineer"}), frozenset({"backend", "software", "engineer"}))
    assert not marketpay.same_job(frozenset({"backend", "engineer"}), frozenset({"sales", "executive"}))


def test_similar_jobs_at_the_same_level(mcf):
    r = marketpay.estimate("Backend Engineer", "$5,000 - $6,000 per month")
    assert r["status"] == "ok" and r["ads"] == 5 and not r["all_levels"] and r["level"] == "mid-level"
    assert (r["low"], r["high"], r["mid"]) == (7000, 9000, 8000)
    assert r["text"] == "$7k–$9k a month (typical $8k)"
    assert r["job"] == "$5k–$6k" and r["verdict"] == "below"
    assert marketpay.estimate("Backend Engineer", "$9,000 - $10,000")["verdict"] == "above"
    assert marketpay.estimate("Backend Engineer", "$7,500 - $8,500")["verdict"] == "in line"
    assert "verdict" not in marketpay.estimate("Backend Engineer", "")


def test_too_few_at_this_level_counts_every_level(mcf):
    r = marketpay.estimate("Senior Backend Engineer")
    assert r["status"] == "ok" and r["all_levels"] and r["ads"] == 7


def test_too_few_overall_says_so(mcf):
    r = marketpay.estimate("Sales Executive")
    assert r["status"] == "few" and "Only 1 similar ad" in r["why"]


def test_a_search_is_kept_for_a_week(mcf):
    marketpay.estimate("Backend Engineer")
    marketpay.estimate("Backend Engineer (Payments)")
    assert mcf == ["backend engineer"]


def test_offline_is_an_answer_not_a_crash():
    r = marketpay.estimate("Backend Engineer")
    assert r["status"] == "error" and "Couldn't reach MyCareersFuture" in r["why"]


def test_saved_on_the_job_and_shown(mcf):
    jid = db.add_job(source="demo", url="http://x/1", company="Lumen", role="Backend Engineer", folder="",
                     state="Ready", salary="$5,000 - $6,000 per month")
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as client:
        assert "Not checked yet" in client.get(f"/jobs/{jid}").text
        r = client.post(f"/jobs/{jid}/market-pay", headers={"origin": "http://127.0.0.1:8765"})
        assert r.status_code == 200
        assert json.loads(db.get_job(jid)["market_pay"])["status"] == "ok"
        page = client.get(f"/jobs/{jid}").text
        assert "$7k–$9k a month (typical $8k)" in page and "Below the market" in page and "From 5 current ads" in page
        dash = client.get("/").text
        assert dash.count("pay below market") == 2  # the Ready list and the table
        assert "pay below market" in client.get("/hunt").text
