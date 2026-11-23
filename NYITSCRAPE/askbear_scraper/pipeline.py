"""Turn the crawl cache into the datasets the rest of AskBear uses.

    python -m askbear_scraper parse [--corpus-with-pdf] [--corpus-without-docs]
                                    [--corpus-with-superseded] [--corpus-with-near-duplicates]

Input (written by ``crawl``, ``docs`` and ``pdf``; never modified here):
    data/raw/crawl_manifest.jsonl     which URLs were fetched and where their HTML is cached
    data/raw/html/<host>/*.html       the cached pages of every NYIT website
    data/processed/documents.jsonl    optional, written by ``docs`` (PDF/Word/Excel/PowerPoint
                                      documents linked on the sites)
    data/processed/pdf_pages.jsonl    optional, written by ``pdf`` (only for --corpus-with-pdf)

Output (data/processed/):
    pages.jsonl          one PageRecord per page of every website (exact copies get ``duplicate_of``)
    sections.jsonl       heading-delimited passages (SectionRecord) of every distinct, indexable page
    courses.jsonl        one CourseRecord per course of the catalog's course directory
    courses.csv          the same courses as a spreadsheet (opens in Excel)
    corpus.jsonl         what the search engine indexes: sections + courses + documents
                         (+ the full catalog PDFs' pages with --corpus-with-pdf)
    parse_summary.json   counts per website, failures and timings of the run

Each page is parsed according to the website its content comes from (the host of the URL it
finally redirected to, see sites.py): catalog.nyit.edu with the catalog parser (html_parser +
course_parser), every other NYIT site with the generic parser (site_parser). The generic
parser first learns, per site, the blocks that repeat on a large share of its pages ("Request
Information / Plan Your Visit / Apply Today"...) and leaves them out of the page text.

Exact copies (the same text under two URLs, also on two different websites), pages that ask
search engines not to index them (<meta name="robots" content="noindex">) and pages with
fewer than MIN_INDEXED_WORDS words stay in pages.jsonl (``duplicate_of`` / ``noindex`` /
``word_count`` tell which) but get no sections and no corpus records. Error pages served
with HTTP 200 ("404 | ...", empty catalog program templates) are skipped altogether. Every
record names its website (``site``; ``metadata["site"]`` in corpus.jsonl).

Passages are cleaned up before they become corpus records:
* a section with fewer than MIN_SECTION_WORDS words ("TOTAL CREDITS: 30", "Yes.") is merged
  into the section before it on the same page (merge_tiny_sections);
* a passage that a website repeats word for word on several of its pages is kept once
  (drop_repeated_sections);
* near-duplicates across the whole corpus (word 5-shingles, Jaccard >= NEAR_DUP_JACCARD) are
  grouped: copies that belong to the same program/level (or to none) are kept once, on the
  most authoritative source (catalog, policy library, www.nyit.edu, ... , PDFs last); copies
  that belong to different programs or courses all stay and share ``metadata["dup_group"]``
  / ``metadata["dup_count"]`` so the search can show one result per group
  (collapse_near_duplicates). A section left out this way keeps ``duplicate_of`` in
  sections.jsonl;
* document pages with fewer than MIN_DOCUMENT_WORDS words and older editions of yearly
  documents (``superseded`` in documents.jsonl, see documents.py) are left out, unless
  ``--corpus-with-superseded`` is given.

Everything is read from the local cache, so parsing needs no network and can be re-run as
often as you like. A page that fails to parse is logged and skipped; it never stops the run.
"""

from __future__ import annotations

import csv
import heapq
import json
import logging
import math
import os
import re
import time
from array import array
from collections import Counter, defaultdict
from dataclasses import fields
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from tqdm import tqdm

from . import config, sites
from .config import Settings
from .crawler import load_manifest
from .models import CorpusRecord, CourseRecord, PageRecord, SectionRecord
from .urls import link_kind, normalize_url, url_host, url_path
from .utils import now_iso, read_json_file, read_jsonl, sha1, word_count, write_json, write_jsonl

log = logging.getLogger("askbear.pipeline")

# Output file names inside data/processed/ (stats.py and validate.py read them too).
PAGES_FILE = "pages.jsonl"
SECTIONS_FILE = "sections.jsonl"
COURSES_FILE = "courses.jsonl"
COURSES_CSV_FILE = "courses.csv"
CORPUS_FILE = "corpus.jsonl"
PARSE_SUMMARY_FILE = "parse_summary.json"
PDF_PAGES_FILE = "pdf_pages.jsonl"          # written by `pdf` (pdf_catalog.py)
PDF_SUMMARY_FILE = "pdf_summary.json"       # written by `pdf` (pdf_catalog.py)
DOCUMENTS_FILE = "documents.jsonl"          # written by `docs` (documents.py)
DOCUMENTS_SUMMARY_FILE = "documents_summary.json"   # written by `docs` (documents.py)
DOCS_MANIFEST_FILE = "docs_manifest.jsonl"  # in data/raw/, written by `docs`

# The website whose pages are parsed with the catalog parser and hold the course directory.
CATALOG_HOST = url_host(config.BASE_URL)    # "catalog.nyit.edu"

# When two websites publish exactly the same text, the copy on the first site of this list
# is kept (then the other sites in alphabetical order): the catalog is the official source,
# the main website the next best link to show.
SITE_PRIORITY: Tuple[str, ...] = (CATALOG_HOST, "www.nyit.edu")

# Two pages only count as copies of each other when their text has at least this many
# words (so two different pages that both just say "Coming soon" are not merged).
MIN_DUPLICATE_WORDS = 10

# Sections of these page types never become search results: the course index is just a
# list of subjects, and every course already has its own corpus record.
CORPUS_EXCLUDED_PAGE_TYPES = ("course_index", "course_listing")

# Course-directory entries that are not courses anyone can take: NYIT lists 344 legacy
# registrar codes under the subject "Conversion - OM" (OM 9600-9783, OM AP01-AP56, ...), all
# with 0 credits, no description and cryptic titles ("Cl Clk FP Outrch"). They stay in
# courses.jsonl / courses.csv but are left out of corpus.jsonl, where hundreds of tiny,
# almost identical records would crowd out real answers to medicine questions.
LEGACY_SUBJECT_NAME = re.compile(r"^conversion\b", re.IGNORECASE)

BOILERPLATE_EXAMPLES = 8       # repeated blocks per site shown in parse_summary.json

# "Not found" pages served with HTTP 200 that the crawler did not recognise: blogs.nyit.edu
# answers a broken link with a page titled "404 | Build Ahead | Blogs NYIT". They are skipped
# (listed under "not_found_page" in parse_summary.json) when their text is this short.
_NOT_FOUND_TITLE = re.compile(r"^\s*(404|page not found|not found|error 404)\b", re.IGNORECASE)
NOT_FOUND_MAX_WORDS = 100

# catalog.nyit.edu answers /curriculum/<slug> and /semester-map/<slug> for slugs that are not a
# program (www.nyit.edu links to many of them) with an empty "Academics > Degrees" template:
# no program name, no text but "TOTAL CREDITS:". The same template for a program that is no
# longer offered is just as empty ("CLOSED Minor in Equity and Innovation"). Skipped as "not
# found" pages too.
_DEGREE_TEMPLATE_PATH = re.compile(r"^/(curriculum|semester-map)/", re.IGNORECASE)
_CLOSED_PROGRAM = re.compile(r"^closed\b", re.IGNORECASE)

# A page with fewer words than this gets no sections: "Coming soon!", "TOTAL CREDITS: 30" or a
# bare Zoom link would only add noise to the index (BM25 favours very short passages).
MIN_INDEXED_WORDS = 5

# A section with fewer words than this is merged into the section before it on its page (or
# into the next one when it comes first): "TOTAL CREDITS: 30" stays with its semester map
# instead of becoming a 4-word search result.
MIN_SECTION_WORDS = 5
# Document pages (PDF, Word, Excel, PowerPoint) with fewer words than this ("Page 3", a
# signature line) are not indexed.
MIN_DOCUMENT_WORDS = 5

# drop_repeated_sections: a passage of at least this many words that a website repeats word
# for word is the same passage whatever its heading (the policy's own page has no heading
# for it, the handbook that reprints it uses the policy's name).
REPEAT_TEXT_ONLY_WORDS = 20

# Near-duplicate corpus records (collapse_near_duplicates): the Jaccard similarity of their
# sets of word 5-shingles (title/heading prefix left out) reaches this value.
NEAR_DUP_JACCARD = 0.9
NEAR_DUP_MIN_TOKENS = 8           # shorter passages are never grouped
SHINGLE_SIZE = 5
NEAR_DUP_EXAMPLES = 10            # groups shown in parse_summary.json

_PART_ONLY = re.compile(r"^\(part \d+\)$")   # heading of a long intro's later parts
_DUP_TOKEN = re.compile(r"[^\W_]+")          # words and numbers, any script
# percent-encoded whitespace in a URL: a typo in NYIT's own links ("/%20academics/...")
_URL_WHITESPACE = re.compile(r"%(?:20|09|0a|0d|a0|c2%a0)", re.IGNORECASE)
_LEVEL_ORDER = {"UG": 0, "GR": 1, "PROF": 2}
_NUMBER = re.compile(r"(\d+)(.*)")

ParsedPage = Tuple[PageRecord, List[SectionRecord]]


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def run_parse(settings: Settings, corpus_with_pdf: bool = False,
              corpus_with_docs: bool = True, corpus_with_superseded: bool = False,
              corpus_with_near_duplicates: bool = False) -> dict:
    """Parse every cached page and write the processed datasets. Returns the summary.

    ``corpus_with_superseded`` keeps older editions of yearly documents in the corpus,
    ``corpus_with_near_duplicates`` skips collapse_near_duplicates (every passage stays)."""
    # Imported here (like the CLI does) so that stats/validate can import this module's
    # constants even while a parser module is broken.
    from . import course_parser, site_parser

    timings: Dict[str, float] = {}
    t0 = t = time.perf_counter()
    entries, skipped = select_entries(settings)
    if not entries:
        log.error("no cached pages found (manifest: %s); run `python -m askbear_scraper crawl` "
                  "first", settings.manifest_path)
        return {"generated_at": now_iso(), "pages": 0, "failures": [], "skipped": skipped,
                "error": "no cached pages"}
    hosts = sorted({url_host(page_url) for page_url, _ in entries}, key=site_rank)
    log.info("parsing %d cached pages of %d websites (%s) from %s", len(entries), len(hosts),
             ", ".join(hosts), settings.html_dir)

    # 1) every site except the catalog: learn the blocks that repeat on most of its pages
    boilerplate, failures = learn_boilerplate(settings, entries, site_parser)
    timings["boilerplate"] = _lap(t)

    # 2) every page -> PageRecord + sections (course listings: page only, no sections)
    t = time.perf_counter()
    parsed: List[ParsedPage] = []
    listings: List[Tuple[str, dict]] = []
    seconds_by_site: Counter = Counter()
    for page_url, entry in tqdm(entries, unit="page", desc="parsing pages", dynamic_ncols=True):
        host = url_host(page_url)
        started = time.perf_counter()
        try:
            is_listing = is_catalog_site(host) and course_parser.is_course_listing(page_url)
            if is_listing:
                listings.append((page_url, entry))
            html = read_cached_html(settings, entry)
            page, secs = site_parser.parse_any(html, page_url, entry.get("fetched_at") or "",
                                               entry=entry, boilerplate=boilerplate)
        except Exception as exc:  # one bad page must never stop the run
            failures.append(_failure(page_url, entry, "page", exc))
            continue
        finally:
            seconds_by_site[host] += time.perf_counter() - started
        if (is_not_found_page(page) or is_empty_catalog_template(page)
                or site_parser.soft_404_reason(page.title, page.page_title, page.text)):
            # error pages served with HTTP 200: "404 | ...", empty catalog templates, web
            # server welcome pages, application error pages, placeholders and test pages
            skipped["not_found_page"].append(page_url)
            continue
        secs = [] if is_listing else list(secs or [])
        _complete_page(page, secs, host, entry)
        parsed.append((page, secs))
    timings["pages"] = _lap(t)
    if skipped["not_found_page"]:
        log.warning("skipped 'not found' pages served with HTTP 200: %d, e.g. %s",
                    len(skipped["not_found_page"]), skipped["not_found_page"][0])

    # 3) catalog only: course listing pages don't show the catalog year, some program pages
    #    don't name their school (the generic sites have no catalog year at all)
    t = time.perf_counter()
    pages = [page for page, _ in parsed]
    catalog = [(page, secs) for page, secs in parsed if is_catalog_site(page.site)]
    catalog_year = most_common_catalog_year(page for page, _ in catalog)
    filled = fill_catalog_year(catalog, catalog_year)
    if filled:
        log.info("catalog_year %r filled in on catalog pages that do not show it: %d",
                 catalog_year, filled)
    schools_filled = fill_school_from_program(catalog)
    # the other sites: a degree's sub-pages (admission requirements, semester map...) take
    # the school of the degree (fill_degree_page_schools never changes a catalog page)
    degree_schools_filled = fill_degree_page_schools(parsed)

    # 4) courses from the course directory pages (/courses?cs=CSCI&cal=UG)
    raw_courses: List[CourseRecord] = []
    courses_per_listing: Dict[str, int] = {}
    year_by_url = {page.url: page.catalog_year for page, _ in catalog}
    for page_url, entry in tqdm(listings, unit="page", desc="parsing courses", dynamic_ncols=True):
        try:
            html = read_cached_html(settings, entry)
            found = course_parser.parse_course_listing(
                html, page_url, year_by_url.get(page_url) or catalog_year)
        except Exception as exc:
            failures.append(_failure(page_url, entry, "courses", exc))
            continue
        for course in found:
            if not course.catalog_year:
                course.catalog_year = catalog_year
        courses_per_listing[page_url] = len(found)
        raw_courses.extend(found)
    courses = dedupe_courses(raw_courses)
    timings["courses"] = _lap(t)

    # 5) exact copies (across all sites), then the corpus
    t = time.perf_counter()
    duplicates = mark_duplicates(pages, canonical_urls=declared_canonicals(entries),
                                 sitemap_urls=load_sitemap_urls(settings))
    shared_level = clear_level_of_shared_pages(catalog)
    sections = [s for page, secs in parsed if is_indexed(page) for s in secs]
    sections, tiny_merged = merge_tiny_sections(sections)
    sections, repeated_sections = drop_repeated_sections(sections, pages)
    pdf_pages = load_pdf_pages(settings) if corpus_with_pdf else []
    documents = load_documents(settings) if corpus_with_docs else []
    left_out: Counter = Counter()
    corpus, id_clashes = build_corpus(sections, courses, pdf_pages, documents=documents,
                                      pages=pages, with_superseded=corpus_with_superseded,
                                      left_out=left_out)
    timings["corpus"] = _lap(t)
    t = time.perf_counter()
    near_dups: dict = {"enabled": False}
    if not corpus_with_near_duplicates:
        corpus, dropped, near_dups = collapse_near_duplicates(
            corpus, corpus_bodies(sections, courses, pdf_pages, documents), pages)
        for sec in sections:
            sec.duplicate_of = dropped.get(sec.section_id)
    timings["near_duplicates"] = _lap(t)

    # 6) write everything
    t = time.perf_counter()
    out = settings.processed_dir
    write_jsonl(out / PAGES_FILE, pages)
    write_jsonl(out / SECTIONS_FILE, sections)
    write_jsonl(out / COURSES_FILE, courses)
    csv_written = write_courses_csv(out / COURSES_CSV_FILE, courses)
    write_jsonl(out / CORPUS_FILE, corpus)
    timings["write"] = _lap(t)
    timings["total"] = _lap(t0)

    corpus_by_source = Counter(r.source for r in corpus)
    noindex = sorted(p.url for p in pages if p.noindex)
    by_site = site_summaries(parsed, sections, corpus, failures, boilerplate, seconds_by_site,
                             repeated_sections)
    summary = {
        "generated_at": now_iso(),
        "sites": list(by_site),
        "catalog_year": catalog_year,
        "corpus_with_pdf": corpus_with_pdf,
        "corpus_with_docs": corpus_with_docs,
        "corpus_with_superseded": corpus_with_superseded,
        "corpus_with_near_duplicates": corpus_with_near_duplicates,
        "cached_pages": len(entries),
        "pages": len(pages),
        "distinct_pages": len(pages) - duplicates,
        "duplicate_pages": duplicates,
        "noindex_pages": len(noindex),
        "too_short_to_index": sum(1 for p in pages if not p.duplicate_of and not p.noindex
                                  and (p.word_count or 0) < MIN_INDEXED_WORDS),
        # page 2, 3, ... of lists of posts (blogs ?p=N): no sections, see is_listing_continuation
        "listing_pages_not_indexed": sum(1 for p in pages if not p.duplicate_of
                                         and is_listing_continuation(p.page_type, p.url)),
        "pages_shared_by_levels": shared_level,
        "schools_filled_from_program": schools_filled,
        "degree_page_schools_filled": degree_schools_filled,
        "pages_by_type": dict(Counter(p.page_type for p in pages if not p.duplicate_of)
                              .most_common()),
        "sections": len(sections),
        "tiny_sections_merged": tiny_merged,
        "repeated_sections_removed": repeated_sections,
        "sections_left_out_as_near_duplicates": sum(1 for s in sections if s.duplicate_of),
        "course_listing_pages": len(listings),
        "empty_course_listings": sorted(u for u, n in courses_per_listing.items() if n == 0),
        "courses_parsed": len(raw_courses),
        "courses": len(courses),
        "duplicate_courses_removed": len(raw_courses) - len(courses),
        "course_subjects": len({c.subject for c in courses}),
        "courses_not_in_corpus": sum(1 for c in courses if not course_in_corpus(c)),
        "course_summary": course_parser.summarize_courses(courses),
        "document_pages_read": len(documents),
        "document_pages_repeated": len(repeated_document_pages(documents)),
        "document_pages_superseded": sum(1 for d in documents if d.get("superseded")),
        # document pages not in the corpus, by reason (empty, too_short, repeated,
        # superseded, site_not_crawled); near-duplicates are under "near_duplicates"
        "document_pages_left_out": dict(sorted(left_out.items())),
        "documents_in_corpus": corpus_by_source.get("document", 0),
        "documents_in_corpus_pdfs": len({r.metadata.get("pdf_url") for r in corpus
                                         if r.source == "document"}),
        "corpus": len(corpus),
        "corpus_by_source": dict(corpus_by_source),
        "corpus_by_site": dict(Counter(str(r.metadata.get("site") or "") for r in corpus)
                               .most_common()),
        "corpus_id_clashes": id_clashes,
        "near_duplicates": near_dups,
        "courses_csv_written": csv_written,
        "by_site": by_site,
        "boilerplate_blocks": {h: len(boilerplate.get(h) or ()) for h in hosts
                               if uses_boilerplate(h)},
        "boilerplate_examples": {h: boilerplate_examples(boilerplate[h]) for h in hosts
                                 if boilerplate.get(h)},
        "noindex": noindex,
        "failures": failures,
        "skipped": skipped,
        "timings_seconds": timings,
    }
    write_json(out / PARSE_SUMMARY_FILE, summary)
    _log_summary(summary, out)
    return summary


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------
def select_entries(settings: Settings) -> Tuple[List[Tuple[str, dict]], Dict[str, List[str]]]:
    """Manifest entries to parse, as ``(page_url, entry)`` sorted by page URL.

    Only entries with status "ok" whose cache file exists are used. ``page_url`` is where
    the content really lives (the redirect target if the URL redirected), and its host
    decides which parser reads it: https://nyit.edu/admissions redirects to
    https://www.nyit.edu/admissions/ and is parsed as a www.nyit.edu page. An entry whose
    content ended up outside the crawl scope of sites.py is skipped: a redirect to a login
    portal or another organisation ("offsite_redirect"), or a page of a website or path that
    sites.py does not crawl (any more), e.g. cached by an earlier crawl ("out_of_scope"). When
    two URLs end up at the same page, the one that was fetched directly wins.
    """
    skipped: Dict[str, List[str]] = {"missing_cache_file": [], "offsite_redirect": [],
                                     "out_of_scope": [], "same_page_as_other_url": [],
                                     "not_found_page": []}
    candidates = []
    for url, entry in load_manifest(settings.manifest_path).items():
        if entry.get("status") != "ok":
            continue
        fname = entry.get("file")
        if not fname or not (settings.html_dir / fname).is_file():
            skipped["missing_cache_file"].append(url)
            continue
        page_url = normalize_url(entry.get("final_url") or url) or url
        if not is_in_scope(page_url):
            skipped[skip_reason(url, page_url)].append(url)
            continue
        candidates.append((page_url, page_url != url, url, entry))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    chosen: List[Tuple[str, dict]] = []
    taken = set()
    for page_url, _, url, entry in candidates:
        if page_url in taken:
            skipped["same_page_as_other_url"].append(url)
            continue
        taken.add(page_url)
        chosen.append((page_url, entry))
    for reason, urls in skipped.items():
        if urls:
            log.warning("skipped manifest entries (%s): %d, e.g. %s", reason, len(urls), urls[0])
    return chosen, skipped


def read_cached_html(settings: Settings, entry: dict) -> str:
    return (settings.html_dir / entry["file"]).read_text(encoding="utf-8", errors="replace")


def load_pdf_pages(settings: Settings) -> List[dict]:
    """PDF page records written by the ``pdf`` step (empty list if it has not run)."""
    path = settings.processed_dir / PDF_PAGES_FILE
    if not path.exists():
        log.warning("--corpus-with-pdf: %s not found; run `python -m askbear_scraper pdf` "
                    "first. The corpus will contain no PDF pages.", path)
        return []
    return list(read_jsonl(path))


def load_documents(settings: Settings) -> List[dict]:
    """Page records of the documents (PDF, Word, Excel, PowerPoint) found on NYIT sites
    (written by ``docs``).

    documents.jsonl written by an older version has no doc_category/edition/superseded
    fields: they are worked out here from the file names (documents.annotate_page_records),
    so the corpus gets them without re-running ``docs``."""
    path = settings.processed_dir / DOCUMENTS_FILE
    if not path.exists():
        log.info("no %s yet: the corpus has no documents (run `python -m askbear_scraper "
                 "docs`, then `parse` again, to add them)", path)
        return []
    records = list(read_jsonl(path))
    if records and any("superseded" not in r or "doc_category" not in r for r in records):
        from .documents import annotate_page_records  # see run_parse

        log.info("%s has no edition/category fields (written by an older version): adding "
                 "them from the file names; run `python -m askbear_scraper docs` to refresh "
                 "the file", path.name)
        annotate_page_records(records)
    return records


def load_sitemap_urls(settings: Settings) -> Set[str]:
    """Every page URL listed in the cached sitemaps (data/raw/sitemaps/<host>.json),
    normalised like the crawl."""
    urls: Set[str] = set()
    folder = settings.raw_dir / "sitemaps"
    if not folder.is_dir():
        return urls
    for path in sorted(folder.glob("*.json")):
        data = read_json_file(path)
        for item in (data.get("pages") if isinstance(data, dict) else None) or []:
            loc = item[0] if isinstance(item, (list, tuple)) and item else item
            url = normalize_url(loc) if isinstance(loc, str) else None
            if url:
                urls.add(url)
    return urls


def declared_canonicals(entries: Iterable[Tuple[str, dict]]) -> Dict[str, str]:
    """``{page_url: the rel=canonical URL the page declares}`` (normalised like the crawl),
    from the crawl manifest."""
    out: Dict[str, str] = {}
    for page_url, entry in entries:
        canonical = normalize_url(str(entry.get("canonical") or "")) if entry.get("canonical") \
            else None
        if canonical:
            out[page_url] = canonical
    return out


# ---------------------------------------------------------------------------
# websites
# ---------------------------------------------------------------------------
def site_rank(host: str) -> Tuple[int, str]:
    """Sort key for websites: catalog.nyit.edu, www.nyit.edu, then the others alphabetically."""
    host = host or ""
    if host in SITE_PRIORITY:
        return SITE_PRIORITY.index(host), ""
    return len(SITE_PRIORITY), host


def is_parsed_site(host: str) -> bool:
    """True for an NYIT website that is crawled (sites.py), i.e. whose pages are parsed."""
    profile = sites.profile_for(host)
    return profile is not None and profile.enabled


def is_in_scope(page_url: str) -> bool:
    """True when ``page_url`` is a page that sites.py lets the crawler visit (enabled website,
    allowed path). The catalog's /print/ pages count too: they are only in the cache when
    the crawl was run with --include-print."""
    return link_kind(page_url, include_print=True) == "page"


def skip_reason(url: str, page_url: str) -> str:
    """Why an out-of-scope manifest entry is not parsed (see select_entries)."""
    profile = sites.profile_for(url_host(page_url))
    if profile is None or (not profile.enabled and url_host(page_url) != url_host(url)):
        return "offsite_redirect"
    return "out_of_scope"


def is_catalog_site(host: str) -> bool:
    profile = sites.profile_for(host or "")
    return profile is not None and profile.parser == "catalog"


def uses_boilerplate(host: str) -> bool:
    """Boilerplate removal is for the generic sites only: the catalog's repeated blocks
    (curriculum rows, requirement tables) are real content."""
    profile = sites.profile_for(host or "")
    return profile is not None and profile.parser != "catalog"


def is_not_found_page(page: PageRecord) -> bool:
    """A short page titled "404" / "Page not found": an error page served with HTTP 200."""
    return looks_like_not_found(page.title, page.page_title, page.word_count)


def looks_like_not_found(title: Optional[str], page_title: Optional[str],
                         words: Optional[int], text: Optional[str] = None) -> bool:
    """See is_not_found_page (also used by validate.py on pages.jsonl records). With
    ``text``, the other error pages that `parse` skips count too: web-server default pages,
    application error pages, placeholders and test pages (site_parser.soft_404_reason)."""
    if text is not None and _soft_404_reason(title, page_title, text):
        return True
    if (words or 0) >= NOT_FOUND_MAX_WORDS:
        return False
    return bool(_NOT_FOUND_TITLE.match(title or "") or _NOT_FOUND_TITLE.match(page_title or ""))


def _soft_404_reason(title: Optional[str], page_title: Optional[str],
                     text: Optional[str]) -> Optional[str]:
    """site_parser.soft_404_reason, imported lazily so that validate.py still works while
    a parser module is broken (it then returns None)."""
    try:
        from .site_parser import soft_404_reason
    except ImportError:  # pragma: no cover - only when the parser itself is broken
        return None
    return soft_404_reason(title or "", page_title or "", text or "")


def is_empty_catalog_template(page: PageRecord) -> bool:
    """An empty catalog program template (see _DEGREE_TEMPLATE_PATH): a broken link, really."""
    return (is_catalog_site(page.site or url_host(page.url))
            and bool(_DEGREE_TEMPLATE_PATH.match(url_path(page.url)))
            and (not page.program or bool(_CLOSED_PROGRAM.match(page.program)))
            and (page.word_count or 0) < MIN_INDEXED_WORDS)


def is_indexed(page: PageRecord) -> bool:
    """Distinct pages with some text that do not ask search engines to leave them out get
    sections (and corpus records); so do the first pages of listings, not their page 2, 3...
    (is_listing_continuation)."""
    return (not page.duplicate_of and not page.noindex
            and (page.word_count or 0) >= MIN_INDEXED_WORDS
            and not is_listing_continuation(page.page_type, page.url))


# query parameters that number the pages of a list of posts: blogs.nyit.edu/<blog>?p=2,
# online.nyit.edu/blog?p=3 (not www's ?fsa_page=N: those pages hold the accomplishments)
_LISTING_PAGE_PARAM = re.compile(r"^(?:p|paged|page|pg)$", re.IGNORECASE)


def listing_page_number(url: str) -> int:
    """Which page of a list of posts ``url`` shows (1 when the URL names none; like
    site_parser.page_number, only when the page number is the URL's one query parameter)."""
    params = parse_qsl(urlsplit(url or "").query)
    if len(params) == 1:
        name, value = params[0]
        if _LISTING_PAGE_PARAM.match(name) and value.isdigit():
            return max(int(value), 1)
    return 1


def is_listing_continuation(page_type: Optional[str], url: str) -> bool:
    """Page 2, 3, ... of a list of posts (page_type "listing", ``?p=N``): it only repeats
    the opening lines of posts that are pages of their own, so it stays in pages.jsonl
    but gets no sections or corpus records. Used by validate.py too."""
    return page_type == "listing" and listing_page_number(url) > 1


def _complete_page(page: PageRecord, secs: Sequence[SectionRecord], host: str,
                   entry: dict) -> None:
    """Every record names its website, a page the crawler saw as noindex stays noindex, and
    every section carries its page's campuses (so the corpus can be filtered by campus)."""
    if not page.site:
        page.site = host
    if entry.get("noindex"):
        page.noindex = True
    for sec in secs:
        if not sec.site:
            sec.site = page.site
        if not getattr(sec, "campuses", None):
            sec.campuses = list(page.campuses or [])


def learn_boilerplate(settings: Settings, entries: Sequence[Tuple[str, dict]],
                      site_parser) -> Tuple[Dict[str, Set[str]], List[dict]]:
    """First pass over every generic site: ``site_parser.find_boilerplate`` on its pages.

    Returns ``({host: {normalised block text}}, failures)``. Pages are read from disk one at
    a time (never all at once); a site whose boilerplate cannot be learned is parsed without
    boilerplate removal.
    """
    by_site: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
    for page_url, entry in entries:
        host = url_host(page_url)
        if uses_boilerplate(host):
            by_site[host].append((page_url, entry))
    boilerplate: Dict[str, Set[str]] = {}
    failures: List[dict] = []
    total = sum(len(v) for v in by_site.values())
    if not total:
        return boilerplate, failures
    bar = tqdm(total=total, unit="page", desc="finding boilerplate", dynamic_ncols=True)
    try:
        for host in sorted(by_site, key=site_rank):
            try:
                found = site_parser.find_boilerplate(_cached_pages(settings, by_site[host], bar))
            except Exception as exc:  # parse this site without boilerplate removal
                log.warning("could not learn the repeated blocks of %s (%s: %s); its pages are "
                            "parsed without boilerplate removal", host, type(exc).__name__, exc)
                log.debug("traceback", exc_info=True)
                failures.append({"url": f"https://{host}/", "file": None, "site": host,
                                 "stage": "boilerplate", "error": f"{type(exc).__name__}: {exc}"})
                continue
            for site, blocks in (found or {}).items():
                if uses_boilerplate(site):
                    boilerplate.setdefault(site, set()).update(blocks or ())
    finally:
        bar.close()
    for host in sorted(boilerplate, key=site_rank):
        log.info("%s: %d repeated blocks (on >= 30%% of its pages) are left out of the text",
                 host, len(boilerplate[host]))
    return boilerplate, failures


def merge_tiny_sections(sections: Sequence[SectionRecord], min_words: int = MIN_SECTION_WORDS
                        ) -> Tuple[List[SectionRecord], int]:
    """Merge every section with fewer than ``min_words`` words into the section before it on
    the same page (its heading becomes a line of that section's text), or into the next
    section when it is the page's first one. "TOTAL CREDITS: 30" stays with its semester
    map, "Updated: 2026-09-01" or "Yes." do not become search results of their own. A page
    whose only section is short keeps it. Returns (sections in order, number merged)."""
    by_page: Dict[str, List[SectionRecord]] = defaultdict(list)
    for sec in sections:
        by_page[sec.doc_id].append(sec)
    merged: Set[str] = set()
    for secs in by_page.values():
        if len(secs) < 2:
            continue
        kept: List[SectionRecord] = []
        pending: List[SectionRecord] = []    # short sections before the first real one
        for sec in secs:
            if word_count(sec.text or "") >= min_words:
                if pending:
                    _prepend_text(sec, [line for p in pending for line in _tiny_lines(p)])
                    pending = []
                kept.append(sec)
            elif kept:
                merged.add(sec.section_id)
                _append_text(kept[-1], _tiny_lines(sec))
            else:
                merged.add(sec.section_id)
                pending.append(sec)
        if pending:                          # every section of the page is short: keep one
            first, rest = pending[0], pending[1:]
            merged.discard(first.section_id)
            _append_text(first, [line for p in rest for line in _tiny_lines(p)])
    if merged:
        log.info("sections with fewer than %d words merged into a neighbouring section of their "
                 "page: %d", min_words, len(merged))
    return [s for s in sections if s.section_id not in merged], len(merged)


def _tiny_lines(sec: SectionRecord) -> List[str]:
    """The lines a short section adds to its neighbour: its heading (unless it only says
    "(part N)") and its text."""
    heading = (sec.heading or "").strip()
    lines = [heading] if heading and not _PART_ONLY.match(heading) else []
    if (sec.text or "").strip():
        lines.append(sec.text.strip())
    return lines


def _append_text(sec: SectionRecord, lines: List[str]) -> None:
    if lines:
        sec.text = "\n".join([sec.text.rstrip()] + lines) if (sec.text or "").strip() \
            else "\n".join(lines)
        sec.word_count = word_count(sec.text)


def _prepend_text(sec: SectionRecord, lines: List[str]) -> None:
    sec.text = "\n".join(lines + [sec.text.lstrip()]) if (sec.text or "").strip() \
        else "\n".join(lines)
    sec.word_count = word_count(sec.text)


def drop_repeated_sections(sections: Sequence[SectionRecord], pages: Iterable[PageRecord]
                           ) -> Tuple[List[SectionRecord], Dict[str, int]]:
    """Keep only one copy of a passage that several pages of the same website repeat word
    for word (catalog.nyit.edu excepted, like the boilerplate rule; collapse_near_duplicates
    covers the catalog).

    apps.nyit.edu's handbooks (/policies/collection/...) reprint whole policies that also
    have their own page; LibGuides repeats the same librarian box and "Quick Info!" links
    on dozens of guides; www.nyit.edu's transfer-credit pages share their AP/IB sections.
    In the index those copies would fill the top results with one answer under several
    links. A passage of REPEAT_TEXT_ONLY_WORDS words or more is the same passage under any
    heading (the policy's own page shows it without a heading, the handbook under the
    policy's name); a shorter one also needs the same heading, where a missing heading or
    "(part N)" stands for the page title (+ the part). Passages of different programs or
    levels are never merged (two degrees can have the same admission requirements, and the
    page title in the corpus text tells them apart). The copy kept is on the most specific
    page: not a home/listing page, then the shortest page (the policy's own page rather than
    the handbook), then the URL. Returns (sections in their original order, {site: copies
    removed}). Before all that, the copy kept is not on page 2, 3... of a paginated listing
    (the accomplishments intro stays on /accomplishments/, not ?fsa_page=106), and it is on
    the page whose title has the most words of the passage's heading (a LibGuides box
    stays on the guide it is about, not on the shortest guide that reuses it). Excerpts
    of posts on a blog's list of posts go too (listing_excerpts).
    """
    from . import site_parser   # (imported here, see run_parse)

    page_by_id = {p.doc_id: p for p in pages}

    def rank(sec: SectionRecord) -> tuple:
        page = page_by_id.get(sec.doc_id)
        hub = page is not None and page.page_type in ("home", "listing")
        later_page = site_parser.page_number(page.url if page is not None else sec.url) > 1
        return (later_page, -_title_match(sec.heading, page.title if page is not None else ""),
                hub, page.word_count if page is not None else 0, sec.url, sec.section_id)

    groups: Dict[tuple, List[SectionRecord]] = defaultdict(list)
    for sec in sections:
        if uses_boilerplate(sec.site or url_host(sec.url)):
            text = _norm_key(sec.text)
            heading = "" if len(text.split()) >= REPEAT_TEXT_ONLY_WORDS else \
                _norm_key(effective_heading(sec))
            groups[(sec.site, sec.program, sec.level, heading, text)].append(sec)
    drop: Set[str] = set()
    removed: Counter = Counter()
    for copies in groups.values():
        if len(copies) > 1:
            for sec in sorted(copies, key=rank)[1:]:
                drop.add(sec.section_id)
                removed[sec.site] += 1
    for sec in listing_excerpts([s for s in sections if s.section_id not in drop], page_by_id):
        drop.add(sec.section_id)
        removed[sec.site] += 1
    if drop:
        log.info("passages repeated word for word on several pages of a website, kept once: "
                 "%d copies removed (%s)", len(drop),
                 ", ".join(f"{h} {n}" for h, n in sorted(removed.items(),
                                                          key=lambda kv: site_rank(kv[0]))))
    kept = [s for s in sections if s.section_id not in drop]
    return kept, {h: removed[h] for h in sorted(removed, key=site_rank)}


# a passage of a list of posts that is this share of a post's text is an excerpt of it
LISTING_EXCERPT_SHARE = 0.8


def listing_excerpts(sections: Sequence[SectionRecord], page_by_id: Dict[str, PageRecord]
                     ) -> List[SectionRecord]:
    """The passages of a list of posts (page_type "listing", a blog's first page) whose
    word 5-shingles are, for LISTING_EXCERPT_SHARE or more, in one passage of a post the
    list links to: the opening lines of a post that is a page of its own. The near-duplicate
    step misses them (an excerpt is much shorter than its post)."""
    listing = [s for s in sections if getattr(page_by_id.get(s.doc_id), "page_type", "") == "listing"]
    if not listing:
        return []
    linked: Dict[str, Set[str]] = defaultdict(set)   # post URL -> listing doc_ids
    for sec in listing:
        for link in page_by_id[sec.doc_id].links:
            linked[normalize_url(link.get("url")) or ""].add(sec.doc_id)
    listing_ids = {s.doc_id for s in listing}
    posts = [s for s in sections if s.url.split("#")[0] in linked and s.doc_id not in listing_ids]
    sets = shingle_sets([s.text for s in listing + posts], min_tokens=2 * SHINGLE_SIZE)
    by_shingle: Dict[int, List[int]] = defaultdict(list)    # shingle -> posts that have it
    for j, post in enumerate(posts):
        for h in sets[len(listing) + j] or ():
            by_shingle[h].append(j)
    out = []
    for i, sec in enumerate(listing):
        mine = sets[i]
        if mine is None:
            continue
        hits = Counter(j for h in mine for j in by_shingle.get(h, ())
                       if sec.doc_id in linked[posts[j].url.split("#")[0]])
        if hits and hits.most_common(1)[0][1] >= LISTING_EXCERPT_SHARE * len(mine):
            out.append(sec)
    return out


_TITLE_WORD = re.compile(r"[^\W_]{3,}")


def _title_match(heading: Optional[str], title: Optional[str]) -> float:
    """Share of the words (3+ letters) of a passage's own heading that its page title has:
    "Mobile Medical Apps" on "Mobile Medical Apps: Home" 1.0, on "Occupational Therapy:
    Mobile Apps" 0.67; 0 without a heading."""
    words = set(_TITLE_WORD.findall((heading or "").casefold()))
    if not words:
        return 0.0
    return len(words & set(_TITLE_WORD.findall((title or "").casefold()))) / len(words)


def _norm_key(text: Optional[str]) -> str:
    return " ".join((text or "").casefold().split())


def effective_heading(sec: SectionRecord) -> str:
    """The heading a passage is known by: its own heading, or the page title when it has none
    (the intro of a policy page) or only says "(part N)" ("<page title> (part N)")."""
    heading = (sec.heading or "").strip()
    if heading and not _PART_ONLY.match(heading):
        return heading
    return " ".join(p for p in ((sec.page_title or "").strip(), heading) if p)


def boilerplate_examples(blocks: Iterable[str], limit: int = BOILERPLATE_EXAMPLES,
                         width: int = 160) -> List[str]:
    """The longest repeated blocks of a site, shortened, for parse_summary.json."""
    longest = sorted(blocks, key=lambda b: (-len(b), b))[:limit]
    return [b if len(b) <= width else b[:width - 3].rstrip() + "..." for b in longest]


def _cached_pages(settings: Settings, site_entries: Sequence[Tuple[str, dict]],
                  bar) -> Iterator[Tuple[str, str]]:
    for page_url, entry in site_entries:
        bar.update(1)
        try:
            html = read_cached_html(settings, entry)
        except OSError as exc:  # reported as a failure by the parsing pass
            log.debug("cannot read %s: %s", entry.get("file"), exc)
            continue
        yield page_url, html


# ---------------------------------------------------------------------------
# post-processing
# ---------------------------------------------------------------------------
def most_common_catalog_year(pages: Iterable[PageRecord]) -> str:
    counts = Counter(p.catalog_year for p in pages if p.catalog_year)
    if not counts:
        log.warning("no catalog page shows a catalog year; catalog_year stays empty")
        return ""
    if len(counts) > 1:
        log.info("catalog years found on pages: %s", dict(counts.most_common()))
    return counts.most_common(1)[0][0]


def fill_catalog_year(parsed: List[ParsedPage], catalog_year: str) -> int:
    """Give pages (and their sections) without a catalog year the common one.

    Pass catalog pages only: the other websites have no catalog year."""
    filled = 0
    if not catalog_year:
        return filled
    for page, secs in parsed:
        if not page.catalog_year:
            page.catalog_year = catalog_year
            filled += 1
        for sec in secs:
            if not sec.catalog_year:
                sec.catalog_year = page.catalog_year
    return filled


def duplicate_rank(page: PageRecord, declared: Iterable[str] = (),
                   listed: Iterable[str] = ()) -> tuple:
    """Which of several identical pages is the canonical copy (smallest first): a page that
    may be indexed, then the site priority (SITE_PRIORITY), then a page of the catalog's own
    navigation over its copy in the "Academics > Degrees" template (/curriculum/<slug>, linked
    from www.nyit.edu: /engineering/computer_science/curriculum keeps its school breadcrumbs
    and anchors), then the URL that the copies name as rel=canonical (``declared``), then a
    URL listed in the site's sitemap (``listed``), then the cleanest URL: no percent-encoded
    spaces (NYIT's own link typos such as /%20academics/...), fewest %-escapes, all lower
    case (/bio/anu-raj/ rather than /bio/Anu.Raj), fewest query parameters, then
    alphabetical."""
    host = page.site or url_host(page.url)
    template_copy = is_catalog_site(host) and bool(_DEGREE_TEMPLATE_PATH.match(url_path(page.url)))
    url = page.url
    path = urlsplit(url).path
    return (bool(page.noindex), site_rank(host), template_copy, url not in declared,
            url not in listed, bool(_URL_WHITESPACE.search(url)), path.count("%"),
            path != path.lower(), _query_size(url), url)


def mark_duplicates(pages: List[PageRecord], min_words: int = MIN_DUPLICATE_WORDS,
                    canonical_urls: Optional[Dict[str, str]] = None,
                    sitemap_urls: Iterable[str] = ()) -> int:
    """Set ``duplicate_of`` on pages whose text exactly equals another page's, on any site.

    The canonical copy is chosen by :func:`duplicate_rank`: a catalog page wins over its
    copy on www.nyit.edu, /courses?cal=GR&cs=PHAS over /courses?cal=GR&cc=PHAS-602&cs=PHAS,
    /financial_aid_graduate/x over /financial_aid_undergraduate/x, and a page that search
    engines may index over a "noindex" copy. Among copies on one website, the URL that one of
    them declares as rel=canonical wins (``canonical_urls``: ``{page url: canonical URL}``,
    from the crawl manifest; a canonical pointing outside the group, like the apps.nyit.edu
    policies that all name https://www.nyit.edu/, is ignored), then a URL of the site's
    sitemap (``sitemap_urls``), then the cleanest URL. Returns the number of duplicates.
    """
    canonical_urls = canonical_urls or {}
    listed = set(sitemap_urls)
    groups: Dict[str, List[PageRecord]] = defaultdict(list)
    for page in pages:
        page.duplicate_of = None
        if page.word_count >= min_words:
            groups[page.content_sha1 or sha1(page.text)].append(page)
    count = 0
    for copies in groups.values():
        if len(copies) < 2:
            continue
        urls = {p.url for p in copies}
        declared = {canonical_urls[p.url] for p in copies if p.url in canonical_urls} & urls
        best = min(copies, key=lambda p: duplicate_rank(p, declared, listed))
        for page in copies:
            if page is not best:
                page.duplicate_of = best.url
                count += 1
    return count


def clear_level_of_shared_pages(parsed: List[ParsedPage]) -> int:
    """A canonical page whose exact copies are filed under another level applies to both.

    NYIT publishes some financial-aid pages twice with the same text, e.g.
    /financial_aid_graduate/federal_perkins_loans and /financial_aid_undergraduate/
    federal_perkins_loans. Only the canonical copy keeps its sections, so it (and its
    sections) gets level None ("not level-specific") instead of "graduate". Returns the
    number of pages changed. Call after mark_duplicates, with the catalog pages only.
    """
    copy_levels: Dict[str, set] = {}
    for page, _ in parsed:
        # a copy whose level is unknown (the /curriculum/<slug> template pages often do not
        # say) is no evidence that the page applies to another level
        if page.duplicate_of and page.level is not None:
            copy_levels.setdefault(page.duplicate_of, set()).add(page.level)
    changed = 0
    for page, secs in parsed:
        if page.duplicate_of or page.level is None:
            continue
        if copy_levels.get(page.url, {page.level}) - {page.level}:
            page.level = None
            for sec in secs:
                sec.level = None
            changed += 1
    if changed:
        log.info("pages whose exact copies belong to another level (level set to None): %d",
                 changed)
    return changed


def fill_school_from_program(parsed: List[ParsedPage]) -> int:
    """Pages that name a program but no school get the school of that program's other pages.

    /semester-map/physician-assistant-studies-ms and /curriculum/physician-assistant-studies-ms
    use another site template (breadcrumbs "Academics > Degrees > ..."), so neither their URL
    nor their breadcrumbs name the school; /health_professions/physician_assistant_studies does.
    The program name is compared as written first, then without its "Doctor of" /
    "Bachelor of" / "Master of" prefix and punctuation ("Doctor of Physical Therapy, D.P.T."
    on /curriculum/physical-therapy-dpt = "Physical Therapy, D.P.T." on the program page).
    Returns the number of pages filled in. Pass catalog pages only.
    """
    schools: Dict[str, set] = {}
    loose: Dict[str, set] = {}
    for page, _ in parsed:
        if page.program and page.school:
            schools.setdefault(page.program, set()).add(page.school)
            loose.setdefault(_program_key(page.program), set()).add(page.school)
    filled = 0
    for page, secs in parsed:
        if page.school or not page.program:
            continue
        known = schools.get(page.program) or loose.get(_program_key(page.program))
        if not known or len(known) != 1:
            continue
        page.school = next(iter(known))
        for sec in secs:
            sec.school = page.school
        filled += 1
    if filled:
        log.info("school filled in from the program's other pages: %d", filled)
    return filled


DEGREE_PAGE_TYPES = ("degree", "curriculum")


def fill_degree_page_schools(parsed: List[ParsedPage]) -> int:
    """Degree pages of the other websites that do not name their school get it from the
    degree they belong to.

    On www.nyit.edu only a degree's landing page (/academics/degrees/computer-science-bs/)
    names the school (through its "Related Links"); its sub-pages (.../admission-requirements/,
    .../semester-map/) do not. Such a page takes the school of the nearest landing page above
    it in the URL path, else of the other pages of its program on the same website, else of the
    catalog's pages of that program -- only when those agree on one school. Catalog pages are
    left alone (see fill_school_from_program). Returns the number of pages filled in.
    """
    landing: Dict[str, str] = {}
    same_site: Dict[Tuple[str, str], set] = defaultdict(set)
    catalog: Dict[str, set] = defaultdict(set)
    for page, _ in parsed:
        if not page.school:
            continue
        if is_catalog_site(page.site):
            if page.program:
                catalog[_loose_program_key(page.program)].add(page.school)
            continue
        if page.page_type in DEGREE_PAGE_TYPES:
            landing[page.url] = page.school
            if page.program:
                same_site[(page.site, _loose_program_key(page.program))].add(page.school)
    filled = 0
    for page, secs in parsed:
        if page.school or is_catalog_site(page.site) or page.page_type not in DEGREE_PAGE_TYPES:
            continue
        school = _ancestor_value(page.url, landing)
        if not school and page.program:
            key = _loose_program_key(page.program)
            for found in (same_site.get((page.site, key)), catalog.get(key)):
                if found and len(found) == 1:
                    school = next(iter(found))
                    break
        if school:
            page.school = school
            for sec in secs:
                sec.school = school
            filled += 1
    if filled:
        log.info("school filled in on degree pages of other websites from their degree: %d", filled)
    return filled


def _ancestor_value(url: str, values: Dict[str, str]) -> Optional[str]:
    """The value of the closest page above ``url`` in its path (normalised like the crawl)."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    for i in range(len(segs) - 1, 0, -1):
        parent = normalize_url(f"{parts.scheme}://{parts.netloc}/{'/'.join(segs[:i])}")
        if parent and parent in values:
            return values[parent]
    return None


def _loose_program_key(program: str) -> str:
    """_program_key that also matches "&" with "and" (www: "Electrical & Computer
    Engineering, M.S."; catalog: "Electrical and Computer Engineering, M.S.")."""
    return _program_key(re.sub(r"\s*&\s*", " and ", program))


_DEGREE_PREFIX = re.compile(r"^(doctor|bachelor|master)(ate)?\s+(of|in)\s+", re.IGNORECASE)


def _program_key(program: str) -> str:
    """"Doctor of Physical Therapy, D.P.T." -> "physical therapy d p t"."""
    name = _DEGREE_PREFIX.sub("", program.strip())
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def course_in_corpus(course) -> bool:
    """False for the legacy "Conversion" course codes (see LEGACY_SUBJECT_NAME), which are
    kept in courses.jsonl but not indexed. Accepts a CourseRecord or a courses.jsonl dict."""
    if isinstance(course, dict):
        subject_name, description = course.get("subject_name"), course.get("description")
    else:
        subject_name, description = course.subject_name, course.description
    return not (LEGACY_SUBJECT_NAME.match(subject_name or "") and not (description or "").strip())


def dedupe_courses(courses: Iterable[CourseRecord]) -> List[CourseRecord]:
    """One record per course_id, sorted by code.

    A course is often listed on several subject pages (ARCH courses also appear on the AAID
    page, for example). The record with the longest description wins; on a tie, the one from
    the course's own subject page, because that is the better link for a search result.
    """
    best: Dict[str, CourseRecord] = {}
    for course in courses:
        current = best.get(course.course_id)
        if current is None or _course_rank(course) > _course_rank(current):
            best[course.course_id] = course
    return sorted(best.values(), key=course_sort_key)


def _course_rank(course: CourseRecord) -> Tuple[int, bool, int]:
    """Higher is better: longest description, own subject page, simplest listing URL."""
    listed_under = dict(parse_qsl(urlsplit(course.url).query)).get("cs", "")
    return (len(course.description or ""),
            listed_under.upper() == (course.subject or "").upper(),
            -_query_size(course.url))


def _query_size(url: str) -> int:
    return len(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def course_sort_key(course: CourseRecord) -> tuple:
    """Subject, then number in numeric order (CSCI 90 < CSCI 110 < CSCI 110L), then level."""
    m = _NUMBER.match(course.number or "")
    number = (int(m.group(1)), m.group(2)) if m else (10 ** 9, course.number or "")
    return (course.subject or "", number, _LEVEL_ORDER.get(course.level_code, 9),
            course.course_id or "")


# ---------------------------------------------------------------------------
# corpus (the search engine's input)
# ---------------------------------------------------------------------------
def repeated_document_pages(documents: Iterable[dict]) -> Set[str]:
    """doc_ids of document pages whose text another document repeats word for word.

    The same page often appears in several PDFs: one report uploaded twice for the B.Arch.
    and the M.Arch., the unchanged pages of each year's student handbook, a form's
    signature page. Only one copy goes into the corpus: the one of a current edition (not
    ``superseded``), then of the most recently modified PDF (then the first by URL and
    page). documents.jsonl keeps every page.
    """
    groups: Dict[str, List[dict]] = defaultdict(list)
    for rec in documents:
        key = _norm_key(rec.get("text"))
        if key and rec.get("doc_id"):
            groups[key].append(rec)
    repeated: Set[str] = set()
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda r: (str(r.get("pdf_url") or ""), r.get("page") or 0))
        keep = max(copies, key=lambda r: (not r.get("superseded"), str(r.get("modified") or "")))
        repeated.update(r["doc_id"] for r in copies if r is not keep)
    return repeated


def build_corpus(sections: Iterable[SectionRecord], courses: Iterable[CourseRecord],
                 pdf_pages: Iterable[dict] = (), documents: Iterable[dict] = (),
                 pages: Optional[Iterable[PageRecord]] = None, with_superseded: bool = False,
                 left_out: Optional[Counter] = None) -> Tuple[List[CorpusRecord], int]:
    """One CorpusRecord per section, course, document page with text and (optionally)
    non-empty catalog PDF page.

    Sections of CORPUS_EXCLUDED_PAGE_TYPES, sections marked ``duplicate_of``, legacy course
    codes (course_in_corpus) and document pages that are empty, shorter than
    MIN_DOCUMENT_WORDS words, repeat another document's page word for word
    (repeated_document_pages) or belong to an older edition (``superseded``, unless
    ``with_superseded``) are left out; ``left_out`` (a Counter) receives the number of
    document pages left out per reason. ``pages`` (optional) adds each page's publication
    dates to its sections.

    Returns the records and the number of records dropped because their id was taken.
    """
    records: List[CorpusRecord] = []
    seen = set()
    documents = list(documents)
    repeated_docs = repeated_document_pages(documents)
    if repeated_docs:
        log.info("document pages that another document repeats word for word, kept once in "
                 "the corpus: %d copies left out", len(repeated_docs))
    left_out = left_out if left_out is not None else Counter()
    clashes = 0
    page_by_id = {p.doc_id: p for p in pages or ()}

    def add(rec: CorpusRecord) -> None:
        nonlocal clashes
        if rec.id in seen:
            clashes += 1
            log.warning("corpus id %s appears twice; keeping the first one", rec.id)
            return
        seen.add(rec.id)
        records.append(rec)

    for sec in sections:
        if (sec.page_type not in CORPUS_EXCLUDED_PAGE_TYPES and (sec.text or "").strip()
                and not getattr(sec, "duplicate_of", None)):
            add(section_to_corpus(sec, page_by_id.get(sec.doc_id)))
    for course in courses:
        if course_in_corpus(course):
            add(course_to_corpus(course))
    for doc_page in documents:
        reason = document_left_out(doc_page, repeated_docs, with_superseded)
        if reason:
            left_out[reason] += 1
            continue
        add(document_to_corpus(doc_page))
    if left_out.get("site_not_crawled"):
        log.warning("document pages of websites that are not crawled (any more) left out of the "
                    "corpus: %d; run `python -m askbear_scraper docs` again",
                    left_out["site_not_crawled"])
    if left_out.get("superseded"):
        log.info("pages of older editions of documents left out of the corpus: %d (add them "
                 "with `parse --corpus-with-superseded`)", left_out["superseded"])
    if left_out.get("too_short"):
        log.info("document pages with fewer than %d words left out of the corpus: %d",
                 MIN_DOCUMENT_WORDS, left_out["too_short"])
    for pdf_page in pdf_pages:
        if (pdf_page.get("text") or "").strip():
            add(pdf_page_to_corpus(pdf_page))
    return records, clashes


def document_left_out(doc_page: dict, repeated: Set[str] = frozenset(),
                      with_superseded: bool = False) -> Optional[str]:
    """Why a documents.jsonl page gets no corpus record (None: it gets one): "empty",
    "too_short" (< MIN_DOCUMENT_WORDS words), "repeated" (another document has the same
    page), "superseded" (an older edition) or "site_not_crawled" (a website sites.py no
    longer crawls: run `docs` again). Used by validate.py too."""
    text = (doc_page.get("text") or "").strip()
    if not text or not doc_page.get("doc_id"):
        return "empty"
    words = doc_page.get("word_count")   # documents.jsonl: words without the " | " separators
    if not isinstance(words, int):
        words = sum(1 for w in text.split() if w != "|")
    if words < MIN_DOCUMENT_WORDS:
        return "too_short"
    if doc_page["doc_id"] in repeated:
        return "repeated"
    if doc_page.get("superseded") and not with_superseded:
        return "superseded"
    if not is_parsed_site(url_host(str(doc_page.get("pdf_url") or doc_page.get("url") or ""))):
        return "site_not_crawled"
    return None


def section_to_corpus(sec: SectionRecord, page: Optional[PageRecord] = None) -> CorpusRecord:
    page_title = sec.page_title or ""
    heading = (sec.heading or "").strip()
    title = page_title
    if heading and heading.lower() != page_title.lower():
        if page_title and _PART_ONLY.match(heading):   # the intro, continued: "X (part 2)"
            title = f"{page_title} {heading}"
        else:
            title = f"{page_title} — {heading}" if page_title else heading
    path = " > ".join(h for h in (sec.heading_path or []) if h)
    return CorpusRecord(
        id=sec.section_id,
        source="section",
        url=sec.url,
        title=title or sec.url,
        text=_join_lines(page_title, path, sec.text),
        metadata={
            "site": sec.site or url_host(sec.url),
            "doc_id": sec.doc_id,
            "page_type": sec.page_type,
            "school": sec.school,
            "program": sec.program,
            "degree": sec.degree,
            "level": sec.level,
            "catalog_year": sec.catalog_year,
            "heading_path": list(sec.heading_path or []),
            "breadcrumbs": list(sec.breadcrumbs or []),
            "campuses": list(getattr(sec, "campuses", None) or
                             (page.campuses if page is not None else None) or []),
            "published": page.published if page is not None else None,
            "modified": page.modified if page is not None else None,
            "event_start": getattr(page, "event_start", None) if page is not None else None,
            "event_end": getattr(page, "event_end", None) if page is not None else None,
        },
    )


def course_link(course: CourseRecord) -> str:
    """Link that opens the course listing with this course's description already expanded.

    The listing page's own JavaScript opens and scrolls to the course named by the ``cc``
    parameter (the site's semester maps link to /courses/?cs=PHAS&cal=GR&cc=PHAS-602).
    Without that script the link still opens the right listing page.
    """
    from .course_parser import course_link as listing_deep_link  # see run_parse

    return listing_deep_link(course)


def course_corpus_id(course_id: str) -> str:
    """Corpus id of a course: "CSCI 260|UG" -> "course-CSCI-260-UG" ("MTIM 893|PROF|2" ->
    "course-MTIM-893-PROF-2").

    Corpus ids never contain whitespace: TREC run and qrels files (trec_eval, Pyserini,
    ir_measures, ranx) are split on whitespace, so "course:CSCI 260|UG" would break every
    line that names a course. ``metadata["course_id"]`` keeps the original course_id."""
    return "course-" + re.sub(r"[^A-Za-z0-9]+", "-", str(course_id or "")).strip("-")


def course_to_corpus(course: CourseRecord) -> CorpusRecord:
    """A course. ``metadata["school"]`` is the official school name, as in the section
    records ("College of Engineering and Computing Sciences", not the course table's
    "College of Eng & Comp Sciences"), so one school filter works for every source."""
    from .course_parser import canonical_school  # see run_parse

    title = f"{course.code}: {course.title}" if course.title else course.code
    return CorpusRecord(
        id=course_corpus_id(course.course_id),
        source="course",
        url=course_link(course),
        title=title,
        text=course.text or _join_lines(title, course.description),
        metadata={
            "site": url_host(course.url) or CATALOG_HOST,
            "course_id": course.course_id,
            "code": course.code,
            "subject": course.subject,
            "subject_name": course.subject_name,
            "level": course.level,
            "credits": course.credits,
            "school": canonical_school(course.school) or None,
            "catalog_year": course.catalog_year,
        },
    )


# how a document page is named in corpus titles, by documents.jsonl ``file_type``:
# "Handbook (PDF, p. 3)", "Needs Analysis (Word, part 2)", "UMB How-To (PowerPoint, slide 4)"
DOCUMENT_PAGE_LABELS = {"pdf": ("PDF", "p."), "docx": ("Word", "part"), "xlsx": ("Excel", "part"),
                        "pptx": ("PowerPoint", "slide")}


def document_title(rec: dict) -> str:
    """The document's title, else a readable version of its file name."""
    title = re.sub(r"\s+", " ", str(rec.get("title") or "")).strip()
    if title:
        return title
    name = unquote(urlsplit(str(rec.get("pdf_url") or rec.get("url") or "")).path.rsplit("/", 1)[-1])
    name = re.sub(r"\.(pdf|docx|xlsx|pptx)$", "", name, flags=re.IGNORECASE)
    label = DOCUMENT_PAGE_LABELS.get(str(rec.get("file_type") or "pdf"), ("PDF", ""))[0]
    return re.sub(r"[\s_+-]+", " ", name).strip() or f"{label} document"


def document_to_corpus(rec: dict) -> CorpusRecord:
    """A page of a document found on an NYIT site (documents.jsonl: a PDF, Word, Excel or
    PowerPoint file). The text starts with the document title and its bookmark/heading path
    (the sheet name of a spreadsheet, the title of a slide), like a section starts with its
    page title and heading path. The metadata says what kind of document it is
    (``doc_category``, ``file_type``), which year/term it is for (``edition``, ``doc_year``,
    ``cohort_year``), whether a newer edition exists (``superseded``), whether it is a scan
    and a coarse ``source_weight`` (1.0 = as useful as a web page, lower for financial
    filings, old newsletters...)."""
    from .documents import document_weight  # see run_parse

    title = document_title(rec)
    file_type = str(rec.get("file_type") or "pdf")
    kind, unit = DOCUMENT_PAGE_LABELS.get(file_type, DOCUMENT_PAGE_LABELS["pdf"])
    toc_path = [str(t) for t in rec.get("toc_path") or [] if t]
    found_on = rec.get("found_on") or []
    if isinstance(found_on, str):
        found_on = [found_on]
    pdf_url = str(rec.get("pdf_url") or str(rec.get("url") or "").split("#")[0])
    category = str(rec.get("doc_category") or "other")
    superseded = bool(rec.get("superseded"))
    return CorpusRecord(
        id=str(rec.get("doc_id")),
        source="document",
        url=str(rec.get("url") or pdf_url),
        title=f"{title} ({kind}, {unit} {rec.get('page')})",
        text=_join_lines(title, " > ".join(toc_path), rec.get("text") or ""),
        metadata={
            "site": rec.get("site") or url_host(pdf_url),
            "pdf_url": pdf_url,
            "page": rec.get("page"),
            "page_count": rec.get("page_count"),
            "found_on": list(found_on),
            "toc_path": toc_path,
            "modified": rec.get("modified"),
            "doc_category": category,
            "edition": rec.get("edition"),
            "doc_year": rec.get("doc_year"),
            "cohort_year": rec.get("cohort_year"),
            "superseded": superseded,
            "scanned": bool(rec.get("scanned")),
            "source_weight": document_weight(category, superseded),
            "file_type": file_type,
        },
    )


def pdf_page_to_corpus(rec: dict) -> CorpusRecord:
    """A page of a catalog PDF. The bookmark path goes first in the text, like the heading
    path of a section, so that BM25 knows which part of the catalog the page belongs to."""
    catalog = str(rec.get("catalog") or "")
    title = f"{catalog[:1].upper()}{catalog[1:]} catalog {rec.get('edition')}, p. {rec.get('page')}"
    if rec.get("section"):
        title += f" — {rec['section']}"
    toc_path = list(rec.get("toc_path") or [])
    url = str(rec.get("url") or "")
    return CorpusRecord(
        id=str(rec.get("doc_id")),
        source="pdf_page",
        url=url,
        title=title,
        text=_join_lines(" > ".join(toc_path), rec.get("text") or ""),
        metadata={
            "site": url_host(url) or CATALOG_HOST,
            "edition": rec.get("edition"),
            "catalog": catalog,
            "page": rec.get("page"),
            "toc_path": toc_path,
        },
    )


# ---------------------------------------------------------------------------
# near-duplicate passages across the whole corpus
# ---------------------------------------------------------------------------
def corpus_bodies(sections: Iterable[SectionRecord], courses: Iterable[CourseRecord],
                  pdf_pages: Iterable[dict] = (), documents: Iterable[dict] = ()
                  ) -> Dict[str, str]:
    """``{corpus id: the record's own text}``, without the title/heading prefix that
    corpus.jsonl puts in front of it (so two copies under different titles still match)."""
    bodies: Dict[str, str] = {s.section_id: s.text or "" for s in sections}
    for course in courses:
        bodies[course_corpus_id(course.course_id)] = course.description or course.text or ""
    for rec in list(pdf_pages) + list(documents):
        if rec.get("doc_id"):
            bodies[str(rec["doc_id"])] = rec.get("text") or ""
    return bodies


def shingle_sets(texts: Sequence[str], size: int = SHINGLE_SIZE,
                 min_tokens: int = NEAR_DUP_MIN_TOKENS) -> List[Optional[array]]:
    """The set of word ``size``-shingles of every text (casefolded words and numbers) as a
    sorted array of 64-bit hashes, or None for a text of fewer than ``min_tokens`` words.

    Words are numbered in order of appearance and a shingle is hashed as a tuple of those
    numbers (integer hashing is not randomised, unlike str hashing, so the result is the
    same in every run)."""
    vocab: Dict[str, int] = {}
    out: List[Optional[array]] = []
    for text in texts:
        ids = [vocab.setdefault(w, len(vocab)) for w in _DUP_TOKEN.findall((text or "").casefold())]
        if len(ids) < max(min_tokens, size):
            out.append(None)
            continue
        out.append(array("q", sorted({hash(tuple(ids[i:i + size]))
                                      for i in range(len(ids) - size + 1)})))
    return out


def similar_pairs(sets: Sequence[Optional[array]], threshold: float = NEAR_DUP_JACCARD
                  ) -> List[Tuple[int, int]]:
    """Every pair (i, j), i < j, whose sets have a Jaccard similarity >= ``threshold``.

    An exact set-similarity join (prefix filtering, as in the AllPairs algorithm) instead
    of MinHash: elements are ordered by how many sets contain them (estimated with a
    count-min sketch; any fixed order gives the same, exact, result, rare-first is only
    faster), and two sets with J >= t always share an element among the first
    ``|A| - ceil(t * |A|) + 1`` elements of each. Candidates sharing such an element (and
    of compatible sizes) are verified with their real Jaccard similarity. Identical sets
    are paired directly and joined only once."""
    pairs: List[Tuple[int, int]] = []
    first_of: Dict[bytes, int] = {}
    unique: List[int] = []
    for i, s in enumerate(sets):
        if s is None:
            continue
        first = first_of.setdefault(s.tobytes(), i)
        if first == i:
            unique.append(i)
        else:
            pairs.append((first, i))
    if len(unique) < 2:
        return pairs
    bits = 22
    mask = (1 << bits) - 1
    row1, row2 = array("I", [0]) * (1 << bits), array("I", [0]) * (1 << bits)
    for i in unique:
        for h in sets[i]:
            row1[h & mask] += 1
            row2[(h >> bits) & mask] += 1

    def rarity(h: int) -> Tuple[int, int]:
        return min(row1[h & mask], row2[(h >> bits) & mask]), h

    index: Dict[int, List[int]] = defaultdict(list)
    eps = 1e-9
    for i in sorted(unique, key=lambda k: (len(sets[k]), k)):
        a = sets[i]
        n = len(a)
        prefix = heapq.nsmallest(n - int(math.ceil(threshold * n - eps)) + 1, a, key=rarity)
        candidates: Set[int] = set()
        for h in prefix:
            bucket = index[h]
            candidates.update(j for j in bucket if len(sets[j]) >= threshold * n - eps)
            bucket.append(i)
        if not candidates:
            continue
        members = set(a)
        for j in candidates:
            b = sets[j]
            common = sum(1 for h in b if h in members)
            if common >= threshold * (n + len(b) - common) - eps:
                pairs.append((min(i, j), max(i, j)))
    return pairs


def _groups(n: int, pairs: Iterable[Tuple[int, int]]) -> List[List[int]]:
    """Connected components (union-find) of the pairs, each sorted, in order of their first
    member; singletons are left out."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    comps: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        comps[find(i)].append(i)
    return sorted((c for c in comps.values() if len(c) > 1), key=lambda c: c[0])


# How authoritative a copy is (smaller first): the catalog (and its course directory), the
# policy library, the main website, the campus/online sites, the other sites, documents
# (PDF, Office files), catalog PDF pages.
_SITE_TIER = {"apps.nyit.edu": 1, "www.nyit.edu": 2, "vancouver.nyit.edu": 3,
              "online.nyit.edu": 3}


def corpus_rank(rec: CorpusRecord, page: Optional[PageRecord] = None) -> tuple:
    """Sort key of the copies of a near-duplicate group: the copy kept is the first one.

    Web passages before PDF pages, by website (catalog > apps.nyit.edu policies >
    www.nyit.edu > vancouver/online > the other sites); among passages of one site, a page
    that is not a home/listing page, then the shortest page (the policy's own page rather
    than the handbook that reprints it); among PDF pages, a current edition, then the most
    recently modified file; then the cleanest URL and the id."""
    meta = rec.metadata or {}
    site = str(meta.get("site") or url_host(rec.url))
    if rec.source in ("section", "course"):
        tier = 0 if is_catalog_site(site) else _SITE_TIER.get(site, 4)
    else:
        tier = 5 if rec.source == "document" else 6
    hub = page is not None and page.page_type in ("home", "listing")
    words = page.word_count if page is not None else 0
    if rec.source == "document":
        modified = re.sub(r"\D", "", str(meta.get("modified") or ""))[:8]
    elif rec.source == "pdf_page":
        # catalog PDFs: the newer edition first ("2026-2027" ranks as 20270000)
        years = re.findall(r"(?:19|20)\d\d", str(meta.get("edition") or ""))
        modified = f"{max(years)}0000" if years else ""
    else:
        modified = ""
    url = rec.url.split("#")[0]
    return (tier, hub, bool(meta.get("superseded")), -int(modified or 0), words,
            bool(_URL_WHITESPACE.search(url)), url != url.lower(), len(url), rec.id)


def _dup_scope(rec: CorpusRecord) -> tuple:
    """What a passage is about: copies with the same scope are interchangeable, copies of
    different programs/levels, courses, people (bio pages) or entering classes (semester
    maps of 2025 and 2026) are not."""
    meta = rec.metadata or {}
    if rec.source == "course":
        return ("course", meta.get("code"), meta.get("level"))
    if rec.source == "section" and meta.get("page_type") == "bio":   # about one person
        return ("bio", meta.get("doc_id"))
    if rec.source == "section":
        return ("program", meta.get("program") or None, meta.get("level") or None, None)
    return ("program", None, None, meta.get("cohort_year") or None)


def _dup_page(rec: CorpusRecord) -> Optional[str]:
    """The web page a passage belongs to: two sections of one page are different parts of it
    ("Fall, Year Four" / "Summer, Year Two" of a semester map), never copies of each other,
    unless their whole corpus text is the same."""
    return str(rec.metadata.get("doc_id")) if rec.source == "section" else None


def collapse_near_duplicates(corpus: List[CorpusRecord], bodies: Dict[str, str],
                             pages: Iterable[PageRecord] = (),
                             threshold: float = NEAR_DUP_JACCARD
                             ) -> Tuple[List[CorpusRecord], Dict[str, str], dict]:
    """Group corpus records whose own text (``bodies``; the title prefix left out) is the
    same or nearly the same (Jaccard >= ``threshold`` on word 5-shingles, NEAR_DUP_MIN_TOKENS
    words or more) or whose whole corpus text is the same, on any website and in any source,
    and keep one copy where copies are interchangeable.

    In each group the copies are ranked by corpus_rank. A copy is left out when its whole
    corpus text (title included) equals a better copy's, or when a better copy on another
    page has the same scope (_dup_scope: the same program and level, or neither; the same
    course; the same entering class): the FERPA notice on the catalog and on www.nyit.edu, a
    policy page and the handbook that reprints it, a PDF page and the web page it was
    printed from. Copies of different programs, courses or classes ("STEM OPT Extension" on
    41 degree pages, cross-listed courses with one description, the 2025 and 2026 semester
    maps of a degree) and similar sections of one page all stay; each gets
    ``metadata["dup_group"]`` (the id of the group's best copy) and
    ``metadata["dup_count"]`` (copies in the corpus), so retrieval can show one result per
    group.

    Returns (the records kept, in their order; {id left out: id of the copy kept}; counts
    for parse_summary.json).
    """
    started = time.perf_counter()
    page_by_id = {p.doc_id: p for p in pages}
    ids = [r.id for r in corpus]
    sets = shingle_sets([bodies.get(i, "") for i in ids])
    pairs = similar_pairs(sets, threshold)
    first_with_text: Dict[str, int] = {}
    for i, rec in enumerate(corpus):      # the same whole text (title included), however short
        key = _norm_key(rec.text)
        if key and first_with_text.setdefault(key, i) != i:
            pairs.append((first_with_text[key], i))
    groups = _groups(len(corpus), pairs)
    dropped: Dict[str, str] = {}
    kept_in_group = 0
    examples: List[dict] = []
    for members in groups:
        recs = sorted((corpus[i] for i in members),
                      key=lambda r: corpus_rank(r, page_by_id.get(str(r.metadata.get("doc_id")))))
        texts: Dict[str, str] = {}
        scopes: Dict[tuple, List[Tuple[str, Optional[str]]]] = defaultdict(list)
        kept: List[CorpusRecord] = []
        for rec in recs:
            text, scope, page_id = _norm_key(rec.text), _dup_scope(rec), _dup_page(rec)
            better = texts.get(text) or next(
                (kept_id for kept_id, kept_page in scopes[scope]
                 if page_id is None or kept_page != page_id), None)
            if better:
                dropped[rec.id] = better
                continue
            texts[text] = rec.id
            scopes[scope].append((rec.id, page_id))
            kept.append(rec)
        if len(kept) > 1:
            kept_in_group += len(kept)
            for rec in kept:
                rec.metadata["dup_group"] = kept[0].id
                rec.metadata["dup_count"] = len(kept)
        if len(examples) < NEAR_DUP_EXAMPLES and len(recs) >= 3:
            examples.append({"kept": [r.id for r in kept][:5], "left_out": len(recs) - len(kept),
                             "title": kept[0].title[:100]})
    out = [r for r in corpus if r.id not in dropped]
    by_id = {r.id: r for r in corpus}
    left = [by_id[i] for i in dropped]
    summary = {
        "enabled": True,
        "threshold": threshold,
        "shingle_words": SHINGLE_SIZE,
        "min_words": NEAR_DUP_MIN_TOKENS,
        "groups": len(groups),
        "records_in_groups": sum(len(g) for g in groups),
        "left_out": len(dropped),
        "left_out_by_source": dict(Counter(r.source for r in left).most_common()),
        "left_out_by_site": dict(sorted(Counter(str(r.metadata.get("site") or "")
                                                for r in left).items(),
                                        key=lambda kv: site_rank(kv[0]))),
        "kept_with_dup_group": kept_in_group,
        "dup_groups_in_corpus": len({r.metadata["dup_group"] for r in out
                                     if r.metadata.get("dup_group")}),
        # validate.py checks the corpus against these (sections carry duplicate_of instead)
        "left_out_ids": {src: sorted(i for i in dropped if by_id[i].source == src)
                         for src in ("course", "document", "pdf_page")
                         if any(by_id[i].source == src for i in dropped)},
        "examples": examples,
        "seconds": _lap(started),
    }
    if dropped or kept_in_group:
        log.info("near-duplicate passages (Jaccard >= %.2f): %d groups; %d copies left out of the "
                 "corpus (%s), %d kept with a dup_group (different programs or courses)",
                 threshold, len(groups), len(dropped),
                 ", ".join(f"{k} {v}" for k, v in summary["left_out_by_source"].items()) or "none",
                 kept_in_group)
    return out, dropped, summary


# ---------------------------------------------------------------------------
# summary per website
# ---------------------------------------------------------------------------
def site_summaries(parsed: Sequence[ParsedPage], sections: Sequence[SectionRecord],
                   corpus: Sequence[CorpusRecord], failures: Sequence[dict],
                   boilerplate: Dict[str, Set[str]], seconds: Dict[str, float],
                   repeated: Optional[Dict[str, int]] = None) -> Dict[str, dict]:
    """Counts per website for parse_summary.json (sites in SITE_PRIORITY order).

    ``corpus_records`` counts every corpus record of the site (passages, courses, PDF
    document pages), like stats.json and STATS.md; ``corpus_by_source`` splits it up.
    ``noindex_pages`` counts distinct pages only (the global ``noindex_pages`` of
    parse_summary.json counts every page record, copies included)."""
    rows: Dict[str, dict] = {}

    def row(host: str) -> dict:
        if host not in rows:
            rows[host] = {"pages": 0, "distinct_pages": 0, "duplicate_pages": 0,
                          "noindex_pages": 0, "words": 0, "sections": 0, "section_words": 0,
                          "repeated_sections_removed": 0,
                          "corpus_records": 0, "corpus_by_source": Counter(), "failures": 0,
                          "boilerplate_blocks": 0, "seconds": 0.0, "page_types": Counter()}
        return rows[host]

    for page, _ in parsed:
        r = row(page.site or url_host(page.url))
        r["pages"] += 1
        if page.duplicate_of:
            r["duplicate_pages"] += 1
            continue
        r["distinct_pages"] += 1
        r["words"] += page.word_count or 0
        r["noindex_pages"] += 1 if page.noindex else 0
        r["page_types"][page.page_type] += 1
    for sec in sections:
        r = row(sec.site or url_host(sec.url))
        r["sections"] += 1
        r["section_words"] += sec.word_count or 0
    for rec in corpus:
        r = row(str(rec.metadata.get("site") or url_host(rec.url)))
        r["corpus_records"] += 1
        r["corpus_by_source"][rec.source] += 1
    for failure in failures:
        row(failure.get("site") or url_host(failure.get("url") or ""))["failures"] += 1
    for host, blocks in boilerplate.items():
        row(host)["boilerplate_blocks"] = len(blocks)
    for host, n in (repeated or {}).items():
        row(host)["repeated_sections_removed"] = n
    for host, secs in seconds.items():
        row(host)["seconds"] = round(secs, 2)
    out: Dict[str, dict] = {}
    for host in sorted(rows, key=site_rank):
        r = rows[host]
        r["page_types"] = dict(r["page_types"].most_common())
        r["corpus_by_source"] = dict(r["corpus_by_source"].most_common())
        out[host] = r
    return out


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def write_courses_csv(path: Path, courses: Iterable[CourseRecord]) -> bool:
    """courses.csv: one row per course, the same columns as courses.jsonl.

    ``attributes`` is stored as a JSON string and lists are joined with "; ". The file is
    UTF-8 with a byte-order mark so that Excel shows dashes and accents correctly;
    pandas.read_csv reads it as is (with the csv module, open it with encoding="utf-8-sig").
    Returns False (and logs why) if the file could not be written, e.g. because it is open
    in Excel.
    """
    columns = [f.name for f in fields(CourseRecord)]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for course in courses:
                row = course.to_dict()
                writer.writerow([_csv_value(row.get(name)) for name in columns])
        os.replace(tmp, path)
    except PermissionError as exc:
        log.error("could not write %s (%s). Is the file open in Excel or another program? "
                  "Close it and run `parse` again; courses.jsonl is complete.", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return value


def _join_lines(*parts: Optional[str]) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _failure(url: str, entry: dict, stage: str, exc: Exception) -> dict:
    log.warning("could not parse %s (%s step): %s: %s", url, stage, type(exc).__name__, exc)
    log.debug("traceback for %s", url, exc_info=True)
    return {"url": url, "file": entry.get("file"), "site": url_host(url), "stage": stage,
            "error": f"{type(exc).__name__}: {exc}"}


def _lap(start: float) -> float:
    return round(time.perf_counter() - start, 2)


def _log_summary(summary: dict, out_dir: Path) -> None:
    by_source = summary["corpus_by_source"]
    log.info("parsed %d pages of %d websites in %.1fs (exact duplicates: %d, noindex: %d, "
             "failures: %d)", summary["pages"], len(summary["sites"]),
             summary["timings_seconds"]["total"], summary["duplicate_pages"],
             summary["noindex_pages"], len(summary["failures"]))
    for host, r in summary["by_site"].items():
        log.info("  %-24s %6d pages (%d distinct), %7d sections, %9d words", host, r["pages"],
                 r["distinct_pages"], r["sections"], r["words"])
    log.info("-> %d sections, %d courses in %d subjects, %d corpus records (%s)",
             summary["sections"], summary["courses"], summary["course_subjects"],
             summary["corpus"], ", ".join(f"{k} {v}" for k, v in by_source.items()) or "empty")
    near = summary.get("near_duplicates") or {}
    if near.get("enabled"):
        log.info("near-duplicates: %d copies left out of the corpus, %d records share a "
                 "dup_group with copies for other programs/courses", near.get("left_out", 0),
                 near.get("kept_with_dup_group", 0))
    if summary["courses_not_in_corpus"]:
        log.info("legacy 'Conversion' course codes kept in %s but not in %s: %d", COURSES_FILE,
                 CORPUS_FILE, summary["courses_not_in_corpus"])
    if summary["empty_course_listings"]:
        log.warning("course listing pages without any course: %d, e.g. %s",
                    len(summary["empty_course_listings"]), summary["empty_course_listings"][0])
    if summary["failures"]:
        log.warning("pages that failed to parse: %d (details in %s)", len(summary["failures"]),
                    out_dir / PARSE_SUMMARY_FILE)
    log.info("wrote %s, %s, %s, %s, %s, %s to %s", PAGES_FILE, SECTIONS_FILE, COURSES_FILE,
             COURSES_CSV_FILE, CORPUS_FILE, PARSE_SUMMARY_FILE, out_dir)
