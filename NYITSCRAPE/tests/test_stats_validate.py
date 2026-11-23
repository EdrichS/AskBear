"""stats.py and validate.py: smoke tests on the fixture dataset (conftest.parsed_dataset)."""

from __future__ import annotations

import json
import re
import shutil

import pytest

from askbear_scraper import cli, stats, validate
from askbear_scraper.config import Settings

from testkit import read_jsonl, write_jsonl

STATUSES = {"PASS", "WARN", "FAIL", "SKIP"}


@pytest.fixture
def dataset_copy(parsed_dataset, tmp_path):
    """A private copy of the parsed fixture dataset that a test may damage."""
    target = tmp_path / "data"
    shutil.copytree(parsed_dataset.data_dir, target)
    return Settings(data_dir=target)


def checks_by_name(report):
    return {c["name"]: c for c in report["checks"]}


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def test_stats_report_on_the_fixture_dataset(dataset_copy, capsys):
    st = stats.run_stats(dataset_copy)
    out = capsys.readouterr().out
    assert out.startswith("AskBear dataset statistics")
    assert "We scraped 18 pages from 10 NYIT websites" in out
    saved = json.loads((dataset_copy.processed_dir / "stats.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(st))
    md = (dataset_copy.processed_dir / "STATS.md").read_text(encoding="utf-8")
    assert md.startswith("# AskBear dataset statistics")
    assert "## Proposal vs. scraped" in md and "| catalog.nyit.edu" in md
    assert st["inputs"]["pages.jsonl"] and not st["inputs"]["pdf_pages.jsonl"]
    assert st["pages"]["records"] == 18 and st["pages"]["distinct"] == 17
    assert st["courses"]["records"] == 7
    assert st["corpus"]["records"] == st["sections"]["count"] + 7
    assert {r["site"] for r in st["sites"] if r["pages"]} >= {"catalog.nyit.edu", "www.nyit.edu"}
    rows = {row["metric"]: row for row in st["proposal_vs_scraped"]}
    assert rows["Catalog PDF pages, 2025-26 (UG + GR)"]["proposal"] == 581
    assert rows["Catalog PDF pages, 2025-26 (UG + GR)"]["scraped"] is None      # pdf step not run
    assert rows["Course codes (UG + GR)"]["scraped"] == 7
    assert rows["Campuses"]["scraped"] == 4


def test_stats_before_anything_was_scraped(settings, capsys):
    settings.ensure_dirs()
    st = stats.run_stats(settings)
    assert not any(st["inputs"].values())
    md = (settings.processed_dir / "STATS.md").read_text(encoding="utf-8")
    assert "n/a" in md and "python -m askbear_scraper crawl" in md
    assert capsys.readouterr().out


def test_stats_number_helpers():
    assert stats.percentile([1, 2, 3, 4], 50) == 2.5
    assert stats.percentile([], 90) is None
    assert stats.describe([1, 2, 3, 10]) == {"count": 4, "total": 16, "min": 1, "median": 2.5, "mean": 4.0,
                                            "p90": 7.9, "max": 10}
    assert stats.describe([])["median"] is None
    assert stats.pct(1, 3) == 33.3 and stats.pct(1, 0) is None
    assert stats.fmt_num(1234567) == "1,234,567" and stats.fmt_num(12.5) == "12.5"
    assert stats.fmt_num(None) == "n/a" and stats.fmt_num(True) == "yes" and stats.fmt_num(3.0) == "3"
    assert stats.fmt_pct(None) == "n/a" and stats.fmt_pct(12.345) == "12.3%"
    assert stats.plural(1, "page") == "1 page" and stats.plural(3, "copy", "copies") == "3 copies"
    assert stats.fmt_time("2026-11-23T07:05:49+00:00") == "2026-11-23 07:05 UTC"
    assert stats._duration(7692) == "2 h 08 min" and stats._duration(75) == "1 min 15 s"
    assert stats._duration(None) == "n/a"


def test_stats_tables_render_as_text_and_markdown():
    blocks = [("h1", "Title"), ("p", "A paragraph."), ("bullets", ["one", "two"]),
              ("kv", [("Pages", "18")]), ("table", (["Site", "Pages"], [["a|b", 3]], "lr"))]
    text = stats.render_text(blocks)
    assert text.startswith("Title\n=====\n") and "- one" in text
    assert re.search(r"^a\|b +3$", text, re.M)
    md = stats.render_markdown(blocks)
    assert "# Title" in md and "| a\\|b | 3 |" in md and "|---|---:|" in md


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_the_fixture_dataset(dataset_copy, capsys):
    report = validate.run_validate(dataset_copy)
    out = capsys.readouterr().out
    assert out.startswith("AskBear data validation")
    assert "Result: " in out
    statuses = [c["status"] for c in report["checks"]]
    assert set(statuses) <= STATUSES
    assert sum(report["counts"].values()) == len(report["checks"]) >= 30
    assert report["counts"] == {s: statuses.count(s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    assert report["passed"] is (report["counts"]["FAIL"] == 0)
    saved = json.loads((dataset_copy.processed_dir / "validation_report.json").read_text(encoding="utf-8"))
    assert saved["counts"] == report["counts"]
    checks = checks_by_name(report)
    for name in ("Unique page URLs and doc_ids", "Duplicate pages", "noindex pages kept out of the index",
                 "Corpus records", "Text hygiene: pages", "Text hygiene: sections", "Course fields"):
        assert checks[name]["status"] == "PASS", (name, checks[name]["message"])
    assert checks["PDF pages match pdf_summary"]["status"] == "SKIP"     # the pdf step did not run
    assert checks["Crawl finished"]["status"] == "WARN"                   # no crawl_summary.json
    for c in report["checks"]:
        assert c["group"] and c["message"] and len(c["examples"]) <= validate.MAX_EXAMPLES


def test_validate_an_empty_data_folder_fails(settings, capsys):
    settings.ensure_dirs()
    report = validate.run_validate(settings)
    assert not report["passed"]
    assert checks_by_name(report)["Crawl finished"]["status"] == "FAIL"
    assert "FAILED" in capsys.readouterr().out


def test_validate_finds_leftover_html_in_sections(dataset_copy):
    path = dataset_copy.processed_dir / "sections.jsonl"
    sections = read_jsonl(path)
    target = next(s for s in sections if s["site"] == "catalog.nyit.edu")
    target["text"] += " Tuition &amp; fees. Back to Top"
    write_jsonl(path, sections)
    report = validate.run_validate(dataset_copy)
    check = checks_by_name(report)["Text hygiene: sections"]
    assert check["status"] == "FAIL" and check["count"] == 2
    assert any(target["section_id"] in e for e in check["examples"])
    assert not report["passed"]


def test_validate_finds_duplicate_corpus_ids(dataset_copy):
    path = dataset_copy.processed_dir / "corpus.jsonl"
    corpus = read_jsonl(path)
    write_jsonl(path, corpus + corpus[:1])
    check = checks_by_name(validate.run_validate(dataset_copy))["Corpus records"]
    assert check["status"] == "FAIL"


def test_a_crashing_check_is_reported_as_fail(dataset_copy, monkeypatch):
    def broken(_inputs):
        raise KeyError("boom")
    monkeypatch.setattr(validate, "CHECKS", [("Crawl", broken)] + validate.CHECKS[1:])
    report = validate.run_validate(dataset_copy)
    first = report["checks"][0]
    assert first["status"] == "FAIL" and "crashed" in first["message"] and first["group"] == "Crawl"


def test_hygiene_check_directly():
    records = [{"id": "a", "site": "catalog.nyit.edu", "text": "fine text"},
               {"id": "b", "site": "catalog.nyit.edu", "text": "ofﬁce <br> javascript: x"}]
    check = validate.hygiene_check("demo", records, "id", ("text",), "FAIL", "demo.jsonl")
    assert check.status == "FAIL" and check.count == 3
    assert validate.hygiene_check("demo", records[:1], "id", ("text",), "FAIL", "x").status == "PASS"
    assert validate.hygiene_check("demo", None, "id", ("text",), "FAIL", "x").status in ("SKIP", "FAIL")


@pytest.mark.parametrize("strict, expected", [(False, 0), (True, 2)])
def test_validate_strict_exit_code_when_a_check_fails(settings, strict, expected, capsys):
    settings.ensure_dirs()
    argv = ["validate", "--data-dir", str(settings.data_dir)] + (["--strict"] if strict else [])
    assert cli.main(argv) == expected


def test_validate_strict_exit_code_follows_the_report(dataset_copy, capsys):
    report = validate.run_validate(dataset_copy)
    code = cli.main(["validate", "--strict", "--data-dir", str(dataset_copy.data_dir)])
    assert code == (0 if report["passed"] else 2)


def test_stats_command(dataset_copy, capsys):
    assert cli.main(["stats", "--data-dir", str(dataset_copy.data_dir)]) == 0
    assert (dataset_copy.processed_dir / "STATS.md").is_file()
    assert "Proposal vs. scraped" in capsys.readouterr().out
