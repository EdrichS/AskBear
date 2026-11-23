"""Documents linked from the NYIT websites (PDF, Word, Excel, PowerPoint): download them
and extract every page.

A lot of NYIT information exists only as PDF files: forms (registration, transfer
credit, scholarship applications), policies and handbooks, program brochures,
semester maps, campus maps; some of it as Word, Excel or PowerPoint files (proposal
templates, class-scheduling sheets, how-to slides). The crawler records every link to a
PDF in ``data/raw/external_links.jsonl`` (kind "pdf") and every link to an Office file
there too (kind "asset"). This step

1. keeps the PDFs and the .docx/.xlsx/.pptx files served by NYIT websites (any host of
   sites.py that is not deliberately skipped, plus such URLs listed in the cached
   sitemaps). The full catalog PDFs (catalog.nyit.edu/files/catalog/archives/) are left to
   the `pdf` step and files of other organisations (government forms, ...) are not
   downloaded. Legacy binary Office files (.doc, .xls, .ppt) are not read: they are listed
   under ``not_extracted_files`` in the summary;
2. downloads each file once, politely (robots.txt, a delay per website, retries), into
   ``data/raw/docs/<host>/`` and records the outcome in ``data/raw/docs_manifest.jsonl``
   (one JSON line per URL; the last line wins), so a re-run only fetches what is missing.
   Files over ``max_mb`` are not saved: the Content-Length header is checked before the
   body is read, and a download without that header stops as soon as it passes the limit.
   A PDF must start like a PDF (else ``not_pdf``); an Office file must be a ZIP package
   with [Content_Types].xml and its main part (else ``not_office``: an HTML sign-in page, a
   legacy file behind a new name; ``encrypted``: a password-protected file);
3. extracts every page of a PDF with the machinery of the catalog PDFs (pdf_catalog: reading
   order, table cells joined with " | ", running headers/footers and page numbers
   removed, wrapped lines joined, section paths from the PDF bookmarks), plus what
   documents need on top of that: landscape pages stored rotated, list markers in a cell
   of their own ("- | item"), form blanks and dot leaders, page numbers and running
   headers a little away from the edge (checked to leave the catalog PDFs' text exactly
   as pdf_catalog extracts it). Word, Excel and PowerPoint files are read by office.py
   (standard library only) and cut into pages: about 150-400 words at heading or paragraph
   boundaries (.docx), rows of one sheet (.xlsx), one slide per page (.pptx); their text
   is then cleaned like PDF text (tidy_text). A file published under several URLs (same
   SHA-1) is extracted once; the other URLs are listed as its aliases in the summary.
   Files whose server sends ``X-Robots-Tag: noindex`` are left out;
4. gives every document a title: its title metadata when that is a real title (not
   "Microsoft Word - form.docx"), else the title printed in large type on page 1 (Office
   files: the first heading or slide title), else the text of a link to it, else its file
   name ("Bachelor of Science in Computer Science General
   Concentration (2026)" for a semester map linked as "2026"). Junk is skipped ("Page 1 of
   2", "Name", the university's name, "M A G A Z I N E"), one-word titles ("Agenda") are a
   last resort, and documents of one file type that end up with the same title are told
   apart by their edition, link text or file name ("Internship Residency Placement by
   Specialty (2016)"). Title metadata is only used when it matches the document (not the
   title an Office file kept from its template), link text without "This link opens in a
   new window";
5. works out what each PDF is (``doc_category``: form, policy_handbook, semester_map,
   financial_filing...), which year/term/date its file name gives (``edition``,
   ``doc_year``), and which PDFs are editions of one document ("..._StudentHandbook20242025"
   and "..._StudentHandbook20252026": ``series``). An older edition whose period is over
   is ``superseded`` (kept here, left out of corpus.jsonl by default); semester maps and
   class calendars are per entering class (``cohort_year``) and never superseded;
6. writes ``data/processed/documents.jsonl`` (one ``DocumentPageRecord`` per page, empty
   pages of scanned documents included; ``file_type`` says pdf, docx, xlsx or pptx and
   ``pdf_url`` is the document's URL whatever its type) and
   ``data/processed/documents_summary.json`` (per document, plus the scanned PDFs and the
   linked Office files that are not read, with the reason).

Re-running ``docs`` re-extracts every downloaded file from data/raw/docs; ``--max-docs 0``
does that without downloading anything.

Run it after `crawl`:  ``python -m askbear_scraper docs [--max-docs N] [--max-mb 40]``
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple
from urllib.parse import unquote

from tqdm import tqdm

from . import office, sites
from .config import Settings
from .crawler import load_manifest
from .fetcher import PoliteFetcher
from .models import DocumentPageRecord
from .pdf_catalog import (file_sha1, find_running_lines, page_rows, pymupdf, rows_to_text,
                          toc_paths, word_count_clean)
from .urls import normalize_url, url_host, url_path
from .utils import (CP1252_PUNCTUATION, append_jsonl, clean_text, dedupe_keep_order,
                    fix_mojibake, now_iso, read_json_file, read_jsonl, short_hash,
                    unescape_leftovers, write_json, write_jsonl)

log = logging.getLogger("askbear.docs")

DOCUMENTS_FILE = "documents.jsonl"
DOCUMENTS_SUMMARY_FILE = "documents_summary.json"
DEFAULT_MAX_MB = 40.0
MB = 1_000_000
MAX_FOUND_ON = 5            # pages kept per PDF in ``found_on`` (like the crawler)
MAX_EXAMPLES = 10           # example URLs per skip reason in the summary
PAGE_NUMBER_ZONE = 0.12     # where bare page numbers are looked for: top/bottom 12% of a page

# docs_manifest.jsonl statuses
OK = "ok"
TOO_LARGE = "too_large"
NOT_PDF = "not_pdf"
NOT_FOUND = "not_found"
HTTP_ERROR = "http_error"
ERROR = "error"
ROBOTS = "robots_disallowed"
EXTRACT_ERROR = "extract_error"     # downloaded, but PyMuPDF cannot read it (summary only)
REDIRECT_LOOP = "redirect_loop"     # the link redirects in a circle (or more than 10 hops)
OFFSITE = "offsite_redirect"        # the link redirects to a sign-in portal or another website
NOT_OFFICE = office.NOT_OFFICE      # a .docx/.xlsx/.pptx link returned something else
ENCRYPTED = office.ENCRYPTED        # a password-protected Office file (OLE2 container)
# outcomes that do not change by asking again (a re-run only retries them with --refresh);
# network and server errors are retried by the next run
FINAL_FAILURES = {NOT_PDF, NOT_FOUND, ROBOTS, REDIRECT_LOOP, OFFSITE, NOT_OFFICE, ENCRYPTED}

# the file types the step reads (DocumentPageRecord.file_type), by the URL's extension
PDF = "pdf"
FILE_TYPES = (PDF,) + office.OFFICE_TYPES
_EXTENSION_TYPES = ((".pdf", PDF), (".docx", office.DOCX), (".xlsx", office.XLSX),
                    (".pptx", office.PPTX))

# reasons why a PDF link is not downloaded
OTHER_WEBSITE = "other_website"         # not an NYIT host (hesc.ny.gov, ed.gov, ...)
SITE_NOT_CRAWLED = "site_not_crawled"   # NYIT host deliberately skipped in sites.py
CATALOG_PDF = "catalog_pdf"             # full catalog PDF: handled by the `pdf` step
INVALID_URL = "invalid_url"
NOINDEX = "noindex"                     # downloaded, but X-Robots-Tag says noindex
OVER_MAX_MB = "over_max_mb"             # downloaded earlier with a higher --max-mb

_CATALOG_ARCHIVE = re.compile(r"^/files/catalog/archives/", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r"[^a-z0-9_-]+")


def type_of_name(name: str) -> Optional[str]:
    """"pdf", "docx", "xlsx" or "pptx" from the extension of a path or file name."""
    name = str(name or "").lower()
    return next((ftype for ext, ftype in _EXTENSION_TYPES if name.endswith(ext)), None)


def document_type(url: str) -> Optional[str]:
    """The file type of a document URL, from the extension of its path (``None``: no PDF
    or Office Open XML extension)."""
    return type_of_name(url_path(url))


def entry_type(entry: dict) -> str:
    """The file type of a docs_manifest.jsonl entry: the extension of the saved file, else of
    the URL; a URL without one is a PDF (served at a URL without ".pdf")."""
    return (type_of_name(str(entry.get("file") or "")) or document_type(str(entry.get("url") or ""))
            or PDF)


def docs_dir(settings: Settings) -> Path:
    """``data/raw/docs``: one folder per host."""
    return settings.docs_dir


def docs_manifest_path(settings: Settings) -> Path:
    return settings.docs_manifest_path


# ---------------------------------------------------------------------------
# Which PDF and Office links are downloaded
# ---------------------------------------------------------------------------
def skip_reason(url: str) -> Optional[str]:
    """Why the document at ``url`` is not one of ours (``None``: it is)."""
    profile = sites.profile_for(url_host(url))
    if profile is None:
        return OTHER_WEBSITE
    if not profile.enabled:
        return SITE_NOT_CRAWLED
    if profile.parser == "catalog" and _CATALOG_ARCHIVE.match(url_path(url)):
        return CATALOG_PDF
    return None


def _as_list(found_on) -> List[str]:
    """``found_on`` is one URL in older crawl outputs and a list in newer ones."""
    if isinstance(found_on, str):
        return [found_on] if found_on else []
    if isinstance(found_on, (list, tuple)):
        return [str(u) for u in found_on if u]
    return []


def _plain(text) -> str:
    """One line of clean text."""
    return " ".join(clean_text(str(text or "")).split())


class Candidates:
    """The PDF and Office links to download, merged from every source, and the links left
    out."""

    def __init__(self) -> None:
        self.items: Dict[str, dict] = {}                   # url -> candidate
        self.skipped: Dict[str, Set[str]] = defaultdict(set)  # reason -> urls
        self.links_read = 0             # PDF links in external_links.jsonl
        self.office_links = 0           # .docx/.xlsx/.pptx links in external_links.jsonl
        self.sitemap_urls = 0           # PDF URLs in the sitemaps
        self.sitemap_office_urls = 0    # .docx/.xlsx/.pptx URLs in the sitemaps
        self.manifest_pdfs = 0

    def add(self, raw_url, anchor_text: str, found_on: List[str], source: str,
            served_as_pdf: bool = False) -> None:
        """Add one PDF or .docx/.xlsx/.pptx link. ``served_as_pdf``: the crawler fetched the
        URL and got a PDF although the URL does not end in .pdf
        (online.nyit.edu/documents/<name>)."""
        url = normalize_url(str(raw_url or ""))
        file_type = (PDF if served_as_pdf else document_type(url)) if url else None
        if not file_type:
            self.skipped[INVALID_URL].add(str(raw_url))
            return
        reason = skip_reason(url)
        if reason:
            self.skipped[reason].add(url)
            return
        item = self.items.setdefault(url, {"url": url, "site": url_host(url), "anchor_text": "",
                                           "found_on": [], "source": source,
                                           "file_type": file_type})
        if anchor_text and not item["anchor_text"]:
            item["anchor_text"] = anchor_text
        item["found_on"] = dedupe_keep_order(item["found_on"] + found_on)[:MAX_FOUND_ON]


def office_type(raw_url) -> Optional[str]:
    """"docx", "xlsx" or "pptx" for a link to an Office Open XML file, else ``None``."""
    ftype = document_type(normalize_url(str(raw_url or "")) or "")
    return ftype if ftype in office.OFFICE_TYPES else None


def find_candidates(settings: Settings) -> Candidates:
    """PDF links from ``external_links.jsonl`` (kind "pdf") and links to .docx/.xlsx/.pptx
    files (kind "asset"), plus such URLs listed in the cached sitemaps, plus URLs without
    ".pdf" that the crawl manifest records as a PDF (status non_html with a PDF
    Content-Type; crawls made before external_links.jsonl listed them)."""
    found = Candidates()
    for rec in read_jsonl(settings.external_links_path):
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "asset" and office_type(rec.get("url")):
            found.office_links += 1
            found.add(rec.get("url"), _plain(rec.get("anchor_text")),
                      _as_list(rec.get("found_on")), "link")
            continue
        if rec.get("kind") != "pdf":
            continue
        found.links_read += 1
        served = bool(rec.get("served_as_pdf")) or "pdf" in str(rec.get("content_type") or "").lower()
        found.add(rec.get("url"), _plain(rec.get("anchor_text")), _as_list(rec.get("found_on")),
                  "link", served_as_pdf=served)
    for path in sorted((settings.raw_dir / "sitemaps").glob("*.json")):
        data = read_json_file(path)
        pages = data.get("pages") if isinstance(data, dict) else None
        for page in pages or []:
            loc = page[0] if isinstance(page, (list, tuple)) and page else page
            if isinstance(loc, str) and url_path(loc).lower().endswith(".pdf"):
                found.sitemap_urls += 1
                found.add(loc, "", [], "sitemap")
            elif isinstance(loc, str) and office_type(loc):
                found.sitemap_office_urls += 1
                found.add(loc, "", [], "sitemap")
    if settings.manifest_path.exists():
        for url, entry in load_manifest(settings.manifest_path).items():
            if entry.get("status") == "non_html" and \
                    "pdf" in str(entry.get("content_type") or "").lower():
                referrer = str(entry.get("referrer") or "")
                found.manifest_pdfs += 1
                found.add(url, "", [referrer] if referrer.startswith("http") else [], "crawl",
                          served_as_pdf=True)
    return found


def doc_filename(url: str, file_type: Optional[str] = None) -> str:
    """Where a document is saved, relative to data/raw/docs:
    ``https://files.nyit.edu/files/admissions/ADM_Agencies.pdf``
    -> ``files.nyit.edu/adm_agencies__<hash>.pdf`` (``.docx``, ``.xlsx``, ``.pptx`` for
    Office files; ``file_type`` defaults to the URL's extension, else pdf)."""
    host = url_host(url)
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        host = _UNSAFE_NAME.sub("_", host) or "unknown"
    stem = _UNSAFE_NAME.sub("_", _file_stem(url).lower()).strip("_")[:60] or "document"
    return f"{host}/{stem}__{short_hash(url)}.{file_type or document_type(url) or PDF}"


def _interleave_by_host(urls: Iterable[str]) -> List[str]:
    """Round-robin over the hosts: the politeness delay of one website does not hold up
    downloads from the others."""
    by_host: Dict[str, List[str]] = defaultdict(list)
    for url in sorted(urls):
        by_host[url_host(url)].append(url)
    queues = [by_host[h] for h in sorted(by_host)]
    out: List[str] = []
    for i in range(max((len(q) for q in queues), default=0)):
        out.extend(q[i] for q in queues if i < len(q))
    return out


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
class DownloadWatch:
    """``requests`` response hook for ``PoliteFetcher.download``: remembers the headers of
    the file that is being downloaded and keeps a file larger than ``max_bytes`` off disk.

    A response that announces a larger Content-Length gets an empty body (it is closed
    without being read); otherwise its body stops as soon as the limit is passed, and
    ``exceeded`` holds the size seen. Responses that are not streamed (robots.txt) are left
    alone."""

    def __init__(self, max_bytes: Optional[int] = None):
        self.max_bytes = max_bytes
        self.reset()

    def reset(self) -> None:
        self.exceeded = 0          # bytes announced (Content-Length) or read before stopping
        self.announced = False
        self.noindex = False       # X-Robots-Tag: noindex
        self.last_modified: Optional[str] = None

    def describe(self) -> str:
        limit = f"the {(self.max_bytes or 0) / MB:g} MB limit"
        if self.announced:
            return f"{self.exceeded / MB:.1f} MB, over {limit}"
        return f"over {limit} (download stopped after {self.exceeded / MB:.2f} MB)"

    def __call__(self, resp, *args, **kwargs):
        if not kwargs.get("stream") or resp.status_code != 200:
            return resp
        robots = str(resp.headers.get("X-Robots-Tag") or "").lower()
        self.noindex = "noindex" in robots or "none" in re.split(r"[\s,:]+", robots)
        self.last_modified = _http_date(resp.headers.get("Last-Modified"))
        if not self.max_bytes:
            return resp
        declared = _content_length(resp.headers.get("Content-Length"))
        if declared > self.max_bytes:
            self.exceeded, self.announced = declared, True
            resp.iter_content = lambda *a, **k: iter(())
            return resp
        body = resp.iter_content
        watch = self

        def limited(chunk_size=1, decode_unicode=False):
            total = 0
            for chunk in body(chunk_size, decode_unicode):
                total += len(chunk)
                if total > watch.max_bytes:
                    watch.exceeded = total
                    return
                yield chunk

        resp.iter_content = limited
        return resp


def _http_date(value) -> Optional[str]:
    """"Wed, 15 May 2024 14:03:22 GMT" -> "2024-05-15T14:03:22+00:00"."""
    try:
        return parsedate_to_datetime(str(value)).isoformat() if value else None
    except (TypeError, ValueError, IndexError):
        return None


def _content_length(value) -> int:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else 0


def looks_like_pdf(path: Path) -> bool:
    """True when the file starts like a PDF. Readers accept the "%PDF-" header anywhere in
    the first 1024 bytes, so a few stray bytes before it are tolerated; an HTML error or
    login page served instead of the file is not."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return b"%PDF-" in head and not head.lstrip().startswith(b"<")


def looks_like_document(path: Path, file_type: str = PDF) -> bool:
    """looks_like_pdf for a PDF; for an Office file: it starts like a ZIP package."""
    if file_type in office.OFFICE_TYPES:
        return office.looks_like_office(path)
    return looks_like_pdf(path)


def content_problem(path: Path, file_type: str, content_type: str = ""
                    ) -> Optional[Tuple[str, str]]:
    """(status, error) when a downloaded file is not the document its link promises, else
    ``None``. A PDF must start like one (NOT_PDF); a .docx/.xlsx/.pptx must be a ZIP package
    with [Content_Types].xml and its main part (office.check_file: NOT_OFFICE, or ENCRYPTED
    for a password-protected file)."""
    served = f"Content-Type: {content_type or 'unknown'}"
    if file_type in office.OFFICE_TYPES:
        problem = office.check_file(path, file_type)
        return (problem[0], f"{problem[1]} ({served})") if problem else None
    return None if looks_like_pdf(path) else (NOT_PDF, f"not a PDF ({served})")


def _status_for(http_status: int) -> str:
    if http_status == 0:
        return ERROR
    if http_status in (404, 410):
        return NOT_FOUND
    return HTTP_ERROR


# why PoliteFetcher.download did not follow a redirect (FetchResult.blocked) -> status
_BLOCKED_STATUS = {
    "robots": ROBOTS,                 # the target's robots.txt forbids it: final
    "loop": REDIRECT_LOOP,            # redirects in a circle: final
    "too_many": REDIRECT_LOOP,        # more than 10 hops: final
    "scope": OFFSITE,                 # a sign-in portal, a skipped host, another website
    "robots_unavailable": ERROR,      # robots.txt of that website cannot be read now: retried
}


def follows_redirect(target: str) -> bool:
    """Redirects of document links are followed to NYIT websites that are crawled
    (sites.py); a redirect to a sign-in portal, a skipped NYIT host or another organisation
    is not requested."""
    return skip_reason(target) in (None, CATALOG_PDF)


def download_document(fetcher: PoliteFetcher, url: str, dest: Path,
                      watch: Optional[DownloadWatch] = None,
                      file_type: Optional[str] = None) -> dict:
    """Download one document to ``dest`` (only replaced by a real document of
    ``file_type``, default: the URL's extension, else pdf; see content_problem) and return
    the manifest fields that describe the outcome (status, final_url, http_status, bytes,
    sha1, error, noindex, last_modified). ``watch`` must be installed in
    ``fetcher.session.hooks``."""
    file_type = file_type or document_type(url) or PDF
    out = {"status": ERROR, "final_url": url, "http_status": 0, "content_type": "",
           "bytes": 0, "sha1": None, "error": "", "fetched_at": now_iso(),
           "noindex": False, "last_modified": None}
    tmp = dest.with_name(dest.name + ".download")
    watch = watch or DownloadWatch()
    watch.reset()
    try:
        if not fetcher.allowed(url):
            out.update(status=ROBOTS, error="disallowed by robots.txt")
            return out
        res = _download(fetcher, url, tmp)
        out.update(final_url=res.final_url, http_status=res.status,
                   content_type=res.content_type, noindex=watch.noindex,
                   last_modified=watch.last_modified)
        if getattr(res, "redirects", None):
            out["redirects"] = len(res.redirects)
        blocked = getattr(res, "blocked", "") or ""
        if blocked:
            # the redirect was not followed: nothing was downloaded from its target
            out.update(status=_BLOCKED_STATUS.get(blocked, ERROR),
                       error=res.error or f"redirect not followed ({blocked})")
        elif watch.exceeded:
            out.update(status=TOO_LARGE, bytes=watch.exceeded, error=watch.describe())
        elif not res.ok:
            out.update(status=_status_for(res.status), error=res.error or f"HTTP {res.status}")
        else:
            problem = content_problem(tmp, file_type, res.content_type)
            if problem:
                out.update(status=problem[0], bytes=tmp.stat().st_size, error=problem[1])
            else:
                os.replace(tmp, dest)
                out.update(status=OK, bytes=dest.stat().st_size, sha1=file_sha1(dest))
    except Exception as exc:  # disk full, file locked by a PDF viewer on Windows, ...
        log.debug("downloading %s failed", url, exc_info=True)
        out.update(status=ERROR, error=f"{type(exc).__name__}: {exc}")
    finally:
        for leftover in (tmp, tmp.with_name(tmp.name + ".part")):
            try:
                leftover.unlink()
            except OSError:  # not there (the usual case) or locked
                pass
    return out


download_pdf = download_document   # the name of the PDF-only version, kept for callers


def _download(fetcher: PoliteFetcher, url: str, dest: Path):
    """``fetcher.download`` with the redirect rule of :func:`follows_redirect` (a fetcher
    without the ``follow`` parameter, e.g. an older test double, downloads as before)."""
    try:
        return fetcher.download(url, dest, follow=follows_redirect)
    except TypeError as exc:
        if "follow" not in str(exc):
            raise
        return fetcher.download(url, dest)


def _cached_ok(settings: Settings, entry: Optional[dict]) -> bool:
    if not entry or entry.get("status") != OK or not entry.get("file"):
        return False
    return looks_like_document(docs_dir(settings) / entry["file"], entry_type(entry))


def _needs_download(settings: Settings, entry: Optional[dict], max_bytes: Optional[int]) -> bool:
    """Download unless a usable outcome is recorded: a cached document, a permanent failure
    (404, not a PDF, not an Office file, encrypted) or a file known to be over the current
    size limit."""
    if entry is None or settings.refresh:
        return True
    status = entry.get("status")
    if status == OK:
        return not _cached_ok(settings, entry)
    if status == TOO_LARGE:  # retried when the limit was raised above its size
        return max_bytes is None or (entry.get("bytes") or 0) <= max_bytes
    return status not in FINAL_FAILURES


def _merge_links(entry: dict, item: dict) -> None:
    """Refresh where a PDF is linked from with what the latest crawl found."""
    merged = dedupe_keep_order(item["found_on"] + _as_list(entry.get("found_on")))
    entry["found_on"] = merged[:MAX_FOUND_ON]
    entry["anchor_text"] = item["anchor_text"] or entry.get("anchor_text") or ""
    entry["site"] = item["site"]


def download_documents(settings: Settings, found: Candidates, manifest: Dict[str, dict],
                       max_docs: Optional[int], max_bytes: Optional[int],
                       fetcher: Optional[PoliteFetcher] = None) -> dict:
    """Download the candidates that are not cached yet; updates ``manifest`` in place."""
    path = docs_manifest_path(settings)
    order = _interleave_by_host(found.items)
    todo = [u for u in order if _needs_download(settings, manifest.get(u), max_bytes)]
    for url in set(order) - set(todo):
        if url in manifest:
            _merge_links(manifest[url], found.items[url])
    postponed: List[str] = []
    if max_docs is not None and len(todo) > max(max_docs, 0):
        todo, postponed = todo[:max(max_docs, 0)], todo[max(max_docs, 0):]
    attempt = set(todo)
    stats = {"to_download": len(todo) + len(postponed), "attempted": len(todo),
             "postponed": len(postponed), "postponed_urls": postponed,
             "cached": sum(1 for u in order
                           if u not in attempt and _cached_ok(settings, manifest.get(u))),
             "downloaded": 0, "failed": 0, "bytes": 0, "network_requests": 0}
    if not todo:
        return stats

    log.info("downloading %d documents (%d already downloaded%s)", len(todo), stats["cached"],
             f", {len(postponed)} left for later runs by --max-docs" if postponed else "")
    fetcher = fetcher or PoliteFetcher(settings)
    watch = DownloadWatch(max_bytes)
    fetcher.session.hooks["response"].append(watch)
    requests_before = fetcher.requests_made
    try:
        for url in tqdm(todo, unit="file", desc="downloading documents", dynamic_ncols=True):
            item = found.items[url]
            file_type = item.get("file_type") or PDF
            fname = doc_filename(url, file_type)
            dest = docs_dir(settings) / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = download_document(fetcher, url, dest, watch, file_type)
            entry = {"url": url, "site": item["site"], "status": result["status"],
                     "file": fname if result["status"] == OK else None}
            entry.update((k, v) for k, v in result.items() if k != "status")
            entry.update(found_on=item["found_on"], anchor_text=item["anchor_text"],
                         source=item["source"])
            manifest[url] = entry
            append_jsonl(path, entry)  # crash-safe: an interrupted run resumes from here
            if entry["status"] == OK:
                stats["downloaded"] += 1
                stats["bytes"] += entry["bytes"]
                log.debug("saved %s (%.2f MB)", fname, entry["bytes"] / MB)
            else:
                stats["failed"] += 1
                log.warning("%s: %s (%s)", entry["status"], url, entry["error"])
    finally:
        fetcher.session.hooks["response"].remove(watch)
        stats["network_requests"] = fetcher.requests_made - requests_before
    return stats


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
# control characters, U+FFFD (glyphs without a Unicode mapping) and private-use characters
# (checkbox and bullet glyphs of Wingdings/Symbol fonts)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\U0000fffd\U0000e000-\U0000f8ff]")
# the private-use glyphs that are bullets and arrows of the Symbol/Wingdings fonts
_PRIVATE_BULLETS = {0xF0B7: "\N{BULLET}", 0xF0A7: "\N{BULLET}", 0xF06E: "\N{BULLET}",
                    0xF0A8: "\N{BULLET}", 0xF076: "\N{BULLET}", 0xF06C: "\N{BULLET}",
                    0xF0D8: "-", 0xF0E0: "-", 0xF0E8: "-", 0xF0F0: "-", 0xF0FC: "\N{CHECK MARK}"}
# one glyph repeated as separate "words": the dot leaders of IRS forms come out as
# "m m m m m m", checkbox rows as "O O O O O"
_GLYPH_RUN = re.compile(r"(?<!\S)([^\s\d|])(?: \1){3,}(?!\S)")
# a word printed letter-spaced: "s e r v i n g", "M A G A Z I N E", "2 0 2 2"
_SPACED_LETTERS = re.compile(r"(?<![^\s|])(?:[^\W\d_] ){3,}[^\W\d_](?![^\s|])")
_SPACED_YEAR = re.compile(r"(?<![^\s|])(?:1 9|2 0) \d \d(?![^\s|])")
# C1 control characters in PDF text that are Windows-1252 punctuation (see utils)
_CP1252 = CP1252_PUNCTUATION
# a list marker that the PDF placed in a cell of its own: "- | text", "2. | text", "iv) | text"
_BULLETS = (r"-*\N{EN DASH}\N{BULLET}\N{TRIANGULAR BULLET}\N{HYPHEN BULLET}\N{BULLET OPERATOR}"
            r"\N{MIDDLE DOT}\N{BLACK SQUARE}\N{BLACK SMALL SQUARE}\N{WHITE CIRCLE}"
            r"\N{BLACK CIRCLE}\N{WHITE BULLET}\N{BLACK RIGHT-POINTING POINTER}\N{CHECK MARK}")
_BLANK = re.compile(r"_{4,}")
_LEADER = re.compile(r"[ \t]*(?:\.[ \t]?){4,}[ \t]*|"
                     r"[ \t]*(?:\N{HORIZONTAL ELLIPSIS}[ \t]?){2,}[ \t]*")
_LIST_MARKER = re.compile(r"^((?:[" + _BULLETS + r"]|\(?(?:\d{1,3}|[A-Za-z]|[ivxIVX]{2,4})[.)])"
                          r"{1,2}) \| ")


def tidy_text(text: str) -> str:
    """Remove what PDFs leave in extracted text besides words (control characters,
    unmapped and private-use glyphs, cells emptied by that) and put list markers back in
    front of their item: "- | Completed application form" -> "- Completed application form".
    Blanks to fill in ("Date ________") become "___" and dot leaders " ... ". Private-use
    bullets of the Symbol/Wingdings fonts become "\N{BULLET}", a glyph repeated as separate
    words ("m m m m m m", the dot leaders of IRS forms) is dropped and letter-spaced words
    are joined ("s e r v i n g" -> "serving", "2 0 2 2" -> "2022")."""
    text = unescape_leftovers(fix_mojibake(text))   # "3â€“12" -> "3–12", "&amp;" -> "&"
    # the same mojibake after the row text went through NFKC ("™" -> "TM", "…" -> "...")
    text = text.replace("\u00e2\u20acTM", "\u2019").replace("\u00e2\u20ac...", "\u2026")
    text = text.translate(_PRIVATE_BULLETS)
    text = _LEADER.sub(" ... ", _BLANK.sub("___", _CONTROL.sub("", text.translate(_CP1252))))
    lines = []
    for line in text.split("\n"):
        line = join_spaced_letters(_GLYPH_RUN.sub(" ", line))
        cells = (c.strip() for c in line.split(" | "))
        line = " | ".join(c for c in cells if c.strip(". "))  # empty or dot-leader cells
        lines.append(_LIST_MARKER.sub(r"\1 ", line))
    return clean_text("\n".join(lines))


def join_spaced_letters(text: str) -> str:
    """"S P R I N G 2 0 0 9" -> "SPRING 2009": runs of four or more single letters and
    spaced-out years are written as words again (shorter runs such as "a b c" are left)."""
    text = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)
    return _SPACED_YEAR.sub(lambda m: m.group(0).replace(" ", ""), text)


def clean_line(text) -> str:
    """One line of clean text for a title or a bookmark: control characters and private-use
    glyphs removed (Symbol-font bullets become "\N{BULLET}"), whitespace collapsed."""
    text = str(text or "").translate(_PRIVATE_BULLETS)
    return _plain(_CONTROL.sub(" ", text.translate(_CP1252)))


# A text layer that does not decode to words: a font without a Unicode map gives random
# ASCII symbols ('!"#$!&\' %(#"!)'), shifted glyph codes ("1HZ<RUN,QVWLWXWH" for "New York
# Institute") or glyphs mapped to Latin-Extended/Greek letters ("EĂŵĞ͗" for "Name:").
_WORDISH = re.compile(r"^[(\"\u201c\u2018'\[]*([A-Z]?[a-z]+(['\u2019-][A-Za-z]+)*|[A-Z]{1,6}s?|"
                      r"\d+([.,:/-]\d+)*%?|[A-Z][a-z]*\.)[.,;:!?)\"\u201d\u2019'\]]*$")
_ODD_SYMBOLS = frozenset('!"#&\'()*+<=>?@[\\]^`{}~')


def _latin_ext_share(text: str) -> float:
    """Share of the letters of ``text`` that are Latin-Extended/IPA/Greek (U+0100-U+03FF):
    a few percent in Polish, Czech or Vietnamese, most of them in a glyph stand-in line."""
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if 0x100 <= ord(c) <= 0x3FF) / len(letters) if letters else 0.0


def _is_wordish(token: str) -> bool:
    """An English-looking word, number or acronym, or a word of letters of any script
    (accents, Chinese...) that is not mostly Latin-Extended glyph stand-ins."""
    if _WORDISH.match(token):
        return True
    core = token.strip("()[]\"'.,;:!?\u201c\u201d\u2018\u2019")
    letters = core.replace("'", "").replace("\u2019", "").replace("-", "")
    return bool(letters) and letters.isalpha() and _latin_ext_share(letters) <= 0.5


def _unreadable(text: str) -> bool:
    """True when text does not look like words in any script (see above). Words in other
    scripts (Chinese, Hindi...) are fine: they are neither ASCII symbols nor Latin-Extended."""
    tokens = text.split()
    chars = [c for c in text if not c.isspace()]
    if not tokens or not chars:
        return False
    wordish = sum(1 for t in tokens if _is_wordish(t)) / len(tokens)
    if wordish >= 0.3:
        return False
    odd = sum(1 for c in chars if c in _ODD_SYMBOLS) / len(chars)
    latin_ext = sum(1 for c in chars if 0x100 <= ord(c) <= 0x3FF) / len(chars)
    long_ascii = sum(len(t) for t in tokens if len(t) > 25 and t.isascii()
                     and "/" not in t and "@" not in t) / len(chars)
    # shifted glyph codes have few vowels ("1HZ<RUN,QVWLWXWH"); English has ~40 %
    ascii_letters = [c for c in text if c.isascii() and c.isalpha()]
    few_vowels = len(ascii_letters) >= 50 and \
        sum(1 for c in ascii_letters if c in "aeiouyAEIOUY") < 0.2 * len(ascii_letters)
    return odd > 0.15 or latin_ext > 0.08 or long_ascii > 0.5 or few_vowels


def _glyph_stand_ins(line: str) -> bool:
    """A line written in glyph stand-ins ("EĂŵĞ͗ WĂƵůZƵƉƉĞƌƚ" for "Name: Paul Ruppert")."""
    return sum(1 for c in line if c.isalpha()) >= 6 and _latin_ext_share(line) > 0.4


def readable_text(text: str) -> str:
    """The page text without the lines of an undecodable text layer (see _unreadable). On
    such a page, the lines that do read as words ("NAAB Template for Faculty Resumes") are
    kept; a page that has none ends up empty, like a scanned page. Lines of glyph stand-ins
    are dropped on every page (a PDF can mix a broken font with good ones)."""
    lines = text.split("\n")
    if not _unreadable(text):
        if not any(_glyph_stand_ins(line) for line in lines):
            return text
        return clean_text("\n".join(line for line in lines if not _glyph_stand_ins(line)))
    kept = [line for line in lines
            if line.strip() and not _unreadable(line) and _line_reads(line)]
    return clean_text("\n".join(kept))


def _line_reads(line: str) -> bool:
    """A line of an undecodable page that still reads: mostly word-like tokens, two of them
    real words (3 to 20 letters, with a vowel: shifted glyph codes give "DQG" for "and"),
    and no glyph stand-ins."""
    tokens = line.split()
    words = [t for t in tokens if _is_wordish(t)]
    real = [t for t in words if 3 <= len(re.sub(r"[^A-Za-z]", "", t)) <= 20
            and re.search(r"[aeiouyAEIOUY]", t)]
    return (bool(tokens) and len(words) >= 0.6 * len(tokens) and len(real) >= 2
            and not _glyph_stand_ins(line))


def extract_document(path: Path) -> Tuple[List[str], List[List[str]], dict]:
    """Text and bookmark path of every page of a PDF, plus facts about the file.

    Raises when the file cannot be opened (corrupt, password-protected)."""
    if pymupdf is None or not hasattr(pymupdf, "open"):
        raise RuntimeError("PyMuPDF is needed for the docs step: pip install pymupdf")
    doc = pymupdf.open(str(path))
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError("the PDF is password-protected")
        meta = dict(doc.metadata or {})
        page_count = doc.page_count
        toc = clean_toc(doc.get_toc(simple=True))
        pages: List[Tuple[list, float]] = []
        image_pages: Set[int] = set()
        heading = None
        for page in doc:
            pno = page.number + 1
            rows: list = []
            try:
                # (a landscape page stored with /Rotate is turned the way it is displayed
                # first, see pdf_catalog._page_lines; printed_title then reads it that way too)
                rows = page_rows(page)
            except Exception as exc:  # one broken page must not lose the whole document
                log.warning("%s page %d: text extraction failed (%s)", path.name, pno, exc)
            try:
                if pno == 1:
                    heading = printed_title(page)
                if page.get_images(full=False):
                    image_pages.add(pno)
            except Exception as exc:
                log.debug("%s page %d: %s", path.name, pno, exc)
            pages.append((rows, page.rect.height))
    finally:
        doc.close()

    drop = [running | numbers
            for running, numbers in zip(find_running_lines(pages), page_number_rows(pages))]
    drop = [skip | edges for skip, edges in zip(drop, repeated_edge_rows(pages, drop))]
    texts = [tidy_text(rows_to_text([row for i, row in enumerate(rows) if i not in skip]))
             for (rows, _height), skip in zip(pages, drop)]
    readable = [readable_text(t) for t in texts]
    unreadable_pages = sum(1 for a, b in zip(texts, readable) if a != b)
    texts = readable
    words = [word_count_clean(t) for t in texts]
    empty = [pno for pno, n in enumerate(words, 1) if n == 0]
    image_only = sum(1 for pno in empty if pno in image_pages)
    info = {
        "pages": page_count,
        "words": sum(words),
        "empty_pages": len(empty),
        "image_only_pages": image_only,
        # a scan without a text layer: most pages are pictures and nothing else
        "scanned": page_count > 0 and image_only > page_count / 2,
        "toc_entries": len(toc),
        "header_footer_lines_removed": sum(len(s) for s in drop),
        "unreadable_pages": unreadable_pages,
        "metadata_title": meta.get("title") or "",
        "printed_title": heading,
        "modified": pdf_date(meta.get("modDate")) or pdf_date(meta.get("creationDate")),
        "producer": " ".join(str(meta.get("producer") or meta.get("creator") or "").split()),
    }
    return texts, toc_paths(toc, page_count), info


# the heading of a template that is still a placeholder: "Course Title", "Click here to enter
# text", "[Insert Program Name]", "Presentation Title"
_TEMPLATE_PLACEHOLDER = re.compile(
    r"^\W*((course|document|report|presentation|project|program|proposal|event|paper|slide|"
    r"form|article|your)\s+)?(title|name|heading)(\s+here)?\W*$|^\W*(click|tap)\s+(here|or tap)\b|"
    r"^\W*(insert|enter|type|add)\s+(the\s+|your\s+|a\s+)?([\w-]+\s+){0,3}(title|name|text|here)\b",
    re.I)


def extract_office_document(path: Path, file_type: str
                            ) -> Tuple[List[str], List[List[str]], dict]:
    """extract_document for a .docx/.xlsx/.pptx file (office.extract_office): the same
    three values, the text of every page cleaned like PDF text (tidy_text) and the facts
    that only PDFs have (image-only pages, scans, removed running headers) set to zero.
    ``printed_title`` is the first heading (Title style first) or slide title.

    Raises office.OfficeError when the file cannot be read (damaged, unsafe, too big)."""
    texts, paths, facts = office.extract_office(path, file_type)
    texts = [tidy_text(t) for t in texts]
    paths = [[t for t in (clean_line(x)[:200] for x in p) if t] for p in paths]
    words = [word_count_clean(t) for t in texts]
    first = clean_line(facts.get("printed_title") or "")[:150]
    if (len(first.split()) > 25 or len(first) < 4 or _NOT_A_TITLE.search(first)
            or _TEMPLATE_PLACEHOLDER.search(first)):
        first = ""
    info = {
        "pages": len(texts),
        "words": sum(words),
        "empty_pages": sum(1 for n in words if n == 0),
        "image_only_pages": 0,
        "scanned": False,
        "toc_entries": int(facts.get("toc_entries") or 0),
        "header_footer_lines_removed": 0,
        "unreadable_pages": 0,
        "metadata_title": facts.get("metadata_title") or "",
        "printed_title": first or None,
        "modified": facts.get("modified"),
        "producer": facts.get("producer") or "",
    }
    return texts, paths, info


def extract_file(path: Path, file_type: str = PDF) -> Tuple[List[str], List[List[str]], dict]:
    """extract_document (PDF) or extract_office_document (.docx/.xlsx/.pptx)."""
    if file_type in office.OFFICE_TYPES:
        return extract_office_document(path, file_type)
    return extract_document(path)


_PAGE_NUMBER = re.compile(r"(page\s*)?\d{1,4}(\s*(/|of)\s*\d{1,4})?|-\s*\d{1,4}\s*-", re.I)
_JUNK_BOOKMARK = re.compile(r"_?(ole_link|hlk|toc|ref|goback)\d*\w*|bookmark\s*\d*|"
                            r"(slide|page)\s*\d+", re.I)


def page_number_rows(pages: List[Tuple[list, float]]) -> List[Set[int]]:
    """Bare page numbers ("7", "Page 7", "7 of 12", "- 7 -") near the top or bottom edge.

    ``find_running_lines`` only drops numbers that count up with the page and sit in the
    outer 8%; documents restart their numbering in attachments, show slide numbers, and Word
    puts footers a little higher. A number near an edge is only taken for a page number when
    at least 10% of the pages (and 3 or more) have one at that edge; in a document of one or
    two pages, when it is the page's own number. A table cell that continues at the top of
    the next page ("General Electives | Credits:" / "39") is therefore kept: it happens on
    1 of the 581 pages of the 2025-2026 catalog PDFs, page numbers on 11-93% of the pages
    of numbered documents."""
    marks: List[Dict[int, str]] = []
    for rows, height in pages:
        top, bottom = PAGE_NUMBER_ZONE * height, (1.0 - PAGE_NUMBER_ZONE) * height
        edge_of = {}
        for i, row in enumerate(rows):
            if (row.y1 <= top or row.y0 >= bottom) and _PAGE_NUMBER.fullmatch(row.text()):
                edge_of[i] = "top" if row.y1 <= top else "bottom"
        marks.append(edge_of)
    if len(pages) >= 3:
        pages_with = Counter(edge for edge_of in marks for edge in set(edge_of.values()))
        edges = {edge for edge, n in pages_with.items() if n >= max(3.0, 0.1 * len(pages))}
        return [{i for i, edge in edge_of.items() if edge in edges} for edge_of in marks]
    return [{i for i in edge_of if re.search(r"\d+", rows[i].text()).group() == str(pno)}
            for pno, (edge_of, (rows, _height)) in enumerate(zip(marks, pages), 1)]


def _edges(rows: list, dropped: Set[int]) -> List[int]:
    """Indexes of the topmost and the bottommost non-empty row of a page that is kept."""
    visible = [i for i, row in enumerate(rows) if i not in dropped and row.text()]
    if not visible:
        return []
    top = min(visible, key=lambda i: rows[i].y0)
    bottom = max(visible, key=lambda i: rows[i].y1)
    return [top] if top == bottom else [top, bottom]


def repeated_edge_rows(pages: List[Tuple[list, float]],
                       dropped: List[Set[int]]) -> List[Set[int]]:
    """Running headers/footers that ``find_running_lines`` misses because they are not in its
    narrow margin: the first or last line of a page whose text (digits ignored) is the first
    or last line of at least half of the pages (3 or more). The first page that shows it
    keeps it, as it is often the document's title there."""
    keys_by_page = [{i: _furniture_key(rows[i].text()) for i in _edges(rows, skip)}
                    for (rows, _height), skip in zip(pages, dropped)]
    counts = Counter(key for keys in keys_by_page for key in set(keys.values()))
    need = max(3.0, 0.5 * len(pages))
    drop: List[Set[int]] = []
    seen: Set[str] = set()
    for keys in keys_by_page:
        repeated = {i for i, key in keys.items() if counts[key] >= need}
        first = {i for i in repeated if keys[i] not in seen}
        seen.update(keys[i] for i in repeated)
        drop.append(repeated - first)
    return drop


def _furniture_key(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def clean_toc(toc: List[list]) -> List[list]:
    """Bookmarks without the names Word exports for its hidden bookmarks ("OLE_LINK7",
    "_Hlk123") and with spaces for the underscores of bookmark names
    ("1._Annual_Stipend" -> "1. Annual Stipend"); control characters and private-use glyphs
    are removed from the titles ("II. \\x07Presenters" -> "II. Presenters")."""
    out = []
    for item in toc or []:
        try:
            level, title, page = int(item[0]), clean_line(item[1]), int(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not re.search(r"[A-Za-z]", title) or _JUNK_BOOKMARK.fullmatch(title):
            continue
        if " " not in title and "_" in title:
            title = " ".join(title.replace("_", " ").split())
        title = title.lstrip("\N{BULLET} ")
        if title:
            out.append([level, title, page])
    return out


_PDF_DATE = re.compile(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                       r"(?:([Zz+-])(\d{2})?'?(\d{2})?'?)?")


def pdf_date(value) -> Optional[str]:
    """PDF date string -> ISO 8601: "D:20230415093000-04'00'" -> "2023-04-15T09:30:00-04:00",
    "D:20230415" -> "2023-04-15". ``None`` when missing or implausible."""
    match = _PDF_DATE.match(str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second, sign, tz_h, tz_m = match.groups()
    try:
        stamp = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0),
                         int(minute or 0), int(second or 0))
    except ValueError:
        return None
    if not 1990 <= stamp.year <= datetime.now().year + 1:
        return None
    if hour is None:
        return stamp.date().isoformat()
    iso = stamp.isoformat()
    if sign in ("Z", "z") or (sign and not tz_h):
        return iso + "+00:00"
    if sign:
        return f"{iso}{sign}{tz_h}:{tz_m or '00'}"
    return iso


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------
_OFFICE_TITLE = re.compile(r"^(microsoft\s+)?(word|powerpoint|excel|publisher)\s+-\s+", re.I)
_FILE_NAME = re.compile(r"\.(docx?|pdf|pptx?|xlsx?|rtf|txt|indd|idml|ai|psd|eps|qxp|pages|key|"
                        r"odt|png|jpe?g|tiff?)\s*$", re.I)
_GENERIC_TITLE = re.compile(
    r"^(untitled( document)?(-?\s*\d+)?|document\s*\d*|title|slide\s*\d*|presentation\s*\d*|"
    r"powerpoint presentation|print|blank|default|new document|layout\s*\d*|page\s*\d*|"
    r"pdf|form|flyer|brochure|adobe acrobat|acrobat pdfmaker.*|scan\w*|img\w*|"
    r"microsoft word document)$", re.I)
_GENERIC_ANCHOR = re.compile(
    r"^(click )?(here|this|link|this link|download|download (the )?(pdf|file|form)|view|"
    r"view (the )?(pdf|file)|pdf|\(pdf\)|read more|learn more|more|more info(rmation)?|"
    r"form|this form|the form|application|document|this document|file|open|print)$", re.I)
_DOWNLOAD_PREFIX = re.compile(r"^(download|view|open|read|see)\s+(the\s+|our\s+|a\s+|an\s+)?",
                              re.I)
_PDF_SUFFIX = re.compile(r"\s*[\[(]\s*pdf[^\])]*[\])]\s*$|\s+-?\s*pdf$", re.I)
_URLISH = re.compile(r"^(https?://|www\.)|\.(edu|com|org|gov|net)(/|$)", re.I)
# web pages printed to PDF keep the page's <title>: "PDF | X", "X | New York Tech"
_PRINTED_PAGE_AFFIXES = re.compile(r"^pdf\s*\|\s*|\s+[|\N{EN DASH}-]\s+(new york institute of "
                                   r"technology|new york tech|nyit)\s*$", re.I)


def _file_stem(url: str) -> str:
    stem = unquote(url_path(url).rstrip("/").rsplit("/", 1)[-1])
    return re.sub(r"\.(pdf|docx|xlsx|pptx)$", "", stem, flags=re.I)


def metadata_title(title: str, url: str) -> Optional[str]:
    """The PDF's own title when it is a real title, not an authoring-tool default
    ("Microsoft Word - form.docx", "Untitled-1") or the file name."""
    title = _PRINTED_PAGE_AFFIXES.sub("", clean_line(title))
    if (len(title) < 4 or not re.search(r"[A-Za-z]{2}", title) or _OFFICE_TITLE.match(title)
            or _FILE_NAME.search(title) or _GENERIC_TITLE.match(title)
            or re.match(r"^([A-Za-z]:\\|/)", title)):
        return None
    squashed = re.sub(r"[\W_]+", "", title).lower()
    if squashed == re.sub(r"[\W_]+", "", _file_stem(url)).lower():
        return None  # just the file name again
    if " " not in title and re.search(r"[_.]", title):
        return None  # "ADM_Agencies", "brochure.final"
    if " " not in title and re.search(r"\d", title):
        return None  # a document code: "E4795IC4" (IRS e-file id), "Vol4No3"
    if "_" in title and re.search(r"\d", title):
        return None  # a file name with spaces: "Vol4No3_006-13-campus buzz"
    return title[:200]


# what a link to an Office file says about its format: "Guide (Powerpoint download)",
# "Affidavit (Word document download)", "Budget plan [XLSX]", "Form - docx"
_OFFICE_SUFFIX = re.compile(r"\s*[\[(]\s*(ms\s+|microsoft\s+)?(word|excel|power\s*point|docx?|xlsx?|pptx?)"
                            r"\b[^\])]*[\])]\s*$|\s+-?\s*(docx?|xlsx?|pptx?)$", re.I)


# what screen-reader text adds to a link: "NY Tech PA Competencies This link opens in a new
# window", "Mod 2.0 (link)", "Guide (opens in new tab)", "Resources [external link]"
_A11Y_SUFFIX = re.compile(r"\s*[\[(]?\s*(this\s+link\s+)?(opens|will\s+open)\s+in\s+(a\s+)?new\s+"
                          r"(browser\s+)?(window|tab)\s*[\])]?\W*$|"
                          r"\s*[\[(]\s*(external\s+)?link\s*[\])]\s*$|\s+-?\s*external\s+link\s*$",
                          re.I)
_CLICK_HERE = re.compile(r"^(please\s+)?click\s+(here\s+)?(for|to)\s+((view|see|download|get|"
                         r"open|read)\s+)?", re.I)
# a piece of a link whose text was split between two elements: "t and Capital Form" (of
# "IT Project and Capital Request Form"), "download one here"
_ANCHOR_FRAGMENT = re.compile(r"^[b-hj-z]\s|\bhere\W*$")


def link_text(text: str) -> str:
    """The text of a link without what screen readers are told about it ("This link opens in
    a new window", "(link)")."""
    text = _plain(text)
    while True:
        shorter = _A11Y_SUFFIX.sub("", text).strip()
        if shorter == text:
            return text
        text = shorter


def office_anchor(text: str) -> str:
    """The text of a link to an Office file without the note on its format."""
    return _OFFICE_SUFFIX.sub("", link_text(text))


def anchor_title(text: str) -> Optional[str]:
    """The text of a link to the PDF, unless it is generic ("here", "Download PDF"), a URL or a
    piece of a split link ("t and Capital Form", "download one here")."""
    text = _PDF_SUFFIX.sub("", link_text(text)).strip(" :-")
    if (not text or _GENERIC_ANCHOR.match(text) or _URLISH.search(text) or _FILE_NAME.search(text)
            or _ANCHOR_FRAGMENT.search(text)):
        return None
    stripped = _DOWNLOAD_PREFIX.sub("", _CLICK_HERE.sub("", text))
    if stripped and not _GENERIC_ANCHOR.match(stripped):
        text = stripped
    if len(text) < 4 or not re.search(r"[A-Za-z]{2}", text):
        return None
    return (text[0].upper() + text[1:])[:200]


def file_title(url: str) -> str:
    """A readable version of the file name: "ADV_DegreeMap_SOHP_ExerciseScienceBS_2025.pdf"
    -> "ADV Degree Map SOHP Exercise Science BS 2025"."""
    stem = _file_stem(url)
    if " " not in stem:
        stem = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", stem)  # url-slug style names
    stem = re.sub(r"[_+]+", " ", stem)
    stem = _GLUED_TO.sub(_split_glued_to, stem)                # WCCCStoCS -> WCCCS to CS
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)           # CampusMap -> Campus Map
    stem = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z]{2})", " ", stem)  # NYITCOMTechnical: NYITCOM T...
    stem = re.sub(r"(?<=[A-Za-z])(?=(19|20)\d\d\b)", " ", stem)  # Form2021 -> Form 2021
    stem = _DATE_IN_NAME.sub(_iso_date, stem)                  # 20231127 -> 2023-11-27
    stem = " ".join(stem.split())
    return stem[:1].upper() + stem[1:] if stem else "PDF document"


_DATE_IN_NAME = re.compile(r"(?<![0-9])((?:19|20)\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?![0-9])")
# "to" glued between two parts of a file name: "WCCCStoCS", "CMSVMathtoCS", "WCCCISToIT"
# (CamelCase splitting alone gives "WCCC Sto CS", "CMSV Mathto CS", "WCCCISTo IT")
_GLUED_TO = re.compile(r"(?<=[A-Za-z])(to|To)(?=[A-Z])")
# words that end in "to" and are not "... to ...": "PhotoGallery", "TorontoCampus", "IntoThe"
_WORDS_ENDING_IN_TO = {"alto", "auto", "bento", "burrito", "canto", "concerto", "cyto", "ditto",
                       "ghetto", "goto", "grotto", "histo", "into", "kyoto", "libretto", "lotto",
                       "magneto", "manifesto", "memento", "mosquito", "motto", "onto", "otto",
                       "pesto", "photo", "pinto", "pluto", "potato", "presto", "pronto", "proto",
                       "risotto", "sacramento", "soto", "tomato", "toronto", "unto", "veto"}


def _split_glued_to(match) -> str:
    to = match.group(1)
    if to == "to":
        before = match.string[:match.start()]
        word = re.search(r"[A-Z]?[a-z]*$", before).group()  # the CamelCase word "to" ends
        if (word + to).lower() in _WORDS_ENDING_IN_TO:
            return to
    return f" {to} "


def _iso_date(match) -> str:
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
                "the", "to", "with"}
_NOT_A_TITLE = re.compile(  # buttons, web addresses, the university's name, generic words,
    # a date line ("Friday, April 24, 9 A.M. - 3 P.M.")
    r"^(apply|register|learn more|read more|more information|click|visit|download|scan|call|"
    r"contact us)\b|\b(now|here)\W*$|https?://|www\.|\.(edu|com|org)\b|"
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s|"
    r"\b\d{1,2}(:\d\d)?\s*[ap]\.\s?m\.|\b\d+(st|nd|rd|th)\s+floor\b|"
    r"^(new york institute of technology|new york tech|nyit)$|^(table of )?contents$|"
    r"^(agenda|introduction|overview|welcome|index|appendix|application|form|memo(randum)?|"
    r"notice|flyer|confidential|draft)$", re.I)
# "School of Management" above a semester map's real title, "Office of the Registrar" on forms
_ORG_UNIT = re.compile(r"^(the\s+)?(office|school|college|department|division|center|centre)"
                       r"\s+(of|for)\b", re.I)
# link text that only says which year/term a PDF is for: "2026", "Fiscal Year 2023", "Fall 2021"
_YEAR_LINK = re.compile(r"^((fiscal|academic|calendar|school)\s+year\s+|fy\s*|"
                        r"(fall|spring|summer|winter|january|february|march|april|may|june|july|"
                        r"august|september|october|november|december)\s+)?(19|20)\d\d"
                        r"(\s*[-/\N{EN DASH}]\s*((19|20)?\d\d))?$", re.I)


def _title_case(text: str) -> str:
    """Title case for an ALL-CAPS heading; short words like "AI", "BS", "EMT" stay upper case."""
    if not re.search(r"[A-Z]{4}", text) or re.search(r"[a-z]", text):
        return text
    words = []
    for i, word in enumerate(text.split()):
        letters = re.sub(r"[^A-Za-z]", "", word)
        if i and letters.lower() in _SMALL_WORDS:
            words.append(word.lower())
        elif len(letters) <= 3 or "." in word:
            words.append(word)              # AI, BS, EMT, B.S.
        else:
            words.append(word[:1] + word[1:].lower())
    return " ".join(words)


def printed_title(page) -> Optional[str]:
    """The title printed at the top of a page: its largest horizontal text in the top 40%
    of the page, when that is clearly larger than the body text (several lines of the same
    size are joined; a school/office name above the title is left out)."""
    lines = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT).get("blocks", []):
        for line in block.get("lines", []) if block.get("type", 0) == 0 else []:
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans or abs(line.get("dir", (1, 0))[0] - 1.0) > 0.01:
                continue                    # empty or vertical text
            text = _plain("".join(s["text"] for s in line["spans"]))
            lines.append((text, max(s.get("size", 0.0) for s in spans), tuple(line["bbox"])))
    if not lines:
        return None
    sizes: Counter = Counter()
    for text, size, _bbox in lines:
        sizes[round(size, 1)] += len(text)
    body = sizes.most_common(1)[0][0]
    candidates = [ln for ln in lines if len(re.findall(r"[A-Za-z]", ln[0])) >= 4
                  and not _NOT_A_TITLE.search(ln[0])
                  and (" " in ln[0] or not re.search(r"\d", ln[0]))]
    top = 0.4 * page.rect.height
    size = max((ln[1] for ln in candidates if ln[2][1] < top), default=0.0)
    if size < 1.2 * body:
        return None
    parts = sorted((ln for ln in candidates if abs(ln[1] - size) <= max(0.3, 0.02 * size)),
                   key=lambda ln: (round(ln[2][1]), ln[2][0]))
    start = next(i for i, ln in enumerate(parts) if ln[2][1] < top)
    block = [parts[start]]
    for ln in parts[start + 1:start + 4]:   # the following lines of the same heading
        if ln[2][1] - block[-1][2][3] > 1.5 * size:
            break
        block.append(ln)
    if len(block) > 1:
        block = [ln for ln in block if not _ORG_UNIT.match(ln[0])] or block
    title = _drop_interleaved_name(" ".join(_title_case(ln[0]) for ln in block).strip(" :-|"))
    if len(title.split()) > 25 or (title.endswith(".") and len(title.split()) > 8):
        return None                         # a sentence (a form's instructions), not a title
    return title[:150] if len(title) >= 4 else None


_NYIT_NAME = ("new", "york", "institute", "of", "technology")


def _drop_interleaved_name(title: str) -> str:
    """Two logos side by side are read line by line, so the partner's name and NYIT's name
    end up interleaved: "Queensborough Community NEW York Institute of College Technology"
    -> "Queensborough Community College" (articulation agreements with other colleges)."""
    words = title.split()
    lower = [re.sub(r"\W", "", w).lower() for w in words]
    if "york" not in lower or "institute" not in lower:
        return title
    keep, taken, i = [], [], 0
    for pos, (word, low) in enumerate(zip(words, lower)):
        if i < len(_NYIT_NAME) and low == _NYIT_NAME[i]:
            i += 1
            taken.append(pos)
            continue
        keep.append(word)
    interleaved = bool(taken) and taken[-1] - taken[0] + 1 > len(taken)
    rest = " ".join(keep)
    if (i >= 4 and interleaved and len(keep) >= 2
            and re.search(r"\b(college|university|school|community|county|borough|boces)\b",
                          rest, re.I)):
        return rest
    return title


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


# Titles that are no title at all (form labels, the university's name, "in this issue")
_JUNK_TITLE = re.compile(
    r"^(page\s*\d+(\s*(of|/)\s*\d+)?|name|date|signature|untitled|\(?anonymous\)?|"
    r"in this issue|(table of )?contents|title|(the\s+)?(new york institute of technology|"
    r"new york tech(nology)?|ny institute of technology|nyit))$", re.I)
# ... or only say who published the document: "School of Management", "NEW YORK COLLEGE OF
# OSTEOPATHIC MEDICINE", "Office of the Registrar" (weak: used only when nothing else exists)
_PUBLISHER_TITLE = re.compile(
    r"^(the\s+)?((new york|nyit|new york tech)\s+)?("
    r"college of (arts and sciences|engineering and computing sciences|osteopathic medicine)|"
    r"school of (architecture and design|health professions|management)|"
    r"(office|department|division|center|centre)\s+(of|for)(\s+the)?(\s+[a-z&,'-]+){1,3})$", re.I)
# words that only date a title: "2018 Program" and "November 2023" are no titles on their own
_DATE_WORDS = re.compile(
    r"(19|20)\d\d|\d{1,2}|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
    r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?|fall|spring|summer|winter|fy|ay", re.I)
_LETTER_SPACED = re.compile(r"(?<!\S)(?:[^\W_] ){2,}[^\W_](?!\S)")
_NYIT_NAME_IN_TITLE = re.compile(r"^\s*new york institute of technology\b\s*|"
                                 r"\s*\bnew york institute of technology\s*$", re.I)
_PARTNER_WORDS = re.compile(r"\b(college|community|county|university|boces|borough)\b", re.I)


def clean_title(text) -> str:
    """A title candidate without control characters and private-use glyphs, letter-spaced
    words joined ("M A G A Z I N E" -> "MAGAZINE"), the university's name dropped when it
    was printed next to a partner's ("Suffolk County NEW York Institute OF Technology" ->
    "Suffolk County") and trailing backslashes, pipes and dashes stripped ("Hear\\")."""
    title = _drop_interleaved_name(clean_line(text))
    title = _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), title)
    shorter = _NYIT_NAME_IN_TITLE.sub(" ", title).strip()
    if shorter != title and 2 <= len(shorter.split()) <= 4 and _PARTNER_WORDS.search(shorter):
        title = shorter                # two logos side by side: the partner's name is the title
    return title.strip(" \\/|:;,*_-\N{BULLET}\N{EN DASH}\N{EM DASH}\N{BLACK SQUARE}"
                       "\N{WHITE SQUARE}\N{BLACK SMALL SQUARE}\N{BLACK CIRCLE}\N{WHITE BULLET}")


def title_quality(title: str) -> str:
    """"junk" (no title at all: "Page 1 of 2", "Name", "Hear", the university's name),
    "weak" (one word besides dates, "Agenda", "MAGAZINE", "2018 Program", "November 2023", or
    just the publisher: "School of Management") or "good"."""
    title = (title or "").strip()
    letters = re.sub(r"[^A-Za-z]", "", title)
    words = re.findall(r"[^\W_]+", title)
    if len(letters) < 2 or len(title) < 4 or _JUNK_TITLE.match(title):
        return "junk"
    if len(words) == 1 and len(letters) < 5:
        return "junk"
    content = [w for w in words if not _DATE_WORDS.fullmatch(w)]
    if len(content) < 2 or _PUBLISHER_TITLE.match(title):
        return "weak"
    return "good"


# words a title shares with nearly every NYIT document: they do not show it is this one's
_COMMON_TITLE_WORDS = _SMALL_WORDS | {"new", "york", "institute", "technology", "tech", "nyit",
                                      "nyitcom", "university", "college", "school", "office"}


def _stem(word: str) -> str:
    """"policies" -> "polic", "procedures" -> "procedur": plural and verb endings off, so that
    "Policies" is found in "AcademicIntegrityPolicy"."""
    return re.sub(r"(ies|es|s|ing|ed)$", "", word) if len(word) > 5 else word


def matches_document(title: str, evidence: Sequence[str]) -> bool:
    """True when ``title`` is about the document that ``evidence`` (its file name, the text of
    links to it, its printed title, its first lines) describes: at least half of the title's
    words (besides "of", "the", "New York Institute of Technology" and numbers) occur there.
    The title metadata that an Office file keeps from the template it was made from fails
    ("THE CAMPAIGN FOR" for a pledge letter, "NYIT VANCOUVER FACULTY HANDBOOK" for a grant
    application)."""
    words = {w for w in _words(title) if not w.isdigit() and w not in _COMMON_TITLE_WORDS}
    if not words:
        return False
    squashed = re.sub(r"[\W_]+", "", " ".join(evidence).lower())
    tokens = set(re.findall(r"[a-z0-9]+", " ".join(t for t, _ in name_tokens(" ".join(evidence)))))
    found = sum(1 for w in words if (_stem(w) in squashed if len(w) >= 4 else w in tokens))
    return 2 * found >= len(words)


def first_lines(text: Optional[str], lines: int = 3, words: int = 12) -> str:
    """The first lines of a page (where its title is), the first ``words`` words of each (not
    the paragraph that follows the title)."""
    kept = [line.split()[:words] for line in (text or "").split("\n") if line.strip()][:lines]
    return " ".join(" ".join(line) for line in kept)


def trusted_metadata_title(meta_title: str, url: str, printed: Optional[str] = None,
                           anchors: Sequence[str] = (), first_text: Optional[str] = None
                           ) -> Optional[str]:
    """metadata_title; when the first page has text (``first_text``), only if the title
    matches the document (matches_document)."""
    meta = metadata_title(meta_title, url)
    if meta and first_lines(first_text):
        evidence = [_file_stem(url), printed or "", first_lines(first_text)]
        if not matches_document(meta, evidence + [link_text(a) for a in anchors]):
            return None
    return meta


def _cut_off(text: str, url: str) -> bool:
    """A link text that stops in the middle of a word of the file name: "University Projec"
    for "University Project and Capital Request Form.docx" (the rest of the link was in
    another element). A plural or verb ending is no cut ("Graduate Program", "Programs")."""
    last = (re.findall(r"[A-Za-z]+", text) or [""])[-1].lower()
    names = {w.lower() for w in re.findall(r"[A-Za-z]+", file_title(url))}
    if len(last) < 3 or last in names:
        return False
    return any(w.startswith(last) and w[len(last):] in ("t", "e", "n", "on", "ion", "ct")
               for w in names)


def pick_title(meta_title: str, printed: Optional[str], anchors: Sequence[str],
               url: str, first_text: Optional[str] = None) -> Tuple[str, str]:
    """(title, where it came from): the PDF's title metadata, else the title printed on its
    first page, else the text of a link to it, else its file name -- the first of them that
    is a real title (title_quality "good"); a weak one ("Agenda", "School of Management") only
    when none is. A link that only names a year or month ("2026" on the semester-map page,
    "April 2022") is added in parentheses when the title lacks that year.

    ``first_text`` is the text of the first page: when it is given, the title metadata is only
    used when it matches the document (matches_document: file name, link text, printed title
    and first lines), not when it is left over from a template."""
    candidates: List[Tuple[str, str]] = []
    meta = trusted_metadata_title(meta_title, url, printed, anchors, first_text)
    if meta:
        candidates.append((clean_title(meta), "metadata"))
    if printed:
        shown, named = clean_title(printed), file_title(url)
        # "Reference Form" -> "Mental Health Counseling Reference Form"
        if (len(_words(shown)) <= 2 < len(_words(named))
                and " ".join(_words(shown)) in " ".join(_words(named))):
            candidates.append((named, "file_name"))
        else:
            candidates.append((shown, "printed_title"))
    candidates += [(clean_title(t), "link_text") for t in map(anchor_title, anchors)
                   if t and not _cut_off(t, url)]
    candidates.append((file_title(url), "file_name"))
    quality = [(t, source, title_quality(t)) for t, source in candidates if t]
    title, source = next(((t, src) for t, src, q in quality if q == "good"),
                         next(((t, src) for t, src, q in quality if q == "weak"),
                              (file_title(url), "file_name")))
    year_link = next((a for a in map(_plain, anchors) if _YEAR_LINK.match(a)), None)
    if year_link and not set(re.findall(r"(?:19|20)\d\d", year_link)) & set(_words(title)):
        title = f"{title} ({year_link})"
    return title[:200], source


# ---------------------------------------------------------------------------
# Editions, series and categories
# ---------------------------------------------------------------------------
class Edition(NamedTuple):
    """The year, term or date a file name gives a document."""

    kind: str           # date | month | term | academic_year | fiscal_year | year | range | cohort
    label: str          # "2025-09-10", "2021-03", "Fall 2020", "2023-24", "FY2015", "2016", ...
    end: date           # the last day the edition covers
    tokens: frozenset   # positions of its words in name_tokens()


_TERMS = {"fall": ("Fall", 12, 31), "fa": ("Fall", 12, 31), "autumn": ("Fall", 12, 31),
          "spring": ("Spring", 5, 31), "sp": ("Spring", 5, 31), "spr": ("Spring", 5, 31),
          "summer": ("Summer", 8, 31), "su": ("Summer", 8, 31), "sum": ("Summer", 8, 31),
          "winter": ("Winter", 2, 28), "wi": ("Winter", 2, 28), "win": ("Winter", 2, 28)}
_SHORT_TERMS = {"fa", "sp", "su", "wi", "spr", "sum", "win"}  # only when glued to the year: FA2020
_MONTHS = {name: i for i, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"), 1)}
_MONTHS.update({name[:3]: i for name, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_REVISION_WORDS = {"rev", "revised", "updated", "update", "approved"}
# words of a file name that only tell versions apart: "_Final", "_Rev01", "V07N02", "_LLR"
_VERSION_WORDS = {"final", "draft", "rev", "revised", "updated", "update", "version", "copy",
                  "compressed", "compress", "approved", "lr", "llr", "hr", "web"}
_VERSION_PREFIXES = {"v", "vol", "volume", "n", "no", "issue", "rev"}   # + a number
_COHORT_NAME = re.compile(r"degree ?map|semester ?map|plan of study|class of|four year plan|"
                          r"4 year plan|curriculum map", re.I)


def name_tokens(name: str) -> List[Tuple[str, int]]:
    """The words of a file name, lower case, with the number of the chunk (between "_", "-",
    spaces...) each comes from: "REGISTRAR_AcademicSenateCalendar_FA2020" ->
    [("registrar", 0), ("academic", 1), ("senate", 1), ("calendar", 1), ("fa", 2), ("2020", 2)]."""
    out: List[Tuple[str, int]] = []
    for chunk_no, chunk in enumerate(c for c in re.split(r"[^A-Za-z0-9]+", name) if c):
        chunk = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", chunk)
        chunk = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", chunk)
        out.extend((t.lower(), chunk_no) for t in chunk.split())
    return out


def _year(token: str) -> Optional[int]:
    return int(token) if len(token) == 4 and token.isdigit() and 1990 <= int(token) <= 2039 \
        else None


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_editions(name: str) -> List[Edition]:
    """Every year/term/date a file name gives, in order: "NYITCOM_StudentHandbook20232024" ->
    [2023-24], "REGISTRAR_AcademicSenateCalendar_FA2020" -> [Fall 2020],
    "NYITCOM_SGA_Agenda_20250910" -> [2025-09-10], "PLAN_..._GlobalFactSheet_AY1718" ->
    [2017-18], "NYITAAUP_2015FiscalYear" -> [FY2015], "Academic_Calendar_-_Year_3_-_Class_of_2025"
    -> [Class of 2025], "EnrichmentEdition_032021" -> [2021-03]."""
    toks = name_tokens(name)
    words = [t for t, _ in toks]
    found: List[Edition] = []
    i, n = 0, len(toks)

    def add(kind: str, label: str, end: date, width: int) -> None:
        found.append(Edition(kind, label, end, frozenset(range(i, i + width))))

    while i < n:
        t = words[i]
        nxt = words[i + 1] if i + 1 < n else ""
        nxt2 = words[i + 2] if i + 2 < n else ""
        glued = i + 1 < n and toks[i + 1][1] == toks[i][1]
        if t == "class" and nxt == "of" and _year(nxt2):
            add("cohort", f"Class of {nxt2}", date(int(nxt2), 8, 31), 3)
            i += 3
            continue
        if t.isdigit() and len(t) == 8:
            y, m, d = int(t[:4]), int(t[4:6]), int(t[6:])
            if _year(t[:4]) and 1 <= m <= 12 and 1 <= d <= calendar.monthrange(y, m)[1]:
                add("date", f"{y}-{m:02d}-{d:02d}", date(y, m, d), 1)
                i += 1
                continue
            if _year(t[:4]) and _year(t[4:]) == y + 1:
                add("academic_year", f"{y}-{t[6:]}", date(y + 1, 8, 31), 1)
                i += 1
                continue
        if t.isdigit() and len(t) == 6:
            for y, m in ((int(t[:4]), int(t[4:])), (int(t[2:]), int(t[:2]))):
                if 1990 <= y <= 2039 and 1 <= m <= 12:
                    add("month", f"{y}-{m:02d}", _month_end(y, m), 1)
                    break
            i += 1
            continue
        if t in _TERMS and (t not in _SHORT_TERMS or glued) and nxt.isdigit() and len(nxt) in (2, 4):
            y = int(nxt) if len(nxt) == 4 else 2000 + int(nxt)
            following = words[i + 2] if i + 2 < n else ""
            if 1990 <= y <= 2039 and not (following.isdigit() and len(following) in (2, 4)
                                          and int(following) % 100 == (y + 1) % 100):
                name_, month, day = _TERMS[t]
                add("term", f"{name_} {y}", date(y, month, day), 2)
                i += 2
                continue
        if t in _MONTHS and _year(nxt):
            add("month", f"{nxt}-{_MONTHS[t]:02d}", _month_end(int(nxt), _MONTHS[t]), 2)
            i += 2
            continue
        if t in ("ay", "fy") and nxt.isdigit():
            y1 = _year(nxt)
            if t == "ay" and y1 and nxt2.isdigit() and int(nxt2) % 100 == (y1 + 1) % 100:
                add("academic_year", f"{y1}-{(y1 + 1) % 100:02d}", date(y1 + 1, 8, 31), 3)
                i += 3
                continue
            if t == "ay" and len(nxt) == 4 and not y1 and int(nxt[2:]) == int(nxt[:2]) + 1:
                y = 2000 + int(nxt[2:])
                add("academic_year", f"{y - 1}-{nxt[2:]}", date(y, 8, 31), 2)
                i += 2
                continue
            y = y1 or (2000 + int(nxt) if len(nxt) == 2 else None)
            if y and 1990 <= y <= 2039:
                if t == "ay":
                    add("academic_year", f"{y - 1}-{y % 100:02d}", date(y, 8, 31), 2)
                else:
                    add("fiscal_year", f"FY{y}", date(y, 6, 30), 2)
                i += 2
                continue
        y = _year(t)
        if y:
            if nxt == "fiscal" and nxt2 == "year":
                add("fiscal_year", f"FY{y}", date(y, 6, 30), 3)
                i += 3
                continue
            y2 = _year(nxt)
            if y2 == y + 1 or (nxt.isdigit() and len(nxt) == 2 and int(nxt) == (y + 1) % 100):
                add("academic_year", f"{y}-{(y + 1) % 100:02d}", date(y + 1, 8, 31), 2)
                i += 2
                continue
            if y2 and y2 > y + 1:
                add("range", f"{y}-{y2}", date(y2, 12, 31), 2)
                i += 2
                continue
            if nxt.isdigit() and len(nxt) == 2 and 1 <= int(nxt) <= 12:
                m = int(nxt)
                if nxt2.isdigit() and len(nxt2) == 2 and 1 <= int(nxt2) <= calendar.monthrange(y, m)[1]:
                    add("date", f"{y}-{m:02d}-{int(nxt2):02d}", date(y, m, int(nxt2)), 3)
                    i += 3
                    continue
                add("month", f"{y}-{m:02d}", _month_end(y, m), 2)
                i += 2
                continue
            add("year", str(y), date(y, 12, 31), 1)
            i += 1
            continue
        if (t.isdigit() and len(t) == 2 and nxt.isdigit() and len(nxt) == 2
                and int(nxt) == int(t) + 1 and int(t) <= 39 and toks[i + 1][1] == toks[i][1] + 1):
            add("academic_year", f"{2000 + int(t)}-{nxt}", date(2001 + int(t), 8, 31), 2)
            i += 2
            continue
        i += 1
    return found


def edition_of(name: str) -> Optional[Edition]:
    """The edition a file name stands for: the date after "Rev"/"Updated" when there is one
    ("2020-NAAB-Procedures..._Rev_May_2025" -> 2025-05), else the first one it gives."""
    found = parse_editions(name)
    if not found:
        return None
    words = [t for t, _ in name_tokens(name)]
    for ed in found:
        first = min(ed.tokens)
        if first and words[first - 1] in _REVISION_WORDS:
            return ed
    return found[0]


def series_name(name: str) -> str:
    """The file name without its years, terms, dates and version words, the name every
    edition of a document shares: "NYITCOM_StudentHandbook20242025" -> "nyitcom student
    handbook", "NYITMagazine_V07N02_Spring2009" -> "nyit magazine"."""
    toks = name_tokens(name)
    used = set()
    for ed in parse_editions(name):
        used |= ed.tokens
    words: List[str] = []
    skip_number = False
    for i, (t, _chunk) in enumerate(toks):
        if i in used or t in _VERSION_WORDS:
            skip_number = t in _VERSION_PREFIXES
            continue
        if t in _VERSION_PREFIXES and i + 1 < len(toks) and toks[i + 1][0].isdigit():
            skip_number = True
            continue
        if skip_number and t.isdigit():
            skip_number = False
            continue
        skip_number = False
        words.append(t)
    while words and words[-1].isdigit():          # "_2027-1": an upload counter
        words.pop()
    return " ".join(words)


# What kind of document a PDF is (rules on the file name + title, first match wins; the
# folder of the URL helps for a few). Coarse, but enough to filter or weight the corpus.
DOC_CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ("semester_map", r"degree ?map|semester ?maps?|plan of study|four year plan|4 year plan|"
                     r"curriculum map"),
    # (a dual degree is two NYIT degrees, not a transfer: "Creating a Dual Degree", "4+1 Dual
    # Degree"; dual/joint admission is with a community college)
    ("transfer_agreement", r"articulation|transfer credits|transfer agreement|transfer "
                           r"equivalen|common equivalents|community college|"
                           r"(joint|dual) admission"),
    ("course_material", r"\bsom mp\b|syllab|assignment|homework|case study|team project|"
                        r"group project|weekly quiz|\bquiz|exam review"),
    ("financial_filing", r"\b990\b|fiscal ?year|\bfy ?\d|return of organization exempt|"
                         r"financial statement|\baudit"),
    ("schedule_calendar", r"calendar|schedule of classes|class ?schedul|exam schedule|"
                          r"timetable|pay dates|important dates|deadlines"),
    ("newsletter_magazine", r"magazine|newsletter|news letter|enrichment edition|bulletin|"
                            r"\bjournal\b|humanism in medicine|\bvol \d|campus buzz"),
    ("form", r"\bforms?\b|application|request|waiver|petition|checklist|worksheet|consent|"
             r"authorization|questionnaire|timesheet|reimbursement|nomination|declaration|"
             r"intent to|registration"),
    ("policy_handbook", r"handbook|polic(y|ies)|procedures?|guidelines?|manual|code of conduct|"
                        r"bylaws|regulations|\brules\b|title ix|compliance|standards|charter|"
                        r"constitution|governance|agreement|contract|\bcba\b|terms and conditions"),
    ("report", r"\breports?\b|accreditation|naab|self ?study|assessment|fact ?sheet|statistic|"
               r"survey|outcomes|placement|match numbers|residenc|enrollment|frequency|"
               r"data\b|findings|recipients|scholars"),
    ("event_program", r"program abstracts|abstracts|symposium|conference|commencement|agenda|"
                      r"minutes|\bsource\b|gala|ceremony|lecture series|corporate challenge|"
                      r"competition|poster|\bevents?\b|invitation|program book"),
    ("training", r"training|tutorial|how ?to|quick start|webinar|workshop|presentation|slides|"
                 r"module|orientation|instructions|e ?portfolio|coordinator"),
    # ("program" alone is no brochure: "Preliminary Analysis for New Program Proposals")
    ("brochure", r"brochure|viewbook|flyer|one ?pager|overview|booklet|catalog|guide|"
                 r"cookbook|\bmap\b|directory|program summary|fellowship"),
)
_CATEGORY_RULES = [(name, re.compile(pattern, re.I)) for name, pattern in DOC_CATEGORIES]
# Words that say what a document is whatever its subject or folder, checked before the rules
# above and before the /policies/ folder (which also holds the forms, templates and survey
# results that go with a policy): a form, template, affidavit, log or sample letter to fill in
# ("Change in Degree Map Request Form", "Missing Receipt Affidavit", "Form 2025"; not "Form
# 990": a tax filing), the results of a survey ("HEDS Sexual Assault Campus Climate Survey ... Frequency
# Report").
KIND_CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ("form", r"\bforms?\b(?! (?!(19|20)\d\d\b)\d)|\btemplates?\b|affidavit|questionnaire|worksheet|check ?list|"
             r"\blogs?\b(?! (in|on|off)\b)|\b(sample|model) letters?\b"),
    ("report", r"\bsurveys?\b|\bfrequency\b"),
)
_KIND_RULES = [(name, re.compile(pattern, re.I)) for name, pattern in KIND_CATEGORIES]
# folders whose documents are all of one kind ...
_FOLDER_CATEGORY = (("semester_maps", "semester_map"), ("magazine", "newsletter_magazine"))
# ... and folders that only say what a document is when its words (KIND_CATEGORIES) do not
_FOLDER_DEFAULT = (("/policies/", "policy_handbook"),)
# A coarse prior for the search (1.0 = as useful as a web page); older editions count half.
CATEGORY_WEIGHTS: Dict[str, float] = {
    "policy_handbook": 1.0, "form": 0.9, "schedule_calendar": 0.9, "semester_map": 0.9,
    "transfer_agreement": 0.8, "brochure": 0.8, "other": 0.7, "training": 0.6, "report": 0.5,
    "course_material": 0.5, "event_program": 0.4, "newsletter_magazine": 0.4,
    "financial_filing": 0.2}


def doc_category(url: str, title: str = "", qualifier: str = "") -> str:
    """One of DOC_CATEGORIES (or "other") for a PDF, from its file name, title and folder: a
    semester-map or magazine folder, else what the words say the document is
    (KIND_CATEGORIES: a form, template, survey), else the /policies/ folder, else the first
    rule of DOC_CATEGORIES that matches. The words of the title's ``qualifier``
    (disambiguate_titles: "(2026-02, Make a copy of the template.)") count for the rules, not
    for KIND_CATEGORIES."""
    path = unquote(url_path(url)).lower()
    for marker, category in _FOLDER_CATEGORY:
        if marker in path:
            return category
    title = title or ""
    own = title[:-len(qualifier) - 3] if qualifier and title.endswith(f" ({qualifier})") else title
    in_name = " ".join(t for t, _ in name_tokens(_file_stem(url)))
    text = in_name + " | " + " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    own_text = in_name + " | " + " ".join(re.findall(r"[a-z0-9]+", own.lower()))
    kind = next((name for name, rule in _KIND_RULES if rule.search(own_text)), None)
    if kind:
        return kind
    for marker, category in _FOLDER_DEFAULT:
        if marker in path:
            return category
    return next((name for name, rule in _CATEGORY_RULES if rule.search(text)), "other")


def document_weight(category: Optional[str], superseded: bool = False) -> float:
    """``source_weight`` of a document page in corpus.jsonl (see CATEGORY_WEIGHTS)."""
    weight = CATEGORY_WEIGHTS.get(category or "other", CATEGORY_WEIGHTS["other"])
    return round(weight * (0.5 if superseded else 1.0), 2)


def _iso_day(value) -> Optional[date]:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None
    except ValueError:
        return None


def annotate_documents(docs: List[dict], reference: Optional[date] = None) -> dict:
    """Add ``doc_category``, ``edition``, ``doc_year``, ``cohort_year``, ``series``,
    ``superseded`` and ``superseded_by`` to per-PDF dicts (pdf_url, site, title, modified).

    PDFs of one website whose file names differ only by a year/term/date (series_name)
    are editions of one document. In a series of semester maps or class calendars every
    edition is for another entering class (``cohort_year``) and stays current. Otherwise an
    edition is ``superseded`` when a newer one exists and its own period is over by
    ``reference`` (default: today): the 2023-24 and 2024-25 student handbooks next to the
    2025-26 one, last year's IRS filing, the Fall 2020 calendar -- but not the Fall 2026 or
    Spring 2027 calendar. A file without a year in its name is dated by its modification
    date. Returns counts for documents_summary.json."""
    reference = reference or date.today()
    by_series: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for doc in docs:
        stem = _file_stem(doc["pdf_url"])
        edition = edition_of(stem)
        doc["edition"] = edition.label if edition else None
        doc["_end"] = edition.end if edition else None
        modified = _iso_day(doc.get("modified"))
        doc["doc_year"] = edition.end.year if edition else (modified.year if modified else None)
        cohort = bool(_COHORT_NAME.search(" ".join(t for t, _ in name_tokens(stem)))
                      or _COHORT_NAME.search(doc.get("title") or "")
                      or "semester_maps" in doc["pdf_url"].lower())
        year = re.search(r"(?:19|20)\d\d", edition.label) if cohort and edition else None
        doc["cohort_year"] = int(year.group()) if year else None
        doc["_cohort"] = cohort
        doc["doc_category"] = doc_category(doc["pdf_url"], doc.get("title") or "",
                                           doc.get("title_qualifier") or "")
        doc["series"], doc["superseded"], doc["superseded_by"] = None, False, None
        key = series_name(stem)
        if len(re.sub(r"[^a-z]", "", key)) >= 3:
            by_series[(doc.get("site") or url_host(doc["pdf_url"]), key)].append(doc)
    series_count = superseded = 0
    for (site, key), members in sorted(by_series.items()):
        if len(members) < 2 or not any(m["_end"] for m in members):
            continue
        series_count += 1
        for m in members:
            m["series"] = f"{site} {key}"
        if any(m["_cohort"] for m in members):
            continue
        dated = [(m["_end"] or _iso_day(m.get("modified")), m) for m in members]
        dated = [(end, m) for end, m in dated if end]
        if len(dated) < 2:
            continue
        newest_end, newest = max(dated, key=lambda em: (em[0], em[1]["pdf_url"]))
        for end, m in dated:
            if end < newest_end and end < reference:
                m["superseded"], m["superseded_by"] = True, newest["pdf_url"]
                superseded += 1
    for doc in docs:
        doc.pop("_end", None)
        doc.pop("_cohort", None)
    return {"series": series_count, "documents_in_series": sum(1 for d in docs if d["series"]),
            "superseded_documents": superseded,
            "cohort_documents": sum(1 for d in docs if d.get("cohort_year")),
            "by_category": dict(Counter(d["doc_category"] for d in docs).most_common())}


def annotate_page_records(records: List[dict], reference: Optional[date] = None) -> None:
    """annotate_documents for documents.jsonl page records written by an older version
    (no category/edition fields): the fields are worked out per PDF and copied to its pages;
    ``scanned`` = most of its pages have no text."""
    by_pdf: Dict[str, List[dict]] = defaultdict(list)
    for rec in records:
        by_pdf[str(rec.get("pdf_url") or str(rec.get("url") or "").split("#")[0])].append(rec)
    docs = [{"pdf_url": url, "site": recs[0].get("site") or url_host(url),
             "title": recs[0].get("title") or "", "modified": recs[0].get("modified")}
            for url, recs in by_pdf.items()]
    annotate_documents(docs, reference)
    for doc in docs:
        recs = by_pdf[doc["pdf_url"]]
        scanned = sum(1 for r in recs if not r.get("word_count")) > len(recs) / 2
        for rec in recs:
            for key in ANNOTATION_FIELDS:
                rec[key] = doc.get(key)
            rec["scanned"] = scanned


ANNOTATION_FIELDS = ("doc_category", "edition", "doc_year", "series", "superseded",
                     "superseded_by", "cohort_year")


def title_key(title: str) -> str:
    """A title compared without case, punctuation and spacing."""
    return " ".join(re.findall(r"[^\W_]+", (title or "").casefold()))


_NAME_DATE_WORD = re.compile(r"((19|20)\d\d(-\d\d){0,2}|\d{1,2}|\d{6}|\d{8})")


def _qualifiers(doc: dict, members: List[dict]) -> Dict[str, str]:
    """What can tell a PDF apart from others with the same title: its edition, another title
    it has (printed/metadata), the text of a link to it, the words of its file name that the
    others' file names do not have, its whole file name."""
    title_words = set(_words(doc["title"]))
    out: Dict[str, str] = {"edition": doc.get("edition") or ""}
    for alt in doc.get("_alt_titles") or []:
        if (title_quality(alt) == "good" and not set(_words(alt)) <= title_words
                and not re.search(r"new york institute of technology", alt, re.I)):
            out["other"] = alt[:80]
            break
    for anchor in doc.get("_anchors") or []:
        text = clean_title(anchor_title(anchor) or "")
        if text and title_quality(text) == "good" and not set(_words(text)) <= title_words:
            out["link"] = text[:80]
            break
    name = file_title(doc["pdf_url"])
    others = [set(file_title(m["pdf_url"]).lower().split()) for m in members if m is not doc]
    common = set.intersection(*others) if others else set()
    words = [w for raw, w in zip(name.split(), _name_words(name, title_words))
             if raw.lower() not in common and w.lower() not in title_words
             and not (w == raw and _NAME_DATE_WORD.fullmatch(w)) and w.lower() not in _VERSION_WORDS]
    while words and words[0].lower() in _SMALL_WORDS:      # "to CS" of "WCCCS to CS"
        words.pop(0)
    while words and words[-1].lower() in _SMALL_WORDS:
        words.pop()
    out["file"] = " ".join(words)[:60]
    out["name"] = name[:80]
    return out


def _name_words(name: str, title_words: Set[str]) -> List[str]:
    """The words of a file_title; a word of the title with a number glued to it is that
    number ("ThinkPairShare1" of "Think Pair Share": "1", not "Share1"). Codes such as
    "ACCT101" or "A1" stay whole."""
    out = []
    for word in name.split():
        glued = re.fullmatch(r"([A-Za-z]+)(\d{1,3})", word)
        out.append(glued.group(2) if glued and glued.group(1).lower() in title_words else word)
    return out


def doc_file_type(doc: dict) -> str:
    """The file type of a per-document dict or a documents.jsonl record: its ``file_type``,
    else the extension of its URL, else pdf."""
    return str(doc.get("file_type") or document_type(str(doc.get("pdf_url") or "")) or PDF)


def title_group(title: str, file_type: str) -> Tuple[str, str]:
    """What two documents must share for their titles to clash: the title (title_key) and
    the file type. The corpus title names the format ("(PDF, p. 1)", "(Word, part 1)",
    "(Excel, part 1)"), so a PDF and an Excel file may have the same title."""
    return title_key(title), file_type or PDF


_QUALIFIER_KINDS = ("edition", "other", "link", "file", "name")


def disambiguate_titles(docs: List[dict]) -> Dict[str, int]:
    """Make titles that several PDFs share tell them apart: "Internship Residency Placement by
    Specialty" x18 -> "... (2016)" ... "(2026)", "Queensborough Community College" x18 ->
    "Queensborough Community College (Transfer Credits and Required Courses for B.S. in
    Biology)", "Student Government Association" -> "... (2025-09-10, Agenda)".

    The PDFs that share a title get, kind by kind (_QUALIFIER_KINDS: edition, another title
    of theirs, link text, the distinctive words of the file name, the file name), the
    qualifier that tells them apart; a kind is only used for PDFs that still share their
    title and only when it differs among them. Only documents of one file type are compared
    (title_group): an Excel sheet titled like a PDF leaves the PDF's title alone. ``docs`` are
    per-PDF dicts with pdf_url, title, edition and optionally file_type and
    _alt_titles/_anchors; the qualifier added is stored as ``title_qualifier``. Returns
    counts."""
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for doc in docs:
        groups[title_group(doc["title"], doc_file_type(doc))].append(doc)
    changed = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        quals = {id(m): _qualifiers(m, members) for m in members}
        labels: Dict[int, List[str]] = {id(m): [] for m in members}

        def full(m: dict) -> str:
            return title_key(" ".join([m["title"]] + labels[id(m)]))

        for kind in _QUALIFIER_KINDS:
            clashes: Dict[str, List[dict]] = defaultdict(list)
            for m in members:
                clashes[full(m)].append(m)
            for same in clashes.values():
                values = {quals[id(m)].get(kind) or "" for m in same}
                if len(same) < 2 or len(values) < 2:
                    continue
                for m in same:
                    value = quals[id(m)].get(kind)
                    if value and title_key(value) not in full(m):
                        labels[id(m)].append(value)
        for m in members:
            if labels[id(m)]:
                label = ", ".join(labels[id(m)])
                m["title"] = f"{m['title']} ({label})"[:200]
                m["title_qualifier"] = label
                changed += 1
    return {"titles_disambiguated": changed,
            "pdfs_sharing_a_title": sum(len(urls) for urls in shared_titles(docs))}


def one_file_in_other_formats(urls: Sequence[str]) -> bool:
    """True when ``urls`` are one document published in several formats ("Form.pdf" and
    "Form.docx" in the same folder): a title they share is no clash (their corpus titles
    say "(PDF, p. 1)" and "(Word, part 1)")."""
    split = [re.match(r"(.*)\.(pdf|docx|xlsx|pptx)$", url, flags=re.I) for url in urls]
    return all(split) and len({m.group(1).lower() for m in split}) == 1 \
        and len({m.group(2).lower() for m in split}) == len(split)


def shared_titles(docs: Iterable[dict]) -> List[List[str]]:
    """The pdf_urls of the documents of one file type that share a title, one list per title
    (title_group: "Form.pdf" and "Form.docx", or a PDF and an Excel sheet titled alike, do
    not count: their corpus titles name the format)."""
    by_title: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for doc in docs:
        by_title[title_group(doc["title"], doc_file_type(doc))].append(doc["pdf_url"])
    return [urls for urls in by_title.values() if len(urls) > 1]


# ---------------------------------------------------------------------------
# The `docs` command
# ---------------------------------------------------------------------------
def _redirect_target(entry: dict) -> Optional[str]:
    final = normalize_url(entry.get("final_url") or "")
    return final if final and final != entry["url"] else None


def doc_url(entry: dict) -> str:
    """The URL a downloaded document is cited with: where the link redirects to when that is
    a URL of the same type of file (site.nyit.edu/files/X.pdf -> files.nyit.edu/files/X.pdf),
    else the link itself."""
    final = _redirect_target(entry)
    if final and skip_reason(final) is None and document_type(final) == entry_type(entry):
        return final
    return entry["url"]


def group_downloads(settings: Settings, manifest: Dict[str, dict], max_bytes: Optional[int]
                    ) -> Tuple[List[List[dict]], Dict[str, List[str]]]:
    """The downloaded PDFs to extract, grouped by identical content (SHA-1), and the URLs
    left out by reason: the link or the URL it redirects to is out of scope (a full catalog
    PDF, a host that sites.py disables, another organisation's website), noindex (the
    server asks search engines not to index the file: X-Robots-Tag) or over_max_mb (a file
    downloaded earlier with a higher --max-mb)."""
    groups: Dict[str, List[dict]] = defaultdict(list)
    left_out: Dict[str, List[str]] = defaultdict(list)
    for url, entry in sorted(manifest.items()):
        if not _cached_ok(settings, entry):
            continue
        final = _redirect_target(entry)
        reason = skip_reason(url) or (skip_reason(final) if final else None)
        if reason:
            left_out[reason].append(url)
        elif entry.get("noindex"):
            left_out[NOINDEX].append(url)
        elif max_bytes and (entry.get("bytes") or 0) > max_bytes:
            left_out[OVER_MAX_MB].append(url)
        else:
            groups[entry.get("sha1") or url].append(entry)
    return list(groups.values()), left_out


def extract_documents(settings: Settings, groups: List[List[dict]],
                      reference: Optional[date] = None
                      ) -> Tuple[List[DocumentPageRecord], List[dict], List[dict]]:
    """One record per page of every distinct document (PDF or Office file), one summary
    entry per document, and the documents that could not be read.

    Once every document is read, the titles that several share are told apart
    (disambiguate_titles) and every document gets its category, edition and series
    (annotate_documents; ``reference`` is the date editions are judged by)."""
    docs: List[dict] = []
    pages: Dict[str, Tuple[List[str], List[List[str]]]] = {}
    failures: List[dict] = []
    used: Set[str] = set()
    for entries in groups:
        # cite the file by a link that is not redirected, if there is one
        entries.sort(key=lambda e: (doc_url(e) != e["url"], e["url"]))
        canonical = entries[0]
        pdf_url = doc_url(canonical)
        if pdf_url in used:  # the file behind a redirect changed between two downloads
            pdf_url = canonical["url"]
            if pdf_url in used:
                log.warning("%s: same URL as another downloaded file, skipped", pdf_url)
                continue
        used.add(pdf_url)
        found_on = dedupe_keep_order(
            u for e in entries for u in _as_list(e.get("found_on")))[:MAX_FOUND_ON]
        file_type = entry_type(canonical)
        try:
            texts, paths, info = extract_file(docs_dir(settings) / canonical["file"], file_type)
        except Exception as exc:  # corrupt or encrypted file: report it, keep the others
            log.error("could not read %s (%s): %s", canonical["file"], pdf_url, exc)
            failures.append({"url": pdf_url, "site": url_host(pdf_url), "file_type": file_type,
                             "status": EXTRACT_ERROR, "error": f"{type(exc).__name__}: {exc}",
                             "found_on": found_on})
            continue
        anchors = [e.get("anchor_text") or "" for e in entries]
        if file_type != PDF:
            anchors = [office_anchor(a) for a in anchors]
        # (Office files only: a PDF's title metadata is mostly its own; the template an
        # Office file was made from leaves its title in docProps/core.xml)
        first_text = next((t for t in texts if t.strip()), "") if file_type != PDF else None
        title, title_source = pick_title(info["metadata_title"], info["printed_title"], anchors,
                                         pdf_url, first_text)
        meta = trusted_metadata_title(info["metadata_title"], pdf_url, info["printed_title"],
                                      anchors, first_text)
        modified = info["modified"] or canonical.get("last_modified")  # else the HTTP date
        aliases = {e["url"] for e in entries} | {doc_url(e) for e in entries}
        pages[pdf_url] = (texts, paths)
        docs.append({
            "pdf_url": pdf_url, "site": url_host(pdf_url), "file_type": file_type,
            "status": OK, "title": title,
            "title_source": title_source, "pages": info["pages"], "words": info["words"],
            "empty_pages": info["empty_pages"], "image_only_pages": info["image_only_pages"],
            "unreadable_pages": info["unreadable_pages"], "scanned": info["scanned"],
            "toc_entries": info["toc_entries"], "modified": modified,
            "bytes": canonical.get("bytes") or 0, "sha1": canonical.get("sha1"),
            "file": canonical["file"], "links": len(entries),
            "aliases": sorted(aliases - {pdf_url}), "found_on": found_on,
            "metadata_title": clean_line(info["metadata_title"]),
            "printed_title": info["printed_title"], "producer": info["producer"],
            "_anchors": anchors,
            "_alt_titles": [clean_title(t) for t in (meta, info["printed_title"]) if t],
        })
    # editions first: they are the best way to tell apart PDFs that share a title
    for doc in docs:
        edition = edition_of(_file_stem(doc["pdf_url"]))
        doc["edition"] = edition.label if edition else None
    title_counts = disambiguate_titles(docs)
    annotation_counts = annotate_documents(docs, reference)
    records: List[DocumentPageRecord] = []
    for doc in docs:
        doc.pop("_anchors", None)
        doc.pop("_alt_titles", None)
        texts, paths = pages.pop(doc["pdf_url"])
        for pno, (text, toc_path) in enumerate(zip(texts, paths), 1):
            records.append(DocumentPageRecord(
                doc_id=f"doc-{short_hash(doc['pdf_url'])}-p{pno:04d}",
                site=doc["site"],
                url=f"{doc['pdf_url']}#page={pno}",
                pdf_url=doc["pdf_url"],
                title=doc["title"],
                found_on=doc["found_on"],
                page=pno,
                page_count=doc["pages"],
                toc_path=toc_path,
                text=text,
                word_count=word_count_clean(text),
                file=doc["file"],
                modified=doc["modified"],
                doc_category=doc["doc_category"],
                edition=doc["edition"],
                doc_year=doc["doc_year"],
                series=doc["series"],
                superseded=doc["superseded"],
                superseded_by=doc["superseded_by"],
                cohort_year=doc["cohort_year"],
                scanned=bool(doc["scanned"]),
                file_type=doc["file_type"],
            ))
    records.sort(key=lambda r: (r.site, r.pdf_url, r.page))
    docs.sort(key=lambda d: (d["site"], d["pdf_url"]))
    if docs:
        log.info("document titles: %d told apart by edition, link text or file name; %d PDFs "
                 "still share a title", title_counts["titles_disambiguated"],
                 title_counts["pdfs_sharing_a_title"])
        log.info("editions: %d series of yearly documents, %d older editions superseded, %d "
                 "semester maps / class calendars", annotation_counts["series"],
                 annotation_counts["superseded_documents"],
                 annotation_counts["cohort_documents"])
    return records, docs, failures


def _download_failures(found: Candidates, manifest: Dict[str, dict]) -> List[dict]:
    out = []
    for url in sorted(found.items):
        entry = manifest.get(url)
        if entry and entry.get("status") != OK:
            out.append({"url": url, "site": entry.get("site"),
                        "file_type": found.items[url].get("file_type") or PDF,
                        "status": entry["status"], "http_status": entry.get("http_status"),
                        "error": entry.get("error", ""),
                        "found_on": _as_list(entry.get("found_on"))})
    return out


def reference_date(manifest: Dict[str, dict]) -> date:
    """The date editions are judged by: the day the last PDF was downloaded (the data is a
    snapshot of that day), else today."""
    fetched = [str(e.get("fetched_at") or "")[:10] for e in manifest.values()
               if e.get("status") == OK]
    day = _iso_day(max(fetched)) if fetched and max(fetched) else None
    return day or date.today()


# Office files linked from NYIT pages (the crawler files them as "asset" links)
OFFICE_EXTENSIONS = (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".rtf", ".odt")
# why a linked Office file is not read (not_extracted_files), besides the download statuses
# of docs_manifest.jsonl, EXTRACT_ERROR and the reasons of Candidates.skipped
LEGACY_FORMAT = "legacy_binary_format"      # .doc, .xls, .ppt (Office 97-2003)
OTHER_FORMAT = "unsupported_format"         # .rtf, .odt
NOT_DOWNLOADED_YET = "not_downloaded_yet"   # no outcome recorded (yet)
_LEGACY_EXTENSIONS = (".doc", ".xls", ".ppt")


def find_unread_files(settings: Settings, reasons: Optional[Dict[str, str]] = None,
                      read: Iterable[str] = ()) -> dict:
    """Word, Excel and PowerPoint files that NYIT pages link to on the websites we crawl
    (external_links.jsonl) and whose text is NOT in documents.jsonl, with the reason:
    legacy binary formats (.doc, .xls, .ppt) and .rtf/.odt are not read at all; a
    .docx/.xlsx/.pptx is missing when its download failed (the manifest status: not_found,
    not_office, encrypted, too_large...), it could not be read (extract_error), it was left
    out (noindex, over_max_mb, max_docs) or it was not tried yet. ``reasons`` maps such
    URLs to their reason; ``read`` lists the URLs whose file was extracted (aliases
    included). Counts per file type and per reason, every file with its reason under
    ``details`` and up to MAX_EXAMPLES example URLs."""
    read = set(read)
    reasons = reasons or {}
    files: Dict[str, Tuple[str, str]] = {}      # url -> (extension, reason)
    for rec in read_jsonl(settings.external_links_path):
        if not isinstance(rec, dict) or rec.get("kind") not in ("asset", "pdf"):
            continue
        url = normalize_url(str(rec.get("url") or ""))
        if not url or skip_reason(url) or url in read:
            continue
        ext = os.path.splitext(url_path(url).lower())[1]
        if ext not in OFFICE_EXTENSIONS:
            continue
        if ext in _LEGACY_EXTENSIONS:
            reason = LEGACY_FORMAT
        elif office_type(url):
            reason = reasons.get(url) or NOT_DOWNLOADED_YET
        else:
            reason = OTHER_FORMAT
        files[url] = (ext, reason)
    def most_first(counts: Counter) -> Dict[str, int]:
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return {"files": len(files),
            "by_type": most_first(Counter(ext for ext, _reason in files.values())),
            "by_reason": most_first(Counter(reason for _ext, reason in files.values())),
            "examples": sorted(files)[:MAX_EXAMPLES],
            "details": [{"url": url, "type": ext, "reason": reason}
                        for url, (ext, reason) in sorted(files.items())]}


def _unread_reasons(found: Candidates, manifest: Dict[str, dict], failures: List[dict],
                    postponed: Sequence[str]) -> Dict[str, str]:
    """Why each Office candidate has no text in documents.jsonl (see find_unread_files)."""
    out: Dict[str, str] = {}
    for reason, urls in found.skipped.items():
        out.update((u, reason) for u in urls if office_type(u))
    out.update((u, "max_docs") for u in postponed if office_type(u))
    for url, item in found.items.items():
        status = (manifest.get(url) or {}).get("status")
        if item.get("file_type") in office.OFFICE_TYPES and status and status != OK:
            out[url] = status
    for failure in failures:
        if failure.get("status") == EXTRACT_ERROR and office_type(failure.get("url")):
            out[failure["url"]] = EXTRACT_ERROR
    return out


def _title_totals(docs: List[dict]) -> dict:
    shared = shared_titles(docs)
    return {"with_qualifier": sum(1 for d in docs if d.get("title_qualifier")),
            "pdfs_sharing_a_title": sum(len(urls) for urls in shared),
            "shared_titles": len(shared),
            "by_source": dict(Counter(d.get("title_source") for d in docs).most_common())}


def _site_totals(docs: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for d in docs:
        t = out.setdefault(d["site"], {"documents": 0, "pdfs": 0, "pages": 0, "words": 0,
                                       "empty_pages": 0, "mb": 0.0})
        t["documents"] += 1
        t["pdfs"] += 1 if d.get("file_type", PDF) == PDF else 0
        t["pages"] += d["pages"]
        t["words"] += d["words"]
        t["empty_pages"] += d["empty_pages"]
        t["mb"] = round(t["mb"] + d["bytes"] / MB, 2)
    return dict(sorted(out.items()))


def _type_totals(docs: List[dict]) -> Dict[str, dict]:
    """Documents, pages and words per file type (pdf, docx, xlsx, pptx), in that order."""
    out: Dict[str, dict] = {}
    for ftype in FILE_TYPES:
        same = [d for d in docs if d.get("file_type", PDF) == ftype]
        if same:
            out[ftype] = {"documents": len(same), "pages": sum(d["pages"] for d in same),
                          "words": sum(d["words"] for d in same)}
    return out


def run_docs(settings: Settings, max_docs: Optional[int] = None, max_mb: Optional[float] = None,
             fetcher: Optional[PoliteFetcher] = None) -> dict:
    """Download the PDF, Word, Excel and PowerPoint documents linked from NYIT pages and
    extract them (see module doc).

    ``max_docs`` caps the downloads of this run (the next run fetches the rest), ``max_mb``
    skips larger files (default 40; 0 = no limit). Returns the summary that is also written
    to documents_summary.json; it has an "error" key only when nothing could be done
    (PyMuPDF missing, no crawl output yet)."""
    started = time.time()
    if pymupdf is None or not hasattr(pymupdf, "open"):
        log.error("PyMuPDF is not installed: run `pip install pymupdf` (see requirements.txt)")
        return {"error": "PyMuPDF is not installed"}
    settings.ensure_dirs()
    manifest_path = docs_manifest_path(settings)
    if not settings.external_links_path.exists() and not manifest_path.exists():
        msg = f"{settings.external_links_path} not found: run `crawl` first"
        log.error(msg)
        return {"error": msg}
    max_mb = DEFAULT_MAX_MB if max_mb is None else float(max_mb)
    max_bytes = int(max_mb * MB) if max_mb > 0 else None

    found = find_candidates(settings)
    manifest = load_manifest(manifest_path)
    log.info("%d PDF links found by the crawler (+%d in sitemaps, +%d served at URLs without "
             ".pdf) and %d links to .docx/.xlsx/.pptx files (+%d in sitemaps): %d documents on "
             "NYIT websites (left out: %s)", found.links_read, found.sitemap_urls,
             found.manifest_pdfs, found.office_links, found.sitemap_office_urls, len(found.items),
             ", ".join(f"{reason} {len(urls)}" for reason, urls in sorted(found.skipped.items()))
             or "none")
    dl = download_documents(settings, found, manifest, max_docs, max_bytes, fetcher)
    write_jsonl(manifest_path, [manifest[u] for u in sorted(manifest)])  # one line per URL

    groups, left_out = group_downloads(settings, manifest, max_bytes)
    for reason, urls in left_out.items():
        found.skipped[reason].update(urls)
    reference = reference_date(manifest)
    records, docs, failures = extract_documents(settings, groups, reference)
    out_path = settings.processed_dir / DOCUMENTS_FILE
    write_jsonl(out_path, records)

    failures += _download_failures(found, manifest)
    skipped = {reason: len(urls) for reason, urls in sorted(found.skipped.items()) if urls}
    examples = {reason: sorted(urls)[:MAX_EXAMPLES]
                for reason, urls in sorted(found.skipped.items()) if urls}
    if dl["postponed_urls"]:
        skipped["max_docs"] = dl["postponed"]
        examples["max_docs"] = dl["postponed_urls"][:MAX_EXAMPLES]
    status_counts = Counter((manifest.get(u) or {}).get("status", "not_downloaded_yet")
                            for u in found.items)
    pdfs = [d for d in docs if d["file_type"] == PDF]
    by_type = Counter(item.get("file_type") for item in found.items.values())
    read_urls = {u for d in docs for u in [d["pdf_url"]] + d["aliases"]}
    unread = find_unread_files(settings, _unread_reasons(found, manifest, failures,
                                                         dl["postponed_urls"]), read_urls)
    summary = {
        "generated_at": now_iso(),
        "seconds": round(time.time() - started, 1),
        "settings": {"max_docs": max_docs, "max_mb": max_mb if max_bytes else None,
                     "refresh": settings.refresh, "delay": settings.delay},
        "input": {"pdf_links": found.links_read, "sitemap_pdf_urls": found.sitemap_urls,
                  "manifest_pdf_urls": found.manifest_pdfs,
                  "office_links": found.office_links,
                  "sitemap_office_urls": found.sitemap_office_urls,
                  "candidates": len(found.items),
                  "candidates_by_type": {t: by_type[t] for t in FILE_TYPES if by_type[t]},
                  "candidates_by_site": dict(sorted(Counter(
                      item["site"] for item in found.items.values()).items()))},
        "skipped": skipped,
        "skipped_examples": examples,
        "downloads": {k: v for k, v in dl.items() if k != "postponed_urls"},
        "status_counts": dict(status_counts.most_common()),
        "totals": {
            "documents": len(docs),
            "pdfs": len(pdfs),
            # documents, pages and words per file type: pdf, docx, xlsx, pptx
            "by_file_type": _type_totals(docs),
            "links": sum(d["links"] for d in docs),
            "duplicate_links": sum(d["links"] - 1 for d in docs),
            "pages": sum(d["pages"] for d in docs),
            "words": sum(d["words"] for d in docs),
            "empty_pages": sum(d["empty_pages"] for d in docs),
            "image_only_pages": sum(d["image_only_pages"] for d in docs),
            # pages whose text layer does not decode to words (see readable_text)
            "unreadable_pages": sum(d["unreadable_pages"] for d in docs),
            "scanned_pdfs": sum(1 for d in pdfs if d["scanned"]),
            "pdfs_with_bookmarks": sum(1 for d in pdfs if d["toc_entries"]),
            "mb": round(sum(d["bytes"] for d in docs) / MB, 2),
            "failed": len(failures),
        },
        "by_site": _site_totals(docs),
        "titles": _title_totals(docs),
        "editions": {
            "reference_date": reference.isoformat(),
            "series": len({d["series"] for d in docs if d.get("series")}),
            "documents_in_series": sum(1 for d in docs if d.get("series")),
            "superseded_documents": sum(1 for d in docs if d.get("superseded")),
            "superseded_pages": sum(d["pages"] for d in docs if d.get("superseded")),
            "cohort_documents": sum(1 for d in docs if d.get("cohort_year")),
            "with_edition": sum(1 for d in docs if d.get("edition")),
        },
        "categories": {cat: {"documents": n,
                             "pdfs": sum(1 for d in pdfs if d["doc_category"] == cat),
                             "pages": sum(d["pages"] for d in docs if d["doc_category"] == cat)}
                       for cat, n in Counter(d["doc_category"] for d in docs).most_common()},
        # PDFs whose pages are mostly images without a text layer (OCR would be needed)
        "scanned_documents": [{"pdf_url": d["pdf_url"], "title": d["title"], "pages": d["pages"]}
                              for d in docs if d["scanned"]],
        # Office files linked from NYIT pages whose text is not extracted, with the reason
        "not_extracted_files": unread,
        "records": len(records),
        "documents": docs,
        "failures": failures,
        "pymupdf_version": getattr(pymupdf, "VersionBind", ""),
    }
    write_json(settings.processed_dir / DOCUMENTS_SUMMARY_FILE, summary)
    _log_summary(summary, out_path)
    return summary


def _log_summary(summary: dict, out_path: Path) -> None:
    t, dl = summary["totals"], summary["downloads"]
    log.info("downloads: %d new (%.1f MB), %d already on disk, %d failed%s",
             dl["downloaded"], dl["bytes"] / MB, dl["cached"], dl["failed"],
             f", {dl['postponed']} left for the next run (--max-docs)" if dl["postponed"] else "")
    log.info("documents: %d (%s; +%d links to identical files), %d pages, %s words, "
             "%d empty pages (%d scanned PDFs without text), %.1f MB",
             t["documents"], ", ".join(f"{n['documents']} {ftype}" for ftype, n in
                                       t["by_file_type"].items()) or "none",
             t["duplicate_links"], t["pages"], f"{t['words']:,}", t["empty_pages"],
             t["scanned_pdfs"], t["mb"])
    for site, s in summary["by_site"].items():
        log.info("  %-24s %4d documents %6d pages %9s words", site, s["documents"], s["pages"],
                 f"{s['words']:,}")
    by_status = Counter(f["status"] for f in summary["failures"])
    if by_status:
        log.info("not extracted: %s (details under \"failures\" in %s)",
                 ", ".join(f"{k} {v}" for k, v in by_status.most_common()), DOCUMENTS_SUMMARY_FILE)
    unread = summary.get("not_extracted_files") or {}
    if unread.get("files"):
        log.info("Office files linked from NYIT pages that are not extracted: %s (%s)",
                 ", ".join(f"{ext} {n}" for ext, n in unread["by_type"].items()),
                 ", ".join(f"{reason} {n}" for reason, n in unread["by_reason"].items()))
    log.info("wrote %d document pages to %s", summary["records"], out_path)
