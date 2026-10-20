"""An offer's end: Accepted or Turned it down (each with Undo), and the box for its pay, start date and answer-by date."""
import csv
import io
import re
import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunt import achievements, db, paths, refresh, statuscheck
from jobhunt.web.app import create_app, long_date, no_reply_days

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def offer_job(n: int = 1, source: str = "jobstreet", state: str = "Offer") -> int:
    jid = db.add_job(source=source, url=f"https://sg.jobstreet.com/job/1000000{n}", company=f"Lumen{n}",
                     role="Engineer", state="Ready")
    db.update_job(jid, state="Applied")
    db.update_job(jid, state="Interview", by="you")
    db.update_job(jid, state=state, by="you")
    return jid


def header(page: str) -> str:
    return re.search(r'<div class="actionbar".*?<div class="card company"', page, re.S).group(0)


# ── the two buttons ───────────────────────────────────────────────────

def test_an_offer_has_accepted_and_turned_it_down_instead_of_a_dead_end(client):
    jid = offer_job()
    bar = header(client.get(f"/jobs/{jid}").text)
    assert "You have an offer" in bar and "Move it along with Status" not in bar
    assert 'name="state" value="Accepted"' in bar and ">Accepted</button>" in bar
    assert 'name="state" value="Declined"' in bar and ">Turned it down</button>" in bar
    assert "Open apply page" not in bar  # nothing left to apply for


def test_accepted_says_so_with_an_undo_back_to_offer(client):
    jid = offer_job()
    r = client.post(f"/jobs/{jid}/state", data={"state": "Accepted", "next": f"/jobs/{jid}"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    loc = r.headers["location"]
    assert "undo=" + str(jid) + ":Offer" in loc and "party=offer" in loc
    assert db.get_job(jid)["state"] == "Accepted"
    page = client.get(loc).text
    assert "Marked the offer as accepted. Congratulations" in page
    assert f'action="/jobs/{jid}/undo"' in page and 'name="state" value="Offer"' in page
    bar = header(page)
    assert "You accepted the offer" in bar and "Move it along with Status" not in bar
    assert ">Accepted</button>" not in bar and ">Turned it down</button>" not in bar  # nothing left to press
    assert "Accepted the offer" in page  # the badge
    r = client.post(f"/jobs/{jid}/undo", data={"state": "Offer", "next": f"/jobs/{jid}"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert "Undone" in client.get(r.headers["location"]).text
    assert db.get_job(jid)["state"] == "Offer"
    assert "You undid that: back to Offer." in db.get_job(jid)["history"]


def test_turned_it_down_says_so_with_an_undo(client):
    jid = offer_job()
    r = client.post(f"/jobs/{jid}/state", data={"state": "Declined", "next": f"/jobs/{jid}"}, headers=SAME_ORIGIN,
                    follow_redirects=False)
    loc = r.headers["location"]
    assert "undo=" + str(jid) + ":Offer" in loc and "party=" not in loc
    page = client.get(loc).text
    assert "Marked the offer as turned down." in page
    bar = header(page)
    assert "You turned the offer down" in bar and "Turned the offer down" in bar  # header and badge
    client.post(f"/jobs/{jid}/undo", data={"state": "Offer"}, headers=SAME_ORIGIN)
    assert db.get_job(jid)["state"] == "Offer"


def test_the_new_states_are_in_the_status_menus_with_icons(client):
    jid = offer_job()
    sprite = (paths.APP_DIR / "jobhunt" / "web" / "static" / "icons.svg").read_text(encoding="utf-8")
    for page in (client.get(f"/jobs/{jid}").text, client.get("/applications").text):
        assert ">Accepted the offer</button>" in page.replace("\n", "")
        assert ">Turned the offer down</button>" in page.replace("\n", "")
        assert all(f'<symbol id="{n}"' in sprite for n in re.findall(r'icons\.svg\?v=[0-9a-f]{10}#([\w-]+)"', page))
    assert '<symbol id="handshake"' in sprite and '<symbol id="circle-minus"' in sprite


# ── everywhere else states are used ───────────────────────────────────

def test_applications_filters_counts_and_dates(client):
    a, d, o = offer_job(1, state="Accepted"), offer_job(2, state="Declined"), offer_job(3)
    never = db.add_job(source="linkedin", url="https://x/9", company="Direct", role="Engineer", state="Ready")
    db.update_job(never, state="Accepted", by="you")  # straight from Ready: no applied date, so the day it ended
    page = client.get("/applications").text
    assert "Accepted the offer <b>2</b>" in page and "Turned the offer down <b>1</b>" in page
    only = client.get("/applications?state=Accepted").text
    assert "Lumen1" in only and "Lumen2" not in only and "Lumen3" not in only and "Direct" in only
    job = next(j for j in db.list_jobs() if j["id"] == never)
    assert db.applied_or_rejected(job) == ("Accepted", date.today().isoformat())
    assert f"Accepted the offer {date.today().isoformat()}" in only
    assert db.applied_or_rejected(next(j for j in db.list_jobs() if j["id"] == d))[0] == "Applied"
    assert {o} == {j["id"] for j in db.list_jobs("Offer")}


def test_no_reply_yet_and_the_replies_check_leave_offers_alone():
    old = (date.today() - timedelta(days=40)).isoformat()
    ids = [offer_job(n, state=s) for n, s in enumerate(("Offer", "Accepted", "Declined"), 1)]
    for jid in ids:
        db._exec("UPDATE jobs SET date_applied = ? WHERE id = ?", (old, jid))
        assert no_reply_days(db.get_job(jid)) == 0
        assert db.get_job(jid)["state"] in refresh.SENT_STATES
    assert statuscheck.jobs_to_check() == {}  # only Applied jobs are ever read from a site


def test_an_accepted_or_declined_offer_still_counts_as_an_offer_and_an_interview():
    db.update_job(offer_job(1), state="Accepted", by="you")
    straight = db.add_job(source="jobstreet", url="https://x/2", company="B", role="Engineer", state="Ready")
    db.update_job(straight, state="Declined", by="you")  # never marked Applied, Interview or Offer
    v = achievements.values()
    assert v["options"] == 2 and v["foot_in_door"] == 2 and v["out_the_door"] == 2


def test_csv_export_has_the_new_states_and_the_offer(client):
    jid = offer_job(state="Accepted")
    db.set_offer(jid, "$6,500 a month", "2026-11-03", "2026-10-25", "Two weeks' notice")
    rows = list(csv.reader(io.StringIO(client.get("/export.csv").text)))
    row = dict(zip(rows[0], rows[1]))
    assert row["state"] == "Accepted" and row["offer_pay"] == "$6,500 a month"
    assert row["offer_start"] == "2026-11-03" and row["offer_answer_by"] == "2026-10-25"
    assert row["offer_note"] == "Two weeks' notice"


# ── the offer box ─────────────────────────────────────────────────────

def test_the_offer_box_saves_and_shows_back(client):
    jid = offer_job()
    page = client.get(f"/jobs/{jid}").text
    assert 'id="offer"' in page and f'action="/jobs/{jid}/offer"' in page and "Save offer details" in page
    r = client.post(f"/jobs/{jid}/offer", data={"pay": "  $6,500 a month\n+ bonus ", "start": "2026-11-03",
                                                "answer_by": "2026-10-25", "note": "Ask about leave.\nReply to Mei."},
                    headers=SAME_ORIGIN, follow_redirects=False)
    assert r.headers["location"].startswith(f"/jobs/{jid}?msg=Offer%20details%20saved") and r.headers["location"].endswith("#offer")
    job = db.get_job(jid)
    assert (job["offer_pay"], job["offer_start"], job["offer_answer_by"]) == ("$6,500 a month + bonus", "2026-11-03",
                                                                               "2026-10-25")
    assert job["offer_note"] == "Ask about leave.\nReply to Mei." and job["state"] == "Offer"
    page = client.get(f"/jobs/{jid}").text
    assert "3 November 2026" in page and "25 October 2026" in page and "Ask about leave." in page
    assert 'value="2026-11-03"' in page and 'value="$6,500 a month + bonus"' in page  # the Edit form has it too
    db.update_job(jid, state="Accepted", by="you")
    assert "3 November 2026" in client.get(f"/jobs/{jid}").text  # still there once it's accepted


def test_the_offer_box_drops_a_date_that_isnt_one_and_can_be_cleared(client):
    jid = offer_job()
    client.post(f"/jobs/{jid}/offer", data={"pay": "5k", "start": "next Monday", "answer_by": "2026-02-30"},
                headers=SAME_ORIGIN)
    job = db.get_job(jid)
    assert job["offer_pay"] == "5k" and job["offer_start"] == "" and job["offer_answer_by"] == ""
    client.post(f"/jobs/{jid}/offer", data={}, headers=SAME_ORIGIN)
    assert db.get_job(jid)["offer_pay"] == ""
    assert "Save offer details" in client.get(f"/jobs/{jid}").text  # empty again: the form, not the summary


def test_the_offer_box_is_only_on_offers(client):
    jid = db.add_job(source="jobstreet", url="https://x/1", company="A", role="Engineer", state="Ready")
    db.update_job(jid, state="Applied")
    assert 'id="offer"' not in client.get(f"/jobs/{jid}").text
    assert client.post("/jobs/999/offer", data={"pay": "1"}, headers=SAME_ORIGIN,
                       follow_redirects=False).headers["location"].startswith("/?err=")
    assert client.post(f"/jobs/{jid}/offer", data={"pay": "1"}).status_code == 403  # not from the app's own page


def test_long_date():
    assert long_date("2026-11-03") == "3 November 2026" and long_date("") == "" and long_date("soon") == ""


# ── an older tracker ──────────────────────────────────────────────────

def test_an_older_tracker_gets_the_offer_columns(data_dir):
    jid = offer_job()
    # the jobs table as it was before the offer columns, with the same row in it
    old = re.search(r"CREATE TABLE IF NOT EXISTS jobs \(.*?\n\);", db.SCHEMA, re.S).group(0)
    old = re.sub(r"\n\s*offer_\w+ [^\n]*", "", old).replace("ad_status TEXT NOT NULL DEFAULT '',",
                                                            "ad_status TEXT NOT NULL DEFAULT ''")
    con = sqlite3.connect(paths.db_path())
    con.execute("ALTER TABLE jobs RENAME TO jobs_now")
    con.executescript(old)
    cols = ", ".join(r[1] for r in con.execute("PRAGMA table_info(jobs)"))
    con.execute(f"INSERT INTO jobs ({cols}) SELECT {cols} FROM jobs_now")
    con.execute("DROP TABLE jobs_now")
    con.commit()
    assert "offer_pay" not in {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    con.close()
    db.init()
    job = db.get_job(jid)
    assert job["state"] == "Offer" and job["company"] == "Lumen1"
    assert (job["offer_pay"], job["offer_start"], job["offer_answer_by"], job["offer_note"]) == ("", "", "", "")
    db.set_offer(jid, "$7k", "2026-12-01")
    assert db.get_job(jid)["offer_start"] == "2026-12-01"
    db.init()  # a second start-up changes nothing
    assert db.get_job(jid)["offer_pay"] == "$7k"
