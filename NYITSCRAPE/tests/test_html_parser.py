"""html_parser.py: the catalog page parser (DOM walker, sections, metadata)."""

from __future__ import annotations

import re

import pytest

from askbear_scraper import html_parser as hp
from askbear_scraper.models import PAGE_TYPES
from askbear_scraper.utils import make_soup, sha1, short_hash, word_count

from testkit import fixture_html, fixture_url

CAT = "https://catalog.nyit.edu"
LEFTOVERS = re.compile(r"&(amp|nbsp|lt|gt|quot|#\d+);|<br|</?(p|div|span|a|td|tr|li|strong)\b|"
                       r"back to top|javascript:|�|[ﬀ-ﬆ]", re.I)


def blocks_of(inner: str):
    """Blocks of a snippet placed in the catalog's content container."""
    soup = make_soup("<html><head><title>T</title></head><body><div class='Main__content'>"
                     f"<div class='Common'>{inner}</div></div></body></html>")
    return hp.html_to_blocks(hp.content_root(soup))


def texts(inner: str):
    return [b.text for b in blocks_of(inner)]


def parse(name: str):
    return hp.parse_page(fixture_html(name), fixture_url(name), "2026-11-23T07:00:00+00:00")


# ---------------------------------------------------------------------------
# the DOM walker
# ---------------------------------------------------------------------------
def test_inline_text_stays_on_one_line_and_blocks_break_lines():
    blocks = blocks_of("<p>Hello <b>world</b>, again<br>second line</p><div>A</div><div>B</div>")
    assert [(b.kind, b.text) for b in blocks] == [
        ("text", "Hello world, again\nsecond line"), ("text", "A"), ("text", "B")]


def test_sup_footnote_markers_become_bracketed_numbers():
    assert texts("<p>Composition<sup>1</sup> and 1<sup>st</sup> year, Brand<sup>®</sup>, "
                 "note<sup>[2]</sup></p>") == ["Composition [1] and 1st year, Brand®, note [2]"]


def test_lists_are_rendered_with_bullets_and_numbers():
    blocks = blocks_of("<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>"
                       "<ol start='3'><li>three</li><li>four</li></ol>")
    assert [(b.kind, b.text, b.level) for b in blocks] == [
        ("list_item", "- one", 1), ("list_item", "- two", 1), ("list_item", "- nested", 2),
        ("list_item", "3. three", 1), ("list_item", "4. four", 1)]


CURRICULUM_TABLE = (
    "<table id='discovery_core_table'><thead><tr><th>Name</th><th>Title</th><th>Credits</th></tr></thead><tbody>"
    "<tr><td colspan='2'><strong>Foundations</strong></td><td>Credits:</td></tr>"
    "<tr><td><a class='classcode' href=\"javascript:getCD('FCWR 101')\">FCWR 101</a></td>"
    "<td>Writing I: Foundations of College Composition<sup>1</sup></td><td>3</td></tr>"
    "<tr id='FCWR 101' style='display:none;'><td colspan='3'>hidden course description</td></tr>"
    "<tr><td></td><td></td><td>&nbsp;</td></tr>"
    "<tr><td>Total: 3 Credits</td><td></td><td></td></tr>"
    "<tr><td colspan='3' style='background-color:#dbdbdb;'><strong>[1]</strong> Intensive English is "
    "not acceptable.</td></tr></tbody></table>")


def test_tables_become_rows_of_cells_joined_with_pipes():
    blocks = blocks_of(CURRICULUM_TABLE)
    assert [(b.kind, b.text, b.strong) for b in blocks] == [
        ("table_row", "Name | Title | Credits", True),              # header row kept
        ("table_row", "Foundations | Credits:", True),              # group header
        ("table_row", "FCWR 101 | Writing I: Foundations of College Composition [1] | 3", False),
        ("text", "Total: 3 Credits", False),                        # one-cell rows are text
        ("text", "[1] Intensive English is not acceptable.", False)]
    assert all(b.table == 1 for b in blocks)


def test_hidden_rows_empty_cells_and_javascript_links_do_not_leak():
    text = hp.blocks_to_text(blocks_of(CURRICULUM_TABLE))
    assert "hidden course description" not in text
    assert "javascript" not in text and "| |" not in text


def test_collapsed_course_description_rows_are_skipped():
    html = ("<table><tr><td>CSCI 260</td><td>Data Structures</td></tr>"
            "<tr class='hidecd'><td>CSCI 260</td><td>Long description repeated from the course list</td></tr></table>")
    assert texts(html) == ["CSCI 260 | Data Structures"]


def test_noise_is_removed():
    html = ("<p>Intro</p><span class='b2t'><a href='#top'>Back to Top</a></span>"
            "<script>var x = 1;</script><style>p {}</style><select class='courseDD'><option>CSCI</option></select>"
            "<a class='Button' href='/x'>Learn More</a><a class='Button' href='/y'>College of Arts and Sciences</a>"
            "<form><input name='q'></form><img src='a.png' alt='logo'><hr><p>After</p>"
            "<div style='display: none'>hidden</div><div hidden>also hidden</div>"
            "<nav class='Subnav'>Explore menu</nav><!-- a comment -->")
    assert texts(html) == ["Intro", "College of Arts and Sciences", "After"]


def test_entities_and_unicode_are_clean():
    assert texts("<p>A &amp; B&nbsp;&nbsp;C “quotes” – dash ﬁnal</p>") == ["A & B C “quotes” – dash final"]


def test_bold_only_line_is_a_pseudo_heading():
    blocks = blocks_of("<p><strong>First Year</strong><br>Some text here.</p>")
    assert [(b.text, b.strong) for b in blocks] == [("First Year", True), ("Some text here.", False)]


def test_headings_record_level_and_anchor_from_id_or_preceding_name():
    blocks = blocks_of("<a name='admissions'></a><h2>Admission Requirements</h2><p>Text.</p>"
                       "<h3 id='materials'>Application Materials</h3><p>More.</p>")
    headings = [(b.text, b.level, b.anchor) for b in blocks if b.kind == "heading"]
    assert headings == [("Admission Requirements", 2, "admissions"), ("Application Materials", 3, "materials")]


def test_sentences_formatted_as_headings_stay_text():
    blocks = blocks_of("<h3>Total Program Credits = 66</h3><h4>* Students must take one of the following</h4>")
    assert [b.kind for b in blocks] == ["text", "text"]


def test_walk_content_handles_none_and_counts_tables():
    assert hp.walk_content(None) == ([], 0)
    root = make_soup(f"<main>{CURRICULUM_TABLE}<table><tr><td>a</td><td>b</td></tr></table></main>").find("main")
    blocks, tables = hp.walk_content(root)
    assert tables == 2 and blocks[-1].text == "a | b" and blocks[-1].table == 2


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def test_sections_split_at_headings_with_paths_and_intro():
    blocks = blocks_of("<p>Intro text.</p><h2>A</h2><p>a text</p><h3>A1</h3><p>a1 text</p>"
                       "<h2>Empty</h2><h3>Child</h3><p>child text</p>")
    sections = hp.split_sections(blocks)
    assert [(s.heading, s.heading_path, s.text) for s in sections] == [
        ("", [], "Intro text."),
        ("A", ["A"], "a text"),
        ("A1", ["A", "A1"], "a1 text"),
        ("Child", ["Empty", "Child"], "child text"),         # empty section dropped, kept in path
    ]


def test_marker_anchor_with_a_short_note_moves_to_the_next_section():
    blocks = blocks_of("<h2>Overview</h2><p>Overview text that is long enough to stay here.</p>"
                       "<a name='admissions_x'></a><p>Short note.</p><h2>Admission Requirements</h2><p>Body.</p>")
    sections = hp.split_sections(blocks)
    assert sections[-1].heading == "Admission Requirements"
    assert sections[-1].anchor == "admissions_x"
    assert sections[-1].text.startswith("Short note.")


def _long_html(paragraphs: int = 30, words: int = 40) -> str:
    return "<h2>Long Policy</h2>" + "".join(
        "<p>" + " ".join(f"w{p}x{i}" for i in range(words)) + ".</p>" for p in range(paragraphs))


def test_long_sections_are_split_into_parts():
    blocks = blocks_of(_long_html())
    sections = hp.split_sections(blocks)
    assert len(sections) >= 3
    assert sections[0].heading == "Long Policy"
    assert [s.heading for s in sections[1:]] == [f"Long Policy (part {k})" for k in range(2, len(sections) + 1)]
    assert all(s.heading_path == ["Long Policy"] for s in sections)
    assert all(hp.PART_MIN_WORDS * 0.5 <= s.words <= hp.MAX_SECTION_WORDS for s in sections)
    # nothing lost, nothing repeated
    joined = " ".join(s.text for s in sections).split()
    assert joined == hp.blocks_to_text(blocks[1:]).split()


def test_a_single_huge_paragraph_is_split_at_sentence_ends():
    sentence = "This sentence has exactly eight words in it. "
    blocks = blocks_of("<h2>Wall</h2><p>" + sentence * 150 + "</p>")
    sections = hp.split_sections(blocks)
    assert len(sections) >= 3
    assert all(s.words <= hp.PART_MAX_WORDS for s in sections)
    assert all(s.text.endswith(".") for s in sections)


def test_long_tables_repeat_their_group_header_in_continued_parts():
    rows = "".join(f"<tr><td>CSCI {100 + i}</td><td>Course number {i} with a rather long descriptive "
                   f"title for testing purposes</td><td>3</td></tr>" for i in range(60))
    html = (f"<h2>Major Requirements</h2><table><tr><td><strong>Computer Science Core</strong></td>"
            f"<td>Credits:</td></tr>{rows}</table>")
    sections = hp.split_sections(blocks_of(html))
    assert len(sections) >= 2
    for part in sections[1:]:
        assert part.text.splitlines()[0] == "Computer Science Core | Credits:"
    all_rows = [ln for s in sections for ln in s.text.splitlines() if ln.startswith("CSCI")]
    assert len(all_rows) == 60                                  # rows are never cut


def test_section_records_ids_urls_and_metadata():
    page, sections = parse("catalog_program.html")
    assert [s.section_id for s in sections] == [f"{page.doc_id}-s{i:03d}" for i in range(len(sections))]
    assert [s.order for s in sections] == list(range(len(sections)))
    by_heading = {s.heading: s for s in sections}
    assert by_heading["Admission Requirements"].url == page.url + "#admissions_csbs_ecebs"
    assert by_heading["Academic Standards"].url == page.url + "#standards"
    assert by_heading["Application Materials"].heading_path == ["Admission Requirements", "Application Materials"]
    for s in sections:
        assert s.doc_id == page.doc_id and s.page_title == page.title
        assert (s.program, s.degree, s.level, s.school, s.site) == \
            (page.program, page.degree, page.level, page.school, "catalog.nyit.edu")
        assert s.text and s.word_count == word_count(s.text)
        assert not s.text.startswith(s.heading) or not s.heading


# ---------------------------------------------------------------------------
# whole pages (real cached pages in tests/fixtures)
# ---------------------------------------------------------------------------
def test_program_page():
    page, sections = parse("catalog_program.html")
    assert page.doc_id == "page-" + short_hash(page.url)
    assert page.title == "Computer Science, B.S."
    assert page.page_title.startswith("Computer Science, B.S. | College of Engineering")
    assert page.breadcrumbs == ["New York Tech Catalog 2026–2027", "Schools and Colleges",
                                "College of Engineering and Computing Sciences",
                                "Undergraduate Programs", "Computer Science, B.S."]
    assert page.catalog_year == "2026-2027"
    assert (page.page_type, page.area) == ("program", "Schools and Colleges")
    assert page.school == "College of Engineering and Computing Sciences"
    assert (page.program, page.degree, page.level) == ("Computer Science, B.S.", "B.S.", "undergraduate")
    assert page.campuses == ["Long Island", "New York City"]
    assert "Admission Requirements" in page.headings and "Academic Standards" in page.headings
    assert page.text.startswith("Full-time Faculty:")
    assert page.word_count == word_count(page.text) > 1000
    assert page.content_sha1 == sha1(page.text)
    assert page.fetched_at == "2026-11-23T07:00:00+00:00" and page.site == "catalog.nyit.edu"
    assert len(sections) == 9


def test_curriculum_page_tables():
    page, sections = parse("catalog_curriculum.html")
    assert page.page_type == "curriculum" and page.num_tables == 3
    assert (page.program, page.degree, page.level) == ("Computer Science, B.S.", "B.S.", "undergraduate")
    lines = page.text.splitlines()
    assert "Foundations | Credits:" in lines
    assert "FCWR 101 | Writing I: Foundations of College Composition [1] | 3" in lines
    assert "Total: 9 Credits" in lines
    assert any(ln.startswith("[1] Intensive English") for ln in lines)
    assert page.links == []                                      # only javascript: course links
    assert any(s.heading.endswith("(part 2)") for s in sections)


def test_semester_map_page_names_its_program():
    page, sections = parse("catalog_semester_map.html")
    assert page.title == "Semester Map: Computer Science, B.S."
    assert page.page_type == "curriculum"
    assert (page.program, page.degree, page.level) == ("Computer Science, B.S.", "B.S.", "undergraduate")
    assert page.school is None            # filled in later by the pipeline (fill_school_from_program)
    assert "Fall, Year One" in page.headings
    assert "CSCI 260 Data Structures | 3" in page.text.splitlines()
    assert {"text": "MATH 170", "url": CAT + "/courses?cal=UG&cs=MATH"} in page.links


def test_school_page():
    page, sections = parse("catalog_school.html")
    assert (page.title, page.page_type) == ("College of Engineering and Computing Sciences", "school")
    assert page.school == "College of Engineering and Computing Sciences"
    assert page.program is None and page.level is None
    assert "- Computer Science, B.S." in page.text.splitlines()
    assert {"text": "Computer Science, B.S.", "url": CAT + "/engineering/computer_science"} in page.links
    assert [s.heading for s in sections][:3] == ["Undergraduate Programs", "Graduate Programs", "Doctoral Programs"]


def test_course_listing_page_record():
    page, _ = parse("catalog_course_listing.html")
    assert page.page_type == "course_listing" and page.level == "undergraduate"
    assert page.title == "Computer Science"
    assert page.catalog_year == ""        # not shown on listing pages (the pipeline fills it in)


@pytest.mark.parametrize("name", ["catalog_school.html", "catalog_program.html", "catalog_curriculum.html",
                                  "catalog_semester_map.html", "catalog_course_listing.html"])
def test_catalog_text_hygiene(name):
    page, sections = parse(name)
    assert page.page_type in PAGE_TYPES
    for text in [page.title, page.text] + [s.text for s in sections] + [s.heading for s in sections]:
        assert not LEFTOVERS.search(text), LEFTOVERS.search(text)
        assert "  " not in text and "\n\n\n" not in text
    for link in page.links:
        assert link["url"].startswith("http") and "javascript" not in link["url"]


def test_soft_404_template_parses_to_an_empty_page():
    page, sections = hp.parse_page(fixture_html("catalog_soft404.html"), CAT + "/engineering/nope")
    assert page.text == "" and sections == [] and page.word_count == 0
    assert page.title == ""


def test_empty_and_broken_html_do_not_crash():
    for html in ("", "<html>", "<p>just a paragraph", "\x00\x01 binary garbage \ufffd"):
        page, sections = hp.parse_page(html, CAT + "/x")
        assert page.url == CAT + "/x" and page.page_type == "other"


def test_title_falls_back_to_the_title_tag_then_any_h1():
    page, _ = hp.parse_page("<title>Tuition | New York Tech Catalog 2026–2027</title><main><p>x</p></main>",
                            CAT + "/tuition")
    assert page.title == "Tuition" and page.catalog_year == "2026-2027" and page.page_type == "tuition"
    page, _ = hp.parse_page("<main><h1>Only an H1</h1><p>x</p></main>", CAT + "/x")
    assert page.title == "Only an H1"


# ---------------------------------------------------------------------------
# metadata helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, degree, level", [
    ("Computer Science, B.S.", "B.S.", "undergraduate"),
    ("Medical and Biological Sciences, D.O./Ph.D.", "D.O./Ph.D.", "professional"),
    ("Minor in Physics", "Minor", "undergraduate"),
    ("Physical Therapy, DPT", "D.P.T.", "doctoral"),
    ("Master of Business Administration", "M.B.A.", "graduate"),
    ("Doctor of Osteopathic Medicine", "D.O.", "professional"),
    ("Advanced Certificate in Energy Technology", "Advanced Certificate", "graduate"),
    ("Architecture, B.Arch.", "B.Arch.", "undergraduate"),
    ("Physician Assistant Studies, M.S.", "M.S.", "graduate"),
    ("Mathematics", None, None),
    ("", None, None),
])
def test_find_degree_and_level(text, degree, level):
    assert hp.find_degree(text) == degree
    assert hp.degree_level(degree) == level


@pytest.mark.parametrize("raw, name", [
    ("Curriculum Requirements for Bachelor of Science in Computer Science", "Computer Science, B.S."),
    ("Minor in Mathematics", "Mathematics, Minor"),
    ("Civil Engineering B.S.", "Civil Engineering, B.S."),
    ("Physical Therapy, DPT", "Physical Therapy, D.P.T."),
    ("Global Health Certificate", "Global Health, Certificate"),
    ("Master of Architecture", "Master of Architecture, M.Arch."),
    ("D.O. Program", "Doctor of Osteopathic Medicine, D.O."),
    ("Computer Science, B.S.", "Computer Science, B.S."),
    ("Something Else", "Something Else"),
])
def test_canonical_program_name(raw, name):
    assert hp.canonical_program_name(raw) == name


def test_catalog_year():
    assert hp.extract_catalog_year(["New York Tech Catalog 2026–2027"], "") == "2026-2027"
    assert hp.extract_catalog_year([], "X | New York Tech Catalog 2025-2026") == "2025-2026"
    assert hp.extract_catalog_year([], "", "2024-2025 Undergraduate Catalog") == "2024-2025"
    assert hp.extract_catalog_year(["New York Tech Catalog 2026–2028"], "") == ""   # not consecutive
    assert hp.extract_catalog_year([], "") == ""


def test_school_and_area():
    crumbs = ["New York Tech Catalog 2026–2027", "Schools and Colleges", "School of Management", "X"]
    assert hp.extract_school(CAT + "/engineering/x", []) == "College of Engineering and Computing Sciences"
    assert hp.extract_school(CAT + "/semester-map/x", crumbs) == "School of Management"
    assert hp.extract_school(CAT + "/x", ["Home", "School of Health Professions"]) == "School of Health Professions"
    assert hp.extract_school(CAT + "/x", ["Home"]) is None
    assert hp.extract_area(crumbs) == "Schools and Colleges"
    assert hp.extract_area(["Catalog", "College of Osteopathic Medicine"]) == "Schools and Colleges"
    assert hp.extract_area(["Catalog"]) is None


def test_find_campuses():
    assert hp.find_campuses("Classes in Manhattan and Old Westbury; also Jonesboro, Arkansas and Vancouver") == \
        ["Long Island", "New York City", "Jonesboro", "Vancouver"]
    assert hp.find_campuses("") == []


@pytest.mark.parametrize("url, crumbs, title, page_type", [
    (CAT + "/", [], "New York Tech Catalog", "home"),
    (CAT + "/courses", [], "Courses", "course_index"),
    (CAT + "/courses?cal=UG&cs=CSCI", [], "Computer Science", "course_listing"),
    (CAT + "/engineering/computer_science/curriculum", [], "Curriculum Requirements for X", "curriculum"),
    (CAT + "/semester-map/computer-science-bs", [], "Semester Map", "curriculum"),
    (CAT + "/engineering/faculty", [], "Faculty", "faculty"),
    (CAT + "/administration_and_faculty/trustees", [], "Trustees", "faculty"),
    (CAT + "/engineering", [], "College of Engineering and Computing Sciences", "school"),
    (CAT + "/engineering/undergraduate", [], "Undergraduate Programs", "program_list"),
    (CAT + "/schools_and_colleges", [], "Schools and Colleges", "program_list"),
    (CAT + "/engineering/computer_science", ["Catalog", "Schools and Colleges", "College",
                                              "Undergraduate Programs", "Computer Science, B.S."],
     "Computer Science, B.S.", "program"),
    (CAT + "/engineering/computer_science_phd", [], "Computer Science", "program"),
    (CAT + "/policies_and_rules/academic_policies", [], "Academic Policies", "policy"),
    (CAT + "/financial_aid_graduate", [], "Financial Aid", "financial_aid"),
    (CAT + "/tuition/fees", [], "Fees", "tuition"),
    (CAT + "/help/print", [], "Print", "help"),
    (CAT + "/student_services/counseling", [], "Counseling", "student_services"),
    (CAT + "/general_information/accreditation", [], "Accreditation", "general_information"),
    (CAT + "/medicine/application_procedure", [], "Application Procedure: D.O. Program", "general_information"),
    (CAT + "/somewhere", [], "Somewhere", "other"),
])
def test_classify_page(url, crumbs, title, page_type):
    assert hp.classify_page(url, crumbs, title) == page_type


def test_page_level():
    assert hp.page_level(CAT + "/medicine/x", [], None) == "professional"
    assert hp.page_level(CAT + "/financial_aid_graduate/x", [], None) == "graduate"
    assert hp.page_level(CAT + "/courses?cal=GR&cs=X", [], None) == "graduate"
    assert hp.page_level(CAT + "/x", ["Catalog", "Doctoral Programs"], None) == "doctoral"
    assert hp.page_level(CAT + "/x", [], "Ph.D.") == "doctoral"
    assert hp.page_level(CAT + "/x", [], None) is None


def test_extract_links_normalises_and_drops_non_pages():
    root = make_soup("<main><a href='/Engineering/'>Eng</a><a href='/engineering'></a>"
                     "<a href='mailto:x@nyit.edu'>mail</a><a href=\"javascript:getCD('X')\">X</a>"
                     "<a href='#top'>top</a><script><a href='/hidden'>h</a></script>"
                     "<a href='https://www.nyit.edu/about'>About</a></main>").find("main")
    assert hp.extract_links(root, CAT + "/") == [
        {"text": "Eng", "url": CAT + "/engineering"},
        {"text": "About", "url": "https://www.nyit.edu/about/"}]
    assert hp.extract_links(None, CAT) == []
