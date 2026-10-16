"""Rebuild PDFs takes the facts from your Profile as it is now, and keeps the wording you edited."""
import json
import os
import re
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from jobhunt import db, paths, profile, refresh, render
from jobhunt.web.app import create_app

from conftest import FIXTURES, TAILOR_REPLY

SAME_ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def built_package(company="Lumen Analytics", role="Backend Engineer") -> int:
    """A package as a hunt leaves it: resume and cover letter HTML + PDFs, made from today's Profile."""
    from jobhunt.browser import render_pdfs
    contact = profile.load_contact()
    folder = render.make_job_folder(company, role)
    pairs = [(render.resume_html(contact, TAILOR_REPLY["resume"]), folder / "resume.pdf"),
             (render.cover_letter_html(contact, company, role, TAILOR_REPLY["cover_letter"]), folder / "cover_letter.pdf")]
    for html, pdf in pairs:
        pdf.with_suffix(".html").write_text(html, encoding="utf-8")
    render_pdfs(pairs)
    return db.add_job(source="demo", url=f"https://x/{folder.name}", company=company, role=role, folder=folder.name,
                      state="Ready")


def pdf_text(path) -> str:
    return " ".join(" ".join(p.extract_text() or "" for p in PdfReader(str(path)).pages).split())


def test_rebuild_takes_a_changed_date_from_the_profile(client, candidate):
    """Zech's bug: he changed a year in his Profile, pressed Rebuild PDFs, and the resume didn't change."""
    jid = built_package()
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    assert "Jul 2020 - Feb 2023" in pdf_text(folder / "resume.pdf")
    # a hand edit to the wording, made before the Profile change
    html = (folder / "resume.html").read_text(encoding="utf-8")
    (folder / "resume.html").write_text(html.replace("Built the order-fulfilment service",
                                                     "Designed and built the order-fulfilment service"), encoding="utf-8")
    profile.save_profile(profile.load_profile().replace("CartNest Commerce - Singapore - Jul 2020 to Feb 2023",
                                                        "CartNest Commerce - Singapore - Jul 2019 to Feb 2023"))
    contact = profile.load_contact()
    profile.save_contact(contact | {"phone": "+65 8111 2222"})

    r = client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    assert "msg=Rebuilt%202%20PDF" in r.headers["location"]
    message = client.get(r.headers["location"]).text
    assert "CartNest Commerce: dates Jul 2020 - Feb 2023 → Jul 2019 - Feb 2023" in message
    assert "your contact details" in message

    text = pdf_text(folder / "resume.pdf")
    assert "Jul 2019 - Feb 2023" in text and "Jul 2020" not in text
    assert "Mar 2023 - Present" in text  # the role that didn't change keeps its dates, in the resume's format
    assert "+65 8111 2222" in text and contact["phone"] not in text
    assert "Designed and built the order-fulfilment service" in text  # your edit survived
    assert "+65 8111 2222" in pdf_text(folder / "cover_letter.pdf")
    assert "Jul 2019 - Feb 2023" in (folder / "resume.html").read_text(encoding="utf-8")  # the source too
    assert "Rebuilt the PDFs with your Profile" in db.get_job(jid)["history"]


def test_rebuild_with_nothing_changed_says_so(client, candidate):
    jid = built_package()
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    before = (folder / "resume.html").read_text(encoding="utf-8")
    r = client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    assert "already%20matched%20your%20Profile" in r.headers["location"]
    assert (folder / "resume.html").read_text(encoding="utf-8") == before


def test_the_button_says_what_it_does(client, candidate):
    jid = built_package()
    page = client.get(f"/jobs/{jid}").text
    assert ">Rebuild PDFs</button>" in page and "employers, job titles and dates as your" in page


# ── the matching, piece by piece ──────────────────────────────────────

def test_profile_lines_reads_roles_and_schools():
    lines = refresh.profile_lines((FIXTURES / "profile.md").read_text(encoding="utf-8"))
    assert [(l.fields[:3], l.dates) for l in lines["experience"]] == [
        (["Senior Backend Engineer", "Shipwise Pte Ltd", "Singapore"], "Mar 2023 to present"),
        (["Backend Engineer", "CartNest Commerce", "Singapore"], "Jul 2020 to Feb 2023")]
    assert [(l.fields[:2], l.dates) for l in lines["education"]] == [
        (["National University of Singapore", "B.Comp. in Computer Science"], "2016 to 2020")]
    other = refresh.profile_lines("## Work Experience\n### **Data Analyst** | Foo Corp | 2018 – 2020\n"
                                  "  - Built a thing in 2019\n## Skills\n- Python since 2015\n")
    assert [(l.fields, l.dates) for l in other["experience"]] == [(["Data Analyst", "Foo Corp"], "2018 – 2020")]


@pytest.mark.parametrize("old, profile_dates, new", [
    ("Jan 2019 - Dec 2021", "Jan 2018 to Dec 2021", "Jan 2018 - Dec 2021"),
    ("2019 - 2021", "2019 to 2021", "2019 - 2021"),
    ("Mar 2023 - Present", "March 2023 to present", "Mar 2023 - Present"),
    ("2019 - 2021", "Jun 2018 - 2021", "Jun 2018 - 2021"),  # shaped differently: the Profile's words
    ("Mar 2023 - Present", "Mar 2022 - Jun 2024", "Mar 2022 - Jun 2024"),
])
def test_new_dates_keep_the_resumes_format(old, profile_dates, new):
    assert refresh.new_dates(old, profile_dates) == new


def resume_with(entries, education=()):
    return render.resume_html({"name": "Alex Tan", "links": []},
                              {"experience": [{"company": c, "title": t, "location": "Singapore", "dates": d,
                                               "bullets": ["Did things"]} for c, t, d in entries],
                               "education": [{"school": s, "degree": g, "dates": d} for s, g, d in education]})


def test_a_renamed_title_or_employer_flows_in():
    profile_text = ("## Experience\n- Staff Engineer - Shipwise Pte Ltd - Singapore - 2021 to present\n"
                    "- Engineer - CartNest Holdings - Singapore - 2018 to 2021\n"
                    "## Education\n- National University of Singapore - B.Comp. - 2014 to 2018\n")
    html = resume_with([("Shipwise Pte Ltd", "Senior Engineer", "2021 - Present"),
                        ("CartNest Commerce", "Engineer", "2018 - 2021"),
                        ("Invented Co", "Wizard", "2010 - 2012")],
                       [("National University of Singapore", "B.Comp.", "2014 - 2017")])
    out, changes, unmatched = refresh.refresh_resume(html, {"name": "Alex Tan", "links": []}, profile_text)
    assert "Shipwise Pte Ltd: title Senior Engineer → Staff Engineer" in changes  # matched by employer
    assert "CartNest Commerce: employer CartNest Commerce → CartNest Holdings" in changes  # by title and dates
    assert "National University of Singapore: dates 2014 - 2017 → 2014 - 2018" in changes
    assert unmatched == ["Invented Co"] and "Wizard" in out  # left alone, and named
    assert "Did things" in out and changes == [c for c in changes if "Invented" not in c]


def test_two_roles_at_one_employer_are_told_apart_by_title():
    profile_text = ("## Experience\n- Senior Engineer - Shipwise - SG - 2022 to present\n"
                    "- Engineer - Shipwise - SG - 2018 to 2021\n")
    html = resume_with([("Shipwise", "Senior Engineer", "2022 - Present"), ("Shipwise", "Engineer", "2019 - 2021")])
    _, changes, unmatched = refresh.refresh_resume(html, {"name": "Alex Tan", "links": []}, profile_text)
    assert changes == ["Shipwise: dates 2019 - 2021 → 2018 - 2021"] and not unmatched


def test_contact_details_and_name_are_swapped_in_the_letter():
    old = render.cover_letter_html({"name": "Alex Tan", "email": "a@x.com", "phone": "1"}, "Lumen", "Dev",
                                   {"paragraphs": ["I like Lumen."]})
    new, changes = refresh.refresh_cover_letter(old, {"name": "Alex Tan Wei", "email": "b@x.com", "phone": "1"},
                                                "Lumen", "Dev")
    assert changes == ["your name", "your contact details"]
    assert "Alex Tan Wei" in new and "b@x.com" in new and "a@x.com" not in new and "I like Lumen." in new
    assert new.count("Alex Tan Wei") == 3  # title, letterhead and signature
    assert json.dumps(refresh.refresh_cover_letter(new, {"name": "Alex Tan Wei", "email": "b@x.com", "phone": "1"},
                                                   "Lumen", "Dev")[1]) == "[]"


def _tab_urls(page_html: str) -> dict:
    import re
    return {name: src for src, name in re.findall(r'data-src="([^"]+)" data-name="([^"]+)"', page_html)}


def test_rebuilt_pdf_shows_in_the_preview_not_a_cached_copy(client, candidate):
    """Zech's bug: after Rebuild PDFs, resume.pdf on disk had the new dates but the job page's preview still
    showed the old PDF. The browser's PDF viewer kept its copy of /files/<id>/resume.pdf: the file links now
    carry the file's version and the files are sent with no-store, so a rebuilt PDF is always fetched again."""
    jid = built_package()
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    before = _tab_urls(client.get(f"/jobs/{jid}").text)
    assert before["resume.pdf"].startswith(f"/files/{jid}/resume.pdf?v=")

    r = client.get(before["resume.pdf"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    cache = r.headers["cache-control"]
    assert "no-store" in cache and "no-cache" in cache
    for name in ("cover_letter.pdf", "resume.html"):  # every file the job page links to, not just the resume
        assert "no-store" in client.get(f"/files/{jid}/{name}").headers["cache-control"]

    profile.save_profile(profile.load_profile().replace("CartNest Commerce - Singapore - Jul 2020 to Feb 2023",
                                                        "CartNest Commerce - Singapore - Jul 2019 to Feb 2023"))
    import os
    os.utime(folder / "resume.pdf", ns=(1, 1))  # so the rebuild changes the version even on a coarse clock
    assert client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False).status_code == 303
    after = _tab_urls(client.get(f"/jobs/{jid}").text)
    assert after["resume.pdf"] != before["resume.pdf"]  # a new address: the frame loads the new file
    assert "Jul 2019 - Feb 2023" in pdf_text(folder / "resume.pdf")
    fetched = client.get(after["resume.pdf"]).content
    assert fetched == (folder / "resume.pdf").read_bytes()


# ── "Your Profile changed after these were made" ──────────────────────

NOTICE = "changed after these were made"


def made_an_hour_ago(jid):
    """The package's PDFs as if built an hour ago, so a Profile save now is surely after them."""
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    then = int((time.time() - 3600) * 1e9)
    for name in ("resume.pdf", "cover_letter.pdf"):
        os.utime(folder / name, ns=(then, then))
    return folder


def test_notice_after_a_profile_change_and_gone_after_rebuild(client, candidate):
    """Zech's request: he updated his Profile after a package was built; the job page should say so,
    with Rebuild right there, and stop saying so once he has rebuilt."""
    jid = built_package()
    made_an_hour_ago(jid)
    assert NOTICE not in client.get(f"/jobs/{jid}").text  # nothing saved since

    profile.save_profile(profile.load_profile().replace("CartNest Commerce - Singapore - Jul 2020 to Feb 2023",
                                                        "CartNest Commerce - Singapore - Jul 2019 to Feb 2023"))
    profile.save_contact(profile.load_contact() | {"phone": "+65 8111 2222"})
    page = client.get(f"/jobs/{jid}").text
    assert NOTICE in page and "Rebuild PDFs to use the new version" in page
    assert "no AI, no tokens" in page  # what the button really does: it never re-runs the tailoring
    assert "CartNest Commerce: dates Jul 2020 - Feb 2023 → Jul 2019 - Feb 2023" in page
    assert "your contact details" in page
    notice = page[page.index('id="pdfsStale"'):]
    notice = notice[:notice.index("</div>")]
    assert f'action="/jobs/{jid}/rebuild"' in notice and ">Rebuild PDFs</button>" in notice
    assert page.count(">Rebuild PDFs</button>") == 1  # the notice's button stands in for the usual one
    assert ">Profile changed</span>" in client.get("/").text  # and the Dashboard row says so

    client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    page = client.get(f"/jobs/{jid}").text
    assert NOTICE not in page and ">Rebuild PDFs</button>" in page
    assert ">Profile changed</span>" not in client.get("/").text


def test_no_notice_when_a_save_changed_nothing_rebuild_uses(client, candidate):
    """Saving the Profile page as it was, or changing what Rebuild can't bring in (skills, screening
    answers), is no reason to rebuild."""
    jid = built_package()
    made_an_hour_ago(jid)
    profile.save_profile(profile.load_profile())
    profile.save_contact(profile.load_contact())
    assert NOTICE not in client.get(f"/jobs/{jid}").text
    profile.save_profile(profile.load_profile().replace("## Skills", "## Skills\n- Rust"))
    profile.save_screening(profile.load_screening() | {"notice_period": "2 months"})
    assert NOTICE not in client.get(f"/jobs/{jid}").text
    assert ">Profile changed</span>" not in client.get("/").text


@pytest.mark.parametrize("state", ["Applied", "Rejected", "Not for me", "Interview"])
def test_no_notice_once_the_documents_were_sent(client, candidate, state):
    jid = built_package()
    made_an_hour_ago(jid)
    profile.save_contact(profile.load_contact() | {"phone": "+65 8111 2222"})
    assert NOTICE in client.get(f"/jobs/{jid}").text
    db.update_job(jid, state=state)
    assert NOTICE not in client.get(f"/jobs/{jid}").text
    assert ">Profile changed</span>" not in client.get("/").text


def test_no_notice_without_documents(client, candidate):
    folder = render.make_job_folder("Lumen Analytics", "Backend Engineer")
    (folder / "posting.pdf").write_bytes(b"%PDF-1.4")
    jid = db.add_job(source="demo", url="https://x/none", company="Lumen Analytics", role="Backend Engineer",
                     folder=folder.name, state="Ready")
    nofolder = db.add_job(source="demo", url="https://x/nofolder", company="Lumen", role="Dev", state="Found")
    profile.save_contact(profile.load_contact() | {"phone": "+65 8111 2222"})
    for j in (jid, nofolder):
        r = client.get(f"/jobs/{j}")
        assert r.status_code == 200 and NOTICE not in r.text
    assert refresh.profile_changes(db.get_job(jid), folder) == []


def test_no_notice_when_the_profile_is_older_than_the_pdfs(client, candidate):
    """A fact edited by hand in resume.html differs from the Profile, but the Profile wasn't saved after
    the PDFs were made: no notice."""
    jid = built_package()
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    html = (folder / "resume.html").read_text(encoding="utf-8")
    (folder / "resume.html").write_text(html.replace("Jul 2020 - Feb 2023", "Jul 2018 - Feb 2023"), encoding="utf-8")
    then = int((time.time() - 3600) * 1e9)
    for p in (paths.profile_path(), profile.contact_path()):
        os.utime(p, ns=(then, then))
    assert refresh.profile_changes(db.get_job(jid), folder) == []
    assert NOTICE not in client.get(f"/jobs/{jid}").text


def test_an_unreadable_document_costs_the_notice_not_the_page(client, candidate):
    jid = built_package()
    folder = made_an_hour_ago(jid)
    profile.save_contact(profile.load_contact() | {"phone": "+65 8111 2222"})
    (folder / "resume.html").write_bytes(bytes([0xff, 0xfe, 0x80]) + b" not utf-8")
    assert client.get("/").status_code == 200
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 200 and NOTICE not in r.text


# ── the cover letter's date ───────────────────────────────────────────
# Zech, 2026-10-16: a letter built on Monday and sent on Friday said Monday.

MADE, TODAY = date(2026, 10, 13), date(2026, 10, 16)
DATED = "The cover letter is dated"


def monday(html: str) -> str:
    """A letter built today, as if built on Monday 13 October 2026."""
    return re.sub(r'(<div class="date">).*?(</div>)', r"\g<1>13 October 2026\g<2>", html, count=1)


def dated_on_monday(jid, monkeypatch):
    """The package's cover letter as if built on Monday 13 October 2026, and today Friday the 16th."""
    from jobhunt.browser import render_pdfs
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    html = monday((folder / "cover_letter.html").read_text(encoding="utf-8"))
    (folder / "cover_letter.html").write_text(html, encoding="utf-8")
    render_pdfs([(html, folder / "cover_letter.pdf")])
    monkeypatch.setattr(refresh, "today", lambda: TODAY)
    return folder


def test_the_letters_date_reads_back_and_redating_changes_nothing_else():
    html = monday(render.cover_letter_html({"name": "Alex Tan"}, "Lumen", "Dev", {"paragraphs": ["I like Lumen."]}))
    assert refresh.letter_date(html) == MADE
    new = refresh.redate(html, TODAY)
    assert refresh.letter_date(new) == TODAY and new == html.replace("13 October 2026", "16 October 2026")
    assert refresh.redate(new, TODAY) == new
    assert refresh.redate(html, date(2026, 10, 1)) == html  # dated later than that (you set it ahead): kept
    edited = html.replace("13 October 2026", "Monday")
    assert refresh.letter_date(edited) is None and refresh.redate(edited, TODAY) == edited  # not a date: kept
    assert refresh.letter_date("<p>no date line</p>") is None
    assert refresh.redate("<p>no date line</p>", TODAY) == "<p>no date line</p>"


def test_job_page_says_the_letter_is_dated_before_today_and_rebuild_dates_it_today(client, candidate, monkeypatch):
    jid = built_package()
    folder = dated_on_monday(jid, monkeypatch)
    letter = (folder / "cover_letter.html").read_text(encoding="utf-8")
    assert "13 October 2026" in pdf_text(folder / "cover_letter.pdf")

    page = client.get(f"/jobs/{jid}").text
    assert "The cover letter is dated 13 October.</b> Rebuild PDFs to date it today (no AI, no tokens)." in page
    assert "JobHunt does this by itself when it applies." in page
    assert NOTICE not in page  # the Profile didn't change: only the date is old
    notice = page[page.index('id="pdfsStale"'):]
    notice = notice[:notice.index("</div>")]
    assert f'action="/jobs/{jid}/rebuild"' in notice and page.count(">Rebuild PDFs</button>") == 1
    assert "Profile changed" not in client.get("/").text  # Approve & apply re-dates it: no Dashboard pill

    r = client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    assert "now%20dated%2016%20October%202026" in r.headers["location"]
    assert "16 October 2026" in pdf_text(folder / "cover_letter.pdf")
    new = (folder / "cover_letter.html").read_text(encoding="utf-8")
    assert new == letter.replace("13 October 2026", "16 October 2026")  # the tailored text is untouched
    assert DATED not in client.get(f"/jobs/{jid}").text


def test_a_letter_from_last_year_says_the_year(client, candidate, monkeypatch):
    jid = built_package()
    dated_on_monday(jid, monkeypatch)
    monkeypatch.setattr(refresh, "today", lambda: date(2027, 1, 4))
    assert "The cover letter is dated 13 October 2026.</b>" in client.get(f"/jobs/{jid}").text


def test_both_notices_together_with_one_button(client, candidate, monkeypatch):
    jid = built_package()
    dated_on_monday(jid, monkeypatch)
    made_an_hour_ago(jid)
    profile.save_contact(profile.load_contact() | {"phone": "+65 8111 2222"})
    page = client.get(f"/jobs/{jid}").text
    assert "The cover letter is dated 13 October.</b> Rebuild PDFs to date it today." in page
    assert NOTICE in page and "your contact details" in page
    assert page.count(">Rebuild PDFs</button>") == 1
    assert ">Profile changed</span>" in client.get("/").text


@pytest.mark.parametrize("state", ["Applied", "Rejected", "Not for me", "Applying"])
def test_no_date_notice_once_the_letter_was_sent(client, candidate, monkeypatch, state):
    jid = built_package()
    dated_on_monday(jid, monkeypatch)
    assert DATED in client.get(f"/jobs/{jid}").text
    db.update_job(jid, state=state)
    assert DATED not in client.get(f"/jobs/{jid}").text


def test_no_date_notice_on_the_day_it_was_made_or_when_the_date_line_was_edited(client, candidate, monkeypatch):
    jid = built_package()
    assert DATED not in client.get(f"/jobs/{jid}").text  # made today
    folder = dated_on_monday(jid, monkeypatch)
    html = (folder / "cover_letter.html").read_text(encoding="utf-8")
    (folder / "cover_letter.html").write_text(html.replace("13 October 2026", "Monday"), encoding="utf-8")
    assert refresh.stale_letter_date(db.get_job(jid), folder) is None
    assert DATED not in client.get(f"/jobs/{jid}").text
    client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    assert "Monday" in (folder / "cover_letter.html").read_text(encoding="utf-8")  # Rebuild keeps your edit


def test_rebuild_keeps_the_date_on_a_letter_already_sent(client, candidate, monkeypatch):
    jid = built_package()
    folder = dated_on_monday(jid, monkeypatch)
    db.update_job(jid, state="Applied")
    r = client.post(f"/jobs/{jid}/rebuild", headers=SAME_ORIGIN, follow_redirects=False)
    assert "now%20dated" not in r.headers["location"]
    assert "13 October 2026" in pdf_text(folder / "cover_letter.pdf")


def test_singpass_jobs_are_not_promised_an_automatic_date(client, candidate, monkeypatch):
    jid = built_package()
    dated_on_monday(jid, monkeypatch)
    db._exec("UPDATE jobs SET source = 'mycareersfuture' WHERE id = ?", (jid,))
    page = client.get(f"/jobs/{jid}").text
    assert DATED in page and "JobHunt does this by itself" not in page


# ── Approve & apply dates the letter today before the agent can attach it ──

class _Agent:
    """Stands in for the apply agent: notes the cover letter it would attach."""
    seen = []

    def __init__(self, folder, **kw):
        self.folder = folder

    def run(self, url):
        from jobhunt.applyagent import ApplyResult
        _Agent.seen.append(((self.folder / "cover_letter.html").read_text(encoding="utf-8"),
                            pdf_text(self.folder / "cover_letter.pdf")))
        return ApplyResult("submitted", "sent", url)


def _apply(jid, monkeypatch, browser=None):
    from jobhunt import hunt, settings
    from jobhunt.browser import BrowserSession
    settings.update({"apply": {"show_browser": False}})
    monkeypatch.setattr(hunt, "ApplyAgent", _Agent)
    monkeypatch.setattr(hunt.Task, "get_llm", lambda self: None)
    _Agent.seen = []
    task = hunt.Task("apply", {}, threading.Event())
    browser = browser or BrowserSession(headless=True)
    try:
        state = hunt.apply_job(task, browser, jid)
    finally:
        browser.close()
    return state, [(e["level"], e["message"]) for e in db.events_since(0) if e["run_id"] == task.run_id]


def test_approve_and_apply_dates_the_letter_today_before_the_agent_attaches_it(data_dir, candidate, monkeypatch):
    jid = built_package()
    folder = dated_on_monday(jid, monkeypatch)
    letter = (folder / "cover_letter.html").read_text(encoding="utf-8")
    state, log = _apply(jid, monkeypatch)
    assert state == "Applied"
    html, text = _Agent.seen[0]
    assert html == letter.replace("13 October 2026", "16 October 2026")  # only the date changed
    assert "16 October 2026" in text and "13 October 2026" not in text  # and the PDF it attaches says so
    assert ("info", "Dated the cover letter today (16 October 2026); it said 13 October 2026.") in log
    assert not (folder / "cover_letter.new.pdf").exists()


def test_a_letter_already_dated_today_is_left_alone(data_dir, candidate, monkeypatch):
    jid = built_package()
    folder = paths.jobs_dir() / db.get_job(jid)["folder"]
    before = (folder / "cover_letter.pdf").read_bytes()
    state, log = _apply(jid, monkeypatch)
    assert state == "Applied" and (folder / "cover_letter.pdf").read_bytes() == before
    assert not any("cover letter" in m for _, m in log)


def test_a_failed_redate_never_blocks_the_apply(data_dir, candidate, monkeypatch):
    from jobhunt.browser import BrowserSession
    jid = built_package()
    folder = dated_on_monday(jid, monkeypatch)
    letter, pdf = (folder / "cover_letter.html").read_text(encoding="utf-8"), (folder / "cover_letter.pdf").read_bytes()

    class Broken(BrowserSession):
        def render_pdf(self, html, out_path):
            out_path.write_bytes(b"half a pdf")
            raise RuntimeError("chromium crashed")
    state, log = _apply(jid, monkeypatch, Broken(headless=True))
    assert state == "Applied"
    assert _Agent.seen[0][0] == letter  # it went with Monday's letter, whole
    assert (folder / "cover_letter.pdf").read_bytes() == pdf and not (folder / "cover_letter.new.pdf").exists()
    assert ("warn", "Couldn't date the cover letter today (RuntimeError), so it goes with the date it has.") in log
