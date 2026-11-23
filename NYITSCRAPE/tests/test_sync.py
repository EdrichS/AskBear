"""Regression tests for the integration ("sync") round: the CLI's crawl exit code, prune and
--refresh-sitemaps, crawl-run history in crawl_summary.json, extension-less PDF links and
redirect outcomes in the `docs` step, the new validate/stats checks, event dates, soft-404
and paginated listing pages in `parse`, and the LibCal (libcal.nyit.edu) pages.

Everything runs offline (testkit.FakeWeb); the LibCal pages in tests/fixtures/sync/ are
synthetic copies of the real templates (no personal data).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from askbear_scraper import cli, pipeline, sites, stats, validate
from askbear_scraper import documents as dc
from askbear_scraper import site_parser as sp
from askbear_scraper.config import Settings
from askbear_scraper.crawler import Crawler, merge_summaries
from askbear_scraper.models import PAGE_TYPES, PageRecord, SectionRecord
from askbear_scraper.urls import normalize_url
from askbear_scraper.utils import write_json

import testkit
from testkit import FakeClock, FakeWeb, html_page, make_fetcher, read_jsonl, write_jsonl

SYNC = Path(__file__).resolve().parent / "fixtures" / "sync"
FIXES_CRAWL = Path(__file__).resolve().parent / "fixtures" / "fixes_crawl"
FETCHED = "2026-11-23T15:00:30+00:00"


def sync_fixture(name: str) -> str:
    return (SYNC / name).read_text(encoding="utf-8")


def inputs(settings: Settings, **fields) -> validate.Inputs:
    base = dict(settings=settings, manifest={}, crawl_summary=None, parse_summary=None,
                pages=None, sections=None, courses=None, corpus=None, pdf_pages=None,
                pdf_summary=None)
    base.update(fields)
    return validate.Inputs(**base)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _fake_run(summary):
    def run(self):
        return dict(summary)
    return run


def test_crawl_exits_1_when_an_enabled_website_saved_no_page(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Crawler, "run", _fake_run({"interrupted": False, "failed_sites": ["catalog.nyit.edu"]}))
    assert cli.main(["crawl", "--data-dir", str(tmp_path / "d")]) == 1
    monkeypatch.setattr(Crawler, "run", _fake_run({"interrupted": False, "failed_sites": []}))
    assert cli.main(["crawl", "--data-dir", str(tmp_path / "d")]) == 0
    monkeypatch.setattr(Crawler, "run", _fake_run({"interrupted": True}))
    assert cli.main(["crawl", "--data-dir", str(tmp_path / "d")]) == 1


def test_all_stops_before_parse_when_a_website_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(Crawler, "run", _fake_run({"interrupted": False, "failed_sites": ["catalog.nyit.edu"]}))
    called = []
    monkeypatch.setattr(cli, "cmd_parse", lambda s, a: called.append("parse") or 0)
    assert cli.main(["all", "--skip-pdf", "--skip-docs", "--data-dir", str(tmp_path / "d")]) == 1
    assert called == []


@pytest.mark.parametrize("command", ["crawl", "all"])
def test_refresh_sitemaps_option(command):
    parser = cli.build_parser()
    assert cli.settings_from_args(parser.parse_args([command, "--refresh-sitemaps"])).refresh_sitemaps
    assert not cli.settings_from_args(parser.parse_args([command])).refresh_sitemaps
    assert Settings().refresh_sitemaps is False


def test_prune_reports_first_and_deletes_only_with_yes(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    keep = testkit.cache_page(settings, "https://www.nyit.edu/about/", html_page("About"))
    dev = testkit.cache_page(settings, "https://catalog-dev.nyit.edu/engineering", html_page("Dev copy"))
    orphan = settings.html_dir / "www.nyit.edu" / "orphan__0123456789.html"
    orphan.write_text(html_page("Orphan"), encoding="utf-8")
    write_jsonl(settings.manifest_path, [keep, dev])

    assert cli.main(["prune", "--data-dir", str(settings.data_dir)]) == 0
    out = capsys.readouterr().out
    assert "1 manifest entries would be removed (catalog-dev.nyit.edu 1)" in out
    assert "2 cached HTML files would be deleted" in out and "prune --yes" in out
    assert orphan.exists() and (settings.html_dir / dev["file"]).exists()
    assert len(read_jsonl(settings.manifest_path)) == 2

    assert cli.main(["prune", "--yes", "--data-dir", str(settings.data_dir)]) == 0
    assert "were removed" in capsys.readouterr().out
    assert not orphan.exists() and not (settings.html_dir / dev["file"]).exists()
    assert (settings.html_dir / keep["file"]).exists()
    assert [e["url"] for e in read_jsonl(settings.manifest_path)] == [keep["url"]]


# ---------------------------------------------------------------------------
# crawl_summary.json: run history and page limits
# ---------------------------------------------------------------------------
def test_merge_summaries_keeps_the_history_of_runs():
    full = {"sites": ["catalog.nyit.edu", "www.nyit.edu"], "finished_at": "2026-11-23T11:06:10+00:00",
            "seconds": 7692.0, "network_requests": 6520, "urls_visited": 7829, "interrupted": False,
            "status_by_site": {"catalog.nyit.edu": {"ok": 9}, "www.nyit.edu": {"ok": 5}},
            "capped_sites": {"www.nyit.edu": 3}}          # written by an older crawler: no "runs"
    run = {"finished_at": "2026-11-23T15:01:03+00:00", "seconds": 52.0, "network_requests": 6,
           "sites": ["libcal.nyit.edu"], "urls_visited": 4, "interrupted": False,
           "status_by_site": {"libcal.nyit.edu": {"ok": 4}}, "capped_sites": {},
           "page_limits": {}}
    run["last_run"] = {k: run[k] for k in ("finished_at", "seconds", "network_requests", "sites",
                                            "urls_visited", "interrupted")}
    merged = merge_summaries(full, run)
    assert [r["sites"] for r in merged["runs"]] == [["catalog.nyit.edu", "www.nyit.edu"], ["libcal.nyit.edu"]]
    assert merged["seconds"] == 7744.0 and merged["network_requests"] == 6526
    assert merged["finished_at"] == run["finished_at"] and merged["last_run"] == run["last_run"]
    assert merged["urls_visited"] == 18 and merged["capped_sites"] == {"www.nyit.edu": 3}
    again = merge_summaries(merged, dict(run, last_run=dict(run["last_run"], seconds=10.0)))
    assert len(again["runs"]) == 3 and again["seconds"] == 7754.0


def test_merge_summaries_counts_the_sites_of_the_run_in_the_manifest():
    previous = {"sites": ["www.nyit.edu", "blogs.nyit.edu"], "runs": [{"seconds": 100.0, "network_requests": 50}],
                "status_by_site": {"www.nyit.edu": {"ok": 2, "not_found": 2}, "blogs.nyit.edu": {"ok": 1}}}
    run = {"sites": ["www.nyit.edu"], "seconds": 5.0, "network_requests": 1,
           "status_by_site": {"www.nyit.edu": {"ok": 2}}}      # the dead links only blogs link to
    run["last_run"] = dict(run)
    manifest = {"https://www.nyit.edu/a/": {"site": "www.nyit.edu", "status": "ok"},
                "https://www.nyit.edu/b/": {"site": "www.nyit.edu", "status": "ok"},
                "https://www.nyit.edu/news/gone/": {"site": "www.nyit.edu", "status": "not_found"},
                "https://www.nyit.edu/bio/gone/": {"site": "www.nyit.edu", "status": "not_found"},
                "https://blogs.nyit.edu/x": {"site": "blogs.nyit.edu", "status": "ok"}}
    merged = merge_summaries(previous, run, manifest)
    assert merged["status_by_site"] == {"blogs.nyit.edu": {"ok": 1}, "www.nyit.edu": {"ok": 2, "not_found": 2}}
    assert merged["urls_visited"] == 5 and merged["network_requests"] == 51


def test_a_resumed_full_crawl_keeps_the_run_history(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", delay=1.0, jitter=0.0, max_retries=0)
    web = FakeWeb()
    web.add("https://www.nyit.edu/", html_page("Home"))
    earlier = {"sites": ["www.nyit.edu"], "runs": [{"finished_at": "2026-11-23T11:06:10+00:00", "seconds": 7692.0,
                                                    "network_requests": 6520, "interrupted": False}]}
    settings.ensure_dirs()
    write_json(settings.raw_dir / "crawl_summary.json", earlier)
    only_www = {"www.nyit.edu": sites.SITES["www.nyit.edu"]}

    def crawl(refresh: bool) -> dict:
        settings.refresh = refresh
        with patch.dict(sites.SITES, only_www, clear=True):
            return Crawler(settings, fetcher=make_fetcher(settings, web, FakeClock())).run()

    summary = crawl(False)       # a full crawl that reuses the cache: its pages are still there
    assert [r.get("seconds") for r in summary["runs"]][0] == 7692.0 and len(summary["runs"]) == 2
    assert summary["network_requests"] == 6520 + summary["last_run"]["network_requests"]
    summary = crawl(True)        # --refresh downloads everything again: a new history
    assert summary["runs"] == [summary["last_run"]]


def test_a_capped_website_records_the_limit_it_reached(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", delay=1.0, jitter=0.0, max_retries=0)
    monkeypatch.setitem(sites.SITES, "www.nyit.edu",
                        replace(sites.SITES["www.nyit.edu"], seeds=("/",), sitemaps=()))
    web = FakeWeb()
    web.add("https://www.nyit.edu/", html_page("Home", links=["/a/", "/b/", "/c/"]))
    for path in ("a", "b", "c"):
        web.add(f"https://www.nyit.edu/{path}/", html_page(path.upper()))
    settings.max_pages = 2
    summary = Crawler(settings, fetcher=make_fetcher(settings, web, FakeClock()),
                      only_sites=["www.nyit.edu"]).run()
    assert summary["capped_sites"] == {"www.nyit.edu": 2}
    assert summary["page_limits"] == {"www.nyit.edu": 2}
    assert summary["runs"] == [summary["last_run"]]


# ---------------------------------------------------------------------------
# docs: extension-less PDF links, redirect outcomes
# ---------------------------------------------------------------------------
ONLINE_PDF = "https://online.nyit.edu/documents/masters-degree-or-bootcamp"


def test_pdf_served_from_a_url_without_pdf_extension_is_a_candidate(settings):
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [
        {"url": ONLINE_PDF, "kind": "pdf", "anchor_text": "", "found_on": [ONLINE_PDF],
         "served_as_pdf": True, "content_type": "application/pdf"},
        {"url": "https://online.nyit.edu/documents/other", "kind": "pdf", "anchor_text": "",
         "found_on": "https://online.nyit.edu/", "content_type": "application/pdf; charset=binary"},
        {"url": "https://www.nyit.edu/about/", "kind": "pdf", "anchor_text": "", "found_on": []},
    ])
    found = dc.find_candidates(settings)
    assert set(found.items) == {ONLINE_PDF, "https://online.nyit.edu/documents/other"}
    assert found.skipped[dc.INVALID_URL] == {"https://www.nyit.edu/about/"}
    assert dc.doc_filename(ONLINE_PDF).startswith("online.nyit.edu/masters-degree-or-bootcamp__")
    assert dc.doc_filename(ONLINE_PDF).endswith(".pdf")


def test_docs_downloads_an_extension_less_pdf(settings, tmp_path):
    settings.ensure_dirs()
    settings.max_retries = 0
    web = FakeWeb()
    web.add(ONLINE_PDF, testkit.pdf_bytes(tmp_path, pages=["Master's degree or bootcamp?"]),
            content_type="application/pdf")
    write_jsonl(settings.external_links_path, [
        {"url": ONLINE_PDF, "kind": "pdf", "anchor_text": "Download the guide",
         "found_on": ["https://online.nyit.edu/blog/x"], "served_as_pdf": True}])
    summary = dc.run_docs(settings, fetcher=make_fetcher(settings, web))
    assert "error" not in summary
    pages = read_jsonl(settings.processed_dir / dc.DOCUMENTS_FILE)
    assert [p["pdf_url"] for p in pages] == [ONLINE_PDF]
    assert "bootcamp" in pages[0]["text"]


@pytest.mark.parametrize("routes, status", [
    # a redirect to another organisation is recorded, never requested
    ({"https://www.nyit.edu/files/a.pdf": ("redirect", "https://example.com/a.pdf")}, dc.OFFSITE),
    # ... and so is a redirect to a sign-in portal
    ({"https://www.nyit.edu/files/a.pdf": ("redirect", "https://sso.nyit.edu/login")}, dc.OFFSITE),
    # a redirect loop
    ({"https://www.nyit.edu/files/a.pdf": ("redirect", "https://www.nyit.edu/files/b.pdf"),
      "https://www.nyit.edu/files/b.pdf": ("redirect", "https://www.nyit.edu/files/a.pdf")}, dc.REDIRECT_LOOP),
    # the target's robots.txt forbids it
    ({"https://www.nyit.edu/files/a.pdf": ("redirect", "https://files.nyit.edu/private/a.pdf")}, dc.ROBOTS),
])
def test_download_pdf_redirect_outcomes(settings, tmp_path, routes, status):
    web = FakeWeb()
    web.robots("files.nyit.edu", "User-agent: *\nDisallow: /private/\n")
    for url, (_, target) in routes.items():
        web.redirect(url, target)
    settings.max_retries = 0
    fetcher = make_fetcher(settings, web)
    out = dc.download_pdf(fetcher, "https://www.nyit.edu/files/a.pdf", tmp_path / "a.pdf")
    assert out["status"] == status, out
    assert status in dc.FINAL_FAILURES                       # not asked again by the next run
    assert not any("example.com" in u or "sso.nyit.edu" in u or "/private/" in u for u in web.requests)
    assert not (tmp_path / "a.pdf").exists()


def test_follows_redirect_only_to_crawled_nyit_websites():
    assert dc.follows_redirect("https://files.nyit.edu/files/x.pdf")
    assert dc.follows_redirect("https://catalog.nyit.edu/files/catalog/archives/X_AY2526.pdf")
    assert not dc.follows_redirect("https://example.com/x.pdf")
    assert not dc.follows_redirect("https://my.nyit.edu/x.pdf")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_crawl_check_fails_when_an_enabled_website_saved_no_page(settings):
    d = inputs(settings, manifest={"https://www.nyit.edu/": testkit.manifest_entry("https://www.nyit.edu/", "x")},
               crawl_summary={"sites": ["catalog.nyit.edu", "www.nyit.edu"], "interrupted": False,
                              "still_queued": 0, "finished_at": "2026-11-24T00:00:00+00:00",
                              "failed_sites": ["catalog.nyit.edu"],
                              "unreachable_reasons": {"catalog.nyit.edu": "robots.txt: HTTP 503"}})
    c = validate.check_crawl_finished(d)
    assert c.status == validate.FAIL and "catalog.nyit.edu" in c.message
    assert c.examples == ["catalog.nyit.edu: robots.txt: HTTP 503"]


def test_crawl_check_names_the_last_restricted_run(settings):
    runs = [{"sites": ["catalog.nyit.edu", "www.nyit.edu"], "finished_at": "2026-11-23T11:06:10+00:00"},
            {"sites": ["libcal.nyit.edu"], "finished_at": "2026-11-23T15:01:03+00:00"}]
    d = inputs(settings, manifest={"https://www.nyit.edu/": testkit.manifest_entry("https://www.nyit.edu/", "x")},
               crawl_summary={"sites": ["catalog.nyit.edu", "www.nyit.edu", "libcal.nyit.edu"],
                              "interrupted": False, "still_queued": 0, "failed_sites": [],
                              "finished_at": "2026-11-23T15:01:03+00:00", "urls_visited": 9,
                              "runs": runs, "last_run": runs[-1]})
    c = validate.check_crawl_finished(d)
    assert c.status == validate.PASS
    assert "2 crawl runs, the last one only for libcal.nyit.edu" in c.message


def test_site_limits_use_the_limit_of_the_crawl_not_the_last_runs_settings(settings):
    d = inputs(settings, crawl_summary={
        "sites": ["libguides.nyit.edu", "libcal.nyit.edu"], "capped_sites": {"libguides.nyit.edu": 914},
        "status_by_site": {"libguides.nyit.edu": {"ok": 400}}, "given_up_sites": {},
        "unreachable_sites": [], "settings": {"max_pages_per_site": 100},
        "last_run": {"sites": ["libcal.nyit.edu"]}})
    c = validate.check_site_limits(d)
    assert c.status == validate.WARN
    limit = sites.SITES["libguides.nyit.edu"].max_pages
    assert c.examples[0].startswith("libguides.nyit.edu: limit 400 pages reached, 914 queued URLs")
    assert f"sites.py now allows {limit:,}" in c.examples[0]
    # a summary that records the limit (current crawler) is taken at its word
    d.crawl_summary["page_limits"] = {"libguides.nyit.edu": 250}
    assert validate.check_site_limits(d).examples[0].startswith("libguides.nyit.edu: limit 250 pages")


def test_redirect_loops_are_reported_as_a_problem_on_nyits_side(settings):
    url = "https://www.nyit.edu/old/"
    d = inputs(settings, manifest={url: testkit.manifest_entry(url, None, status="redirect_loop",
                                                               http_status=301)})
    c = validate.check_crawl_errors(d)
    assert c.status == validate.WARN and "redirect loop" in c.message
    assert c.examples == [f"{url}  (redirect loop)"]


def _page(url, title, text="", page_type="page", **extra):
    return dict(url=url, title=title, page_title=title, text=text,
                word_count=len(text.split()), page_type=page_type, **extra)


def test_real_pages_check_flags_sign_in_screens_and_server_default_pages(settings):
    pages = [_page("https://eportfolios.nyit.edu/", "Log in | Mahara", "Username Password"),
             _page("https://nyitetic.nyit.edu/", "IIS Windows Server", ""),
             _page("https://www.nyit.edu/about/", "About", "About New York Tech " * 30)]
    c = validate.check_real_pages(inputs(settings, manifest=None, pages=pages))
    assert c.status == validate.FAIL and c.count == 2
    assert any("sign-in screen" in e for e in c.examples)
    assert validate.check_real_pages(inputs(settings, manifest=None, pages=pages[2:])).status == validate.PASS


def test_pagination_check(settings):
    m = {u: testkit.manifest_entry(u, "x") for u in (
        "https://blogs.nyit.edu/cfe-weekly-teaching-notes", "https://www.nyit.edu/about/")}
    c = validate.check_pagination(inputs(settings, manifest=m))
    assert c.status == validate.WARN
    assert any(e.startswith("blogs.nyit.edu: sites.py follows ?p= page links") for e in c.examples)
    assert any(e.startswith("www.nyit.edu: sites.py follows ?fsa_page/?mrp_page=") for e in c.examples)
    for u in ("https://blogs.nyit.edu/cfe-weekly-teaching-notes?p=2",
              "https://www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=2"):
        m[u] = testkit.manifest_entry(u, "x")
    # each page-number parameter counts on its own: www's ?mrp_page= listing is still missing
    c = validate.check_pagination(inputs(settings, manifest=m))
    assert c.status == validate.WARN and len(c.examples) == 1
    assert c.examples[0].startswith("www.nyit.edu: sites.py follows ?mrp_page=")
    u = "https://www.nyit.edu/medicine/college-of-osteopathic-medicine/about/research-at-nyitcom/projects/?mrp_page=2"
    m[u] = testkit.manifest_entry(u, "x")
    assert validate.check_pagination(inputs(settings, manifest=m)).status == validate.PASS
    # recorded by the crawler: a page-number parameter that was normalised away
    summary = {"sites": ["blogs.nyit.edu"], "dropped_query_self_links": {"blogs.nyit.edu": {"p": 7, "tag": 9}}}
    c = validate.check_pagination(inputs(settings, manifest=m, crawl_summary=summary))
    assert c.status == validate.WARN and c.count == 1 and "?p=" in c.examples[0]


def test_dates_check_uses_event_start_for_events_and_dates_communications(settings):
    pages = [_page(f"https://events.nyit.edu/event/{i}", "E", "x " * 20, "event",
                   published=None, event_start=f"2026-12-0{i}T12:00:00-05:00") for i in range(1, 6)]
    pages += [_page(f"https://files.nyit.edu/files/m{i}.html", "M", "x " * 20, "communication",
                    published="2021-07-29") for i in range(5)]
    c = validate.check_dates(inputs(settings, pages=pages))
    assert c.status == validate.PASS
    assert "event 100% of 5" in c.message and "communication 100% of 5" in c.message


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def test_stats_show_every_crawl_run_and_redirect_loops():
    assert stats.STATUS_LABEL["redirect_loop"] == "redirect loop"
    assert "redirect_loop" in stats.STATUS_ORDER
    manifest = {"https://www.nyit.edu/": testkit.manifest_entry("https://www.nyit.edu/", "x"),
                "https://www.nyit.edu/old/": testkit.manifest_entry("https://www.nyit.edu/old/", None,
                                                                    status="redirect_loop")}
    runs = [{"sites": ["www.nyit.edu"], "finished_at": "2026-11-23T11:06:10+00:00", "seconds": 60,
             "urls_visited": 2, "network_requests": 3},
            {"sites": ["libcal.nyit.edu"], "finished_at": "2026-11-23T15:01:03+00:00", "seconds": 52,
             "urls_visited": 4, "network_requests": 6}]
    summary = {"sites": ["www.nyit.edu", "libcal.nyit.edu"], "runs": runs, "last_run": runs[-1],
               "finished_at": runs[-1]["finished_at"], "seconds": 112, "network_requests": 9,
               "interrupted": False, "still_queued": 0, "failed_sites": []}
    crawl = stats.crawl_stats(manifest, summary)
    assert crawl["sites_in_last_run"] == ["libcal.nyit.edu"]
    assert [r["sites"] for r in crawl["runs"]] == [["www.nyit.edu"], ["libcal.nyit.edu"]]
    text = stats.render_text(stats._crawl_blocks(crawl))
    assert "Crawl runs" in text and "Network requests (all runs)" in text and "redirect loop" in text


def test_corpus_stats_count_contact_details():
    records = [stats._slim_corpus({"id": "a", "source": "section", "metadata": {"site": "www.nyit.edu"},
                                   "text": "Write to registrar@nyit.edu or call 516.686.7520."}),
               stats._slim_corpus({"id": "b", "source": "document", "metadata": {},
                                   "text": "Contact Registrar@NYIT.edu and help@example.org"}),
               stats._slim_corpus({"id": "c", "source": "course", "metadata": {},
                                   "text": "CSCI 260: Data Structures, 3.0 credits (2026-2027)."})]
    contacts = stats.corpus_stats(records)["contacts"]
    assert contacts == {"records_with_email": 2, "distinct_emails": 2, "distinct_nyit_emails": 1,
                        "records_with_email_by_source": {"section": 1, "document": 1},
                        "records_with_phone": 1}


def test_page_stats_report_event_dates_separately():
    pages = [{"url": "u1", "page_type": "event", "event_start": "2026-12-02T12:30:00-05:00"},
             {"url": "u2", "page_type": "news", "published": "2026-11-01"}]
    st = stats.page_stats(pages)
    assert (st["dated_pages"], st["events_with_time"], st["first_event"]) == (1, 1, "2026-12-02")


# ---------------------------------------------------------------------------
# parse: soft-404 pages, listing pages 2+, event metadata
# ---------------------------------------------------------------------------
def test_parse_skips_web_server_default_pages_and_paginated_listings(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    listing = (FIXES_CRAWL / "blogs_listing.html").read_text(encoding="utf-8")
    page1 = normalize_url("https://blogs.nyit.edu/cfe-weekly-teaching-notes")
    page2 = normalize_url("https://blogs.nyit.edu/cfe-weekly-teaching-notes?&p=2")
    assert page2.endswith("?p=2")
    entries = [
        testkit.cache_page(settings, page1, listing),
        testkit.cache_page(settings, page2, listing.replace("Brief hybrids", "Older post")),
        testkit.cache_page(settings, "https://www.nyit.edu/about/",
                           (FIXES_CRAWL / "iis_default.html").read_text(encoding="utf-8")),
        testkit.cache_page(settings, testkit.fixture_url("www_news.html"), testkit.fixture_html("www_news.html")),
    ]
    write_jsonl(settings.manifest_path, entries)
    summary = pipeline.run_parse(settings)
    assert "https://www.nyit.edu/about/" in summary["skipped"]["not_found_page"]
    pages = {p["url"]: p for p in read_jsonl(settings.processed_dir / pipeline.PAGES_FILE)}
    assert "https://www.nyit.edu/about/" not in pages
    assert pages[page2]["page_type"] == "listing" and pages[page2]["title"].endswith("(page 2)")
    sections = read_jsonl(settings.processed_dir / pipeline.SECTIONS_FILE)
    with_sections = {s["url"].split("#")[0] for s in sections}
    assert page2 not in with_sections and page1 in with_sections
    assert summary["listing_pages_not_indexed"] == 1


def test_listing_page_numbers():
    assert pipeline.listing_page_number("https://blogs.nyit.edu/x?p=3") == 3
    assert pipeline.listing_page_number("https://blogs.nyit.edu/x") == 1
    assert pipeline.is_listing_continuation("listing", "https://online.nyit.edu/blog?p=2")
    assert not pipeline.is_listing_continuation("page", "https://online.nyit.edu/blog?p=2")
    # the accomplishments pages hold the accomplishments themselves: always indexed
    assert not pipeline.is_listing_continuation(
        "listing", "https://www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=4")


def test_looks_like_not_found_with_text():
    assert pipeline.looks_like_not_found("IIS Windows Server", "", 0, text="")
    assert not pipeline.looks_like_not_found("IIS Windows Server", "", 0)        # titles alone
    assert not pipeline.looks_like_not_found("About", "About", 3, text="About New York Tech")


def test_section_corpus_records_carry_event_times():
    page = PageRecord(doc_id="page-1", url="https://libcal.nyit.edu/event/1", title="Workshop",
                      page_title="Workshop", breadcrumbs=[], catalog_year="", page_type="event",
                      area=None, school=None, program=None, degree=None, level=None, campuses=[],
                      headings=[], text="x", word_count=1, num_tables=0, links=[], content_sha1="",
                      fetched_at=FETCHED, site="libcal.nyit.edu",
                      event_start="2026-12-02T12:30:00-05:00", event_end="2026-12-02T13:30:00-05:00")
    sec = SectionRecord(section_id="page-1-s000", doc_id="page-1", url=page.url, page_title="Workshop",
                        heading="", heading_path=[], text="A workshop.", word_count=2, order=0,
                        page_type="event", site="libcal.nyit.edu")
    meta = pipeline.section_to_corpus(sec, page).metadata
    assert (meta["event_start"], meta["event_end"]) == (page.event_start, page.event_end)
    assert meta["published"] is None
    fields = [f for f in PageRecord.__dataclass_fields__]
    assert fields[-1] == "noindex" and fields.index("event_start") == fields.index("modified") + 1


# ---------------------------------------------------------------------------
# LibCal pages and paginated titles (site_parser)
# ---------------------------------------------------------------------------
def test_libcal_hours_page():
    page, secs = sp.parse_any(sync_fixture("libcal_hours.html"), "https://libcal.nyit.edu/hours", FETCHED)
    assert "library_hours" in PAGE_TYPES and page.page_type == "library_hours"
    assert page.title == "Library Opening Hours (as of November 23, 2026)"
    lines = page.text.splitlines()
    assert lines[0] == "Nov 23 Monday | Nov 24 Tuesday"
    assert "Example Campus Library | 8am – 8pm | 8am – 6pm" in lines
    assert "Example Campus Library | Closed | –" in lines           # partly published week kept
    assert "Example Medical Library | – | –" not in lines             # no hours yet: dropped
    assert "Dec 07 Monday | Dec 08 Tuesday" not in lines              # a week with no row left
    assert "Weekly Hours" not in page.text and "Powered by" not in page.text
    assert page.text.count("Example Campus Library | 8am – 8pm") == 1  # monthly view left out
    assert secs and all(s.page_type == "library_hours" for s in secs)


def test_libcal_event_gets_its_time_from_json_ld():
    page, secs = sp.parse_any(sync_fixture("libcal_event.html"), "https://libcal.nyit.edu/event/1", FETCHED)
    assert page.page_type == "event" and page.title == "Study Skills Workshop"
    assert (page.event_start, page.event_end) == ("2026-12-02T12:30:00-05:00", "2026-12-02T13:30:00-05:00")
    assert page.published is None
    assert all("Event box" not in s.heading_path for s in secs)


def test_libcal_home_page_title():
    html = html_page("LibCal - New York Institute of Technology",
                     "<div id='s-lc-page-maincontent'><h2>Welcome</h2><p>Reserve a group study room "
                     "or schedule an appointment with a librarian.</p></div>")
    page, _ = sp.parse_any(html, "https://libcal.nyit.edu/", FETCHED)
    assert page.title == "LibCal: Library Room Reservations, Librarian Appointments and Events"


@pytest.mark.parametrize("url, number", [
    ("https://www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=4", 4),
    ("https://blogs.nyit.edu/cfe-weekly-teaching-notes?p=2", 2),
    ("https://online.nyit.edu/blog?p=1", 1),
    ("https://www.nyit.edu/about/", 1),
    # LibGuides: p is the id of a guide's tab, not a page number
    ("https://libguides.nyit.edu/c.php?g=61858&p=398465", 1),
])
def test_page_number(url, number):
    assert sp.page_number(url) == number
    assert pipeline.listing_page_number(url) == (1 if "fsa_page" in url else number)


def test_libguides_tab_keeps_its_title():
    body = ("<div id='s-lg-guide-main'><h2>Specialized Statistics</h2><p>Statistics from "
            "government agencies and research organizations, by topic.</p></div>")
    page, _ = sp.parse_any(html_page("Specialized Statistics - Statistical Resources", body),
                           "https://libguides.nyit.edu/c.php?g=61858&p=398465", FETCHED)
    assert "(page" not in page.title


def test_blog_gallery_buttons_are_not_text():
    body = ("<div class='blog-item-detail'><div class='blog-item-header'><h2>Grace Phang</h2></div>"
            "<div class='carousel flexslider'><ul class='slides'><li><img src='a.jpg' alt=''>"
            "<p class='flex-caption'>Studio work, spring 2024</p></li></ul>"
            "<div class='carousel-gallery-navigation carousel-navigation'>"
            "<a class='flex-prev' href='#'>Previous</a><a class='flex-next' href='#'>Next</a></div></div>"
            "<p>Grace designed an accessible app for the campus shuttle in her UX/UI course.</p></div>")
    page, secs = sp.parse_any(html_page("Grace Phang | Blogs NYIT", body),
                              "https://blogs.nyit.edu/uxui/grace_phang", FETCHED)
    assert "Previous" not in page.text and "Next" not in page.text
    assert "Studio work, spring 2024" in page.text and "shuttle" in page.text


def test_paginated_pages_get_a_page_number_in_their_title():
    body = ("<main><h1>Faculty &amp; Staff Accomplishments</h1><div class='List'><div class='Card'>"
            "<h3>A. Professor</h3><p>Published a paper on information retrieval in a journal.</p>"
            "</div></div></main>")
    page, secs = sp.parse_any(html_page("Faculty & Staff Accomplishments", body),
                              "https://www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=3", FETCHED)
    assert page.title == "Faculty & Staff Accomplishments (page 3)"
    assert any(s.heading == "A. Professor" for s in secs)


def test_libcal_profile_and_template():
    profile = sites.SITES["libcal.nyit.edu"]
    assert profile.enabled
    assert sp.detect_template("libcal.nyit.edu", None).name == "libcal"
    assert json.loads(json.dumps(sp._HOST_TEMPLATES))["libcal.nyit.edu"] == "libcal"


# ---------------------------------------------------------------------------
# robustness: a download cut off mid-stream, parse determinism
# ---------------------------------------------------------------------------
class _CutBody(testkit._Body):
    """A response body whose connection drops after the first 100 bytes."""

    def stream(self, chunk_size=1024, decode_content=None):
        import urllib3

        yield self.read(100)
        raise urllib3.exceptions.ProtocolError("Connection broken: IncompleteRead(100 bytes read)")


class _CutWeb(FakeWeb):
    def _response(self, request, reply):
        resp = super()._response(request, reply)
        if request.url.endswith("/cut.pdf"):
            resp.raw = _CutBody(reply.content())
        return resp


def test_a_pdf_download_cut_off_mid_stream_leaves_no_file_and_is_retried_later(settings, tmp_path):
    web = _CutWeb()
    url = "https://www.nyit.edu/files/cut.pdf"
    web.add(url, testkit.pdf_bytes(tmp_path, pages=["A long handbook page."] * 3),
            content_type="application/pdf")
    settings.max_retries = 1
    dest = tmp_path / "out" / "cut.pdf"
    out = dc.download_pdf(make_fetcher(settings, web), url, dest)
    assert out["status"] == dc.ERROR and "ChunkedEncodingError" in out["error"]
    assert dc.ERROR not in dc.FINAL_FAILURES                 # the next `docs` run tries again
    assert web.count(lambda u: u == url) == 2                 # one retry
    assert not dest.exists() and list(dest.parent.glob("*")) == []   # no partial file left


def test_parse_is_deterministic(tmp_path):
    outputs = []
    for name in ("first", "second"):
        settings = Settings(data_dir=tmp_path / name / "data")
        testkit.build_fixture_crawl(settings)
        pipeline.run_parse(settings)
        outputs.append({f: (settings.processed_dir / f).read_bytes()
                        for f in (pipeline.PAGES_FILE, pipeline.SECTIONS_FILE, pipeline.COURSES_FILE,
                                  pipeline.COURSES_CSV_FILE, pipeline.CORPUS_FILE)})
    assert outputs[0] == outputs[1]
    assert all(outputs[0].values())


def test_near_duplicate_catalog_pdf_pages_keep_the_newer_edition():
    """`parse --corpus-with-pdf`: when two editions print the same page, the copy kept is the
    current edition's (the ids alone would sort 2025-2026 first)."""
    text = ("Students must complete the core curriculum, the major requirements and the free "
            "electives listed below to earn the degree. ") * 4
    pages = [{"doc_id": f"pdf-{ed}-undergraduate-p0100", "edition": ed, "catalog": "undergraduate",
              "page": 100, "page_count": 300, "text": text, "toc_path": ["Programs"],
              "url": f"https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY{code}.pdf#page=100"}
             for ed, code in (("2025-2026", "2526"), ("2026-2027", "2627"))]
    corpus = [pipeline.pdf_page_to_corpus(r) for r in pages]
    kept, dropped, info = pipeline.collapse_near_duplicates(
        corpus, {r["doc_id"]: r["text"] for r in pages})
    assert [r.id for r in kept] == ["pdf-2026-2027-undergraduate-p0100"]
    assert info["left_out_by_source"] == {"pdf_page": 1}
    ranks = sorted(corpus, key=pipeline.corpus_rank)
    assert ranks[0].metadata["edition"] == "2026-2027"


def test_pdfs_recorded_as_non_html_in_the_crawl_manifest_are_candidates(settings, tmp_path):
    """Crawls made before external_links.jsonl listed PDFs at URLs without ".pdf" recorded them
    only in the manifest (status non_html): `docs` finds them there, without a new crawl."""
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [])
    write_jsonl(settings.manifest_path, [
        testkit.manifest_entry(ONLINE_PDF, None, status="non_html", http_status=200,
                               content_type="application/pdf;charset=UTF-8",
                               referrer="https://online.nyit.edu/blog/bootcamp"),
        testkit.manifest_entry("https://comapp.nyit.edu/x.skill", None, status="non_html",
                               content_type="application/zip")])
    found = dc.find_candidates(settings)
    assert set(found.items) == {ONLINE_PDF} and found.manifest_pdfs == 1
    assert found.items[ONLINE_PDF]["found_on"] == ["https://online.nyit.edu/blog/bootcamp"]
    summary = dc.run_docs(settings, max_docs=0, fetcher=make_fetcher(settings, FakeWeb()))
    assert summary["input"]["manifest_pdf_urls"] == 1
    assert summary["skipped"] == {"max_docs": 1}                    # left for a run with network
