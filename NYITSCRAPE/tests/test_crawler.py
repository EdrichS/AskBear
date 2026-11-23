"""crawler.py: the multi-site BFS crawler, run against an in-memory web (testkit.FakeWeb)
through the real PoliteFetcher (fake clock: delays are recorded, not waited for)."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from askbear_scraper import crawler as crawler_mod
from askbear_scraper import sites
from askbear_scraper.config import Settings
from askbear_scraper.crawler import (Crawler, canonical_url, course_dropdown_links, is_login_page,
                                     is_soft_404, load_manifest, meta_robots)
from askbear_scraper.urls import cache_filename
from askbear_scraper.utils import make_soup

from testkit import (FakeClock, FakeWeb, Reply, fixture_html, html_page, make_fetcher, read_jsonl,
                     write_jsonl)

CAT = "https://catalog.nyit.edu"
WWW = "https://www.nyit.edu"
TWO_SITES = ["catalog.nyit.edu", "www.nyit.edu"]


@pytest.fixture
def two_sites(monkeypatch):
    """catalog.nyit.edu and www.nyit.edu with a single seed ("/") and no sitemaps."""
    monkeypatch.setitem(sites.SITES, "catalog.nyit.edu",
                        replace(sites.SITES["catalog.nyit.edu"], seeds=("/",)))
    monkeypatch.setitem(sites.SITES, "www.nyit.edu",
                        replace(sites.SITES["www.nyit.edu"], seeds=("/",), sitemaps=()))
    return TWO_SITES


def run_crawl(settings, web, only=TWO_SITES, clock=None):
    fetcher = make_fetcher(settings, web, clock or FakeClock())
    try:
        return Crawler(settings, fetcher=fetcher, only_sites=only).run()
    except KeyboardInterrupt:  # never let a test interrupt the whole pytest session
        pytest.fail("KeyboardInterrupt escaped Crawler.run()")


def small_web() -> FakeWeb:
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog home", links=[
        "/a", "/b", WWW + "/x/", "/files/catalog/archives/Catalog.pdf", "mailto:registrar@nyit.edu",
        "https://example.com/partner", "https://my.nyit.edu/portal", "/files/foo", "#top",
        "javascript:void(0)"]))
    web.add(CAT + "/a", html_page("A", links=["/c", "/"]))
    web.add(CAT + "/b", html_page("B"))
    web.add(CAT + "/c", html_page("C", links=["/a"]))
    web.add(WWW + "/", html_page("WWW home", links=["/x/", "/y"]))
    web.add(WWW + "/x/", html_page("X", links=[CAT + "/b"]))
    web.add(WWW + "/y/", html_page("Y"))
    return web


# ---------------------------------------------------------------------------
# a full crawl
# ---------------------------------------------------------------------------
def test_bfs_over_two_websites(settings, two_sites):
    web = small_web()
    summary = run_crawl(settings, web)

    pages = web.page_requests()
    assert sorted(pages) == sorted([CAT + "/", CAT + "/a", CAT + "/b", CAT + "/c",
                                    WWW + "/", WWW + "/x/", WWW + "/y/"])        # each page once
    manifest = load_manifest(settings.manifest_path)
    assert {u: e["status"] for u, e in manifest.items()} == {u: "ok" for u in pages}
    assert manifest[CAT + "/"]["depth"] == 0
    assert manifest[CAT + "/a"]["depth"] == 1
    assert manifest[CAT + "/c"]["depth"] == 2
    assert manifest[CAT + "/a"]["referrer"] == CAT + "/"
    for url, entry in manifest.items():
        assert entry["site"] == entry["file"].split("/")[0]
        assert entry["file"] == cache_filename(url)
        html = (settings.html_dir / entry["file"]).read_text(encoding="utf-8")
        assert entry["sha1"] and entry["title"] in html and not entry["from_cache"]

    graph = {r["url"]: r["outlinks"] for r in read_jsonl(settings.link_graph_path)}
    assert graph[CAT + "/"] == [CAT + "/a", CAT + "/b", WWW + "/x/"]
    external = {r["url"]: r for r in read_jsonl(settings.external_links_path)}
    assert external[CAT + "/files/catalog/archives/Catalog.pdf"]["kind"] == "pdf"
    assert external[CAT + "/files/catalog/archives/Catalog.pdf"]["found_on"] == [CAT + "/"]
    assert external["https://example.com/partner"]["kind"] == "external"
    assert external["https://my.nyit.edu/portal"]["kind"] == "disabled"
    assert external[CAT + "/files/foo"]["kind"] == "excluded"
    assert not any(u.startswith(("mailto:", "javascript:")) for u in external)

    saved = json.loads((settings.raw_dir / "crawl_summary.json").read_text(encoding="utf-8"))
    assert saved == summary
    assert summary["sites"] == TWO_SITES
    assert summary["status_counts"] == {"ok": 7}
    assert summary["status_by_site"] == {"catalog.nyit.edu": {"ok": 4}, "www.nyit.edu": {"ok": 3}}
    assert summary["urls_visited"] == 7 and summary["still_queued"] == 0
    assert not summary["interrupted"]
    assert summary["linked_but_not_crawled_sites"] == {"my.nyit.edu": 1}
    assert summary["pdf_links"] == 1 and summary["external_links"] == 1
    assert summary["network_requests"] == len(web.requests)    # 7 pages + 2 robots.txt
    assert summary["settings"]["user_agent"].startswith("AskBearBot")


def test_websites_take_turns(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/1", "/2", "/3"]))
    web.add(WWW + "/", html_page("WWW", links=["/1/", "/2/", "/3/"]))
    for i in (1, 2, 3):
        web.add(f"{CAT}/{i}", html_page(f"C{i}"))
        web.add(f"{WWW}/{i}/", html_page(f"W{i}"))
    clock = FakeClock()
    run_crawl(settings, web, clock=clock)
    hosts = [u.split("/")[2] for u in web.page_requests()]
    assert hosts == ["catalog.nyit.edu", "www.nyit.edu"] * 4          # round robin
    # each site waited for its own delay; none waited for the other one
    assert all(s <= settings.delay + 1e-9 for s in clock.sleeps)


def test_only_sites_restricts_the_crawl(settings, two_sites):
    web = small_web()
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert not any(u.startswith(WWW) for u in web.requests)
    assert summary["sites"] == ["catalog.nyit.edu"]
    assert summary["status_counts"] == {"ok": 4}


def test_no_crawlable_site_raises(settings):
    with pytest.raises(ValueError):
        Crawler(settings, fetcher=make_fetcher(settings, FakeWeb()), only_sites=["example.com"]).run()


def test_per_site_page_limit(settings, two_sites):
    settings.max_pages = 2
    web = small_web()
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert summary["status_counts"] == {"ok": 2}
    assert summary["capped_sites"] == {"catalog.nyit.edu": 2}          # /b and /c were dropped
    assert CAT + "/c" not in web.requests


def test_sitemap_pages_are_crawled_and_cached(settings, monkeypatch):
    monkeypatch.setitem(sites.SITES, "www.nyit.edu", replace(
        sites.SITES["www.nyit.edu"], seeds=(), sitemaps=(WWW + "/sitemap_index.xml",)))
    web = FakeWeb()
    web.add(WWW + "/sitemap_index.xml",
            f"<urlset><url><loc>{WWW}/listed</loc><lastmod>2026-01-01</lastmod></url></urlset>",
            content_type="application/xml")
    web.add(WWW + "/listed/", html_page("Listed"))
    summary = run_crawl(settings, web, only=["www.nyit.edu"])
    assert summary["sitemap_pages_by_site"] == {"www.nyit.edu": 1}
    assert load_manifest(settings.manifest_path)[WWW + "/listed/"]["referrer"] == "sitemap"
    cached = json.loads((settings.raw_dir / "sitemaps" / "www.nyit.edu.json").read_text(encoding="utf-8"))
    assert cached["pages"] == [[WWW + "/listed", "2026-01-01"]]
    web.requests.clear()
    run_crawl(settings, web, only=["www.nyit.edu"])
    assert web.requests == []                       # sitemap list and page both from the cache


# ---------------------------------------------------------------------------
# pages that are not saved
# ---------------------------------------------------------------------------
def test_soft_404_and_login_pages_are_not_saved_or_followed(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/no_such_program", "/signin"]))
    web.add(CAT + "/no_such_program", fixture_html("catalog_soft404.html"))
    web.add(CAT + "/signin", html_page("Sign in", body='<form><input type="PASSWORD" name="p"></form>',
                                       links=["/secret"]))
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    manifest = load_manifest(settings.manifest_path)
    assert manifest[CAT + "/no_such_program"]["status"] == "soft404"
    assert manifest[CAT + "/signin"]["status"] == "login_page"
    for url in (CAT + "/no_such_program", CAT + "/signin"):
        assert manifest[url]["file"] is None
        assert not (settings.html_dir / cache_filename(url)).exists()
    assert summary["status_counts"] == {"ok": 1, "soft404": 1, "login_page": 1}
    assert len(web.page_requests()) == 3            # the soft 404's menu links are not followed


def test_nofollow_pages_are_saved_but_their_links_not_followed(settings, two_sites):
    web = FakeWeb()
    head = '<meta name="robots" content="noindex, nofollow"><link rel="canonical" href="/canon">'
    web.add(CAT + "/", html_page("Catalog", links=["/private"], head=head))
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    entry = load_manifest(settings.manifest_path)[CAT + "/"]
    assert entry["status"] == "ok" and entry["noindex"] and entry["nofollow"]
    assert entry["canonical"] == CAT + "/canon"
    assert CAT + "/private" not in web.requests
    graph = {r["url"]: r["outlinks"] for r in read_jsonl(settings.link_graph_path)}
    assert graph[CAT + "/"] == [CAT + "/private"]
    assert summary["status_counts"] == {"ok": 1}


def test_course_dropdown_subjects_are_crawled(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/courses/?cs=CSCI&cal=UG"]))
    web.add(CAT + "/courses?cal=UG&cs=CSCI", fixture_html("catalog_course_listing.html"))
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    for subject in ("ACCT", "ARCH", "AAID", "EENG", "MATH"):
        assert f"{CAT}/courses?cal=UG&cs={subject}" in web.requests
    assert web.requests.count(CAT + "/courses?cal=UG&cs=CSCI") == 1


def test_course_dropdown_links():
    soup = make_soup(fixture_html("catalog_course_listing.html"))
    links = course_dropdown_links(soup, CAT + "/courses?cal=UG&cs=CSCI")
    assert links == [f"{CAT}/courses?cal=UG&cs={s}" for s in ("ACCT", "ARCH", "AAID", "CSCI", "EENG", "MATH")]
    assert course_dropdown_links(soup, CAT + "/courses") == []           # no level: nothing
    assert course_dropdown_links(make_soup("<p>no dropdown</p>"), CAT + "/courses?cal=UG&cs=X") == []


def test_redirects_off_site_or_to_a_skipped_host_are_not_saved(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/apply-form", "/portal"]))
    web.redirect(CAT + "/apply-form", "https://forms.example.com/apply")
    web.add("https://forms.example.com/apply", html_page("Apply"))
    web.redirect(CAT + "/portal", "https://my.nyit.edu/")
    web.add("https://my.nyit.edu/", html_page("Portal"))
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    manifest = load_manifest(settings.manifest_path)
    for url, final in ((CAT + "/apply-form", "https://forms.example.com/apply"),
                       (CAT + "/portal", "https://my.nyit.edu/")):
        assert manifest[url]["status"] == "offsite_redirect"
        assert manifest[url]["final_url"] == final
        assert manifest[url]["file"] is None


def test_http_errors_and_non_html_links(settings, two_sites):
    web = FakeWeb()
    web.add(CAT + "/", html_page("Catalog", links=["/gone", "/broken", "/feed"]))
    web.add(CAT + "/gone", "gone", status=410)
    web.add(CAT + "/broken", "oops", status=500)
    web.add(CAT + "/feed", "<rss/>", content_type="application/rss+xml")
    settings.max_retries = 0
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    manifest = load_manifest(settings.manifest_path)
    assert manifest[CAT + "/gone"]["status"] == "not_found"
    assert manifest[CAT + "/broken"]["status"] == "http_error"
    assert manifest[CAT + "/broken"]["http_status"] == 500
    assert manifest[CAT + "/feed"]["status"] == "non_html"
    assert summary["status_counts"] == {"ok": 1, "not_found": 1, "http_error": 1, "non_html": 1}


def test_redirect_to_a_page_already_visited_is_a_duplicate(settings, two_sites):
    web = small_web()
    web.add(CAT + "/b", html_page("B", links=["/old-b"]))
    web.redirect(CAT + "/old-b", CAT + "/b")
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    entry = load_manifest(settings.manifest_path)[CAT + "/old-b"]
    assert entry["status"] == "duplicate" and entry["final_url"] == CAT + "/b"
    assert summary["status_counts"]["duplicate"] == 1


def test_robots_txt_disallowed_pages_are_never_requested(settings, two_sites):
    web = small_web()
    web.robots("catalog.nyit.edu", "User-agent: *\nDisallow: /b\n")
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert load_manifest(settings.manifest_path)[CAT + "/b"]["status"] == "robots_disallowed"
    assert CAT + "/b" not in web.requests
    assert summary["status_counts"]["robots_disallowed"] == 1


def test_unreachable_website_is_skipped(settings, two_sites):
    import requests

    web = small_web()
    web.routes[WWW + "/robots.txt"] = requests.exceptions.ConnectionError("no route to host")
    summary = run_crawl(settings, web)
    assert summary["unreachable_sites"] == ["www.nyit.edu"]
    entry = load_manifest(settings.manifest_path)[WWW + "/"]
    assert entry["status"] == "error" and "unreachable" in entry["error"]
    assert not any(u.startswith(WWW) and not u.endswith("robots.txt") for u in web.requests)


def test_circuit_breaker_gives_up_on_a_failing_website(settings, two_sites):
    settings.max_retries = 0
    web = FakeWeb()
    web.add(WWW + "/", html_page("WWW", links=[f"/p{i}/" for i in range(1, 7)]))
    for i in range(1, 7):
        web.add(f"{WWW}/p{i}/", "Service Unavailable", status=503)
    summary = run_crawl(settings, web, only=["www.nyit.edu"])
    assert crawler_mod.MAX_CONSECUTIVE_FAILURES == 3
    assert summary["given_up_sites"] == {"www.nyit.edu": 3}
    assert [u for u in web.page_requests() if "/p" in u] == [f"{WWW}/p{i}/" for i in (1, 2, 3)]
    assert summary["status_counts"] == {"ok": 1, "http_error": 3}


def test_a_success_resets_the_failure_count(settings, two_sites):
    settings.max_retries = 0
    web = FakeWeb()
    links = ["/e1/", "/e2/", "/ok/", "/e3/", "/e4/", "/last/"]
    web.add(WWW + "/", html_page("WWW", links=links))
    for path in ("/e1/", "/e2/", "/e3/", "/e4/"):
        web.add(WWW + path, "error", status=500)
    web.add(WWW + "/ok/", html_page("OK"))
    web.add(WWW + "/last/", html_page("Last"))
    summary = run_crawl(settings, web, only=["www.nyit.edu"])
    assert summary["given_up_sites"] == {}
    assert WWW + "/last/" in web.requests


# ---------------------------------------------------------------------------
# cache, resume, manifest
# ---------------------------------------------------------------------------
def test_second_run_reads_everything_from_the_cache(settings, two_sites):
    web = small_web()
    first = run_crawl(settings, web)
    web.requests.clear()
    second = run_crawl(settings, web)
    assert web.page_requests() == []                       # 0 page downloads
    assert len(web.requests) <= len(TWO_SITES)            # at most robots.txt (sitemap lookup)
    assert second["status_counts"] == first["status_counts"]
    entries = load_manifest(settings.manifest_path)
    assert all(e["from_cache"] for e in entries.values())


def test_refresh_downloads_again(settings, two_sites):
    web = small_web()
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    web.requests.clear()
    settings.refresh = True
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert len(web.page_requests()) == 4


def test_second_run_does_not_download_duplicate_redirects_again(settings, two_sites):
    web = small_web()
    web.add(CAT + "/b", html_page("B", links=["/old-b"]))
    web.redirect(CAT + "/old-b", CAT + "/b")
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    web.requests.clear()
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.page_requests() == []


def test_a_capped_rerun_keeps_the_manifest_entries_it_did_not_visit(settings, two_sites):
    web = small_web()
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    before = set(load_manifest(settings.manifest_path))
    capped = Settings(data_dir=settings.data_dir, jitter=0.0, max_pages=1)
    run_crawl(capped, web, only=["catalog.nyit.edu"])
    assert set(load_manifest(settings.manifest_path)) == before


def test_a_site_restricted_rerun_keeps_pages_linked_from_other_sites(settings, two_sites):
    web = small_web()
    web.add(WWW + "/", html_page("WWW home", links=["/x/", "/y", CAT + "/semester-map/computer-science-bs"]))
    web.add(CAT + "/semester-map/computer-science-bs", html_page("Semester Map"))
    run_crawl(settings, web)
    assert CAT + "/semester-map/computer-science-bs" in load_manifest(settings.manifest_path)
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert CAT + "/semester-map/computer-science-bs" in load_manifest(settings.manifest_path)


def test_restricted_crawl_keeps_other_sites_entries(settings, two_sites):
    web = small_web()
    run_crawl(settings, web)
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    manifest = load_manifest(settings.manifest_path)
    assert {u for u in manifest if u.startswith(WWW)} == {WWW + "/", WWW + "/x/", WWW + "/y/"}


def test_failed_pages_are_retried_by_the_next_run(settings, two_sites):
    settings.max_retries = 0
    web = small_web()
    web.add(CAT + "/b", "temporarily broken", status=502)
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert load_manifest(settings.manifest_path)[CAT + "/b"]["status"] == "http_error"
    web.add(CAT + "/b", html_page("B is back"))
    web.requests.clear()
    run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert web.page_requests() == [CAT + "/b"]
    assert load_manifest(settings.manifest_path)[CAT + "/b"]["status"] == "ok"


def test_interrupted_crawl_saves_progress_and_resumes(settings, two_sites):
    web = small_web()
    web.routes[CAT + "/c"] = [KeyboardInterrupt(), Reply(html_page("C"))]
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert summary["interrupted"] and summary["still_queued"] >= 0
    done = load_manifest(settings.manifest_path)
    assert done and all(e["status"] == "ok" for e in done.values())
    web.requests.clear()
    summary = run_crawl(settings, web, only=["catalog.nyit.edu"])
    assert not summary["interrupted"]
    assert web.page_requests() == [CAT + "/c"]               # only what was missing


def test_load_manifest_last_line_wins(tmp_path):
    path = tmp_path / "crawl_manifest.jsonl"
    write_jsonl(path, [
        {"url": CAT + "/a", "status": "error"},
        {"url": CAT + "/b", "status": "ok"},
        {"status": "ok"},                                   # no url: ignored
        {"url": CAT + "/a", "status": "ok", "file": "catalog.nyit.edu/a.html"},
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"url": "' + CAT + '/c", "sta')            # crash in the middle of a line
    manifest = load_manifest(path)
    assert set(manifest) == {CAT + "/a", CAT + "/b"}
    assert manifest[CAT + "/a"] == {"url": CAT + "/a", "status": "ok", "file": "catalog.nyit.edu/a.html"}
    assert load_manifest(tmp_path / "missing.jsonl") == {}


def test_old_flat_cache_layout_is_migrated(settings):
    settings.ensure_dirs()
    flat = "engineering__7ec28515c3.html"
    (settings.html_dir / flat).write_text("<html>engineering</html>", encoding="utf-8")
    write_jsonl(settings.manifest_path, [
        {"url": CAT + "/engineering", "status": "ok", "file": flat},
        {"url": CAT + "/courses?cal=GR&cc=PHAS-601&cs=PHAS", "status": "ok", "file": "courses.html"},
        {"url": CAT + "/lost", "status": "ok", "file": "lost__0000000000.html"},
        {"url": WWW + "/", "status": "ok", "file": "www.nyit.edu/index.html", "site": "www.nyit.edu"},
    ])
    Crawler(settings, fetcher=make_fetcher(settings, FakeWeb()))
    manifest = load_manifest(settings.manifest_path)
    assert set(manifest) == {CAT + "/engineering", CAT + "/lost", WWW + "/"}   # non-canonical URL dropped
    moved = manifest[CAT + "/engineering"]
    assert moved["file"] == "catalog.nyit.edu/" + flat and moved["site"] == "catalog.nyit.edu"
    assert (settings.html_dir / "catalog.nyit.edu" / flat).read_text(encoding="utf-8") == "<html>engineering</html>"
    assert not (settings.html_dir / flat).exists()
    lost = manifest[CAT + "/lost"]
    assert lost["file"] is None and lost["status"] == "error"         # will be downloaded again
    assert manifest[WWW + "/"]["file"] == "www.nyit.edu/index.html"


# ---------------------------------------------------------------------------
# page checks
# ---------------------------------------------------------------------------
def test_is_soft_404():
    assert is_soft_404(make_soup(fixture_html("catalog_soft404.html")))
    assert not is_soft_404(make_soup(fixture_html("catalog_program.html")))
    assert not is_soft_404(make_soup(fixture_html("www_news.html")))
    assert is_soft_404(make_soup("<title>Page not found | New York Tech</title><main>x</main>"))
    assert is_soft_404(make_soup("<title>404 Not Found</title>"))
    assert not is_soft_404(make_soup("<title>Error 4040 codes</title><p>text</p>"))


def test_is_login_page():
    assert is_login_page(make_soup('<form><input type="password"></form>'))
    assert is_login_page(make_soup('<input type="Password" name="pw">'))
    assert not is_login_page(make_soup('<form><input type="text" name="q"></form>'))


@pytest.mark.parametrize("content, expected", [
    ("noindex, nofollow", (True, True)),
    ("NOINDEX", (True, False)),
    ("index, follow", (False, False)),
    ("nofollow", (False, True)),
])
def test_meta_robots(content, expected):
    assert meta_robots(make_soup(f'<head><meta name="Robots" content="{content}"></head>')) == expected


def test_meta_robots_none_means_noindex_and_nofollow():
    assert meta_robots(make_soup('<head><meta name="robots" content="none"></head>')) == (True, True)


def test_meta_robots_absent_and_canonical():
    soup = make_soup('<head><link rel="canonical" href="https://www.nyit.edu/about"></head>')
    assert meta_robots(soup) == (False, False)
    assert canonical_url(soup, WWW + "/x/") == WWW + "/about/"
    assert canonical_url(make_soup("<p>x</p>"), WWW + "/") is None


# ---------------------------------------------------------------------------
# opt-in live smoke test (ASKBEAR_LIVE=1)
# ---------------------------------------------------------------------------
@pytest.mark.live
def test_live_crawl_of_two_real_catalog_pages(tmp_path, monkeypatch):
    """Crawls the catalog homepage and one program page, 2 s apart, into tmp_path."""
    monkeypatch.setitem(sites.SITES, "catalog.nyit.edu", replace(
        sites.SITES["catalog.nyit.edu"], seeds=("/", "/engineering/computer_science"),
        follow_links=False))
    settings = Settings(data_dir=tmp_path / "data", delay=2.0, max_retries=1,
                        contact=os.environ.get("ASKBEAR_CONTACT", ""))
    summary = Crawler(settings, only_sites=["catalog.nyit.edu"]).run()
    manifest = load_manifest(settings.manifest_path)
    assert summary["status_counts"].get("ok") == 2, summary["status_counts"]
    program = manifest[CAT + "/engineering/computer_science"]
    html = (settings.html_dir / program["file"]).read_text(encoding="utf-8")
    assert "Computer Science" in html and program["title"]
    assert summary["network_requests"] <= 4             # robots.txt + 2 pages (+ 1 retry at most)
