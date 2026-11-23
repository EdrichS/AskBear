"""Full catalog PDFs: download them and turn every page into a ``PdfPageRecord``.

NYIT publishes every catalog edition as PDF files (linked from /help/print and
/help/archive), e.g. ``/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf``.
This step

1. downloads the requested editions/kinds into ``data/raw/pdf/`` (skipped when a valid
   copy is already there, so each file is downloaded only once),
2. extracts one record per page from EVERY known catalog PDF in that folder, with the
   section path of the page and a link that opens the PDF at that page (``...pdf#page=7``),
3. writes ``data/processed/pdf_pages.jsonl`` and ``data/processed/pdf_summary.json``.

How the text is extracted (checked on every page of the 2025-2026 and 2026-2027 PDFs,
which are Safari "print to PDF" copies of the website: one column, no running headers,
no page numbers):

* Reading order: the PDF content stream is already in the website's reading order, which
  is what ``page.get_text()`` returns. ``get_text(sort=True)`` re-sorts lines by position
  and so splits table cells that wrap over two lines and multi-line table headers. We keep
  the stream order (the word sequence is identical to ``get_text()``) and only change the
  separators between lines.
* Tables: every table cell is a separate text line. Cells on the same visual row are joined
  with " | " (like the HTML parser does) and a cell that wraps over several lines is kept
  together: "FCWR 151 | Writing II: Foundations of Research Writing [1] | 3".
* Footnote markers are small raised digits that ``get_text()`` glues to the word
  ("Writing1"); they become " [1]".
* Running headers/footers and page numbers are removed only when they sit in the top or
  bottom margin AND repeat on many pages (page numbers must count up with the page), so the
  many "3" credit cells of the curriculum tables are never touched.
* A hyphen at the end of a line is a real hyphen ("full-" / "time"): browsers only break
  lines after a hyphen. The halves are joined WITHOUT deleting it ("full-time"); classic
  de-hyphenation would produce "fulltime". Soft hyphens (U+00AD) are removed.
* Web-page chrome printed into the PDFs is dropped: "Back to Top" lines and the arrows of
  buttons ("More About Admissions >" -> "More About Admissions").

Section paths come from the PDF bookmarks. The 2026-2027 PDFs have no bookmarks, so for
them the outline is rebuilt from the printed headings (see ``heading_toc``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import config
from .config import Settings
from .fetcher import PoliteFetcher
from .models import PdfPageRecord
from .utils import clean_text, dedupe_keep_order, now_iso, write_json, write_jsonl

try:  # PyMuPDF >= 1.24.3
    import pymupdf
except ImportError:  # older PyMuPDF only has the legacy module name
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        pymupdf = None  # type: ignore[assignment]

log = logging.getLogger("askbear.pdf")

PDF_PAGES_FILE = "pdf_pages.jsonl"
PDF_SUMMARY_FILE = "pdf_summary.json"

SUPERSCRIPT = 1              # MuPDF span flags: raised text (footnote markers) ...
BOLD = 16                    # ... and bold text (headings)
MARGIN = 0.08                # top/bottom share of the page where headers/footers live
REPEAT_SHARE = 0.4           # a margin line on >= 40% of the pages is a running header/footer
WRAPPER_SHARE = 0.8          # bookmarks holding >= 80% of all bookmarks wrap the whole document
NEAR_EMPTY_WORDS = 20

_FOOTNOTE_MARK = re.compile(r"\d{1,3}(?:,\d{1,3})*")
_PAGE_NUMBER = re.compile(r"(?:page\s*)?(\d{1,4})(?:\s*(?:/|of)\s*\d{1,4})?", re.IGNORECASE)
_HYPHEN_END = re.compile(r"[a-z][-\u2010\u2011]$")  # a letter + hyphen at the end of a line
_DIGITS = re.compile(r"\d+")
# Web-page chrome printed into the PDFs: "Back to Top" links and the arrows of buttons
# ("More About Admissions >", "Jump to Year Three Courses \u00bb"), as in html_parser.
_BACK_TO_TOP = re.compile(r"^back to top$", re.IGNORECASE)
_TRAILING_ARROW = re.compile(r"\s+[>\u00bb\u203a]+$")


# ---------------------------------------------------------------------------
# Files and downloads
# ---------------------------------------------------------------------------
def pdf_path(settings: Settings, kind: str, edition: str) -> Path:
    """``data/raw/pdf/NYITGraduateCatalog_AY2526.pdf`` for ('graduate', '2025-2026')."""
    return settings.pdf_dir / f"{config.PDF_KINDS[kind]}_{config.PDF_EDITIONS[edition]}.pdf"


def is_valid_pdf(path: Path) -> bool:
    """True for a non-empty file that starts like a PDF (not an HTML error page)."""
    try:
        with Path(path).open("rb") as fh:
            return fh.read(4) == b"%PDF"
    except OSError:
        return False


def local_catalog_pdfs(settings: Settings) -> List[Tuple[str, str, Path]]:
    """(edition, kind, path) of every known catalog PDF present in ``data/raw/pdf``."""
    found = []
    for edition in sorted(config.PDF_EDITIONS):
        for kind in sorted(config.PDF_KINDS):
            path = pdf_path(settings, kind, edition)
            if not path.exists():
                continue
            if is_valid_pdf(path):
                found.append((edition, kind, path))
            else:
                log.warning("%s is not a valid PDF and is skipped (delete it or use --refresh)",
                            path)
    return found


def download_catalog_pdfs(settings: Settings, editions: Sequence[str], kinds: Sequence[str],
                          fetcher: Optional[PoliteFetcher] = None) -> List[dict]:
    """Download the requested catalog PDFs into ``settings.pdf_dir`` (politely, only once).

    A file that already exists, is non-empty and starts with ``%PDF`` is not downloaded
    again unless ``settings.refresh`` is set. Returns one dict per PDF with edition,
    catalog, url, file, status ("cached" | "downloaded" | "failed"), bytes and error.
    """
    settings.pdf_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for edition in editions:
        for kind in kinds:
            url = config.pdf_url(kind, edition)
            path = pdf_path(settings, kind, edition)
            item = {"edition": edition, "catalog": kind, "url": url, "file": path.name,
                    "status": "cached", "bytes": 0, "error": ""}
            if not settings.refresh and is_valid_pdf(path):
                log.info("%s is already downloaded", path.name)
            else:
                fetcher = fetcher or PoliteFetcher(settings)
                item.update(_download_one(fetcher, url, path))
            item["bytes"] = path.stat().st_size if path.exists() else 0
            results.append(item)
    return results


def _download_one(fetcher: PoliteFetcher, url: str, path: Path) -> dict:
    """Download into a temporary file and only replace ``path`` with a real PDF."""
    if not fetcher.allowed(url):
        log.error("robots.txt does not allow %s", url)
        return {"status": "failed", "error": "disallowed by robots.txt"}
    log.info("downloading %s", url)
    tmp = path.with_name(path.name + ".download")
    res = fetcher.download(url, tmp)
    try:
        if not res.ok:
            error = res.error or f"HTTP {res.status}"
            log.error("could not download %s: %s", url, error)
            return {"status": "failed", "error": error}
        if not is_valid_pdf(tmp):  # e.g. an HTML "page not found" served with HTTP 200
            error = f"not a PDF (Content-Type: {res.content_type or 'unknown'})"
            log.error("could not download %s: %s", url, error)
            return {"status": "failed", "error": error}
        os.replace(tmp, path)
    except OSError as exc:  # e.g. the PDF is open in a viewer on Windows
        log.error("could not save %s: %s", path, exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if tmp.exists():
            tmp.unlink()
    log.info("saved %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
    return {"status": "downloaded", "error": ""}


# ---------------------------------------------------------------------------
# Outline -> section path of every page
# ---------------------------------------------------------------------------
def toc_paths(toc: List[list], page_count: int) -> List[List[str]]:
    """Bookmark path of every page (index 0 = page 1), outermost first.

    ``toc`` is ``doc.get_toc(simple=True)``: ``[level, title, page]`` in outline order. The
    path of page p is the stack of open bookmarks after applying, in outline order, every
    entry that starts on a page <= p. Bookmarks that wrap the whole document (the root
    "... Academic Catalogs 2025–2026" and the "... Catalog: Table of Contents" node) are
    left out so paths start at real sections, e.g. ["General Information", "Academic
    Calendar – New York Campuses"]; a page covered only by such wrappers keeps the
    innermost one.
    """
    entries = _toc_entries(toc, page_count)
    wrappers = _wrapper_entries(entries)
    paths: List[List[str]] = []
    for page in range(1, page_count + 1):
        stack: List[int] = []
        for i, (level, _title, start) in enumerate(entries):
            if start <= page:
                while stack and entries[stack[-1]][0] >= level:
                    stack.pop()
                stack.append(i)
        titles = [entries[i][1] for i in stack if entries[i][1] and i not in wrappers]
        if not titles:
            titles = [entries[i][1] for i in stack if entries[i][1]][-1:]
        paths.append(titles)
    return paths


def _toc_entries(toc: List[list], page_count: int) -> List[Tuple[int, str, int]]:
    """Valid (level, clean title, start page) entries; bookmarks without a target are skipped.
    Control characters and private-use glyphs are removed from the titles."""
    entries = []
    for item in toc or []:
        try:
            level, title, page = int(item[0]), str(item[1] or ""), int(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if 1 <= page <= page_count:
            title = " ".join(clean_text(_TOC_JUNK.sub(" ", title)).split())
            entries.append((level, title, page))
    return entries


# control characters and private-use glyphs (Symbol/Wingdings bullets) in bookmark titles
_TOC_JUNK = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ue000-\uf8ff]")


def _wrapper_entries(entries: List[Tuple[int, str, int]]) -> Set[int]:
    """Indexes of bookmarks whose subtree runs to the end and holds most other bookmarks."""
    n = len(entries)
    wrappers = set()
    for i, (level, _title, _page) in enumerate(entries):
        end = i + 1
        while end < n and entries[end][0] > level:
            end += 1
        if n > 1 and end == n and end - i - 1 >= WRAPPER_SHARE * (n - 1):
            wrappers.add(i)
    return wrappers


# ---------------------------------------------------------------------------
# Text lines of a page
# ---------------------------------------------------------------------------
class _Line(NamedTuple):
    text: str                                    # footnote markers rendered as " [n]"
    bbox: Tuple[float, float, float, float]
    size: float                                  # largest font size in the line
    bold: bool                                   # every visible span is bold


def _line_text(line: dict) -> Tuple[str, float]:
    """Text and font size of one MuPDF line; raised footnote digits become " [n]"."""
    spans = line.get("spans") or []
    size = max((s.get("size", 0.0) for s in spans), default=0.0)
    parts: List[str] = []
    for i, span in enumerate(spans):
        text = span.get("text", "")
        mark = text.strip()
        if (span.get("flags", 0) & SUPERSCRIPT and span.get("size", 0.0) < 0.9 * size
                and _FOOTNOTE_MARK.fullmatch(mark)):
            nxt = spans[i + 1].get("text", "") if i + 1 < len(spans) else ""
            text = f" [{mark}]" + (" " if nxt[:1].isalnum() else "")
        parts.append(text)
    return "".join(parts), size


def _page_lines(page) -> List[_Line]:
    """Every text line of a ``pymupdf`` page in content-stream order (images skipped).

    A landscape page stored with a /Rotate entry is first turned the way it is displayed
    (``remove_rotation``, PyMuPDF >= 1.22): otherwise its coordinates are those of the
    unrotated page and the row logic reads it column by column."""
    if getattr(page, "rotation", 0) and hasattr(page, "remove_rotation"):
        page.remove_rotation()
    lines = []
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:  # image block
            continue
        for line in block.get("lines", []):
            text, size = _line_text(line)
            if text:
                visible = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                bold = bool(visible) and all(s.get("flags", 0) & BOLD for s in visible)
                lines.append(_Line(text, tuple(line["bbox"]), size, bold))
    return lines


# ---------------------------------------------------------------------------
# Lines -> rows of table cells -> page text
# ---------------------------------------------------------------------------
class _Cell:
    """Text of one table cell (or of a whole text line) and its bounding box."""

    __slots__ = ("text", "x0", "y0", "x1", "y1", "line_y0", "line_y1")

    def __init__(self, text: str, bbox: Sequence[float]):
        self.text = text
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.line_y0, self.line_y1 = self.y0, self.y1   # top/bottom of the cell's last line

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1

    def add(self, text: str, bbox: Sequence[float], sep: Optional[str]) -> None:
        """Append text that continues this cell (``sep=None``: a wrapped line)."""
        if sep is None:
            self.text = _join_wrapped(self.text, text)
            self.line_y0, self.line_y1 = bbox[1], bbox[3]
        else:
            self.text += sep + text
            self.line_y0, self.line_y1 = min(self.line_y0, bbox[1]), max(self.line_y1, bbox[3])
        self.x0, self.y0 = min(self.x0, bbox[0]), min(self.y0, bbox[1])
        self.x1, self.y1 = max(self.x1, bbox[2]), max(self.y1, bbox[3])


class _Row:
    """One visual line of the page: a single text line or the cells of a table row."""

    __slots__ = ("cells", "size")

    def __init__(self, cell: _Cell, size: float):
        self.cells = [cell]
        self.size = size

    @property
    def y0(self) -> float:
        return min(c.y0 for c in self.cells)

    @property
    def y1(self) -> float:
        return max(c.y1 for c in self.cells)

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.cells)

    def text(self) -> str:
        cells = (clean_text(c.text) for c in self.cells)
        return " | ".join(c for c in cells if c)  # drops empty (&nbsp;) cells


def _join_wrapped(a: str, b: str) -> str:
    """Join two wrapped lines: "full-" + "time" -> "full-time", "infor\\xad" + "mation"."""
    a, b = a.rstrip(), b.lstrip()
    if a.endswith("\xad"):
        return a[:-1] + b
    if _HYPHEN_END.search(a) and b[:1].isalpha():   # also "Income-" + "Based"
        return a + b
    return a + " " + b


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _on_same_row(row: _Row, bbox: Sequence[float], size: float) -> bool:
    """The line sits on the row's visual line, to the right of its last cell."""
    min_overlap = max(1.0, 0.25 * min(size, row.size))
    return (_overlap(row.y0, row.y1, bbox[1], bbox[3]) > min_overlap
            and bbox[0] >= row.cells[-1].x1 - 1.0)


def _below(cell: _Cell, bbox: Sequence[float], size: float) -> bool:
    """The line is the next line of ``cell``: same column, one line pitch lower.

    The pitch is measured between line bottoms (independent of the font's box height):
    wrapped lines in the NYIT PDFs are 1.13 x the font size apart, table rows at least
    1.38 x. A footnote marker pushes its line down by ~0.3 x, so a line whose top-to-top
    pitch fits is accepted too.
    """
    if size <= 0 or _overlap(cell.x0, cell.x1, bbox[0], bbox[2]) <= 0:
        return False
    bottom = (bbox[3] - cell.line_y1) / size
    top = (bbox[1] - cell.line_y0) / size
    return 0.6 <= bottom <= 1.3 or (0.6 <= top <= 1.3 and bottom <= 1.6)


def _wraps_last_cell(row: _Row, bbox: Sequence[float], size: float) -> bool:
    """A table cell (not the first one) continues on the next line, e.g. a long description."""
    return (len(row.cells) >= 2 and _below(row.cells[-1], bbox, size)
            and bbox[0] >= row.cells[-2].x1 - 1.0)


def _rejoin_first_cell(rows: List[_Row], bbox: Sequence[float], size: float) -> bool:
    """A FIRST-column cell wrapped over several lines (each became a row) and the next cell
    jumps back up next to its first line: merge those rows into one cell, add the new one."""
    last = len(rows) - 1
    if last < 1 or len(rows[last].cells) != 1:
        return False
    first = last
    while first > max(0, last - 6):          # a cell of at most 7 lines
        upper, lower = rows[first - 1], rows[first]
        if len(upper.cells) != 1 or not _below(upper.cells[0], lower.cells[0].bbox, upper.size):
            return False
        first -= 1
        right_edge = max(r.x1 for r in rows[first:])
        if (_overlap(rows[first].y0, rows[first].y1, bbox[1], bbox[3]) > max(1.0, 0.25 * size)
                and bbox[0] >= right_edge - 1.0):
            cell = rows[first].cells[0]
            for row in rows[first + 1:]:
                cell.add(row.cells[0].text, row.cells[0].bbox, None)
            del rows[first + 1:]
            return True
    return False


def _add_line(rows: List[_Row], text: str, bbox: Sequence[float], size: float) -> None:
    """Attach one text line (in stream order) to the current row or start a new row."""
    row = rows[-1] if rows else None
    if row is not None and _on_same_row(row, bbox, size):
        last = row.cells[-1]
        gap = bbox[0] - last.x1
        # MuPDF already merges words of one text line, so a separate line on the same row
        # with more than a space between them is the next table cell
        if gap >= 0.3 * size or not last.text.strip() or not text.strip():
            row.cells.append(_Cell(text, bbox))
        else:                                            # one text line split in two
            last.add(text, bbox, "" if gap < 0.15 * size else " ")
    elif row is not None and _wraps_last_cell(row, bbox, size):
        row.cells[-1].add(text, bbox, None)
    elif row is not None and _rejoin_first_cell(rows, bbox, size):
        rows[-1].cells.append(_Cell(text, bbox))
    else:
        rows.append(_Row(_Cell(text, bbox), size))


def _rows_from_lines(lines: List[_Line]) -> List[_Row]:
    rows: List[_Row] = []
    for line in lines:
        _add_line(rows, line.text, line.bbox, line.size)
    return rows


def page_rows(page) -> List[_Row]:
    """The page's text as rows of cells, in the PDF's own (= the website's) reading order."""
    return _rows_from_lines(_page_lines(page))


def _word_continues(prev_line: str, prev_raw: str, text: str) -> bool:
    """The previous text line ended in the middle of a word ("full-" / "time ...")."""
    if " | " in prev_line or " | " in text:   # never glue table rows together
        return False
    if prev_raw.endswith("\xad"):             # soft hyphen (already removed by clean_text)
        return text[:1].isalpha()
    return bool(_HYPHEN_END.search(prev_line)) and text[:1].isalpha()


def rows_to_text(rows: List[_Row]) -> str:
    """Clean page text: one line per row, cells joined with " | "."""
    lines: List[str] = []
    prev_raw = ""
    for row in rows:
        text = _TRAILING_ARROW.sub("", row.text())
        if not text or _BACK_TO_TOP.match(text):
            continue
        if lines and _word_continues(lines[-1], prev_raw, text):
            word, _, text = text.partition(" ")
            lines[-1] += word
            text = text.strip()
        prev_raw = row.cells[-1].text.rstrip()
        if text:
            lines.append(text)
    return clean_text("\n".join(lines))


def page_text(page) -> str:
    """Clean text of one ``pymupdf`` page (handy for experiments: ``page_text(doc[210])``)."""
    return rows_to_text(page_rows(page))


# ---------------------------------------------------------------------------
# Outline from the printed headings (for PDFs without bookmarks)
# ---------------------------------------------------------------------------
def heading_toc(pages: Sequence[Tuple[List[_Line], float]]) -> List[list]:
    """Rebuild an outline ``[[level, title, page], ...]`` from the printed headings.

    ``pages`` is ``[(lines, page_width), ...]``. Every printed web page starts with a small
    bold label at the top right that names its part of the catalog ("General Information",
    "College of Engineering and Computing Sciences Curriculum") and a title in the largest
    bold font ("Computer Science, B.S."). Labels become level 1 and titles level 2 (a title
    that wraps onto a second line is joined). Tested on the 2025-2026 PDFs, which have both:
    the deepest heading equals the bookmark section on 85% of the pages; most other pages
    differ because a bookmark points one page off or marks a sub-heading.
    """
    chars: Counter = Counter()
    for lines, _width in pages:
        for line in lines:
            chars[round(line.size, 1)] += len(line.text)
    if not chars:
        return []
    body = chars.most_common(1)[0][0]
    big = Counter(round(line.size, 1) for lines, _w in pages for line in lines
                  if line.bold and line.size >= 1.3 * body)
    title_size = max((s for s, n in big.items() if n >= 3), default=None)

    toc: List[list] = []
    prev_title: Optional[Tuple[int, float]] = None   # (page, bottom) of the last title line
    for pno, (lines, width) in enumerate(pages, 1):
        for line in lines:
            title = " ".join(clean_text(line.text).split())
            is_title = (line.bold and title_size is not None
                        and abs(round(line.size, 1) - title_size) < 0.2)
            if title and is_title:
                if (prev_title is not None and prev_title[0] == pno
                        and 0 < line.bbox[3] - prev_title[1] <= 1.6 * line.size):
                    toc[-1][1] = _join_wrapped(toc[-1][1], title)  # a long title's 2nd line
                else:
                    toc.append([2, title, pno])
                prev_title = (pno, line.bbox[3])
                continue
            prev_title = None
            if title and line.bold and line.size <= 0.95 * body and line.bbox[0] >= 0.5 * width:
                toc.append([1, title, pno])
    # a label at the bottom of a page belongs with the title at the top of the next one
    for i in range(len(toc) - 1):
        if toc[i][0] == 1 and toc[i + 1][0] == 2:
            toc[i][2] = toc[i + 1][2]
    return toc


# ---------------------------------------------------------------------------
# Running headers / footers
# ---------------------------------------------------------------------------
def _margin_rows(rows: List[_Row], height: float) -> List[int]:
    top, bottom = MARGIN * height, (1.0 - MARGIN) * height
    return [i for i, r in enumerate(rows) if r.y1 <= top or r.y0 >= bottom]


def _furniture_key(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def find_running_lines(pages: List[Tuple[List[_Row], float]]) -> List[Set[int]]:
    """Row indexes to drop on every page: running headers/footers and page numbers.

    ``pages`` is ``[(rows, page_height), ...]``. A row counts only if it sits in the top or
    bottom margin; it is dropped when its text (digits ignored) shows up in the margin of
    >= 40% of the pages, or when it is a bare page number that counts up with the page.
    """
    n = len(pages)
    seen_on: Counter = Counter()
    offsets: Counter = Counter()
    for pno, (rows, height) in enumerate(pages, 1):
        keys = set()
        for i in _margin_rows(rows, height):
            text = rows[i].text()
            match = _PAGE_NUMBER.fullmatch(text)
            if match:
                offsets[int(match.group(1)) - pno] += 1
            elif text:
                keys.add(_furniture_key(text))
        seen_on.update(keys)
    min_pages = max(3, REPEAT_SHARE * n)
    repeated = {k for k, c in seen_on.items() if c >= min_pages}
    offset: Optional[int] = None
    if offsets:
        best, count = offsets.most_common(1)[0]
        offset = best if count >= min_pages else None

    drop: List[Set[int]] = []
    for pno, (rows, height) in enumerate(pages, 1):
        found = set()
        for i in _margin_rows(rows, height):
            text = rows[i].text()
            match = _PAGE_NUMBER.fullmatch(text)
            if match:
                if offset is not None and int(match.group(1)) - pno == offset:
                    found.add(i)
            elif text and _furniture_key(text) in repeated:
                found.add(i)
        drop.append(found)
    return drop


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def word_count_clean(text: str) -> int:
    """Whitespace words, not counting the " | " table-cell separators."""
    return sum(1 for w in text.split() if w != "|") if text else 0


def extract_pdf(path: Path, kind: str, edition: str, url: str) -> List[PdfPageRecord]:
    """One ``PdfPageRecord`` per page of a catalog PDF (empty pages included)."""
    return _extract(Path(path), kind, edition, url)[0]


def _extract(path: Path, kind: str, edition: str, url: str) -> Tuple[List[PdfPageRecord], dict]:
    """Records plus per-PDF numbers for pdf_summary.json."""
    if pymupdf is None or not hasattr(pymupdf, "open"):
        raise RuntimeError("PyMuPDF is needed for the pdf step: pip install pymupdf")
    doc = pymupdf.open(str(path))
    try:
        page_count = doc.page_count
        toc = doc.get_toc(simple=True)
        title = (doc.metadata or {}).get("title") or ""
        pages: List[Tuple[List[_Line], float, float]] = []
        words_raw = 0
        for page in doc:
            words_raw += len(page.get_text().split())  # the proposal's counting method
            try:
                lines = _page_lines(page)
            except Exception as exc:  # a broken page must not lose the whole PDF
                log.warning("%s page %d: text extraction failed (%s)", path.name,
                            page.number + 1, exc)
                lines = []
            pages.append((lines, page.rect.width, page.rect.height))
    finally:
        doc.close()

    rows_by_page = [_rows_from_lines(lines) for lines, _w, _h in pages]
    drop = find_running_lines([(rows, h) for rows, (_l, _w, h) in zip(rows_by_page, pages)])
    if toc:
        outline, source = toc, "bookmarks"
    else:
        outline = heading_toc([(lines, width) for lines, width, _h in pages])
        source = "headings" if outline else "none"
    paths = toc_paths(outline, page_count)

    records = []
    for pno, (rows, skip, toc_path) in enumerate(zip(rows_by_page, drop, paths), 1):
        text = rows_to_text([row for i, row in enumerate(rows) if i not in skip])
        records.append(PdfPageRecord(
            doc_id=f"pdf-{edition}-{kind}-p{pno:04d}",
            edition=edition,
            catalog=kind,
            page=pno,
            page_count=page_count,
            url=f"{url}#page={pno}",
            toc_path=toc_path,
            section=toc_path[-1] if toc_path else None,
            text=text,
            word_count=word_count_clean(text),
            pdf_file=path.name,
        ))

    empty = [r.page for r in records if r.word_count == 0]
    near_empty = [r.page for r in records if 0 < r.word_count < NEAR_EMPTY_WORDS]
    removed = sum(len(s) for s in drop)
    if empty:
        log.warning("%s: %d empty pages (kept as records): %s", path.name, len(empty), empty)
    if near_empty:
        log.info("%s: pages with fewer than %d words: %s", path.name, NEAR_EMPTY_WORDS, near_empty)
    if removed:
        log.info("%s: removed %d running header/footer/page-number lines", path.name, removed)
    if source != "bookmarks":
        log.info("%s has no bookmarks: section paths rebuilt from %d printed headings",
                 path.name, len(outline))
    info = {
        "edition": edition,
        "catalog": kind,
        "file": path.name,
        "url": url,
        "pages": page_count,
        "words_clean": sum(r.word_count for r in records),
        "words_raw": words_raw,
        "empty_pages": len(empty),
        "empty_page_numbers": empty,
        "toc_entries": len(toc),
        "toc_source": source,
        "outline_entries": len(outline),
        "header_footer_lines_removed": removed,
        "pdf_title": title,
        "bytes": path.stat().st_size,
        "sha1": file_sha1(path),
    }
    return records, info


def file_sha1(path: Path) -> str:
    """SHA-1 of a file's bytes (read in 1 MB chunks)."""
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_file_sha1 = file_sha1  # old name, kept for existing callers


# ---------------------------------------------------------------------------
# The `pdf` command
# ---------------------------------------------------------------------------
def _totals(pdfs: List[dict]) -> Dict[str, int]:
    return {
        "pdfs": len(pdfs),
        "pages": sum(p["pages"] for p in pdfs),
        "words_clean": sum(p["words_clean"] for p in pdfs),
        "words_raw": sum(p["words_raw"] for p in pdfs),
        "empty_pages": sum(p["empty_pages"] for p in pdfs),
    }


def run_pdf(settings: Settings, editions: List[str], kinds: List[str]) -> dict:
    """Download the requested PDFs, extract every catalog PDF on disk, write the outputs."""
    started = time.time()
    if pymupdf is None or not hasattr(pymupdf, "open"):
        log.error("PyMuPDF is not installed: run `pip install pymupdf` (see requirements.txt)")
        return {"failed": [{"error": "PyMuPDF is not installed"}]}
    settings.ensure_dirs()
    editions = dedupe_keep_order(editions)
    kinds = dedupe_keep_order(kinds)

    downloads = download_catalog_pdfs(settings, editions, kinds)
    failed = [{k: d[k] for k in ("edition", "catalog", "url", "error")}
              for d in downloads if d["status"] == "failed"]

    records: List[PdfPageRecord] = []
    pdfs: List[dict] = []
    for edition, kind, path in local_catalog_pdfs(settings):
        url = config.pdf_url(kind, edition)
        try:
            recs, info = _extract(path, kind, edition, url)
        except Exception as exc:  # corrupt file: report it, keep the others
            log.error("could not read %s: %s", path.name, exc)
            failed.append({"edition": edition, "catalog": kind, "url": url,
                           "error": f"extraction failed: {type(exc).__name__}: {exc}"})
            continue
        log.info("%s: %d pages, %s words (raw %s), section paths from %s",
                 path.name, info["pages"], f"{info['words_clean']:,}",
                 f"{info['words_raw']:,}", info["toc_source"])
        records.extend(recs)
        pdfs.append(info)

    records.sort(key=lambda r: (r.edition, r.catalog, r.page))
    out_path = settings.processed_dir / PDF_PAGES_FILE
    write_jsonl(out_path, records)

    by_edition = {ed: _totals([p for p in pdfs if p["edition"] == ed])
                  for ed in sorted({p["edition"] for p in pdfs})}
    summary = {
        "generated_at": now_iso(),
        "seconds": round(time.time() - started, 1),
        "requested": {"editions": editions, "kinds": kinds},
        "downloads": downloads,
        "pdfs": pdfs,
        "by_edition": by_edition,
        "totals": _totals(pdfs),
        "records": len(records),
        "pymupdf_version": getattr(pymupdf, "VersionBind", ""),
        "failed": failed,
    }
    write_json(settings.processed_dir / PDF_SUMMARY_FILE, summary)

    log.info("wrote %d PDF pages from %d PDFs to %s", len(records), len(pdfs), out_path)
    _log_proposal_check(pdfs)
    for item in failed:
        log.error("FAILED %s %s: %s", item.get("edition", ""), item.get("catalog", ""),
                  item["error"])
    return summary


def _log_proposal_check(pdfs: List[dict]) -> None:
    """Compare the 2025-2026 undergraduate + graduate PDFs with the proposal's numbers."""
    base = [p for p in pdfs if p["edition"] == "2025-2026"
            and p["catalog"] in ("undergraduate", "graduate")]
    if len(base) == 2:
        t = _totals(base)
        log.info("2025-2026 undergraduate + graduate: %d pages, %s raw words "
                 "(proposal: %d pages, ~%dK words)", t["pages"], f"{t['words_raw']:,}",
                 config.PROPOSAL_TARGETS["pdf_pages_2025_2026"],
                 int(config.PROPOSAL_TARGETS["pdf_words_2025_2026"]) // 1000)
