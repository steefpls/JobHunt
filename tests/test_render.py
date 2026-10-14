"""Job folders, model-output clean-up, the honesty check and the match report."""
from jobhunt import render

from conftest import FIXTURES, TAILOR_REPLY


def test_folder_names_are_clean_and_unique(data_dir):
    a = render.make_job_folder("Lumen Analytics Pte. Ltd.", "Backend Engineer (Python/Go) *Urgent*")
    b = render.make_job_folder("Lumen Analytics Pte. Ltd.", "Backend Engineer (Python/Go) *Urgent*")
    assert a.name.endswith("_Lumen-Analytics-Pte-Ltd_Backend-Engineer-Python-Go-Urgent")
    assert b.name == a.name + "_2"
    assert a.parent == data_dir / "jobs"


def test_normalise_tailor_fixes_common_slips():
    data = {"resume": {}, "cover_letter": {}, "requirements": [{"requirement": "x"}], "gaps": ["k8s"], "score": 82}
    out = render.normalise_tailor(data)
    assert out["match_report"]["score"] == 8.2
    assert out["match_report"]["gaps"] == ["k8s"]
    assert "requirements" not in out
    assert render.normalise_tailor({"match_report": {"score": "great"}})["match_report"]["score"] == "?"


def test_honesty_warning_flags_invented_employer():
    profile = (FIXTURES / "profile.md").read_text(encoding="utf-8")
    assert render.honesty_warnings(profile, TAILOR_REPLY["resume"]) == []
    fake = {"experience": [{"company": "Google", "title": "Staff Engineer"}], "education": [{"school": "MIT"}]}
    warnings = render.honesty_warnings(profile, fake)
    assert len(warnings) == 2 and "Google" in warnings[0] and "MIT" in warnings[1]


def test_resume_uses_profile_contact_not_model_output():
    html = render.resume_html({"name": "Alex Tan", "email": "a@example.com", "phone": "", "location": "SG", "links": []},
                              TAILOR_REPLY["resume"])
    assert "Alex Tan" in html and "a@example.com" in html
    assert "Shipwise Pte Ltd" in html
    assert html.index("Shipwise") < html.index("National University")  # section_order respected


def test_resume_escapes_html_from_the_model():
    html = render.resume_html({"name": "A", "links": []}, {"summary": "<script>alert(1)</script>"})
    assert "<script>alert(1)" not in html


def test_match_report_markdown():
    md = render.match_report_md("Lumen", "Backend", TAILOR_REPLY["match_report"], ["Employer 'X' is not in your profile"],
                                ["good fit"])
    assert md.startswith("# Match report - Lumen")
    assert "> **Check before sending:**" in md
    assert "3+ years Python" in md and "Kubernetes administration" in md
    assert "Tailoring score: 8/10" in md


LONG_URL = ("https://sg.jobstreet.com/job/87654321?type=standard&ref=search-standalone&origin=jobTitle"
            "&searchRequestToken=0a1b2c3d-4e5f-6789-abcd-ef0123456789&tracking=SG-JOB-SEARCH-RESULTS-PAGE-LINK-"
            + "x" * 120)


def test_link_label_is_short_and_one_line():
    assert render.link_label("https://www.linkedin.com/jobs/view/123/") == "linkedin.com/jobs/view/123"
    assert render.link_label(LONG_URL) == "sg.jobstreet.com/job/87654321"
    label = render.link_label("https://careers.example.com/" + "deep/" * 30)
    assert label.startswith("careers.example.com/deep/") and label.endswith("…") and len(label) == 48
    assert render.web_url("javascript:alert(1)") == "#"


def test_posting_pdf_link_opens_the_whole_url(tmp_path):
    from pypdf import PdfReader
    from jobhunt.browser import render_pdfs
    from jobhunt.extract import Posting
    posting = Posting(source="jobstreet", url=LONG_URL, title="Backend Engineer", company="Lumen", location="SG",
                      text="Build APIs.")
    html = render.posting_html(posting)
    assert "Open the original posting: sg.jobstreet.com/job/87654321" in html
    out = tmp_path / "posting.pdf"
    render_pdfs([(html, out)])
    uris = [a.get_object()["/A"]["/URI"] for page in PdfReader(str(out)).pages
            for a in page.get("/Annots") or [] if a.get_object().get("/Subtype") == "/Link"]
    assert uris == [LONG_URL]  # one link, and it holds the full URL


def test_posting_pdf_never_links_a_non_web_url():
    from jobhunt.extract import Posting
    html = render.posting_html(Posting(source="demo", url="javascript:alert(1)", title="t", company="c",
                                       location="", text="x"))
    assert "javascript:" not in html and "<a " not in html
