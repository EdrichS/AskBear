"""Dataset statistics for the project report.

    python -m askbear_scraper stats

Reads whatever exists so far (the crawl manifest, crawl summary and sitemap lists in
data/raw/, the outputs of ``parse``, ``docs`` and ``pdf`` in data/processed/), prints a
report and writes:

    data/processed/stats.json   every number, machine-readable
    data/processed/STATS.md     the same report as Markdown, ready to paste into the final
                                project report

The report opens with a short summary ("We scraped N pages from K NYIT websites ..."),
followed by one table per topic: the websites, the "Proposal vs. scraped" comparison with
slide 9 of the project proposal (config.PROPOSAL_TARGETS), the crawl, the pages, the search
passages, the courses, the PDFs and the search corpus. Inputs that do not exist yet are
shown as "n/a" together with the command that creates them, so ``stats`` can be run at any
point of the project.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config, sites
from .config import Settings
from .crawler import load_manifest
from .pipeline import (CORPUS_FILE, COURSES_FILE, DOCS_MANIFEST_FILE, DOCUMENTS_FILE,
                       DOCUMENTS_SUMMARY_FILE, MIN_INDEXED_WORDS, MIN_SECTION_WORDS, PAGES_FILE,
                       PARSE_SUMMARY_FILE, PDF_PAGES_FILE,
                       PDF_SUMMARY_FILE, SECTIONS_FILE, is_catalog_site, is_parsed_site,
                       site_rank)
from .urls import link_kind, normalize_url, url_host
from .utils import now_iso, read_jsonl, sha1, word_count, write_json

log = logging.getLogger("askbear.stats")

STATS_JSON_FILE = "stats.json"
STATS_MD_FILE = "STATS.md"
CRAWL_SUMMARY_FILE = "crawl_summary.json"   # in data/raw/, written when a crawl finishes
SITEMAPS_DIR = "sitemaps"                   # in data/raw/: <host>.json, written by `crawl`

PROPOSAL_EDITION = "2025-2026"              # the PDFs the proposal's numbers come from
LEVELS = ("undergraduate", "graduate", "doctoral", "professional")
CATALOG_ORDER = {"undergraduate": 0, "graduate": 1, "osteopathic": 2}

# Crawl statuses (crawler.py) in the order the report lists them, with a short column label.
STATUS_ORDER = ("ok", "duplicate", "not_found", "http_error", "soft404", "login_page",
                "offsite_redirect", "redirect_loop", "non_html", "robots_disallowed", "error")
STATUS_LABEL = {
    "ok": "saved", "duplicate": "dup. redirect", "not_found": "404", "http_error": "HTTP error",
    "soft404": "soft 404", "login_page": "login", "offsite_redirect": "off-site",
    "redirect_loop": "redirect loop", "non_html": "not HTML", "robots_disallowed": "robots.txt",
    "error": "network error",
}
STATUS_MEANING = {
    "ok": "saved and parsed",
    "duplicate": "redirect to a page we already have",
    "not_found": "HTTP 404/410 (broken link on the site)",
    "http_error": "other HTTP error, e.g. 500 on NYIT's side",
    "soft404": "missing page served as an empty template (skipped)",
    "login_page": "sign-in form, nothing public to index (skipped)",
    "offsite_redirect": "redirect to a login portal or another organisation (skipped)",
    "redirect_loop": "the URL redirects in a circle on NYIT's side (cached)",
    "non_html": "link that is not an HTML page",
    "robots_disallowed": "blocked by robots.txt",
    "error": "network error after all retries",
}
DOC_STATUS_MEANING = {
    "ok": "downloaded and extracted",
    "too_large": "larger than the size limit (--max-mb), skipped",
    "not_pdf": "the link did not return a PDF",
    "not_found": "HTTP 404/410 (broken link on the site)",
    "http_error": "other HTTP error",
    "error": "network or extraction error",
    "robots_disallowed": "blocked by robots.txt",
    "redirect_loop": "the link redirects in a circle (or more than 10 times)",
    "offsite_redirect": "the link redirects to a sign-in portal or another website",
    "not_office": "a .docx/.xlsx/.pptx link did not return that kind of file",
    "encrypted": "a password-protected (encrypted) Office file",
}
# Why a linked Office file has no text in documents.jsonl (not_extracted_files.by_reason);
# the download statuses above are reasons too
UNREAD_REASON_LABEL = {
    "legacy_binary_format": "legacy .doc/.xls/.ppt (not read)",
    "unsupported_format": ".rtf/.odt (not read)",
    "extract_error": "could not be read",
    "not_downloaded_yet": "not downloaded yet",
    "max_docs": "left for a later run (--max-docs)",
    "noindex": "noindex", "over_max_mb": "larger than --max-mb",
}
FILE_TYPE_NAMES = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint"}

# Websites found through links (no profile in sites.py) with fewer pages than this share one
# row in the report's per-website tables (stats.json keeps every site).
MIN_SITE_ROW_PAGES = 20

# Reasons the `docs` step gives for PDF links it does not download (documents_summary.json).
DOC_SKIP_LABEL = {
    "other_website": "not on an NYIT website", "site_not_crawled": "on a website that is not "
    "crawled", "catalog_pdf": "full catalog PDFs (see `pdf`)", "invalid_url": "invalid URL",
    "max_docs": "left for a later run (--max-docs)", "over_max_mb": "larger than --max-mb",
}

RUN_PARSE = "`python -m askbear_scraper parse`"
RUN_PDF = "`python -m askbear_scraper pdf --edition 2025-2026`"
RUN_CRAWL = "`python -m askbear_scraper crawl`"
RUN_DOCS = "`python -m askbear_scraper docs`"

# A report is a list of blocks rendered twice: as text (console) and as Markdown.
#   ("h1"|"h2"|"h3", title) | ("p", text) | ("bullets", [text]) | ("kv", [(label, value)])
#   ("table", (headers, rows, align))   align: one "l"/"r" per column
Block = Tuple[str, Any]


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def run_stats(settings: Settings) -> dict:
    """Compute the statistics, print the report, write stats.json + STATS.md."""
    stats = compute_stats(settings)
    blocks = report_blocks(stats)
    emit(render_text(blocks))

    out = settings.processed_dir
    write_json(out / STATS_JSON_FILE, stats)
    write_text_file(out / STATS_MD_FILE, render_markdown(blocks))
    log.info("wrote %s and %s to %s", STATS_JSON_FILE, STATS_MD_FILE, out)
    return stats


def write_text_file(path: Path, text: str) -> None:
    """Write ``text`` to a temporary file next to ``path`` and move it into place (like
    utils.write_json): a crash never leaves half a file, and when ``path`` is a symbolic link
    the link itself is replaced -- the file it points to is never written through."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def compute_stats(settings: Settings) -> dict:
    """Every number of the report as one JSON-serialisable dict."""
    raw, out = settings.raw_dir, settings.processed_dir
    manifest = load_manifest(settings.manifest_path) if settings.manifest_path.exists() else None
    crawl_summary = load_json(raw / CRAWL_SUMMARY_FILE)
    sitemaps = load_sitemaps(raw / SITEMAPS_DIR)
    parse_summary = load_json(out / PARSE_SUMMARY_FILE)
    # the big files are loaded without the fields the statistics do not need
    pages = load_jsonl(out / PAGES_FILE, _slim_page)
    sections = load_jsonl(out / SECTIONS_FILE, _slim_section)
    courses = load_jsonl(out / COURSES_FILE)
    corpus = load_jsonl(out / CORPUS_FILE, _slim_corpus)
    pdf_pages = load_jsonl(out / PDF_PAGES_FILE, _slim_text)
    pdf_summary = load_json(out / PDF_SUMMARY_FILE)
    documents = load_jsonl(out / DOCUMENTS_FILE, _slim_text)
    documents_summary = load_json(out / DOCUMENTS_SUMMARY_FILE)
    docs_manifest = load_jsonl(raw / DOCS_MANIFEST_FILE)

    stats = {
        "generated_at": now_iso(),
        "data_dir": str(settings.data_dir),
        "inputs": {
            "crawl_manifest": manifest is not None,
            CRAWL_SUMMARY_FILE: crawl_summary is not None,
            "sitemap_lists": len(sitemaps),
            PARSE_SUMMARY_FILE: parse_summary is not None,
            PAGES_FILE: pages is not None,
            SECTIONS_FILE: sections is not None,
            COURSES_FILE: courses is not None,
            CORPUS_FILE: corpus is not None,
            PDF_PAGES_FILE: pdf_pages is not None,
            PDF_SUMMARY_FILE: pdf_summary is not None,
            DOCUMENTS_FILE: documents is not None,
            DOCUMENTS_SUMMARY_FILE: documents_summary is not None,
            DOCS_MANIFEST_FILE: docs_manifest is not None,
        },
        "crawl": crawl_stats(manifest, crawl_summary, sitemaps),
        "pages": page_stats(pages),
        "sections": section_stats(sections),
        "courses": course_stats(courses),
        "pdf": pdf_stats(pdf_summary, pdf_pages),
        "documents": document_stats(documents, documents_summary, docs_manifest),
        "corpus": corpus_stats(corpus),
        "parse": {
            "available": parse_summary is not None,
            "failures": len((parse_summary or {}).get("failures") or []),
            "seconds": ((parse_summary or {}).get("timings_seconds") or {}).get("total"),
            "corpus_with_docs": (parse_summary or {}).get("corpus_with_docs"),
            "corpus_with_pdf": (parse_summary or {}).get("corpus_with_pdf"),
        },
    }
    stats["corpus"].update(corpus_cleanup_stats(corpus, parse_summary))
    stats["sites"] = site_stats(pages, sections, corpus, stats["crawl"], parse_summary)
    stats["schools"] = stats["pages"].get("schools", [])
    stats["campuses"] = sorted(stats["pages"].get("campuses", {}))
    stats["proposal_vs_scraped"] = proposal_comparison(stats)
    return stats


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_jsonl(path: Path, transform: Optional[Callable[[dict], dict]] = None
               ) -> Optional[List[dict]]:
    """All records of a JSONL file (optionally slimmed by ``transform``), or None when the
    file does not exist."""
    if not Path(path).exists():
        return None
    if transform is None:
        return list(read_jsonl(path))
    return [transform(rec) for rec in read_jsonl(path)]


def load_json(path: Path) -> Optional[Any]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None


def load_sitemaps(folder: Path) -> Dict[str, List[str]]:
    """host -> normalised page URLs listed in its sitemaps (data/raw/sitemaps/<host>.json)."""
    out: Dict[str, List[str]] = {}
    folder = Path(folder)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        host = str(data.get("host") or path.stem).lower()
        urls = []
        for item in data.get("pages") or []:
            loc = item[0] if isinstance(item, (list, tuple)) and item else item
            url = normalize_url(loc) if isinstance(loc, str) else None
            if url:
                urls.append(url)
        out[host] = list(dict.fromkeys(urls))
    return out


def _slim_page(rec: dict) -> dict:
    rec = dict(rec)
    rec["links"] = len(rec.get("links") or [])
    rec.pop("text", None)
    return rec


def _slim_section(rec: dict) -> dict:
    rec = dict(rec)
    rec["text_sha1"] = sha1(rec.pop("text", None) or "")
    return rec


def _slim_corpus(rec: dict) -> dict:
    rec = dict(rec)
    text = rec.pop("text", None) or ""
    rec["words"] = word_count(text)
    rec["emails"] = sorted({m.lower() for m in EMAIL.findall(text)})
    rec["has_phone"] = bool(PHONE.search(text))
    return rec


# contact details that NYIT prints on its public pages (office and staff e-mail addresses,
# phone numbers): counted for the report, not removed (see docs/METHODOLOGY.md, ethics)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d.-])(?:\(\d{3}\)\s?|\d{3}[.-])\d{3}[.-]\d{4}(?![\d-]|\.\d)")


def _slim_text(rec: dict) -> dict:
    rec = dict(rec)
    rec.pop("text", None)
    return rec


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """q-th percentile (0-100) with linear interpolation (numpy's default method)."""
    data = sorted(values)
    if not data:
        return None
    pos = (len(data) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def describe(values: Iterable[float]) -> dict:
    """count / total / min / median / mean / p90 / max of a list of numbers."""
    data = list(values)
    if not data:
        return {"count": 0, "total": 0, "min": None, "median": None, "mean": None,
                "p90": None, "max": None}
    return {
        "count": len(data),
        "total": sum(data),
        "min": min(data),
        "median": statistics.median(data),
        "mean": round(statistics.fmean(data), 1),
        "p90": round(percentile(data, 90), 1),
        "max": max(data),
    }


def pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def site_of(rec: dict) -> str:
    """The website of a page/section record (records written before phase 2 have no "site")."""
    return str(rec.get("site") or url_host(str(rec.get("url") or "")))


def site_name(host: str) -> str:
    profile = sites.SITES.get(host)
    return profile.name if profile is not None else "Other NYIT website (found through links)"


def ordered_hosts(hosts: Iterable[str]) -> List[str]:
    return sorted({h for h in hosts if h}, key=site_rank)


def short_host(host: str) -> str:
    """"catalog.nyit.edu" -> "catalog" (for narrow table cells)."""
    return host[:-len(".nyit.edu")] if host.endswith(".nyit.edu") else host


# ---------------------------------------------------------------------------
# statistics per input
# ---------------------------------------------------------------------------
def crawl_stats(manifest: Optional[Dict[str, dict]], summary: Optional[dict],
                sitemaps: Optional[Dict[str, List[str]]] = None) -> dict:
    if manifest is None:
        return {"available": False}
    sitemaps = sitemaps or {}
    statuses = Counter(e.get("status") or "?" for e in manifest.values())
    ok = [e for e in manifest.values() if e.get("status") == "ok"]
    fetched = sorted(e["fetched_at"] for e in ok if e.get("fetched_at"))
    keep = ("finished_at", "seconds", "interrupted", "urls_seen", "urls_visited",
            "still_queued", "network_requests", "pdf_links", "external_links")
    summary = summary if isinstance(summary, dict) else None
    multi_site = bool(summary and "sites" in summary)

    by_host: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
    for url, e in manifest.items():
        by_host[str(e.get("site") or url_host(url))].append((url, e))
    listed_counts = dict((summary or {}).get("sitemap_pages_by_site") or {})
    rows = []
    for host in ordered_hosts(set(by_host) | set(sitemaps) | set(listed_counts)):
        entries = by_host.get(host, [])
        counts = Counter(e.get("status") or "?" for _, e in entries)
        ok_here = [(u, e) for u, e in entries if e.get("status") == "ok"]
        elsewhere = sum(1 for u, e in ok_here
                        if url_host(normalize_url(e.get("final_url") or u) or u) != host)
        row = {
            "site": host,
            "name": site_name(host),
            "urls": len(entries),
            "ok": counts.get("ok", 0),
            "statuses": {s: counts[s] for s in _status_order(counts)},
            "html_megabytes": round(sum(e.get("bytes") or 0 for _, e in ok_here) / 1e6, 1),
            "content_on_other_site": elsewhere,
        }
        row.update(_sitemap_coverage(host, sitemaps.get(host), listed_counts.get(host),
                                     manifest, {u for u, _ in ok_here}))
        rows.append(row)
    return {
        "available": True,
        "urls": len(manifest),
        "pages_ok": statuses.get("ok", 0),
        "status_counts": {s: statuses[s] for s in _status_order(statuses)},
        "html_megabytes": round(sum(e.get("bytes") or 0 for e in ok) / 1e6, 1),
        # ... of the pages that `parse` reads (not those of hosts sites.py no longer crawls)
        "parsed_html_megabytes": round(sum(
            e.get("bytes") or 0 for e in ok
            if is_parsed_site(url_host(normalize_url(e.get("final_url") or e.get("url") or "")
                                       or ""))) / 1e6, 1),
        "first_fetch": fetched[0] if fetched else None,
        "last_fetch": fetched[-1] if fetched else None,
        "summary": {k: summary.get(k) for k in keep} if summary else None,
        "summary_format": ("multi-site" if multi_site else "catalog only (older crawler)")
        if summary else None,
        "finished": bool(summary and not summary.get("interrupted")
                         and not summary.get("still_queued")),
        # pages fetched after crawl_summary.json was written: a newer crawl is running (or
        # stopped before writing its summary)
        "newer_crawl": bool(summary and fetched and str(fetched[-1]) > str(summary.get(
            "finished_at") or "")),
        # after `crawl --site` runs, "sites" is every website the summary covers and
        # "last_run" describes the restricted run; "runs" lists every run since the last
        # full crawl
        "sites_in_last_run": (list(((summary.get("last_run") or {}) if isinstance(
            summary.get("last_run"), dict) else summary).get("sites") or [])
            if multi_site else None),
        "runs": [{k: r.get(k) for k in ("finished_at", "seconds", "sites", "urls_visited",
                                         "network_requests", "interrupted")}
                 for r in (summary or {}).get("runs") or [] if isinstance(r, dict)],
        "failed_sites": list((summary or {}).get("failed_sites") or []),
        "unreachable_reasons": dict((summary or {}).get("unreachable_reasons") or {}),
        "dropped_query_self_links": dict((summary or {}).get("dropped_query_self_links") or {}),
        "sites": rows,
        "hosts_with_saved_urls": sum(1 for r in rows if r["ok"]),
        # hosts with saved pages that `parse` skips (sites.py disables them now)
        "hosts_not_parsed": [r["site"] for r in rows if r["ok"] and not is_parsed_site(r["site"])
                             and not r["content_on_other_site"]],
        # where the saved content lives (legacy hosts redirect to www.nyit.edu, ...)
        "sites_with_pages": len({h for h in (url_host(normalize_url(e.get("final_url") or
                                                                    e.get("url") or "") or "")
                                             for e in ok) if is_parsed_site(h)}),
        "content_on_other_site": sum(r["content_on_other_site"] for r in rows),
        "capped_sites": dict((summary or {}).get("capped_sites") or {}),
        "given_up_sites": dict((summary or {}).get("given_up_sites") or {}),
        "unreachable_sites": list((summary or {}).get("unreachable_sites") or []),
        "discovered_sites": list((summary or {}).get("discovered_sites") or []),
        "linked_but_not_crawled_sites": dict(
            (summary or {}).get("linked_but_not_crawled_sites") or {}),
    }


def _status_order(counts: Dict[str, int]) -> List[str]:
    known = [s for s in STATUS_ORDER if counts.get(s)]
    return known + sorted(s for s in counts if s not in STATUS_ORDER and counts[s])


def _sitemap_coverage(host: str, listed: Optional[List[str]], listed_count: Optional[int],
                      manifest: Dict[str, dict], ok_urls: set) -> dict:
    """How many of the pages a site lists in its sitemaps were crawled."""
    if listed is None:
        return {"sitemap_urls": listed_count, "sitemap_in_scope": None, "sitemap_saved": None,
                "sitemap_not_saved": {}, "saved_not_in_sitemap": None}
    in_scope = [u for u in listed if link_kind(u) == "page"]
    not_saved: Counter = Counter()
    saved = 0
    for url in in_scope:
        entry = manifest.get(url)
        status = entry.get("status") if entry else "not visited"
        if status == "ok":
            saved += 1
        else:
            not_saved[status or "?"] += 1
    listed_set = set(listed)
    return {
        "sitemap_urls": len(listed),
        "sitemap_in_scope": len(in_scope),
        "sitemap_saved": saved,
        "sitemap_not_saved": dict(not_saved.most_common()),
        "saved_not_in_sitemap": sum(1 for u in ok_urls if u not in listed_set),
    }


def page_stats(pages: Optional[List[dict]]) -> dict:
    if pages is None:
        return {"available": False}
    distinct = [p for p in pages if not p.get("duplicate_of")]
    catalog_all = [p for p in pages if is_catalog_site(site_of(p))]
    catalog = [p for p in distinct if is_catalog_site(site_of(p))]
    by_type: Counter = Counter()
    words_by_type: Dict[str, List[int]] = {}
    sites_by_type: Dict[str, Counter] = defaultdict(Counter)
    for p in distinct:
        page_type = p.get("page_type") or "other"
        by_type[page_type] += 1
        words_by_type.setdefault(page_type, []).append(p.get("word_count") or 0)
        sites_by_type[page_type][site_of(p)] += 1
    years = Counter(p.get("catalog_year") for p in catalog_all if p.get("catalog_year"))
    campuses = Counter(c for p in distinct for c in (p.get("campuses") or []))
    program_pages = [p for p in catalog if p.get("page_type") == "program"]
    degree_pages = [p for p in distinct if p.get("page_type") == "degree"]
    official = set(config.SCHOOL_BY_PATH.values())
    dates = sorted(str(p["published"])[:10] for p in distinct if p.get("published"))
    events = sorted(str(p["event_start"])[:10] for p in distinct if p.get("event_start"))
    return {
        "available": True,
        "records": len(pages),
        "distinct": len(distinct),
        "duplicates": len(pages) - len(distinct),
        "noindex": sum(1 for p in distinct if p.get("noindex")),
        "noindex_records": sum(1 for p in pages if p.get("noindex")),   # copies included
        "too_short_to_index": sum(1 for p in distinct if not p.get("noindex")
                                  and (p.get("word_count") or 0) < MIN_INDEXED_WORDS),
        "sites": len({site_of(p) for p in distinct}),
        "words": describe(p.get("word_count") or 0 for p in distinct),
        # course listing tables repeat what courses.jsonl holds; this is the rest of the sites
        "words_outside_course_directory": sum(
            p.get("word_count") or 0 for p in distinct
            if p.get("page_type") not in ("course_listing", "course_index")),
        "tables": sum(p.get("num_tables") or 0 for p in distinct),
        "links": sum(_count(p.get("links")) for p in distinct),
        "catalog_pages": len(catalog),
        "catalog_year": years.most_common(1)[0][0] if years else None,
        "catalog_years": dict(years.most_common()),
        "by_type": [{"page_type": t, "pages": n, "words": sum(words_by_type[t]),
                     "median_words": statistics.median(words_by_type[t]),
                     "sites": dict(sorted(sites_by_type[t].items(),
                                          key=lambda kv: (-kv[1], site_rank(kv[0]))))}
                    for t, n in by_type.most_common()],
        "by_school": dict(Counter(p.get("school") or "(no school)" for p in distinct)
                          .most_common()),
        "by_school_catalog": dict(Counter(p.get("school") or "(no school)" for p in catalog)
                                  .most_common()),
        "by_level": dict(Counter(p.get("level") or "(not level-specific)" for p in distinct)
                         .most_common()),
        "by_level_catalog": dict(Counter(p.get("level") or "(not level-specific)"
                                         for p in catalog).most_common()),
        "schools": sorted({p["school"] for p in distinct if p.get("school") in official}),
        "other_school_names": sorted({p["school"] for p in distinct
                                      if p.get("school") and p["school"] not in official}),
        "campuses": dict(campuses.most_common()),
        "program_pages": len(program_pages),
        "curriculum_pages": sum(1 for p in catalog if p.get("page_type") == "curriculum"),
        "programs": len({p["program"] for p in catalog if p.get("program")}),
        "program_pages_by_degree": dict(Counter(p.get("degree") or "(unknown)"
                                                for p in program_pages).most_common()),
        "degree_pages": len(degree_pages),
        "degree_pages_by_degree": dict(Counter(p.get("degree") or "(unknown)"
                                               for p in degree_pages).most_common()),
        "dated_pages": len(dates),
        "first_published": dates[0] if dates else None,
        "last_published": dates[-1] if dates else None,
        # events are dated by when they take place (event_start), not by ``published``
        "events_with_time": len(events),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
    }


def _count(value: Any) -> int:
    """Links are slimmed to a number when pages.jsonl is loaded for statistics."""
    if isinstance(value, int):
        return value
    return len(value or [])


SECTION_BUCKETS = ((0, 20, "under 20"), (20, 50, "20-49"), (50, 150, "50-149"),
                   (150, 400, "150-399"), (400, 1000, "400-999"), (1000, None, "1,000 or more"))


def section_stats(sections: Optional[List[dict]]) -> dict:
    if sections is None:
        return {"available": False}
    words = [s.get("word_count") or 0 for s in sections]
    text_counts = Counter(_text_key(s) for s in sections if _text_key(s))
    buckets = []
    for lo, hi, label in SECTION_BUCKETS:
        n = sum(1 for w in words if w >= lo and (hi is None or w < hi))
        buckets.append({"words": label, "sections": n, "share_pct": pct(n, len(words))})
    per_site: Dict[str, List[dict]] = defaultdict(list)
    for s in sections:
        per_site[site_of(s)].append(s)
    return {
        "available": True,
        "count": len(sections),
        "pages_with_sections": len({s.get("doc_id") for s in sections}),
        "words": describe(words),
        "length_buckets": buckets,
        "with_anchor_link": sum(1 for s in sections if "#" in (s.get("url") or "")),
        "over_1000_words": sum(1 for w in words if w > 1000),
        "repeated_text": sum(n for n in text_counts.values() if n > 1),
        "repeated_text_by_site": dict(Counter(
            site_of(s) for s in sections if text_counts.get(_text_key(s), 0) > 1).most_common()),
        "by_site": [{"site": host, "sections": len(recs),
                     "pages": len({s.get("doc_id") for s in recs}),
                     "words": describe(s.get("word_count") or 0 for s in recs),
                     "with_anchor_link": sum(1 for s in recs if "#" in (s.get("url") or ""))}
                    for host, recs in sorted(per_site.items(), key=lambda kv: site_rank(kv[0]))],
    }


def _text_key(section: dict) -> str:
    if "text_sha1" in section:
        return section["text_sha1"] if section.get("word_count") else ""
    return section.get("text") or ""


def course_stats(courses: Optional[List[dict]]) -> dict:
    if courses is None:
        return {"available": False}

    def summarize(recs: List[dict]) -> dict:
        return {
            "records": len(recs),
            "codes": len({c.get("code") for c in recs}),
            "subjects": len({c.get("subject") for c in recs}),
            "with_description_pct": pct(sum(1 for c in recs if c.get("description")), len(recs)),
            "with_prerequisites_pct": pct(sum(1 for c in recs if c.get("prerequisites")),
                                          len(recs)),
        }

    levels = sorted({c.get("level") or "?" for c in courses},
                    key=lambda lv: (LEVELS.index(lv) if lv in LEVELS else 9, lv))
    ug_gr = [c for c in courses if c.get("level_code") in ("UG", "GR")]
    per_subject = Counter((c.get("subject"), c.get("subject_name") or "") for c in courses)
    overall = summarize(courses)
    return {
        "available": True,
        "records": len(courses),
        "unique_codes": overall["codes"],
        "unique_subjects": overall["subjects"],
        "ug_gr_unique_codes": len({c.get("code") for c in ug_gr}),
        "ug_gr_unique_subjects": len({c.get("subject") for c in ug_gr}),
        "with_description_pct": overall["with_description_pct"],
        "with_prerequisites_pct": overall["with_prerequisites_pct"],
        "with_corequisites_pct": pct(sum(1 for c in courses if c.get("corequisites")),
                                     len(courses)),
        "description_words": describe(word_count(c["description"]) for c in courses
                                      if c.get("description")),
        "by_level": {lv: summarize([c for c in courses if (c.get("level") or "?") == lv])
                     for lv in levels},
        "largest_subjects": [{"subject": s, "subject_name": name, "courses": n}
                             for (s, name), n in per_subject.most_common(10)],
    }


def pdf_catalog_rows(pdf_summary: Any, pdf_pages: Optional[List[dict]]) -> List[dict]:
    """One row per (edition, catalog) PDF: pages, words_clean, words_raw, empty_pages.

    Values come from pdf_summary.json; PDFs that only appear in pdf_pages.jsonl are
    counted from the page records (words_raw is then unknown).
    """
    rows: Dict[Tuple[str, str], dict] = {}
    for item in pdf_summary_items(pdf_summary):
        key = (str(item.get("edition") or ""), str(item.get("catalog") or item.get("kind") or ""))
        rows[key] = {"edition": key[0], "catalog": key[1], "pages": item.get("pages"),
                     "words_clean": item.get("words_clean"), "words_raw": item.get("words_raw"),
                     "empty_pages": item.get("empty_pages"), "file": item.get("file")}
    counted: Dict[Tuple[str, str], dict] = {}
    for rec in pdf_pages or []:
        key = (str(rec.get("edition") or ""), str(rec.get("catalog") or ""))
        agg = counted.setdefault(key, {"pages": 0, "words_clean": 0, "empty_pages": 0,
                                       "file": rec.get("pdf_file")})
        agg["pages"] += 1
        agg["words_clean"] += rec.get("word_count") or 0
        agg["empty_pages"] += 1 if not rec.get("word_count") else 0
    for key, agg in counted.items():
        row = rows.setdefault(key, {"edition": key[0], "catalog": key[1], "words_raw": None})
        for name, value in agg.items():
            if row.get(name) is None:
                row[name] = value
    return sorted(rows.values(), key=lambda r: (r["edition"], CATALOG_ORDER.get(r["catalog"], 9),
                                                r["catalog"]))


def pdf_summary_items(summary: Any) -> List[dict]:
    """The per-PDF entries of pdf_summary.json, wherever they are in the file."""
    if isinstance(summary, list):
        items = summary
    elif isinstance(summary, dict):
        items = []
        for key in ("pdfs", "catalogs", "files", "per_pdf", "items"):
            if isinstance(summary.get(key), list):
                items = summary[key]
                break
        else:
            for value in summary.values():
                if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                    items = value
                    break
                if isinstance(value, dict) and "edition" in value:
                    items.append(value)
    else:
        items = []
    return [i for i in items if isinstance(i, dict) and i.get("edition")
            and (i.get("catalog") or i.get("kind"))]


def pdf_stats(pdf_summary: Any, pdf_pages: Optional[List[dict]]) -> dict:
    rows = pdf_catalog_rows(pdf_summary, pdf_pages)
    if not rows:
        return {"available": False, "catalogs": []}
    target = {r["catalog"]: r for r in rows
              if r["edition"] == PROPOSAL_EDITION and r["catalog"] in ("undergraduate", "graduate")}
    complete = len(target) == 2

    def total(field: str) -> Optional[int]:
        values = [r.get(field) for r in target.values()]
        return sum(values) if complete and all(v is not None for v in values) else None

    return {
        "available": True,
        "catalogs": rows,
        "total_pages": sum(r.get("pages") or 0 for r in rows),
        "proposal_edition": {
            "edition": PROPOSAL_EDITION,
            "complete": complete,
            "pages": {c: r.get("pages") for c, r in target.items()},
            "pages_total": total("pages"),
            "words_raw_total": total("words_raw"),
            "words_clean_total": total("words_clean"),
        },
    }


def document_summary_items(summary: Any) -> List[dict]:
    """The per-PDF entries of documents_summary.json (a list of dicts with a URL), wherever
    the `docs` step put them."""
    if isinstance(summary, list):
        candidates = [summary]
    elif isinstance(summary, dict):
        candidates = [summary.get(k) for k in ("documents", "docs", "pdfs", "files", "items",
                                               "per_document", "per_doc")]
        candidates += list(summary.values())
    else:
        candidates = []
    for value in candidates:
        if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
            value = [dict(v, url=v.get("url") or k) for k, v in value.items()]
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) \
                and any(v.get("url") or v.get("pdf_url") for v in value):
            return value
    return []


def document_stats(documents: Optional[List[dict]], summary: Any,
                   docs_manifest: Optional[List[dict]]) -> dict:
    """Documents linked from the NYIT sites (the `docs` step): PDFs and Word, Excel and
    PowerPoint files (``file_type``). Scans and bookmarks are counted for PDFs only."""
    if documents is None and docs_manifest is None and summary is None:
        return {"available": False}
    pages = documents or []
    by_pdf: Dict[str, List[dict]] = defaultdict(list)
    for rec in pages:
        by_pdf[str(rec.get("pdf_url") or str(rec.get("url") or "").split("#")[0])].append(rec)
    # the `docs` step's own verdicts (scanned = most pages are images without text; bookmarks
    # = the PDF has an outline) when documents_summary.json has them, so both files agree
    items = {str(i.get("pdf_url") or i.get("url")): i for i in document_summary_items(summary)}
    per_site: Dict[str, dict] = {}
    pdf_rows = []
    by_type: Dict[str, dict] = {}
    for pdf_url, recs in by_pdf.items():
        host = str(recs[0].get("site") or url_host(pdf_url))
        file_type = str(recs[0].get("file_type") or "pdf")
        is_pdf = file_type == "pdf"
        words = sum(r.get("word_count") or 0 for r in recs)
        empty = sum(1 for r in recs if not r.get("word_count"))
        item = items.get(pdf_url) or {}
        scanned = is_pdf and (bool(item["scanned"]) if "scanned" in item else empty == len(recs))
        bookmarks = is_pdf and (bool(item["toc_entries"]) if "toc_entries" in item
                                else any(r.get("toc_path") for r in recs))
        row = per_site.setdefault(host, {"site": host, "documents": 0, "pdfs": 0, "pages": 0,
                                         "words": 0, "empty_pages": 0, "scanned_pdfs": 0})
        row["documents"] += 1
        row["pdfs"] += 1 if is_pdf else 0
        row["pages"] += len(recs)
        row["words"] += words
        row["empty_pages"] += empty
        row["scanned_pdfs"] += 1 if scanned else 0
        kind = by_type.setdefault(file_type, {"documents": 0, "pages": 0, "words": 0})
        kind["documents"] += 1
        kind["pages"] += len(recs)
        kind["words"] += words
        pdf_rows.append({"pdf_url": pdf_url, "site": host, "file_type": file_type,
                         "title": recs[0].get("title") or "",
                         "pages": len(recs), "words": words, "empty_pages": empty,
                         "scanned": scanned, "has_bookmarks": bookmarks})
    downloads = list(load_manifest_rows(docs_manifest))
    statuses = Counter(d.get("status") or "?" for d in downloads)
    ok = [d for d in downloads if d.get("status") == "ok"]
    unique_files = {d.get("sha1") or d.get("file") or d.get("url") for d in ok}
    links = None
    if isinstance(summary, dict):   # {"pdf_links": N, "candidates": M} (key name varied)
        links = next((summary[k] for k in ("input", "links")
                      if isinstance(summary.get(k), dict)), None)
    skipped = summary.get("skipped") if isinstance(summary, dict) else None
    categories: Counter = Counter()
    superseded: Dict[str, int] = {}
    cohorts = set()
    for rec in pages:
        categories[rec.get("doc_category") or "other"] += 1
        url = str(rec.get("pdf_url") or "")
        if rec.get("superseded"):
            superseded[url] = superseded.get(url, 0) + 1
        if rec.get("cohort_year"):
            cohorts.add(url)
    extra = summary if isinstance(summary, dict) else {}
    return {
        "available": True,
        # pages per kind of document (documents.DOC_CATEGORIES)
        "pages_by_category": dict(categories.most_common()),
        "superseded_pdfs": len(superseded),           # documents of any file type
        "superseded_pages": sum(superseded.values()),
        "cohort_pdfs": len(cohorts),
        "series": (extra.get("editions") or {}).get("series"),
        "titles": extra.get("titles") or {},
        "not_extracted_files": extra.get("not_extracted_files") or {},
        # from documents_summary.json (when the `docs` step wrote these keys)
        "links_found": links.get("pdf_links") if isinstance(links, dict) else None,
        "office_links_found": links.get("office_links") if isinstance(links, dict) else None,
        "nyit_pdfs": links.get("candidates") if isinstance(links, dict) else None,
        "not_downloaded": {str(k): v for k, v in skipped.items() if isinstance(v, int)}
        if isinstance(skipped, dict) else {},
        "pdf_links": len(downloads) if docs_manifest is not None else None,
        "statuses": dict(statuses.most_common()),
        "downloaded": len(ok) if docs_manifest is not None else None,
        "unique_files": len(unique_files) if docs_manifest is not None else None,
        "aliases": len(ok) - len(unique_files) if docs_manifest is not None else None,
        "megabytes": round(sum(d.get("bytes") or 0 for d in ok) / 1e6, 1)
        if docs_manifest is not None else None,
        "documents": len(by_pdf),
        "pdfs": sum(1 for r in pdf_rows if r["file_type"] == "pdf"),
        # documents, pages and words per file type: pdf, docx, xlsx, pptx
        "by_file_type": {t: by_type[t] for t in sorted(by_type, key=_file_type_rank)},
        "pages": len(pages),
        "words": describe(r.get("word_count") or 0 for r in pages),
        "empty_pages": sum(1 for r in pages if not r.get("word_count")),
        "scanned_pdfs": sum(1 for r in pdf_rows if r["pages"] and r["scanned"]),
        "with_bookmarks": sum(1 for r in pdf_rows if r["has_bookmarks"]),
        "pages_per_pdf": describe(r["pages"] for r in pdf_rows if r["file_type"] == "pdf"),
        "by_site": sorted(per_site.values(), key=lambda r: site_rank(r["site"])),
        "largest": sorted(pdf_rows, key=lambda r: (-r["words"], r["pdf_url"]))[:5],
        "summary_items": len(document_summary_items(summary)),
    }


def _file_type_rank(file_type: str) -> Tuple[int, str]:
    order = list(FILE_TYPE_NAMES)
    return (order.index(file_type) if file_type in order else len(order), file_type)


def document_count_text(docs: dict) -> str:
    """"60 PDFs" when only PDFs were extracted, else "65 documents (60 PDFs, 3 Word files,
    2 Excel files)"."""
    total = docs.get("documents", docs.get("pdfs")) or 0
    kinds = docs.get("by_file_type") or {}
    if not kinds or set(kinds) == {"pdf"}:
        return plural(total, "PDF")
    names = [plural(v["documents"], "PDF" if t == "pdf" else f"{FILE_TYPE_NAMES.get(t, t)} file")
             for t, v in kinds.items()]
    return f"{plural(total, 'document')} ({', '.join(names)})"


def load_manifest_rows(rows: Optional[List[dict]]) -> Iterable[dict]:
    """docs_manifest.jsonl: the last line per URL wins (like the crawl manifest)."""
    latest: Dict[str, dict] = {}
    for rec in rows or []:
        if rec.get("url"):
            latest[rec["url"]] = rec
    return latest.values()


def corpus_stats(corpus: Optional[List[dict]]) -> dict:
    if corpus is None:
        return {"available": False}
    words_by_source: Dict[str, List[int]] = {}
    by_site: Dict[str, Counter] = defaultdict(Counter)
    for rec in corpus:
        source = rec.get("source") or "?"
        words = rec["words"] if "words" in rec else word_count(rec.get("text") or "")
        words_by_source.setdefault(source, []).append(words)
        meta = rec.get("metadata") or {}
        by_site[str(meta.get("site") or url_host(str(rec.get("url") or "")))][source] += 1
    emails = Counter(e for rec in corpus for e in rec.get("emails") or [])
    with_email = [rec for rec in corpus if rec.get("emails")]
    return {
        "available": True,
        "records": len(corpus),
        "words": describe(w for ws in words_by_source.values() for w in ws),
        "by_source": {src: {"records": len(ws), "words": describe(ws)}
                      for src, ws in sorted(words_by_source.items(),
                                            key=lambda kv: -len(kv[1]))},
        "by_site": [{"site": host, "records": sum(c.values()), "by_source": dict(c)}
                    for host, c in sorted(by_site.items(), key=lambda kv: site_rank(kv[0]))],
        # contact details printed on NYIT's public pages (kept in the text, see the README)
        "contacts": {
            "records_with_email": len(with_email),
            "distinct_emails": len(emails),
            "distinct_nyit_emails": sum(1 for e in emails if e.endswith("nyit.edu")),
            "records_with_email_by_source": dict(Counter(r.get("source") for r in with_email)
                                                 .most_common()),
            "records_with_phone": sum(1 for rec in corpus if rec.get("has_phone")),
        },
    }


def corpus_cleanup_stats(corpus: Optional[List[dict]], parse_summary: Any) -> dict:
    """What `parse` left out of the corpus or grouped: near-duplicate passages, short and
    superseded PDF document pages, merged short sections (from parse_summary.json)."""
    summary = parse_summary if isinstance(parse_summary, dict) else {}
    near = summary.get("near_duplicates") or {}
    groups = Counter(str((r.get("metadata") or {}).get("dup_group")) for r in corpus or []
                     if (r.get("metadata") or {}).get("dup_group"))
    return {
        "near_duplicates_collapsed": bool(near.get("enabled")),
        "near_duplicates_left_out": near.get("left_out"),
        "near_duplicates_left_out_by_source": near.get("left_out_by_source") or {},
        "dup_groups": len(groups),
        "records_in_dup_groups": sum(groups.values()),
        "document_pages_left_out": summary.get("document_pages_left_out") or {},
        "tiny_sections_merged": summary.get("tiny_sections_merged"),
        "corpus_with_superseded": summary.get("corpus_with_superseded"),
    }


def site_stats(pages: Optional[List[dict]], sections: Optional[List[dict]],
               corpus: Optional[List[dict]], crawl: dict, parse_summary: Any) -> List[dict]:
    """One row per website: pages, words, passages, corpus records, page types."""
    rows: Dict[str, dict] = {}

    def row(host: str) -> dict:
        if host not in rows:
            rows[host] = {"site": host, "name": site_name(host), "saved": None, "pages": 0,
                          "distinct": 0, "duplicates": 0, "noindex": 0, "words": 0,
                          "page_words": [], "sections": 0, "section_words": [],
                          "corpus_records": 0, "page_types": Counter(),
                          "boilerplate_blocks": None}
        return rows[host]

    for p in pages or []:
        r = row(site_of(p))
        r["pages"] += 1
        if p.get("duplicate_of"):
            r["duplicates"] += 1
            continue
        r["distinct"] += 1
        r["noindex"] += 1 if p.get("noindex") else 0
        r["words"] += p.get("word_count") or 0
        r["page_words"].append(p.get("word_count") or 0)
        r["page_types"][p.get("page_type") or "other"] += 1
    for s in sections or []:
        r = row(site_of(s))
        r["sections"] += 1
        r["section_words"].append(s.get("word_count") or 0)
    for rec in corpus or []:
        meta = rec.get("metadata") or {}
        row(str(meta.get("site") or url_host(str(rec.get("url") or ""))))["corpus_records"] += 1
    for crawl_row in crawl.get("sites") or []:
        if crawl_row.get("ok") and crawl_row["site"] in rows:
            rows[crawl_row["site"]]["saved"] = crawl_row["ok"]
    by_site = parse_summary.get("by_site") if isinstance(parse_summary, dict) else None
    for host, info in (by_site or {}).items():
        if host in rows and isinstance(info, dict):
            rows[host]["boilerplate_blocks"] = info.get("boilerplate_blocks")
    out = []
    for host in ordered_hosts(rows):
        r = rows[host]
        page_words, section_words = r.pop("page_words"), r.pop("section_words")
        r["median_page_words"] = statistics.median(page_words) if page_words else None
        r["median_section_words"] = statistics.median(section_words) if section_words else None
        r["page_types"] = dict(r["page_types"].most_common())
        if r["pages"] or r["corpus_records"]:
            out.append(r)
    return out


def proposal_comparison(st: dict) -> List[dict]:
    """Rows of the "Proposal vs. scraped" table (config.PROPOSAL_TARGETS)."""
    targets = config.PROPOSAL_TARGETS
    pdf = st["pdf"].get("proposal_edition") or {}
    pdf_pages = pdf.get("pages") or {}
    courses, pages = st["courses"], st["pages"]
    rows: List[dict] = []

    def row(metric: str, key: str, scraped: Optional[int], note: str, shown: str = "") -> None:
        target = targets.get(key)
        rows.append({"metric": metric, "target_key": key, "proposal": target,
                     "proposal_shown": shown or fmt_num(target), "scraped": scraped,
                     "difference": difference(scraped, target), "note": note})

    row("Catalog PDF pages, 2025-26 (UG + GR)", "pdf_pages_2025_2026", pdf.get("pages_total"),
        "full printed catalogs" if pdf.get("complete") else f"not extracted yet: {RUN_PDF}")
    row("- undergraduate catalog", "pdf_pages_undergraduate_2025_2026",
        pdf_pages.get("undergraduate"), "")
    row("- graduate catalog", "pdf_pages_graduate_2025_2026", pdf_pages.get("graduate"), "")
    words_clean = pdf.get("words_clean_total")
    row("Words in those PDFs", "pdf_words_2025_2026", pdf.get("words_raw_total"),
        f"proposal's method; {fmt_num(words_clean)} after cleaning" if words_clean else "",
        shown=f"~{targets['pdf_words_2025_2026'] // 1000}K")

    if courses.get("available"):
        code_note = f"website, 2026-27; {fmt_num(courses['unique_codes'])} incl. professional"
        subject_note = f"{fmt_num(courses['unique_subjects'])} incl. professional"
    else:
        code_note = subject_note = f"not parsed yet: {RUN_PARSE}"
    row("Course codes (UG + GR)", "course_codes", courses.get("ug_gr_unique_codes"), code_note,
        shown=f"{fmt_num(targets['course_codes'])}+")
    row("Subject areas (UG + GR)", "subject_areas", courses.get("ug_gr_unique_subjects"),
        subject_note)

    if pages.get("available"):
        schools = pages.get("schools") or []
        missing = [s for s in config.SCHOOL_BY_PATH.values() if s not in schools]
        campuses = list(pages.get("campuses") or {})
        if not schools:
            school_note = "none found yet (crawl incomplete?)"
        elif missing:
            school_note = f"missing: {_short_list(missing)}"
        else:
            school_note = "from the web pages"
        row("Schools and colleges", "schools", len(schools), school_note)
        row("Campuses", "campuses", len(campuses), ", ".join(campuses) or "none found")
    else:
        row("Schools and colleges", "schools", None, f"not parsed yet: {RUN_PARSE}")
        row("Campuses", "campuses", None, "")
    return rows


def difference(scraped: Optional[float], target: Optional[float]) -> str:
    if scraped is None or not target:
        return ""
    if scraped == target:
        return "exact"
    return f"{100.0 * (scraped - target) / target:+.1f}%"


def _short_list(names: Sequence[str]) -> str:
    short = [n.replace("College of ", "").replace("School of ", "") for n in names]
    return ", ".join(short) or "none found"


# ---------------------------------------------------------------------------
# the report (shared by the console and STATS.md)
# ---------------------------------------------------------------------------
def report_blocks(st: dict) -> List[Block]:
    blocks: List[Block] = [("h1", "AskBear dataset statistics"), ("p", _intro(st))]
    blocks += [("p", paragraph) for paragraph in _overview(st)]
    blocks += [("h2", "At a glance"), ("kv", _glance(st))]
    blocks += _site_blocks(st)
    blocks.append(("h2", "Proposal vs. scraped"))
    blocks.append(("table", (
        ["Metric", "Proposal", "Scraped", "Difference", "Note"],
        [[r["metric"], r["proposal_shown"], fmt_num(r["scraped"]), r["difference"], r["note"]]
         for r in st["proposal_vs_scraped"]],
        "lrrrl")))
    blocks.append(("p", "Proposal = slide 9 of the AskBear proposal, measured on the 2025-26 "
                        "undergraduate and graduate catalog PDFs. Course and subject counts "
                        "come from the live catalog's course directory (2026-27 edition), "
                        "schools and campuses from the parsed web pages of all sites."))
    blocks += _crawl_blocks(st["crawl"])
    blocks += _page_blocks(st["pages"])
    blocks += _section_blocks(st["sections"], small_sites(st))
    blocks += _course_blocks(st["courses"])
    blocks += _pdf_blocks(st["pdf"])
    blocks += _document_blocks(st["documents"], st["corpus"])
    blocks += _corpus_blocks(st["corpus"], small_sites(st))
    blocks += [("h2", "How the numbers were measured"), ("bullets", [
        "Words = whitespace-separated tokens (`len(text.split())`), the method used for "
        "the proposal's numbers.",
        "Web page text is the main content area only (no menus, sidebar, header or footer); "
        "tables are kept as one \"cell | cell\" line per row. On every site except the "
        "catalog, blocks that repeat on a large share of the site's pages (calls to action, "
        "\"Request Information / Plan Your Visit / Apply Today\", cookie notices) are left out.",
        "A URL that redirects to another NYIT site (e.g. nyit.edu -> www.nyit.edu) is "
        "counted on the site where its content lives.",
        "Pages whose text is identical to another page (on any site) are counted once "
        "(\"distinct\"); the copy kept is on the catalog first, then www.nyit.edu, then the "
        "other sites. The copies stay in pages.jsonl with `duplicate_of` set.",
        "Pages that ask search engines not to index them (`noindex`) and pages with fewer "
        f"than {MIN_INDEXED_WORDS} words (\"Coming soon!\") stay in pages.jsonl but get no "
        "passages and no corpus records. Error pages served with HTTP 200 (\"404 | ...\", "
        "empty catalog program templates) are left out.",
        "Sections are the search passages: a page is split at its h2-h6 headings and very "
        "long sections are split further into parts.",
        "Courses are de-duplicated by code and level (course_id), keeping the record with "
        "the longest description. The legacy registrar codes listed under \"Conversion - OM\" "
        "(0 credits, no description) are counted as courses but left out of corpus.jsonl.",
        "PDF \"raw\" words use PyMuPDF's plain page text, like the proposal; \"clean\" words "
        "are counted after removing running headers/footers and normalising the text. "
        "PDF documents linked from the sites are extracted the same way; a PDF whose "
        "pages have no text at all is counted as scanned (image-only). Word, Excel and "
        "PowerPoint files (.docx, .xlsx, .pptx) are read from their XML and cut into pages: "
        "about 150-400 words of a Word file, rows of one sheet, one slide.",
    ])]
    return blocks


def _intro(st: dict) -> str:
    crawl, pages = st["crawl"], st["pages"]
    text = f"Generated {fmt_time(st['generated_at'])} by `python -m askbear_scraper stats`"
    if pages.get("catalog_year"):
        text += f"; the catalog is the New York Tech Catalog {pages['catalog_year']}"
    if crawl.get("first_fetch"):
        first, last = crawl["first_fetch"][:10], crawl["last_fetch"][:10]
        text += f"; pages fetched {first}" + (f" to {last}" if last != first else "")
    return text + "."


def _overview(st: dict) -> List[str]:
    """The paragraphs that open the report: "We scraped N pages from K NYIT websites ...",
    then the other sources and the search corpus."""
    crawl, pages, secs = st["crawl"], st["pages"], st["sections"]
    courses, docs, pdf, corpus = st["courses"], st["documents"], st["pdf"], st["corpus"]
    site_rows = [r for r in st["sites"] if r["pages"]]
    parts: List[str] = []
    more: List[str] = []
    if pages.get("available") and site_rows:
        hosts = [r["site"] for r in sorted(site_rows, key=lambda r: (-r["pages"], r["site"]))]
        text = (f"We scraped {fmt_num(pages['records'])} pages from {plural(len(hosts), 'NYIT website')} "
                f"({_join_names(hosts)})")
        megabytes = crawl.get("parsed_html_megabytes") or crawl.get("html_megabytes")
        if crawl.get("available") and megabytes:
            text += f", {fmt_num(megabytes, 1)} MB of HTML"
        parts.append(text + ".")
        text = (f"After setting aside {plural(pages['duplicates'], 'exact copy', 'exact copies')} "
                f"of other pages, {fmt_num(pages['distinct'])} distinct pages with "
                f"{fmt_num(pages['words']['total'])} words remain")
        if secs.get("available"):
            text += (f"; they are split into {fmt_num(secs['count'])} search passages (median "
                     f"{fmt_num(secs['words']['median'])} words)")
        parts.append(text + ".")
        if pages.get("noindex"):
            parts.append(f"{plural(pages['noindex'], 'distinct page')}"
                         f"{_records_note(pages)} that ask search engines not to "
                         f"index them {'is' if pages['noindex'] == 1 else 'are'} kept but not "
                         "indexed.")
    elif crawl.get("available"):
        parts.append(f"The crawl has saved {fmt_num(crawl['pages_ok'])} pages so far; run "
                     f"{RUN_PARSE} to turn them into datasets.")
    if courses.get("available") and courses.get("records"):
        more.append(f"The catalog's course directory adds {plural(courses['records'], 'course')} "
                    f"({plural(courses['unique_codes'], 'unique code')} in "
                    f"{plural(courses['unique_subjects'], 'subject')}).")
    n_docs = docs.get("documents", docs.get("pdfs")) if docs.get("available") else 0
    if n_docs and set(docs.get("by_file_type") or {"pdf": 1}) == {"pdf"}:
        more.append(f"{plural(n_docs, 'PDF document')} linked from the sites (forms, "
                    f"handbooks, reports...) {'adds' if n_docs == 1 else 'add'} "
                    f"{plural(docs['pages'], 'page')} "
                    f"({plural(docs['words']['total'], 'word')}).")
    elif n_docs:
        more.append(f"{document_count_text(docs)} linked from the sites (forms, handbooks, "
                    f"reports, spreadsheets, slides...) add {plural(docs['pages'], 'page')} "
                    f"({plural(docs['words']['total'], 'word')}).")
    if pdf.get("available"):
        editions = sorted({r["edition"] for r in pdf["catalogs"]})
        more.append(f"The full catalog PDF{'s' if len(pdf['catalogs']) != 1 else ''} "
                    f"({', '.join(editions)}) {'have' if len(pdf['catalogs']) != 1 else 'has'} "
                    f"{plural(pdf['total_pages'], 'page')}.")
    if corpus.get("available"):
        sources = ", ".join(f"{fmt_num(v['records'])} {_source_label(src, v['records'])}"
                            for src, v in corpus["by_source"].items())
        more.append(f"The search corpus (corpus.jsonl) has {fmt_num(corpus['records'])} "
                    f"records: {sources}.")
    return [" ".join(p) for p in (parts, more) if p]


def _records_note(pages: dict) -> str:
    """" (65 page records including copies)" when copies of noindex pages exist."""
    records = pages.get("noindex_records")
    if records is None or records == pages.get("noindex"):
        return ""
    return f" ({plural(records, 'page record')} including copies)"


def _join_names(names: Sequence[str], limit: int = 6) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


SOURCE_LABELS = {"section": ("web page passage", "web page passages"),
                 "course": ("course", "courses"),
                 "document": ("document page (PDF/Office)", "document pages (PDF/Office)"),
                 "pdf_page": ("catalog PDF page", "catalog PDF pages")}


def _source_label(source: str, n: int) -> str:
    one, many = SOURCE_LABELS.get(source, (source, source))
    return one if n == 1 else many


def _glance(st: dict) -> List[Tuple[str, str]]:
    crawl, pages, secs = st["crawl"], st["pages"], st["sections"]
    courses, pdf, corpus, docs = st["courses"], st["pdf"], st["corpus"], st["documents"]
    rows: List[Tuple[str, str]] = []
    if crawl.get("available"):
        size = fmt_num(crawl["html_megabytes"], 1)
        rows.append(("Websites with saved pages", fmt_num(crawl["sites_with_pages"])))
        rows.append(("Web pages saved by the crawler",
                     f"{fmt_num(crawl['pages_ok'])} ({size} MB of HTML)"))
    else:
        rows.append(("Web pages saved by the crawler", f"n/a: run {RUN_CRAWL}"))
    if pages.get("available"):
        copies = plural(pages["duplicates"], "exact copy", "exact copies")
        rows.append(("Distinct pages parsed", f"{fmt_num(pages['distinct'])} ({copies} set aside)"))
        rows.append(("Words on those pages",
                     f"{fmt_num(pages['words']['total'])} "
                     f"({fmt_num(pages['words_outside_course_directory'])} outside the course "
                     "directory)"))
        if pages.get("catalog_pages"):
            program_pages = plural(pages["program_pages"], "program page")
            curriculum_pages = plural(pages["curriculum_pages"], "curriculum page")
            rows.append(("Catalog programs (degrees, minors, certificates)",
                         f"{fmt_num(pages['programs'])} on {program_pages} + {curriculum_pages}"))
        if pages.get("degree_pages"):
            rows.append(("Degree pages outside the catalog", fmt_num(pages["degree_pages"])))
    else:
        rows.append(("Distinct pages parsed", f"n/a: run {RUN_PARSE}"))
    if secs.get("available"):
        rows.append(("Search passages (sections)",
                     f"{fmt_num(secs['count'])}, median {fmt_num(secs['words']['median'])} words"))
    if courses.get("available"):
        rows.append(("Courses", f"{fmt_num(courses['records'])} records, "
                                f"{fmt_num(courses['unique_codes'])} unique codes in "
                                f"{fmt_num(courses['unique_subjects'])} subjects"))
    if docs.get("available") and docs.get("documents", docs.get("pdfs")):
        rows.append(("Documents from the sites (PDF, Word, Excel, PowerPoint)",
                     f"{document_count_text(docs)}, {fmt_num(docs['pages'])} pages, "
                     f"{fmt_num(docs['words']['total'])} words"))
    else:
        rows.append(("Documents from the sites (PDF, Word, Excel, PowerPoint)",
                     f"n/a: run {RUN_DOCS}"))
    if pdf.get("available"):
        editions = sorted({r["edition"] for r in pdf["catalogs"]})
        rows.append(("Catalog PDF pages", f"{fmt_num(pdf['total_pages'])} pages in "
                                          f"{len(pdf['catalogs'])} PDFs ({', '.join(editions)})"))
    else:
        rows.append(("Catalog PDF pages", f"n/a: run {RUN_PDF}"))
    if corpus.get("available"):
        parts = ", ".join(f"{src} {fmt_num(v['records'])}"
                          for src, v in corpus["by_source"].items())
        rows.append(("Search corpus records", f"{fmt_num(corpus['records'])} ({parts})"))
    return rows


def _site_blocks(st: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Websites")]
    rows = st["sites"]
    if not rows:
        return blocks + [("p", f"No parsed pages yet: run {RUN_CRAWL}, then {RUN_PARSE}.")]
    small = small_sites(st)
    shown = profiled_first(r for r in rows if r["site"] not in small)
    if small:
        merged = {"site": f"{plural(len(small), 'other website')}", "name": "found through links",
                  "page_types": Counter()}
        for key in ("pages", "distinct", "words", "sections", "corpus_records"):
            merged[key] = sum(r[key] for r in rows if r["site"] in small)
        for r in rows:
            if r["site"] in small:
                merged["page_types"].update(r["page_types"])
        merged["page_types"] = dict(merged["page_types"].most_common())
        shown.append(merged)
    table = []
    for r in shown:
        types = ", ".join(f"{t} {fmt_num(n)}" for t, n in list(r["page_types"].items())[:3])
        if len(r["page_types"]) > 3:
            types += ", ..."
        name = r["name"] if r["site"] in sites.SITES else "found through links"
        table.append([r["site"], name, fmt_num(r["pages"]), fmt_num(r["distinct"]),
                      fmt_num(r["words"]), fmt_num(r["sections"]), fmt_num(r["corpus_records"]),
                      types])
    total = ["total", "", fmt_num(sum(r["pages"] for r in rows)),
             fmt_num(sum(r["distinct"] for r in rows)), fmt_num(sum(r["words"] for r in rows)),
             fmt_num(sum(r["sections"] for r in rows)),
             fmt_num(sum(r["corpus_records"] for r in rows)), ""]
    blocks.append(("table", (["Website", "Content", "Pages", "Distinct", "Words", "Passages",
                              "Corpus records", "Main page types"], table + [total],
                             "llrrrrrl")))
    notes = ["Pages = page records in pages.jsonl; Distinct = without exact copies of other "
             "pages; Words = words on the distinct pages; Passages = sections in "
             "sections.jsonl; Corpus records = everything corpus.jsonl holds for the site "
             "(passages, courses, PDF pages)."]
    if small:
        notes.append(f"Other websites: {_join_names(sorted(small), 12)} (each with fewer than "
                     f"{MIN_SITE_ROW_PAGES} pages; found through links, crawled with the default "
                     "profile).")
    moved = st["crawl"].get("content_on_other_site") if st["crawl"].get("available") else 0
    if moved:
        notes.append(f"{plural(moved, 'saved URL')} on legacy hosts such as nyit.edu and "
                     "site.nyit.edu redirect to another NYIT site and are counted there.")
    blocks.append(("p", " ".join(notes)))
    return blocks


def profiled_first(rows: Iterable[dict]) -> List[dict]:
    """Table rows of the websites of sites.py first (catalog, www, then A-Z), then the
    websites found through links."""
    return sorted(rows, key=lambda r: (r["site"] not in sites.SITES, site_rank(r["site"])))


def _merge_only_groups(small: Sequence[str], present: Sequence[str]) -> List[str]:
    """The small websites of a table, or none when fewer than two of them are in it."""
    here = [h for h in small if h in present]
    return here if len(here) > 1 else []


def small_sites(st: dict) -> List[str]:
    """Websites found through links that have too few pages for their own table row
    (only when there are at least two of them)."""
    small = [r["site"] for r in st.get("sites") or []
             if r["site"] not in sites.SITES and r["pages"] < MIN_SITE_ROW_PAGES]
    return small if len(small) > 1 else []


def _crawl_blocks(crawl: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Crawl")]
    if not crawl.get("available"):
        return blocks + [("p", f"No crawl manifest found: run {RUN_CRAWL}.")]
    summary = crawl.get("summary")
    saved = f"{fmt_num(crawl['pages_ok'])} on {plural(crawl['hosts_with_saved_urls'], 'host')}"
    if crawl["hosts_with_saved_urls"] != crawl["sites_with_pages"]:
        saved += (f" ({plural(crawl['sites_with_pages'], 'website')} after redirects"
                  + (f"; {_join_names(crawl.get('hosts_not_parsed') or [], 4)} not parsed "
                     "(disabled in sites.py)" if crawl.get("hosts_not_parsed") else "") + ")")
    rows = [("URLs in the manifest", fmt_num(crawl["urls"])),
            ("Pages saved (status ok)", saved),
            ("First / last page fetched", f"{fmt_time(crawl['first_fetch'])} / "
                                          f"{fmt_time(crawl['last_fetch'])}")]
    if summary:
        state = "finished" if crawl["finished"] else (
            "interrupted" if summary.get("interrupted") else
            f"stopped with {fmt_num(summary.get('still_queued'))} URLs still queued")
        state = (f"{state} at {fmt_time(summary.get('finished_at'))}, "
                 f"{_duration(summary.get('seconds'))}")
        if crawl.get("newer_crawl"):
            state += ("; pages were fetched after that, so a newer crawl is running or stopped "
                      "early (its summary is not written yet)")
        runs = crawl.get("runs") or []
        if len(runs) > 1:
            # a full crawl topped up by `crawl --site` runs: totals cover every run
            rows += [("Crawl runs", "; ".join(
                f"{fmt_time(r.get('finished_at'))}: {_join_names(r.get('sites') or [], 3)} "
                f"({fmt_num(r.get('urls_visited'))} URLs, {_duration(r.get('seconds'))})"
                for r in runs)),
                ("Crawl state", state),
                ("Network requests (all runs)", fmt_num(summary.get("network_requests")))]
        else:
            rows += [("Last crawl run", state),
                     ("Network requests (last run)", fmt_num(summary.get("network_requests")))]
        rows.append(("PDF / external links found", f"{fmt_num(summary.get('pdf_links'))} / "
                                                   f"{fmt_num(summary.get('external_links'))}"))
        if crawl.get("sites_in_last_run") is None:
            rows.append(("Websites in the last run", "catalog.nyit.edu only (crawl_summary.json "
                                                     "written by the older catalog-only crawler)"))
        elif len(runs) <= 1:
            rows.append(("Websites in the last run", _join_names(crawl["sites_in_last_run"], 12)))
        if crawl.get("failed_sites"):
            reasons = crawl.get("unreachable_reasons") or {}
            rows.append(("Enabled websites with no saved page", "; ".join(
                f"{h} ({reasons[h]})" if reasons.get(h) else h for h in crawl["failed_sites"])))
    else:
        rows.append(("Last crawl run", f"no {CRAWL_SUMMARY_FILE} yet (crawl still running "
                                       "or interrupted)"))
    blocks.append(("kv", rows))

    site_rows = [r for r in crawl["sites"] if r["ok"]]
    # hosts where nothing was saved (login portals, dead links...) share one row
    empty_rows = [r for r in crawl["sites"] if r["urls"] and not r["ok"]]
    if empty_rows:
        merged: Counter = Counter()
        for r in empty_rows:
            merged.update(r["statuses"])
        site_rows = site_rows + [{
            "site": f"{plural(len(empty_rows), 'other host')} (nothing saved)",
            "urls": sum(r["urls"] for r in empty_rows), "statuses": dict(merged),
            "html_megabytes": 0.0}]
    columns = [s for s in STATUS_ORDER if any(r["statuses"].get(s) for r in site_rows)]
    columns += sorted({s for r in site_rows for s in r["statuses"]} - set(columns))
    blocks.append(("h3", "Crawl status by website"))
    table = [[r["site"], fmt_num(r["urls"])] + [_blank_zero(r["statuses"].get(s)) for s in columns]
             + [fmt_num(r["html_megabytes"], 1)] for r in site_rows]
    blocks.append(("table", (["Website", "URLs"] + [STATUS_LABEL.get(s, s) for s in columns]
                             + ["MB"], table, "lr" + "r" * len(columns) + "r")))
    if empty_rows:
        names = [r["site"] for r in empty_rows]
        blocks.append(("p", f"Hosts without saved pages: {_join_names(names, 10)}. Saved = pages "
                            "stored and parsed; the other columns are URLs that were skipped "
                            "(see the table below)."))
    blocks.append(("table", (["Status", "URLs", "Meaning"],
                             [[s, fmt_num(n), STATUS_MEANING.get(s, "")]
                              for s, n in crawl["status_counts"].items()], "lrl")))

    mapped = [r for r in crawl["sites"] if r.get("sitemap_urls")]
    if mapped:
        blocks.append(("h3", "Sitemaps vs. saved pages"))
        table = []
        for r in mapped:
            not_saved = ", ".join(f"{STATUS_LABEL.get(s, s)} {fmt_num(n)}"
                                  for s, n in (r.get("sitemap_not_saved") or {}).items())
            table.append([r["site"], fmt_num(r["sitemap_urls"]), fmt_num(r["sitemap_in_scope"]),
                          fmt_num(r["sitemap_saved"]), not_saved or "-",
                          fmt_num(r["saved_not_in_sitemap"])])
        blocks.append(("table", (["Website", "In sitemaps", "In scope", "Saved",
                                  "Not saved (why)", "Saved, found via links"], table,
                                 "lrrrlr")))
        blocks.append(("p", "In scope = sitemap URLs that the site's rules in sites.py allow "
                            "(e.g. only /faq/ pages on libanswers.nyit.edu). Saved pages that "
                            "no sitemap lists were found by following links."))
    notes = []
    if crawl.get("capped_sites"):
        notes.append("Page limit reached (sites.py `max_pages`), URLs left out: " + ", ".join(
            f"{h} {fmt_num(n)}" for h, n in sorted(crawl["capped_sites"].items(),
                                                     key=lambda kv: site_rank(kv[0]))) + ".")
    if crawl.get("given_up_sites"):
        notes.append("Websites that kept failing (the crawler gave up on their queued URLs): "
                     + ", ".join(f"{h} {fmt_num(n)}" for h, n in sorted(
                         crawl["given_up_sites"].items(), key=lambda kv: site_rank(kv[0]))) + ".")
    if crawl.get("unreachable_sites"):
        notes.append("Websites that could not be reached at all: "
                     + _join_names(sorted(crawl["unreachable_sites"], key=site_rank), 10) + ".")
    if crawl.get("discovered_sites"):
        notes.append("NYIT websites found through links (crawled with the default profile, "
                     f"at most {sites.DEFAULT_PROFILE.max_pages} pages each): "
                     + ", ".join(crawl["discovered_sites"]) + ".")
    skipped = crawl.get("linked_but_not_crawled_sites") or {}
    if skipped:
        parts = []
        for host, n in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
            profile = sites.SITES.get(host)
            reason = f" ({profile.reason})" if profile is not None and profile.reason else ""
            parts.append(f"{host}{reason}: {plural(n, 'link')}")
        more = f"; {len(skipped) - 8} more" if len(skipped) > 8 else ""
        notes.append("Linked but deliberately not crawled: " + "; ".join(parts) + more + ".")
    if notes:
        blocks.append(("bullets", notes))
    return blocks


def _page_blocks(pages: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Web pages")]
    if not pages.get("available"):
        return blocks + [("p", f"No {PAGES_FILE} yet: run {RUN_PARSE}.")]
    w = pages["words"]
    kv = [
        ("Page records", f"{fmt_num(pages['records'])} ({fmt_num(pages['distinct'])} distinct, "
                         f"{plural(pages['duplicates'], 'exact copy', 'exact copies')})"),
        ("Words (distinct pages)", f"{fmt_num(w['total'])} total, median {fmt_num(w['median'])} "
                                   f"per page, max {fmt_num(w['max'])}"),
        ("Tables / links in main content",
         f"{fmt_num(pages['tables'])} / {fmt_num(pages['links'])}"),
        ("Pages marked noindex (not indexed)",
         f"{plural(pages['noindex'], 'distinct page')}{_records_note(pages)}"),
        (f"Pages with fewer than {MIN_INDEXED_WORDS} words (not indexed)",
         fmt_num(pages.get("too_short_to_index"))),
        ("Catalog year", ", ".join(f"{y} ({plural(n, 'catalog page')})"
                                   for y, n in pages["catalog_years"].items()) or "n/a"),
        ("Campuses mentioned", ", ".join(f"{c} ({plural(n, 'page')})"
                                         for c, n in pages["campuses"].items()) or "none"),
    ]
    if pages.get("dated_pages"):
        kv.append(("Pages with a publication date",
                   f"{fmt_num(pages['dated_pages'])} ({pages['first_published']} to "
                   f"{pages['last_published']})"))
    if pages.get("events_with_time"):
        kv.append(("Events with a date and time (event_start)",
                   f"{fmt_num(pages['events_with_time'])} ({pages['first_event']} to "
                   f"{pages['last_event']})"))
    blocks.append(("kv", kv))
    blocks.append(("h3", "Distinct pages by type"))
    blocks.append(("table", (["Page type", "Pages", "Words", "Median words", "Websites"],
                             [[r["page_type"], fmt_num(r["pages"]), fmt_num(r["words"]),
                               fmt_num(r["median_words"]), _site_counts(r["sites"])]
                              for r in pages["by_type"]], "lrrrl")))
    blocks.append(("h3", "Distinct pages by school"))
    blocks.append(("table", _split_table("School / college", pages["by_school"],
                                         pages["by_school_catalog"])))
    if pages.get("other_school_names"):
        blocks.append(("p", "School names outside the six official ones: "
                            + ", ".join(pages["other_school_names"]) + "."))
    blocks.append(("h3", "Distinct pages by level"))
    blocks.append(("table", _split_table("Level", pages["by_level"], pages["by_level_catalog"])))
    if pages["program_pages_by_degree"]:
        blocks.append(("h3", "Catalog program pages by degree"))
        blocks.append(("table", (["Degree", "Program pages"],
                                 [[d, fmt_num(n)] for d, n in
                                  pages["program_pages_by_degree"].items()], "lr")))
    if pages.get("degree_pages_by_degree"):
        blocks.append(("h3", "Degree pages outside the catalog, by degree"))
        blocks.append(("table", (["Degree", "Degree pages"],
                                 [[d, fmt_num(n)] for d, n in
                                  pages["degree_pages_by_degree"].items()], "lr")))
    return blocks


def _with_sites(total: int, per_site: Dict[str, int], limit: int = 3) -> str:
    """"1,443 (apps 1,020, catalog 154, ...)" -- the websites that contribute most."""
    if not total or not per_site:
        return fmt_num(total)
    return f"{fmt_num(total)} ({_site_counts(per_site, limit)})"


def _site_counts(counts: Dict[str, int], limit: int = 3) -> str:
    items = list(counts.items())
    text = ", ".join(f"{short_host(h)} {fmt_num(n)}" for h, n in items[:limit])
    return text + (", ..." if len(items) > limit else "")


def _split_table(label: str, total: Dict[str, int], catalog: Dict[str, int]):
    rows = [[name, _blank_zero(catalog.get(name)), _blank_zero(n - (catalog.get(name) or 0)),
             fmt_num(n)] for name, n in total.items()]
    return [label, "Catalog", "Other sites", "Total"], rows, "lrrr"


def _section_blocks(secs: dict, small: Sequence[str] = ()) -> List[Block]:
    blocks: List[Block] = [("h2", "Sections (search passages)")]
    if not secs.get("available"):
        return blocks + [("p", f"No {SECTIONS_FILE} yet: run {RUN_PARSE}.")]
    w = secs["words"]
    blocks.append(("kv", [
        ("Sections", f"{fmt_num(secs['count'])} from {fmt_num(secs['pages_with_sections'])} pages"),
        ("Words per section", f"min {fmt_num(w['min'])}, median {fmt_num(w['median'])}, "
                              f"mean {fmt_num(w['mean'], 1)}, p90 {fmt_num(w['p90'], 1)}, "
                              f"max {fmt_num(w['max'])}"),
        ("Total words", fmt_num(w["total"])),
        ("Sections with their own #anchor link", fmt_num(secs["with_anchor_link"])),
        ("Sections longer than 1,000 words", fmt_num(secs["over_1000_words"])),
        ("Sections whose text also appears elsewhere", _with_sites(
            secs["repeated_text"], secs.get("repeated_text_by_site") or {})),
    ]))
    blocks.append(("table", (["Section length (words)", "Sections", "Share"],
                             [[b["words"], fmt_num(b["sections"]), fmt_pct(b["share_pct"])]
                              for b in secs["length_buckets"]], "lrr")))
    if len(secs.get("by_site") or []) > 1:
        small = _merge_only_groups(small, [r["site"] for r in secs["by_site"]])
        rows = [[r["site"], fmt_num(r["sections"]), fmt_num(r["pages"]),
                 fmt_num(r["words"]["median"]), fmt_num(r["words"]["p90"], 1),
                 fmt_num(r["words"]["max"]), fmt_num(r["with_anchor_link"])]
                for r in profiled_first(secs["by_site"]) if r["site"] not in small]
        merged = [r for r in secs["by_site"] if r["site"] in small]
        if merged:
            rows.append([plural(len(merged), "other website"),
                         fmt_num(sum(r["sections"] for r in merged)),
                         fmt_num(sum(r["pages"] for r in merged)), "-", "-",
                         fmt_num(max(r["words"]["max"] or 0 for r in merged)),
                         fmt_num(sum(r["with_anchor_link"] for r in merged))])
        blocks.append(("h3", "Sections by website"))
        blocks.append(("table", (
            ["Website", "Sections", "Pages", "Median words", "p90 words", "Max words",
             "With #anchor"], rows, "lrrrrrr")))
    return blocks


def _course_blocks(courses: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Courses")]
    if not courses.get("available"):
        return blocks + [("p", f"No {COURSES_FILE} yet: run {RUN_PARSE}.")]
    d = courses["description_words"]
    blocks.append(("kv", [
        ("Course records", fmt_num(courses["records"])),
        ("Unique course codes", f"{fmt_num(courses['unique_codes'])} "
                                f"({fmt_num(courses['ug_gr_unique_codes'])} UG/GR)"),
        ("Subject areas", f"{fmt_num(courses['unique_subjects'])} "
                          f"({fmt_num(courses['ug_gr_unique_subjects'])} UG/GR)"),
        ("With a description", fmt_pct(courses["with_description_pct"])),
        ("With prerequisites / corequisites", f"{fmt_pct(courses['with_prerequisites_pct'])} / "
                                              f"{fmt_pct(courses['with_corequisites_pct'])}"),
        ("Words per description (if any)", f"median {fmt_num(d['median'])}, "
                                           f"max {fmt_num(d['max'])}"),
    ]))
    blocks.append(("table", (
        ["Level", "Courses", "Codes", "Subjects", "With description", "With prerequisites"],
        [[lv, fmt_num(v["records"]), fmt_num(v["codes"]), fmt_num(v["subjects"]),
          fmt_pct(v["with_description_pct"]), fmt_pct(v["with_prerequisites_pct"])]
         for lv, v in courses["by_level"].items()],
        "lrrrrr")))
    blocks.append(("h3", "Largest subjects"))
    blocks.append(("table", (["Subject", "Name", "Courses"],
                             [[s["subject"], s["subject_name"], fmt_num(s["courses"])]
                              for s in courses["largest_subjects"]], "llr")))
    return blocks


def _pdf_blocks(pdf: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Catalog PDFs")]
    if not pdf.get("available"):
        return blocks + [("p", f"No PDF data yet: run {RUN_PDF} (and `pdf` for 2026-2027).")]
    blocks.append(("table", (
        ["Edition", "Catalog", "Pages", "Words (clean)", "Words (raw)", "Empty pages"],
        [[r["edition"], r["catalog"], fmt_num(r.get("pages")), fmt_num(r.get("words_clean")),
          fmt_num(r.get("words_raw")), fmt_num(r.get("empty_pages"))] for r in pdf["catalogs"]],
        "llrrrr")))
    prop = pdf["proposal_edition"]
    if prop["complete"]:
        blocks.append(("p", f"2025-26 undergraduate + graduate: {fmt_num(prop['pages_total'])} "
                            f"pages, {fmt_num(prop['words_raw_total'])} raw words, "
                            f"{fmt_num(prop['words_clean_total'])} clean words."))
    else:
        blocks.append(("p", f"The 2025-26 undergraduate and graduate PDFs (the proposal's "
                            f"numbers) are not both extracted yet: run {RUN_PDF}."))
    return blocks


def _document_blocks(docs: dict, corpus: dict) -> List[Block]:
    blocks: List[Block] = [("h2", "Documents from the NYIT websites (PDF, Word, Excel, "
                                  "PowerPoint)")]
    if not docs.get("available"):
        return blocks + [("p", f"No documents yet: run {RUN_DOCS} after the crawl (it downloads "
                               "the PDFs and Word/Excel/PowerPoint files linked from the "
                               "crawled pages: handbooks, forms, reports...), then parse again.")]
    w = docs["words"]
    kv = []
    if docs.get("links_found") is not None:
        found = f"{fmt_num(docs['links_found'])}"
        if docs.get("nyit_pdfs") is not None:
            found += f", {fmt_num(docs['nyit_pdfs'])} of them served by NYIT websites"
        kv.append(("PDF links found by the crawler", found))
    if docs.get("office_links_found"):
        kv.append(("Word/Excel/PowerPoint links found by the crawler (.docx, .xlsx, .pptx)",
                   fmt_num(docs["office_links_found"])))
    if docs.get("not_downloaded"):
        kv.append(("Not downloaded on purpose", ", ".join(
            f"{DOC_SKIP_LABEL.get(k, k)} {fmt_num(n)}" for k, n in docs["not_downloaded"].items())))
    if docs.get("pdf_links") is not None:
        statuses = ", ".join(f"{s} {fmt_num(n)}" for s, n in docs["statuses"].items())
        kv.append(("Download attempts (docs step)", f"{fmt_num(docs['pdf_links'])} ({statuses})"))
        kv.append(("Downloaded", f"{fmt_num(docs['downloaded'])} ({fmt_num(docs['megabytes'], 1)} "
                                 f"MB; {fmt_num(docs['unique_files'])} distinct files, "
                                 f"{plural(docs['aliases'], 'copy', 'copies')} of the same file "
                                 "at another URL)"))
    median = docs["pages_per_pdf"]["median"]
    kv += [
        ("Documents extracted", f"{document_count_text(docs)}, {fmt_num(docs['pages'])} pages"
                                + (f" (median {fmt_num(median)} pages per PDF)"
                                   if median is not None else "")),
        ("Words", f"{fmt_num(w['total'])} total, median {fmt_num(w['median'])} per page"),
        ("Pages without text", f"{fmt_num(docs['empty_pages'])} "
                               f"({fmt_pct(pct(docs['empty_pages'], docs['pages']))}); "
                               f"{plural(docs['scanned_pdfs'], 'scanned (image-only) PDF')}"),
        ("PDFs with bookmarks", fmt_num(docs["with_bookmarks"])),
    ]
    kinds = docs.get("by_file_type") or {}
    if set(kinds) - {"pdf"}:
        kv.append(("File types", ", ".join(
            f"{t} {fmt_num(v['documents'])} ({plural(v['pages'], 'page')}, "
            f"{plural(v['words'], 'word')})" for t, v in kinds.items())))
    if docs.get("pages_by_category"):
        kv.append(("Kinds of documents (pages)", ", ".join(
            f"{cat.replace('_', ' ')} {fmt_num(n)}" for cat, n in docs["pages_by_category"].items())))
    if docs.get("superseded_pdfs") is not None and docs.get("series") is not None:
        kv.append(("Older editions (superseded)",
                   f"{plural(docs['superseded_pdfs'], 'document')} ({plural(docs['superseded_pages'], 'page')})"
                   f" in {plural(docs['series'], 'series', 'series')} of yearly documents; left out of "
                   "the corpus unless `parse --corpus-with-superseded`; "
                   f"{plural(docs.get('cohort_pdfs') or 0, 'semester map / class calendar', 'semester maps / class calendars')}"
                   " are kept (one per entering class)"))
    unread = docs.get("not_extracted_files") or {}
    if unread.get("files"):
        reasons = unread.get("by_reason") or {}
        kv.append(("Linked Office files not extracted", ", ".join(
            f"{ext} {fmt_num(n)}" for ext, n in unread["by_type"].items())
            + (" (" + ", ".join(f"{UNREAD_REASON_LABEL.get(r, DOC_STATUS_MEANING.get(r, r))} "
                                f"{fmt_num(n)}" for r, n in reasons.items()) + ")"
               if reasons else " (Word/Excel/PowerPoint files: run `docs` again to read them)")))
    in_corpus = ((corpus.get("by_source") or {}).get("document") or {}).get("records") \
        if corpus.get("available") else None
    kv.append(("Pages in the search corpus", fmt_num(in_corpus) if in_corpus is not None
               else "n/a"))
    blocks.append(("kv", kv))
    if docs["by_site"]:
        blocks.append(("table", (
            ["Website", "Documents", "Pages", "Words", "Pages without text", "Scanned PDFs"],
            [[r["site"], fmt_num(r.get("documents", r["pdfs"])), fmt_num(r["pages"]),
              fmt_num(r["words"]),
              fmt_num(r["empty_pages"]), fmt_num(r["scanned_pdfs"])] for r in docs["by_site"]],
            "lrrrrr")))
    failed = {s: n for s, n in (docs.get("statuses") or {}).items() if s != "ok"}
    if failed:
        blocks.append(("table", (["Download status", "Files", "Meaning"],
                                 [[s, fmt_num(n), DOC_STATUS_MEANING.get(s, "")]
                                  for s, n in failed.items()], "lrl")))
    if docs["largest"]:
        blocks.append(("h3", "Largest documents"))
        blocks.append(("table", (["Document", "Website", "Pages", "Words"],
                                 [[_shorten(r["title"] or r["pdf_url"], 70), r["site"],
                                   fmt_num(r["pages"]), fmt_num(r["words"])]
                                  for r in docs["largest"]], "llrr")))
    return blocks


def _corpus_blocks(corpus: dict, small: Sequence[str] = ()) -> List[Block]:
    blocks: List[Block] = [("h2", "Search corpus (corpus.jsonl)")]
    if not corpus.get("available"):
        return blocks + [("p", f"No {CORPUS_FILE} yet: run {RUN_PARSE}.")]
    rows = [[src, fmt_num(v["records"]), fmt_num(v["words"]["total"]),
             fmt_num(v["words"]["median"])] for src, v in corpus["by_source"].items()]
    rows.append(["total", fmt_num(corpus["records"]), fmt_num(corpus["words"]["total"]),
                 fmt_num(corpus["words"]["median"])])
    blocks.append(("table", (["Source", "Records", "Words", "Median words"], rows, "lrrr")))
    sources = list(corpus["by_source"])
    if corpus.get("by_site"):
        small = _merge_only_groups(small, [r["site"] for r in corpus["by_site"]])
        blocks.append(("h3", "Corpus records by website"))
        table = [[r["site"]] + [_blank_zero(r["by_source"].get(s)) for s in sources]
                 + [fmt_num(r["records"])] for r in profiled_first(corpus["by_site"])
                 if r["site"] not in small]
        merged = [r for r in corpus["by_site"] if r["site"] in small]
        if merged:
            table.append([plural(len(merged), "other website")]
                         + [_blank_zero(sum(r["by_source"].get(s, 0) for r in merged))
                            for s in sources] + [fmt_num(sum(r["records"] for r in merged))])
        blocks.append(("table", (["Website"] + sources + ["Total"], table,
                                 "l" + "r" * (len(sources) + 1))))
    notes = []
    if corpus.get("near_duplicates_collapsed"):
        left = corpus.get("near_duplicates_left_out_by_source") or {}
        notes.append(f"Near-duplicates: {plural(corpus.get('near_duplicates_left_out') or 0, 'copy', 'copies')}"
                     f" of other passages ({', '.join(f'{k} {fmt_num(v)}' for k, v in left.items()) or 'none'})"
                     " are left out; "
                     f"{plural(corpus.get('records_in_dup_groups') or 0, 'record')} in "
                     f"{plural(corpus.get('dup_groups') or 0, 'group')} repeat a passage for another "
                     "program, course or entering class and share `metadata.dup_group` "
                     "(show one result per group).")
    left_docs = corpus.get("document_pages_left_out") or {}
    if left_docs:
        notes.append("PDF document pages not in the corpus: " + ", ".join(
            f"{reason.replace('_', ' ')} {fmt_num(n)}" for reason, n in left_docs.items()) + ".")
    if corpus.get("tiny_sections_merged"):
        notes.append(f"{plural(corpus['tiny_sections_merged'], 'section')} with fewer than "
                     f"{MIN_SECTION_WORDS} words merged into the passage before them.")
    contacts = corpus.get("contacts") or {}
    if contacts.get("records_with_email") or contacts.get("records_with_phone"):
        notes.append(f"Contact details printed on NYIT's public pages stay in the text: "
                     f"{plural(contacts.get('records_with_email') or 0, 'record')} contain an "
                     f"e-mail address ({fmt_num(contacts.get('distinct_emails'))} distinct, "
                     f"{fmt_num(contacts.get('distinct_nyit_emails'))} of them @nyit.edu), "
                     f"{plural(contacts.get('records_with_phone') or 0, 'record')} a phone number "
                     "(mostly office and staff contacts; mailto: links were never followed).")
    if notes:
        blocks.append(("bullets", notes))
    blocks.append(("p", "Each record is one retrievable passage with a link to its source: "
                        "section = part of a web page (text starts with the page title and "
                        "heading path), course = one course of the catalog's course directory, "
                        "document = one page of a PDF found on an NYIT site, pdf_page = one "
                        "page of a full catalog PDF (only with `parse --corpus-with-pdf`)."))
    return blocks


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def fmt_num(value: Any, decimals: int = 0) -> str:
    """1234 -> "1,234"; 12.5 -> "12.5" (or "12" with decimals=0 if whole); None -> "n/a"."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if decimals == 0 and not value.is_integer():
            decimals = 1
        return f"{value:,.{decimals}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _blank_zero(value: Optional[int]) -> str:
    """Table cells: "-" instead of 0/None, so the non-zero numbers stand out."""
    return fmt_num(value) if value else "-"


def fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def plural(count: Optional[int], singular: str, plural_form: str = "") -> str:
    """plural(1, "page") -> "1 page"; plural(3, "copy", "copies") -> "3 copies"."""
    return f"{fmt_num(count)} {singular if count == 1 else (plural_form or singular + 's')}"


def _shorten(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 3].rstrip() + "..."


def format_table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                 align: str = "") -> List[str]:
    """Plain-text table with aligned columns; ``align`` is "l"/"r" per column."""
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    align = align.ljust(len(headers), "l")

    def line(row: Sequence[str]) -> str:
        return "  ".join(c.rjust(w) if a == "r" else c.ljust(w)
                         for c, w, a in zip(row, widths, align)).rstrip()

    return [line(cells[0]), "  ".join("-" * w for w in widths)] + [line(r) for r in cells[1:]]


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                   align: str = "") -> List[str]:
    align = align.ljust(len(headers), "l")

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(cell(h) for h in headers) + " |",
           "|" + "|".join("---:" if a == "r" else "---" for a in align) + "|"]
    out += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return out


def render_text(blocks: Sequence[Block], width: int = 100) -> str:
    out: List[str] = []
    previous = ""
    for kind, content in blocks:
        if kind == "p" and previous == "p":
            out.append("")      # a blank line between two paragraphs
        previous = kind
        if kind == "h1":
            out += [content, "=" * len(content)]
        elif kind == "h2":
            out += ["", content, "-" * len(content)]
        elif kind == "h3":
            out += ["", content + ":"]
        elif kind == "p":
            out += textwrap.wrap(content.replace("`", ""), width)
        elif kind == "bullets":
            for item in content:
                out += textwrap.wrap(item.replace("`", ""), width, initial_indent="- ",
                                     subsequent_indent="  ")
        elif kind == "kv":
            label_width = max((len(label) for label, _ in content), default=0)
            out += [f"{label:<{label_width}}  {str(value).replace('`', '')}"
                    for label, value in content]
        elif kind == "table":
            headers, rows, align = content
            out += format_table(headers, [[str(c).replace("`", "") for c in r] for r in rows],
                                align)
    return "\n".join(out) + "\n"


def render_markdown(blocks: Sequence[Block]) -> str:
    out: List[str] = []
    for kind, content in blocks:
        if kind in ("h1", "h2", "h3"):
            out += ["#" * int(kind[1]) + " " + content, ""]
        elif kind == "p":
            out += [content, ""]
        elif kind == "bullets":
            out += [f"- {item}" for item in content] + [""]
        elif kind == "kv":
            out += markdown_table(["Measure", "Value"], content, "ll") + [""]
        elif kind == "table":
            out += markdown_table(*content) + [""]
    return "\n".join(out).rstrip() + "\n"


def emit(text: str) -> None:
    """print() that never crashes on a console that cannot show some characters
    (e.g. Windows with output redirected to a file)."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding))
    sys.stdout.flush()


def fmt_time(iso: Optional[str]) -> str:
    """"2026-11-23T07:05:49+00:00" -> "2026-11-23 07:05 UTC"."""
    if not iso:
        return "n/a"
    text = str(iso).replace("T", " ")
    if text.endswith("+00:00"):
        return text[:16] + " UTC"
    return text[:16]


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"
