"""The company summary's sources beyond the posting (Zech, 2026-10-15: "It doesn't do much of a summary", "a
literal summary of the job posting and not so much the company"): Singapore's company registry, one web search,
Wikipedia; and a recruiter posting shown as the agency that posted it and the client it hires for."""
import json
import socket
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from jobhunt import company, company_sources as more, db, sites
from jobhunt.web.app import create_app

from conftest import FIXTURES
from test_company import SAME_ORIGIN, _Browser, _PacedContext, _Task

PIVOT = json.loads((FIXTURES / "acra_pivot_live.json").read_text(encoding="utf-8"))
WIKI_AMAT = (FIXTURES / "wikipedia_applied_materials_live.json").read_text(encoding="utf-8")
PIVOT_POSTING = ("Our client, a leading game developer with studios across Asia, is hiring a Gameplay Programmer "
                 "for its Kent Ridge office.\nBuild combat systems in Unity.\n"
                 "PIVOT SEARCH PTE. LTD. | EA Licence No. 21C0718")


class FakeWeb:
    """Answers http_get by host: {host: callable(url, params) -> (status, body)}; every call is noted."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, url, params=None, headers=None, timeout=20):
        host = urlparse(url).hostname
        self.calls.append((host, dict(params or {})))
        if host not in self.answers:
            return 404, "not here"
        return self.answers[host](url, params or {})


def acra(records_by_filter=None, records_by_q=None, status=200):
    def answer(url, params):
        if status != 200:
            return status, '{"code":24,"name":"TOO_MANY_REQUESTS"}'
        if "filters" in params:
            wanted = json.loads(params["filters"])["entity_name"]
            recs = [r for r in (records_by_filter or []) if r["entity_name"] in wanted]
        else:
            recs = records_by_q or []
        return 200, json.dumps({"success": True, "result": {"records": recs}})
    return answer


def challenged(url, params):
    return 202, "<html><body><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too.</div></body></html>"


def ddg(*links):
    rows = "".join(f'<a class="result__a" href="//duckduckgo.com/l/?uddg={u.replace(":", "%3A").replace("/", "%2F")}'
                   f'&rut=x">{t}</a>' for u, t in links)
    return lambda url, params: (200, f"<html><body>{rows}</body></html>")


def no_wiki(url, params):
    return 200, json.dumps({"batchcomplete": True})


def _job(**kw):
    return {"id": 1, "source": "indeed", "url": "https://sg.indeed.com/viewjob?jk=0123456789abcdee",
            "company": "PIVOT SEARCH PTE. LTD.", "role": "Gameplay Programmer", "location": "Kent Ridge, Singapore",
            "posting_text": PIVOT_POSTING, "salary": "", **kw}


# ── ACRA, the company registry ────────────────────────────────────────

def test_registry_names_are_matched_in_all_their_spellings():
    assert more.registry_base("Applied Materials South East Asia Pte Ltd") == "APPLIED MATERIALS SOUTH EAST ASIA"
    assert more.registry_base("PIVOT SEARCH PTE. LTD.") == "PIVOT SEARCH"
    assert more.registry_base("Acme (Pte.) Ltd.") == "ACME" and more.registry_base("Acme Private Limited") == "ACME"
    assert more.name_key("Tan & Sons Pte Ltd") == more.name_key("TAN AND SONS PRIVATE LIMITED") == "TANANDSONS"
    assert more.name_key("A.B.C. PTE.LTD.") == more.name_key("ABC Pte Ltd")
    v = more.name_variants("Pivot Search Pte Ltd")
    assert {"PIVOT SEARCH PTE. LTD.", "PIVOT SEARCH PTE LTD", "PIVOT SEARCH PRIVATE LIMITED", "PIVOT SEARCH"} <= set(v)
    assert more.dataset_for("Pivot Search") == more.ACRA_DATASETS["P"]
    assert more.dataset_for("3M Technologies") == more.ACRA_DATASETS["Others"]
    assert len(more.ACRA_DATASETS) == 27


def test_registry_record_as_seen_live(monkeypatch):
    """PIVOT SEARCH PTE. LTD. in ACRA's open data on data.gov.sg (live, 2026-10-15): one exact lookup, no key."""
    web = FakeWeb(**{"data.gov.sg": acra(PIVOT["result"]["records"])})
    monkeypatch.setattr(more, "http_get", web)
    reg = more.acra_lookup("PIVOT SEARCH PTE. LTD.")
    assert reg.uen == "202125857G" and reg.status == "Live Company" and reg.postal_code == "269204"
    assert reg.text.split("\n")[0] == "PIVOT SEARCH PTE. LTD., UEN 202125857G, Live Company, registered on 2021-07-26"
    assert "Registered office address: 66 Duchess Avenue, #04-02, Duchess Crest, Singapore 269204" in reg.text
    # the record gives only the code: its title comes from the SSIC list
    assert reg.industry_line.startswith("Main registered business activity: Employment agencies") and "(SSIC 78104)" in reg.industry_line
    assert "Human resource consultancy services (SSIC 70204)" in reg.text
    assert web.calls == [("data.gov.sg", web.calls[0][1])] and web.calls[0][1]["resource_id"] == more.ACRA_DATASETS["P"]


def test_no_confident_match_says_so(monkeypatch):
    rec = dict(PIVOT["result"]["records"][0])
    twin = rec | {"uen": "201900001A"}
    web = FakeWeb(**{"data.gov.sg": acra([rec, twin])})
    monkeypatch.setattr(more, "http_get", web)
    with pytest.raises(more.Unavailable, match="2 registered entities share this name"):
        more.acra_lookup("Pivot Search Pte Ltd")
    # nothing under that exact name: looked up once more by its words, and a near name is not taken
    web = FakeWeb(**{"data.gov.sg": acra([], [rec | {"entity_name": "PIVOT SEARCH ASIA PTE. LTD."}])})
    monkeypatch.setattr(more, "http_get", web)
    with pytest.raises(more.Unavailable, match="no company registered under this name"):
        more.acra_lookup("Pivot Search Pte Ltd")
    assert len(web.calls) == 2 and "q" in web.calls[1][1]
    # the same name spelt differently is found by its words
    web = FakeWeb(**{"data.gov.sg": acra([], [rec | {"entity_name": "PIVOT SEARCH PTE.LTD."}])})
    monkeypatch.setattr(more, "http_get", web)
    assert more.acra_lookup("Pivot Search Pte Ltd").uen == "202125857G"
    # a struck-off namesake doesn't make a live company ambiguous
    web = FakeWeb(**{"data.gov.sg": acra([rec, twin | {"entity_status_description": "Struck Off"}])})
    monkeypatch.setattr(more, "http_get", web)
    assert more.acra_lookup("Pivot Search Pte Ltd").uen == "202125857G"


def test_data_gov_sg_asking_to_slow_down_is_waited_out_once(monkeypatch):
    answers = iter([(429, "{}"), (429, "{}")])
    monkeypatch.setattr(more, "http_get", lambda url, params=None, headers=None, timeout=20: next(answers))
    with pytest.raises(more.Unavailable, match="too many requests"):
        more.acra_lookup("Pivot Search Pte Ltd")
    answers = iter([(429, "{}"), (200, json.dumps(PIVOT))])
    monkeypatch.setattr(more, "http_get", lambda url, params=None, headers=None, timeout=20: next(answers))
    assert more.acra_lookup("Pivot Search Pte Ltd").uen == "202125857G"


# ── web search ────────────────────────────────────────────────────────

def test_search_results_are_unwrapped_and_must_carry_the_name(monkeypatch):
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    web = FakeWeb(**{"data.gov.sg": acra(PIVOT["result"]["records"]), "en.wikipedia.org": no_wiki,
                     "html.duckduckgo.com": ddg(("https://sg.linkedin.com/company/pivot-search", "LinkedIn"),
                                                ("https://www.pivotsearch.com.sg/about", "About Pivot Search"))})
    monkeypatch.setattr(more, "http_get", web)
    # the site doesn't say "Singapore", but it gives the registered postal code: that's them
    about = ("<html><title>Pivot Search</title><body><main>Pivot Search places tech talent across Asia."
             "<a href='/contact'>Contact</a></main><footer>66 Duchess Ave, 269204</footer></body></html>")
    browser = _Browser({"https://www.pivotsearch.com.sg/about": about,
                        "https://www.pivotsearch.com.sg/contact": "<html><main>hello@pivotsearch.com.sg</main></html>"})
    srcs, skipped = company.gather(_PacedContext(browser), _job(), None)
    assert [s.kind for s in srcs] == ["posting", "registry", "website", "website"]
    assert srcs[2].url == "https://www.pivotsearch.com.sg/about" and "web search" in srcs[2].label
    assert ("html.duckduckgo.com", {"q": '"Pivot Search" Singapore', "kl": "sg-en"}) in web.calls
    assert not any("linkedin" in u for u in browser.isolated)


def test_a_search_robot_check_is_skipped_for_the_rest_of_the_hunt(monkeypatch):
    web = FakeWeb(**{"html.duckduckgo.com": challenged})
    monkeypatch.setattr(more, "http_get", web)
    ctx = _PacedContext(_Browser({}))
    _, skipped = company.gather(ctx, _job(), None)
    assert "Web search: the search showed a robot check" in skipped
    _, skipped = company.gather(ctx, _job(company="Acme Robotics Pte Ltd"), None)
    assert [h for h, _ in web.calls].count("html.duckduckgo.com") == 1  # never asked again
    assert any("robot check earlier" in s for s in skipped)


def test_a_practice_board_job_asks_nothing_of_the_internet(monkeypatch):
    web = FakeWeb()
    monkeypatch.setattr(more, "http_get", web)
    company.gather(_PacedContext(_Browser({})), _job(source="demo", url="http://127.0.0.1:9/demo/job/1"), {},
                   practice_base="http://127.0.0.1:9/demo")
    assert web.calls == []


# ── Wikipedia ─────────────────────────────────────────────────────────

def test_wikipedia_names_must_match():
    assert more.wiki_match("Applied Materials", "Applied Materials South East Asia Pte Ltd") == "group"
    assert more.wiki_match("Applied Materials", "APPLIED MATERIALS INC") == "same"
    assert more.wiki_match("Shopee", "Shopee Singapore Private Limited") == "group"
    assert more.wiki_match("Semitool", "Applied Materials South East Asia Pte Ltd") == ""
    assert more.wiki_match("Summit Engineering (company)", "Summit Engineering Pte Ltd") == "same"
    assert more.wiki_match("Summit", "Summit Engineering Pte Ltd") == ""  # 'engineering' isn't a region


def test_a_regional_subsidiary_reads_its_groups_article_as_seen_live(monkeypatch):
    claims = {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.appliedmaterials.com/"}}}]}}
    web = FakeWeb(**{"en.wikipedia.org": lambda u, p: (200, WIKI_AMAT),
                     "www.wikidata.org": lambda u, p: (200, json.dumps(claims))})
    monkeypatch.setattr(more, "http_get", web)
    w = more.wikipedia("Applied Materials South East Asia Pte Ltd", [], "Applied Materials is hiring an R&D engineer.")
    assert w.title == "Applied Materials" and w.how == "group" and w.website == "https://www.appliedmaterials.com/"
    assert "headquartered in Santa Clara, California" in w.text
    assert web.calls[0][1]["gsrsearch"] == "Applied Materials"


def test_a_same_named_article_elsewhere_is_not_taken_for_a_singapore_firm(monkeypatch):
    page = {"query": {"pages": [{"title": "Summit Engineering", "index": 1, "fullurl": "https://en.wikipedia.org/wiki/S",
                                 "extract": "Summit Engineering is an American company that builds bridges in Ohio.",
                                 "pageprops": {"wikibase_item": "Q1"}}]}}
    web = FakeWeb(**{"en.wikipedia.org": lambda u, p: (200, json.dumps(page)),
                     "www.wikidata.org": lambda u, p: (200, '{"claims": {}}')})
    monkeypatch.setattr(more, "http_get", web)
    with pytest.raises(more.Unavailable, match="may be a different company"):
        more.wikipedia("Summit Engineering Pte Ltd", [], "Summit Engineering is hiring a site engineer.")
    page["query"]["pages"][0]["extract"] += " Its Asian office is in Singapore."
    assert more.wikipedia("Summit Engineering Pte Ltd", [], "").how == "same company"


# ── a summary about the company, honest about recruiters ─────────────

def _gathered(monkeypatch):
    web = FakeWeb(**{"data.gov.sg": acra(PIVOT["result"]["records"]), "html.duckduckgo.com": challenged,
                     "en.wikipedia.org": no_wiki})
    monkeypatch.setattr(more, "http_get", web)
    return company.gather(_PacedContext(_Browser({})), _job(), None)


def test_a_recruiter_posting_is_about_the_agency_and_keeps_the_client_apart(monkeypatch):
    """Seen 2026-10-15: the client's description (a game developer) showed under 'About PIVOT SEARCH PTE. LTD.'."""
    srcs, _ = _gathered(monkeypatch)
    assert [s.kind for s in srcs] == ["posting", "registry"]
    reply = {
        # the AI describing the client as the agency, from the posting: never shown as the agency's
        "what_they_do": {"text": "A leading game developer with studios across Asia.", "source": "S1",
                         "quote": "a leading game developer with studios across Asia"},
        "industry": {"text": "Games", "source": "S1", "quote": "a leading game developer"},
        "job_location": {"text": "not found", "source": "", "quote": ""},
        "agency": True,
        "client": {"text": "A leading game developer with studios across Asia.", "short": "Game developer",
                   "source": "S1", "quote": "a leading game developer with studios across Asia"},
    }
    out = company.verify(reply, srcs, job=_job())
    f = out["fields"]
    assert out["agency"] is True
    assert f["what_they_do"] is None  # not the client's words as the agency's
    assert f["industry"]["text"].startswith("Employment agencies") and f["industry"]["source"] == "S2"  # the registry
    assert f["registered"]["text"] == "PIVOT SEARCH PTE. LTD., UEN 202125857G, Live Company, registered on 2021-07-26"
    assert f["job_location"] == {"text": "Kent Ridge, Singapore", "source": "S1", "quote": "Location: Kent Ridge, Singapore",
                                 "board_field": True}
    assert out["client"]["text"].startswith("A leading game developer") and out["client"]["short"] == "Game developer"
    assert company.one_line({"status": "ok", **out}) == "Recruiter posting · for: Game developer · Kent Ridge, Singapore"
    md = company.summary_md({"status": "ok", "company": "PIVOT SEARCH PTE. LTD.", **out})
    assert md.startswith("# Posted by: PIVOT SEARCH PTE. LTD. (a recruitment agency)")
    assert "## Hiring for: an unnamed client" in md and "game developer" not in md.split("## Hiring for")[0]


def test_what_they_do_from_the_posting_is_labelled_so():
    srcs = [company.Source("S1", "The job posting", "u", "Title: Engineer\nCompany: Acme\nLocation: Tuas, Singapore\n\n"
                           "Acme makes industrial pumps for ships.", "posting")]
    out = company.verify({"what_they_do": {"text": "Makes industrial pumps for ships.", "source": "S1",
                                           "quote": "Acme makes industrial pumps for ships"}},
                         srcs, job={"location": "Tuas, Singapore"})
    assert out["fields"]["what_they_do"]["from_posting"] is True
    assert out["fields"]["job_location"]["text"] == "Tuas, Singapore"
    assert "(from the job posting only)" in company.summary_md({"status": "ok", "company": "Acme", **out})


def test_where_the_job_is_comes_from_the_boards_location_field():
    """Seen on JobStreet: 'Where the job is' said Not found though the page header said Kent Ridge."""
    srcs = [company.Source("S1", "The job posting", "u", company.posting_source_text(
        {"role": "Engineer", "company": "Acme", "location": "Kent Ridge, Singapore", "posting_text": "Build things."}),
        "posting")]
    out = company.verify({"job_location": {"text": "not found", "source": "", "quote": ""}}, srcs,
                         job={"location": "Kent Ridge, Singapore"})
    assert out["fields"]["job_location"]["text"] == "Kent Ridge, Singapore"
    # a bare country on the board, and a checked street address from another page: the address stays
    srcs.append(company.Source("S2", "board", "u", "Workplace address: 1 Fusionopolis Way, Singapore 138632", "board"))
    out = company.verify({"job_location": {"text": "1 Fusionopolis Way, Singapore 138632", "source": "S2",
                                           "quote": "1 Fusionopolis Way, Singapore 138632"}},
                         [company.Source("S1", "The job posting", "u", "Location: Singapore\n\nWork.", "posting"), srcs[1]],
                         job={"location": "Singapore"})
    assert out["fields"]["job_location"]["text"] == "1 Fusionopolis Way, Singapore 138632"


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_the_job_page_shows_posted_by_and_hiring_for(client, monkeypatch):
    srcs, _ = _gathered(monkeypatch)
    jid = db.add_job(source="indeed", url=_job()["url"], company="PIVOT SEARCH PTE. LTD.", role="Gameplay Programmer",
                     location="Kent Ridge, Singapore", posting_text=PIVOT_POSTING, state="Ready")
    reply = {"agency": True, "client": {"text": "A leading game developer with studios across Asia.",
                                        "short": "Game developer", "source": "S1",
                                        "quote": "a leading game developer with studios across Asia"}}
    monkeypatch.setattr(company, "gather", lambda ctx, job, info=None, practice_base="": (srcs, ["Web search: robot"]))
    result = company.summarise(_Task(reply), _PacedContext(_Browser({})), jid)
    assert result["status"] == "ok"
    page = client.get(f"/jobs/{jid}").text
    assert "Posted by: PIVOT SEARCH PTE. LTD." in page and "About PIVOT SEARCH" not in page
    assert "Hiring for: unnamed client" in page and "From the job posting" in page
    head, hiring = page.split("Hiring for:", 1)
    assert "game developer" not in head.split('id="company"', 1)[1]  # never under the agency
    assert "Employment agencies" in head and "UEN 202125857G" in head
    assert "Kent Ridge, Singapore" in hiring.split("Sources")[0]
    assert "Singapore&#39;s company registry (ACRA)" in page or "Singapore's company registry (ACRA)" in page
    assert "Recruiter posting · for: Game developer" in client.get("/").text


def test_a_company_summary_from_the_posting_alone_says_so_on_the_job_page(client):
    jid = db.add_job(source="jobstreet", url="https://sg.jobstreet.com/job/1", company="Acme", role="Engineer",
                     location="Tuas, Singapore", posting_text="Acme makes industrial pumps for ships.", state="Ready")
    info = {"status": "ok", "agency": False, "company": "Acme", "made_at": "2026-10-15T20:00", "dropped": 0,
            "notable": [], "client": None, "sources": [{"id": "S1", "label": "The job posting", "url": "", "kind": "posting"}],
            "fields": {"what_they_do": {"text": "Makes pumps.", "source": "S1", "quote": "Acme makes industrial pumps",
                                        "from_posting": True, "posting_only": True}}}
    db.set_company_info(jid, json.dumps(info))
    page = client.get(f"/jobs/{jid}").text
    assert "About Acme" in page and "From the job posting only" in page and "Registered as" in page


# ── what the live check found (2026-10-15) ───────────────────────────

def test_a_website_that_shows_a_block_page_is_not_asked_again(monkeypatch):
    """Seen live: appliedmaterials.com answered 'Access Denied', and the same site, named by Wikidata, the board
    and the posting, was asked three times."""
    monkeypatch.setattr(company, "resolve", lambda host, port: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))])
    denied = "<html><head><title>Access Denied</title></head><body>You don't have permission</body></html>"
    browser = _Browser({"https://www.appliedmaterials.com/": denied, "https://www.appliedmaterials.com/sg/en.html": denied,
                        "http://appliedmaterials.com": denied})
    job = _job(source="jobstreet", url="https://sg.jobstreet.com/job/1", company="Applied Materials South East Asia Pte Ltd",
               posting_text="Visit https://www.appliedmaterials.com/sg/en.html and http://appliedmaterials.com today.")
    srcs, skipped = company.gather(_PacedContext(browser), job, {"website": "https://www.appliedmaterials.com/"})
    assert len(browser.isolated) == 1 and sum("robot check" in s for s in skipped) == 1


def test_the_second_page_is_never_the_home_page_again():
    """Seen live: pivotsearch.co's 'About' link pointed at https://www.pivotsearch.co/#about, read twice."""
    home = ("<a href='https://www.pivotsearch.co/#about'>About</a><a href='/?page_id=136'>Contact</a>"
            "<a href='https://www.pivotsearch.co/'>Home contact</a>")
    assert company.subpage_link(home, "https://pivotsearch.co/") == "https://pivotsearch.co/?page_id=136"
    assert company.subpage_link("<a href='https://www.x.co/#contact'>Contact</a>", "https://x.co/") == ""


def test_the_registered_office_fills_the_companys_office_when_nothing_else_does(monkeypatch):
    srcs, _ = _gathered(monkeypatch)
    out = company.verify({"agency": True}, srcs, job=_job())
    assert out["fields"]["company_location"] == {
        "text": "66 Duchess Avenue, #04-02, Duchess Crest, Singapore 269204 (registered office)", "source": "S2",
        "quote": "Registered office address: 66 Duchess Avenue, #04-02, Duchess Crest, Singapore 269204"}


def test_the_dashboard_line_names_the_client_briefly_or_not_at_all():
    """Seen live: 'for: The posting does not name the client; the role…'."""
    long = {"text": "The posting does not name the client; the role involves M&E works at data centres.", "source": "S1"}
    info = {"status": "ok", "agency": True, "client": long, "fields": {"job_location": {"text": "Islandwide"}}}
    assert company.one_line(info) == "Recruiter posting · for an unnamed client · Islandwide"
    info["client"] = long | {"short": "Data centre builder"}
    assert company.one_line(info) == "Recruiter posting · for: Data centre builder · Islandwide"


def test_from_the_job_posting_says_only_when_nothing_else_described_the_company():
    posting = company.Source("S1", "The job posting", "u", "Location: Tuas\n\nAcme makes industrial pumps.", "posting")
    site = company.Source("S2", "The company's website", "https://acme.sg", "Acme Pte Ltd. Contact us.", "website")
    reply = {"what_they_do": {"text": "Makes industrial pumps.", "source": "S1", "quote": "Acme makes industrial pumps"}}
    assert company.verify(reply, [posting], job={})["fields"]["what_they_do"]["posting_only"] is True
    assert company.verify(reply, [posting, site], job={})["fields"]["what_they_do"]["posting_only"] is False


# ── what the review found (2026-10-15) ───────────────────────────────

def test_a_legal_ending_is_only_ever_a_whole_word():
    assert more.registry_base("Unlimited Pte Ltd") == "UNLIMITED" and more.registry_base("Bancorp Pte Ltd") == "BANCORP"
    assert more.registry_base("Zinc Pte Ltd") == "ZINC" and more.registry_base("Stripe Inc") == "STRIPE INC"


def test_a_name_with_a_legal_ending_only_matches_that_legal_form(monkeypatch):
    rec = dict(PIVOT["result"]["records"][0])
    sole = rec | {"uen": "53000000A", "entity_name": "PIVOT SEARCH", "entity_type_description": "Sole Proprietorship/ Partnership"}
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"data.gov.sg": acra([sole])}))
    with pytest.raises(more.Unavailable, match="no company registered"):
        more.acra_lookup("Pivot Search Pte Ltd")  # never the business that shares its words
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"data.gov.sg": acra([sole, rec | {"entity_status_description": "Struck Off"}])}))
    assert more.acra_lookup("Pivot Search Pte Ltd").uen == "202125857G"  # the company, struck off as it is
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"data.gov.sg": acra([sole])}))
    assert more.acra_lookup("Pivot Search").uen == "53000000A"  # no legal ending in the name: any form
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"data.gov.sg": acra([], [rec | {"entity_name": "STRIPE PTE. LTD."}])}))
    with pytest.raises(more.Unavailable):
        more.acra_lookup("Stripe Inc")


@pytest.mark.parametrize("text,agency", [
    ("EA North region manager wanted.", False),
    ("We are not a recruitment agency; we hire for ourselves.", False),
    ("EA Reg No: R1234567", True),
    ("We support our client’s innovation journey through design thinking.", False),
    ("We support our client's innovation journey.", False),
    ("Our client is a leading bank.", True),
])
def test_agency_signs_are_whole_words_and_not_denials(text, agency):
    assert company.verify({"agency": "false"}, [company.Source("S1", "p", "u", "Title: x\n" + text, "posting")])["agency"] is agency


def test_a_named_client_on_the_boards_record_is_never_the_agencys():
    """MyCareersFuture names the employer an agency posts for: when the job's company is that employer, the
    summary is about it; when it isn't, the record's facts about it are the client's, never the agency's."""
    board = company.Source("S2", "MyCareersFuture's record", "u", "Posted by: BEST RECRUITERS PTE. LTD.\n"
                           "Hiring company (the employer this was posted for): MEGA GAMES PTE. LTD.\nEmployees: 500\n"
                           "About MEGA GAMES PTE. LTD., in its own words:\nMega Games builds mobile games for kids.", "board")
    posting = company.Source("S1", "The job posting", "u", "Location: Singapore\n\nOur client, a games studio, is hiring.",
                             "posting")
    reply = {"agency": True, "what_they_do": {"text": "Builds mobile games for kids.", "source": "S2",
                                              "quote": "Mega Games builds mobile games for kids"},
             "size": {"text": "500 employees", "source": "S2", "quote": "Employees: 500"}}
    out = company.verify(reply, [posting, board], job={"company": "BEST RECRUITERS PTE. LTD."})
    assert out["agency"] and out["fields"]["what_they_do"] is None and out["fields"]["size"] is None
    assert out["client_name"] == "MEGA GAMES PTE. LTD."
    out = company.verify(reply, [posting, board], job={"company": "Mega Games Pte Ltd"})
    assert out["agency"] is False and out["fields"]["what_they_do"]["text"] == "Builds mobile games for kids."
    assert out["posted_by"] == "BEST RECRUITERS PTE. LTD." and out["notable"][0]["quote"] == "Posted by: BEST RECRUITERS PTE. LTD."
    assert company.one_line({"status": "ok", **out}).startswith("Via a recruiter · ")


def test_an_agency_summary_saved_before_the_split_hides_the_clients_facts(client):
    jid = db.add_job(source="indeed", url="https://sg.indeed.com/viewjob?jk=1", company="PIVOT SEARCH PTE. LTD.",
                     role="Programmer", location="Singapore", posting_text="Our client makes games.", state="Ready")
    old = {"status": "ok", "agency": True, "company": "PIVOT SEARCH PTE. LTD.", "made_at": "2026-10-15T19:00", "dropped": 0,
           "notable": [], "sources": [{"id": "S1", "label": "The job posting", "url": ""}],
           "fields": {"what_they_do": {"text": "CLIENT makes games", "source": "S1", "quote": "Our client makes games"}}}
    db.set_company_info(jid, json.dumps(old))
    page = client.get(f"/jobs/{jid}").text
    assert "Posted by: PIVOT SEARCH PTE. LTD." in page and "CLIENT makes games" not in page


def test_wikipedia_group_matches_need_place_words_and_the_posting_to_name_the_group(monkeypatch):
    assert more.wiki_match("Apex", "Apex Holdings Pte Ltd") == "" and more.wiki_match("Dragon", "Dragon Services Pte Ltd") == ""
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"en.wikipedia.org": lambda u, p: (200, WIKI_AMAT),
                                                     "www.wikidata.org": lambda u, p: (200, '{"claims": {}}')}))
    with pytest.raises(more.Unavailable, match="different company"):
        more.wikipedia("Applied Materials South East Asia Pte Ltd", [], "We make wafers.")
    assert more.wikipedia("Applied Materials South East Asia Pte Ltd", [], "Join Applied Materials in Singapore").how == "group"
    # seen live: the board named appliedmaterials.com, Wikidata the same site; still the group's article
    monkeypatch.setattr(more, "http_get", FakeWeb(**{"en.wikipedia.org": lambda u, p: (200, WIKI_AMAT), "www.wikidata.org":
        lambda u, p: (200, json.dumps({"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.appliedmaterials.com/"}}}]}}))}))
    assert more.wikipedia("Applied Materials South East Asia Pte Ltd", ["https://appliedmaterials.com"], "").how == "group"


def test_a_source_that_says_slow_down_is_rested_not_asked_by_every_package(monkeypatch):
    web = FakeWeb(**{"data.gov.sg": acra(status=429)})
    monkeypatch.setattr(more, "http_get", web)
    ctx = _PacedContext(_Browser({}))
    company.gather(ctx, _job(), None)
    asked = [h for h, _ in web.calls].count("data.gov.sg")
    assert asked == 2  # once, and once more after the wait it asks for
    _, skipped = company.gather(ctx, _job(), None)
    assert [h for h, _ in web.calls].count("data.gov.sg") == asked
    assert any("slow down" in s for s in skipped)


def test_a_stop_asks_nothing_more():
    import threading
    stop = threading.Event()
    stop.set()
    with pytest.raises(more.Unavailable, match="stopped"):
        more.polite_get("https://data.gov.sg/api/action/datastore_search", stop_event=stop)
