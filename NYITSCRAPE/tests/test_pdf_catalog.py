"""pdf_catalog.py: the full catalog PDFs (small PDFs are generated with PyMuPDF)."""

from __future__ import annotations

import json

import pytest

from askbear_scraper import config
from askbear_scraper import pdf_catalog as pc

from testkit import FakeWeb, make_fetcher, make_pdf, pdf_bytes, read_jsonl

UG_URL = config.pdf_url("undergraduate", "2025-2026")

PAGES = [
    ["General Information", "Welcome to the catalog. The office of the registrar is here."],
    ["Academic Calendar", "Fall term starts in September.", "Classes are full-", "time for most students."],
    ["Admissions", "First year students apply online."],
    [],                                                       # an empty page
    ["Transfer students", "Credits transfer after review."],
]
TOC = [[1, "New York Institute of Technology Academic Catalogs 2025–2026", 1],
       [2, "Undergraduate Catalog: Table of Contents", 1],
       [3, "General Information", 1], [4, "Academic Calendar", 2],
       [3, "Admissions", 3], [4, "Transfer", 5]]


@pytest.fixture
def catalog_pdf(settings):
    """A 5-page "2025-2026 undergraduate catalog" with bookmarks, a running header and
    page numbers, saved where the pdf step expects it."""
    return make_pdf(pc.pdf_path(settings, "undergraduate", "2025-2026"), PAGES, toc=TOC,
                    metadata={"title": "Undergraduate Catalog 2025-2026"},
                    header="New York Tech Undergraduate Catalog 2025-2026", footer_numbers=True)


# ---------------------------------------------------------------------------
# bookmarks -> section path of every page
# ---------------------------------------------------------------------------
def test_toc_paths_drop_the_wrapper_bookmarks():
    assert pc.toc_paths(TOC, 6) == [
        ["General Information"],
        ["General Information", "Academic Calendar"],
        ["Admissions"],
        ["Admissions"],
        ["Admissions", "Transfer"],
        ["Admissions", "Transfer"],
    ]


def test_toc_paths_edge_cases():
    assert pc.toc_paths([], 2) == [[], []]
    assert pc.toc_paths([[1, "Only", 2]], 3) == [[], ["Only"], ["Only"]]
    assert pc.toc_paths([[1, "Out of range", 0], [1, "Too far", 9], ["bad"], [1, None, 1]], 2) == [[], []]
    # a document covered only by its wrapper keeps the innermost wrapper
    assert pc.toc_paths([[1, "Root", 1], [2, "Contents", 1]], 1) == [["Contents"]]
    # titles are cleaned
    assert pc.toc_paths([[1, "  Tuition and   Fees ", 1], [1, "Other", 2]], 2)[0] == ["Tuition and Fees"]


# ---------------------------------------------------------------------------
# text extraction
# ---------------------------------------------------------------------------
def test_extract_pdf_records(catalog_pdf):
    records = pc.extract_pdf(catalog_pdf, "undergraduate", "2025-2026", UG_URL)
    assert [r.page for r in records] == [1, 2, 3, 4, 5]
    first = records[0]
    assert first.doc_id == "pdf-2025-2026-undergraduate-p0001"
    assert (first.edition, first.catalog, first.page_count) == ("2025-2026", "undergraduate", 5)
    assert first.url == UG_URL + "#page=1"
    assert first.pdf_file == "NYITUndergraduateCatalog_AY2526.pdf"
    assert [r.toc_path for r in records] == [["General Information"], ["General Information", "Academic Calendar"],
                                             ["Admissions"], ["Admissions"], ["Admissions", "Transfer"]]
    assert [r.section for r in records] == ["General Information", "Academic Calendar", "Admissions",
                                            "Admissions", "Transfer"]
    assert first.text == "General Information\nWelcome to the catalog. The office of the registrar is here."
    assert first.word_count == pc.word_count_clean(first.text) == 13


def test_running_headers_and_page_numbers_are_removed(catalog_pdf):
    records = pc.extract_pdf(catalog_pdf, "undergraduate", "2025-2026", UG_URL)
    for r in records:
        assert "Undergraduate Catalog 2025-2026" not in r.text
        assert not any(line.strip().isdigit() for line in r.text.splitlines())


def test_wrapped_line_with_a_hyphen_keeps_the_hyphen(catalog_pdf):
    text = pc.extract_pdf(catalog_pdf, "undergraduate", "2025-2026", UG_URL)[1].text
    assert "full-time" in text and "fulltime" not in text and "full- time" not in text


def test_empty_pages_are_kept_with_zero_words(catalog_pdf):
    empty = pc.extract_pdf(catalog_pdf, "undergraduate", "2025-2026", UG_URL)[3]
    assert (empty.page, empty.text, empty.word_count) == (4, "", 0)
    assert empty.toc_path == ["Admissions"]


def test_pdf_without_bookmarks_gets_its_outline_from_printed_headings(settings):
    path = make_pdf(settings.data_dir / "plain.pdf", [["Big Title", "Body text one."], ["Plain page."]])
    records = pc.extract_pdf(path, "graduate", "2026-2027", config.pdf_url("graduate", "2026-2027"))
    assert len(records) == 2 and records[0].text.startswith("Big Title")
    assert all(isinstance(r.toc_path, list) for r in records)


def test_heading_toc_from_bold_labels_and_titles():
    Line = pc._Line
    body = [Line("Body text " + "x" * 40, (72, 300 + i * 14, 500, 312 + i * 14), 10.0, False) for i in range(8)]
    pages = []
    for i, title in enumerate(["Computer Science, B.S.", "Data Science, M.S.", "Physics, B.S."]):
        lines = [Line("General Information", (400, 40, 560, 50), 8.0, True),          # small bold label, right
                 Line(title, (72, 80, 400, 104), 20.0, True)] + body                    # large bold title
        pages.append((lines, 612.0))
    assert pc.heading_toc(pages) == [
        [1, "General Information", 1], [2, "Computer Science, B.S.", 1],
        [1, "General Information", 2], [2, "Data Science, M.S.", 2],
        [1, "General Information", 3], [2, "Physics, B.S.", 3]]
    assert pc.heading_toc([]) == []


def test_text_helpers():
    assert pc._join_wrapped("full-", "time") == "full-time"
    assert pc._join_wrapped("infor­", "mation") == "information"
    assert pc._join_wrapped("Income-", "Based") == "Income-Based"
    assert pc._join_wrapped("word", "next") == "word next"
    assert pc._join_wrapped("3-", "4") == "3- 4"
    assert pc.word_count_clean("FCWR 101 | Writing I | 3") == 5
    assert pc.word_count_clean("") == 0


def test_rows_to_text_joins_cells_and_drops_web_chrome():
    rows = []
    for cells, y in ((["FCWR 101", "Writing I", "3"], 100), (["Back to Top"], 120),
                     (["More About Admissions >"], 140), (["The ofﬁce is open"], 160)):
        row = pc._Row(pc._Cell(cells[0], (72, y, 150, y + 10)), 10.0)
        for i, cell in enumerate(cells[1:], 1):
            row.cells.append(pc._Cell(cell, (72 + 150 * i, y, 200 + 150 * i, y + 10)))
        rows.append(row)
    assert pc.rows_to_text(rows) == "FCWR 101 | Writing I | 3\nMore About Admissions\nThe office is open"


def test_is_valid_pdf(tmp_path):
    good, html, empty = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    good.write_bytes(b"%PDF-1.7 ...")
    html.write_bytes(b"<!DOCTYPE html><title>Page not found</title>")
    empty.write_bytes(b"")
    assert pc.is_valid_pdf(good)
    assert not pc.is_valid_pdf(html) and not pc.is_valid_pdf(empty)
    assert not pc.is_valid_pdf(tmp_path / "missing.pdf")


def test_paths_and_local_pdfs(settings, catalog_pdf):
    assert pc.pdf_path(settings, "graduate", "2025-2026").name == "NYITGraduateCatalog_AY2526.pdf"
    (settings.pdf_dir / "NYITGraduateCatalog_AY2526.pdf").write_text("<html>not a pdf</html>", encoding="utf-8")
    (settings.pdf_dir / "something_else.pdf").write_bytes(catalog_pdf.read_bytes())
    assert pc.local_catalog_pdfs(settings) == [("2025-2026", "undergraduate", catalog_pdf)]
    with pytest.raises(ValueError):
        config.pdf_url("law", "2025-2026")
    with pytest.raises(ValueError):
        config.pdf_url("graduate", "1999-2000")


def test_file_sha1(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    assert pc.file_sha1(path) == "a9993e364706816aba3e25717850c26c9cd0d89d"


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------
def test_download_saves_only_real_pdfs_and_skips_cached_files(settings, web, clock, tmp_path):
    good = pdf_bytes(tmp_path, pages=["Graduate catalog page."])
    web.add(config.pdf_url("graduate", "2025-2026"), good, content_type="application/pdf")
    web.add(config.pdf_url("graduate", "2024-2025"), "<html>Page not found</html>")        # HTTP 200
    web.add(config.pdf_url("graduate", "2023-2024"), "gone", status=404)
    fetcher = make_fetcher(settings, web, clock)
    results = pc.download_catalog_pdfs(settings, ["2025-2026", "2024-2025", "2023-2024"], ["graduate"],
                                       fetcher=fetcher)
    by_edition = {r["edition"]: r for r in results}
    assert by_edition["2025-2026"]["status"] == "downloaded"
    assert by_edition["2025-2026"]["bytes"] == len(good)
    assert by_edition["2024-2025"]["status"] == "failed" and "not a PDF" in by_edition["2024-2025"]["error"]
    assert by_edition["2023-2024"]["status"] == "failed" and by_edition["2023-2024"]["error"] == "HTTP 404"
    assert pc.pdf_path(settings, "graduate", "2025-2026").read_bytes() == good
    assert not pc.pdf_path(settings, "graduate", "2024-2025").exists()
    assert not list(settings.pdf_dir.glob("*.download*"))
    web.requests.clear()
    again = pc.download_catalog_pdfs(settings, ["2025-2026"], ["graduate"], fetcher=fetcher)
    assert again[0]["status"] == "cached" and web.requests == []


def test_download_respects_robots_txt(settings, web, clock):
    web.robots("catalog.nyit.edu", "User-agent: *\nDisallow: /files/\n")
    results = pc.download_catalog_pdfs(settings, ["2025-2026"], ["graduate"],
                                       fetcher=make_fetcher(settings, web, clock))
    assert results[0]["status"] == "failed" and "robots" in results[0]["error"]
    assert web.page_requests() == []


# ---------------------------------------------------------------------------
# the `pdf` command
# ---------------------------------------------------------------------------
def test_run_pdf_writes_pages_and_summary(settings, catalog_pdf):
    summary = pc.run_pdf(settings, editions=["2025-2026", "2025-2026"], kinds=["undergraduate"])
    assert summary["failed"] == []
    assert summary["requested"] == {"editions": ["2025-2026"], "kinds": ["undergraduate"]}
    assert summary["downloads"][0]["status"] == "cached"            # no network needed
    assert summary["records"] == 5
    info = summary["pdfs"][0]
    assert (info["pages"], info["empty_pages"], info["empty_page_numbers"]) == (5, 1, [4])
    assert info["toc_entries"] == 6 and info["toc_source"] == "bookmarks"
    assert info["pdf_title"] == "Undergraduate Catalog 2025-2026"
    assert info["header_footer_lines_removed"] >= 9
    assert info["words_raw"] > info["words_clean"] > 0
    assert summary["totals"]["pages"] == 5 and summary["by_edition"]["2025-2026"]["pdfs"] == 1
    records = read_jsonl(settings.processed_dir / "pdf_pages.jsonl")
    assert [r["doc_id"] for r in records] == [f"pdf-2025-2026-undergraduate-p{i:04d}" for i in range(1, 6)]
    saved = json.loads((settings.processed_dir / "pdf_summary.json").read_text(encoding="utf-8"))
    assert saved["records"] == 5 and saved["totals"] == summary["totals"]


def test_run_pdf_covers_every_pdf_on_disk_and_reports_broken_ones(settings, catalog_pdf):
    make_pdf(pc.pdf_path(settings, "graduate", "2026-2027"), [["Graduate page one."], ["Two."]])
    pc.pdf_path(settings, "osteopathic", "2025-2026").write_bytes(b"%PDF-1.4 this is not really a pdf")
    summary = pc.run_pdf(settings, editions=["2025-2026"], kinds=["undergraduate"])
    records = read_jsonl(settings.processed_dir / "pdf_pages.jsonl")
    assert [(r["edition"], r["catalog"]) for r in records] == \
        [("2025-2026", "undergraduate")] * 5 + [("2026-2027", "graduate")] * 2     # sorted
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["catalog"] == "osteopathic"
    assert summary["failed"][0]["error"].startswith("extraction failed")


def test_run_pdf_reports_failed_downloads(settings, monkeypatch):
    monkeypatch.setattr(pc, "PoliteFetcher", lambda s: make_fetcher(s, FakeWeb()))
    summary = pc.run_pdf(settings, editions=["2026-2027"], kinds=["graduate"])
    assert summary["records"] == 0
    assert summary["failed"] == [{"edition": "2026-2027", "catalog": "graduate",
                                  "url": config.pdf_url("graduate", "2026-2027"), "error": "HTTP 404"}]
