"""Indeed ads checked for closure in the hunt's own browser."""
from pathlib import Path

import pytest

from jobhunt import adcheck, db, login_check, sites

FIX = Path(__file__).resolve().parent / "fixtures"
JK = "ad5444e9460a3553"
EXPIRED = (FIX / "indeed_viewjob_expired.html").read_text(encoding="utf-8")
OPEN = (FIX / "indeed_viewjob_open.html").read_text(encoding="utf-8")
ROBOT = (FIX / "indeed_robot_check_live.html").read_text(encoding="utf-8")
SIGN_IN = "<html><head><title>Sign In | Indeed Accounts</title></head><body><form>Email address</form></body></html>"


def _indeed(company, jk, state="Ready"):
    return db.add_job(source="indeed", url=f"https://sg.indeed.com/viewjob?jk={jk}", company=company, role="Dev",
                      state=state)


class Browser:
    """The hunt's browser: `pages` maps a job key to (html, status, final url)."""
    def __init__(self, pages, headless=False):
        self.pages, self.headless, self.urls = pages, headless, []
        self.last_url, self.last_status = "", 0

    def load_html(self, url, tab=None):
        self.urls.append(url)
        jk = url.split("jk=")[-1]
        html, self.last_status, final = self.pages.get(jk, (OPEN.replace(JK, jk), 200, ""))
        self.last_url = final or url
        return html


def _ctx(browser):
    return sites.SiteContext(browser, {"min_delay_s": 0, "max_delay_s": 0})


# ── reading Indeed's page ─────────────────────────────────────────────

def test_indeed_pages_read_as_closed_open_or_not_sure():
    assert adcheck.indeed_state(EXPIRED, JK) == ("closed", "Indeed says the job has expired")
    assert adcheck.indeed_state(OPEN, JK) == ("open", "")
    assert adcheck.indeed_state(OPEN, "0123456789abcdef1")[0] == "unknown"  # another job's page
    search = (FIX / "indeed_search_live.html").read_text(encoding="utf-8")
    assert adcheck.indeed_state(search, JK)[0] == "unknown"  # sent to a search page: not a sign it closed
    assert adcheck.indeed_state("<html><body>Something went wrong</body></html>", JK)[0] == "unknown"


def test_indeeds_robot_check_and_sign_in_page_stop_the_check():
    with pytest.raises(sites.SiteBlocked):
        adcheck.indeed_state(ROBOT, JK)
    with pytest.raises(sites.SiteBlocked):
        adcheck.indeed_state(SIGN_IN, JK, "https://secure.indeed.com/auth?continue=%2Fviewjob")
    with pytest.raises(sites.SiteBlocked):  # the title sits past 6 KB of style on the real page
        sites.check_blocked(ROBOT, f"https://sg.indeed.com/viewjob?jk={JK}")


# ── as a hunt starts ──────────────────────────────────────────────────

def test_a_hunt_flags_an_expired_indeed_ad_and_moves_nothing():
    gone, live = _indeed("Gone Co", JK), _indeed("Live Co", "c716dea90f0a01f4", state="Needs you")
    _indeed("Sent Co", "889d3ac98e00e706", state="Applied")  # not waiting for you: not checked
    browser = Browser({JK: (EXPIRED, 200, "")})
    logs = []
    assert adcheck.run(_ctx(browser), log=logs.append) == 2
    assert sorted(browser.urls) == ["https://sg.indeed.com/viewjob?jk=ad5444e9460a3553",
                                    "https://sg.indeed.com/viewjob?jk=c716dea90f0a01f4"]
    assert adcheck.closed(db.get_job(gone))["why"] == "Indeed says the job has expired"
    assert adcheck.load(db.get_job(live))["state"] == "open"
    assert db.get_job(gone)["state"] == "Ready"  # flagged, never moved
    assert any("Gone Co - Dev has closed" in l for l in logs)
    assert adcheck.run(_ctx(browser)) == 0  # checked lately: not loaded again


@pytest.mark.parametrize("page", [(ROBOT, 403, ""), (ROBOT, 200, ""),
                                  (SIGN_IN, 200, "https://secure.indeed.com/auth?continue=%2Fviewjob")])
def test_a_robot_check_or_sign_in_ends_indeeds_check_for_that_hunt(page):
    first, second = _indeed("First Co", JK), _indeed("Second Co", "c716dea90f0a01f4")
    street = db.add_job(source="jobstreet", url="https://sg.jobstreet.com/job/1", company="Street Co", role="Dev",
                        state="Ready")

    class Both(Browser):
        def load_html(self, url, tab=None):
            if "jobstreet" in url:
                self.urls.append(url)
                self.last_url, self.last_status = url, 200
                return '{"isExpired":true}'
            return super().load_html(url, tab)
    browser, logs = Both({JK: page, "c716dea90f0a01f4": page}), []
    adcheck.run(_ctx(browser), log=logs.append)
    assert len([u for u in browser.urls if "indeed" in u]) == 1  # stopped at the first
    for j in (first, second):
        assert adcheck.load(db.get_job(j)) is None and adcheck.closed(db.get_job(j)) is None  # left unflagged
    assert adcheck.closed(db.get_job(street))  # the other sites carry on
    assert any("Indeed turned the check away" in l for l in logs)


def test_indeed_is_checked_only_with_a_window_and_never_when_logged_out():
    _indeed("Gone Co", JK)
    headless = Browser({JK: (EXPIRED, 200, "")}, headless=True)
    logs = []
    assert adcheck.run(_ctx(headless), log=logs.append) == 0 and headless.urls == []
    assert any("when a hunt has Indeed ticked" in l for l in logs)
    login_check.save_status({"indeed": {"state": "out", "detail": "", "account": "", "checked_at": "2026-10-23T09:00:00"}})
    window, logs = Browser({JK: (EXPIRED, 200, "")}), []
    assert adcheck.run(_ctx(window), log=logs.append) == 0 and window.urls == []
    assert any("logged in to Indeed" in l for l in logs)
    login_check.save_status({"indeed": {"state": "in", "detail": "", "account": "", "checked_at": "2026-10-23T09:00:00"}})
    assert adcheck.run(_ctx(window)) == 1


def test_a_hunt_loads_at_most_a_few_indeed_pages():
    for n in range(adcheck.INDEED_LIMIT + 3):
        _indeed(f"Co {n}", f"{0xb000000000000000 + n:016x}")
    browser, logs = Browser({}), []
    assert adcheck.run(_ctx(browser), log=logs.append) == adcheck.INDEED_LIMIT
    assert len(browser.urls) == adcheck.INDEED_LIMIT
    assert any("the rest are checked next hunt" in l for l in logs)


def test_an_indeed_job_from_mycareersfuture_is_still_asked_there():
    job = {"source": "indeed", "url": f"https://sg.indeed.com/viewjob?jk={JK}",
           "apply_url": "https://www.mycareersfuture.gov.sg/job/it/dev-0123456789abcdef0123456789abcdef"}
    assert adcheck.where(job) == "mycareersfuture"
    assert adcheck.where({**job, "apply_url": ""}) == "indeed"
