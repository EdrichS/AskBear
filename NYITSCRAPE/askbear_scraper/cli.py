"""Command-line interface.

    python -m askbear_scraper sites      # which NYIT websites are crawled (and why not)
    python -m askbear_scraper crawl      # download every page of every NYIT site into data/raw/html
    python -m askbear_scraper pdf        # download + extract the full catalog PDFs
    python -m askbear_scraper docs       # download + extract linked PDF and Office files
                                         # (docs --max-docs 0: re-extract without downloading)
    python -m askbear_scraper parse      # turn cached pages into JSONL datasets
    python -m askbear_scraper stats      # dataset statistics (compare with the proposal)
    python -m askbear_scraper validate   # quality checks on the outputs
    python -m askbear_scraper all        # crawl -> pdf -> docs -> parse -> stats -> validate
    python -m askbear_scraper prune      # list (--yes: delete) cache entries sites.py no longer crawls

Global options (--data-dir, -v, --log-file) work before or after the command name.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List, Optional

from . import __version__, config
from .config import Settings
from .utils import setup_logging

log = logging.getLogger("askbear")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def _add_network_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("politeness / network")
    g.add_argument("--delay", type=float, default=1.0,
                   help="seconds to wait between requests (default: 1.0; please don't go below 0.5)")
    g.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    g.add_argument("--retries", type=int, default=4, help="retries per request on errors (default: 4)")
    g.add_argument("--contact", default="",
                   help="team e-mail to include in the User-Agent, e.g. you@nyit.edu (recommended)")
    g.add_argument("--refresh", action="store_true",
                   help="ignore the local cache and download everything again")


def _add_crawl_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("what to crawl")
    g.add_argument("--site", action="append", metavar="HOST",
                   help="crawl only this website, e.g. --site catalog.nyit.edu; repeat for "
                        "several (default: every site listed by the `sites` command)")
    g.add_argument("--max-pages", type=int, default=None,
                   help="stop after this many pages PER WEBSITE (default: each site's own "
                        "limit in sites.py; this can only lower it). Pages cached earlier "
                        "stay in the data, but use a separate --data-dir for quick tests")
    g.add_argument("--include-print", action="store_true",
                   help="also crawl catalog /print/ pages (whole sections on one page; "
                        "duplicates content)")
    g.add_argument("--refresh-sitemaps", action="store_true",
                   help="download the sitemap lists again even if the cached copy is newer "
                        "than 7 days (older copies are always downloaded again)")


def _add_corpus_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("what goes into corpus.jsonl")
    g.add_argument("--corpus-with-pdf", action="store_true",
                   help="also add the catalog PDF pages (from `pdf`); off by default because "
                        "they repeat the catalog website")
    g.add_argument("--corpus-without-docs", action="store_true",
                   help="leave out the documents (PDF, Word, Excel, PowerPoint) found on NYIT "
                        "sites (from `docs`)")
    g.add_argument("--corpus-with-superseded", action="store_true",
                   help="also add older editions of yearly PDF documents (last year's "
                        "handbook, past term calendars...; marked superseded in documents.jsonl)")
    g.add_argument("--corpus-with-near-duplicates", action="store_true",
                   help="keep every copy of a passage that other pages repeat (nearly) word for "
                        "word; by default one copy is kept per program/course")


def _common_options() -> argparse.ArgumentParser:
    """--data-dir, -v and --log-file, accepted after the command name too
    (``python -m askbear_scraper parse --data-dir D``). SUPPRESS keeps the value given
    before the command name unless the option is repeated after it."""
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("general")
    g.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                   help="where raw and processed data are stored (default: the data folder "
                        "next to the askbear_scraper package, whatever the current folder)")
    g.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="show debug messages")
    g.add_argument("--log-file", type=Path, default=argparse.SUPPRESS,
                   help="also write log messages to this file")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m askbear_scraper",
        description="AskBear stage 1: scrape the public NYIT websites (catalog.nyit.edu, "
                    "www.nyit.edu, ...) into clean datasets for search.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, default=config.PROJECT_ROOT / "data",
                        help="where raw and processed data are stored (default: the data folder "
                             "next to the askbear_scraper package, whatever the current folder)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="also write log messages to this file")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    common = [_common_options()]

    sub.add_parser("sites", parents=common,
                   help="list the NYIT websites that are crawled, and why others are not")

    p = sub.add_parser("crawl", parents=common,
                       help="download every page of the NYIT websites (cached, resumable)")
    _add_network_options(p)
    _add_crawl_options(p)

    p = sub.add_parser("parse", parents=common,
                       help="parse cached pages into pages/sections/courses/corpus JSONL")
    _add_corpus_options(p)

    p = sub.add_parser(
        "pdf", parents=common, help="download the full catalog PDFs and extract page text",
        description="Download the requested catalog PDFs into data/raw/pdf (each file only "
                    "once) and extract one record per page. pdf_pages.jsonl always covers "
                    "EVERY catalog PDF already in data/raw/pdf, so runs for different "
                    "editions add up.")
    _add_network_options(p)
    p.add_argument("--edition", action="append", choices=sorted(config.PDF_EDITIONS),
                   help="catalog year; repeat for several (default: 2026-2027). "
                        "Use 2025-2026 to reproduce the proposal's 581 pages / ~249K words.")
    p.add_argument("--kind", action="append", choices=sorted(config.PDF_KINDS),
                   help="which catalog; repeat for several (default: undergraduate + graduate; "
                        "osteopathic = College of Osteopathic Medicine)")

    p = sub.add_parser(
        "docs", parents=common,
        help="download and extract the PDF, Word, Excel and PowerPoint documents linked from "
             "NYIT pages (handbooks, forms...)",
        description="Download every PDF and every .docx/.xlsx/.pptx file linked from the crawled "
                    "NYIT pages (catalog PDFs are handled by `pdf`) into data/raw/docs and "
                    "extract one record per page (Office files: about 150-400 words, a sheet's "
                    "rows, a slide) into data/processed/documents.jsonl. Run `crawl` first.")
    _add_network_options(p)
    p.add_argument("--max-docs", type=int, default=None,
                   help="download at most this many documents (default: all); 0 downloads "
                        "nothing and only re-extracts the files already in data/raw/docs")
    p.add_argument("--max-mb", type=float, default=None,
                   help="skip files larger than this many megabytes (default: 40; 0 = no limit)")

    sub.add_parser("stats", parents=common, help="print and save dataset statistics")

    p = sub.add_parser(
        "prune", parents=common,
        help="list (and with --yes delete) cached pages that sites.py no longer crawls",
        description="Clean up the crawl cache: manifest entries of websites or paths that "
                    "sites.py no longer crawls (e.g. catalog-dev.nyit.edu) and HTML files "
                    "that no manifest entry refers to. Without --yes nothing is changed: the "
                    "command only reports what would be removed. `parse` ignores these "
                    "entries anyway, so pruning only saves disk space.")
    p.add_argument("--yes", action="store_true",
                   help="really remove the entries and delete the files (cannot be undone)")

    p = sub.add_parser("validate", parents=common, help="run data-quality checks on the outputs")
    p.add_argument("--strict", action="store_true",
                   help="exit with an error code if any check fails (useful in scripts)")

    p = sub.add_parser("all", parents=common,
                       help="crawl, pdf, docs, parse, stats and validate in one go")
    _add_network_options(p)
    _add_crawl_options(p)
    p.add_argument("--edition", action="append", choices=sorted(config.PDF_EDITIONS),
                   help="PDF catalog year; repeat for several (default: 2026-2027)")
    p.add_argument("--kind", action="append", choices=sorted(config.PDF_KINDS),
                   help="PDF catalog; repeat for several (default: undergraduate + graduate)")
    p.add_argument("--skip-pdf", action="store_true", help="skip the catalog PDFs (`pdf` step)")
    p.add_argument("--skip-docs", action="store_true",
                   help="skip the documents linked on NYIT sites (`docs` step)")
    p.add_argument("--max-docs", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--max-mb", type=float, default=None, help=argparse.SUPPRESS)
    _add_corpus_options(p)
    p.add_argument("--strict", action="store_true",
                   help="exit with an error code if a validation check fails")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    s = Settings(data_dir=args.data_dir)
    for name, attr in (("delay", "delay"), ("timeout", "timeout"), ("retries", "max_retries"),
                       ("contact", "contact"), ("refresh", "refresh"), ("max_pages", "max_pages"),
                       ("include_print", "include_print_pages"),
                       ("refresh_sitemaps", "refresh_sitemaps")):
        # options a command does not have, or that were not given, keep the defaults
        if getattr(args, name, None) is not None:
            setattr(s, attr, getattr(args, name))
    if s.delay < 0.5:
        log.warning("--delay %.2f is very aggressive for a university server; using 0.5", s.delay)
        s.delay = 0.5
    return s


# ---------------------------------------------------------------------------
# commands (imports are local so one broken step never blocks the others)
# ---------------------------------------------------------------------------
def cmd_sites(s: Settings, args) -> int:
    from .sites import describe_sites

    print(describe_sites())
    return 0


def cmd_crawl(s: Settings, args) -> int:
    from .crawler import Crawler

    only = getattr(args, "site", None)
    log.info("crawling %s with a %.1fs delay per website (cache: %s)",
             ", ".join(only) if only else "every NYIT website in sites.py", s.delay, s.html_dir)
    try:
        summary = Crawler(s, only_sites=only).run()
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    failed = summary.get("failed_sites") or []
    if failed:
        # `all` stops here, so it never parses a dataset without (say) the catalog
        log.error("the crawl saved no page of %s (see failed_sites and unreachable_reasons in "
                  "crawl_summary.json); run `crawl` again later", ", ".join(failed))
    return 1 if summary["interrupted"] or failed else 0


def cmd_prune(s: Settings, args) -> int:
    from .crawler import prune_cache

    report = prune_cache(s, dry_run=not getattr(args, "yes", False))
    verb = "would be" if report["dry_run"] else "were"
    print(f"{report['entries_removed']:,} manifest entries {verb} removed"
          + (" (" + ", ".join(f"{h} {n:,}" for h, n in
                              report["entries_removed_by_site"].items()) + ")"
             if report["entries_removed_by_site"] else ""))
    print(f"{report['files_deleted']:,} cached HTML files {verb} deleted "
          f"({report['megabytes_freed']:,.1f} MB)")
    for example in report.get("examples") or []:
        print(f"  e.g. {example}")
    if report["dry_run"] and (report["entries_removed"] or report["files_deleted"]):
        print("Nothing was changed. Run `python -m askbear_scraper prune --yes` to remove them "
              "(cannot be undone; `parse` ignores them anyway).")
    return 0


def cmd_docs(s: Settings, args) -> int:
    from .documents import run_docs

    summary = run_docs(s, max_docs=getattr(args, "max_docs", None),
                       max_mb=getattr(args, "max_mb", None))
    return 1 if summary.get("error") else 0


def cmd_parse(s: Settings, args) -> int:
    from .pipeline import run_parse

    summary = run_parse(s, corpus_with_pdf=getattr(args, "corpus_with_pdf", False),
                        corpus_with_docs=not getattr(args, "corpus_without_docs", False),
                        corpus_with_superseded=getattr(args, "corpus_with_superseded", False),
                        corpus_with_near_duplicates=getattr(args, "corpus_with_near_duplicates",
                                                            False))
    return 1 if summary.get("error") else 0


def cmd_pdf(s: Settings, args) -> int:
    from .pdf_catalog import run_pdf

    editions = args.edition or list(config.DEFAULT_PDF_EDITIONS)
    kinds = args.kind or list(config.DEFAULT_PDF_KINDS)
    summary = run_pdf(s, editions=editions, kinds=kinds)
    return 0 if not summary.get("failed") else 1


def cmd_stats(s: Settings, args) -> int:
    from .stats import run_stats

    run_stats(s)
    return 0


def cmd_validate(s: Settings, args) -> int:
    from .validate import run_validate

    report = run_validate(s)
    if getattr(args, "strict", False) and not report.get("passed", False):
        return 2
    return 0


def cmd_all(s: Settings, args) -> int:
    code = cmd_crawl(s, args)
    if code:
        return code
    if not args.skip_pdf:
        code = cmd_pdf(s, args) or code
    if not args.skip_docs:
        code = cmd_docs(s, args) or code
    code = cmd_parse(s, args) or code
    cmd_stats(s, args)
    return cmd_validate(s, args) or code


COMMANDS = {
    "sites": cmd_sites,
    "crawl": cmd_crawl,
    "docs": cmd_docs,
    "parse": cmd_parse,
    "pdf": cmd_pdf,
    "stats": cmd_stats,
    "validate": cmd_validate,
    "all": cmd_all,
    "prune": cmd_prune,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    setup_logging(args.verbose, args.log_file)
    settings = settings_from_args(args)
    settings.ensure_dirs()
    try:
        return COMMANDS[args.command](settings, args)
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        return 130
