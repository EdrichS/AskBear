"""Data-quality checks on the scraper's outputs.

    python -m askbear_scraper validate [--strict]

Runs 46 checks over data/raw/ and data/processed/, grouped by topic (crawl, pages,
catalog, passages, text hygiene, catalog PDFs, PDF documents, corpus), and prints one line
per check:

    PASS  as expected
    WARN  worth a look, but the data is usable (e.g. a page NYIT's own server cannot serve)
    FAIL  something is wrong with the data or a parser; fix it before the data goes to
          the indexing stage
    SKIP  the input of this check does not exist yet (e.g. the PDFs were not extracted)

A few examples of offending records follow the table. Everything, with up to 10
example ids/URLs per check, is saved to data/processed/validation_report.json. With
``--strict`` the command exits with code 2 when a check FAILs (useful in scripts).
"""

from __future__ import annotations

import logging
import re
import shutil
import textwrap
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from . import config, sites
from .config import Settings
from .crawler import _LOGIN_TITLE, SPARSE_MAX_WORDS, load_manifest
from .models import PAGE_TYPES
from .pipeline import (CATALOG_HOST, CORPUS_EXCLUDED_PAGE_TYPES, CORPUS_FILE, COURSES_FILE,
                       DOCS_MANIFEST_FILE, DOCUMENTS_FILE, DOCUMENTS_SUMMARY_FILE,
                       MIN_DOCUMENT_WORDS, MIN_INDEXED_WORDS, MIN_SECTION_WORDS, PAGES_FILE,
                       PARSE_SUMMARY_FILE, PDF_PAGES_FILE, PDF_SUMMARY_FILE, SECTIONS_FILE,
                       course_corpus_id, course_in_corpus, document_left_out, is_catalog_site,
                       is_in_scope, is_listing_continuation, looks_like_not_found,
                       repeated_document_pages, site_rank)
from .stats import (CRAWL_SUMMARY_FILE, SITEMAPS_DIR, document_summary_items, emit, fmt_time,
                    load_json, load_jsonl, load_sitemaps, pdf_summary_items, plural, short_host,
                    site_of)
from .urls import link_kind, normalize_url, url_host, url_path
from .utils import now_iso, sha1, write_json

log = logging.getLogger("askbear.validate")

REPORT_FILE = "validation_report.json"
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
MAX_EXAMPLES = 10           # examples saved per check (3 are printed)
PRINTED_EXAMPLES = 3

# Pages that NYIT's own server answers with HTTP 500 (seen November 2026). They are
# listed under "Crawl errors", but there is nothing to fix on our side.
KNOWN_SERVER_ERRORS = ("/administration_and_faculty/faculty", "/medicine/faculty")

SHORT_PAGE_WORDS = 20
LONG_SECTION_WORDS = 1000
MIN_DESCRIPTION_SHARE = 0.80
MAX_EMPTY_PDF_SHARE = 0.05
MAX_EMPTY_DOCUMENT_SHARE = 0.20  # forms and scans: a share of image-only pages is normal
MAX_OTHER_PAGE_SHARE = 0.15     # share of a site's pages that page_type rules leave as "other"
MIN_PROGRAM_FIELD_SHARE = 0.90  # catalog program pages that should have program/degree/level
MIN_DEGREE_FIELD_SHARE = 0.80   # www.nyit.edu degree pages that should have them
MAX_BAD_COURSE_SHARE = 0.05     # above this, odd course codes/credits are a parser bug
MAX_CREDITS = 60.0
MIN_SITEMAP_COVERAGE = 0.80     # share of a site's sitemap pages that should be saved
MAX_EMPTY_TEXT_SHARE = 0.50     # sites.py sites: pages without text before it is a parser bug
MAX_EMPTY_CATALOG_SHARE = 0.01  # ... the same for the catalog (a few empty NYIT templates exist)
MIN_PAGES_FOR_EMPTY_FAIL = 10   # sites with fewer pages only get a WARN
MAX_DIRTY_SHARE = 0.01          # generic sites: records with leftovers before it is a parser bug
BOILERPLATE_SHARE = 0.30        # a paragraph in more of a site's passages than this is boilerplate
MIN_LEAK_SECTIONS = 20          # sites with fewer passages are not checked for boilerplate
MIN_LEAK_WORDS = 3              # paragraphs shorter than this are ignored ("Apply Now")
MAX_ANCHOR_PAGES_PER_SITE = 400 # cached pages read per site to verify #anchor links
REPEATED_TITLE_MIN = 10         # a title on this many pages (and 5% of a site) is suspicious
MIRROR_SHARE = 0.50             # a site whose pages mostly copy another site's is a mirror
MIN_MIRROR_PAGES = 5            # ... checked for sites with at least this many pages
MIN_DATED_SHARE = 0.50          # news/events that should have a publication date
PAGE_LEVELS = (None, "undergraduate", "graduate", "doctoral", "professional")
COURSE_LEVELS = {"undergraduate": "UG", "graduate": "GR", "professional": "PROF"}
COURSE_CODE = re.compile(r"^[A-Z]{2,6} [0-9]{3}[A-Z0-9]*$")      # "CSCI 260", "BIOL 110L"
# A few real codes use letters in the number part ("OM AP01", "ARCH ABRDG", "BLFA BILLN1");
# they are printed like that on the site, so they are reported but not counted as problems.
COURSE_CODE_LETTERS = re.compile(r"^[A-Z]{2,6} [A-Z0-9]{1,8}$")
CORPUS_SOURCES = ("section", "course", "document", "pdf_page")
DATED_PAGE_TYPES = ("news", "media_coverage", "expert_insight", "event", "blog_post",
                    "magazine_article", "communication")
# query parameters that page through a listing ("?p=2", "?paged=3", "?fsa_page=4"): when
# links that differ only by one of them normalise to the same page, pages are lost
_PAGINATION_PARAM = re.compile(r"^(p|page|paged|pg|start|offset|pagenum|page_?no|\w+_page)$",
                               re.I)
SKIPPED_STATUSES = ("soft404", "login_page", "offsite_redirect")
# Why `parse` leaves a cached page out (parse_summary.json "skipped")
SKIP_LABELS = {
    "same_page_as_other_url": ("URL alias of another page", "URL aliases of other pages"),
    "out_of_scope": ("page outside the crawl scope of sites.py",
                     "pages outside the crawl scope of sites.py"),
    "offsite_redirect": ("redirect to a non-NYIT or login site",
                         "redirects to non-NYIT or login sites"),
    "not_found_page": ("'not found' page served with HTTP 200",
                       "'not found' pages served with HTTP 200"),
    "missing_cache_file": ("entry without its cache file", "entries without their cache file"),
}

# Titles that name a template or the university instead of the page (a title-extraction bug).
GENERIC_TITLES = {
    "staff & faculty directory", "staff and faculty directory", "page not found", "not found",
    "404", "error 404", "new york tech", "new york institute of technology", "nyit",
    "home", "untitled", "index", "libguides", "libanswers", "events calendar",
}
# ... a homepage may be called after the university, but never "Page not found"
HOME_TITLES_OK = {"new york tech", "new york institute of technology", "nyit", "home",
                  "events calendar", "libguides", "libanswers"}
_NOT_FOUND_TITLE = re.compile(r"\b(page not found|404 not found|error 404)\b", re.I)

# Leftovers that must never reach the search index.
HYGIENE_PATTERNS: Tuple[Tuple[str, Any], ...] = (
    ("replacement character U+FFFD", re.compile("�")),
    ("HTML entity", re.compile(r"&(?:amp|nbsp|lt|gt|quot|apos|ndash|mdash|lsquo|rsquo|ldquo|"
                               r"rdquo|hellip|copy|reg|trade|bull|middot|shy|#[0-9]{2,6}|"
                               r"#[xX][0-9a-fA-F]{2,5});")),
    ("HTML tag", re.compile(r"<br\b|</?(?:a|b|div|em|h[1-6]|i|li|ol|p|span|strong|sup|sub|"
                            r"table|td|th|tr|ul)\b[^<>]{0,200}>", re.I)),
    ("'Back to Top' link", re.compile(r"back to top", re.I)),
    ("javascript: link", re.compile(r"javascript:")),
    ("ligature character", re.compile("[ﬀ-ﬆ]")),  # ff fi fl ffi ffl st
    ("mojibake", re.compile("â€|Ã[\u0080-¿]")),  # UTF-8 read as cp1252
    ("control character", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")),
    ("private-use glyph", re.compile("[\ue000-\uf8ff]")),   # Symbol/Wingdings bullets
)
# ... and what PDF text extraction leaves on top of that
PDF_HYGIENE_PATTERNS: Tuple[Tuple[str, Any], ...] = (
    # one glyph repeated as separate words: the dot leaders of IRS forms ("m m m m m m")
    ("single-glyph run", re.compile(r"(?<!\S)([^\s\d|])(?: \1){3,}(?!\S)")),
    # a word printed letter-spaced ("S P R I N G", "s e r v i n g")
    ("letter-spaced word", re.compile(r"(?<![^\s|])(?:[^\W\d_] ){3,}[^\W\d_](?![^\s|])")),
)
_ID_OR_NAME = re.compile(r"""\b(?:id|name)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I)
_DATA_COURSE = re.compile(r"""data-course\s*=\s*["']([^"']+)["']""")
_SPACES = re.compile(r"\s+")


@dataclass
class Check:
    name: str
    status: str                 # PASS | WARN | FAIL | SKIP
    message: str
    count: int = 0              # number of offending items (0 when there are none)
    examples: List[str] = field(default_factory=list)
    group: str = ""             # topic heading in the report ("Crawl", "Pages", ...)

    def to_dict(self) -> dict:
        return asdict(self)


def check(name: str, status: str, message: str, offenders: Sequence[str] = ()) -> Check:
    return Check(name, status, message, len(offenders), list(offenders[:MAX_EXAMPLES]))


@dataclass
class Inputs:
    """Everything the checks look at (None = the file does not exist)."""

    settings: Settings
    manifest: Optional[Dict[str, dict]]
    crawl_summary: Optional[dict]
    parse_summary: Optional[dict]
    pages: Optional[List[dict]]
    sections: Optional[List[dict]]
    courses: Optional[List[dict]]
    corpus: Optional[List[dict]]
    pdf_pages: Optional[List[dict]]
    pdf_summary: Any
    documents: Optional[List[dict]] = None
    documents_summary: Any = None
    docs_manifest: Optional[List[dict]] = None
    sitemaps: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def distinct_pages(self) -> List[dict]:
        return [p for p in self.pages or [] if not p.get("duplicate_of")]

    @property
    def multi_site_summary(self) -> bool:
        """crawl_summary.json written by the multi-site crawler (it lists "sites")."""
        return isinstance(self.crawl_summary, dict) and "sites" in self.crawl_summary


def load_inputs(settings: Settings) -> Inputs:
    out = settings.processed_dir
    manifest_path = settings.manifest_path
    return Inputs(
        settings=settings,
        manifest=load_manifest(manifest_path) if manifest_path.exists() else None,
        crawl_summary=load_json(settings.raw_dir / CRAWL_SUMMARY_FILE),
        parse_summary=load_json(out / PARSE_SUMMARY_FILE),
        pages=load_jsonl(out / PAGES_FILE, _without_links),
        sections=load_jsonl(out / SECTIONS_FILE),
        courses=load_jsonl(out / COURSES_FILE),
        corpus=load_jsonl(out / CORPUS_FILE, _slim_corpus),
        pdf_pages=load_jsonl(out / PDF_PAGES_FILE),
        pdf_summary=load_json(out / PDF_SUMMARY_FILE),
        documents=load_jsonl(out / DOCUMENTS_FILE),
        documents_summary=load_json(out / DOCUMENTS_SUMMARY_FILE),
        docs_manifest=load_jsonl(settings.raw_dir / DOCS_MANIFEST_FILE),
        sitemaps=load_sitemaps(settings.raw_dir / SITEMAPS_DIR),
    )


def _without_links(rec: dict) -> dict:
    rec = dict(rec)
    rec.pop("links", None)
    return rec


def _slim_corpus(rec: dict) -> dict:
    """Corpus records without their text (the passages are checked in their own files),
    plus a hash of the text to find records that repeat each other."""
    rec = dict(rec)
    text = rec.pop("text", None) or ""
    rec["has_text"] = bool(text.strip())
    rec["text_key"] = sha1(" ".join(text.casefold().split())) if text.strip() else ""
    return rec


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def run_validate(settings: Settings) -> dict:
    """Run every check, print the report, write validation_report.json.

    Returns {"passed": bool (no FAIL), "counts": {...}, "checks": [...], ...}.
    """
    data = load_inputs(settings)
    checks: List[Check] = []
    for group, fn in CHECKS:
        try:
            result = fn(data)
        except Exception as exc:  # a bug in one check must not hide the others
            log.debug("check %s crashed", fn.__name__, exc_info=True)
            result = Check(fn.__name__.replace("check_", "").replace("_", " "), FAIL,
                           f"the check itself crashed: {type(exc).__name__}: {exc}")
        for c in result if isinstance(result, list) else [result]:
            c.group = group
            checks.append(c)

    counts = Counter(c.status for c in checks)
    report = {
        "generated_at": now_iso(),
        "data_dir": str(settings.data_dir),
        "passed": counts[FAIL] == 0,
        "counts": {s: counts.get(s, 0) for s in (PASS, WARN, FAIL, SKIP)},
        "checks": [c.to_dict() for c in checks],
    }
    path = settings.processed_dir / REPORT_FILE
    write_json(path, report)
    emit(render_report(checks, report, str(settings.data_dir), str(path)))
    return report


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------
def check_crawl_finished(d: Inputs) -> Check:
    name = "Crawl finished"
    if d.manifest is None:
        return check(name, FAIL, f"no crawl manifest at {d.settings.manifest_path}: run "
                                 "`python -m askbear_scraper crawl`")
    s = d.crawl_summary
    resume = ("Run `python -m askbear_scraper crawl` again to finish it (cached pages are "
              "not downloaded twice).")
    hosts = {e.get("site") or url_host(u) for u, e in d.manifest.items()}
    if not isinstance(s, dict):
        return check(name, WARN, f"no {CRAWL_SUMMARY_FILE} yet: the crawl is still running or "
                                 f"stopped early ({len(d.manifest):,} URLs on "
                                 f"{plural(len(hosts), 'website')} so far). {resume}")
    if s.get("interrupted"):
        visited = s.get("urls_visited") or 0
        return check(name, WARN, f"the last crawl was interrupted after {visited:,} URLs. {resume}")
    if s.get("still_queued"):
        return check(name, WARN, f"the last crawl stopped with {s['still_queued']:,} URLs still "
                                 f"queued. {resume}")
    if _crawl_running(d):
        return check(name, WARN, f"pages were fetched after the last crawl finished "
                                 f"({fmt_time(s.get('finished_at'))}): a new crawl may be running. "
                                 f"{resume}")
    visited = s.get("urls_visited") or len(d.manifest)
    if not d.multi_site_summary:
        return check(name, WARN, f"the last crawl finished ({visited:,} URLs, "
                                 f"{fmt_time(s.get('finished_at'))}) but it was the older "
                                 f"catalog-only crawl ({CRAWL_SUMMARY_FILE} lists no websites): run "
                                 "`python -m askbear_scraper crawl` to cover every NYIT website")
    failed = list(s.get("failed_sites") or [])
    if failed:
        reasons = s.get("unreachable_reasons") or {}
        return check(name, FAIL, f"the crawl finished but saved no page of "
                                 f"{plural(len(failed), 'enabled website')} of sites.py: "
                                 f"{', '.join(failed)} (unreachable, or robots.txt could not be "
                                 "read). The dataset is incomplete: run `python -m askbear_scraper "
                                 "crawl --site HOST` again later (cached pages are not "
                                 "downloaded twice)",
                     [f"{h}: {reasons[h]}" if reasons.get(h) else h for h in failed])
    crawled = s.get("sites") or []
    msg = (f"{visited:,} URLs on {plural(len(crawled), 'website')} visited, nothing left in the "
           f"queue (finished {fmt_time(s.get('finished_at'))})")
    runs = [r for r in s.get("runs") or [] if isinstance(r, dict)]
    last = s.get("last_run") if isinstance(s.get("last_run"), dict) else None
    if len(runs) > 1 and last:
        msg += (f"; {plural(len(runs), 'crawl run')}, the last one only for "
                f"{', '.join(last.get('sites') or []) or 'some websites'} "
                f"({fmt_time(last.get('finished_at'))})")
    return check(name, PASS, msg)


def check_site_limits(d: Inputs) -> Check:
    name = "Website page limits and failures"
    if not d.multi_site_summary:
        return check(name, SKIP, f"needs a {CRAWL_SUMMARY_FILE} written by the multi-site crawler")
    capped_all = d.crawl_summary.get("capped_sites") or {}
    # a host that sites.py disables now (e.g. catalog-dev.nyit.edu, a development copy the
    # crawl met before it was disabled) is not parsed: its limit does not matter
    capped = {h: n for h, n in capped_all.items() if _crawled_site(h)}
    disabled_note = ""
    if len(capped) < len(capped_all):
        skipped = sorted(set(capped_all) - set(capped), key=site_rank)
        disabled_note = (f"; {', '.join(skipped)} also reached a limit but "
                         f"{'is' if len(skipped) == 1 else 'are'} no longer crawled (disabled in "
                         "sites.py)")
    given_up = d.crawl_summary.get("given_up_sites") or {}
    unreachable = sorted(d.crawl_summary.get("unreachable_sites") or [], key=site_rank)
    if not capped and not given_up:
        note = (f"; unreachable hosts found through links: {', '.join(unreachable)}"
                if unreachable else "")
        status = WARN if any(_profiled(h) and sites.SITES[h].enabled for h in unreachable) \
            else PASS
        return check(name, status, "no website reached its page limit (sites.py max_pages) or "
                                   f"kept failing{note}{disabled_note}",
                     [f"{h}: could not be reached (no connection)" for h in unreachable])
    offenders = [f"{h}: gave up after repeated failures, {n:,} queued URLs not crawled"
                 for h, n in sorted(given_up.items(), key=lambda kv: site_rank(kv[0]))]
    used = d.crawl_summary.get("page_limits") or {}
    saved = d.crawl_summary.get("status_by_site") or {}
    last = d.crawl_summary.get("last_run") if isinstance(d.crawl_summary.get("last_run"), dict) \
        else d.crawl_summary
    last_sites = set(last.get("sites") or [])
    for host in sorted(capped, key=site_rank):
        profile = sites.profile_for(host)
        now = profile.max_pages if profile is not None else None
        limit = used.get(host)
        if not limit:   # older summaries do not say which limit was reached
            ok = int((saved.get(host) or {}).get("ok") or 0)
            # "settings" are those of the last run (`crawl --site` runs merge the summary)
            cli = (d.crawl_summary.get("settings") or {}).get("max_pages_per_site") \
                if host in last_sites else None
            limit = min(now, cli) if (now and cli) else (now or cli)
            if ok and limit and ok < limit:
                limit = ok   # sites.py allows more pages now than when the crawl ran
        text = (f"{host}: limit {limit:,} pages reached, {capped[host]:,} queued URLs not crawled"
                if limit else f"{host}: {capped[host]:,} queued URLs not crawled")
        if limit and now and now > limit:
            text += (f" (sites.py now allows {now:,}: `python -m askbear_scraper crawl --site "
                     f"{host}` fetches the rest)")
        offenders.append(text)
    parts = []
    if capped:
        parts.append(f"{plural(len(capped), 'website')} reached the page limit and "
                     f"{sum(capped.values()):,} of their URLs were not crawled (if they matter, "
                     "raise max_pages for that site in sites.py; --max-pages can only lower a "
                     "site's limit)")
    if given_up:
        parts.append(f"the crawler gave up on {plural(len(given_up), 'website')} that kept failing "
                     f"({sum(given_up.values()):,} queued URLs not crawled)")
    if unreachable:
        parts.append(f"unreachable: {', '.join(unreachable)}")
    return check(name, WARN, "; ".join(parts) + disabled_note,
                 offenders + [f"{h}: could not be reached (no connection)" for h in unreachable])


def check_pagination(d: Inputs) -> Check:
    """Listings split over several pages (?p=2, ?fsa_page=3...) must be crawled page by
    page: their later pages are the only links to older blog posts, and www.nyit.edu's
    accomplishments pages hold the accomplishments themselves."""
    name = "Paginated listings crawled"
    if d.manifest is None:
        return check(name, SKIP, "no crawl manifest")
    offenders: List[str] = []
    # 1) recorded by the crawler: links that differ from their page only by a page number
    #    were normalised to the page itself (sites.py keep_query_params drops the parameter)
    dropped = (d.crawl_summary or {}).get("dropped_query_self_links") or {} \
        if isinstance(d.crawl_summary, dict) else {}
    for host in sorted(dropped, key=site_rank):
        if not _crawled_site(host):
            continue
        for param, n in sorted((dropped.get(host) or {}).items()):
            if _PAGINATION_PARAM.match(param):
                offenders.append(f"{host}: {n:,} links with ?{param}= were normalised to the page "
                                 f"they are on (add {param!r} to keep_query_params of {host} in "
                                 "sites.py)")
    # 2) sites.py keeps a page-number parameter, but no cached URL has it: the listing pages
    #    after page 1 were not crawled yet (the crawl ran before that rule existed)
    queries: Dict[str, Set[str]] = defaultdict(set)
    for url in d.manifest:
        query = urlsplit(url).query
        if query:
            queries[url_host(url)].update(k for k, _ in parse_qsl(query, keep_blank_values=True))
    for host, profile in sorted(sites.SITES.items(), key=lambda kv: site_rank(kv[0])):
        params = [q for q in profile.keep_query_params if _PAGINATION_PARAM.match(q)]
        if not profile.enabled or not params or not any(url_host(u) == host for u in d.manifest):
            continue
        missing = [q for q in params if q not in queries.get(host, set())]
        if missing:
            offenders.append(f"{host}: sites.py follows ?{'/?'.join(missing)}= page links but no "
                             "cached URL has one: pages 2, 3... of its listings (and what only "
                             "they link to) are not crawled yet; run `python -m askbear_scraper "
                             f"crawl --site {host}`")
    if offenders:
        hosts = sorted({o.split(":", 1)[0] for o in offenders}, key=site_rank)
        return check(name, WARN, f"paginated listings of {plural(len(hosts), 'website')} "
                                 f"({', '.join(hosts)}) are only crawled up to page 1, so some "
                                 "pages behind them are missing (coverage gap, not an error)",
                     offenders)
    return check(name, PASS, "every page-number parameter that sites.py follows occurs in the "
                             "cache, and no page link was normalised away")


def check_crawl_errors(d: Inputs) -> Check:
    name = "Crawl errors"
    if d.manifest is None:
        return check(name, SKIP, "no crawl manifest")
    failed = [(url, e) for url, e in sorted(d.manifest.items(), key=lambda kv: (
        site_rank(kv[1].get("site") or url_host(kv[0])), kv[0]))
        if e.get("status") in ("http_error", "error", "not_found", "redirect_loop")]
    skipped = Counter(e.get("status") for e in d.manifest.values()
                      if e.get("status") in SKIPPED_STATUSES)
    labels = {"soft404": ("empty 'soft 404' page", "empty 'soft 404' pages"),
              "login_page": ("login page", "login pages"),
              "offsite_redirect": ("redirect to a non-NYIT or login site",
                                   "redirects to non-NYIT or login sites")}
    skip_note = "; skipped on purpose: " + ", ".join(plural(n, *labels[s]) for s, n in
                                                     skipped.most_common()) if skipped else ""
    if not failed:
        return check(name, PASS, f"every URL could be fetched{skip_note}")
    known = {url for url, e in failed if _known_server_error(url, e)}
    per_site: Dict[str, Counter] = defaultdict(Counter)
    for url, e in failed:
        per_site[e.get("site") or url_host(url)][_fetch_error(e)] += 1
    biggest = sorted(per_site.items(), key=lambda kv: (-sum(kv[1].values()), site_rank(kv[0])))
    spread = "; ".join(f"{short_host(h)} {sum(c.values()):,} ("
                       + ", ".join(f"{k} {v:,}" for k, v in c.most_common(2)) + ")"
                       for h, c in biggest[:6])
    if len(biggest) > 6:
        rest = sum(sum(c.values()) for _, c in biggest[6:])
        spread += f"; {plural(len(biggest) - 6, 'other website')} {rest:,}"
    offenders = [f"{url}  ({_fetch_error(e)}{', known NYIT-side problem' if url in known else ''})"
                 for url, e in failed]
    msg = f"{plural(len(failed), 'URL')} could not be fetched ({spread})"
    if known:
        msg += (f"; {len(known)} {_are(len(known))} catalog faculty pages that NYIT's server "
                "answers with HTTP 500 (a known problem on their side)")
    others = len(failed) - len(known)
    loops = sum(1 for _, e in failed if e.get("status") == "redirect_loop")
    others = len(failed) - len(known) - loops
    if others:
        msg += (f"; {'the other ' if known or loops else ''}{plural(others, 'URL')}: broken links "
                "on NYIT's sites (404) or temporary errors (`crawl` retries them when run again)")
    if loops:
        msg += (f"; {plural(loops, 'URL')} {'redirects' if loops == 1 else 'redirect'} in a circle "
                "on NYIT's side (redirect loop, cached: not retried)")
    return check(name, WARN, msg + skip_note, offenders)


def _known_server_error(url: str, entry: dict) -> bool:
    if url_host(url) != CATALOG_HOST:
        return False
    path = url_path(url)
    return path in KNOWN_SERVER_ERRORS or (entry.get("http_status") == 500
                                           and path.endswith("/faculty"))


def _fetch_error(entry: dict) -> str:
    """"HTTP 404", or the short name of a network error ("ConnectTimeout")."""
    if entry.get("status") == "redirect_loop":
        return "redirect loop"
    if entry.get("http_status"):
        return f"HTTP {entry['http_status']}"
    error = str(entry.get("error") or entry.get("status") or "error")
    return _cut(error.split(":", 1)[0].strip() or error, 40)


def check_site_coverage(d: Inputs) -> Check:
    name = "Every website produced pages"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    counts = Counter(site_of(p) for p in d.distinct_pages)
    listed = dict((d.crawl_summary or {}).get("sitemap_pages_by_site") or {}) \
        if isinstance(d.crawl_summary, dict) else {}
    expected = sorted({h for h, p in sites.SITES.items() if p.enabled
                       and (d.sitemaps.get(h) or listed.get(h))}, key=site_rank)
    if CATALOG_HOST not in expected and d.manifest is not None and any(
            url_host(u) == CATALOG_HOST and e.get("status") == "ok" for u, e in d.manifest.items()):
        expected.insert(0, CATALOG_HOST)
    finished = _crawl_complete(d) and not _crawl_running(d)
    last_run = set(d.crawl_summary.get("sites") or []) if d.multi_site_summary else {CATALOG_HOST}
    missing = [h for h in expected if not counts.get(h)]
    low: List[str] = []
    for host in expected:
        urls = [u for u in d.sitemaps.get(host) or [] if link_kind(u) == "page"]
        if not urls or d.manifest is None:
            continue
        statuses = Counter((d.manifest.get(u) or {}).get("status") or "not visited" for u in urls)
        # "duplicate" = a redirect to a page that was saved under another URL: covered too
        covered = statuses["ok"] + statuses["duplicate"]
        if covered / len(urls) < MIN_SITEMAP_COVERAGE:
            why = ", ".join(f"{s} {n:,}" for s, n in statuses.most_common()
                            if s not in ("ok", "duplicate"))
            low.append(f"{host}: {covered:,} of {len(urls):,} sitemap pages saved ({why})")
    detail = ", ".join(f"{short_host(h)} {counts[h]:,}" for h in sorted(counts, key=site_rank)
                       if h in sites.SITES)
    others = [h for h in counts if h not in sites.SITES]
    if others:
        detail += (f"; {plural(len(others), 'other website')} found through links: "
                   f"{sum(counts[h] for h in others):,} pages")
    if missing:
        hard = [h for h in missing if finished and h in last_run]
        status = FAIL if hard else WARN
        why = ("the crawl finished, so their pages were lost (parser or routing problem)" if hard
               else "they were not crawled yet (crawl unfinished, or not in the last run)")
        return check(name, status, f"{plural(len(missing), 'website')} with pages in their "
                                   f"sitemaps {'has' if len(missing) == 1 else 'have'} no parsed "
                                   f"pages: {', '.join(missing)}; {why}. Pages per site: "
                                   f"{detail or 'none'}", missing + low)
    if low and finished:
        return check(name, WARN, f"every website with a sitemap has pages, but "
                                 f"{plural(len(low), 'website')} saved less than "
                                 f"{MIN_SITEMAP_COVERAGE:.0%} of the pages its sitemaps list. "
                                 f"Distinct pages per site: {detail}", low)
    note = f" ({len(low)} not fully crawled yet: the crawl is not finished)" if low else ""
    return check(name, PASS, f"{plural(len(counts), 'website')} with distinct pages{note}: {detail}")


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def check_pages_parsed(d: Inputs) -> Check:
    name = "All crawled pages parsed"
    if not d.pages:
        state = "no" if d.pages is None else "an empty"
        return check(name, FAIL, f"{state} {PAGES_FILE}: run `python -m askbear_scraper parse`")
    failures = (d.parse_summary or {}).get("failures") or []
    page_failures = [f for f in failures if f.get("stage") != "boilerplate"]
    site_failures = [f for f in failures if f.get("stage") == "boilerplate"]
    examples = [f"{f.get('url')}  [{f.get('stage')}] {f.get('error')}"
                for f in page_failures + site_failures]
    if page_failures:
        per_site = Counter(f.get("site") or url_host(f.get("url") or "") for f in page_failures)
        spread = ", ".join(f"{short_host(h)} {n:,}" for h, n in per_site.most_common())
        return check(name, FAIL, f"{plural(len(page_failures), 'page')} could not be parsed "
                                 f"({spread}): a parser bug or an unusual page; details under "
                                 f"'failures' in {PARSE_SUMMARY_FILE}", examples)
    bp_note = ""
    if site_failures:
        hosts = sorted({f.get("site") or url_host(f.get("url") or "") for f in site_failures},
                       key=site_rank)
        bp_note = (f"; the repeated-block (boilerplate) analysis failed on {', '.join(hosts)}, "
                   "so those pages were parsed without boilerplate removal")
    status = WARN if site_failures else PASS
    if d.manifest is None:
        return check(name, status, f"{len(d.pages):,} page records (no manifest to compare "
                                   f"with){bp_note}", examples)
    page_urls = {p.get("url") for p in d.pages}
    skipped = {u for urls in ((d.parse_summary or {}).get("skipped") or {}).values() for u in urls}
    cached = [(url, e) for url, e in d.manifest.items()
              if e.get("status") == "ok" and e.get("file")
              and (d.settings.html_dir / e["file"]).is_file()]
    missing = []
    aliases = 0
    for url, e in cached:
        final = normalize_url(e.get("final_url") or url) or url
        if {url, final} & skipped and not {url} & page_urls:
            aliases += 1
        elif not {url, final} & page_urls:
            missing.append(url)
    missing.sort()
    if missing:
        what = f"{plural(len(missing), 'cached page')} {_are(len(missing))}"
        return check(name, WARN, f"{what} not in {PAGES_FILE} (crawled after the last parse?): "
                                 "run `python -m askbear_scraper parse` again", missing)
    reasons = {reason: len(urls) for reason, urls in
               ((d.parse_summary or {}).get("skipped") or {}).items() if urls}
    note = ("; skipped on purpose: " + ", ".join(
        plural(n, *SKIP_LABELS.get(reason, (reason, reason))) for reason, n in
        sorted(reasons.items(), key=lambda kv: -kv[1]))) if reasons else ""
    return check(name, status, f"{len(d.pages):,} page records for {len(cached) - aliases:,} "
                               f"cached pages, no page failed{note}{bp_note}", examples)


def check_page_ids(d: Inputs) -> Check:
    name = "Unique page URLs and doc_ids"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    dup_urls = _repeated(p.get("url") for p in d.pages)
    dup_ids = _repeated(p.get("doc_id") for p in d.pages)
    if dup_urls or dup_ids:
        return check(name, FAIL, f"{len(dup_urls)} URL(s) and {len(dup_ids)} doc_id(s) appear more "
                                 "than once", [f"url {u}" for u in dup_urls] +
                     [f"doc_id {i}" for i in dup_ids])
    n_sites = len({site_of(p) for p in d.pages})
    return check(name, PASS, f"{len(d.pages):,} pages on {plural(n_sites, 'website')}, every URL "
                             "and doc_id is unique")


def check_page_sites(d: Inputs) -> Check:
    name = "Website of every page and section"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    problems: List[str] = []
    for p in d.pages:
        host = url_host(p.get("url") or "")
        if not p.get("site"):
            problems.append(f"{p.get('url')}: no site")
        elif p["site"] != host:
            problems.append(f"{p.get('url')}: site {p['site']!r} is not the URL's host")
        elif not is_in_scope(p.get("url") or ""):
            problems.append(f"{p.get('url')}: outside the crawl scope of sites.py (a website or "
                            "path that is not crawled)")
    page_site = {p.get("doc_id"): p.get("site") for p in d.pages}
    for s in d.sections or []:
        expected = page_site.get(s.get("doc_id"))
        if expected and s.get("site") != expected:
            problems.append(f"{s.get('section_id')}: site {s.get('site')!r}, its page is on "
                            f"{expected}")
    if problems:
        old = " (written by an older version? run `python -m askbear_scraper parse` again)" \
            if not any(p.get("site") for p in d.pages) else ""
        return check(name, FAIL, f"{len(problems):,} page/section record(s) with a missing or "
                                 "wrong `site`, or outside the crawl scope of sites.py (every "
                                 f"record must name the crawled website it comes from){old}",
                     problems)
    hosts = sorted({p["site"] for p in d.pages}, key=site_rank)
    return check(name, PASS, f"every page and section names its website: {', '.join(hosts)}")


def check_real_pages(d: Inputs) -> Check:
    name = "No error, login or off-site pages"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    bad: List[str] = []
    if d.manifest is not None:
        ok_urls: Set[str] = set()
        for url, e in d.manifest.items():
            if e.get("status") == "ok":
                ok_urls.add(url)
                ok_urls.add(normalize_url(e.get("final_url") or url) or url)
        for p in d.pages:
            url = p.get("url")
            if url in ok_urls:
                continue
            entry = d.manifest.get(url)
            status = entry.get("status") if entry else "not in the manifest"
            bad.append(f"{url}  (crawl status: {status})")
    for p in d.pages:
        title = p.get("page_title") or p.get("title") or ""
        if looks_like_not_found(p.get("title"), p.get("page_title"), p.get("word_count"),
                                text=p.get("text") or "") or \
                _NOT_FOUND_TITLE.search(p.get("page_title") or ""):
            bad.append(f"{p.get('url')}  (title {title!r})")
        elif (p.get("word_count") or 0) < SPARSE_MAX_WORDS and \
                (_LOGIN_TITLE.match(p.get("page_title") or "") or _LOGIN_TITLE.match(p.get("title") or "")):
            # a sign-in screen whose form is built by JavaScript (no password field in the HTML)
            bad.append(f"{p.get('url')}  (sign-in screen: title {title!r})")
    if bad:
        return check(name, FAIL, f"{plural(len(bad), 'page')} in {PAGES_FILE} {_are(len(bad))} not "
                                 "a saved page of a crawled site (soft 404, login page, redirect "
                                 "off-site, a 'Page not found' template, a web-server default "
                                 "page or an application error page)", bad)
    return check(name, PASS, f"all {len(d.pages):,} pages were saved with status ok; no 'Page not "
                             "found', sign-in, web-server default, error or off-site page was "
                             "parsed")


def check_page_content(d: Inputs) -> Check:
    name = "Page titles and text"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    no_title = [p.get("url") for p in d.pages if not (p.get("title") or "").strip()]
    no_text: Dict[str, List[str]] = defaultdict(list)
    totals = Counter(site_of(p) for p in d.pages)
    for p in d.pages:
        if not (p.get("text") or "").strip():
            no_text[site_of(p)].append(p.get("url"))
    hard = [h for h, urls in no_text.items()
            if (is_catalog_site(h) or (_profiled(h) and totals[h] >= MIN_PAGES_FOR_EMPTY_FAIL))
            and len(urls) > (MAX_EMPTY_CATALOG_SHARE if is_catalog_site(h)
                             else MAX_EMPTY_TEXT_SHARE) * totals[h]]
    offenders = [f"no title: {u}" for u in no_title] + \
        [f"no text: {u}" for h in sorted(no_text, key=site_rank) for u in no_text[h]]
    spread = ", ".join(f"{short_host(h)} {len(u):,} of {totals[h]:,}"
                       for h, u in sorted(no_text.items(), key=lambda kv: site_rank(kv[0])))
    if no_title or hard:
        return check(name, FAIL, f"{len(no_title)} page(s) without a title; pages without any "
                                 f"text: {spread or 'none'} (expected at most "
                                 f"{MAX_EMPTY_CATALOG_SHARE:.0%} on the catalog and "
                                 f"{MAX_EMPTY_TEXT_SHARE:.0%} on the other sites of sites.py: more "
                                 "means the parser misses their content)", offenders)
    if no_text:
        return check(name, WARN, f"all pages have a title; some pages have no text ({spread}): "
                                 "image galleries, script-only pages or empty templates; they get "
                                 "no passages (check a few on the live site)", offenders)
    return check(name, PASS, f"all {len(d.pages):,} pages have a title and text")


def check_page_titles(d: Inputs) -> Check:
    name = "Specific page titles"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    distinct = d.distinct_pages
    generic = []
    for p in distinct:
        title = _norm(p.get("title"))
        if p.get("page_type") in ("course_listing", "course_index"):
            continue        # titled after the subject, e.g. "NYIT" (/courses?cal=UG&cs=NYIT)
        if title in GENERIC_TITLES and not (p.get("page_type") == "home"
                                            and title in HOME_TITLES_OK):
            generic.append(f"{p.get('url')}  title {p.get('title')!r}")
    repeated = []
    per_site: Dict[str, Counter] = defaultdict(Counter)
    for p in distinct:
        if p.get("page_type") not in ("course_listing", "course_index"):
            per_site[site_of(p)][_norm(p.get("title"))] += 1
    for host in sorted(per_site, key=site_rank):
        counts = per_site[host]
        limit = max(REPEATED_TITLE_MIN, 0.05 * sum(counts.values()))
        repeated += [f"{host}: {n:,} pages titled {title!r}" for title, n in counts.most_common()
                     if title and n >= limit]
    if generic and (len(generic) >= 5 or len(generic) > 0.01 * len(distinct)):
        return check(name, FAIL, f"{plural(len(generic), 'page')} {_have(len(generic))} a generic "
                                 "title (template or university name instead of the page's own "
                                 "title: a title-extraction bug)", generic + repeated)
    if generic or repeated:
        return check(name, WARN, f"{len(generic)} page(s) with a generic title, {len(repeated)} "
                                 "title(s) shared by many pages of one website",
                     generic + repeated)
    return check(name, PASS, "no generic titles ('Staff & Faculty Directory', 'Page not found', "
                             "'New York Tech'...) and no title shared by many pages")


def check_page_metadata(d: Inputs) -> Check:
    name = "Page types and levels"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    bad = [f"{p.get('url')}  page_type={p.get('page_type')!r}" for p in d.pages
           if p.get("page_type") not in PAGE_TYPES]
    bad += [f"{p.get('url')}  level={p.get('level')!r}" for p in d.pages
            if p.get("level") not in PAGE_LEVELS]
    if bad:
        return check(name, FAIL, f"{len(bad)} invalid page_type/level value(s); allowed page "
                                 f"types: {', '.join(PAGE_TYPES)}", bad)
    distinct = d.distinct_pages
    types = Counter(p.get("page_type") for p in distinct)
    spread = ", ".join(f"{t} {n:,}" for t, n in types.most_common())
    per_site: Dict[str, List[dict]] = defaultdict(list)
    for p in distinct:
        per_site[site_of(p)].append(p)
    other, sites_ = [], []
    for host, recs in sorted(per_site.items(), key=lambda kv: site_rank(kv[0])):
        urls = [p.get("url") for p in recs if p.get("page_type") == "other"]
        if recs and len(urls) / len(recs) > MAX_OTHER_PAGE_SHARE:
            sites_.append(f"{host} {len(urls)} of {len(recs)}")
            other += urls
    if other:
        return check(name, WARN, f"many pages are classified as 'other' on "
                                 f"{', '.join(sites_)}; the page_type rules may need updating "
                                 f"({spread})", other)
    return check(name, PASS, f"all values valid ({spread})")


def check_short_pages(d: Inputs) -> Check:
    name = f"Short pages (< {SHORT_PAGE_WORDS} words)"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    # a course listing with one course is short by nature; courses are checked separately
    short = sorted(((site_rank(site_of(p)), p.get("word_count") or 0, p.get("url"))
                    for p in d.distinct_pages
                    if (p.get("word_count") or 0) < SHORT_PAGE_WORDS
                    and p.get("page_type") != "course_listing"))
    if short:
        per_site = Counter(url_host(url) for _, _, url in short)
        spread = ", ".join(f"{short_host(h)} {n:,}" for h, n in
                           sorted(per_site.items(), key=lambda kv: site_rank(kv[0])))
        return check(name, WARN, f"{plural(len(short), 'page')} {_have(len(short))} fewer than "
                                 f"{SHORT_PAGE_WORDS} words ({spread}); thank-you pages and link "
                                 "hubs are short by nature, others may hide content the parser "
                                 "missed", [f"{url}  ({n} words)" for _, n, url in short])
    return check(name, PASS, f"no page has fewer than {SHORT_PAGE_WORDS} words (course listings "
                             "are checked as courses)")


def check_duplicate_pages(d: Inputs) -> Check:
    name = "Duplicate pages"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    by_url = {p.get("url"): p for p in d.pages}
    dups = [p for p in d.pages if p.get("duplicate_of")]
    broken = [f"{p.get('url')} -> {p['duplicate_of']}" for p in dups
              if p["duplicate_of"] not in by_url]
    chained = [f"{p.get('url')} -> {p['duplicate_of']} -> {by_url[p['duplicate_of']]['duplicate_of']}"
               for p in dups if by_url.get(p["duplicate_of"], {}).get("duplicate_of")]
    if broken or chained:
        return check(name, FAIL, f"{len(broken)} duplicate_of value(s) point to a URL that is not "
                                 f"in {PAGES_FILE}, {len(chained)} to another copy", broken + chained)
    if not dups:
        return check(name, PASS, "no two pages have the same text")
    cross = sum(1 for p in dups if site_of(p) != site_of(by_url[p["duplicate_of"]]))
    note = f" ({cross:,} of them copies of a page on another website)" if cross else ""
    return check(name, PASS, f"{plural(len(dups), 'page')} with exactly the same text as another "
                             f"page{note}: kept in {PAGES_FILE} with duplicate_of set, left out of "
                             "sections and the corpus",
                 [f"{p.get('url')}  = copy of {p['duplicate_of']}" for p in dups])


def check_mirror_sites(d: Inputs) -> Check:
    name = "No copies of another website"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    by_url = {p.get("url"): p for p in d.pages}
    titles: Dict[str, Set[str]] = defaultdict(set)
    per_site: Dict[str, List[dict]] = defaultdict(list)
    for p in d.pages:
        per_site[site_of(p)].append(p)
        titles[site_of(p)].add(_norm(p.get("title")))
    mirrors = []
    for host, recs in sorted(per_site.items(), key=lambda kv: site_rank(kv[0])):
        if len(recs) < MIN_MIRROR_PAGES:
            continue
        shared: Counter = Counter()   # other website -> pages of `host` that repeat it
        for p in recs:
            original = by_url.get(p.get("duplicate_of") or "")
            if original is not None and site_of(original) != host:
                shared[site_of(original)] += 1
                continue
            title = _norm(p.get("title"))
            for other in titles:
                if other != host and title and title in titles[other] \
                        and site_rank(other) < site_rank(host):
                    shared[other] += 1
                    break
        for other, n in shared.items():
            if n >= MIRROR_SHARE * len(recs):
                mirrors.append(f"{host}: {n:,} of its {len(recs):,} pages are copies of (or have "
                               f"the same title as) pages of {other}")
    if mirrors:
        return check(name, WARN, f"{plural(len(mirrors), 'website')} {_appear(len(mirrors))} to "
                                 "copy another NYIT website (a development or test copy?); its "
                                 "pages add near-duplicate passages: exclude it in sites.py",
                     mirrors)
    return check(name, PASS, f"no website repeats most of another website's pages "
                             f"({plural(len(per_site), 'website')} compared)")


def check_noindex(d: Inputs) -> Check:
    name = "noindex pages kept out of the index"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    noindex = {p.get("doc_id"): p.get("url") for p in d.pages if p.get("noindex")}
    if not noindex:
        return check(name, PASS, "no parsed page asks search engines not to index it")
    problems = [f"section {s.get('section_id')} of noindex page {noindex[s.get('doc_id')]}"
                for s in d.sections or [] if s.get("doc_id") in noindex]
    problems += [f"corpus record {r.get('id')} of noindex page "
                 f"{noindex[(r.get('metadata') or {}).get('doc_id')]}"
                 for r in d.corpus or [] if (r.get("metadata") or {}).get("doc_id") in noindex]
    if problems:
        return check(name, FAIL, f"{len(problems):,} section/corpus record(s) come from pages "
                                 "marked noindex", problems)
    per_site = Counter(url_host(u) for u in noindex.values())
    spread = ", ".join(f"{short_host(h)} {n:,}" for h, n in
                       sorted(per_site.items(), key=lambda kv: site_rank(kv[0])))
    return check(name, PASS, f"{plural(len(noindex), 'noindex page')} ({spread}) {_are(len(noindex))} "
                             f"in {PAGES_FILE} but have no sections or corpus records",
                 sorted(noindex.values()))


def check_dates(d: Inputs) -> Check:
    name = "Publication dates (news, events...)"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    by_type: Dict[str, List[dict]] = defaultdict(list)
    for p in d.distinct_pages:
        if p.get("page_type") in DATED_PAGE_TYPES:
            by_type[p["page_type"]].append(p)
    if not by_type:
        return check(name, SKIP, "no news, event, blog or magazine pages parsed")
    parts, low, offenders = [], [], []
    for page_type in DATED_PAGE_TYPES:
        recs = by_type.get(page_type)
        if not recs:
            continue
        # an event is dated by when it takes place (event_start); older datasets kept that
        # in ``published``
        field_name = "event_start" if page_type == "event" else "published"
        dated = [p for p in recs if _page_date(p, page_type)]
        bad = [p for p in dated if not re.match(r"^\d{4}-\d{2}-\d{2}",
                                                 str(_page_date(p, page_type)))]
        share = len(dated) / len(recs)
        parts.append(f"{page_type} {share:.0%} of {len(recs):,}")
        offenders += [f"{p.get('url')}  {field_name}={_page_date(p, page_type)!r} (not ISO)"
                      for p in bad]
        if len(recs) >= 5 and share < MIN_DATED_SHARE:
            low.append(page_type)
            offenders += [f"{p.get('url')}  ({page_type}, no date)" for p in recs
                          if not _page_date(p, page_type)]
    msg = "pages with a date (publication date; events: when they take place): " + ", ".join(parts)
    if low or offenders:
        what = f"; less than {MIN_DATED_SHARE:.0%} for {', '.join(low)}" if low else ""
        return check(name, WARN, msg + what + " (dates help to rank recent news and events)",
                     offenders)
    return check(name, PASS, msg)


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------
def _page_date(page: dict, page_type: str) -> Optional[str]:
    if page_type == "event":
        return page.get("event_start") or page.get("published")
    return page.get("published")


def check_catalog_year(d: Inputs) -> Check:
    name = "Catalog year"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    catalog = [p for p in d.pages if is_catalog_site(site_of(p))]
    if not catalog:
        return check(name, SKIP, f"no {CATALOG_HOST} pages parsed")
    records = [("page", p.get("url"), p.get("catalog_year")) for p in catalog]
    records += [("course", c.get("course_id"), c.get("catalog_year")) for c in d.courses or []]
    years = Counter(y or "" for _, _, y in records)
    main = next((y for y, _ in years.most_common() if y), "")
    if not main:
        return check(name, WARN, "no catalog page shows a catalog year (expected e.g. 2026-2027)")
    odd = [f"{kind} {rid}: {y or '(empty)'}" for kind, rid, y in records if y != main]
    leaked = [f"page {p.get('url')}: {p.get('catalog_year')}" for p in d.pages
              if p.get("catalog_year") and not is_catalog_site(site_of(p))]
    if odd or leaked:
        return check(name, WARN, f"{plural(len(odd), 'catalog record')} with another catalog year "
                                 f"than the common {main}; {len(leaked)} page(s) of other sites "
                                 "with a catalog year", odd + leaked)
    return check(name, PASS, f"{main} on all {len(catalog):,} catalog pages"
                             + (f" and {len(d.courses):,} courses" if d.courses else ""))


def check_schools(d: Inputs) -> Check:
    name = "Every school has catalog pages"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    catalog = Counter(p.get("school") for p in d.distinct_pages if is_catalog_site(site_of(p)))
    others = Counter(p.get("school") for p in d.distinct_pages
                     if not is_catalog_site(site_of(p)) and p.get("school"))
    schools = list(config.SCHOOL_BY_PATH.values())
    missing = [s for s in schools if not catalog.get(s)]
    detail = ", ".join(f"{_short_school(s)} {catalog.get(s, 0):,}" for s in schools)
    if others:
        detail += f"; pages of other sites with a school: {sum(others.values()):,}"
        unknown = sorted(s for s in others if s not in schools)
        if unknown:
            detail += f" (names outside the official six: {', '.join(unknown[:5])})"
    if missing:
        if not _crawl_complete(d) or _crawl_running(d) or not catalog:
            return check(name, WARN, f"{len(missing)} of {len(schools)} schools have no catalog "
                                     f"pages yet; the crawl is not finished: {detail}", missing)
        if _crawl_capped(d, CATALOG_HOST):
            return check(name, WARN, f"{len(missing)} of {len(schools)} schools have no catalog "
                                     "pages: the crawl stopped at its page limit (--max-pages or "
                                     f"sites.py max_pages), so the catalog is incomplete: {detail}",
                         missing)
        return check(name, FAIL, f"{len(missing)} of {len(schools)} schools have no catalog pages "
                                 f"although the crawl finished (school detection broken?): "
                                 f"{detail}", missing)
    return check(name, PASS, f"all {len(schools)} schools have catalog pages: {detail}")


def _crawl_complete(d: Inputs) -> bool:
    """The last crawl ran to the end (crawl_summary.json, not interrupted, empty queue)."""
    s = d.crawl_summary
    return isinstance(s, dict) and not s.get("interrupted") and not s.get("still_queued")


def _crawl_capped(d: Inputs, host: str) -> bool:
    """The last crawl stopped crawling ``host`` at a page limit (a quick test run)."""
    s = d.crawl_summary if isinstance(d.crawl_summary, dict) else {}
    return host in (s.get("capped_sites") or {})


def _crawl_running(d: Inputs) -> bool:
    """Pages were fetched after the last crawl_summary.json was written (a new crawl)."""
    if d.manifest is None or not isinstance(d.crawl_summary, dict):
        return False
    newest = max((e.get("fetched_at") or "" for e in d.manifest.values()), default="")
    return _later(newest, d.crawl_summary.get("finished_at"))


def check_program_metadata(d: Inputs) -> List[Check]:
    return [_program_fields(d, "Program metadata (catalog)", ("program", "curriculum"),
                            MIN_PROGRAM_FIELD_SHARE, catalog_only=True),
            _program_fields(d, "Degree pages metadata (other sites)", ("degree",),
                            MIN_DEGREE_FIELD_SHARE, catalog_only=False)]


def _program_fields(d: Inputs, name: str, page_types: Sequence[str], min_share: float,
                    catalog_only: bool) -> Check:
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    progs = [p for p in d.distinct_pages if p.get("page_type") in page_types
             and is_catalog_site(site_of(p)) == catalog_only]
    if not progs:
        return check(name, SKIP, f"no {'/'.join(page_types)} pages parsed yet")
    missing = {f: [p.get("url") for p in progs if not p.get(f)]
               for f in ("program", "degree", "level")}
    shares = {f: 1 - len(urls) / len(progs) for f, urls in missing.items()}
    what = "program/curriculum pages" if catalog_only else "degree pages"
    msg = (f"{len(progs):,} {what}: program name on {shares['program']:.0%}, "
           f"degree on {shares['degree']:.0%}, level on {shares['level']:.0%}")
    low = [f for f, share in shares.items() if share < min_share]
    if low:
        offenders = [f"no {f}: {u}" for f in low for u in missing[f]]
        return check(name, WARN, msg + f" (expected at least {min_share:.0%})", offenders)
    return check(name, PASS, msg)


def check_course_coverage(d: Inputs) -> Check:
    name = "Course listings produced courses"
    if not d.pages:
        return _no(name, PAGES_FILE, d.pages)
    summary = d.parse_summary or {}
    if "empty_course_listings" in summary:
        total = summary.get("course_listing_pages") or 0
        empty = list(summary["empty_course_listings"])
    else:  # older parse summary: compare listing pages with the courses' source pages
        listings = [p.get("url") for p in d.pages if p.get("page_type") == "course_listing"]
        with_courses = {c.get("url") for c in d.courses or []}
        total, empty = len(listings), sorted(u for u in listings if u not in with_courses)
    if not total:
        return check(name, SKIP, "no course listing pages (/courses?cs=...&cal=...) crawled yet")
    if empty:
        return check(name, WARN, f"{len(empty)} of {total:,} course listing pages produced no "
                                 "courses (empty subject on the site, or a parser miss)", empty)
    return check(name, PASS, f"all {total:,} course listing pages produced courses")


def check_course_fields(d: Inputs) -> Check:
    name = "Course fields"
    if d.courses is None:
        return _no(name, COURSES_FILE)
    if not d.courses:
        listings = (d.parse_summary or {}).get("course_listing_pages")
        status = FAIL if listings else SKIP
        return check(name, status, f"{COURSES_FILE} is empty" +
                     (f" although {listings} listing pages were parsed" if listings else ""))
    severe: List[str] = []
    odd: List[str] = []
    letter_codes: List[str] = []
    variants: List[str] = []
    for c in d.courses:
        cid = c.get("course_id") or "(no course_id)"
        code, level = c.get("code") or "", c.get("level")
        if not (c.get("title") or "").strip():
            severe.append(f"{cid}: empty title")
        if COURSE_LEVELS.get(level) is None or COURSE_LEVELS[level] != c.get("level_code"):
            severe.append(f"{cid}: level={level!r} level_code={c.get('level_code')!r}")
        base_id = f"{code}|{c.get('level_code')}"
        if cid != base_id:
            # "MTIM 893|PROF|2": the site uses one code for several different courses
            if re.fullmatch(re.escape(base_id) + r"\|\d+", cid):
                variants.append(cid)
            else:
                severe.append(f"{cid}: course_id does not match code|level_code")
        if not COURSE_CODE.match(code):
            if COURSE_CODE_LETTERS.match(code):
                letter_codes.append(code)
            else:
                odd.append(f"{cid}: unusual code {code!r}")
        if c.get("subject") and code.split(" ")[0] != c.get("subject"):
            odd.append(f"{cid}: subject {c.get('subject')!r} does not match the code")
        if not _credits_ok(c):
            odd.append(f"{cid}: credits {c.get('credits')!r} -> "
                       f"{c.get('credits_min')!r}..{c.get('credits_max')!r}")
    dup_ids = _repeated(c.get("course_id") for c in d.courses)
    severe += [f"{i}: course_id appears more than once" for i in dup_ids]
    n = len(d.courses)
    if severe:
        return check(name, FAIL, f"{len(severe)} problem(s) in {n:,} courses (missing title, bad "
                                 "level, duplicate or inconsistent course_id)", severe + odd)
    if odd:
        status = FAIL if len(odd) > MAX_BAD_COURSE_SHARE * n else WARN
        return check(name, status, f"{len(odd)} of {n:,} courses have an unusual code, subject or "
                                   "credit value; check whether the site really prints them like "
                                   "that", odd)
    note = ""
    if letter_codes:
        examples = ", ".join(sorted(set(letter_codes))[:3])
        note += (f"; {len(letter_codes)} codes use letters in the number part as printed on the "
                 f"site (e.g. {examples})")
    if variants:
        note += (f"; {len(variants)} course_ids end in |2, |3... because the site uses the same "
                 f"code for different courses (e.g. {variants[0]})")
    return check(name, PASS, f"{n:,} courses: codes, titles, levels, course_ids and credits valid"
                             + note)


def _credits_ok(course: dict) -> bool:
    raw = str(course.get("credits") or "")
    lo, hi = course.get("credits_min"), course.get("credits_max")
    if not re.search(r"\d", raw):       # "", "Variable": nothing to parse
        return True
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return False
    return 0 <= lo <= hi <= MAX_CREDITS


def check_course_descriptions(d: Inputs) -> Check:
    name = "Course descriptions"
    if not d.courses:
        return _no(name, COURSES_FILE, d.courses)
    n = len(d.courses)
    without = [f"{c.get('course_id')}  {c.get('url')}" for c in d.courses
               if not (c.get("description") or "").strip()]
    share = 1 - len(without) / n
    prereq = sum(1 for c in d.courses if c.get("prerequisites")) / n
    per_level = []
    for code in ("UG", "GR", "PROF"):
        recs = [c for c in d.courses if c.get("level_code") == code]
        if recs:
            have = sum(1 for c in recs if (c.get("description") or "").strip())
            per_level.append(f"{code} {have / len(recs):.0%}")
    msg = (f"{share:.1%} of {n:,} courses have a description ({', '.join(per_level)}); "
           f"{prereq:.1%} list prerequisites")
    if share < MIN_DESCRIPTION_SHARE:
        return check(name, WARN, msg + f". Expected at least {MIN_DESCRIPTION_SHARE:.0%}; the site "
                                       "itself shows no description for some courses", without)
    return check(name, PASS, msg)


def check_course_links(d: Inputs) -> Check:
    name = "Course deep links"
    courses = [r for r in d.corpus or [] if r.get("source") == "course"]
    if not courses or d.manifest is None:
        return check(name, SKIP, f"needs course records in {CORPUS_FILE} and the crawl cache")
    files = _cached_files(d.manifest)
    keys: Dict[str, Set[str]] = {}
    broken: List[str] = []
    checked = 0
    for r in courses:
        parts = urlsplit(r.get("url") or "")
        params = parse_qsl(parts.query, keep_blank_values=True)
        course_key = dict(params).get("cc")
        if not course_key:
            continue
        others = urlencode([(k, v) for k, v in params if k != "cc"])
        listing = normalize_url(parts._replace(query=others).geturl()) or ""
        if listing not in keys:
            html = _cached_html(d.settings.html_dir, files.get(listing))
            keys[listing] = set(_DATA_COURSE.findall(html))
        if not keys[listing]:
            continue
        checked += 1
        if course_key not in keys[listing]:
            broken.append(f"{r.get('id')}  {r.get('url')}")
    if not checked:
        return check(name, SKIP, "no course link with a cc= parameter could be checked")
    if broken:
        return check(name, WARN, f"{len(broken)} of {checked:,} course links name a course that "
                                 "is not on their listing page (the link opens the listing "
                                 "without expanding the course)", broken)
    return check(name, PASS, f"all {checked:,} course links open their listing page with the "
                             "course description expanded (?cc=SUBJ-NUM)")


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def check_sections(d: Inputs) -> Check:
    name = "Sections: ids and text"
    if d.sections is None:
        return _no(name, SECTIONS_FILE)
    if not d.sections:
        return check(name, FAIL if d.pages else SKIP, f"{SECTIONS_FILE} is empty")
    page_by_id = {p.get("doc_id"): p for p in d.pages or []}
    problems = [f"{sid}: section_id appears more than once"
                for sid in _repeated(s.get("section_id") for s in d.sections)]
    for s in d.sections:
        sid = s.get("section_id")
        page = page_by_id.get(s.get("doc_id"))
        if not (s.get("text") or "").strip():
            problems.append(f"{sid}: empty text")
        if d.pages is not None and page is None:
            problems.append(f"{sid}: its doc_id is not in {PAGES_FILE}")
        elif page and page.get("duplicate_of"):
            problems.append(f"{sid}: belongs to a duplicate page")
        elif page and page.get("page_type") == "course_listing":
            problems.append(f"{sid}: belongs to a course listing page")
        elif page and (page.get("word_count") or 0) < MIN_INDEXED_WORDS:
            problems.append(f"{sid}: belongs to a page with fewer than {MIN_INDEXED_WORDS} words")
        elif page and is_listing_continuation(page.get("page_type"), page.get("url") or ""):
            problems.append(f"{sid}: belongs to page 2+ of a list of posts (?p=N)")
    if problems:
        return check(name, FAIL, f"{len(problems)} problem(s) in {len(d.sections):,} sections",
                     problems)
    per_site = Counter(site_of(s) for s in d.sections)
    spread = ", ".join(f"{short_host(h)} {n:,}" for h, n in
                       sorted(per_site.items(), key=lambda kv: site_rank(kv[0])))
    return check(name, PASS, f"{len(d.sections):,} sections ({spread}), ids unique, none empty")


def check_section_length(d: Inputs) -> Check:
    name = f"Section length (<= {LONG_SECTION_WORDS:,} words)"
    if not d.sections:
        return _no(name, SECTIONS_FILE, d.sections)
    words = sorted(((s.get("word_count") or 0, s.get("section_id"), s.get("url"))
                    for s in d.sections), key=lambda x: -x[0])
    long = [f"{sid}  ({n:,} words)  {url}" for n, sid, url in words if n > LONG_SECTION_WORDS]
    median = sorted(w[0] for w in words)[len(words) // 2]
    if long:
        per_site = Counter(url_host(url) for n, _, url in words if n > LONG_SECTION_WORDS)
        spread = ", ".join(f"{short_host(h)} {n:,}" for h, n in per_site.most_common())
        what = f"{plural(len(long), 'section')} {_are(len(long))}"
        return check(name, WARN, f"{what} longer than {LONG_SECTION_WORDS:,} words ({spread}; "
                                 f"longest {words[0][0]:,}); very long passages are hard to rank",
                     long)
    return check(name, PASS, f"longest section has {words[0][0]:,} words (median {median:,})")


def check_section_anchors(d: Inputs) -> Check:
    name = "Section #anchor links"
    if not d.sections or d.manifest is None:
        return check(name, SKIP, f"needs {SECTIONS_FILE} and the crawl cache")
    with_anchor = [s for s in d.sections if "#" in (s.get("url") or "")]
    if not with_anchor:
        return check(name, WARN, "no section has an #anchor link, so every result will open at "
                                 "the top of its page")
    files = _cached_files(d.manifest)
    pages_by_site: Dict[str, List[str]] = defaultdict(list)
    for s in with_anchor:
        page_url = s["url"].partition("#")[0]
        pages_by_site[url_host(page_url)].append(page_url)
    sampled: Set[str] = set()
    sampled_sites = []
    for host, urls in pages_by_site.items():
        unique = sorted(set(urls))
        if len(unique) > MAX_ANCHOR_PAGES_PER_SITE:
            step = len(unique) / MAX_ANCHOR_PAGES_PER_SITE
            unique = [unique[int(i * step)] for i in range(MAX_ANCHOR_PAGES_PER_SITE)]
            sampled_sites.append(host)
        sampled.update(unique)
    targets: Dict[str, Set[str]] = {}
    broken: List[str] = []
    checked = 0
    for s in with_anchor:
        page_url, _, anchor = s["url"].partition("#")
        if page_url not in sampled:
            continue
        if page_url not in targets:
            targets[page_url] = _page_targets(d.settings.html_dir, files.get(page_url))
        if not targets[page_url]:
            continue
        checked += 1
        if anchor not in targets[page_url] and unquote(anchor) not in targets[page_url]:
            broken.append(f"{s.get('section_id')}  {s['url']}")
    if not checked:
        return check(name, SKIP, "the cached HTML of these pages is not available")
    note = (f" (on {', '.join(short_host(h) for h in sorted(sampled_sites, key=site_rank))} a "
            f"sample of {MAX_ANCHOR_PAGES_PER_SITE} pages was checked)" if sampled_sites else "")
    if broken:
        return check(name, WARN, f"{len(broken)} of {checked:,} section links point to an #anchor "
                                 "that does not exist in the page's HTML (the link still opens "
                                 f"the page, just not at the right spot){note}", broken)
    return check(name, PASS, f"all {checked:,} section #anchor links exist in their page's "
                             f"HTML{note}")


def _page_targets(html_dir: Path, filename: Optional[str]) -> Set[str]:
    """Every id="..." and name="..." in a cached page (the possible #anchor targets)."""
    html = _cached_html(html_dir, filename)
    return {next(g for g in m.groups() if g is not None) for m in _ID_OR_NAME.finditer(html)}


def check_boilerplate_leakage(d: Inputs) -> Check:
    name = "Boilerplate in passages"
    if not d.sections:
        return _no(name, SECTIONS_FILE, d.sections)
    per_site: Dict[str, List[dict]] = defaultdict(list)
    for s in d.sections:
        per_site[site_of(s)].append(s)
    leaks: List[Tuple[str, float, int, str]] = []
    checked = []
    for host, secs in sorted(per_site.items(), key=lambda kv: site_rank(kv[0])):
        if len(secs) < MIN_LEAK_SECTIONS:
            continue
        checked.append(host)
        df: Counter = Counter()
        for s in secs:
            df.update({line for line in (_norm(x) for x in (s.get("text") or "").split("\n"))
                       if len(line.split()) >= MIN_LEAK_WORDS})
        for line, n in df.items():
            if n > BOILERPLATE_SHARE * len(secs):
                leaks.append((host, n / len(secs), len(secs), line))
    if not checked:
        return check(name, SKIP, f"no website has {MIN_LEAK_SECTIONS} passages yet")
    if leaks:
        leaks.sort(key=lambda x: (site_rank(x[0]), -x[1]))
        per_site_n = Counter(h for h, _, _, _ in leaks)
        spread = ", ".join(f"{short_host(h)} {n}" for h, n in per_site_n.items())
        return check(name, WARN, f"{plural(len(leaks), 'paragraph')} {_appear(len(leaks))} in "
                                 f"more than {BOILERPLATE_SHARE:.0%} of a website's passages "
                                 f"({spread}): boilerplate the parser did not remove, or a field "
                                 "that many pages share (it adds little to ranking)",
                     [f"{h}: in {share:.0%} of {n:,} passages: \"{_cut(line, 90)}\""
                      for h, share, n, line in leaks])
    return check(name, PASS, f"no paragraph repeats in more than {BOILERPLATE_SHARE:.0%} of a "
                             f"website's passages ({plural(len(checked), 'website')} checked)")


# ---------------------------------------------------------------------------
# text hygiene
# ---------------------------------------------------------------------------
def hygiene_check(label: str, records: Optional[List[dict]], id_key: str,
                  fields_: Sequence[str], status_if_dirty: str, missing_file: str,
                  command: str = "parse", per_site: bool = False,
                  extra_patterns: Sequence[Tuple[str, Any]] = ()) -> Check:
    """Leftover HTML, entities, noise links, ligatures, broken, control and private-use
    characters (+ ``extra_patterns``).

    With ``per_site``, leftovers on the catalog are a FAIL; on the other websites of sites.py
    they are a FAIL only when more than MAX_DIRTY_SHARE of the site's records have them (a
    parser bug rather than odd text that NYIT itself publishes); on websites found through
    links they are a WARN."""
    name = f"Text hygiene: {label}"
    if not records:
        return _no(name, missing_file, records, command)
    found: Counter = Counter()
    offenders: List[str] = []
    dirty: Counter = Counter()
    totals: Counter = Counter()
    for rec in records:
        hits = set()
        site = site_of(rec)
        totals[site] += 1
        for key in fields_:
            text = _as_text(rec.get(key))
            for what, pattern in tuple(HYGIENE_PATTERNS) + tuple(extra_patterns):
                m = pattern.search(text) if text else None
                if m and what not in hits:
                    hits.add(what)
                    offenders.append(f"{rec.get(id_key)} [{key}] {what}: "
                                     f"...{_snippet(text, m.start(), m.end())}...")
        found.update(hits)
        dirty[site] += 1 if hits else 0
    if not found:
        extra = "".join(f", {what}s" for what, _ in extra_patterns)
        return check(name, PASS, f"{len(records):,} records checked, no leftover HTML, entities, "
                                 "noise links, ligatures, broken, control or private-use "
                                 f"characters{extra}")
    detail = ", ".join(f"{what} in {n:,}" for what, n in found.most_common())
    status = status_if_dirty
    if per_site:
        hard = [h for h, n in dirty.items()
                if n and (is_catalog_site(h) or (_profiled(h) and n > MAX_DIRTY_SHARE * totals[h]))]
        status = FAIL if hard else WARN
        spread = ", ".join(f"{short_host(h)} {n:,} of {totals[h]:,}" for h, n in
                           sorted(dirty.items(), key=lambda kv: site_rank(kv[0])) if n)
        detail += f"; per website: {spread}"
    return check(name, status, f"leftovers in {sum(dirty.values()):,} of {len(records):,} records: "
                               f"{detail}", offenders)


def check_hygiene_pages(d: Inputs) -> Check:
    return hygiene_check("pages", d.pages, "url", ("title", "headings", "text"), FAIL,
                         PAGES_FILE, per_site=True)


def check_hygiene_sections(d: Inputs) -> Check:
    return hygiene_check("sections", d.sections, "section_id", ("heading", "heading_path", "text"),
                         FAIL, SECTIONS_FILE, per_site=True)


def check_hygiene_courses(d: Inputs) -> Check:
    return hygiene_check("courses", d.courses, "course_id",
                         ("title", "description", "prerequisites", "corequisites", "hours",
                          "attributes", "text"), FAIL, COURSES_FILE)


def check_hygiene_pdf(d: Inputs) -> Check:
    # PDF text comes from NYIT's files, not from our HTML parser: a WARN, not a FAIL
    return hygiene_check("catalog PDF pages", d.pdf_pages, "doc_id", ("text", "section"), WARN,
                         PDF_PAGES_FILE, command="pdf")


def check_hygiene_documents(d: Inputs) -> Check:
    return hygiene_check("PDF documents", d.documents, "doc_id", ("title", "toc_path", "text"),
                         WARN, DOCUMENTS_FILE, command="docs", extra_patterns=PDF_HYGIENE_PATTERNS)


def _as_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def _snippet(text: str, start: int, end: int, context: int = 30) -> str:
    return text[max(0, start - context):end + context].replace("\n", " / ")


# ---------------------------------------------------------------------------
# catalog PDFs
# ---------------------------------------------------------------------------
def check_pdf_page_counts(d: Inputs) -> Check:
    name = "PDF pages match pdf_summary"
    if not d.pdf_pages and not pdf_summary_items(d.pdf_summary):
        return check(name, SKIP, "no PDF data (optional): `python -m askbear_scraper pdf "
                                 "--edition 2025-2026` reproduces the proposal's 581 pages")
    if d.pdf_pages is None:
        return check(name, FAIL, f"{PDF_SUMMARY_FILE} exists but {PDF_PAGES_FILE} does not: run "
                                 "`python -m askbear_scraper pdf` again")
    pages_by_pdf: Dict[Tuple[str, str], List[int]] = {}
    declared: Dict[Tuple[str, str], set] = {}
    for rec in d.pdf_pages:
        key = (str(rec.get("edition")), str(rec.get("catalog")))
        pages_by_pdf.setdefault(key, []).append(rec.get("page"))
        declared.setdefault(key, set()).add(rec.get("page_count"))
    problems: List[str] = []
    summary = {(str(i.get("edition")), str(i.get("catalog") or i.get("kind"))): i
               for i in pdf_summary_items(d.pdf_summary)}
    for key, item in sorted(summary.items()):
        got = len(pages_by_pdf.get(key, []))
        if item.get("pages") != got:
            problems.append(f"{key[0]} {key[1]}: {PDF_SUMMARY_FILE} says {item.get('pages')} "
                            f"pages, {PDF_PAGES_FILE} has {got}")
    for key, pages in sorted(pages_by_pdf.items()):
        if summary and key not in summary:
            problems.append(f"{key[0]} {key[1]}: in {PDF_PAGES_FILE} but not in {PDF_SUMMARY_FILE}")
        if sorted(pages) != list(range(1, len(pages) + 1)):
            problems.append(f"{key[0]} {key[1]}: page numbers are not 1..{len(pages)} without gaps")
        if declared[key] != {len(pages)}:
            problems.append(f"{key[0]} {key[1]}: page_count {sorted(declared[key], key=str)} but "
                            f"{len(pages)} page records")
    if problems:
        return check(name, FAIL, f"{len(problems)} mismatch(es) between the PDF files", problems)
    detail = ", ".join(f"{e} {c} {len(p):,}" for (e, c), p in sorted(pages_by_pdf.items()))
    note = "" if summary else f" ({PDF_SUMMARY_FILE} missing, page numbers checked only)"
    return check(name, PASS, f"{len(pages_by_pdf)} PDFs, every page present: {detail}{note}")


def check_pdf_empty_pages(d: Inputs) -> Check:
    name = f"PDF empty pages (< {MAX_EMPTY_PDF_SHARE:.0%})"
    if not d.pdf_pages:
        return check(name, SKIP, "no PDF pages extracted")
    per_pdf: Dict[Tuple[str, str], List[dict]] = {}
    for rec in d.pdf_pages:
        per_pdf.setdefault((str(rec.get("edition")), str(rec.get("catalog"))), []).append(rec)
    empty = [r for r in d.pdf_pages if not r.get("word_count")]
    too_many = []
    for (edition, catalog), recs in sorted(per_pdf.items()):
        n_empty = sum(1 for r in recs if not r.get("word_count"))
        if n_empty > MAX_EMPTY_PDF_SHARE * len(recs):
            too_many.append(f"{edition} {catalog}: {n_empty} of {len(recs)} pages")
    msg = f"{len(empty)} of {len(d.pdf_pages):,} PDF pages have no text"
    if too_many:
        return check(name, WARN, msg + "; too many in: " + "; ".join(too_many) +
                     " (scanned images or a text-extraction problem)",
                     [f"{r.get('doc_id')}  {r.get('url')}" for r in empty])
    return check(name, PASS, (msg + " (cover or blank pages)") if empty else msg)


# ---------------------------------------------------------------------------
# documents found on the NYIT sites (`docs`): PDFs and Word/Excel/PowerPoint files
# (``file_type``; ``pdf_url`` is the document's URL whatever its type)
# ---------------------------------------------------------------------------
def _is_pdf(rec: dict) -> bool:
    """A documents.jsonl page of a PDF (records written before Office files were read have
    no ``file_type``: they are all PDFs)."""
    return str(rec.get("file_type") or "pdf") == "pdf"


def check_document_pages(d: Inputs) -> Check:
    name = "PDF documents: every page present"
    items = document_summary_items(d.documents_summary)
    if d.documents is None and not items and d.docs_manifest is None:
        return check(name, SKIP, "no PDF documents (optional): run `python -m askbear_scraper "
                                 "docs` after the crawl, then `parse` again")
    if d.documents is None:
        return check(name, FAIL, f"the `docs` step ran but there is no {DOCUMENTS_FILE}: run "
                                 "`python -m askbear_scraper docs` again")
    problems: List[str] = []
    by_pdf: Dict[str, List[dict]] = defaultdict(list)
    for rec in d.documents:
        pdf_url = str(rec.get("pdf_url") or "")
        by_pdf[pdf_url].append(rec)
        page = rec.get("page")
        if not pdf_url or not sites.is_nyit_host(url_host(pdf_url)):
            problems.append(f"{rec.get('doc_id')}: pdf_url {pdf_url!r} is not on an NYIT website")
        if str(rec.get("file_type") or "pdf") not in ("pdf", "docx", "xlsx", "pptx"):
            problems.append(f"{rec.get('doc_id')}: unknown file_type {rec.get('file_type')!r}")
        if rec.get("url") != f"{pdf_url}#page={page}":
            problems.append(f"{rec.get('doc_id')}: url {rec.get('url')!r} is not pdf_url#page={page}")
        if rec.get("site") and rec["site"] != url_host(pdf_url):
            problems.append(f"{rec.get('doc_id')}: site {rec['site']!r} is not the PDF's host")
    problems += [f"{i}: doc_id appears more than once"
                 for i in _repeated(r.get("doc_id") for r in d.documents)]
    for pdf_url, recs in sorted(by_pdf.items()):
        numbers = sorted(r.get("page") or 0 for r in recs)
        if numbers != list(range(1, len(recs) + 1)):
            problems.append(f"{pdf_url}: page numbers are not 1..{len(recs)} without gaps")
        if {r.get("page_count") for r in recs} != {len(recs)}:
            problems.append(f"{pdf_url}: page_count {sorted({r.get('page_count') for r in recs}, key=str)}"
                            f" but {len(recs)} page records")
    mismatched = []
    for item in items:
        url = item.get("pdf_url") or item.get("url")
        pages = item.get("pages") if isinstance(item.get("pages"), int) else item.get("page_count")
        if url not in by_pdf or not isinstance(pages, int):
            continue
        if pages != len(by_pdf[url]):
            mismatched.append(f"{url}: {DOCUMENTS_SUMMARY_FILE} says {pages} pages, "
                              f"{DOCUMENTS_FILE} has {len(by_pdf[url])}")
    if problems or mismatched:
        return check(name, FAIL, f"{len(problems) + len(mismatched)} problem(s) in "
                                 f"{DOCUMENTS_FILE} ({len(by_pdf):,} documents)",
                     problems + mismatched)
    compared = sum(1 for i in items if (i.get("pdf_url") or i.get("url")) in by_pdf)
    note = (f"; page counts agree with {DOCUMENTS_SUMMARY_FILE} for {compared:,} documents"
            if items else f"; {DOCUMENTS_SUMMARY_FILE} missing or without per-document entries")
    types = Counter(str(recs[0].get("file_type") or "pdf") for recs in by_pdf.values())
    kinds = (" (" + ", ".join(f"{t} {n:,}" for t, n in sorted(types.items())) + ")"
             if set(types) - {"pdf"} else "")
    return check(name, PASS, f"{len(by_pdf):,} documents{kinds}, {len(d.documents):,} pages, "
                             "page numbers complete, ids unique, every page links to "
                             f"pdf_url#page=N{note}")


def check_document_text(d: Inputs) -> Check:
    name = "PDF documents: pages with text"
    if not d.documents:
        return _no(name, DOCUMENTS_FILE, d.documents, command="docs")
    by_pdf: Dict[str, List[dict]] = defaultdict(list)
    for rec in d.documents:
        by_pdf[str(rec.get("pdf_url"))].append(rec)
    empty = [r for r in d.documents if not r.get("word_count")]
    # the `docs` step's verdict when documents_summary.json has it (most pages are images
    # without text), else: no page has text. Only a PDF can be a scan: an Office file
    # without text (slides that are only pictures) is not counted.
    verdicts = {str(i.get("pdf_url") or i.get("url")): bool(i.get("scanned"))
                for i in document_summary_items(d.documents_summary) if "scanned" in i}
    scanned = sorted(u for u, recs in by_pdf.items() if _is_pdf(recs[0])
                     and verdicts.get(u, all(not r.get("word_count") for r in recs)))
    share = len(empty) / len(d.documents)
    msg = (f"{len(empty):,} of {len(d.documents):,} document pages ({share:.1%}) have no text; "
           f"{plural(len(scanned), 'PDF')} {_are(len(scanned))} scanned images (most pages are "
           f"pictures without a text layer; kept in {DOCUMENTS_FILE}, their empty pages are not "
           "indexed)")
    if share > MAX_EMPTY_DOCUMENT_SHARE:
        return check(name, WARN, msg + f"; more than {MAX_EMPTY_DOCUMENT_SHARE:.0%} (OCR would be "
                                       "needed to index them)",
                     [f"scanned: {u}" for u in scanned]
                     + [f"{r.get('doc_id')}  {r.get('url')}" for r in empty])
    return check(name, PASS, msg, [f"scanned: {u}" for u in scanned])


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------
def check_corpus(d: Inputs) -> Check:
    name = "Corpus records"
    if d.corpus is None:
        return _no(name, CORPUS_FILE)
    if not d.corpus:
        return check(name, FAIL, f"{CORPUS_FILE} is empty")
    problems = [f"{i}: id appears more than once" for i in _repeated(r.get("id") for r in d.corpus)]
    for r in d.corpus:
        rid = r.get("id")
        url = r.get("url") or ""
        meta = r.get("metadata") or {}
        if r.get("source") not in CORPUS_SOURCES:
            problems.append(f"{rid}: unknown source {r.get('source')!r}")
        host = url_host(url) if url.startswith("https://") else ""
        if not host or not sites.is_nyit_host(host):
            problems.append(f"{rid}: url {url!r} is not an https link to an NYIT website")
        elif r.get("source") == "section" and not is_in_scope(url.split("#")[0]):
            problems.append(f"{rid}: url {url!r} is outside the crawl scope of sites.py")
        if not r.get("has_text", bool((r.get("text") or "").strip())):
            problems.append(f"{rid}: empty text")
        if not (r.get("title") or "").strip():
            problems.append(f"{rid}: empty title")
        if not meta.get("site"):
            problems.append(f"{rid}: metadata has no site")
    sources = Counter(r.get("source") for r in d.corpus)
    spread = ", ".join(f"{s} {n:,}" for s, n in sources.most_common())
    if problems:
        old = " (written by an older version? run `python -m askbear_scraper parse` again)" \
            if not any((r.get("metadata") or {}).get("site") for r in d.corpus) else ""
        return check(name, FAIL, f"{len(problems):,} problem(s) in {len(d.corpus):,} records "
                                 f"({spread}){old}", problems)
    n_sites = len({(r.get("metadata") or {}).get("site") for r in d.corpus})
    return check(name, PASS, f"{len(d.corpus):,} records ({spread}) from "
                             f"{plural(n_sites, 'website')}; ids unique, every record has a "
                             "title, text, its website and an https link to an NYIT site")


def check_corpus_in_sync(d: Inputs) -> List[Check]:
    return [_corpus_pages_in_sync(d), _corpus_documents_in_sync(d)]


def _corpus_pages_in_sync(d: Inputs) -> Check:
    name = "Corpus matches sections and courses"
    if d.corpus is None or d.sections is None or d.courses is None or not (d.sections or d.courses):
        return check(name, SKIP, f"needs {CORPUS_FILE}, {SECTIONS_FILE} and {COURSES_FILE}")
    expected = {s.get("section_id") for s in d.sections
                if s.get("page_type") not in CORPUS_EXCLUDED_PAGE_TYPES
                and (s.get("text") or "").strip() and not s.get("duplicate_of")}
    left_out = _near_duplicate_ids(d)
    expected |= {course_corpus_id(c.get("course_id")) for c in d.courses
                 if course_in_corpus(c)} - left_out.get("course", set())
    legacy = sum(1 for c in d.courses if not course_in_corpus(c))
    got = {r.get("id") for r in d.corpus if r.get("source") in ("section", "course")}
    missing, extra = sorted(expected - got), sorted(got - expected)
    if missing or extra:
        return check(name, FAIL, f"{CORPUS_FILE} is out of sync: {len(missing)} section/course "
                                 f"record(s) missing, {len(extra)} unknown; run `python -m "
                                 "askbear_scraper parse` again",
                     [f"missing {i}" for i in missing] + [f"unknown {i}" for i in extra])
    pdf_in_corpus = sum(1 for r in d.corpus if r.get("source") == "pdf_page")
    note = ""
    if legacy:
        what = plural(legacy, "legacy 'Conversion' course code")
        note += f" ({what} {_are(legacy)} deliberately left out)"
    copies = sum(1 for s in d.sections if s.get("duplicate_of")) + len(left_out.get("course", ()))
    if copies:
        note += (f"; {plural(copies, 'near-duplicate passage')} {_are(copies)} represented by "
                 "the copy kept (duplicate_of)")
    if d.pdf_pages and not pdf_in_corpus:
        note += "; catalog PDF pages are not in the corpus (add them with `parse --corpus-with-pdf`)"
    return check(name, PASS, f"every section and course has exactly one corpus record{note}")


def _corpus_documents_in_sync(d: Inputs) -> Check:
    name = "Corpus matches PDF documents"
    if d.corpus is None:
        return _no(name, CORPUS_FILE)
    got = {r.get("id") for r in d.corpus if r.get("source") == "document"}
    if d.documents is None:
        if got:
            return check(name, WARN, f"{len(got):,} document records in {CORPUS_FILE} but no "
                                     f"{DOCUMENTS_FILE}: run `parse` again")
        return check(name, SKIP, f"no {DOCUMENTS_FILE} (run `python -m askbear_scraper docs`, "
                                 "then `parse`)")
    with_text = {r.get("doc_id") for r in d.documents if (r.get("text") or "").strip()}
    summary = d.parse_summary or {}
    with_docs = summary.get("corpus_with_docs")
    if not got and with_docs is False:
        return check(name, PASS, f"PDF documents left out of the corpus on purpose (`parse "
                                 f"--corpus-without-docs`); {len(with_text):,} pages with text "
                                 "available")
    repeated = repeated_document_pages(d.documents)
    with_superseded = bool(summary.get("corpus_with_superseded"))
    reasons = Counter()
    expected = set()
    for rec in d.documents:
        reason = document_left_out(rec, repeated, with_superseded)
        if reason:
            reasons[reason] += 1
        else:
            expected.add(rec.get("doc_id"))
    near = _near_duplicate_ids(d).get("document", set()) & expected
    expected -= near
    missing, extra = sorted(expected - got), sorted(got - expected)
    if missing or extra:
        return check(name, WARN, f"{CORPUS_FILE} does not match {DOCUMENTS_FILE}: {len(missing):,} "
                                 f"document page(s) missing, {len(extra):,} unknown (`docs` ran "
                                 "after `parse`?): run `python -m askbear_scraper parse` again",
                     [f"missing {i}" for i in missing] + [f"unknown {i}" for i in extra])
    labels = {"empty": "without text", "too_short": f"with fewer than {MIN_DOCUMENT_WORDS} words",
              "repeated": "that repeat another document's page word for word",
              "superseded": "of older editions", "site_not_crawled": "of websites no longer crawled"}
    notes = [f"{plural(n, 'page')} {labels.get(reason, reason)}" for reason, n in
             sorted(reasons.items())]
    if near:
        notes.append(f"{plural(len(near), 'near-duplicate page')} of another passage")
    note = f" ({', '.join(notes)} left out)" if notes else ""
    return check(name, PASS, f"every document page that belongs in the corpus has one record: "
                             f"{len(got):,} records{note}")


def _near_duplicate_ids(d: Inputs) -> Dict[str, Set[str]]:
    """Corpus ids that `parse` left out as near-duplicates, per source (parse_summary.json)."""
    near = (d.parse_summary or {}).get("near_duplicates") or {}
    return {src: set(ids or []) for src, ids in (near.get("left_out_ids") or {}).items()}


# Corpus ids go into whitespace-separated TREC run and qrels files (trec_eval, Pyserini,
# ir_measures, ranx): letters, digits and - _ . : only
_TREC_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


def check_corpus_ids(d: Inputs) -> Check:
    name = "Corpus ids usable in TREC run/qrels files"
    if d.corpus is None:
        return _no(name, CORPUS_FILE)
    bad = [f"{r.get('id')!r} ({r.get('source')})" for r in d.corpus
           if not _TREC_SAFE_ID.match(str(r.get("id") or ""))]
    if bad:
        return check(name, FAIL, f"{len(bad):,} corpus id(s) contain whitespace or other characters "
                                 "that break whitespace-separated TREC run and qrels files "
                                 "(trec_eval, Pyserini, ir_measures): run `python -m "
                                 "askbear_scraper parse` again", bad)
    examples = ", ".join(sorted({str(r.get("id")) for r in d.corpus if r.get("source") == "course"})[:1]
                         + [str(r.get("id")) for r in d.corpus[:1]])
    return check(name, PASS, f"all {len(d.corpus):,} ids consist of letters, digits and - _ . : "
                             f"only{f' (e.g. {examples})' if examples else ''}")


def check_corpus_duplicates(d: Inputs) -> Check:
    name = "Corpus: duplicate and near-duplicate passages"
    if not d.corpus:
        return _no(name, CORPUS_FILE, d.corpus)
    near = (d.parse_summary or {}).get("near_duplicates") or {}
    by_text: Dict[str, List[str]] = defaultdict(list)
    for r in d.corpus:
        if r.get("text_key"):
            by_text[r["text_key"]].append(str(r.get("id")))
    exact = [ids for ids in by_text.values() if len(ids) > 1]
    ids = {r.get("id") for r in d.corpus}
    members: Dict[str, List[dict]] = defaultdict(list)
    for r in d.corpus:
        group = (r.get("metadata") or {}).get("dup_group")
        if group:
            members[group].append(r)
    problems = [f"dup_group {g} is not a corpus id" for g in members if g not in ids]
    problems += [f"dup_group {g}: dup_count {recs[0]['metadata'].get('dup_count')} but "
                 f"{len(recs)} records" for g, recs in members.items()
                 if any((r.get("metadata") or {}).get("dup_count") != len(recs) for r in recs)]
    if problems:
        return check(name, FAIL, f"{len(problems)} inconsistent dup_group(s): run `python -m "
                                 "askbear_scraper parse` again", problems)
    copies = sum(len(i) - 1 for i in exact)
    if not near.get("enabled"):
        return check(name, WARN, f"near-duplicates were not collapsed (`parse "
                                 f"--corpus-with-near-duplicates` or an older version); "
                                 f"{plural(copies, 'record')} {'repeats' if copies == 1 else 'repeat'}"
                                 " another record's text",
                     [" = ".join(i[:3]) for i in exact])
    msg = (f"{plural(near.get('left_out', 0), 'near-duplicate copy', 'near-duplicate copies')} "
           f"left out of the corpus ({', '.join(f'{k} {v:,}' for k, v in (near.get('left_out_by_source') or {}).items()) or 'none'}); "
           f"{plural(sum(len(r) for r in members.values()), 'record')} in "
           f"{plural(len(members), 'dup_group')} (the same passage for different programs, "
           "courses or entering classes: show one result per dup_group)")
    if exact:
        return check(name, WARN, f"{plural(copies, 'record')} {'repeats' if copies == 1 else 'repeat'}"
                                 f" another record's whole text (too short to be grouped?); {msg}",
                     [" = ".join(i[:3]) for i in exact])
    return check(name, PASS, f"no two records have the same text; {msg}")


def check_short_passages(d: Inputs) -> Check:
    name = f"Short passages (< {MIN_SECTION_WORDS} words)"
    if not d.sections:
        return _no(name, SECTIONS_FILE, d.sections)
    per_page = Counter(s.get("doc_id") for s in d.sections)
    short = [s for s in d.sections if (s.get("word_count") or 0) < MIN_SECTION_WORDS
             and not s.get("duplicate_of") and s.get("page_type") not in CORPUS_EXCLUDED_PAGE_TYPES]
    unmerged = [f"{s.get('section_id')}  {s.get('url')}  ({s.get('word_count')} words)"
                for s in short if per_page[s.get("doc_id")] > 1]
    doc_words = {r.get("doc_id"): r.get("word_count") or 0 for r in d.documents or []}
    short_docs = [f"{r.get('id')}  ({doc_words[r.get('id')]} words)" for r in d.corpus or []
                  if r.get("source") == "document" and r.get("id") in doc_words
                  and doc_words[r.get("id")] < MIN_DOCUMENT_WORDS]
    merged = (d.parse_summary or {}).get("tiny_sections_merged")
    note = f"{plural(merged, 'short section')} merged into a neighbour; " if merged else ""
    if unmerged or short_docs:
        return check(name, WARN, f"{note}{plural(len(unmerged), 'short section')} on pages with "
                                 f"other sections and {plural(len(short_docs), 'short PDF page')} "
                                 "in the corpus (run `parse` again)", unmerged + short_docs)
    return check(name, PASS, f"{note}{plural(len(short), 'short section')} left, each the only "
                             f"section of its page; no PDF page with fewer than "
                             f"{MIN_DOCUMENT_WORDS} words in the corpus")


def check_document_titles(d: Inputs) -> Check:
    name = "PDF documents: titles"
    if not d.documents:
        return _no(name, DOCUMENTS_FILE, d.documents, command="docs")
    # (validate imports no PDF code otherwise)
    from .documents import doc_file_type, one_file_in_other_formats, title_group, title_quality

    titles: Dict[str, str] = {}
    types: Dict[str, str] = {}
    for rec in d.documents:
        url = str(rec.get("pdf_url"))
        titles.setdefault(url, str(rec.get("title") or ""))
        types.setdefault(url, doc_file_type(rec))
    # titles only need to differ within one file type: the corpus title names the format
    # ("(PDF, p. 1)", "(Excel, part 1)")
    by_title: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    by_text: Dict[str, List[str]] = defaultdict(list)
    for url, title in sorted(titles.items()):
        key = title_group(title, types[url])
        by_title[key].append(url)
        by_text[key[0]].append(url)
    shared = {key: urls for key, urls in by_title.items() if len(urls) > 1}
    across = [urls for urls in by_text.values() if len({types[u] for u in urls}) > 1]
    # the same form as X.pdf and X.docx: one document, its corpus titles tell the formats apart
    twins = sum(len(urls) for urls in across if one_file_in_other_formats(urls))
    others = sum(len(urls) for urls in across if not one_file_in_other_formats(urls))
    junk = [f"{url}: {title!r}" for url, title in sorted(titles.items())
            if title_quality(title) == "junk"]
    type_names = {"pdf": "PDFs", "docx": "Word files", "xlsx": "Excel files",
                  "pptx": "PowerPoint files"}
    offenders = [f"{len(urls)} {type_names.get(key[1], key[1] + ' files')} titled "
                 f"{titles[urls[0]]!r}: {', '.join(urls[:3])}"
                 for key, urls in sorted(shared.items(), key=lambda kv: -len(kv[1]))] + junk
    n_shared = sum(len(u) for u in shared.values())
    if offenders:
        return check(name, WARN, f"{plural(n_shared, 'document')} share a title with another "
                                 f"document of the same file type ({plural(len(shared), 'title')}) "
                                 f"and {plural(len(junk), 'title')} look like no title at all "
                                 "(`python -m askbear_scraper docs --max-docs 0` re-extracts the "
                                 "titles)", offenders)
    notes = []
    if twins:
        notes.append(f"{plural(twins, 'file')} published both as PDF and as Word/Excel/PowerPoint "
                     "share the title of their other format")
    if others:
        notes.append(f"{plural(others, 'document')} share a title with a document of another "
                     "file type only")
    note = f" ({'; '.join(notes)}: the corpus titles name the format)" if notes else ""
    return check(name, PASS, f"all {len(titles):,} documents have a title of their own within "
                             f"their file type{note}")


def check_document_editions(d: Inputs) -> Check:
    name = "PDF documents: older editions"
    if not d.documents:
        return _no(name, DOCUMENTS_FILE, d.documents, command="docs")
    if not any("superseded" in r for r in d.documents):
        return check(name, WARN, f"{DOCUMENTS_FILE} has no edition fields (written by an older "
                                 "version; `parse` works them out from the file names): run "
                                 "`python -m askbear_scraper docs --max-docs 0` to refresh it")
    pdfs: Dict[str, dict] = {}
    for rec in d.documents:
        pdfs.setdefault(str(rec.get("pdf_url")), rec)
    superseded = {u for u, r in pdfs.items() if r.get("superseded")}
    problems = [f"{u}: superseded_by {r.get('superseded_by')!r} is not a downloaded PDF"
                for u, r in sorted(pdfs.items())
                if r.get("superseded") and r.get("superseded_by") not in pdfs]
    with_superseded = bool((d.parse_summary or {}).get("corpus_with_superseded"))
    in_corpus = sorted({str((r.get("metadata") or {}).get("pdf_url")) for r in d.corpus or []
                        if r.get("source") == "document"} & superseded)
    if in_corpus and not with_superseded:
        problems += [f"{u}: older edition in {CORPUS_FILE}" for u in in_corpus]
    if problems:
        return check(name, FAIL, f"{len(problems)} problem(s) with older editions", problems)
    series = {r.get("series") for r in pdfs.values() if r.get("series")}
    cohorts = sum(1 for r in pdfs.values() if r.get("cohort_year"))
    pages = sum(1 for r in d.documents if r.get("superseded"))
    where = ("kept in the corpus (`parse --corpus-with-superseded`)" if with_superseded
             else "left out of the corpus (`parse --corpus-with-superseded` adds them)")
    return check(name, PASS, f"{plural(len(series), 'series', 'series')} of documents published "
                             f"per year/term; {plural(len(superseded), 'older edition')} "
                             f"({plural(pages, 'page')}) {where}; "
                             f"{plural(cohorts, 'semester map or class calendar', 'semester maps and class calendars')}"
                             " stay (one per entering class)",
                 sorted(superseded))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _no(name: str, filename: str, records: Optional[list] = None, command: str = "parse") -> Check:
    """SKIP result for a check whose input file is missing (records None) or empty."""
    if records is None:
        return check(name, SKIP, f"no {filename} yet (run `python -m askbear_scraper {command}`)")
    return check(name, SKIP, f"{filename} is empty")


def _are(count: int) -> str:
    return "is" if count == 1 else "are"


def _have(count: int) -> str:
    return "has" if count == 1 else "have"


def _appear(count: int) -> str:
    return "appears" if count == 1 else "appear"


def _crawled_site(host: str) -> bool:
    """True for an NYIT host that sites.py lets the crawler visit (profile enabled)."""
    profile = sites.profile_for(host)
    return profile is not None and profile.enabled


def _profiled(host: str) -> bool:
    """A website listed in sites.py (not one found through links)."""
    return host in sites.SITES


def _repeated(values: Iterable[Any]) -> List[Any]:
    return sorted((v for v, n in Counter(values).items() if n > 1), key=str)


def _norm(text: Any) -> str:
    return _SPACES.sub(" ", str(text or "")).strip().lower()


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 3].rstrip() + "..."


def _later(a: Optional[str], b: Optional[str]) -> bool:
    """True if ISO timestamp ``a`` is later than ``b`` (False if either is missing/odd)."""
    try:
        return datetime.fromisoformat(str(a)) > datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return False


def _short_school(name: str) -> str:
    return name.replace("College of ", "").replace("School of ", "")


def _cached_files(manifest: Dict[str, dict]) -> Dict[str, str]:
    """URL (requested and final) -> cache file name, for pages saved by the crawler."""
    files: Dict[str, str] = {}
    for url, e in manifest.items():
        if e.get("status") == "ok" and e.get("file"):
            files.setdefault(url, e["file"])
            files.setdefault(normalize_url(e.get("final_url") or url) or url, e["file"])
    return files


def _cached_html(html_dir: Path, filename: Optional[str]) -> str:
    path = html_dir / filename if filename else None
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


CHECKS: List[Tuple[str, Callable[[Inputs], Any]]] = [
    ("Crawl", check_crawl_finished),
    ("Crawl", check_site_limits),
    ("Crawl", check_crawl_errors),
    ("Crawl", check_site_coverage),
    ("Crawl", check_pagination),
    ("Pages", check_pages_parsed),
    ("Pages", check_page_ids),
    ("Pages", check_page_sites),
    ("Pages", check_real_pages),
    ("Pages", check_page_content),
    ("Pages", check_page_titles),
    ("Pages", check_page_metadata),
    ("Pages", check_short_pages),
    ("Pages", check_duplicate_pages),
    ("Pages", check_mirror_sites),
    ("Pages", check_noindex),
    ("Pages", check_dates),
    ("Catalog", check_catalog_year),
    ("Catalog", check_schools),
    ("Catalog", check_program_metadata),
    ("Catalog", check_course_coverage),
    ("Catalog", check_course_fields),
    ("Catalog", check_course_descriptions),
    ("Catalog", check_course_links),
    ("Passages (sections)", check_sections),
    ("Passages (sections)", check_section_length),
    ("Passages (sections)", check_section_anchors),
    ("Passages (sections)", check_boilerplate_leakage),
    ("Text hygiene", check_hygiene_pages),
    ("Text hygiene", check_hygiene_sections),
    ("Text hygiene", check_hygiene_courses),
    ("Text hygiene", check_hygiene_pdf),
    ("Text hygiene", check_hygiene_documents),
    ("Catalog PDFs", check_pdf_page_counts),
    ("Catalog PDFs", check_pdf_empty_pages),
    ("PDF documents", check_document_pages),
    ("PDF documents", check_document_text),
    ("PDF documents", check_document_titles),
    ("PDF documents", check_document_editions),
    ("Corpus", check_corpus),
    ("Corpus", check_corpus_ids),
    ("Corpus", check_corpus_in_sync),
    ("Corpus", check_corpus_duplicates),
    ("Corpus", check_short_passages),
]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_report(checks: Sequence[Check], report: dict, data_dir: str, report_path: str) -> str:
    width = max(80, min(shutil.get_terminal_size((100, 24)).columns, 120))
    num_w = len(str(len(checks)))
    name_w = max(len(c.name) for c in checks)
    title = "AskBear data validation"
    lines = [title, "=" * len(title), f"data folder: {data_dir}"]
    group = None
    for i, c in enumerate(checks, 1):
        if c.group != group:
            group = c.group
            lines += ["", f"{group}" if group else "Checks"]
        prefix = f"{i:>{num_w}}. {c.status}  {c.name:<{name_w}}  "
        body = _wrap(c.message, max(30, width - len(prefix)))
        lines.append(prefix + body[0])
        lines += [" " * len(prefix) + rest for rest in body[1:]]

    flagged = [(i, c) for i, c in enumerate(checks, 1) if c.status in (WARN, FAIL) and c.examples]
    if flagged:
        lines += ["", f"Examples (up to {PRINTED_EXAMPLES} per check; up to {MAX_EXAMPLES} are "
                      f"saved in {REPORT_FILE}):"]
        indent = " " * (num_w + 2)
        for i, c in flagged:
            lines.append(f"{i:>{num_w}}. {c.status}  {c.name}")
            for example in c.examples[:PRINTED_EXAMPLES]:
                lines += _wrap(example, width - len(indent) - 2, indent + "- ", indent + "  ")
            if c.count > PRINTED_EXAMPLES:
                lines.append(f"{indent}  ... {c.count - PRINTED_EXAMPLES:,} more")

    counts = report["counts"]
    verdict = "FAILED" if not report["passed"] else (
        "PASSED with warnings" if counts[WARN] else "PASSED")
    lines += ["", f"Result: {verdict}  ({counts[PASS]} PASS, {counts[WARN]} WARN, "
                  f"{counts[FAIL]} FAIL, {counts[SKIP]} SKIP)",
              f"Full report: {report_path}"]
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int, first: str = "", rest: str = "") -> List[str]:
    return textwrap.wrap(text, width, initial_indent=first, subsequent_indent=rest,
                         break_long_words=False, break_on_hyphens=False) or [first]
