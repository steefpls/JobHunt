"""Rebuild PDFs takes the facts from your Profile as it is now, and keeps the wording you edited."""
import json

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
