"""Regression tests for the parse fixes of the extraction-fidelity review.

Real pages, trimmed, are in tests/fixtures/fixes_parse (sources.json there says where each
one comes from and what was cut); the other cases are small hand-written pages.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from askbear_scraper import html_parser
from askbear_scraper import site_parser as sp
from askbear_scraper.models import PAGE_TYPES, PageRecord
from askbear_scraper.utils import make_soup

from testkit import fixture_html, fixture_url

FIXES = Path(__file__).resolve().parent / "fixtures" / "fixes_parse"
SOURCES = json.loads((FIXES / "sources.json").read_text(encoding="utf-8"))


def parse_fixture(name: str, **kwargs):
    html = (FIXES / name).read_text(encoding="utf-8")
    return sp.parse_any(html, SOURCES[name]["final_url"], "2026-11-23T07:00:00+00:00", **kwargs)


def page(body: str, title: str = "Test Page | New York Tech", body_class: str = "page", head: str = "") -> str:
    return (f"<html><head><title>{title}</title>{head}</head><body class='{body_class}'><main>"
            f"{body}</main></body></html>")


def section(sections, heading):
    found = [s for s in sections if s.heading == heading]
    assert found, (heading, [s.heading for s in sections])
    return found[0]


# ---------------------------------------------------------------------------
# panels hidden until a control shows them
# ---------------------------------------------------------------------------
def test_advisor_panels_of_a_drop_down_become_one_section_per_program():
    rec, sections = parse_fixture("www_find_an_advisor.html")
    text = rec.text
    # the content of the display:none panels is page text now
    assert "Please email one of the advisors below for an advising appointment" in text
    assert "Department Chair: Prof. Gertrudis Brens" in text
    assert "Select One" not in text
    # each panel is a section under its option's label, one level below the heading
    # above the drop-down
    arch = section(sections, "Architecture, B.ARCH and Architectural Technology B.S.A.T.")
    assert arch.heading_path == ["Continuing Undergraduate Students",
                                 "Architecture, B.ARCH and Architectural Technology B.S.A.T."]
    assert "Dongsei Kim" in arch.text and "Long Island" in arch.text
    assert section(sections, "Bioengineering, B.S.").heading_path[0] == "Continuing Undergraduate Students"
    assert section(sections, "Architecture, M. ARCH.").heading_path == ["Graduate Students", "Architecture, M. ARCH."]
    # the panels' own headings ("Long Island") are bold lines, not sections
    assert not any(s.heading in ("Long Island", "New York City") for s in sections)
    # the same instructions under two programs belong to both of them
    shared = "All current students will be assigned to an academic advisor mid-semester"
    assert shared in section(sections, "Biochemistry, B.S.").text
    assert shared in section(sections, "Biology, B.S.").text


def test_panels_no_control_points_at_stay_hidden():
    rec, _ = parse_fixture("www_find_an_advisor.html")
    assert "Please contact advising@nyit.edu for assistance" not in rec.text      # content2
    assert "Joo-Kwang Yun" not in rec.text                                      # content21


def test_tab_panels_are_shown_but_modals_menus_and_forms_stay_hidden():
    html = page(
        "<h2>Costs</h2>"
        "<button role='tab' aria-controls='tab-fees'>Fees</button>"
        "<div id='tab-fees' role='tabpanel' hidden><p>The technology fee is 300 dollars per semester for every student.</p></div>"
        "<a href='#' data-toggle='collapse' data-target='#more'>More</a>"
        "<div id='more' style='display: none'><p>Payment plans spread the balance over four monthly payments.</p></div>"
        "<a href='#' data-toggle='collapse' data-target='#popup'>Open</a>"
        "<div id='popup' class='modal' style='display:none'><p>Sign up for our newsletter today please friend.</p></div>"
        "<select><option value='quiz'>Quiz</option></select>"
        "<div id='quiz' style='display:none'><p>Answer the quiz question below now.</p><input name='a'></div>"
        "<div id='orphan' style='display:none'><p>Old text that no control shows any more.</p></div>")
    rec, _ = sp.parse_any(html, "https://www.nyit.edu/costs/")
    assert "The technology fee is 300 dollars" in rec.text
    assert "Payment plans spread the balance" in rec.text
    assert "newsletter" not in rec.text and "quiz question" not in rec.text
    assert "Old text" not in rec.text


def test_more_panels_holding_account_details_stay_hidden():
    def more(i, inner):
        return (f"<li><div id='s-lg-content-{i}'><a href='https://example.org/{i}'>Database {i}</a>"
                f"<div class='s-lg-label-moreinfo'><a class='s-lg-label-more' href='javascript:void(0);'>more...</a>"
                f"<a class='s-lg-label-less' style='display:none' href='javascript:void(0);'>less...</a></div>"
                f"<div class='s-lg-content-moreinfo' id='s-lg-content-moreinfo-{i}' style='display:none;'>{inner}</div>"
                f"</div></li>")
    html = ("<html><head><title>Weather Data - SaME Lab - LibGuides at New York Institute of Technology</title>"
            "</head><body><div id='s-lg-guide-main'><h2>Data sources</h2><ul>"
            + more(1, "Hourly readings of the rooftop weather station since 2019, as spreadsheets.")
            + more(2, "U: demo-user<br>P: not-a-real-password")
            + more(3, "Username: demo<br>Password: placeholder-only")
            + "</ul></div></body></html>")
    rec, _ = sp.parse_any(html, "https://libguides.nyit.edu/samelab")
    assert "Hourly readings of the rooftop weather station" in rec.text
    for gone in ("demo-user", "not-a-real-password", "Password", "placeholder-only", "more...", "less..."):
        assert gone not in rec.text


@pytest.mark.parametrize("inner, secret", [
    ("U: demo<br>P: demo123", True),
    ("Username: demo<br>Password: demo123", True),
    ("Use the PIN code: 0000 at the door", True),
    ("Long Island<br>P: (516) 686-7657", False),           # a phone number, no user name
    ("Passwords are reset every 90 days by the IT Service Desk.", False),
])
def test_has_credentials(inner, secret):
    assert sp.has_credentials(make_soup(f"<div>{inner}</div>").find("div")) is secret


def test_libguides_more_descriptions_are_text_and_toggles_go():
    links, _ = parse_fixture("libguides_moreinfo_links.html")
    assert "Focuses on hard-to-find local and regional business news coverage" in links.text
    buttons, _ = parse_fixture("libguides_moreinfo_buttons.html")
    assert "DETAIL Digital www.detail.de/en is an online knowledge platform" in buttons.text
    for rec in (links, buttons):
        assert "more..." not in rec.text and "less..." not in rec.text


# ---------------------------------------------------------------------------
# within-page repeats
# ---------------------------------------------------------------------------
COURSE = ("A student may remediate only one course in academic Year 2. In such cases, the student will be "
          "placed on Academic Warning status. A grade of U (Unsatisfactory) will be recorded on the official "
          "transcript until the course is successfully remediated, at which time the grade will be changed to "
          "P (Pass). If unsuccessful in remediating the course, the student will be assigned the grade of F "
          "(Fail) and be referred to the SPC to petition to repeat the year on Academic Probation.")
CLERKSHIP = ("A student may remediate only one clerkship per academic year. In such cases, the student will be "
             "placed on Academic Warning status. A grade of U (Unsatisfactory) will be recorded on the official "
             "transcript until the clerkship is successfully remediated, at which time the grade will be changed "
             "to P (Pass). If unsuccessful in remediating the clerkship, the student will be assigned the grade "
             "of F (Fail) and be referred to the SPC to petition to repeat the year on Academic Probation.")


def test_policy_rules_that_differ_in_one_word_are_both_kept():
    html = page(f"<h2>Pre-Clinical Years</h2><p>{COURSE}</p><h2>Clinical Years</h2><p>{CLERKSHIP}</p>",
                title="NYITCOM Student Handbook | New York Tech")
    rec, _ = sp.parse_any(html, "https://apps.nyit.edu/policies/collection/nyitcom_student_handbook")
    assert "remediate only one course in academic Year 2" in rec.text
    assert "remediate only one clerkship per academic year" in rec.text


def test_exact_copies_are_dropped_only_within_their_section():
    para = ("The building adjacent to the parking garage is the student union. Please enter the student union "
            "and proceed through the building north to reach the hall where the college offices are.")
    html = page(f"<h2>From the North</h2><p>Take exit 23.</p><p>{para}</p><p>{para}</p>"
                f"<h2>From the South</h2><p>Take exit 44.</p><p>{para}</p>")
    rec, sections = sp.parse_any(html, "https://www.nyit.edu/directions/")
    assert section(sections, "From the North").text.count("parking garage") == 1     # copy dropped
    assert "parking garage" in section(sections, "From the South").text            # kept there
    assert rec.text.count("parking garage") == 2


def test_magazine_near_copies_go_but_not_when_a_decisive_word_differs():
    base = ("The leadership team of the university promised to {verb} its values as it grows its research "
            "enterprise across campuses in New York and beyond, building on decades of work by faculty, staff, "
            "students and alumni who shaped the institution and its mission of access to opportunity for every "
            "learner who comes to study, teach or work here.")
    desktop, mobile = base.format(verb="uphold"), base.format(verb="upheld")     # Foleon's two copies
    rec, _ = sp.parse_any(page(f"<article><p>{desktop}</p><p>{mobile}</p></article>",
                               title="First Byte - 2025 Spring - NYIT Magazine"),
                          "https://magazine.nyit.edu/spring2025/first-byte")
    assert rec.text.count("The leadership team") == 1
    first, second = base.format(verb="uphold") + " First year.", base.format(verb="uphold") + " Second year."
    rec, _ = sp.parse_any(page(f"<article><p>{first}</p><p>{second}</p></article>",
                               title="Second Story - 2025 Spring - NYIT Magazine"),
                          "https://magazine.nyit.edu/spring2025/second-story")
    assert rec.text.count("The leadership team") == 2


def test_near_copy_rules():
    words = "a student may remediate only one course in academic year 2 in such cases".split()
    assert sp._near_copy(words, words)
    assert not sp._near_copy(words, [w if w != "course" else "clerkship" for w in words])
    nsf = ("recipients of awards from the nsf are required to comply with specific requirements to ensure "
           "appropriate training in the responsible conduct of research and researchers applying for support "
           "will need to be familiar with these regulations").split()
    assert not sp._near_copy(nsf, [w if w != "nsf" else "nih" for w in nsf])
    assert not sp._near_copy(nsf + ["i"], nsf + ["ii"])
    assert not sp._near_copy(nsf + ["first"], nsf + ["second"])
    assert sp._near_copy(nsf + ["uphold"], nsf + ["upheld"])
    assert not sp._near_copy(nsf, nsf[:20])                                  # far apart


# ---------------------------------------------------------------------------
# titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, title", [
    ("www_degree_mba_finance.html", "Management, M.B.A. – Finance"),
    ("www_degree_bs_finance.html", "Business Administration, B.S. – Finance"),
])
def test_degree_pages_are_titled_with_their_concentration(name, title):
    rec, sections = parse_fixture(name)
    assert rec.title == title and rec.program == title
    assert {s.page_title for s in sections} == {title}


def test_degree_sub_pages_keep_the_program_after_their_own_title():
    crumbs = ("<div class='yoast-breadcrumbs'><span><a href='/'>Home</a> &gt; <a href='/academics/'>Academics</a> &gt; "
              "<a href='/academics/degrees/'>Degrees</a> &gt; <a href='/academics/degrees/management-mba-finance/'>"
              "Management, M.B.A. – Finance</a> &gt; <span>Admission Requirements</span></span></div>")
    html = page(f"<div class='Hero'><h1 class='Hero__heading'>Admission Requirements</h1>{crumbs}</div>"
                "<div class='Common'><p>Applicants need a bachelor's degree from an accredited institution.</p></div>",
                title="Admission Requirements : Master of Business Administration, M.B.A. – Finance | New York Tech")
    rec, _ = sp.parse_any(html, "https://www.nyit.edu/academics/degrees/management-mba-finance/admission-requirements/")
    assert rec.title == "Admission Requirements: Management, M.B.A. – Finance"


@pytest.mark.parametrize("title, candidates, expected", [
    ("Management, M.B.A.", ["Management, M.B.A. – Finance"], "Management, M.B.A. – Finance"),
    ("Message from the Chair", ["", "Message from the Chair: Department of Physics"],
     "Message from the Chair: Department of Physics"),
    ("Master of Business Administration", ["Master of Business Administration ( MBA)"],
     "Master of Business Administration (MBA)"),
    ("Faculty Profile: Rosemary Gallagher", ["Faculty Profile: Rosemary Gallagher: Rosemary Gallagher"], ""),
    ("School of Management", ["School of Management Archives"], ""),
    ("Osteopathic Medicine Center", ["Osteopathic Medicine Center (Page 2)"], ""),
    ("Business", ["Businesses and Startups"], ""),
    ("Management, M.B.A.", ["Management, M.B.A."], ""),
])
def test_fuller_title(title, candidates, expected):
    assert sp._fuller_title(title, candidates) == expected


# ---------------------------------------------------------------------------
# pages that are not pages
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, reason", [
    ("nyitetic_iis.html", "web server default page"),
    ("eportfolios_invalid_parameter.html", "application error page"),
    ("eportfolios_legal.html", "placeholder page"),
])
def test_server_default_error_and_placeholder_pages_get_no_sections(name, reason):
    rec, sections = parse_fixture(name)
    assert sections == [] and rec.page_type == "other"
    assert sp.soft_404_reason(rec.title, rec.page_title, rec.text) == reason


@pytest.mark.parametrize("url, title, body", [
    ("https://blogs.nyit.edu/uxui/test_class_of_2020", "Test / Class of 2020 | UX/UI Blog | Blogs NYIT",
     "<p>Test Stuff Here!</p>"),
    ("https://www.nyit.edu/news/articles/cache/", "cache | New York Tech", "<p>A Lifelong Learner</p>"),
    ("https://virtualtour.nyit.edu/nyit/long-island-campus", "Circuit Virtual Tours",
     "<p>Your browser is unsupported. Please upgrade your browser before visiting this site.</p>"),
])
def test_test_cache_and_browser_wall_pages_get_no_sections(url, title, body):
    rec, sections = sp.parse_any(page(f"<h1>{title.split(' | ')[0]}</h1>{body}", title=title), url)
    assert sections == [] and rec.page_type == "other"
    assert sp.soft_404_reason(rec.title, rec.page_title, rec.text) is not None


def test_soft_404_reason_leaves_real_pages_alone():
    assert sp.soft_404_reason("Test-Optional Policy", "Test-Optional Policy | New York Tech",
                              "First-time applicants may apply without test scores.") is None
    assert sp.soft_404_reason("Testing Center", "", "Book an exam room.") is None
    assert sp.soft_404_reason("IIS Windows Server", "", "word " * 80) is None
    for name in ("www_degree_mba_finance.html", "nyitetic_errsela.html", "libanswers_contacts.html"):
        rec, sections = parse_fixture(name)
        assert sections and sp.soft_404_reason(rec.title, rec.page_title, rec.text) is None
    rec, sections = sp.parse_any(fixture_html("www_bio.html"), fixture_url("www_bio.html"))
    assert sections and rec.page_type == "bio"


# ---------------------------------------------------------------------------
# a page-wide ASP.NET <form>
# ---------------------------------------------------------------------------
def test_aspnet_page_inside_one_form_keeps_its_text():
    rec, sections = parse_fixture("nyitetic_errsela.html")
    assert "is a collaborative research and student engagement program created by New York Institute of " \
           "Technology" in rec.text
    assert rec.word_count >= 100 and len(sections) == 1
    assert "Digital Fluency" not in rec.text            # the Bootstrap navbar inside the form


def test_real_forms_are_still_dropped():
    html = page("<p>Questions about housing? Write to the residence life office any time.</p>"
                "<form action='/send'><label>Your first name</label><input name='n'>"
                "<label>Your email address</label><input name='e'><button>Send</button></form>")
    rec, _ = sp.parse_any(html, "https://www.nyit.edu/housing/contact/")
    assert "residence life office" in rec.text and "first name" not in rec.text


# ---------------------------------------------------------------------------
# repeated boilerplate below the site-wide threshold
# ---------------------------------------------------------------------------
def test_libanswers_library_address_cards_are_dropped():
    rec, sections = parse_fixture("libanswers_contacts.html")
    assert "you might want to narrow down your topic" in rec.text
    for gone in ("For Further Assistance", "Wisser Memorial Library", "(516) 686-7657", "1855 Broadway"):
        assert gone not in rec.text
    assert len(sections) == 1


def test_library_addresses_in_an_answer_are_kept():
    html = page("<div id='s-la-page-column-1'><p>The Manhattan Library is on the second floor of 1855 Broadway, "
                "next to the student lounge.</p><p>For further assistance:</p><ul><li>Ask at the front desk of "
                "the library about group study rooms and their opening hours.</li></ul></div>",
                title="Where can I study? - Ask a Librarian")
    rec, _ = sp.parse_any(html, "https://libanswers.nyit.edu/faq/1")
    assert "1855 Broadway" in rec.text and "front desk" in rec.text


CARDS = ("<ul><li>Manhattan Library<br>1855 Broadway, New York, NY 10023-7692<br>(212) 261-1526</li>"
         "<li>Wisser Memorial Library<br>Northern Blvd. Old Westbury, NY 11568-8000<br>(516) 686-7657</li></ul>"
         "<h2>Comments (0)</h2>")


@pytest.mark.parametrize("question, answer", [
    ("Where is the Manhattan library?",
     "<p>The Manhattan Library is on the second floor, next to the student lounge.</p><p>For further assistance:</p>"),
    ("NYIT's library", "<p>Thank you for your inquiry. Our librarians can be reached as shown below.</p>"),
])
def test_address_cards_that_answer_the_question_are_kept(question, answer):
    html = page(f"<div id='s-la-page-column-1'>{answer}{CARDS}</div>", title=f"{question} - Ask a Librarian")
    rec, _ = sp.parse_any(html, "https://libanswers.nyit.edu/faq/2")
    assert "1855 Broadway" in rec.text and "(516) 686-7657" in rec.text


def test_empty_directory_widget_cta_and_copyright_lines_are_dropped():
    body = ("<p>She advises international applicants on admission and evaluates their transcripts.</p>"
            "<p>Schedule Tour</p><p>Register Here!</p><p>Download Background</p>"
            "<p>To attend, register here before May 1 and bring your student ID card.</p>"
            "<p>Copyright © 2020 New York Institute of Technology</p>"
            "<p>New York Institute of Technology Magazine © 2024 All Rights Reserved</p>"
            "<p>7 types of work are protected by copyright:</p>"
            "<div class='Widget'><h2>Contact Information</h2> No Directory Data Found </div>")
    rec, sections = sp.parse_any(page(body), "https://www.nyit.edu/about/x/")
    for gone in ("No Directory Data Found", "Contact Information", "Schedule Tour", "Register Here",
                 "Download Background", "©"):
        assert gone not in rec.text
    assert "register here before May 1" in rec.text
    assert "7 types of work are protected by copyright:" in rec.text


def test_block_key_ignores_the_year_of_a_copyright_notice():
    assert sp.block_key("New York Institute of Technology Magazine © 2024 All Rights Reserved") == \
        sp.block_key("New York Institute of Technology Magazine © 2026 All Rights Reserved")
    assert sp.block_key("Fall 2024 calendar") != sp.block_key("Fall 2025 calendar")


def test_online_deadline_tile_is_dropped():
    tile = ("<div class='PromoDateBlock'><div class='PromoDateBlock-date'><div class='PromoDateBlock-month'>Nov</div>"
            "<div class='PromoDateBlock-day'>13</div></div><div class='PromoDateBlock-content'>"
            "<div class='PromoDateBlock-title'><span class='Link'>Priority Deadline</span></div>"
            "<div class='PromoDateBlock-description'>November 13</div>"
            "<div class='PromoDateBlock-label'>Spring 2027</div></div></div>")
    rec, _ = sp.parse_any(page("<p>Apply early to start your online degree in the spring term.</p>" + tile),
                          "https://online.nyit.edu/admissions")
    lines = rec.text.splitlines()
    assert "Nov" not in lines and "13" not in lines
    assert "Priority Deadline" in lines and "November 13" in lines


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------
def test_bio_pages_have_no_publication_date():
    assert '"datePublished":"2025-10-30' in fixture_html("www_bio.html")      # the directory import
    rec, _ = sp.parse_any(fixture_html("www_bio.html"), fixture_url("www_bio.html"))
    assert rec.page_type == "bio" and rec.published is None


def test_event_dates_go_to_event_fields():
    assert {"event_start", "event_end"} <= {f.name for f in fields(PageRecord)}
    rec, _ = sp.parse_any(fixture_html("events_event.html"), fixture_url("events_event.html"))
    assert (rec.event_start, rec.event_end) == ("2026-11-30T13:00:00-05:00", "2026-11-30T14:00:00-05:00")
    assert rec.published is None


def test_events_keep_their_start_in_published_with_a_page_model_without_event_fields(monkeypatch):
    """An older models.PageRecord (no event fields): the start time stays in ``published``."""
    monkeypatch.setattr(sp, "_page_fields",
                        lambda: {f.name for f in fields(PageRecord)} - {"event_start", "event_end"})
    rec, _ = sp.parse_any(fixture_html("events_event.html"), fixture_url("events_event.html"))
    assert rec.published == "2026-11-30T13:00:00-05:00"
    assert rec.event_start is None


def test_event_times():
    assert sp.event_times("2026-12-01T17:45:00Z", "2026-12-01T18:45:00Z", "2026-12-01T12:45:00-05:00") == \
        ("2026-12-01T12:45:00-05:00", "2026-12-01T13:45:00-05:00")
    assert sp.event_times("2026-12-01T05:00:00Z", "2026-12-01T05:00:00Z", "2026-12-01T00:00:00-05:00") == \
        ("2026-12-01T00:00:00-05:00", None)                                   # all day
    assert sp.event_times("2026-12-01T17:45:00Z", None, None) == ("2026-12-01T17:45:00Z", None)


@pytest.mark.parametrize("url, date", [
    ("https://magazine.nyit.edu/fall2024/lighting-the-path-forward", "2024-09-01"),
    ("https://magazine.nyit.edu/spring2026/", "2026-03-01"),
    ("https://magazine.nyit.edu/annual-report/2024/", None),
])
def test_magazine_issue_date(url, date):
    assert sp.issue_date(url) == date


@pytest.mark.parametrize("name, title, date", [
    ("20210729_IndoorMasksPolicy.html", "Indoor Masks Policy Effective Immediately", "2021-07-29"),
    ("Email_provost_20200408_StaffAnnouncement.html", "Faculty Update: April 20, 2020", "2020-04-20"),
    ("Email_Provost_20200902_Reflections.html", "Faculty Update September 2020", "2020-09-02"),
    ("FacultyUpdateApr2021.html", "FacultyUpdateApr2021", "2021-04-01"),
    ("Faculty_Update__October_2021.html", "Faculty Update: October 2021", "2021-10-01"),
    ("Faculty_Update__September_10.html", "Faculty Update: September 10", None),
])
def test_communication_dates(name, title, date):
    url = "https://files.nyit.edu/files/scea/" + name
    assert sp.is_communication(url)
    assert sp.communication_date(url, title) == date


def test_saved_emails_are_dated_communications():
    html = page("<p>Dear New York Tech Students, Faculty, and Staff,</p><p>Effective immediately, masks are "
                "required indoors in public areas and classrooms.</p>",
                title="Indoor Masks Policy Effective Immediately")
    rec, _ = sp.parse_any(html, "https://files.nyit.edu/files/scea/20210729_IndoorMasksPolicy.html")
    assert rec.published == "2021-07-29"
    assert "communication" in PAGE_TYPES and rec.page_type == "communication"
    assert not sp.is_communication("https://files.nyit.edu/files/scea/handbook.pdf")
    assert not sp.is_communication("https://www.nyit.edu/news/x.html")


# ---------------------------------------------------------------------------
# <sup> spacing
# ---------------------------------------------------------------------------
def test_superscript_text_keeps_the_spaces_around_it():
    html = ("<main><p><i><sup>This article was contributed by </sup><strong><sup>Sophie Yoakum</sup></strong>"
            "<sup>, Programs &amp; Retention Coordinator.</sup></i></p>"
            "<p>Composition<sup>1</sup> in the 1<sup>st</sup> year, Brand<sup>®</sup></p></main>")
    blocks = html_parser.html_to_blocks(make_soup(html).find("main"))
    assert [b.text for b in blocks] == [
        "This article was contributed by Sophie Yoakum, Programs & Retention Coordinator.",
        "Composition [1] in the 1st year, Brand®"]
