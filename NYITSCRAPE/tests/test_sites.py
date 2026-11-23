"""sites.py: the website profiles that decide what is crawled and how."""

from __future__ import annotations

import re

import pytest

from askbear_scraper import sites
from askbear_scraper.models import PAGE_TYPES

ENABLED = ("catalog.nyit.edu", "www.nyit.edu", "vancouver.nyit.edu", "magazine.nyit.edu",
           "events.nyit.edu", "apps.nyit.edu", "libanswers.nyit.edu", "libguides.nyit.edu",
           "blogs.nyit.edu", "online.nyit.edu", "deseversky.nyit.edu", "libcal.nyit.edu",
           "eportfolios.nyit.edu")


def test_every_profile_is_keyed_by_its_host_and_documented():
    for host, profile in sites.SITES.items():
        assert host == profile.host == host.lower()
        assert sites.is_nyit_host(host)
        assert profile.name and profile.reason, host
        assert profile.parser in ("catalog", "generic")
        assert profile.trailing_slash in ("strip", "add", "keep")
        assert profile.max_pages > 0
        for pattern in profile.allow_patterns + profile.exclude_patterns:
            re.compile(pattern)


@pytest.mark.parametrize("host", ENABLED)
def test_main_sites_are_enabled(host):
    assert sites.SITES[host].enabled


@pytest.mark.parametrize("host", ["my.nyit.edu", "campusgroups.nyit.edu", "alumni.nyit.edu",
                                  "apply.nyit.edu", "web.nyit.edu", "registrations.nyit.edu",
                                  "identity.nyit.edu", "catalog-dev.nyit.edu",
                                  "learningspace.nyit.edu", "iccjb.nyit.edu",
                                  "comresearchdata.nyit.edu", "nyitetic.nyit.edu",
                                  "virtualtour.nyit.edu"])
def test_login_form_and_robots_opt_out_sites_are_disabled_with_a_reason(host):
    profile = sites.SITES[host]
    assert not profile.enabled
    assert profile.reason


def test_only_the_catalog_uses_the_catalog_parser():
    assert [h for h, p in sites.SITES.items() if p.parser == "catalog"] == ["catalog.nyit.edu"]
    catalog = sites.SITES["catalog.nyit.edu"]
    assert catalog.case_insensitive and catalog.trailing_slash == "strip"
    assert set(catalog.keep_query_params) == {"cs", "cal"}
    assert "/courses" in catalog.seeds


def test_wordpress_sites_share_the_theme_rules():
    for host in ("www.nyit.edu", "vancouver.nyit.edu"):
        profile = sites.SITES[host]
        assert profile.trailing_slash == "add"
        # only the pagination of www's accomplishments and research-project listings is kept
        assert profile.keep_query_params == (
            ("fsa_page", "mrp_page") if host == "www.nyit.edu" else ())
        assert profile.breadcrumb_selector == ".yoast-breadcrumbs"
        assert any(s.startswith("https://" + host) for s in profile.sitemaps)


@pytest.mark.parametrize("host, path, allowed", [
    ("catalog.nyit.edu", "/engineering", True),
    ("catalog.nyit.edu", "/files/catalog/archives/x.pdf", False),
    ("catalog.nyit.edu", "/_utilities/course_descriptions_ajax?cc=CSCI%20260", False),
    ("catalog.nyit.edu", "/print/engineering", False),
    ("catalog.nyit.edu", "/printing", True),            # only the /print/ folder
    ("www.nyit.edu", "/about/", True),
    ("www.nyit.edu", "/wp-json/wp/v2/pages", False),
    ("www.nyit.edu", "/news/articles/feed/", False),
    ("www.nyit.edu", "/author/admin/", False),
    ("apps.nyit.edu", "/policies", True),
    ("apps.nyit.edu", "/policies/collection/student_handbook", True),
    ("apps.nyit.edu", "/people", False),
    ("events.nyit.edu", "/event/x", True),
    ("events.nyit.edu", "/event/x?replytocom=5", False),
    ("events.nyit.edu", "/", False),
    ("libanswers.nyit.edu", "/faq/12", True),
    ("libanswers.nyit.edu", "/faq/", False),
    ("libguides.nyit.edu", "/apa", True),
    ("libguides.nyit.edu", "/srch.php?q=x", False),
    ("libguides.nyit.edu", "/blog/x", False),
    ("deseversky.nyit.edu", "/images/a", False),
    ("deseversky.nyit.edu", "/weddings", True),
])
def test_path_allowed(host, path, allowed):
    assert sites.SITES[host].path_allowed(path) is allowed


def test_profile_for_known_hosts_ignores_case_and_trailing_dot():
    assert sites.profile_for("WWW.NYIT.EDU.") is sites.SITES["www.nyit.edu"]


def test_profile_for_returns_none_for_non_nyit_hosts():
    for host in ("example.com", "nyit.edu.evil.com", "evilnyit.edu", "", None):
        assert sites.profile_for(host) is None


def test_unknown_nyit_hosts_get_the_conservative_default_profile():
    profile = sites.profile_for("newly-found.nyit.edu")
    assert profile.host == "newly-found.nyit.edu"
    assert profile.enabled and profile.parser == "generic"
    assert profile.max_pages == sites.DEFAULT_PROFILE.max_pages == 300
    assert profile.seeds == ()


@pytest.mark.parametrize("host", ["ebsco.arktos.nyit.edu", "sso.nyit.edu", "cas.nyit.edu",
                                  "pwreset.nyit.edu", "apps3.nyit.edu", "esapps.nyit.edu",
                                  "banner.nyit.edu", "outlook.nyit.edu"])
def test_proxy_and_account_hosts_get_a_disabled_default_profile(host):
    profile = sites.profile_for(host)
    assert profile is not None and not profile.enabled
    assert "sign-in" in profile.reason or "proxy" in profile.reason


@pytest.mark.parametrize("host, disabled", [
    ("www-dev.nyit.edu", True), ("staging.nyit.edu", True), ("uat-apps.nyit.edu", True),
    ("sandbox.nyit.edu", True), ("preview.nyit.edu", True),
    ("devices.nyit.edu", False), ("developer.nyit.edu", False), ("testing-center.nyit.edu", True),
    ("contest.nyit.edu", False),
])
def test_dev_host_labels_are_matched_as_whole_labels(host, disabled):
    assert (not sites.profile_for(host).enabled) is disabled


def test_is_nyit_host():
    assert sites.is_nyit_host("nyit.edu")
    assert sites.is_nyit_host("catalog.nyit.edu")
    assert sites.is_nyit_host("CATALOG.NYIT.EDU.")
    assert not sites.is_nyit_host("nyit.edu.com")
    assert not sites.is_nyit_host("")


def test_crawl_hosts_default_is_every_enabled_profile_in_a_stable_order():
    hosts = [p.host for p in sites.crawl_hosts()]
    assert hosts == [h for h, p in sites.SITES.items() if p.enabled]
    assert hosts[0] == "catalog.nyit.edu"
    assert set(ENABLED) <= set(hosts)
    assert not {"my.nyit.edu", "catalog-dev.nyit.edu"} & set(hosts)


def test_crawl_hosts_restricted_to_named_sites():
    hosts = [p.host for p in sites.crawl_hosts(["WWW.nyit.edu", "catalog.nyit.edu"])]
    assert hosts == ["catalog.nyit.edu", "www.nyit.edu"]


def test_crawl_hosts_accepts_unlisted_nyit_hosts_but_not_other_domains():
    hosts = [p.host for p in sites.crawl_hosts(["comapp.nyit.edu", "example.com"])]
    assert hosts == ["comapp.nyit.edu"]
    assert sites.crawl_hosts(["example.com"]) == []


def test_describe_sites_lists_every_profile_and_the_pattern_rules():
    table = sites.describe_sites()
    lines = table.splitlines()
    assert lines[0].split()[:3] == ["HOST", "CRAWLED", "NAME"]
    for host in sites.SITES:
        assert host in table
    assert "arktos" in table and "other *.nyit.edu" in table
    assert len(lines) == len(sites.SITES) + 4


def test_common_remove_selectors_do_not_remove_main_content():
    for selector in sites.COMMON_REMOVE_SELECTORS:
        assert selector not in ("main", "article", "body", ".Common", "div")


def test_policy_page_type_exists_for_apps():
    assert "policy" in PAGE_TYPES
