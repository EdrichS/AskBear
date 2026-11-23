"""Regression tests for the HTML-extraction fixes of review 5: headings that are text,
the LibGuides author profile box, the faculty & staff accomplishments (literal "\\n",
item dates) and the image numbers at the start of a LiveWhale event description."""

from __future__ import annotations

import pytest

from askbear_scraper import site_parser as sp
from askbear_scraper.html_parser import Block


def libguide(boxes: str) -> str:
    return ("<html><head><title>Finding Articles: Home - LibGuides at New York Institute of Technology</title>"
            "</head><body><div id='s-lg-guide-main'><h1 id='s-lg-guide-name'>Finding Articles</h1>"
            f"{boxes}</div></body></html>")


def box(title: str, content: str) -> str:
    return (f"<div class='s-lib-box s-lib-box-std'><h2 class='s-lib-box-title'>{title}</h2>"
            f"<div class='s-lib-box-content'>{content}</div></div>")


# ---------------------------------------------------------------------------
# headings followed directly by another heading
# ---------------------------------------------------------------------------
STEP1 = "Step 1. Locate the name of the PUBLICATION in your citation (underlined in blue)."


def test_sentence_headings_followed_by_a_heading_become_text():
    html = libguide(
        box(STEP1, "<img src='step1.png' alt=''>")
        + box("Step 2. Locate the Journal Locator on the Library homepage.",
              "<p>Type the title of the journal and press Search to see where it is held.</p>")
        + box("Live Chat!", "<div class='s-lib-widget'></div>")
        + box("Search Tips", "<p>Search for the journal title, not the title of the article.</p>")
        + box("Step 5. Access your article.", "<img src='step5.png' alt=''>")
        + box("RSS Feed / The New York Times: Science Times", "<div class='s-lg-rss'><ul><li>x</li></ul></div>"))
    rec, sections = sp.parse_any(html, "https://libguides.nyit.edu/c.php?g=139233&p=911728")
    lines = rec.text.splitlines()
    assert STEP1 in lines and "Step 5. Access your article." in lines
    # in a passage, not only in the page text (a heading without body text has no section)
    assert any(STEP1 in s.text for s in sections)
    assert any("Step 5. Access your article." in s.text for s in sections)
    for widget in ("Live Chat!", "RSS Feed"):
        assert widget not in rec.text


def test_panelists_styled_as_headings_are_kept():
    body = ("<div class='blog-item-detail'><div class='blog-item-header'><h2>Digital Feudalism</h2></div>"
            "<h3>NOVEMBER 3, 2021</h3><h4>WELCOME:</h4><h3>Chair Giovanni Santamaria</h3>"
            "<h4>INTRODUCTIONS AND MODERATION:</h4><h3>Associate Prof. Pablo Lorenzo-Eiroa</h3>"
            "<h3>Assistant Prof. Dong Sei Kim</h3><h4>PANELISTS:</h4>"
            "<h3>“Insecurities” Sean Anderson, Undergraduate Program Director and Associate Professor, "
            "Cornell AAP</h3><h3>The Family, Re-appropriation of Housing in NYC, Jana Leo</h3>"
            "<p>The map is the continuation of the territory by other means.</p></div>")
    html = f"<html><head><title>Digital Feudalism | MS ACT | Blogs NYIT</title></head><body>{body}</body></html>"
    rec, sections = sp.parse_any(html, "https://blogs.nyit.edu/msact/digital_feudalism_lecture")
    for kept in ("Associate Prof. Pablo Lorenzo-Eiroa", "PANELISTS:",
                 "“Insecurities” Sean Anderson, Undergraduate Program Director and Associate Professor, "
                 "Cornell AAP"):
        assert kept in rec.text.splitlines()
        assert any(kept in s.text for s in sections)


def test_empty_title_headings_are_still_dropped_and_tiles_still_listed():
    blocks = [Block("heading", "Contact Information", level=2), Block("heading", "Quick Links", level=2),
              Block("text", "Call the help desk at extension 4357 for any question."),
              Block("heading", "How-to Videos:", level=4), Block("heading", "Resources", level=2),
              Block("heading", "CFE Weekly Teaching Notes", level=3), Block("heading", "Build Ahead", level=3),
              Block("heading", "Teaching Tips", level=3)]
    out = sp._tidy_headings(blocks)
    assert [(b.kind, b.text) for b in out] == [
        ("heading", "Quick Links"), ("text", "Call the help desk at extension 4357 for any question."),
        ("heading", "Resources"), ("list_item", "- CFE Weekly Teaching Notes"), ("list_item", "- Build Ahead"),
        ("list_item", "- Teaching Tips")]


@pytest.mark.parametrize("text, kept", [
    ("Step 5. Access your article.", True),
    ("Associate Prof. Pablo Lorenzo-Eiroa", True),
    ("File Name: LastName_FirstName.pdf", True),
    ("2. Measuring building performance", True),
    ("Open Data and Municipal Data Policy and Platforms", True),
    ("Live Chat!", False),
    ("New To OER?", False),
    ("Contact Information", False),
    ("RSS Feed / Science Magazine / A.A.A.S.", False),
    ("New Books at Art & Architecture Library in 2026", False),
    ("Follow: @nyitlibraries and @nyitart_arch_library", False),
    ("2026 Commencement", False),
])
def test_is_text_heading(text, kept):
    assert sp._is_text_heading(Block("heading", text, level=2)) is kept


# ---------------------------------------------------------------------------
# LibGuides author profile box
# ---------------------------------------------------------------------------
def profile(name: str, email: str = "", contact: str = "", subjects: str = "") -> str:
    return ("<div class='s-lib-profile-container' id='s-lib-profile-409761'>"
            "<a href='//libguides.nyit.edu/prf.php?id=1'>"
            "<div class='s-lib-profile-div s-lib-profile-center s-lib-profile-image'><img alt='' src='p.jpg'></div>"
            f"<div class='s-lib-profile-div s-lib-profile-center s-lib-profile-name'>{name}</div></a>"
            "<div class='s-lib-profile-div s-lib-profile-center s-lib-profile-pronouns'>She/Her</div>"
            "<div class='s-lib-profile-div s-lib-profile-center s-lib-profile-email'>"
            + (f"<a class='label label-info btn' href='mailto:{email}' title='{email}'>Email Me</a>" if email else "")
            + "</div><div class='s-lib-profile-div s-lib-profile-widget-lc'><div class='s-lib-profile-widget'>"
            "<script>jQuery.getScript('https://libcal.nyit.edu/js/myscheduler.min.js');</script>"
            "<button id='mysched_138795'>Schedule Appointment</button></div></div>"
            f"<div class='s-lib-profile-div s-lib-profile-contact'>{contact}</div>"
            "<div class='s-lib-profile-div s-lib-profile-social'>Social: "
            "<a href='https://twitter.com/x'>Twitter Page</a></div>"
            f"<div class='s-lib-profile-div s-lib-profile-subjects'>{subjects}</div></div>")


HARTUP = profile("Catherine Hartup", "chartup@nyit.edu",
                 "<strong>Contact:</strong> <div>Manhattan Library <br/>1855 Broadway, 2nd Fl.<br/>"
                 "New York, NY 10023</div>"
                 "<div>646-273-6017</div>",
                 "<strong>Subjects:</strong> <a href='/sb.php?subject_id=1'>Architecture, Design, Fine Art</a>, "
                 "<a href='/sb.php?subject_id=2'>Nursing</a>")
SPHERE = "<p>Open the Street View app and tap the camera button to start a new photo sphere.</p>"


def test_libguides_profile_box_is_a_section_of_its_own():
    html = libguide(box("Create 360° photos", SPHERE) + box("Instruction and Reference Librarian", HARTUP))
    rec, sections = sp.parse_any(html, "https://libguides.nyit.edu/accessibility")
    assert sections[0].text == "Open the Street View app and tap the camera button to start a new photo sphere."
    prof = sections[-1]
    assert prof.heading == "Librarian: Catherine Hartup" and prof.heading_path == ["Librarian: Catherine Hartup"]
    assert prof.text.splitlines() == [
        "Name: Catherine Hartup", "Job title: Instruction and Reference Librarian",
        "Subjects: Architecture, Design, Fine Art, Nursing", "Email: chartup@nyit.edu", "Phone: 646-273-6017",
        "Address: Manhattan Library, 1855 Broadway, 2nd Fl., New York, NY 10023"]
    for noise in ("Email Me", "Schedule Appointment", "Contact:", "She/Her", "Twitter", "jQuery"):
        assert noise not in rec.text


def test_library_account_profile_is_long_enough_to_stay_a_passage():
    # "Librarian" / "NYIT Library" alone (2 words) was merged into the end of the passage before it
    account = profile("NYIT Library", "askalibrarian@nyit.edu")
    html = libguide(box("Create 360° photos", SPHERE) + box("Librarian", account))
    rec, sections = sp.parse_any(html, "https://libguides.nyit.edu/360_Degree_Pics")
    assert [s.heading for s in sections] == ["Create 360° photos", "Library contact: NYIT Library"]
    assert sections[1].text == "Name: NYIT Library\nEmail: askalibrarian@nyit.edu"
    assert sections[1].word_count >= 5           # pipeline.MIN_SECTION_WORDS: not merged into its neighbour
    assert "Librarian" not in sections[0].text and "NYIT Library" not in sections[0].text


def test_profile_lines_are_never_boilerplate_and_intro_lines_before_it_stay():
    body = box("Searching", "<p>Check the peer-reviewed box on the search screen of each database.</p>"
                            "<p>In EBSCOhost databases:</p><img src='ebsco.png' alt=''>"
                            "<p>In ProQuest databases:</p><img src='proquest.png' alt=''>")
    html = libguide(body + box("Librarian", HARTUP))
    url = "https://libguides.nyit.edu/c.php?g=61912&p=398265"
    keys = {"libguides.nyit.edu": {sp.block_key("Name: Catherine Hartup"), sp.block_key("Email: chartup@nyit.edu")}}
    rec, sections = sp.parse_any(html, url, boilerplate=keys)
    assert sections[0].text.endswith("In EBSCOhost databases:\nIn ProQuest databases:")
    assert sections[1].heading == "Librarian: Catherine Hartup"
    assert "Name: Catherine Hartup" in sections[1].text and "Email: chartup@nyit.edu" in sections[1].text
    assert "Job title: Librarian" in sections[1].text


# ---------------------------------------------------------------------------
# faculty & staff accomplishments
# ---------------------------------------------------------------------------
def card(name: str, body: str, date: str) -> str:
    return (f"<div class='Card Card--alt'><div class='Card__main'><div class='Card__content flush'>"
            f"<h3>{name}</h3><strong>School of Health Professions Nursing</strong>{body}</div>"
            f"<div class='Card__footer'><div class='Card__meta underline'>"
            f"<a href='#' class='type-filter-link' data-type='Publication'>Publication</a></div>"
            f"<div class='Card__date'>{date}</div></div></div></div>")


def www_page(body: str, title: str) -> str:
    return (f"<html><head><title>{title} | New York Tech</title></head><body class='page'><main>"
            f"<div class='Hero'><h1 class='Hero__heading'>{title}</h1></div>{body}</main></body></html>")


def test_accomplishments_keep_their_date_and_lose_literal_newlines():
    cards = (card("Sheldon Fields", "<p><strong>Sheldon D. Fields, Ph.D.</strong>, professor of nursing, "
                                    "co-authored an article,\\n<a href='x'>\"Building effective multilevel HIV "
                                    "prevention partnerships with\\n\"Black men who have sex with men\"</a> "
                                    "published on October 21, 2018.</p>\\n\\n", "10.21.18")
             + card("Milan Toma", "<p>Milan Toma, Ph.D., was interviewed for an article published in "
                                  "<a href='y'>\\nTrainer Magazine</a>.</p>", "03.02.21"))
    rec, sections = sp.parse_any(www_page(cards, "Faculty & Staff Accomplishments"),
                                 "https://www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=104")
    assert "\\n" not in rec.text
    assert "co-authored an article, \"Building effective multilevel HIV prevention partnerships with \"Black" \
        in rec.text
    assert "published in Trainer Magazine." in rec.text
    toma = next(s for s in sections if s.heading == "Milan Toma")
    assert toma.text.splitlines()[0] == "March 2, 2021"
    assert "October 21, 2018" in next(s for s in sections if s.heading == "Sheldon Fields").text
    assert "03.02.21" not in rec.text and "Publication" not in rec.text


def test_teaser_card_dates_are_still_removed_elsewhere():
    cards = card("Nursing Students Win Award", "<p>Our nursing students won the state simulation award "
                                               "this spring.</p>", "03.02.21")
    rec, _ = sp.parse_any(www_page(cards, "Nursing News"), "https://www.nyit.edu/health-professions/news/")
    assert "03.02.21" not in rec.text and "March 2, 2021" not in rec.text
    assert "won the state simulation award" in rec.text


def test_literal_newline_of_a_windows_path_is_kept():
    assert sp._LITERAL_NEWLINE.sub(" ", r"Save it to C:\new\files today") == r"Save it to C:\new\files today"
    assert sp._LITERAL_NEWLINE.sub(" ", r"an article,\n\n\n see") == "an article, see"


# ---------------------------------------------------------------------------
# LiveWhale event description
# ---------------------------------------------------------------------------
def test_event_description_loses_the_leading_image_number():
    desc = "  1141280720                    This lecture launches the newly published book."
    assert sp._event_text(desc) == "This lecture launches the newly published book."
    assert sp._event_text("2026 Commencement will be held on May 17.") == \
        "2026 Commencement will be held on May 17."
