"""site_parser.py: every non-catalog NYIT website (real cached pages in tests/fixtures)."""

from __future__ import annotations

import re

import pytest

from askbear_scraper import site_parser as sp
from askbear_scraper.models import PAGE_TYPES

from testkit import fixture_html, fixture_url

NOISE = re.compile(r"request information|apply today|plan your visit|back to top|was this helpful|"
                   r"&amp;|&nbsp;|<br|javascript:|�", re.I)


def parse(name: str, **kwargs):
    return sp.parse_any(fixture_html(name), fixture_url(name), "2026-11-23T07:00:00+00:00", **kwargs)


# ---------------------------------------------------------------------------
# routing by host
# ---------------------------------------------------------------------------
def test_catalog_pages_go_to_the_catalog_parser():
    page, sections = parse("catalog_program.html")
    assert page.site == "catalog.nyit.edu" and page.catalog_year == "2026-2027"
    assert page.page_type == "program" and all(s.site == "catalog.nyit.edu" for s in sections)


def test_catalog_noindex_comes_from_the_manifest_entry():
    page, _ = parse("catalog_program.html", entry={"noindex": True})
    assert page.noindex


def test_the_url_host_decides_the_parser_not_the_html():
    # catalog HTML served by a generic host is parsed generically (no catalog year)
    page, _ = sp.parse_any(fixture_html("catalog_program.html"), "https://newly-found.nyit.edu/cs")
    assert page.site == "newly-found.nyit.edu" and page.catalog_year == ""
    assert page.page_type in PAGE_TYPES


@pytest.mark.parametrize("name, site, page_type", [
    ("www_home.html", "www.nyit.edu", "home"),
    ("www_degree.html", "www.nyit.edu", "degree"),
    ("www_news.html", "www.nyit.edu", "news"),
    ("www_bio.html", "www.nyit.edu", "bio"),
    ("vancouver_page.html", "vancouver.nyit.edu", "page"),
    ("apps_policy.html", "apps.nyit.edu", "policy"),
    ("libanswers_faq.html", "libanswers.nyit.edu", "faq"),
    ("libguides_guide.html", "libguides.nyit.edu", "library_guide"),
    ("events_event.html", "events.nyit.edu", "event"),
    ("magazine_article.html", "magazine.nyit.edu", "magazine_article"),
    ("blogs_post.html", "blogs.nyit.edu", "blog_post"),
    ("online_page.html", "online.nyit.edu", "page"),
])
def test_page_types_and_sites(name, site, page_type):
    page, sections = parse(name)
    assert (page.site, page.page_type) == (site, page_type)
    assert page.catalog_year == ""
    assert page.text and page.word_count > 50
    assert sections and all(s.site == site and s.page_type == page_type for s in sections)
    assert [s.section_id for s in sections] == [f"{page.doc_id}-s{i:03d}" for i in range(len(sections))]
    for text in [page.title, page.text] + [s.text for s in sections]:
        assert not NOISE.search(text), (name, NOISE.search(text))


# ---------------------------------------------------------------------------
# titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, title", [
    ("www_home.html", "New York Institute of Technology"),
    ("www_degree.html", "Technology Entrepreneurship Minor"),
    ("www_news.html", "NYIT Ranks Well Among Best Engineering Programs in U.S."),
    ("www_bio.html", "Amin Milani Fard"),                       # h1 says "Staff & Faculty Directory"
    ("vancouver_page.html", "Student Employment"),
    ("apps_policy.html", "Technical Standards for Students with Disabilities"),
    ("libanswers_faq.html", "How can I find business case studies?"),
    ("libguides_guide.html", "Library Student Ambassador Program"),
    ("events_event.html", "Graduate Admissions Info Session | Spotlight: College of Engineering and Computing Sciences"),
    ("magazine_article.html", "Alumni Connections - 2026 Spring"),
    ("blogs_post.html", "Research Activities: Chris Douvris, Ph.D., Associate Professor"),
    ("online_page.html", "Online Master’s Tuition and Financial Aid"),
])
def test_titles(name, title):
    page, _ = parse(name)
    assert page.title == title
    assert page.title.casefold() not in sp.GENERIC_TITLES or name == "www_home.html"


def test_bio_page_never_uses_the_directory_heading():
    page, sections = parse("www_bio.html")
    assert "Staff & Faculty Directory" in fixture_html("www_bio.html")
    assert page.title == "Amin Milani Fard"
    assert all(s.page_title == "Amin Milani Fard" for s in sections)
    assert page.school == "College of Engineering and Computing Sciences"
    assert page.breadcrumbs == ["Home", "Directory", "Amin Milani Fard"]
    assert page.text.startswith("Associate Professor; Computer Science")


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------
def test_wordpress_dates_description_and_breadcrumbs():
    page, _ = parse("www_news.html")
    assert page.published == "2018-09-24T15:50:00+00:00"
    assert page.modified == "2026-08-31T02:54:53+00:00"
    assert page.description.startswith("NYIT engineering rose to No. 35 nationally")
    assert page.breadcrumbs == ["Home", "New York Tech News", "Articles",
                                "NYIT Ranks Well Among Best Engineering Programs in U.S."]
    assert page.area == "New York Tech News"
    assert page.level is None                     # an article is not about one level of study
    assert {"text": "NYIT’s undergraduate engineering programs",
            "url": "https://catalog.nyit.edu/engineering/undergraduate"} in page.links


def test_degree_page_program_school_and_snapshot():
    page, sections = parse("www_degree.html")
    assert (page.program, page.degree, page.level) == ("Technology Entrepreneurship, Minor", "Minor", "undergraduate")
    assert page.school == "College of Engineering and Computing Sciences"
    assert page.text.splitlines()[0] == "15 Credits | Locations: New York, NY, Long Island, NY"
    assert sections[1].url.endswith("#h-why-minor-in-technology-entrepreneurship-at-new-york-tech")
    assert sections[1].heading == "Why Minor in Technology Entrepreneurship at New York Tech?"


def test_home_page_sections_link_to_heading_anchors():
    page, sections = parse("www_home.html")
    assert page.campuses == ["Long Island", "New York City", "Jonesboro", "Vancouver"]
    assert [s.url for s in sections] == [
        "https://www.nyit.edu/#h-find-your-program", "https://www.nyit.edu/#h-meet-our-doers-makers-amp-innovators",
        "https://www.nyit.edu/#h-world-improving-research", "https://www.nyit.edu/#h-where-we-are"]


def test_event_metadata():
    page, sections = parse("events_event.html")
    # when the event starts: PageRecord.event_start, or `published` while models.py has no
    # event fields (see site_parser._page_dates)
    assert (getattr(page, "event_start", None) or page.published) == "2026-11-30T13:00:00-05:00"
    assert page.modified is None
    assert page.school == "College of Engineering and Computing Sciences"     # named in the title
    assert page.level == "graduate"
    assert page.links == []
    lines = page.text.splitlines()
    assert lines[0].startswith("Explore New York Institute of Technology’s graduate programs")
    assert lines[1] == "When: Monday, November 30, 2026, 1 p.m. to 2 p.m."
    assert len(sections) == 1


def test_library_pages():
    faq, _ = parse("libanswers_faq.html")
    assert faq.modified == "2024-01-31" and faq.level is None
    assert faq.text.startswith("How do I find business case studies at NYIT Library?")
    guide, sections = parse("libguides_guide.html")
    assert guide.modified == "2024-06-12"
    assert guide.headings == ["Library Ambassador Program", "Benefits", "Responsibilities", "Requirements"]
    assert sections[1].url == "https://libguides.nyit.edu/Libraryambassador#s-lg-box-wrapper-34333628"


def test_magazine_blog_online_and_policy_pages():
    mag, _ = parse("magazine_article.html")
    assert mag.breadcrumbs == [] and mag.published == "2026-03-01"     # the spring2026 issue
    blog, _ = parse("blogs_post.html")
    assert blog.published == "2019-09-11" and blog.area == "Arts & Sciences"
    online, sections = parse("online_page.html")
    assert online.breadcrumbs == ["Home", "Admissions", "Tuition & Financial Aid"]
    assert "Online Data Science, M.S. | Online Artificial Intelligence, M.S." in online.text.splitlines()
    assert any(s.heading_path == ["Types of Aid", "Loan Options"] for s in sections)
    policy, _ = parse("apps_policy.html")
    assert policy.breadcrumbs == ["New York Tech", "Policies & Guidelines",
                                  "Technical Standards for Students with Disabilities"]
    assert policy.area == "Policies & Guidelines"
    assert policy.description is None          # apps.nyit.edu copies the homepage's <meta> tags


def test_vancouver_pages_always_name_their_campus():
    page, _ = sp.parse_any("<html><head><title>Fees | New York Tech - Vancouver</title></head>"
                           "<body><main><h1>Fees</h1><p>Tuition and fees for the program.</p></main></body></html>",
                           "https://vancouver.nyit.edu/fees/")
    assert page.campuses == ["Vancouver"] and page.title == "Fees"


def test_noindex_from_the_entry_or_the_meta_tag():
    html = ("<html><head><title>Hidden | New York Tech</title><meta name='robots' content='noindex, follow'>"
            "</head><body><main><h1>Hidden</h1><p>Some hidden text for nobody.</p></main></body></html>")
    page, _ = sp.parse_any(html, "https://www.nyit.edu/hidden/")
    assert page.noindex
    page, _ = sp.parse_any(html, "https://www.nyit.edu/hidden/", entry={"noindex": False})
    assert not page.noindex                    # the crawler's verdict wins
    page, _ = sp.parse_any(fixture_html("www_news.html"), fixture_url("www_news.html"), entry={"noindex": True})
    assert page.noindex


def test_not_found_page_served_with_http_200_has_no_text():
    html = ("<html><head><title>404 | Build Ahead | Blogs NYIT</title></head><body><main>"
            "<h1>404</h1><p>Sorry, nothing here.</p></main></body></html>")
    page, sections = sp.parse_any(html, "https://blogs.nyit.edu/build_ahead/missing")
    assert page.page_type == "other" and page.text == "" and sections == []


def test_wordpress_body_classes_give_the_page_type():
    def page_type(body_class, path="/news/x/"):
        html = (f"<html><head><title>X | New York Tech</title></head><body class='{body_class}'><main>"
                "<h1>X</h1><p>Some text of this page that is long enough.</p></main></body></html>")
        return sp.parse_any(html, "https://www.nyit.edu" + path)[0].page_type

    assert page_type("single single-post") == "news"
    assert page_type("single single-bio") == "bio"
    assert page_type("single-media_coverage") == "media_coverage"
    assert page_type("single-expert_insight") == "expert_insight"
    assert page_type("page page-id-5") == "page"
    assert page_type("page", "/academics/degrees/data-science-ms/") == "degree"
    assert page_type("page", "/academics/degrees/data-science-ms/curriculum/") == "curriculum"
    assert page_type("page", "/academics/management/") == "school"


def test_link_hubs_are_listings():
    links = "".join(f"<li><a href='/p{i}/'>Office number {i} of the university</a></li>" for i in range(20))
    html = (f"<html><head><title>Offices | New York Tech</title></head><body class='page'><main>"
            f"<h1>Offices</h1><ul>{links}</ul></main></body></html>")
    page, _ = sp.parse_any(html, "https://www.nyit.edu/about/offices/")
    assert page.page_type == "listing"


# ---------------------------------------------------------------------------
# boilerplate
# ---------------------------------------------------------------------------
CTA = "Join thousands of students who chose New York Tech for a career-ready education today."


def generic_page(i: int, extra: str = "") -> str:
    return (f"<html><head><title>Page {i} | New York Tech</title>"
            f"<meta name='description' content='The same generic description of every page.'></head>"
            f"<body class='page'><main><h1>Page {i}</h1>"
            f"<p>Unique text number {i} about topic {i} with enough words to matter here.</p>"
            f"<p>{CTA}</p>{extra}</main></body></html>")


def test_find_boilerplate_learns_repeated_blocks_per_site():
    pages = [(f"https://www.nyit.edu/p{i}/", generic_page(i)) for i in range(6)]
    pages += [(f"https://catalog.nyit.edu/p{i}", generic_page(i)) for i in range(6)]   # never analysed
    found = sp.find_boilerplate(pages, min_pages=5, min_share=0.5)
    assert set(found) == {"www.nyit.edu"}
    assert sp.block_key(CTA) in found["www.nyit.edu"]
    assert sp.DESCRIPTION_KEY + sp.block_key("The same generic description of every page.") in found["www.nyit.edu"]
    assert not any("unique text" in k for k in found["www.nyit.edu"])
    assert sp.find_boilerplate(pages[:3], min_pages=5) == {}         # too few pages to tell


def test_boilerplate_is_dropped_from_the_page_text():
    pages = [(f"https://www.nyit.edu/p{i}/", generic_page(i)) for i in range(6)]
    boilerplate = sp.find_boilerplate(pages, min_pages=5, min_share=0.5)
    url, html = pages[0]
    with_bp, _ = sp.parse_any(html, url, boilerplate=boilerplate)
    without_bp, _ = sp.parse_any(html, url)
    assert CTA in without_bp.text and CTA not in with_bp.text
    assert "Unique text number 0" in with_bp.text
    assert without_bp.description and with_bp.description is None     # the site's generic description
    # a JSON round trip turns the sets into lists: still accepted
    as_lists = {k: sorted(v) for k, v in boilerplate.items()}
    assert sp.parse_any(html, url, boilerplate=as_lists)[0].text == with_bp.text


def test_boilerplate_is_never_applied_to_the_catalog():
    html, url = fixture_html("catalog_curriculum.html"), fixture_url("catalog_curriculum.html")
    line = "FCWR 101 | Writing I: Foundations of College Composition [1] | 3"
    page, _ = sp.parse_any(html, url, boilerplate={"catalog.nyit.edu": {sp.block_key(line)}})
    assert line in page.text.splitlines()


def test_cta_and_navigation_blocks_of_the_nyit_theme_are_removed():
    html = ("<html><head><title>Admissions | New York Tech</title></head><body class='page'><main>"
            "<div class='Hero'><h1>Admissions</h1><nav class='Breadcrumbs'><div class='yoast-breadcrumbs'>"
            "<a href='/'>Home</a> &gt; Admissions</div></nav></div>"
            "<nav class='Subnav'>Explore Explore Admissions Apply</nav>"
            "<div class='Common'><p>Our admissions team reviews every application with care.</p>"
            "<p><a href='/news/'>Read More</a></p></div>"
            "<div class='CTA'><h2>Keep Exploring</h2><div class='wp-block-buttons'>"
            "<a href='/rfi/'>Request Information</a><a href='/visit/'>Plan Your Visit</a></div></div>"
            "</main></body></html>")
    page, sections = sp.parse_any(html, "https://www.nyit.edu/admissions/")
    assert page.title == "Admissions" and page.breadcrumbs == ["Home", "Admissions"]
    assert page.text == "Our admissions team reviews every application with care."
    assert len(sections) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page_title, title", [
    ("Research Facilities | New York Tech", "Research Facilities"),
    ("First Byte - 2025 Spring - NYIT Magazine", "First Byte - 2025 Spring"),
    ("Student Employment | New York Tech - Vancouver", "Student Employment"),
    ("How can I find X? - Ask a Librarian", "How can I find X?"),
    ("Build the Relevance | CFE Weekly Teaching Notes | Blogs NYIT", "Build the Relevance"),
    ("X | Events Calendar | New York Tech", "X"),
    ("", ""),
])
def test_title_from_page_title(page_title, title):
    assert sp.title_from_page_title(page_title) == title


@pytest.mark.parametrize("value, plain, with_month", [
    ("2024-05-15T19:33:00+00:00", "2024-05-15T19:33:00+00:00", "2024-05-15T19:33:00+00:00"),
    ("2024-05-15 10:00", "2024-05-15T10:00", "2024-05-15T10:00"),
    ("May 15, 2024", "2024-05-15", "2024-05-15"),
    ("15 Sep 2026", "2026-09-15", "2026-09-15"),
    ("Sept. 3rd, 2021", "2021-09-03", "2021-09-03"),
    ("News Staff | May 15, 2024", "2024-05-15", "2024-05-15"),
    ("September 2024", None, "2024-09"),
    ("Feb 30, 2024", None, None),
    ("nonsense", None, None),
    (None, None, None),
])
def test_iso_date(value, plain, with_month):
    assert sp.iso_date(value) == plain
    assert sp.iso_date(value, allow_month=True) == with_month


def test_event_when():
    assert sp.event_when("2026-12-01T17:45:00Z", "2026-12-01T18:45:00Z", "2026-12-01T12:45:00-05:00") == \
        "Tuesday, December 1, 2026, 12:45 p.m. to 1:45 p.m."
    assert sp.event_when("2026-12-01T05:00:00Z", "2026-12-01T05:00:00Z", "2026-12-01T00:00:00-05:00") == \
        "Tuesday, December 1, 2026"                                   # all-day event
    assert sp.event_when("2026-12-01T17:45:00Z", "2026-12-02T18:45:00Z", "2026-12-01T12:45:00-0500") == \
        "Tuesday, December 1, 2026, 12:45 p.m. to Wednesday, December 2, 2026, 1:45 p.m."
    assert sp.event_when("2026-12-01T17:45:00Z", None, None) == "Tuesday, December 1, 2026"
    assert sp.event_when(None, None, None) == ""


def test_read_json_ld_repairs_livewhale_quirks():
    soup = sp.make_soup(
        '<script type="application/ld+json">/*<![CDATA[*/{"@type": "Event", "name": "A",'
        ' "startDate": "2026-12-01T17:45:00Z",}/*]]>*/</script>'
        '<script type="application/ld+json">{"@graph": [{"@type": "WebPage", "datePublished": "2024-01-02"},'
        ' {"@type": "Person"}]}</script>'
        '<script type="application/ld+json">{ not json at all </script>')
    items = sp.read_json_ld(soup)
    assert [i.get("@type") for i in items] == ["Event", None, "WebPage", "Person"]
    assert items[0]["startDate"] == "2026-12-01T17:45:00Z"


def test_read_meta_first_value_wins():
    soup = sp.make_soup('<meta property="og:title" content="First"><meta property="og:title" content="Second">'
                        '<meta name="Description" content=" A page. "><meta name="empty" content="">')
    assert sp.read_meta(soup) == {"og:title": "First", "description": "A page."}


@pytest.mark.parametrize("slug, degree", [
    ("artificial-intelligence-ms", "M.S."), ("master-of-cybersecurity-ms-online", "M.S."),
    ("energy-management-advanced-certificate", "Advanced Certificate"), ("architecture-barch", "B.Arch."),
    ("technology-entrepreneurship-minor", "Minor"), ("data-science-mds", "M.D.S."), ("foo-bar", None),
])
def test_slug_degree(slug, degree):
    assert sp.slug_degree(slug) == degree


def test_schools_from_urls_and_text():
    assert sp.school_from_url("https://www.nyit.edu/academics/engineering-and-computing-sciences/x/") == \
        "College of Engineering and Computing Sciences"
    assert sp.school_from_url("https://www.nyit.edu/medicine/x/") == "College of Osteopathic Medicine"
    assert sp.school_from_url("https://catalog.nyit.edu/engineering") is None
    assert sp.school_from_url("https://www.nyit.edu/about/") is None
    assert sp.school_in_text("Associate Professor; Computer Science, College of Engineering & Computing "
                             "Sciences") == "College of Engineering and Computing Sciences"
    assert sp.school_in_text("School of Management and College of Arts and Sciences") is None


def test_useful_description_and_block_key():
    assert sp.useful_description("A real description of the page.") == "A real description of the page."
    for junk in ("Lionel", "Loading...", "Subscribe * indicates required Email Address", "-----", None, ""):
        assert sp.useful_description(junk) is None
    assert sp.block_key("- Request Information.") == "request information"
    assert sp.block_key("1. Apply Today") == "apply today"


def test_detect_template_by_markup_on_unknown_hosts():
    wp = sp.make_soup("<body class='wp-theme-nyit page'><main><div class='Hero'></div></main></body>")
    assert sp.detect_template("new.nyit.edu", wp).name == "nyit"
    guide = sp.make_soup("<body><div id='s-lg-guide-main'></div></body>")
    assert sp.detect_template("guides.nyit.edu", guide).name == "libguides"
    assert sp.detect_template("other.nyit.edu", sp.make_soup("<p>x</p>")).name == "generic"
    assert sp.detect_template("www.nyit.edu", sp.make_soup("<p>x</p>")).name == "nyit"


def test_empty_html_does_not_crash():
    for html in ("", "<html><body></body></html>", "<p>only text</p>"):
        page, sections = sp.parse_any(html, "https://newly-found.nyit.edu/x")
        assert page.site == "newly-found.nyit.edu" and page.page_type in PAGE_TYPES
