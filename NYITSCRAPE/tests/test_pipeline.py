"""pipeline.py: `parse` end to end on a temp data folder built from the HTML fixtures."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import fields

import pytest

from askbear_scraper import pipeline
from askbear_scraper.config import Settings
from askbear_scraper.models import CorpusRecord, CourseRecord, PageRecord, SectionRecord

import testkit
from testkit import (DEV_COPY_URL, DUPLICATE_URL, LEGACY_URL, MISSING_FILE_URL, build_fixture_crawl,
                     cache_page, fixture_html, fixture_url, read_jsonl, write_jsonl)

NEWS_URL = fixture_url("www_news.html")
MAGAZINE_URL = fixture_url("magazine_article.html")
LISTING_URL = fixture_url("catalog_course_listing.html")
SEMESTER_MAP_URL = fixture_url("catalog_semester_map.html")


@pytest.fixture(scope="module")
def parsed(parsed_dataset):
    """The outputs of `parse` on the fixture crawl (conftest.parsed_dataset; read-only)."""
    out = parsed_dataset.processed_dir
    return {
        "settings": parsed_dataset,
        "summary": json.loads((out / "parse_summary.json").read_text(encoding="utf-8")),
        "pages": {p["url"]: p for p in read_jsonl(out / "pages.jsonl")},
        "sections": read_jsonl(out / "sections.jsonl"),
        "courses": read_jsonl(out / "courses.jsonl"),
        "corpus": read_jsonl(out / "corpus.jsonl"),
    }


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------
def test_every_output_file_is_written(parsed):
    out = parsed["settings"].processed_dir
    for name in ("pages.jsonl", "sections.jsonl", "courses.jsonl", "courses.csv", "corpus.jsonl",
                 "parse_summary.json"):
        assert (out / name).is_file(), name
    summary = parsed["summary"]
    assert summary["pages"] == len(parsed["pages"]) == 18      # 17 fixture pages + the exact copy
    assert summary["cached_pages"] == 18
    assert summary["failures"] == []
    assert summary["timings_seconds"]["total"] >= 0


def test_pages_of_every_site_are_parsed_and_routed_by_host(parsed):
    pages = parsed["pages"]
    expected = {name: src for name, src in testkit.fixture_sources().items() if src.get("cache_file")}
    assert set(pages) == {src["final_url"] or src["url"] for src in expected.values()} | {DUPLICATE_URL}
    sites = Counter(p["site"] for p in pages.values())
    assert sites["catalog.nyit.edu"] == 5 and sites["www.nyit.edu"] == 5
    assert len(sites) == 10
    assert pages[fixture_url("catalog_program.html")]["catalog_year"] == "2026-2027"
    assert all(p["catalog_year"] == "" for p in pages.values() if p["site"] != "catalog.nyit.edu")


def test_skipped_entries(parsed):
    skipped = parsed["summary"]["skipped"]
    assert skipped["missing_cache_file"] == [MISSING_FILE_URL]
    assert skipped["out_of_scope"] == [DEV_COPY_URL]               # catalog-dev.nyit.edu is disabled
    assert skipped["same_page_as_other_url"] == [LEGACY_URL]       # the direct URL wins
    assert skipped["offsite_redirect"] == [] and skipped["not_found_page"] == []
    assert not any(url.startswith("https://catalog-dev.") for url in parsed["pages"])
    assert not any(s["url"].startswith("https://catalog-dev.") for s in parsed["sections"])


def test_exact_copies_are_marked_and_get_no_sections(parsed):
    pages = parsed["pages"]
    assert pages[DUPLICATE_URL]["duplicate_of"] == NEWS_URL
    assert pages[NEWS_URL]["duplicate_of"] is None
    assert pages[DUPLICATE_URL]["content_sha1"] == pages[NEWS_URL]["content_sha1"]
    doc_ids = {s["doc_id"] for s in parsed["sections"]}
    assert pages[NEWS_URL]["doc_id"] in doc_ids and pages[DUPLICATE_URL]["doc_id"] not in doc_ids
    assert parsed["summary"]["duplicate_pages"] == 1 and parsed["summary"]["distinct_pages"] == 17


def test_noindex_pages_are_kept_but_not_indexed(parsed):
    page = parsed["pages"][MAGAZINE_URL]
    assert page["noindex"] and page["text"]
    assert not any(s["doc_id"] == page["doc_id"] for s in parsed["sections"])
    assert not any(r["metadata"].get("doc_id") == page["doc_id"] for r in parsed["corpus"])
    assert parsed["summary"]["noindex"] == [MAGAZINE_URL]


def test_catalog_post_processing(parsed):
    pages = parsed["pages"]
    listing = pages[LISTING_URL]
    assert listing["catalog_year"] == "2026-2027"                 # filled from the other catalog pages
    assert not any(s["doc_id"] == listing["doc_id"] for s in parsed["sections"])   # no listing sections
    semester_map = pages[SEMESTER_MAP_URL]
    assert semester_map["school"] == "College of Engineering and Computing Sciences"   # from its program
    map_sections = [s for s in parsed["sections"] if s["doc_id"] == semester_map["doc_id"]]
    assert map_sections and all(s["school"] == semester_map["school"] for s in map_sections)
    assert parsed["summary"]["schools_filled_from_program"] == 1
    assert parsed["summary"]["catalog_year"] == "2026-2027"


def test_courses_and_csv(parsed):
    courses = parsed["courses"]
    assert [c["code"] for c in courses] == ["CSCI 101", "CSCI 125", "CSCI 260", "CSCI 425", "CSCI 426",
                                            "CSCI 455", "CSCI 456"]
    assert all(c["catalog_year"] == "2026-2027" for c in courses)
    path = parsed["settings"].processed_dir / "courses.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")         # UTF-8 BOM for Excel
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == [f.name for f in fields(CourseRecord)]
    assert len(rows) == 7
    csci260 = next(r for r in rows if r["code"] == "CSCI 260")
    assert json.loads(csci260["attributes"])["Prerequisite Course(s)"] == "Prerequisite: CSCI 185."
    assert csci260["corequisites"] == "" and csci260["credits_min"] == "3.0"


def test_sections_belong_to_indexed_pages(parsed):
    by_id = {p["doc_id"]: p for p in parsed["pages"].values()}
    for s in parsed["sections"]:
        page = by_id[s["doc_id"]]
        assert not page["duplicate_of"] and not page["noindex"]
        assert s["site"] == page["site"] and s["text"].strip()
        assert s["section_id"].startswith(s["doc_id"] + "-s")
    assert len({s["section_id"] for s in parsed["sections"]}) == len(parsed["sections"])
    assert parsed["summary"]["sections"] == len(parsed["sections"])


def test_corpus_records(parsed):
    corpus = parsed["corpus"]
    ids = [r["id"] for r in corpus]
    assert len(ids) == len(set(ids)) == parsed["summary"]["corpus"]
    in_corpus = [s for s in parsed["sections"] if not s["duplicate_of"]]
    assert Counter(r["source"] for r in corpus) == {"section": len(in_corpus), "course": 7}
    assert all(" " not in i for i in ids)                     # usable in TREC run/qrels files
    for r in corpus:
        assert set(r) == {f.name for f in fields(CorpusRecord)}
        assert r["url"].startswith("https://") and ".nyit.edu" in r["url"]
        assert r["text"].strip() and r["title"].strip() and r["metadata"]["site"]
    course = next(r for r in corpus if r["id"] == "course-CSCI-260-UG")
    assert course["metadata"]["course_id"] == "CSCI 260|UG"
    assert course["url"] == "https://catalog.nyit.edu/courses?cal=UG&cc=CSCI-260&cs=CSCI"
    assert course["title"] == "CSCI 260: Data Structures"
    assert course["metadata"]["school"] == "College of Engineering and Computing Sciences"
    section = next(r for r in corpus if r["source"] == "section"
                   and r["metadata"]["heading_path"] == ["Admission Requirements"]
                   and r["url"].startswith(fixture_url("catalog_program.html")))
    assert section["title"] == "Computer Science, B.S. — Admission Requirements"
    assert section["text"].startswith("Computer Science, B.S.\nAdmission Requirements\n")
    assert section["url"].endswith("#admissions_csbs_ecebs")
    assert section["metadata"]["program"] == "Computer Science, B.S."
    news = next(r for r in corpus if r["url"] == NEWS_URL)
    assert news["metadata"]["published"] == "2018-09-24T15:50:00+00:00"


def test_parse_summary_per_site(parsed):
    summary = parsed["summary"]
    assert summary["sites"][:2] == ["catalog.nyit.edu", "www.nyit.edu"]
    www = summary["by_site"]["www.nyit.edu"]
    assert (www["pages"], www["distinct_pages"], www["duplicate_pages"]) == (5, 4, 1)
    assert summary["by_site"]["magazine.nyit.edu"]["noindex_pages"] == 1
    assert summary["corpus_by_source"] == {"section": summary["sections"], "course": 7}
    assert summary["course_listing_pages"] == 1 and summary["courses"] == 7
    assert summary["empty_course_listings"] == []


def test_outputs_are_identical_across_two_runs(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    build_fixture_crawl(settings)
    pipeline.run_parse(settings)
    out = settings.processed_dir
    before = {name: (out / name).read_bytes() for name in
              ("pages.jsonl", "sections.jsonl", "courses.jsonl", "courses.csv", "corpus.jsonl")}
    pipeline.run_parse(settings)
    for name, data in before.items():
        assert (out / name).read_bytes() == data, name


def test_documents_and_catalog_pdf_pages_join_the_corpus(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    build_fixture_crawl(settings, extras=False)
    doc = {"site": "www.nyit.edu", "pdf_url": "https://www.nyit.edu/files/handbook.pdf", "title": "Handbook",
           "found_on": "https://www.nyit.edu/students/", "page_count": 3, "toc_path": ["Rules"],
           "modified": "2025-01-15", "file": "www.nyit.edu/handbook.pdf", "word_count": 5}
    write_jsonl(settings.processed_dir / "documents.jsonl", [
        dict(doc, doc_id="doc-a-p0001", page=1, url=doc["pdf_url"] + "#page=1",
             text="Students must follow the rules of the campus."),
        dict(doc, doc_id="doc-a-p0002", page=2, url=doc["pdf_url"] + "#page=2", text=""),        # empty
        dict(doc, doc_id="doc-a-p0003", page=3, url=doc["pdf_url"] + "#page=3",
             text="Signature page for the student and the advisor."),
        dict(doc, doc_id="doc-b-p0001", page=1, pdf_url="https://www.nyit.edu/files/old.pdf",
             url="https://www.nyit.edu/files/old.pdf#page=1",
             text="Signature page for the student and the advisor.", modified="2020-01-01"),
        dict(doc, doc_id="doc-c-p0001", page=1, site="my.nyit.edu", pdf_url="https://my.nyit.edu/x.pdf",
             url="https://my.nyit.edu/x.pdf#page=1", text="Portal only text for signed in users."),
    ])
    write_jsonl(settings.processed_dir / "pdf_pages.jsonl", [
        {"doc_id": "pdf-2025-2026-undergraduate-p0001", "edition": "2025-2026", "catalog": "undergraduate",
         "page": 1, "page_count": 2, "url": "https://catalog.nyit.edu/files/catalog/archives/X.pdf#page=1",
         "toc_path": ["General Information"], "section": "General Information", "text": "Welcome.",
         "word_count": 1, "pdf_file": "X.pdf"},
        {"doc_id": "pdf-2025-2026-undergraduate-p0002", "edition": "2025-2026", "catalog": "undergraduate",
         "page": 2, "page_count": 2, "url": "https://catalog.nyit.edu/files/catalog/archives/X.pdf#page=2",
         "toc_path": [], "section": None, "text": "", "word_count": 0, "pdf_file": "X.pdf"},
    ])
    summary = pipeline.run_parse(settings, corpus_with_pdf=True)
    corpus = {r["id"]: r for r in read_jsonl(settings.processed_dir / "corpus.jsonl")}
    docs = {i: r for i, r in corpus.items() if r["source"] == "document"}
    assert set(docs) == {"doc-a-p0001", "doc-a-p0003"}       # empty, repeated and disabled-host pages left out
    first = docs["doc-a-p0001"]
    assert first["title"] == "Handbook (PDF, p. 1)"
    assert first["text"] == "Handbook\nRules\nStudents must follow the rules of the campus."
    assert first["metadata"]["found_on"] == ["https://www.nyit.edu/students/"]
    assert first["metadata"]["site"] == "www.nyit.edu"
    pdf = corpus["pdf-2025-2026-undergraduate-p0001"]
    assert pdf["source"] == "pdf_page" and pdf["title"] == "Undergraduate catalog 2025-2026, p. 1 — General Information"
    assert "pdf-2025-2026-undergraduate-p0002" not in corpus
    assert summary["document_pages_read"] == 5 and summary["documents_in_corpus"] == 2
    assert summary["document_pages_repeated"] == 1

    pipeline.run_parse(settings, corpus_with_pdf=False, corpus_with_docs=False)
    sources = {r["source"] for r in read_jsonl(settings.processed_dir / "corpus.jsonl")}
    assert sources == {"section", "course"}


def test_no_cached_pages_gives_an_error_summary(settings):
    settings.ensure_dirs()
    summary = pipeline.run_parse(settings)
    assert summary["error"] == "no cached pages" and summary["pages"] == 0


def test_one_broken_page_never_stops_the_run(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    build_fixture_crawl(settings, extras=False)
    from askbear_scraper import site_parser

    real = site_parser.parse_any

    def flaky(html, url, *args, **kwargs):
        if url == NEWS_URL:
            raise RuntimeError("parser exploded")
        return real(html, url, *args, **kwargs)

    monkeypatch.setattr(site_parser, "parse_any", flaky)
    summary = pipeline.run_parse(settings)
    assert summary["pages"] == 16
    assert [(f["url"], f["stage"], f["error"]) for f in summary["failures"]] == \
        [(NEWS_URL, "page", "RuntimeError: parser exploded")]
    assert summary["by_site"]["www.nyit.edu"]["failures"] == 1


def test_error_pages_served_with_http_200_are_skipped(settings):
    settings.ensure_dirs()
    not_found = ("<html><head><title>404 | Build Ahead | Blogs NYIT</title></head><body><main><h1>404</h1>"
                 "<p>Sorry, nothing here.</p></main></body></html>")
    empty_template = ("<html><head><title>Curriculum | New York Tech</title></head><body><main>"
                      "<div class='Main__content'><div class='Common'><p>TOTAL CREDITS:</p></div></div>"
                      "</main></body></html>")
    write_jsonl(settings.manifest_path, [
        cache_page(settings, "https://blogs.nyit.edu/build_ahead/missing", not_found),
        cache_page(settings, "https://catalog.nyit.edu/curriculum/no-such-program", empty_template),
        cache_page(settings, NEWS_URL, fixture_html("www_news.html")),
    ])
    summary = pipeline.run_parse(settings)
    assert sorted(summary["skipped"]["not_found_page"]) == [
        "https://blogs.nyit.edu/build_ahead/missing", "https://catalog.nyit.edu/curriculum/no-such-program"]
    assert summary["pages"] == 1


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
def page_record(url, text="same text " * 10, site=None, **kw) -> PageRecord:
    from askbear_scraper.urls import url_host
    from askbear_scraper.utils import sha1, short_hash

    defaults = dict(doc_id="page-" + short_hash(url), url=url, title="T", page_title="T", breadcrumbs=[],
                    catalog_year="", page_type="page", area=None, school=None, program=None, degree=None,
                    level=None, campuses=[], headings=[], text=text, word_count=len(text.split()),
                    num_tables=0, links=[], content_sha1=sha1(text), fetched_at="", site=site or url_host(url))
    defaults.update(kw)
    return PageRecord(**defaults)


def test_mark_duplicates_prefers_the_catalog_then_indexable_then_simple_urls():
    catalog = page_record("https://catalog.nyit.edu/tuition/fees")
    www = page_record("https://www.nyit.edu/tuition/fees/")
    hidden = page_record("https://catalog.nyit.edu/aaa_hidden", noindex=True)
    query = page_record("https://catalog.nyit.edu/courses?cal=GR&cs=PHAS&x=1")
    short = page_record("https://www.nyit.edu/a/", text="Coming soon")
    short_copy = page_record("https://www.nyit.edu/b/", text="Coming soon")
    pages = [www, hidden, query, catalog, short, short_copy]
    assert pipeline.mark_duplicates(pages) == 3
    assert catalog.duplicate_of is None
    assert {p.duplicate_of for p in (www, hidden, query)} == {catalog.url}
    assert short.duplicate_of is None and short_copy.duplicate_of is None     # too short to compare


def test_select_entries_prefers_the_direct_url_and_skips_out_of_scope(settings):
    settings.ensure_dirs()
    html = fixture_html("www_news.html")
    write_jsonl(settings.manifest_path, [
        cache_page(settings, "https://nyit.edu/news/x", html, final_url=NEWS_URL),
        cache_page(settings, NEWS_URL, html),
        cache_page(settings, "https://www.nyit.edu/wp-admin/x/", html),
        cache_page(settings, "https://catalog.nyit.edu/go", html, final_url="https://my.nyit.edu/"),
        testkit.manifest_entry("https://www.nyit.edu/err/", None, status="error"),
    ])
    chosen, skipped = pipeline.select_entries(settings)
    assert [(url, e["url"]) for url, e in chosen] == [(NEWS_URL, NEWS_URL)]
    assert skipped["same_page_as_other_url"] == ["https://nyit.edu/news/x"]
    assert skipped["out_of_scope"] == ["https://www.nyit.edu/wp-admin/x/"]
    assert skipped["offsite_redirect"] == ["https://catalog.nyit.edu/go"]


def course(code, title="T", subject_name="Computer Science", description="d", url=None, level_code="UG"):
    subject, number = code.split(" ")
    return CourseRecord(f"{code}|{level_code}", code, subject, subject_name, number, title, "3.0", 3.0, 3.0,
                        "School of Management", "undergraduate", level_code, description, None, None, None, {},
                        f"{code}: {title}", url or f"https://catalog.nyit.edu/courses?cal={level_code}&cs={subject}")


def test_dedupe_and_sort_courses():
    a = course("CSCI 110", description="short", url="https://catalog.nyit.edu/courses?cal=UG&cs=MATH")
    b = course("CSCI 110", description="a much longer description")
    found = pipeline.dedupe_courses([course("CSCI 110L"), a, course("CSCI 90"), b,
                                     course("CSCI 110", level_code="GR")])
    assert [c.course_id for c in found] == ["CSCI 90|UG", "CSCI 110|UG", "CSCI 110|GR", "CSCI 110L|UG"]
    assert found[1].description == "a much longer description"


def test_build_corpus_leaves_out_listing_sections_legacy_codes_and_id_clashes():
    sec = SectionRecord("page-x-s000", "page-x", "https://catalog.nyit.edu/x", "X", "", [], "text", 1, 0,
                        page_type="policy", site="catalog.nyit.edu")
    listing = SectionRecord("page-y-s000", "page-y", "https://catalog.nyit.edu/courses?cal=UG&cs=A", "Y", "",
                            [], "text", 1, 0, page_type="course_listing")
    empty = SectionRecord("page-z-s000", "page-z", "https://catalog.nyit.edu/z", "Z", "", [], "  ", 0, 0)
    legacy = course("OM 9600", subject_name="Conversion - OM", description="")
    records, clashes = pipeline.build_corpus([sec, listing, empty, sec], [course("CSCI 260"), legacy])
    assert [r.id for r in records] == ["page-x-s000", "course-CSCI-260-UG"]
    assert clashes == 1
    assert pipeline.course_in_corpus(legacy) is False
    assert pipeline.course_in_corpus({"subject_name": "Conversion - OM", "description": "Real course"}) is True


def test_section_to_corpus_titles():
    def sec(heading, page_title="Tuition"):
        return SectionRecord("p-s1", "p", "https://catalog.nyit.edu/t#a", page_title, heading, [heading] if heading else [],
                             "body", 1, 1, site="catalog.nyit.edu")
    assert pipeline.section_to_corpus(sec("Fees")).title == "Tuition — Fees"
    assert pipeline.section_to_corpus(sec("")).title == "Tuition"
    assert pipeline.section_to_corpus(sec("(part 2)")).title == "Tuition (part 2)"
    assert pipeline.section_to_corpus(sec("tuition")).title == "Tuition"
    assert pipeline.section_to_corpus(sec("Fees", page_title="")).title == "Fees"


def test_drop_repeated_sections_keeps_the_copy_on_the_most_specific_page():
    policy = page_record("https://apps.nyit.edu/policies/conduct", text="x " * 100, page_type="policy")
    handbook = page_record("https://apps.nyit.edu/policies/collection/handbook", text="x " * 5000,
                           page_type="policy")
    catalog = page_record("https://catalog.nyit.edu/policies_and_rules/a", text="y " * 50)
    catalog2 = page_record("https://catalog.nyit.edu/policies_and_rules/b", text="z " * 50)

    def sec(page, i):
        return SectionRecord(f"{page.doc_id}-s{i:03d}", page.doc_id, page.url, page.title, "Code of Conduct",
                             ["Code of Conduct"], "Students must behave.", 3, i, site=page.site)
    sections = [sec(handbook, 0), sec(policy, 0), sec(catalog, 0), sec(catalog2, 0)]
    kept, removed = pipeline.drop_repeated_sections(sections, [policy, handbook, catalog, catalog2])
    assert [s.doc_id for s in kept] == [policy.doc_id, catalog.doc_id, catalog2.doc_id]   # catalog: never merged
    assert removed == {"apps.nyit.edu": 1}


def test_repeated_sections_stay_on_the_page_they_are_about():
    lib = "https://libguides.nyit.edu/"
    apps = page_record(lib + "mobileapps", text="m " * 300, title="Mobile Medical Apps: Home")
    ot = page_record(lib + "c.php?g=1&p=2", text="o " * 80, title="Occupational Therapy: Mobile Apps")
    other = page_record(lib + "c.php?g=3&p=4", text="c " * 50, title="Architecture & Design: Codes & Standards")
    www = "https://www.nyit.edu/faculty-and-staff/accomplishments/"
    first = page_record(www, text="a " * 800, title="Faculty & Staff Accomplishments")
    later = page_record(www + "?fsa_page=106", text="a " * 500, title="Faculty & Staff Accomplishments (page 106)")

    def sec(page, heading, text):
        return SectionRecord(f"{page.doc_id}-s000", page.doc_id, page.url, page.title, heading,
                             [heading] if heading else [], text, len(text.split()), 0, site=page.site)
    box = "Apps for drug references, anatomy and clinical calculators, reviewed by the library."
    intro = "Share your accomplishment with the university community. " * 4
    sections = [sec(other, "Mobile Medical Apps", box), sec(ot, "Mobile Medical Apps", box),
                sec(apps, "Mobile Medical Apps", box), sec(later, "", intro), sec(first, "", intro)]
    kept, removed = pipeline.drop_repeated_sections(sections, [apps, ot, other, first, later])
    # the guide the box is about, not the shortest guide that reuses it; page 1, not page 106
    assert [s.doc_id for s in kept] == [apps.doc_id, first.doc_id]
    assert removed == {"www.nyit.edu": 1, "libguides.nyit.edu": 2}
    assert pipeline._title_match("Mobile Medical Apps", "Occupational Therapy: Mobile Apps") == 2 / 3


def test_listing_excerpts_of_linked_posts_go():
    blog = "https://blogs.nyit.edu/dining"
    post_text = ("Do not spoil your produce: keep apples away from bananas, store herbs like flowers "
                 "in a glass of water and wash berries only right before you eat them. ") * 3
    listing = page_record(blog, text="x " * 120, page_type="listing",
                          links=[{"text": "Don't spoil your produce", "url": blog + "/dont_spoil_your_produce"}])
    post = page_record(blog + "/dont_spoil_your_produce", text=post_text, page_type="blog_post")
    other = page_record(blog + "/other", text=post_text, page_type="blog_post")     # not linked

    def sec(page, i, text):
        return SectionRecord(f"{page.doc_id}-s{i:03d}", page.doc_id, page.url, page.title, "", [],
                             text, len(text.split()), i, site=page.site)
    excerpt = sec(listing, 1, post_text.split(". ")[0] + " and wash berries only right before you eat them.")
    intro = sec(listing, 0, "Campus Dining news, recipes and interviews with our chefs and nutrition team.")
    sections = [intro, excerpt, sec(post, 0, post_text)]
    kept, removed = pipeline.drop_repeated_sections(sections, [listing, post])
    assert [s.section_id for s in kept] == [intro.section_id, sections[2].section_id]
    assert removed == {"blogs.nyit.edu": 1}
    # an excerpt of a post the list does not link to stays (it may be the list's own text)
    kept, _ = pipeline.drop_repeated_sections([intro, excerpt, sec(other, 0, post_text)], [listing, other])
    assert excerpt in kept


def test_repeated_document_pages_keep_the_newest_copy():
    docs = [{"doc_id": "a", "text": "Same  page", "pdf_url": "u1", "page": 1, "modified": "2020"},
            {"doc_id": "b", "text": "same page", "pdf_url": "u2", "page": 1, "modified": "2024"},
            {"doc_id": "c", "text": "unique", "pdf_url": "u3", "page": 1},
            {"doc_id": "d", "text": "", "pdf_url": "u4", "page": 1}]
    assert pipeline.repeated_document_pages(docs) == {"a"}


def test_courses_csv_reports_a_file_locked_by_excel(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError("The process cannot access the file because it is being used")
    monkeypatch.setattr(pipeline.os, "replace", locked)
    path = tmp_path / "courses.csv"
    assert pipeline.write_courses_csv(path, [course("CSCI 260")]) is False
    assert not path.exists() and not (tmp_path / "courses.csv.tmp").exists()


def test_site_helpers():
    assert sorted(["b.nyit.edu", "www.nyit.edu", "a.nyit.edu", "catalog.nyit.edu"], key=pipeline.site_rank) == \
        ["catalog.nyit.edu", "www.nyit.edu", "a.nyit.edu", "b.nyit.edu"]
    assert pipeline.is_catalog_site("catalog.nyit.edu") and not pipeline.is_catalog_site("www.nyit.edu")
    assert pipeline.uses_boilerplate("www.nyit.edu") and not pipeline.uses_boilerplate("catalog.nyit.edu")
    assert pipeline.is_parsed_site("comapp.nyit.edu") and not pipeline.is_parsed_site("catalog-dev.nyit.edu")
    assert not pipeline.is_parsed_site("example.com")
    assert pipeline.looks_like_not_found("404", "", 3)
    assert pipeline.looks_like_not_found("Missing", "Page not found | Blogs NYIT", 10)
    assert not pipeline.looks_like_not_found("404", "", 500)
    assert pipeline.document_title({"pdf_url": "https://www.nyit.edu/files/Campus_Map-2025.pdf"}) == "Campus Map 2025"
