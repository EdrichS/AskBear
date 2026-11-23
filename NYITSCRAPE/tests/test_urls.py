"""urls.py: per-host URL normalisation, crawl scope (link_kind) and cache file names."""

from __future__ import annotations

import re

import pytest

from askbear_scraper import urls
from askbear_scraper.urls import cache_filename, is_crawlable, link_kind, normalize_url

CATALOG = "https://catalog.nyit.edu"


# ---------------------------------------------------------------------------
# catalog.nyit.edu: case-insensitive pages, no trailing slash, only cs/cal kept
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("href", [
    "/engineering/computer_science",
    "/engineering/computer_science/",
    "/Engineering/Computer_Science",
    " /engineering/computer_science",          # the menus put a space before the path
    "http://catalog.nyit.edu/engineering/computer_science#admissions",
    "https://CATALOG.nyit.edu/engineering//computer_science",
    "/engineering/./other/../computer_science",
])
def test_catalog_spellings_of_one_page_normalise_to_the_same_url(href):
    assert normalize_url(href, CATALOG + "/") == CATALOG + "/engineering/computer_science"


def test_catalog_file_paths_keep_their_case():
    pdf = "/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf"
    assert normalize_url(pdf, CATALOG) == CATALOG + pdf


def test_catalog_keeps_only_the_cs_and_cal_query_parameters_sorted():
    assert normalize_url("/courses/?cs=CSCI&cal=UG", CATALOG) == CATALOG + "/courses?cal=UG&cs=CSCI"
    assert normalize_url("/courses?cs=CSCI&cal=UG&foo=1&utm_source=x", CATALOG) == \
        CATALOG + "/courses?cal=UG&cs=CSCI"


def test_cc_parameter_is_dropped_because_it_only_unfolds_one_course():
    assert normalize_url("/courses?cal=GR&cc=PHAS-601&cs=PHAS", CATALOG) == \
        CATALOG + "/courses?cal=GR&cs=PHAS"


def test_catalog_root_and_empty_query():
    assert normalize_url("https://catalog.nyit.edu/?") == CATALOG + "/"
    assert normalize_url("https://catalog.nyit.edu") == CATALOG + "/"


# ---------------------------------------------------------------------------
# WordPress sites (www, vancouver): trailing slash added, every query dropped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("href, expected", [
    ("https://www.nyit.edu/about", "https://www.nyit.edu/about/"),
    ("https://www.nyit.edu/about/", "https://www.nyit.edu/about/"),
    ("https://www.nyit.edu/about/?utm_source=x&page=2", "https://www.nyit.edu/about/"),
    ("https://www.nyit.edu/news/?cc=1", "https://www.nyit.edu/news/"),
    ("//www.nyit.edu/admissions", "https://www.nyit.edu/admissions/"),
    ("HTTPS://WWW.NYIT.EDU:443/About", "https://www.nyit.edu/About/"),      # path case is kept
    ("https://www.nyit.edu//about//offices", "https://www.nyit.edu/about/offices/"),
    ("https://www.nyit.edu/a/./b/../c", "https://www.nyit.edu/a/c/"),
    ("https://vancouver.nyit.edu/about#team", "https://vancouver.nyit.edu/about/"),
    ("https://www.nyit.edu", "https://www.nyit.edu/"),
])
def test_wordpress_urls_end_with_a_slash_and_lose_their_query(href, expected):
    assert normalize_url(href) == expected


def test_wordpress_files_do_not_get_a_trailing_slash():
    assert normalize_url("https://www.nyit.edu/files/Form.PDF") == "https://www.nyit.edu/files/Form.PDF"
    assert normalize_url("https://www.nyit.edu/wp-content/uploads/a.jpg") == \
        "https://www.nyit.edu/wp-content/uploads/a.jpg"


def test_trailing_slash_kept_as_written_on_the_magazine():
    assert normalize_url("https://magazine.nyit.edu/fall2024/") == "https://magazine.nyit.edu/fall2024/"
    assert normalize_url("https://magazine.nyit.edu/fall2024") == "https://magazine.nyit.edu/fall2024"


def test_other_hosts_strip_the_trailing_slash_and_keep_non_tracking_queries():
    assert normalize_url("https://blogs.nyit.edu/foo/") == "https://blogs.nyit.edu/foo"
    assert normalize_url("https://libguides.nyit.edu/c.php?g=123&p=456&t=789") == \
        "https://libguides.nyit.edu/c.php?g=123&p=456"
    assert normalize_url("https://events.nyit.edu/event/x?fbclid=1") == "https://events.nyit.edu/event/x"


def test_non_nyit_urls_keep_scheme_and_lose_only_tracking_parameters():
    assert normalize_url("http://example.com/A?utm_source=1&b=2") == "http://example.com/A?b=2"
    assert normalize_url("https://example.com/a/b/") == "https://example.com/a/b"


def test_http_is_upgraded_to_https_on_nyit_hosts_and_ports_are_kept():
    assert normalize_url("http://www.nyit.edu/x") == "https://www.nyit.edu/x/"
    assert normalize_url("https://www.nyit.edu:8443/x") == "https://www.nyit.edu:8443/x/"


def test_relative_links_resolve_against_the_page():
    assert normalize_url("../policies", CATALOG + "/engineering/computer_science") == CATALOG + "/policies"
    assert normalize_url("curriculum", CATALOG + "/engineering/computer_science/") == \
        CATALOG + "/engineering/computer_science/curriculum"


@pytest.mark.parametrize("href", [
    None, "", "   ", "#top", "#", "mailto:someone@nyit.edu", "MAILTO:x@nyit.edu", "tel:5165551234",
    "javascript:void(0);", "JavaScript:getCD('FCWR 101')", "data:text/plain,hi", "ftp://x.nyit.edu/a",
    "sms:123", "file:///etc/passwd", "http://[bad", "https://www.nyit.edu:notaport/",
])
def test_non_page_hrefs_give_none(href):
    assert normalize_url(href) is None


def test_normalisation_is_idempotent():
    for href in ("/Engineering/Computer_Science/", "/courses/?cs=CSCI&cal=UG&cc=CSCI-260",
                 "https://www.nyit.edu/about?x=1", "https://libguides.nyit.edu/c.php?p=2&g=1"):
        once = normalize_url(href, CATALOG)
        assert normalize_url(once) == once


# ---------------------------------------------------------------------------
# link_kind: page | pdf | asset | excluded | disabled | external
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url, kind", [
    ("https://catalog.nyit.edu/engineering", "page"),
    ("https://catalog.nyit.edu/courses?cal=UG&cs=CSCI", "page"),
    ("https://catalog.nyit.edu/files/x.pdf", "pdf"),
    ("https://catalog.nyit.edu/files/A.PDF", "pdf"),
    ("https://my.nyit.edu/form.pdf", "pdf"),                 # PDFs are decided later (docs step)
    ("https://catalog.nyit.edu/files/x.docx", "asset"),
    ("https://www.nyit.edu/style.css", "asset"),
    ("https://www.nyit.edu/wp-content/uploads/x.jpg", "asset"),
    ("https://catalog.nyit.edu/files/foo", "excluded"),
    ("https://catalog.nyit.edu/_utilities/course_descriptions_ajax", "excluded"),
    ("https://catalog.nyit.edu/print/engineering", "excluded"),
    ("https://www.nyit.edu/wp-admin/", "excluded"),
    ("https://www.nyit.edu/news/feed/", "excluded"),
    ("https://www.nyit.edu/news/page/2/", "excluded"),
    ("https://www.nyit.edu/tag/x/", "excluded"),
    ("https://apps.nyit.edu/directory", "excluded"),         # only /policies is in scope
    ("https://apps.nyit.edu/policies/academic_integrity", "page"),
    ("https://events.nyit.edu/calendar", "excluded"),        # endless calendar views
    ("https://events.nyit.edu/event/12009-info-session", "page"),
    ("https://libanswers.nyit.edu/faq/158255", "page"),
    ("https://libanswers.nyit.edu/search", "excluded"),
    ("https://libguides.nyit.edu/az.php", "excluded"),
    ("https://libguides.nyit.edu/az/databases", "excluded"),
    ("https://my.nyit.edu/", "disabled"),
    ("https://example.com/", "external"),
    ("https://nyit.edu.evil.com/", "external"),
    ("https://evilnyit.edu/", "external"),
    ("https://newly-found.nyit.edu/x", "page"),              # any other *.nyit.edu host
])
def test_link_kind(url, kind):
    assert link_kind(url) == kind


def test_print_pages_are_only_crawled_on_request():
    url = "https://catalog.nyit.edu/print/engineering"
    assert not is_crawlable(url)
    assert is_crawlable(url, include_print=True)
    assert link_kind(url, include_print=True) == "page"


@pytest.mark.parametrize("host", [
    "ebscohost-com.arktos.nyit.edu",       # library EZproxy rewrites licensed databases
    "arktos.nyit.edu",
    "sso.nyit.edu", "login.nyit.edu", "pwreset.nyit.edu", "identity.nyit.edu", "apps2.nyit.edu",
    "esapps2.nyit.edu", "workday.nyit.edu", "webmail.nyit.edu", "portal.nyit.edu",
])
def test_proxy_and_sign_in_hosts_are_disabled(host):
    assert link_kind(f"https://{host}/x") == "disabled"


@pytest.mark.parametrize("host", ["catalog-dev.nyit.edu", "www-staging.nyit.edu", "test.nyit.edu",
                                  "qa-portal.nyit.edu", "dev.nyit.edu", "beta.nyit.edu"])
def test_development_and_staging_hosts_are_disabled(host):
    assert link_kind(f"https://{host}/engineering") == "disabled"


@pytest.mark.parametrize("host", ["devices.nyit.edu", "deseversky.nyit.edu", "testimonials.nyit.edu",
                                  "apps.nyit.edu"])
def test_hosts_that_only_look_like_dev_or_app_hosts_are_crawled(host):
    path = "/policies" if host == "apps.nyit.edu" else "/"
    assert link_kind(f"https://{host}{path}") == "page"


def test_is_internal_and_url_helpers():
    assert urls.is_internal("https://www.nyit.edu/")
    assert urls.is_internal("https://my.nyit.edu/")            # disabled, but NYIT
    assert not urls.is_internal("https://example.com/")
    assert urls.url_host("https://WWW.nyit.edu/x") == "www.nyit.edu"
    assert urls.url_path("https://www.nyit.edu") == "/"
    assert urls.path_segments(CATALOG + "/engineering/computer_science") == ["engineering", "computer_science"]
    assert urls.has_non_html_extension("https://www.nyit.edu/a.MP4")


# ---------------------------------------------------------------------------
# cache_filename: readable, unique, valid on Windows
# ---------------------------------------------------------------------------
_WINDOWS_RESERVED = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def test_cache_filename_examples():
    assert cache_filename(CATALOG + "/engineering/computer_science/curriculum") == \
        "catalog.nyit.edu/engineering__computer_science__curriculum__63ea4861cf.html"
    assert cache_filename(CATALOG + "/courses?cal=UG&cs=CSCI") == \
        "catalog.nyit.edu/courses__cal-ug_cs-csci__0242845aa9.html"
    assert cache_filename(CATALOG + "/") == "catalog.nyit.edu/index__28b0d8396e.html"


@pytest.mark.parametrize("url", [
    "https://www.nyit.edu/a:b*c?d|e=<f>/",
    "https://www.nyit.edu/" + "x" * 400,
    "https://blogs.nyit.edu/biological_chemical_sciences/%E2%80%9Chttps:/scifinder-cas-org.ezproxy."
    "cul.columbia.edu/scifinder/references/answers/DF0CFC5A:DF0D6929/2.html",
    "https://www.nyit.edu/news/articles/caf%C3%A9-%E2%80%93-r%C3%A9sum%C3%A9/",
    "https://www.nyit.edu/con/",            # CON, PRN, AUX ... are reserved names on Windows
    "https://www.nyit.edu/ end with space . /",
])
def test_cache_filename_is_a_safe_relative_path_on_every_os(url):
    name = cache_filename(url)
    folder, _, base = name.partition("/")
    assert folder == urls.url_host(url)
    assert "/" not in base and "\\" not in name
    assert not _WINDOWS_RESERVED.search(name)
    assert base.endswith(".html") and not base.startswith(".")
    assert not base.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"}
    assert len(name) <= 180                    # far below Windows' 260-character path limit
    assert name == name.lower()


def test_cache_filename_differs_for_urls_that_differ_only_in_case():
    a = cache_filename("https://www.nyit.edu/About/")
    b = cache_filename("https://www.nyit.edu/about/")
    assert a.lower() != b.lower()             # no clash on case-insensitive file systems
