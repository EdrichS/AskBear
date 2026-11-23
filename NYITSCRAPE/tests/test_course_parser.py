"""course_parser.py: the course directory (/courses?cs=<SUBJECT>&cal=<UG|GR|PROF>)."""

from __future__ import annotations

import pytest

from askbear_scraper import course_parser as cp
from askbear_scraper.models import CourseRecord

from testkit import fixture_html

CAT = "https://catalog.nyit.edu"
LISTING = CAT + "/courses?cal=UG&cs=CSCI"


@pytest.fixture(scope="module")
def courses():
    found = cp.parse_course_listing(fixture_html("catalog_course_listing.html"), LISTING, "2026-2027")
    return {c.code: c for c in found}


# ---------------------------------------------------------------------------
# the real listing page (cut to 7 courses)
# ---------------------------------------------------------------------------
def test_every_course_of_the_page_is_parsed_in_page_order(courses):
    assert list(courses) == ["CSCI 101", "CSCI 125", "CSCI 260", "CSCI 425", "CSCI 426", "CSCI 455", "CSCI 456"]
    for c in courses.values():
        assert isinstance(c, CourseRecord)
        assert c.course_id == f"{c.code}|UG"
        assert (c.subject, c.subject_name) == ("CSCI", "Computer Science")
        assert (c.level, c.level_code) == ("undergraduate", "UG")
        assert c.school == "College of Eng & Comp Sciences"
        assert c.url == LISTING and c.catalog_year == "2026-2027"
        assert c.text.startswith(f"{c.code}: {c.title}\n")


def test_course_with_prerequisite_and_hours(courses):
    c = courses["CSCI 260"]
    assert (c.number, c.title) == ("260", "Data Structures")
    assert (c.credits, c.credits_min, c.credits_max) == ("3.0", 3.0, 3.0)
    assert c.description.startswith("The classic data structures, such as stacks, queues")
    assert c.prerequisites == "CSCI 185." and c.corequisites is None
    assert c.hours == "3-0-3"
    assert c.attributes == {
        "Prerequisite Course(s)": "Prerequisite: CSCI 185.",
        "Classroom Hours - Laboratory and/or Studio Hours – Course Credits": "3-0-3"}
    assert c.text.splitlines() == [
        "CSCI 260: Data Structures",
        "Computer Science (CSCI), undergraduate course, 3.0 credits, College of Engineering and Computing Sciences.",
        c.description,
        "Prerequisites: CSCI 185.",
        "Hours (class-lab-credits): 3-0-3",
    ]


def test_corequisite_written_in_the_prerequisite_field(courses):
    c = courses["CSCI 125"]
    assert c.prerequisites is None
    assert c.corequisites == "MATH 141 or higher"
    assert c.hours == "3-1-3"
    assert "Corequisites: MATH 141 or higher" in c.text


def test_prerequisite_and_corequisite_together(courses):
    c = courses["CSCI 425"]
    assert (c.prerequisites, c.corequisites) == ("CSCI 353.", "CSCI 435.")


def test_enrollment_restriction_is_a_prerequisite(courses):
    assert courses["CSCI 455"].prerequisites == "Course is limited to seniors"


def test_course_without_description_or_hours(courses):
    c = courses["CSCI 101"]
    assert c.description == "" and c.hours is None and c.prerequisites is None
    assert c.attributes == {}                     # a label printed without a value is dropped
    assert c.text == ("CSCI 101: Computer Concept\nComputer Science (CSCI), undergraduate course, "
                      "3.0 credits, College of Engineering and Computing Sciences.")


def test_two_credit_course(courses):
    c = courses["CSCI 456"]
    assert (c.credits, c.credits_min, c.credits_max, c.hours) == ("2.0", 2.0, 2.0, "1-3-2")


def test_course_text_is_clean(courses):
    for c in courses.values():
        for value in (c.title, c.description, c.prerequisites or "", c.text):
            assert "  " not in value and "&amp;" not in value and "<" not in value


def test_subject_names_from_the_dropdown():
    names = cp.subject_names(fixture_html("catalog_course_listing.html"))
    assert names == {"ACCT": "Accounting", "ARCH": "Architecture", "AAID": "Architecture/Interior Design",
                     "CSCI": "Computer Science", "EENG": "Electrical Engineering", "MATH": "Mathematics"}
    assert cp.subject_names("<p>no dropdown</p>") == {}


# ---------------------------------------------------------------------------
# synthetic listing pages for the quirks
# ---------------------------------------------------------------------------
def summary_row(key, code, title, credits="3.0", school="College of Arts & Sciences"):
    return (f'<tr><td nowrap><a href="javascript:void(0);" data-course="{key}" class="viewCourseInfo">{code}</a></td>'
            f'<td><a href="javascript:void(0);" data-course="{key}" class="viewCourseInfo">{title}</a></td>'
            f"<td>{credits}</td><td>{school}</td></tr>")


def detail_row(key, inner):
    return f'<tr class="hideCourseInfo {key}"><td colspan="4">{inner}</td></tr>'


def listing(rows, options=('<option value="BIOL">BIOL - Biology</option>',)):
    return ("<html><body><select class='courseDD'>" + "".join(options) + "</select>"
            "<table id='course_catalog_table'><thead><tr><th>Name</th><th>Title</th><th>Credits</th>"
            "<th>School</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></body></html>")


def test_detail_rows_are_paired_by_key_even_out_of_order_or_missing():
    html = listing([
        summary_row("BIOL-110", "BIOL 110", "General Biology I"),
        summary_row("BIOL-150", "BIOL 150", "Anatomy"),
        detail_row("BIOL-150", "Bones and muscles.<br><br><strong>Prerequisite Course(s): </strong>BIOL 110"),
        detail_row("BIOL-110", "Cells and genes.<br><br><strong>Classroom Hours - Laboratory and/or Studio "
                               "Hours – Course Credits: </strong>3-3-4"),
        summary_row("BIOL-199", "BIOL 199", "No Details"),
        detail_row("BIOL-999", "An orphan detail row"),
    ])
    found = {c.code: c for c in cp.parse_course_listing(html, CAT + "/courses?cal=UG&cs=BIOL")}
    assert list(found) == ["BIOL 110", "BIOL 150", "BIOL 199"]
    assert found["BIOL 110"].description == "Cells and genes." and found["BIOL 110"].hours == "3-3-4"
    assert found["BIOL 150"].description == "Bones and muscles." and found["BIOL 150"].prerequisites == "BIOL 110"
    assert found["BIOL 199"].description == "" and found["BIOL 199"].attributes == {}
    assert found["BIOL 110"].subject_name == "Biology"


def test_same_code_same_title_is_merged_and_different_titles_are_kept_apart():
    html = listing([
        summary_row("LDB-505", "LDB 505", "Leadership", "4.0"), detail_row("LDB-505", "Short."),
        summary_row("LDB-505", "LDB 505", "Leadership", "4.5"), detail_row("LDB-505", "A longer description."),
        summary_row("MTIM-893", "MTIM 893", "Cardiology IV", "6.0"), detail_row("MTIM-893", ""),
        summary_row("MTIM-893", "MTIM 893", "Infectious Disease IV", "6.0"), detail_row("MTIM-893", ""),
    ])
    found = cp.parse_course_listing(html, CAT + "/courses?cal=PROF&cs=LDB")
    assert [c.course_id for c in found] == ["LDB 505|PROF", "MTIM 893|PROF", "MTIM 893|PROF|2"]
    merged = found[0]
    assert merged.credits == "4.0 or 4.5" and (merged.credits_min, merged.credits_max) == (4.0, 4.5)
    assert merged.description == "A longer description.\nShort."
    assert (found[1].title, found[2].title) == ("Cardiology IV", "Infectious Disease IV")
    assert all(c.level == "professional" for c in found)


def test_flag_labels_and_unusual_codes():
    html = listing([
        summary_row("ARCH-701B", "ARCH 701B", "Thesis", "1.0-6.0"),
        detail_row("ARCH-701B", "Independent thesis work. Classroom Hours- Laboratory and/or Studio Hours- "
                                "Course Credits: 3-0-3<br><br><strong>Inst Cnst</strong>"),
        summary_row("OM-AP01", "OM AP01", "Cl Clk FP Outrch", "0.0"),
        detail_row("OM-AP01", ""),
    ])
    found = {c.code: c for c in cp.parse_course_listing(html, CAT + "/courses?cal=GR&cs=ARCH")}
    thesis = found["ARCH 701B"]
    assert (thesis.number, thesis.credits_min, thesis.credits_max) == ("701B", 1.0, 6.0)
    assert thesis.description == "Independent thesis work."          # hours sentence moved out
    assert thesis.hours == "3-0-3"
    assert thesis.attributes == {"Inst Cnst": "Instructor consent required"}
    assert "Instructor consent required." in thesis.text
    assert found["OM AP01"].number == "AP01"


def test_page_without_course_table_gives_no_courses():
    assert cp.parse_course_listing("<html><p>Courses</p></html>", LISTING) == []


def test_listing_url_is_canonical_without_cc():
    found = cp.parse_course_listing(fixture_html("catalog_course_listing.html"),
                                    CAT + "/courses?cal=UG&cc=CSCI-260&cs=CSCI")
    assert {c.url for c in found} == {LISTING}


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url, expected", [
    (LISTING, True),
    (CAT + "/courses/?cs=CSCI&cal=UG", True),
    (CAT + "/courses?cal=ug&cs=csci", True),
    (CAT + "/courses", False),
    (CAT + "/courses?cs=CSCI", False),
    (CAT + "/courses?cs=&cal=UG", False),
    (CAT + "/engineering?cs=CSCI&cal=UG", False),
    ("", False),
])
def test_is_course_listing(url, expected):
    assert cp.is_course_listing(url) is expected


def test_listing_params_and_links():
    assert cp.listing_params(CAT + "/courses?cal=ug&cs=csci") == ("CSCI", "UG")
    assert cp.listing_params(CAT + "/courses") == ("", "")
    assert cp.canonical_listing_url(CAT + "/courses?cal=GR&cc=PHAS-601&cs=PHAS") == CAT + "/courses?cal=GR&cs=PHAS"
    course = CourseRecord("CSCI 260|UG", "CSCI 260", "CSCI", "Computer Science", "260", "Data Structures",
                          "3.0", 3.0, 3.0, "", "undergraduate", "UG", "", None, None, None, {}, "", LISTING)
    assert cp.course_link(course) == CAT + "/courses?cal=UG&cc=CSCI-260&cs=CSCI"


@pytest.mark.parametrize("text, expected", [
    ("3.0", (3.0, 3.0)), ("1.0-3.0", (1.0, 3.0)), ("1.0 - 6.0", (1.0, 6.0)), ("1 to 3", (1.0, 3.0)),
    ("Variable", (None, None)), ("", (None, None)), (None, (None, None)), (".5", (0.5, 0.5)),
    ("3.0 or 4.0", (3.0, 4.0)),
])
def test_parse_credits(text, expected):
    assert cp.parse_credits(text) == expected


@pytest.mark.parametrize("value, expected", [
    ("3-0-3", "3-0-3"), ("1 - 2 - 1", "1-2-1"), ("3-0-3.", "3-0-3"), ("3–0–3", "3-0-3"),
    ("Classroom 3-0-3 hrs", "3-0-3"), ("TBA", "TBA"), ("", None), (None, None),
])
def test_normalize_hours(value, expected):
    assert cp.normalize_hours(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("Prerequisite: ARCH 602. Corequisite: ARCH 705", ("ARCH 602.", "ARCH 705")),
    ("Corequisite: MATH 141 or higher", (None, "MATH 141 or higher")),
    ("Course is limited to juniors and seniors", ("Course is limited to juniors and seniors", None)),
    ("Co-requisite: NURS 351. Course is limited to Nursing students.",
     ("Course is limited to Nursing students.", "NURS 351.")),
    ("Prerequisites: CSCI 185; Corequisites: CSCI 260", ("CSCI 185", "CSCI 260")),
    ("Prerequisite: CSCI 125 Co-requisite: CSCI 185", ("CSCI 125", "CSCI 185")),
    ("Prerequsites: BIOL 110", ("BIOL 110", None)),                  # misspelt on the site
    ("", (None, None)),
])
def test_split_requisites(value, expected):
    assert cp.split_requisites(value) == expected


def test_strip_hours_sentence():
    assert cp.strip_hours_sentence("Intro. Classroom Hours- Laboratory and/or Studio Hours- Course Credits: "
                                   "3-0-3") == ("Intro.", "3-0-3")
    assert cp.strip_hours_sentence("No hours here.") == ("No hours here.", None)


def test_canonical_school():
    assert cp.canonical_school("College of Eng & Comp Sciences") == "College of Engineering and Computing Sciences"
    assert cp.canonical_school("school of arch & design") == "School of Architecture and Design"
    assert cp.canonical_school("Unknown School") == "Unknown School"
    assert cp.canonical_school("") == ""


def test_dedupe_and_summarize_courses(courses):
    first = list(courses.values())
    other_page = cp.parse_course_listing(fixture_html("catalog_course_listing.html"),
                                         CAT + "/courses?cal=UG&cs=MATH")
    deduped = cp.dedupe_courses(other_page + first)
    assert [c.course_id for c in deduped] == [c.course_id for c in other_page]
    assert all(c.url == LISTING for c in deduped)                    # the course's own subject page
    summary = cp.summarize_courses(deduped)
    assert summary["records"] == summary["unique_codes"] == 7
    assert summary["unique_subjects"] == 1
    assert summary["per_level"] == {"undergraduate": {"records": 7, "unique_codes": 7, "unique_subjects": 1}}
    assert summary["with_description_pct"] == round(100 * 6 / 7, 1)
    assert summary["labels"]["Classroom Hours - Laboratory and/or Studio Hours – Course Credits"] == 6
    assert cp.summarize_courses([c.to_dict() for c in deduped]) == summary
    assert cp.summarize_courses([])["records"] == 0
