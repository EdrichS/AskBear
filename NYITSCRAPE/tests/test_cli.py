"""cli.py: argument parsing, help texts and how the commands are wired together."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from askbear_scraper import __version__, cli, config

COMMANDS = ["sites", "crawl", "pdf", "docs", "parse", "stats", "validate", "all"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def exit_code(argv):
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    return info.value.code


def test_main_help(capsys):
    assert exit_code(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: python -m askbear_scraper")
    for command in COMMANDS:
        assert command in out


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_has_help(command, capsys):
    assert exit_code([command, "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: python -m askbear_scraper {command}")
    assert "--data-dir" in out


def test_version(capsys):
    assert exit_code(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"python -m askbear_scraper {__version__}"


@pytest.mark.parametrize("argv", [
    ["no-such-command"],
    ["crawl", "--delay", "fast"],
    ["crawl", "--max-pages", "ten"],
    ["crawl", "--unknown-flag"],
    ["pdf", "--edition", "1999-2000"],
    ["pdf", "--kind", "law"],
    ["docs", "--max-docs", "x"],
    ["docs", "--max-mb"],
    ["validate", "--strict=yes"],
    ["parse", "--corpus-with-pdf", "extra-argument"],
])
def test_bad_arguments_exit_with_code_2(argv, capsys):
    assert exit_code(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_no_command_prints_help_and_fails(capsys):
    assert cli.main([]) == 1
    assert "usage:" in capsys.readouterr().out


def test_sites_command(tmp_path, capsys):
    assert cli.main(["sites", "--data-dir", str(tmp_path / "d")]) == 0
    out = capsys.readouterr().out
    assert "catalog.nyit.edu" in out and "my.nyit.edu" in out


def test_global_options_work_before_or_after_the_command(tmp_path):
    parser = cli.build_parser()
    before = parser.parse_args(["--data-dir", str(tmp_path / "a"), "-v", "parse"])
    after = parser.parse_args(["parse", "--data-dir", str(tmp_path / "b"), "-v"])
    assert before.data_dir == tmp_path / "a" and before.verbose
    assert after.data_dir == tmp_path / "b" and after.verbose
    default = parser.parse_args(["parse"])
    assert default.data_dir == config.PROJECT_ROOT / "data"


def test_settings_from_args():
    args = cli.build_parser().parse_args(
        ["crawl", "--delay", "2", "--timeout", "12", "--retries", "1", "--contact", "you@nyit.edu",
         "--refresh", "--max-pages", "10", "--include-print", "--site", "catalog.nyit.edu"])
    s = cli.settings_from_args(args)
    assert (s.delay, s.timeout, s.max_retries, s.max_pages) == (2.0, 12.0, 1, 10)
    assert s.refresh and s.include_print_pages and s.respect_robots   # robots.txt is always obeyed
    assert "you@nyit.edu" in s.user_agent
    assert args.site == ["catalog.nyit.edu"]


def test_defaults_and_minimum_delay():
    s = cli.settings_from_args(cli.build_parser().parse_args(["crawl", "--delay", "0.1"]))
    assert s.delay == 0.5                                # never faster than 0.5 s
    s = cli.settings_from_args(cli.build_parser().parse_args(["parse"]))
    assert (s.delay, s.max_retries, s.max_pages, s.respect_robots) == (1.0, 4, 0, True)


def test_main_creates_the_data_folders_and_writes_a_log_file(tmp_path, capsys):
    data, log_file = tmp_path / "data", tmp_path / "logs" / "run.log"
    assert cli.main(["stats", "--data-dir", str(data), "--log-file", str(log_file)]) == 0
    for sub in ("raw/html", "raw/pdf", "processed"):
        assert (data / sub).is_dir()
    logging.getLogger("askbear").info("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello from the test" in log_file.read_text(encoding="utf-8")


def test_crawl_with_no_crawlable_site_fails_cleanly(tmp_path, capsys):
    assert cli.main(["crawl", "--site", "example.com", "--data-dir", str(tmp_path / "d")]) == 1


def test_parse_without_a_crawl_fails(tmp_path, capsys):
    assert cli.main(["parse", "--data-dir", str(tmp_path / "d")]) == 1


def test_docs_without_a_crawl_fails(tmp_path, capsys):
    assert cli.main(["docs", "--data-dir", str(tmp_path / "d")]) == 1


def test_parse_command_on_the_fixture_crawl(tmp_path, capsys):
    import testkit
    from askbear_scraper.config import Settings

    settings = Settings(data_dir=tmp_path / "data")
    testkit.build_fixture_crawl(settings, extras=False)
    assert cli.main(["parse", "--corpus-without-docs", "--data-dir", str(settings.data_dir)]) == 0
    assert (settings.processed_dir / "corpus.jsonl").is_file()


def test_keyboard_interrupt_exits_with_130(monkeypatch, tmp_path, capsys):
    def interrupted(settings, args):
        raise KeyboardInterrupt
    monkeypatch.setitem(cli.COMMANDS, "stats", interrupted)
    assert cli.main(["stats", "--data-dir", str(tmp_path / "d")]) == 130


# ---------------------------------------------------------------------------
# `all`: crawl -> pdf -> docs -> parse -> stats -> validate
# ---------------------------------------------------------------------------
@pytest.fixture
def recorded_steps(monkeypatch):
    calls = []
    results = {}

    def fake(name):
        def step(settings, args):
            calls.append(name)
            return results.get(name, 0)
        return step

    for name in ("crawl", "pdf", "docs", "parse", "stats", "validate"):
        monkeypatch.setattr(cli, f"cmd_{name}", fake(name))
    return calls, results


def test_all_runs_every_step_in_order(recorded_steps, tmp_path):
    calls, _ = recorded_steps
    assert cli.main(["all", "--data-dir", str(tmp_path / "d")]) == 0
    assert calls == ["crawl", "pdf", "docs", "parse", "stats", "validate"]


def test_all_can_skip_the_pdf_steps(recorded_steps, tmp_path):
    calls, _ = recorded_steps
    assert cli.main(["all", "--skip-pdf", "--skip-docs", "--data-dir", str(tmp_path / "d")]) == 0
    assert calls == ["crawl", "parse", "stats", "validate"]


def test_all_stops_when_the_crawl_fails(recorded_steps, tmp_path):
    calls, results = recorded_steps
    results["crawl"] = 1
    assert cli.main(["all", "--data-dir", str(tmp_path / "d")]) == 1
    assert calls == ["crawl"]


def test_all_reports_a_failed_step_but_finishes(recorded_steps, tmp_path):
    calls, results = recorded_steps
    results["pdf"] = 1
    assert cli.main(["all", "--data-dir", str(tmp_path / "d")]) == 1
    assert calls == ["crawl", "pdf", "docs", "parse", "stats", "validate"]


def test_all_offline_end_to_end(tmp_path, monkeypatch, capsys):
    """crawl -> pdf -> docs -> parse -> stats -> validate through the CLI, against FakeWeb."""
    import json
    from dataclasses import replace

    import testkit
    from askbear_scraper import crawler, documents, pdf_catalog, sites

    web = testkit.FakeWeb()
    pages = ("catalog_school.html", "catalog_program.html", "catalog_curriculum.html",
             "catalog_course_listing.html", "www_home.html", "www_degree.html", "www_news.html")
    for name in pages:
        web.add(testkit.fixture_url(name), testkit.fixture_html(name))
    minor_form = "https://www.nyit.edu/files/registrar/REGISTRAR_Forms_ApplicationToDeclareUGMinor.pdf"
    web.add(minor_form, testkit.pdf_bytes(tmp_path, "form.pdf", pages=["Application to Declare a Minor",
                                                                     "Student name and signature."]),
            content_type="application/pdf")
    web.add(config.pdf_url("undergraduate", "2025-2026"),
            testkit.pdf_bytes(tmp_path, "ug.pdf", pages=["General Information", "Admissions"],
                              toc=[[1, "General Information", 1], [1, "Admissions", 2]]),
            content_type="application/pdf")
    monkeypatch.setitem(sites.SITES, "catalog.nyit.edu", replace(
        sites.SITES["catalog.nyit.edu"], seeds=("/engineering", "/courses?cal=UG&cs=CSCI")))
    monkeypatch.setitem(sites.SITES, "www.nyit.edu", replace(
        sites.SITES["www.nyit.edu"], sitemaps=(),
        seeds=("/", "/academics/degrees/technology-entrepreneurship-minor/",
               "/news/articles/nyit-ranks-well-among-best-engineering-programs-in-u-s/")))

    def fake_fetcher(settings, *args, **kwargs):
        return testkit.make_fetcher(settings, web)

    for module in (crawler, pdf_catalog, documents):
        monkeypatch.setattr(module, "PoliteFetcher", fake_fetcher)

    data = tmp_path / "data"
    code = cli.main(["all", "--site", "catalog.nyit.edu", "--site", "www.nyit.edu", "--edition", "2025-2026",
                     "--kind", "undergraduate", "--corpus-with-pdf", "--data-dir", str(data)])
    assert code == 0
    summary = json.loads((data / "raw" / "crawl_summary.json").read_text(encoding="utf-8"))
    assert summary["status_counts"]["ok"] == len(pages) and not summary["interrupted"]
    processed = data / "processed"
    for name in ("pdf_pages.jsonl", "pdf_summary.json", "documents.jsonl", "documents_summary.json",
                 "pages.jsonl", "sections.jsonl", "courses.jsonl", "courses.csv", "corpus.jsonl",
                 "parse_summary.json", "stats.json", "STATS.md", "validation_report.json"):
        assert (processed / name).is_file(), name
    corpus = testkit.read_jsonl(processed / "corpus.jsonl")
    sources = {r["source"] for r in corpus}
    assert sources == {"section", "course", "document", "pdf_page"}
    document = next(r for r in corpus if r["source"] == "document")
    assert document["url"] == minor_form + "#page=1"
    assert document["metadata"]["found_on"] == [testkit.fixture_url("www_degree.html")]
    report = json.loads((processed / "validation_report.json").read_text(encoding="utf-8"))
    assert {c["status"] for c in report["checks"]} <= {"PASS", "WARN", "FAIL", "SKIP"}
    checks = {c["name"]: c for c in report["checks"]}
    for name in ("PDF pages match pdf_summary", "PDF empty pages (< 5%)", "PDF documents: every page present",
                 "Corpus matches PDF documents", "Text hygiene: catalog PDF pages", "Text hygiene: PDF documents",
                 "Corpus records", "Course fields", "Unique page URLs and doc_ids"):
        assert checks[name]["status"] == "PASS", (name, checks[name]["message"])
    assert "AskBear data validation" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the real entry point
# ---------------------------------------------------------------------------
def test_python_dash_m_entry_point(tmp_path):
    result = subprocess.run([sys.executable, "-m", "askbear_scraper", "--version"], cwd=str(PROJECT_ROOT),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"python -m askbear_scraper {__version__}"
