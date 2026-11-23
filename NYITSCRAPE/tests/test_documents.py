"""documents.py: PDF documents linked from NYIT pages (the `docs` step), against FakeWeb."""

from __future__ import annotations

import json

import pytest

from askbear_scraper import documents as dc

from testkit import FakeWeb, Reply, make_fetcher, make_pdf, read_jsonl, write_jsonl

WWW = "https://www.nyit.edu"
HANDBOOK = WWW + "/files/handbook.pdf"
HANDBOOK_COPY = "https://site.nyit.edu/files/Student_Handbook.pdf"
FORM = "https://files.nyit.edu/files/Registration_Form.pdf"


@pytest.fixture
def pdfs(tmp_path):
    src = tmp_path / "pdf-src"
    handbook = make_pdf(src / "handbook.pdf",
                        [["Student Handbook", "Welcome to New York Tech."], ["Chapter two about policies."], []],
                        toc=[[1, "Welcome", 1], [1, "Policies", 2]],
                        metadata={"title": "Student Handbook 2026", "modDate": "D:20250115093000-05'00'"})
    form = make_pdf(src / "form.pdf", [["A form to fill in.", "Name ________ Date ______"]],
                    metadata={"title": "Microsoft Word - form.docx"})
    return {"handbook": handbook.read_bytes(), "form": form.read_bytes()}


def link(url, anchor="", found_on="https://www.nyit.edu/students/", kind="pdf"):
    return {"url": url, "kind": kind, "anchor_text": anchor, "found_on": found_on}


@pytest.fixture
def docs_site(settings, pdfs):
    """external_links.jsonl as the crawler writes it + the web serving those PDFs."""
    web = FakeWeb()
    web.add(HANDBOOK, pdfs["handbook"], content_type="application/pdf")
    web.add(HANDBOOK_COPY, pdfs["handbook"], content_type="application/pdf")           # same file
    web.add(FORM, pdfs["form"], content_type="application/pdf",
            **{"Last-Modified": "Wed, 15 May 2024 14:03:22 GMT"})
    web.add(WWW + "/files/missing.pdf", "nope", status=404)
    web.add(WWW + "/files/login.pdf", "<!DOCTYPE html><html><title>Sign in</title></html>")
    web.add(WWW + "/files/huge.pdf", pdfs["handbook"], content_type="application/pdf",
            **{"Content-Length": "50000000"})
    web.add(WWW + "/files/secret.pdf", pdfs["form"], content_type="application/pdf", **{"X-Robots-Tag": "noindex"})
    web.add(WWW + "/files/busy.pdf", "busy", status=500)
    settings.ensure_dirs()
    settings.max_retries = 0
    write_jsonl(settings.external_links_path, [
        link(HANDBOOK, "Student Handbook (PDF)", "https://www.nyit.edu/students/"),
        link(HANDBOOK_COPY, "here", ["https://www.nyit.edu/a/", "https://www.nyit.edu/b/"]),
        link(FORM, "Registration Form", ["https://www.nyit.edu/registrar/"]),
        link(WWW + "/files/missing.pdf", "Missing"),
        link(WWW + "/files/login.pdf", "Login"),
        link(WWW + "/files/huge.pdf", "Huge"),
        link(WWW + "/files/secret.pdf", "Secret"),
        link(WWW + "/files/busy.pdf", "Busy"),
        link("https://hesc.ny.gov/tap.pdf", "TAP application"),
        link("https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf", "UG"),
        link("https://my.nyit.edu/private.pdf", "portal"),
        link(WWW + "/about/", "not a PDF", kind="external"),
        link("mailto:x@nyit.edu.pdf", "broken"),
    ])
    return web


def run(settings, web, **kwargs):
    return dc.run_docs(settings, fetcher=make_fetcher(settings, web), **kwargs)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------
def test_run_docs_downloads_dedupes_and_extracts(settings, docs_site):
    summary = run(settings, docs_site)
    assert "error" not in summary
    assert summary["skipped"] == {"catalog_pdf": 1, "invalid_url": 1, "noindex": 1, "other_website": 1,
                                  "site_not_crawled": 1}
    assert summary["status_counts"] == {"ok": 4, "http_error": 1, "not_found": 1, "not_pdf": 1, "too_large": 1}
    assert summary["downloads"]["downloaded"] == 4 and summary["downloads"]["failed"] == 4
    assert summary["totals"]["pdfs"] == 2                   # the handbook copy is the same file
    assert summary["totals"]["duplicate_links"] == 1
    assert summary["totals"]["pages"] == 4 and summary["totals"]["empty_pages"] == 1
    assert summary["records"] == 4

    docs = {d["sha1"]: d for d in summary["documents"]}
    assert len(docs) == 2
    handbook = next(d for d in summary["documents"] if d["pages"] == 3)
    assert handbook["title"] == "Student Handbook 2026" and handbook["title_source"] == "metadata"
    assert handbook["modified"] == "2025-01-15T09:30:00-05:00"
    assert len(handbook["aliases"]) == 1 and handbook["links"] == 2
    assert set(handbook["found_on"]) == {"https://www.nyit.edu/students/", "https://www.nyit.edu/a/",
                                         "https://www.nyit.edu/b/"}
    form = next(d for d in summary["documents"] if d["pdf_url"] == FORM)
    assert form["title"] == "Registration Form" and form["title_source"] == "link_text"
    assert form["modified"] == "2024-05-15T14:03:22+00:00"   # from the HTTP Last-Modified header

    records = read_jsonl(settings.processed_dir / "documents.jsonl")
    assert [(r["site"], r["page"]) for r in records] == \
        sorted((r["site"], r["page"]) for r in records)       # sorted by site, pdf_url, page
    form_page = next(r for r in records if r["pdf_url"] == FORM)
    assert form_page["doc_id"].startswith("doc-") and form_page["doc_id"].endswith("-p0001")
    assert form_page["url"] == FORM + "#page=1"
    assert form_page["text"] == "A form to fill in.\nName ___ Date ___"
    assert form_page["found_on"] == ["https://www.nyit.edu/registrar/"]
    hb_pages = [r for r in records if r["page_count"] == 3]
    assert [r["toc_path"] for r in hb_pages] == [["Welcome"], ["Policies"], ["Policies"]]
    assert hb_pages[2]["word_count"] == 0 and hb_pages[2]["text"] == ""
    for r in records:
        assert (settings.docs_dir / r["file"]).is_file()

    failures = {f["url"]: f["status"] for f in summary["failures"]}
    assert failures == {WWW + "/files/busy.pdf": "http_error", WWW + "/files/huge.pdf": "too_large",
                        WWW + "/files/login.pdf": "not_pdf", WWW + "/files/missing.pdf": "not_found"}
    saved = json.loads((settings.processed_dir / "documents_summary.json").read_text(encoding="utf-8"))
    assert saved["records"] == 4 and saved["totals"] == summary["totals"]


def test_docs_manifest_and_files_on_disk(settings, docs_site):
    run(settings, docs_site)
    manifest = {e["url"]: e for e in read_jsonl(settings.docs_manifest_path)}
    assert list(manifest) == sorted(manifest)                 # one line per URL, sorted
    ok = manifest[HANDBOOK]
    assert ok["status"] == "ok" and ok["file"] == dc.doc_filename(HANDBOOK) and ok["sha1"]
    assert (settings.docs_dir / ok["file"]).read_bytes().startswith(b"%PDF-")
    assert manifest[WWW + "/files/huge.pdf"]["file"] is None
    assert manifest[WWW + "/files/huge.pdf"]["bytes"] == 50_000_000
    assert manifest[WWW + "/files/missing.pdf"]["http_status"] == 404
    leftovers = [p for p in settings.docs_dir.rglob("*") if p.suffix in (".download", ".part")]
    assert leftovers == []
    assert not (settings.docs_dir / dc.doc_filename(WWW + "/files/login.pdf")).exists()


def test_rerun_only_fetches_what_is_missing(settings, docs_site):
    run(settings, docs_site)
    docs_site.requests.clear()
    summary = run(settings, docs_site)
    # 404, not-a-PDF and too-large files are not asked for again; a server error is
    assert docs_site.page_requests() == [WWW + "/files/busy.pdf"]
    assert summary["downloads"]["cached"] == 4
    assert summary["records"] == 4


def test_raising_the_size_limit_retries_too_large_files(settings, docs_site):
    run(settings, docs_site)
    docs_site.requests.clear()
    summary = run(settings, docs_site, max_mb=0)                # 0 = no limit
    assert WWW + "/files/huge.pdf" in docs_site.page_requests()
    assert summary["status_counts"]["ok"] == 5


def test_stream_without_content_length_stops_at_the_limit(settings, pdfs):
    web = FakeWeb()
    big = pdfs["handbook"] + b"%" + b"0" * 20_000
    web.add(WWW + "/files/big.pdf", big, content_type="application/pdf")
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [link(WWW + "/files/big.pdf", "Big")])
    summary = run(settings, web, max_mb=0.005)                  # 5,000 bytes
    entry = read_jsonl(settings.docs_manifest_path)[0]
    assert entry["status"] == "too_large" and entry["file"] is None
    assert "download stopped" in entry["error"]
    assert summary["records"] == 0
    assert not any(settings.docs_dir.rglob("*.pdf"))


def test_max_docs_postpones_the_rest(settings, docs_site):
    summary = run(settings, docs_site, max_docs=2)
    assert summary["downloads"]["attempted"] == 2
    assert summary["downloads"]["postponed"] == 6
    assert summary["skipped"]["max_docs"] == 6
    assert len(docs_site.page_requests()) == 2


def test_found_on_as_string_or_list_and_sitemap_pdfs(settings):
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [
        link(HANDBOOK, "Handbook", "https://www.nyit.edu/one/"),
        link(HANDBOOK, "", ["https://www.nyit.edu/two/", "https://www.nyit.edu/one/"]),
    ])
    sitemap = settings.raw_dir / "sitemaps" / "www.nyit.edu.json"
    sitemap.parent.mkdir(parents=True)
    sitemap.write_text(json.dumps({"host": "www.nyit.edu", "pages": [
        [WWW + "/files/listed.pdf", None], [WWW + "/about/", None], "not-a-pair"]}), encoding="utf-8")
    found = dc.find_candidates(settings)
    assert found.links_read == 2 and found.sitemap_urls == 1
    assert found.items[HANDBOOK]["found_on"] == ["https://www.nyit.edu/one/", "https://www.nyit.edu/two/"]
    assert found.items[HANDBOOK]["anchor_text"] == "Handbook"
    assert found.items[WWW + "/files/listed.pdf"]["source"] == "sitemap"


def test_without_crawl_output_the_step_reports_an_error(settings):
    summary = dc.run_docs(settings, fetcher=make_fetcher(settings, FakeWeb()))
    assert "error" in summary and "crawl" in summary["error"]


def test_unreadable_pdf_is_reported_not_fatal(settings, pdfs):
    web = FakeWeb()
    web.add(WWW + "/files/broken.pdf", b"%PDF-1.4\n this file is cut off", content_type="application/pdf")
    web.add(FORM, pdfs["form"], content_type="application/pdf")
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [link(WWW + "/files/broken.pdf", "Broken"), link(FORM, "Form")])
    summary = run(settings, web)
    assert summary["totals"]["pdfs"] == 1
    assert [f["status"] for f in summary["failures"]] == ["extract_error"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url, reason", [
    (WWW + "/files/admissions/ADM_Agencies.pdf", None),
    ("https://catalog.nyit.edu/files/other.pdf", None),
    ("https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf", "catalog_pdf"),
    ("https://my.nyit.edu/x.pdf", "site_not_crawled"),
    ("https://catalog-dev.nyit.edu/files/x.pdf", "site_not_crawled"),
    ("https://ebsco.arktos.nyit.edu/x.pdf", "site_not_crawled"),
    ("https://hesc.ny.gov/form.pdf", "other_website"),
])
def test_skip_reason(url, reason):
    assert dc.skip_reason(url) == reason


def test_doc_filename_is_safe_and_unique():
    name = dc.doc_filename("https://files.nyit.edu/files/admissions/ADM_Agencies.pdf")
    assert name.startswith("files.nyit.edu/adm_agencies__") and name.endswith(".pdf")
    odd = dc.doc_filename("https://www.nyit.edu/files/My%20Form%20(2025)%3A%20v2.pdf")
    assert odd.startswith("www.nyit.edu/my_form_2025_v2__")
    assert dc.doc_filename("https://www.nyit.edu/files/.pdf").startswith("www.nyit.edu/document__")
    assert dc.doc_filename(WWW + "/a.pdf") != dc.doc_filename(WWW + "/A.pdf")
    long = dc.doc_filename(WWW + "/" + "x" * 300 + ".pdf")
    assert len(long.split("/", 1)[1]) <= 60 + 2 + 10 + 4


def test_interleave_by_host():
    urls = ["https://a.nyit.edu/1.pdf", "https://a.nyit.edu/2.pdf", "https://b.nyit.edu/1.pdf",
            "https://a.nyit.edu/3.pdf", "https://c.nyit.edu/1.pdf"]
    assert dc._interleave_by_host(urls) == [
        "https://a.nyit.edu/1.pdf", "https://b.nyit.edu/1.pdf", "https://c.nyit.edu/1.pdf",
        "https://a.nyit.edu/2.pdf", "https://a.nyit.edu/3.pdf"]


def test_looks_like_pdf(tmp_path):
    cases = {"a": b"%PDF-1.7 ...", "b": b"\r\n  %PDF-1.4", "c": b"<html>%PDF-</html>", "d": b"", "e": b"PK\x03\x04"}
    for name, body in cases.items():
        (tmp_path / name).write_bytes(body)
    assert [dc.looks_like_pdf(tmp_path / n) for n in cases] == [True, True, False, False, False]
    assert not dc.looks_like_pdf(tmp_path / "missing")


def test_tidy_text():
    raw = "- | Completed application form\nDate ________ Signature\nChapter 1 ........ 5\n2. | Second item\n" \
          "\u0092quoted\u0092 text\nab\x07c\n | \nA | . | B"
    assert dc.tidy_text(raw) == ("- Completed application form\nDate ___ Signature\nChapter 1 ... 5\n"
                                 "2. Second item\n’quoted’ text\nabc\n\nA | B")


@pytest.mark.parametrize("title, expected", [
    ("Microsoft Word - form.docx", None), ("Untitled-1", None), ("ADM_Agencies", None), ("E4795IC4", None),
    ("Vol4No3_006-13-campus buzz", None), ("", None), ("abc", None), ("Document1", None),
    ("C:\\Users\\x\\doc", None), ("Student Handbook 2025", "Student Handbook 2025"),
    ("PDF | Annual Report | New York Tech", "Annual Report"),
])
def test_metadata_title(title, expected):
    assert dc.metadata_title(title, WWW + "/files/ADM_Agencies.pdf") == expected


@pytest.mark.parametrize("anchor, expected", [
    ("Download PDF", None), ("here", None), ("Click here", None), ("form.pdf", None),
    (WWW + "/x.pdf", None), ("Download the Student Handbook (PDF)", "Student Handbook"),
    ("Student Handbook [PDF, 2 MB]", "Student Handbook"), ("View the 2025 Annual Report", "2025 Annual Report"),
    ("fall 2025 semester map", "Fall 2025 semester map"),
])
def test_anchor_title(anchor, expected):
    assert dc.anchor_title(anchor) == expected


def test_file_title_and_pick_title():
    assert dc.file_title(WWW + "/files/ADV_DegreeMap_SOHP_ExerciseScienceBS_2025.pdf") == \
        "ADV Degree Map SOHP Exercise Science BS 2025"
    assert dc.file_title(WWW + "/files/campus-map.pdf") == "Campus map"
    assert dc.file_title(WWW + "/files/Form2021.pdf") == "Form 2021"
    assert dc.pick_title("Real Title Here", "Printed", ["Anchor"], WWW + "/x.pdf") == ("Real Title Here", "metadata")
    assert dc.pick_title("", "Printed Title", ["Anchor"], WWW + "/x.pdf") == ("Printed Title", "printed_title")
    assert dc.pick_title("", None, ["Download PDF", "Anchor Text"], WWW + "/x.pdf") == ("Anchor Text", "link_text")
    assert dc.pick_title("", None, ["2026"], WWW + "/files/ComputerScienceBS_SemesterMap.pdf") == \
        ("Computer Science BS Semester Map (2026)", "file_name")
    assert dc.pick_title("", "Reference Form", [], WWW + "/files/Mental_Health_Counseling_Reference_Form.pdf") == \
        ("Mental Health Counseling Reference Form", "file_name")


@pytest.mark.parametrize("value, iso", [
    ("D:20230415093000-04'00'", "2023-04-15T09:30:00-04:00"),
    ("D:20230415093000+05'30'", "2023-04-15T09:30:00+05:30"),
    ("D:20230415093000Z", "2023-04-15T09:30:00+00:00"),
    ("D:20230415093000", "2023-04-15T09:30:00"),
    ("D:20230415", "2023-04-15"),
    ("D:18000101", None), ("D:20231301", None), ("garbage", None), ("", None), (None, None),
])
def test_pdf_date(value, iso):
    assert dc.pdf_date(value) == iso


def test_clean_toc():
    toc = [[1, "OLE_LINK7", 1], [1, "_Hlk123456", 2], [1, "1._Annual_Stipend", 3], [2, "  Real   Title ", 4],
           [1, "12345", 5], [1, "x", "bad"], [1, "Bookmark 3", 6], ["a"], [1, "_GoBack", 7]]
    assert dc.clean_toc(toc) == [[1, "1. Annual Stipend", 3], [2, "Real Title", 4]]


def test_readable_text_drops_undecodable_text_layers():
    good = "The application deadline is May 1. Students must submit transcripts."
    assert dc.readable_text(good) == good
    garbage = "!\"#$!&' %(#\"!)*+ ,-./0 !\"#$%& '()*+,\n" * 5
    assert dc.readable_text(garbage) == ""
    # a font without a Unicode map: glyph codes shifted by 29 ("New York Institute of Technology")
    shifted = "1HZ<RUN,QVWLWXWH 7HFKQRORJ\\ 2IILFH 6WXGHQW $SSOLFDWLRQ\n" * 3
    assert dc.readable_text(shifted + "NAAB Template for Faculty Resumes") == "NAAB Template for Faculty Resumes"
    # glyph stand-ins ("Name: Paul Ruppert") are dropped on any page
    assert dc.readable_text("EĂŵĞ͗ WĂƵůZƵƉƉĞƌƚ\nThe applicant must sign below.") == "The applicant must sign below."


def test_title_printed_in_large_type_on_page_one(tmp_path):
    path = make_pdf(tmp_path / "ga.pdf", [["Graduate Assistantship Application",
                                           "Please fill in every field of this form and return it to the office.",
                                           "Applicants must be enrolled full-time in a graduate program."]],
                    title_size=24, metadata={"title": "Microsoft Word - GA_form.docx"})
    texts, paths, info = dc.extract_document(path)
    assert info["printed_title"] == "Graduate Assistantship Application"
    assert info["pages"] == 1 and info["words"] > 15 and not info["scanned"]
    assert texts[0].startswith("Graduate Assistantship Application\n") and paths == [[]]
    assert dc.pick_title(info["metadata_title"], info["printed_title"], ["here"], WWW + "/files/GA_form.pdf") == \
        ("Graduate Assistantship Application", "printed_title")


def test_download_watch_describe():
    watch = dc.DownloadWatch(max_bytes=40_000_000)
    watch.exceeded, watch.announced = 50_000_000, True
    assert watch.describe() == "50.0 MB, over the 40 MB limit"
    watch.announced = False
    assert watch.describe().startswith("over the 40 MB limit (download stopped after")


def test_http_and_reply_helpers():
    assert dc._http_date("Wed, 15 May 2024 14:03:22 GMT") == "2024-05-15T14:03:22+00:00"
    assert dc._http_date("yesterday") is None and dc._http_date(None) is None
    assert dc._content_length(" 1234 ") == 1234 and dc._content_length(None) == 0
    assert isinstance(Reply(), Reply)
