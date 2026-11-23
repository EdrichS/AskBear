"""utils.py, config.py and models.py: text cleaning, JSONL I/O, settings, record contracts."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from askbear_scraper import config, models, utils
from askbear_scraper.config import Settings


@pytest.mark.parametrize("raw, clean", [
    ("  Hello   world \t ", "Hello world"),
    ("ofﬁce ﬂoor", "office floor"),                     # PDF ligatures (NFKC)
    ("zero​width soft­hyphen", "zerowidth softhyphen"),
    ("a\r\nb\rc", "a\nb\nc"),
    ("para 1\n\n\n\n\npara 2", "para 1\n\npara 2"),
    ("Reporting Out\x9d After", "Reporting Out After"),   # stray C1 control in NYIT's HTML
    ("Tech\x92s \x93AI\x94 \x96 more\x85", "Tech’s “AI” – more..."),
    ("a\x00b\x1fc\x7f", "abc"),                 # dropped, as the PDF text cleaner does
    ("\x88 bullet \x80", "bullet"),              # symbol-font glyphs, not punctuation
    ("", ""),
    (None, ""),
])
def test_clean_text(raw, clean):
    assert utils.clean_text(raw) == clean


def test_entities_and_mojibake():
    assert utils.unescape_leftovers("New York Tech&amp;rsquo;s R&amp;amp;D") == "New York Tech’s R&D"
    assert utils.unescape_leftovers("AT&T; r&eaute;sum&eacute;") == "AT&T; résumé"
    assert utils.has_leftover_entity("x &nbsp; y") and not utils.has_leftover_entity("AT&T; rocks")
    assert utils.fix_mojibake("donâ€™t â€“ cafÃ©") == "don’t – café"
    assert utils.has_mojibake("cafÃ©") and not utils.has_mojibake("café ÃÂ")
    assert utils.fix_mojibake("plain text") == "plain text"


def test_hashes_and_counts():
    assert utils.sha1("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"
    assert utils.short_hash("abc") == "a9993e3647" and len(utils.short_hash("x", 6)) == 6
    assert utils.word_count("one two\nthree") == 3 and utils.word_count("") == 0
    assert utils.dedupe_keep_order(["b", "a", "", "b", "c"]) == ["b", "a", "c"]
    assert utils.now_iso().endswith("+00:00")


def test_jsonl_round_trip_is_atomic_utf8_and_crash_safe(tmp_path):
    path = tmp_path / "sub" / "x.jsonl"
    record = models.CorpusRecord("id-1", "section", "https://www.nyit.edu/", "Café – “x”", "text", {"site": "www"})
    assert utils.write_jsonl(path, [record, {"a": "ü"}]) == 2
    assert not path.with_suffix(".jsonl.tmp").exists()
    assert "Café – “x”" in path.read_text(encoding="utf-8")          # no \\u escapes
    utils.append_jsonl(path, {"b": 1})
    with path.open("a", encoding="utf-8") as fh:
        fh.write('\n{"truncated": ')                                # a crash mid-line
    assert list(utils.read_jsonl(path)) == [record.to_dict(), {"a": "ü"}, {"b": 1}]
    assert list(utils.read_jsonl(tmp_path / "missing.jsonl")) == []


def test_json_files(tmp_path):
    path = tmp_path / "a" / "b.json"
    utils.write_json(path, {"x": "–"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": "–"}
    assert utils.read_json_file(path) == {"x": "–"}
    path.write_text("{broken", encoding="utf-8")
    assert utils.read_json_file(path) is None and utils.read_json_file(tmp_path / "none.json") is None


def test_make_soup_handles_the_catalogs_malformed_html():
    soup = utils.make_soup("<html><head><title>T</title><main><div class='Main__content'>x</div></main>")
    assert soup.select_one("div.Main__content").get_text() == "x"


def test_settings_paths(tmp_path):
    s = Settings(data_dir=tmp_path)
    assert s.raw_dir == tmp_path / "raw" and s.html_dir == tmp_path / "raw" / "html"
    assert s.pdf_dir == tmp_path / "raw" / "pdf" and s.docs_dir == tmp_path / "raw" / "docs"
    assert s.processed_dir == tmp_path / "processed"
    assert s.manifest_path.name == "crawl_manifest.jsonl" and s.docs_manifest_path.name == "docs_manifest.jsonl"
    assert s.link_graph_path.name == "link_graph.jsonl" and s.external_links_path.name == "external_links.jsonl"
    s.ensure_dirs()
    assert s.html_dir.is_dir() and s.pdf_dir.is_dir() and s.processed_dir.is_dir()


def test_default_data_dir_is_under_the_project_root(tmp_path):
    # (conftest points PROJECT_ROOT to a temp folder during the tests)
    assert Settings().data_dir == config.PROJECT_ROOT / "data"
    assert "AskBearBot" in Settings().user_agent and "contact" not in Settings().user_agent


def test_pdf_urls_and_constants():
    assert config.pdf_url("undergraduate", "2025-2026") == \
        "https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf"
    assert config.DEFAULT_PDF_EDITIONS == ("2026-2027",)
    assert config.PROPOSAL_TARGETS["pdf_pages_2025_2026"] == 581
    assert "cc" in config.TRACKING_PARAMS and ".pdf" in config.NON_HTML_EXTENSIONS
    assert len(config.SCHOOL_BY_PATH) == 6 and len(config.CAMPUS_PATTERNS) == 4


def test_record_contracts():
    page_fields = [f.name for f in fields(models.PageRecord)]
    assert page_fields[:2] == ["doc_id", "url"] and page_fields[-1] == "noindex"
    assert {"site", "description", "published", "modified"} <= set(page_fields)
    assert [f.name for f in fields(models.CorpusRecord)] == ["id", "source", "url", "title", "text", "metadata"]
    assert len(set(models.PAGE_TYPES)) == len(models.PAGE_TYPES)
    for record_type in (models.PageRecord, models.SectionRecord, models.CourseRecord, models.PdfPageRecord,
                        models.DocumentPageRecord, models.CorpusRecord):
        assert hasattr(record_type, "to_dict")


def test_setup_logging(tmp_path):
    import logging

    log_file = tmp_path / "logs" / "x.log"
    utils.setup_logging(verbose=True, log_file=log_file)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.WARNING
    logging.getLogger("askbear.test").debug("debug line")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "debug line" in log_file.read_text(encoding="utf-8")
