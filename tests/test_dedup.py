"""The same job found on two job sites is looked at once: titles and company names read the same way
on every board, jobs already judged count as well as tracked ones, and pay tells same-title ads apart."""
from jobhunt import db


def test_titles_read_the_same_on_every_board():
    assert db.title_key("Sr. Software Engineer (Singapore) - Urgent Hiring") == db.title_key("Senior Software Engineer")
    assert db.title_key("Software Engineer, Backend") == db.title_key("Backend Software Engineer")
    assert db.title_key("Software Engineer (Backend)") != db.title_key("Software Engineer (Frontend)")
    assert db.title_key("Software Engineer - Payments") != db.title_key("Software Engineer")


def test_a_job_tracked_from_one_board_is_known_on_another(data_dir):
    db.add_job(source="mycareersfuture", url="https://mcf/1", company="ALTAX PTE. LTD.", role="Software Engineer",
               salary="$4,000 to $6,000 Monthly")
    dup = db.duplicate_of("https://indeed/1", "Altax Pte Ltd", "Software Engineer (Singapore)", "$4,000 - $6,000 a month")
    assert dup and dup["source"] == "mycareersfuture"
    assert db.is_duplicate("https://linkedin/1", "ALTAX", "Software Engineer")  # no pay shown: same job


def test_two_ads_with_one_title_and_different_pay_are_two_jobs(data_dir):
    db.add_job(source="mycareersfuture", url="https://mcf/1", company="ALTAX PTE. LTD.", role="Software Engineer",
               salary="$4,000 to $6,000 Monthly")
    assert not db.is_duplicate("https://indeed/2", "ALTAX PTE. LTD.", "Software Engineer", "$7,000 - $10,000 a month")
    assert db.is_duplicate("https://indeed/3", "ALTAX PTE. LTD.", "Software Engineer", "$5,000 - $8,000 a month")  # overlaps


def test_a_job_judged_not_a_fit_on_one_board_isnt_judged_again_on_another(data_dir):
    db.mark_seen("https://jobstreet/9", "jobstreet", "Lumen Analytics", "Data Engineer", "skip", "needs 8 years",
                 salary="$6,000 - $8,000 per month")
    assert db.duplicate_of("https://indeed/9", "LUMEN ANALYTICS PTE. LTD.", "Data Engineer", "")["source"] == "jobstreet"
    assert not db.is_duplicate("https://indeed/9", "Lumen Analytics", "Data Analyst")


def test_a_posting_that_failed_to_load_or_its_own_earlier_try_doesnt_count(data_dir):
    db.mark_seen("https://jobstreet/5", "jobstreet", "Lumen", "Data Engineer", "error", "timeout")
    assert not db.is_duplicate("https://indeed/5", "Lumen", "Data Engineer")
    db.mark_seen("https://indeed/6", "indeed", "Lumen", "ML Engineer", "skip", "not a fit")
    assert not db.is_duplicate("https://indeed/6", "Lumen", "ML Engineer")  # was_seen handles a URL's own record


MCF_AD = "https://www.mycareersfuture.gov.sg/job/it/software-engineer-altax-9e368c9b"


def test_an_indeed_job_whose_apply_button_leads_to_a_tracked_mycareersfuture_job_is_that_job(data_dir):
    # rejected on MyCareersFuture under the agency's name; Indeed shows the client's
    jid = db.add_job(source="mycareersfuture", url=MCF_AD, company="Talent Agency Pte Ltd", role="Software Engineer",
                     state="Not for me")
    dup = db.duplicate_of("https://sg.indeed.com/viewjob?jk=1", "ALTAX", "Software Developer", "", MCF_AD)
    assert dup and dup["source"] == "mycareersfuture" and db.get_job(jid)["state"] == "Not for me"
    # a search link names no ad
    assert not db.is_duplicate("https://sg.indeed.com/viewjob?jk=2", "ALTAX", "Software Developer", "",
                               "https://www.mycareersfuture.gov.sg/search?search=Software%20Developer")


def test_a_mycareersfuture_job_already_judged_through_indeed_isnt_judged_again(data_dir):
    db.mark_seen("https://sg.indeed.com/viewjob?jk=1", "indeed", "ALTAX", "Software Developer", "skip", "not a fit",
                 apply_url=MCF_AD)
    assert db.duplicate_of(MCF_AD, "Talent Agency Pte Ltd", "Software Engineer")["source"] == "indeed"


def test_an_indeed_job_applied_through_its_mycareersfuture_link_is_known_on_mycareersfuture(data_dir):
    db.add_job(source="indeed", url="https://sg.indeed.com/viewjob?jk=1", company="ALTAX", role="Dev",
               apply_url=MCF_AD, state="Applied")
    assert db.is_duplicate(MCF_AD, "Talent Agency", "Software Engineer")
