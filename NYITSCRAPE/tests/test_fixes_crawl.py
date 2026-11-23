"""Regression tests for the crawl fixes (crawler.py, fetcher.py, urls.py, sites.py, sitemaps.py).

Every test runs offline against testkit.FakeWeb through the real PoliteFetcher (fake
clock: delays are recorded, not waited for). Fixture pages in tests/fixtures/fixes_crawl/
are small synthetic copies of the structure of real NYIT pages (no personal data).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import requests

from askbear_scraper import fetcher as fetcher_mod
from askbear_scraper import sites
from askbear_scraper.config import Settings
from askbear_scraper.crawler import (Crawler, is_login_page, is_soft_404, load_manifest,
                                     merge_summaries)
from askbear_scraper.urls import MAX_SLUG_LENGTH, cache_filename, link_kind, normalize_url
from askbear_scraper.utils import make_soup

from testkit import FakeClock, FakeWeb, Reply, html_page, make_fetcher, read_jsonl, write_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fixes_crawl"
CAT = "https://catalog.nyit.edu"
WWW = "https://www.nyit.edu"
SITE = "https://site.nyit.edu"
BOTH = ["catalog.nyit.edu", "www.nyit.edu"]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def two_sites(monkeypatch):
    """catalog.nyit.edu and www.nyit.edu with a single seed ("/") and no sitemaps."""
    monkeypatch.setitem(sites.SITES, "catalog.nyit.edu",
                        replace(sites.SITES["catalog.nyit.edu"], seeds=("/",)))
    monkeypatch.setitem(sites.SITES, "www.nyit.edu",
                        replace(sites.SITES["www.nyit.edu"], seeds=("/",), sitemaps=()))
    return BOTH


def crawl(settings, web, only=BOTH, clock=None):
    fetcher = make_fetcher(settings, web, clock or FakeClock())
    return Crawler(settings, fetcher=fetcher, only_sites=only).run()


def manifest(settings):
    return load_manifest(settings.manifest_path)


def summary_file(settings):
    return json.loads((settings.raw_dir / "crawl_summary.json").read_text(encoding="utf-8"))


def base_web() -> FakeWeb:
    """A catalog with 3 pages and a www home that is the only page linking to a catalog
    semester map (like the 174 /semester-map/ pages linked only from www.nyit.edu)."""
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/a", "/b", "/files/x.pdf"]))
    web.add(CAT + "/a", html_page("A", links=["/files/a.pdf"]))
    web.add(CAT + "/b", html_page("B"))
    web.add(WWW + "/", html_page("WWW", links=["/x/", CAT + "/semester-map/cs-bs", "/forms/w.pdf",
                                               "https://example.com/partner"]))
    web.add(WWW + "/x/", html_page("X"))
    web.add(CAT + "/semester-map/cs-bs", html_page("Semester map", links=["/files/map.pdf"]))
    return web


# ---------------------------------------------------------------------------
# 1. a capped, restricted or interrupted run never loses what it did not visit
# ---------------------------------------------------------------------------
def test_quick_test_run_with_max_pages_keeps_every_cached_page(settings, two_sites):
    web = base_web()
    crawl(settings, web)
    before = manifest(settings)
    graph_before = {r["url"] for r in read_jsonl(settings.link_graph_path)}
    quick = Settings(data_dir=settings.data_dir, delay=1.0, jitter=0.0, max_pages=1)
    summary = crawl(quick, web)                               # `crawl --max-pages 1`
    after = manifest(quick)
    assert set(after) == set(before)
    assert {u: e["status"] for u, e in after.items()} == {u: e["status"] for u, e in before.items()}
    assert {r["url"] for r in read_jsonl(settings.link_graph_path)} == graph_before
    pdfs = {r["url"] for r in read_jsonl(settings.external_links_path) if r["kind"] == "pdf"}
    assert {CAT + "/files/x.pdf", CAT + "/files/a.pdf", WWW + "/forms/w.pdf",
            CAT + "/files/map.pdf"} <= pdfs
    assert summary["capped_sites"]                            # the run itself was capped


def test_site_restricted_run_merges_link_graph_external_links_and_summary(settings, two_sites):
    web = base_web()
    crawl(settings, web)
    full_graph = {r["url"]: r["outlinks"] for r in read_jsonl(settings.link_graph_path)}
    web.requests.clear()
    summary = crawl(settings, web, only=["catalog.nyit.edu"])  # `crawl --site catalog.nyit.edu`
    assert web.page_requests() == []                           # everything came from the cache
    graph = {r["url"]: r["outlinks"] for r in read_jsonl(settings.link_graph_path)}
    assert graph == full_graph                                 # www pages' records kept
    external = {r["url"]: r for r in read_jsonl(settings.external_links_path)}
    assert external[WWW + "/forms/w.pdf"]["found_on"] == [WWW + "/"]
    assert external["https://example.com/partner"]["kind"] == "external"
    # the semester map is only linked from www: not visited, but kept with its PDF link
    assert manifest(settings)[CAT + "/semester-map/cs-bs"]["status"] == "ok"
    assert CAT + "/files/map.pdf" in external
    saved = summary_file(settings)
    assert saved == summary
    assert saved["sites"] == ["catalog.nyit.edu", "www.nyit.edu"]
    assert saved["last_run"]["sites"] == ["catalog.nyit.edu"]
    assert saved["status_by_site"]["www.nyit.edu"] == {"ok": 2}   # from the earlier run
    assert saved["status_counts"]["ok"] == sum(c.get("ok", 0) for c in saved["status_by_site"].values())
    assert saved["urls_visited"] == sum(sum(c.values()) for c in saved["status_by_site"].values())
    assert saved["pdf_links"] == 4


def test_interrupted_run_keeps_unvisited_entries_and_links(settings, two_sites):
    web = base_web()
    crawl(settings, web)
    before = set(manifest(settings))
    settings.refresh = True                                     # every page is downloaded again
    web.routes[CAT + "/a"] = [KeyboardInterrupt(), Reply(html_page("A", links=["/files/a.pdf"]))]
    summary = crawl(settings, web)
    assert summary["interrupted"]
    assert set(manifest(settings)) == before
    assert CAT + "/files/map.pdf" in {r["url"] for r in read_jsonl(settings.external_links_path)}


def test_complete_refresh_crawl_drops_pages_no_longer_linked(settings, two_sites, monkeypatch):
    # "every website" = catalog + www here
    monkeypatch.setattr(sites, "SITES", {h: sites.SITES[h] for h in BOTH})
    web = base_web()
    crawl(settings, web, only=None)
    web.add(WWW + "/", html_page("WWW", links=["/x/"]))      # the semester map link is gone
    crawl(settings, web, only=["www.nyit.edu"])               # a --site run never drops anything
    assert CAT + "/semester-map/cs-bs" in manifest(settings)
    settings.refresh = True
    crawl(settings, web, only=None)                           # `crawl --refresh` of every site
    after = manifest(settings)
    assert CAT + "/semester-map/cs-bs" not in after
    assert CAT + "/a" in after
    assert CAT + "/files/map.pdf" not in {r["url"] for r in read_jsonl(settings.external_links_path)}


def test_merge_summaries_keeps_other_sites_and_recomputes_totals():
    previous = {"sites": ["catalog.nyit.edu", "www.nyit.edu"], "interrupted": False,
                "status_by_site": {"catalog.nyit.edu": {"ok": 9}, "www.nyit.edu": {"ok": 5, "duplicate": 2}},
                "sitemap_pages_by_site": {"www.nyit.edu": 7, "catalog.nyit.edu": 0},
                "capped_sites": {"www.nyit.edu": 3}, "given_up_sites": {},
                "unreachable_sites": ["iris.nyit.edu"], "linked_but_not_crawled_sites": {"my.nyit.edu": 4},
                "still_queued": 0}
    current = {"sites": ["catalog.nyit.edu"], "interrupted": False, "urls_seen": 4,
               "status_by_site": {"catalog.nyit.edu": {"ok": 3, "not_found": 1}},
               "sitemap_pages_by_site": {"catalog.nyit.edu": 0}, "capped_sites": {},
               "given_up_sites": {}, "still_queued_by_site": {}, "unreachable_sites": [],
               "failed_sites": [], "linked_but_not_crawled_sites": {"my.nyit.edu": 1, "apply.nyit.edu": 2},
               "last_run": {"sites": ["catalog.nyit.edu"]}}
    merged = merge_summaries(previous, current)
    assert merged["sites"] == ["catalog.nyit.edu", "www.nyit.edu"]
    assert merged["status_by_site"] == {"catalog.nyit.edu": {"ok": 3, "not_found": 1},
                                        "www.nyit.edu": {"ok": 5, "duplicate": 2}}
    assert merged["status_counts"] == {"ok": 8, "not_found": 1, "duplicate": 2}
    assert merged["urls_visited"] == 11 and merged["urls_seen"] == 11
    assert merged["capped_sites"] == {"www.nyit.edu": 3}
    assert merged["sitemap_pages_by_site"] == {"www.nyit.edu": 7, "catalog.nyit.edu": 0}
    assert merged["unreachable_sites"] == ["iris.nyit.edu"]
    assert merged["linked_but_not_crawled_sites"] == {"my.nyit.edu": 4, "apply.nyit.edu": 2}
    assert merged["last_run"] == {"sites": ["catalog.nyit.edu"]}
    # an earlier interrupted crawl of other websites is not "finished" by a --site run
    assert merge_summaries(dict(previous, interrupted=True), current)["interrupted"]
    assert merge_summaries(None, current) == current


# ---------------------------------------------------------------------------
# 2. re-running is cheap: duplicates and redirect loops are cached
# ---------------------------------------------------------------------------
def test_cached_duplicate_whose_target_comes_later_still_gets_the_target_crawled(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/b", "/old-b"]))
    web.add(CAT + "/b", html_page("B", links=["/c"]))
    web.redirect(CAT + "/old-b", CAT + "/b")
    web.add(CAT + "/c", html_page("C"))
    crawl(settings, web, only=["catalog.nyit.edu"])
    assert manifest(settings)[CAT + "/old-b"]["status"] == "duplicate"
    # next run: the home page only links to the old URL
    entries = manifest(settings)
    entries[CAT + "/"]["file"] = cache_filename(CAT + "/")
    (settings.html_dir / entries[CAT + "/"]["file"]).write_text(
        html_page("Catalog", links=["/old-b"]), encoding="utf-8")
    web.requests.clear()
    crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.page_requests() == []
    graph = {r["url"] for r in read_jsonl(settings.link_graph_path)}
    assert CAT + "/b" in graph and CAT + "/c" in graph          # the target was visited


def test_redirect_loop_is_detected_early_cached_and_not_retried(settings, two_sites):
    web = FakeWeb()
    web.add(WWW + "/", html_page("WWW", links=["/library/chat/"]))
    # www <-> site.nyit.edu loop, with or without the trailing slash
    web.redirect(WWW + "/library/chat/", SITE + "/library/chat/")
    web.redirect(SITE + "/library/chat/", WWW + "/library/chat/")
    web.redirect(WWW + "/library/chat", SITE + "/library/chat")
    web.redirect(SITE + "/library/chat", WWW + "/library/chat")
    crawl(settings, web, only=["www.nyit.edu"])
    entry = manifest(settings)[WWW + "/library/chat/"]
    assert entry["status"] == "redirect_loop" and "loop" in entry["error"]
    assert web.count(lambda u: "/library/chat" in u) == 4        # 2 + 2, not 2 x 31
    web.requests.clear()
    crawl(settings, web, only=["www.nyit.edu"])
    assert web.page_requests() == []


def test_slash_added_by_normalisation_is_dropped_when_it_causes_a_loop(settings, two_sites):
    web = FakeWeb()
    web.add(WWW + "/", html_page("WWW", links=["/about/nyit_at_a_glance"]))
    web.redirect(WWW + "/about/nyit_at_a_glance/", SITE + "/about/nyit_at_a_glance/")
    web.redirect(SITE + "/about/nyit_at_a_glance/", WWW + "/about/nyit_at_a_glance/")
    web.redirect(WWW + "/about/nyit_at_a_glance", SITE + "/about/nyit_at_a_glance")
    web.redirect(SITE + "/about/nyit_at_a_glance", SITE + "/about/at_a_glance")
    web.redirect(SITE + "/about/at_a_glance", WWW + "/about/at-a-glance/")
    web.add(WWW + "/about/at-a-glance/", html_page("At a glance"))
    crawl(settings, web, only=["www.nyit.edu"])
    entry = manifest(settings)[WWW + "/about/nyit_at_a_glance/"]
    assert entry["status"] == "ok" and entry["final_url"] == WWW + "/about/at-a-glance/"


def test_page_that_failed_with_5xx_gets_one_attempt_on_the_next_run(settings, two_sites):
    settings.max_retries = 3
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/medicine/faculty"]))
    web.add(CAT + "/medicine/faculty", "Server Error", status=500)
    crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.count(lambda u: u.endswith("/faculty")) == 4      # 1 + 3 retries
    web.requests.clear()
    clock = FakeClock()
    crawl(settings, web, only=["catalog.nyit.edu"], clock=clock)
    assert web.page_requests() == [CAT + "/medicine/faculty"]    # one attempt, no back-off
    assert all(s <= settings.delay + 1e-9 for s in clock.sleeps)  # only the politeness delay


# ---------------------------------------------------------------------------
# 3. redirects: politeness, robots.txt and scope are checked for every hop
# ---------------------------------------------------------------------------
def test_redirect_to_disabled_or_foreign_site_is_never_requested(settings, two_sites):
    web = FakeWeb()
    web.add(WWW + "/", html_page("WWW", links=["/admissions/nonmatric_courses/", "/facebook/"]))
    web.redirect(WWW + "/admissions/nonmatric_courses/",
                 "https://registrations.nyit.edu/admissions/nonmatric_courses")
    web.redirect(WWW + "/facebook/", "https://www.facebook.com/mynyit")
    crawl(settings, web, only=["www.nyit.edu"])
    entries = manifest(settings)
    assert entries[WWW + "/admissions/nonmatric_courses/"]["status"] == "offsite_redirect"
    assert entries[WWW + "/admissions/nonmatric_courses/"]["final_url"] == \
        "https://registrations.nyit.edu/admissions/nonmatric_courses"
    assert entries[WWW + "/facebook/"]["status"] == "offsite_redirect"
    assert not any("registrations.nyit.edu" in u or "facebook.com" in u for u in web.requests)


def test_redirect_into_a_path_robots_txt_forbids_is_not_requested(settings, two_sites):
    web = FakeWeb()
    web.robots("site.nyit.edu", "User-agent: *\nDisallow: /private\n")
    web.add(WWW + "/", html_page("WWW", links=["/old/"]))
    web.redirect(WWW + "/old/", SITE + "/private/page")
    web.add(SITE + "/private/page", html_page("Private"))
    crawl(settings, web, only=["www.nyit.edu"])
    entry = manifest(settings)[WWW + "/old/"]
    assert entry["status"] == "robots_disallowed" and entry["final_url"] == SITE + "/private/page"
    assert SITE + "/private/page" not in web.requests


def test_download_follows_redirects_hop_by_hop_with_robots_check(settings, web, clock, tmp_path):
    fetcher = make_fetcher(settings, web, clock)
    web.robots("files.nyit.edu", "User-agent: *\nDisallow: /secret/\n")
    web.redirect(WWW + "/forms/a.pdf", "https://files.nyit.edu/files/a.pdf")
    web.add("https://files.nyit.edu/files/a.pdf", b"%PDF-1.4 a", content_type="application/pdf")
    web.redirect(WWW + "/forms/b.pdf", "https://files.nyit.edu/secret/b.pdf")
    res = fetcher.download(WWW + "/forms/a.pdf", tmp_path / "a.pdf")
    assert res.ok and (tmp_path / "a.pdf").read_bytes() == b"%PDF-1.4 a"
    assert res.final_url == "https://files.nyit.edu/files/a.pdf"
    res = fetcher.download(WWW + "/forms/b.pdf", tmp_path / "b.pdf")
    assert not res.ok and res.blocked == fetcher_mod.BLOCKED_ROBOTS
    assert "https://files.nyit.edu/secret/b.pdf" not in web.requests and not (tmp_path / "b.pdf").exists()


@pytest.mark.parametrize("status, verdict", [(401, fetcher_mod.DISALLOWED), (403, fetcher_mod.DISALLOWED),
                                             (404, fetcher_mod.ALLOWED), (410, fetcher_mod.ALLOWED)])
def test_robots_txt_http_status_rules(fetcher, web, status, verdict):
    web.routes["https://catalog.nyit.edu/robots.txt"] = Reply("no", status, content_type="text/plain")
    assert fetcher.check(CAT + "/engineering") == verdict


def test_robots_txt_server_error_blocks_until_it_can_be_read(settings, web, clock):
    fetcher = make_fetcher(settings, web, clock)
    web.routes["https://catalog.nyit.edu/robots.txt"] = Reply("busy", 503, content_type="text/plain")
    assert fetcher.check(CAT + "/x") == fetcher_mod.WAIT
    assert web.requests.count(CAT + "/robots.txt") == 2          # first try: 2 attempts
    assert fetcher.check(CAT + "/x") == fetcher_mod.WAIT          # next try not due yet: no request
    assert web.requests.count(CAT + "/robots.txt") == 2
    assert fetcher.check(CAT + "/x", wait=True) == fetcher_mod.WAIT
    assert clock.sleeps[-1] == pytest.approx(fetcher_mod.ROBOTS_REPROBE_DELAYS[0])
    assert fetcher.check(CAT + "/x", wait=True) == fetcher_mod.UNREACHABLE
    assert fetcher.check(CAT + "/y", wait=True) == fetcher_mod.UNREACHABLE   # no more tries
    assert web.requests.count(CAT + "/robots.txt") == fetcher_mod.MAX_ROBOTS_PROBES + 1


# ---------------------------------------------------------------------------
# 4. one failed robots.txt request no longer skips a website
# ---------------------------------------------------------------------------
def test_a_transient_robots_failure_does_not_skip_the_catalog(settings, two_sites):
    web = base_web()
    web.routes[CAT + "/robots.txt"] = [requests.exceptions.ConnectionError("reset"),
                                       requests.exceptions.ConnectionError("reset"),
                                       Reply("not found", 404)]
    summary = crawl(settings, web)
    assert summary["status_by_site"]["catalog.nyit.edu"]["ok"] == 4
    assert summary["unreachable_sites"] == [] and summary["failed_sites"] == []
    assert summary["given_up_sites"] == {}


def test_a_website_that_stays_unreachable_is_reported_as_failed(settings, two_sites):
    web = base_web()
    web.routes[CAT + "/robots.txt"] = requests.exceptions.ConnectionError("no route to host")
    summary = crawl(settings, web)
    assert summary["unreachable_sites"] == ["catalog.nyit.edu"]
    assert summary["failed_sites"] == ["catalog.nyit.edu"]
    assert "no route to host" in summary["unreachable_reasons"]["catalog.nyit.edu"]
    assert not any(u.startswith(CAT) and not u.endswith("robots.txt") for u in web.requests)
    assert summary["status_by_site"]["www.nyit.edu"]["ok"] == 2   # the other site was crawled
    assert manifest(settings)[CAT + "/"]["status"] == "error"     # retried by the next run


# ---------------------------------------------------------------------------
# 5. a page that cannot be saved does not abort the crawl
# ---------------------------------------------------------------------------
def test_cache_write_error_is_recorded_and_the_crawl_goes_on(settings, two_sites, monkeypatch):
    web = base_web()
    real_write = Path.write_text

    def failing_write(self, *args, **kwargs):
        if self.name.startswith("a__"):
            raise FileNotFoundError(2, "No such file or directory (path too long)", str(self))
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    summary = crawl(settings, web, only=["catalog.nyit.edu"])
    entry = manifest(settings)[CAT + "/a"]
    assert entry["status"] == "error" and "could not save" in entry["error"]
    assert entry["file"] is None
    assert summary["status_by_site"]["catalog.nyit.edu"]["ok"] == 2
    assert (settings.raw_dir / "crawl_summary.json").exists()
    monkeypatch.setattr(Path, "write_text", real_write)
    web.requests.clear()
    crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.page_requests() == [CAT + "/a"]                   # retried, then saved
    assert manifest(settings)[CAT + "/a"]["status"] == "ok"


def test_cache_file_names_stay_short_for_windows():
    url = ("https://vancouver.nyit.edu/academics/degrees/master-of-science-cybersecurity-ms/"
           "admission-requirements/flexible-mathematics-prerequisite-option/")
    name = cache_filename(url)
    folder, _, base = name.partition("/")
    assert folder == "vancouver.nyit.edu"
    assert len(base) <= MAX_SLUG_LENGTH + len("__0123456789.html") == 77
    assert len("data/raw/html/" + name) <= 115
    assert cache_filename(url) != cache_filename(url.replace("option", "options"))


# ---------------------------------------------------------------------------
# 6. sign-in screens, server default pages, script shells and error templates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["mahara_login.html"])
def test_sign_in_screen_without_password_field_is_a_login_page(name):
    soup = make_soup(fixture(name))
    assert is_login_page(soup) and not is_soft_404(soup)


def test_help_pages_about_signing_in_are_not_login_pages():
    words = " ".join(["Follow these steps to reach the library databases from home."] * 30)
    for title in ("Sign In to Your Library Account: Step by Step | NYIT",
                  "Off-campus Library Access: OKTA Sign-In | Library e-News",
                  "Movies Login and Password - Ask a Librarian"):
        assert not is_login_page(make_soup(html_page(title, body=words)))
    assert is_login_page(make_soup("<title>Login | Portal</title><p>Welcome</p>"))
    assert is_login_page(make_soup("<title>Sign in</title><div id=app></div>"))


@pytest.mark.parametrize("name", ["iis_default.html", "tour_shell.html", "mahara_invalid_parameter.html"])
def test_pages_without_content_are_soft_404s(name):
    assert is_soft_404(make_soup(fixture(name)))


@pytest.mark.parametrize("title", ["Welcome to nginx!", "Apache2 Ubuntu Default Page: It works",
                                   "Test Page for the Apache HTTP Server on Fedora", "IIS7"])
def test_web_server_default_pages_are_soft_404s(title):
    assert is_soft_404(make_soup(f"<title>{title}</title><p>Something</p>"))


def test_real_pages_mentioning_browsers_or_errors_are_not_soft_404s():
    long_text = " ".join(["Please upgrade your browser to use the new portal features."] * 20)
    assert not is_soft_404(make_soup(html_page("Browser support | ITS", body=long_text)))
    assert not is_soft_404(make_soup(html_page("Access Denied: a history of censorship",
                                               body=" ".join(["word"] * 400))))
    assert not is_soft_404(make_soup(html_page("Ask a Librarian")))


def test_junk_pages_are_not_saved_and_cached_ones_are_reclassified(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/tool", "/old"]))
    web.add(CAT + "/tool", fixture("mahara_login.html"))
    web.add(CAT + "/old", html_page("Old", links=["/x"]))
    crawl(settings, web, only=["catalog.nyit.edu"])
    entries = manifest(settings)
    assert entries[CAT + "/tool"]["status"] == "login_page" and entries[CAT + "/tool"]["file"] is None
    # a page cached by an older crawler that the new checks recognise as junk
    (settings.html_dir / entries[CAT + "/old"]["file"]).write_text(fixture("iis_default.html"),
                                                                    encoding="utf-8")
    web.requests.clear()
    crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.page_requests() == []
    assert manifest(settings)[CAT + "/old"]["status"] == "soft404"
    assert CAT + "/old" not in {r["url"] for r in read_jsonl(settings.link_graph_path)}


def test_cached_entries_of_an_older_crawler_get_meta_robots(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/private"],
                                 head='<meta name="robots" content="none">'))
    crawl(settings, web, only=["catalog.nyit.edu"])
    entries = manifest(settings)
    old = {k: v for k, v in entries[CAT + "/"].items() if k not in ("noindex", "nofollow", "canonical")}
    write_jsonl(settings.manifest_path, [old] + [e for u, e in entries.items() if u != CAT + "/"])
    web.requests.clear()
    crawl(settings, web, only=["catalog.nyit.edu"])
    entry = manifest(settings)[CAT + "/"]
    assert entry["noindex"] is True and entry["nofollow"] is True
    assert CAT + "/private" not in web.requests


# ---------------------------------------------------------------------------
# 7. content types: PDFs at extension-less URLs, bodies that are not read
# ---------------------------------------------------------------------------
def test_pdf_at_an_extensionless_url_is_handed_to_docs_without_reading_it(settings, two_sites):
    web = FakeWeb()
    web.add(WWW + "/", html_page("WWW", links=["/documents/masters-degree-or-bootcamp/"]))
    body = b"%PDF-1.7\n" + b"x" * 5000
    web.add(WWW + "/documents/masters-degree-or-bootcamp/", body,
            content_type="application/pdf;charset=UTF-8", **{"Content-Length": str(len(body))})
    crawl(settings, web, only=["www.nyit.edu"])
    entry = manifest(settings)[WWW + "/documents/masters-degree-or-bootcamp/"]
    assert entry["status"] == "non_html" and entry["bytes"] == len(body)
    external = {r["url"]: r for r in read_jsonl(settings.external_links_path)}
    rec = external[WWW + "/documents/masters-degree-or-bootcamp/"]
    assert rec["kind"] == "pdf" and rec["served_as_pdf"] and rec["found_on"] == [WWW + "/"]
    assert rec["content_type"].startswith("application/pdf")
    web.requests.clear()
    crawl(settings, web, only=["www.nyit.edu"])                  # cached: still listed
    assert web.page_requests() == []
    assert WWW + "/documents/masters-degree-or-bootcamp/" in \
        {r["url"] for r in read_jsonl(settings.external_links_path)}


def test_body_of_a_declined_content_type_is_not_read(fetcher, web):
    web.add(WWW + "/big.bin", b"x" * 100, content_type="application/octet-stream",
            **{"Content-Length": "100"})
    res = fetcher.get(WWW + "/big.bin", read_body=fetcher_mod.is_html_type)
    assert res.ok and not res.body_read and res.content == b"" and res.size == 100
    upper = fetcher.get(WWW + "/big.bin")
    assert upper.content == b"x" * 100


def test_uppercase_html_content_type_is_saved_with_its_text(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog home"), content_type="Text/HTML; charset=UTF-8")
    crawl(settings, web, only=["catalog.nyit.edu"])
    entry = manifest(settings)[CAT + "/"]
    assert entry["status"] == "ok"
    assert "Catalog home" in (settings.html_dir / entry["file"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 8. sitemaps are refreshed, and an incomplete list is not cached
# ---------------------------------------------------------------------------
def _www_with_sitemap(monkeypatch):
    monkeypatch.setitem(sites.SITES, "www.nyit.edu", replace(
        sites.SITES["www.nyit.edu"], seeds=(), sitemaps=(WWW + "/sitemap_index.xml",)))


def test_old_sitemap_cache_is_downloaded_again(settings, monkeypatch):
    _www_with_sitemap(monkeypatch)
    web = FakeWeb()
    web.add(WWW + "/sitemap_index.xml", f"<urlset><url><loc>{WWW}/new/</loc></url></urlset>",
            content_type="application/xml")
    web.add(WWW + "/new/", html_page("New"))
    cache = settings.raw_dir / "sitemaps" / "www.nyit.edu.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"host": "www.nyit.edu", "fetched_at": "2026-01-01T00:00:00+00:00",
                                 "pages": []}), encoding="utf-8")
    summary = crawl(settings, web, only=["www.nyit.edu"])
    assert summary["sitemap_pages_by_site"] == {"www.nyit.edu": 1}
    assert manifest(settings)[WWW + "/new/"]["status"] == "ok"
    # a fresh cache is used as it is
    web.requests.clear()
    crawl(settings, web, only=["www.nyit.edu"])
    assert web.requests == []


def test_sitemap_list_with_a_failed_sitemap_is_not_cached(settings, monkeypatch):
    _www_with_sitemap(monkeypatch)
    web = FakeWeb()
    web.add(WWW + "/sitemap_index.xml",
            f"<sitemapindex><sitemap><loc>{WWW}/a.xml</loc></sitemap>"
            f"<sitemap><loc>{WWW}/b.xml</loc></sitemap></sitemapindex>", content_type="application/xml")
    web.add(WWW + "/a.xml", f"<urlset><url><loc>{WWW}/a/</loc></url></urlset>")
    web.add(WWW + "/b.xml", "busy", status=503)
    web.add(WWW + "/a/", html_page("A"))
    settings.max_retries = 0
    summary = crawl(settings, web, only=["www.nyit.edu"])
    assert summary["sitemap_pages_by_site"] == {"www.nyit.edu": 1}
    assert not (settings.raw_dir / "sitemaps" / "www.nyit.edu.json").exists()


# ---------------------------------------------------------------------------
# 9. page limits count distinct pages
# ---------------------------------------------------------------------------
def test_exact_copies_do_not_use_up_the_page_limit(settings, monkeypatch):
    monkeypatch.setitem(sites.SITES, "libguides.nyit.edu", replace(
        sites.SITES["libguides.nyit.edu"], seeds=("/",), sitemaps=(), max_pages=3))
    web = FakeWeb()
    guide = html_page("APA guide")
    web.add("https://libguides.nyit.edu/", html_page("Guides", links=["/apa", "/c.php?g=1&p=2", "/mla"]))
    web.add("https://libguides.nyit.edu/apa", guide)
    web.add("https://libguides.nyit.edu/c.php?g=1&p=2", guide)       # the same tab, another URL
    web.add("https://libguides.nyit.edu/mla", html_page("MLA guide"))
    summary = crawl(settings, web, only=["libguides.nyit.edu"])
    assert summary["status_by_site"]["libguides.nyit.edu"]["ok"] == 4
    assert summary["capped_sites"] == {}


# ---------------------------------------------------------------------------
# 10. sites.py scope fixes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/gold_coast", "/ereservespolicy", "/data/search",
                                  "/marketing/searchengines", "/systematicreview/searching",
                                  "/authors", "/goodreads", "/erp", "/sbir", "/rss_feeds", "/ldap",
                                  "/c.php?g=944555&p=6808816"])
def test_libguides_guides_with_tool_like_names_are_crawled(path):
    assert link_kind(normalize_url("https://libguides.nyit.edu" + path)) == "page"


@pytest.mark.parametrize("path", ["/az.php", "/az.php?a=1", "/az/databases", "/srch.php?q=x", "/srch",
                                  "/go.php?c=1", "/er.php", "/err.php", "/friendly.php?s=x",
                                  "/ld.php?content_id=1", "/prf.php?id=1", "/sb.php?subject_id=1",
                                  "/content_rss.php", "/rss.php", "/loader.php", "/process_login.php",
                                  "/widget_box.php", "/search", "/search.php?q=x", "/blog", "/blog/x",
                                  "/admin", "/login.php", "/auth"])
def test_libguides_tools_stay_excluded(path):
    assert link_kind(normalize_url("https://libguides.nyit.edu" + path)) == "excluded"


def test_blog_listing_pages_keep_their_page_number():
    base = "https://blogs.nyit.edu/cfe-weekly-teaching-notes"
    soup = make_soup(fixture("blogs_listing.html"))
    links = [normalize_url(a["href"], base) for a in soup.find_all("a", href=True)]
    assert base + "?p=2" in links and base + "?p=29" in links
    assert base in links                                         # ?tag= views are dropped
    assert link_kind(base + "?p=29") == "page"
    assert sites.SITES["blogs.nyit.edu"].max_pages >= 4000
    assert normalize_url("?p=3", "https://online.nyit.edu/blog") == "https://online.nyit.edu/blog?p=3"


def test_online_vendor_privacy_and_cookie_notices_are_skipped():
    assert link_kind("https://online.nyit.edu/privacy-policy") == "excluded"
    assert link_kind("https://online.nyit.edu/cookies") == "excluded"
    assert link_kind("https://online.nyit.edu/ms-data-science/curriculum") == "page"


def test_library_hours_and_events_are_crawled_but_not_booking_forms():
    for path in ("/", "/hours", "/event/15500847"):
        assert link_kind(normalize_url("https://libcal.nyit.edu" + path)) == "page", path
    for path in ("/booking/manhattan", "/appointments", "/appointment/32798", "/reserve/studio",
                 "/allspaces", "/scheduler.php", "/event/15500847/ical", "/calendar/events", "/r/new"):
        assert link_kind(normalize_url("https://libcal.nyit.edu" + path)) == "excluded", path
    assert "/hours" in sites.SITES["libcal.nyit.edu"].seeds


def test_tool_hosts_are_disabled_and_eportfolios_is_only_its_home_page():
    for host in ("learningspace.nyit.edu", "iccjb.nyit.edu", "comresearchdata.nyit.edu",
                 "nyitetic.nyit.edu", "virtualtour.nyit.edu", "oaproxy-sso.nyit.edu",
                 "sso.lib.nyit.edu", "my-login.nyit.edu"):
        assert link_kind(f"https://{host}/x") == "disabled", host
    assert link_kind("https://eportfolios.nyit.edu/") == "page"
    for path in ("/admin", "/admin/site/privacy.php", "/legal.php", "/about.php",
                 "/artefact/file/download.php"):
        assert link_kind("https://eportfolios.nyit.edu" + path) == "excluded", path
    for host in ("wifi.nyit.edu", "biggive.nyit.edu", "lamp.soecs.nyit.edu", "lassos.nyit.edu"):
        assert link_kind(f"https://{host}/") == "page", host


# ---------------------------------------------------------------------------
# 11. optional cache clean-up (orphan files, hosts sites.py no longer crawls)
# ---------------------------------------------------------------------------
def test_prune_cache_reports_then_removes_out_of_scope_entries_and_orphans(settings, two_sites):
    from askbear_scraper.crawler import prune_cache
    from testkit import cache_page

    web = base_web()
    crawl(settings, web)
    dev = cache_page(settings, "https://catalog-dev.nyit.edu/engineering", html_page("Dev copy"))
    write_jsonl(settings.manifest_path, list(manifest(settings).values()) + [dev])
    orphan = settings.html_dir / "courses__cal-gr_cc-phas-601__0123456789.html"   # old flat layout
    orphan.write_text("<html>old</html>", encoding="utf-8")
    before = set(manifest(settings))

    report = prune_cache(settings)                               # dry run: nothing changes
    assert report["dry_run"] and report["entries_removed"] == 1 and report["files_deleted"] == 2
    assert set(manifest(settings)) == before and orphan.exists()

    report = prune_cache(settings, dry_run=False)
    assert report["entries_removed_by_site"] == {"catalog-dev.nyit.edu": 1}
    assert set(manifest(settings)) == before - {"https://catalog-dev.nyit.edu/engineering"}
    assert not orphan.exists() and not (settings.html_dir / dev["file"]).exists()
    for entry in manifest(settings).values():                    # every saved page is still there
        if entry["status"] == "ok":
            assert (settings.html_dir / entry["file"]).exists()


def test_crawl_summary_reports_pagination_thrown_away_by_normalisation(settings, monkeypatch):
    listing = "https://blogs.nyit.edu/cfe-weekly-teaching-notes"
    web = FakeWeb()
    web.add(listing, fixture("blogs_listing.html"))
    for n in (2, 3, 29):
        web.add(f"{listing}?p={n}", html_page(f"Page {n}"))
    blogs = sites.SITES["blogs.nyit.edu"]
    monkeypatch.setitem(sites.SITES, "blogs.nyit.edu",
                        replace(blogs, seeds=("/cfe-weekly-teaching-notes",), keep_query_params=()))
    old = crawl(settings, web, only=["blogs.nyit.edu"])          # the profile before the fix
    assert old["dropped_query_self_links"] == {"blogs.nyit.edu": {"p": 3, "tag": 1}}
    monkeypatch.setitem(sites.SITES, "blogs.nyit.edu",
                        replace(blogs, seeds=("/cfe-weekly-teaching-notes",)))
    new = crawl(Settings(data_dir=settings.data_dir / "new", delay=1.0, jitter=0.0), web,
                only=["blogs.nyit.edu"])
    assert new["dropped_query_self_links"] == {"blogs.nyit.edu": {"tag": 1}}
    assert {f"{listing}?p={n}" for n in (2, 3, 29)} <= set(web.page_requests())


def test_faculty_accomplishments_pages_are_crawled_without_their_filters():
    base = "https://www.nyit.edu/faculty-and-staff/accomplishments/"
    href = "/faculty-and-staff/accomplishments/?&fsa_keyword=&fsa_school=&fsa_role=&fsa_type=&fsa_topic=&fsa_page=128"
    assert normalize_url(href, base) == base + "?fsa_page=128"
    filtered = ("/faculty-and-staff/accomplishments/?fsa_keyword=&fsa_school=College+of+Arts+%26+Sciences"
                "&fsa_page=1")
    assert normalize_url(filtered, base) == base + "?fsa_page=1"
    assert normalize_url("https://www.nyit.edu/news/?page=2") == "https://www.nyit.edu/news/"
    assert normalize_url("https://vancouver.nyit.edu/x/?fsa_page=2") == "https://vancouver.nyit.edu/x/"
