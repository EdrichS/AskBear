"""Turn one cached catalog page into a PageRecord and its SectionRecords.

The core is a small DOM walker (:func:`html_to_blocks`) that converts the main
content of a page into a flat list of :class:`Block` objects: headings,
paragraphs, list items and table rows, each already cleaned and on one line.
Everything else is built from those blocks:

* :func:`blocks_to_text`  -> the clean page text (``PageRecord.text``)
* :func:`split_sections`  -> heading-delimited passages (``SectionRecord``); very
  long ones are split again into parts of ~150-400 words
* :func:`classify_page` and the metadata helpers -> page type, school, program,
  degree, level, catalog year ... from the URL, breadcrumbs, <h1> and <title>.

The walker, the sectioning and the link/section helpers (:func:`walk_content`,
:func:`extract_links`, :func:`section_records`) are shared with site_parser.py,
which parses the other NYIT websites; the catalog-specific rules live here.

Why a DOM walk instead of ``get_text()``: ``get_text()`` glues words from adjacent
blocks together ("RequirementsFirst Year"), turns footnote markers into stray
digits ("Composition1") and loses the table structure. The walker keeps inline
text on one line, breaks lines at block elements, renders tables row by row
("CSCI 260 | Data Structures | 3") and drops navigation/noise.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, quote, urlsplit

from bs4.element import (CData, Comment, Declaration, Doctype, NavigableString,
                         ProcessingInstruction, Tag)

from . import config
from .models import PAGE_TYPES, PageRecord, SectionRecord
from .urls import normalize_url, path_segments, url_host
from .utils import clean_text, make_soup, sha1, short_hash, word_count

log = logging.getLogger("askbear.html_parser")

# Passage sizes (words). Sections longer than MAX_SECTION_WORDS are split into
# parts of roughly PART_MIN_WORDS..PART_MAX_WORDS.
MAX_SECTION_WORDS = 450
PART_MIN_WORDS = 150
PART_MAX_WORDS = 400

# ---------------------------------------------------------------------------
# HTML vocabulary
# ---------------------------------------------------------------------------
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Elements whose content is never page text.
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "object", "embed", "svg",
    "canvas", "form", "select", "option", "optgroup", "button", "input", "textarea",
    "img", "picture", "video", "audio", "source", "track", "map", "area", "head",
    "meta", "link", "title", "base", "nav", "header", "footer",
}

# Elements that start and end a line of text.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "aside", "main", "blockquote", "address",
    "center", "pre", "figure", "figcaption", "dl", "dt", "dd", "details", "summary",
    "fieldset", "legend", "caption", "body", "html", "hgroup", "tbody", "thead",
    "tfoot", "tr", "td", "th", "li", "ul", "ol",
}

_BOLD_TAGS = {"strong", "b"}
_STRING_SKIP = (Comment, Doctype, CData, ProcessingInstruction, Declaration)

# public names for site_parser.py
BLOCK_TAGS = frozenset(_BLOCK_TAGS)
NON_TEXT_STRINGS = _STRING_SKIP     # bs4 string types that are not page text (comments, ...)

# Page chrome that must never leak into the text when we fall back to <main>/<body>.
_CHROME_CLASSES = {
    "Hero", "Breadcrumbs", "Main__subnav", "Subnav", "Header", "Footer", "Nav",
    "skip-link", "b2t", "catalogs",
}
_CHROME_IDS = {"weblog"}                 # banner of the /print/ pages

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# Table rows that the site's CSS keeps collapsed until a course code is clicked: the
# "Academics > Degrees" curriculum template (/curriculum/<slug>, also used on www.nyit.edu)
# puts each course's full description in a ``tr.hidecd`` row under the course. Those
# descriptions repeat the course directory (courses.jsonl) on every curriculum page that
# lists the course, so they are not page text.
_COLLAPSED_ROW_CLASSES = {"hidecd"}
_BOLD_STYLE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.I)
_WS = re.compile(r"\s+")
_WORD_CHAR = re.compile(r"\w")
_LAYOUT_ROW = re.compile(r"^layout-\d+-\d+-of-12$")
_TOGGLE_NOISE = {"", "more", "less", "read more", "show more", "show less", "+", "-", "expand"}
_BACK_TO_TOP = re.compile(r"^\s*(back to top|return to top|top of page|\^?\s*top)\s*$", re.I)
# a.Button links whose text is a generic call to action ("Go To Entry", "Apply Online ...").
# Buttons that name something ("College of Arts and Sciences") are kept: on hub pages
# such as /schools_and_colleges they are the only content.
_CALL_TO_ACTION = re.compile(
    r"^(go to entry|more|less|(read|learn|view|see|find out|click|download|request|register|visit|"
    r"contact|apply|complete|obtain|browse|explore|start|submit|schedule)\b.*|more information\b.*|"
    r"additional info\b.*|how to\b.*)$", re.I)
_TRAILING_ARROW = re.compile(r"\s+[>»›]+$")        # "More About Admissions >"
_CELL_LEADING_SEP = re.compile(r"^[-–—|:•]+\s+")
_FOOTNOTE_SUP = re.compile(r"^[\[(]?([0-9]{1,3}|[*†‡§]+|[a-zA-Z])[\])]?$")
_ORDINAL_SUP = re.compile(r"^(st|nd|rd|th)$", re.I)
_GROUP_LABEL = re.compile(r"^(credits?|credit hours?|hours?|semester hours?|sh)\s*:?$", re.I)
# "Total Program Credits = 66", "TOTAL CREDITS: 98": a summary line styled as a heading
_TOTAL_LINE = re.compile(r"^total\b.*\d", re.I)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
@dataclass
class Block:
    """One line-level unit of page content.

    kind   : "heading" | "text" | "list_item" | "table_row"
    text   : clean text (a "text" block may contain newlines from <br>)
    level  : heading level 1-6; nesting depth for list items; 0 otherwise
    anchor : an id/name that a URL fragment can jump to: the element's own id, or
             an empty ``<a name="...">`` marker placed right before this block
    marker : True when ``anchor`` came from such an empty marker element
    strong : the whole line is bold (a "pseudo-heading" such as "First Year"), or a
             table group-header row such as "Foundations | Credits:"
    table  : 1-based number of the table a row belongs to (0 = not a table row)
    """

    kind: str
    text: str
    level: int = 0
    anchor: str = ""
    marker: bool = False
    strong: bool = False
    table: int = 0

    @property
    def words(self) -> int:
        return word_count(self.text)


def _is_hidden(el: Tag) -> bool:
    if el.has_attr("hidden"):
        return True
    style = el.get("style")
    return bool(style) and bool(_HIDDEN_STYLE.search(style))


def _is_bold_line(line: Tuple[str, int, int]) -> bool:
    """A short line whose letters are all bold, e.g. "First Year" or "Objective"."""
    text, bold, plain = line
    return bold > 0 and plain == 0 and word_count(text) <= 12 and \
        len(re.sub(r"[^A-Za-z]", "", text)) >= 3


def _classes(el: Tag) -> List[str]:
    cls = el.get("class") or []
    return cls if isinstance(cls, list) else str(cls).split()


def _collapse(text: str) -> str:
    return _WS.sub(" ", text)


def _document_ids(el: Tag) -> Set[str]:
    """ids/names that occur exactly once in the whole document (safe fragment targets)."""
    root = el
    while root.parent is not None:
        root = root.parent
    counts: Dict[str, int] = {}
    for tag in root.find_all(True):
        for key in ("id", "name"):
            if key == "name" and tag.name != "a":
                continue
            value = tag.get(key)
            if isinstance(value, str) and value.strip():
                counts[value.strip()] = counts.get(value.strip(), 0) + 1
    return {k for k, n in counts.items() if n == 1}


def _in_page_tocs(root: Tag) -> Set[int]:
    """Containers that only hold links to anchors on the same page ("click a topic").

    They duplicate the headings below them, so as a passage they would only attract
    queries away from the real sections. Returns the ``id()`` of each such element.
    """
    frag_links = [a for a in root.find_all("a", href=True)
                  if a["href"].strip().startswith("#") and len(a["href"].strip()) > 1]
    if len(frag_links) < 4:
        return set()
    found: Set[int] = set()
    seen: Set[int] = set()
    for a in frag_links:
        for anc in a.parents:
            if anc is root or anc is None:
                break
            if id(anc) in seen or anc.name not in ("div", "ul", "ol", "td", "table", "nav"):
                continue
            seen.add(id(anc))
            links = anc.find_all("a", href=True)
            if len(links) < 4 or not all(x["href"].strip().startswith("#") for x in links):
                continue
            link_chars = sum(len(x.get_text(strip=True)) for x in links)
            if len(anc.get_text(strip=True)) - link_chars <= 10:
                found.add(id(anc))
    return found


class _Walker:
    """Depth-first walk over a content element that emits :class:`Block` objects."""

    def __init__(self, unique_ids: Set[str], skip: Optional[Set[int]] = None):
        self.ids = unique_ids
        self.skip = skip or set()
        self.blocks: List[Block] = []
        self.tables = 0
        self.last_heading_level = 0
        # state of the block being built: finished lines (<br>) + the current line
        self.lines: List[Tuple[str, int, int]] = []   # (text, bold chars, plain chars)
        self.parts: List[str] = []
        self.bold_chars = 0
        self.plain_chars = 0
        self.bold_depth = 0
        self.anchor = ""
        self.anchor_marker = False
        self.bullet = ""          # "- " / "3. " waiting for the first line of a list item
        self.list_depth = 0
        self.in_li = 0

    # ------------------------------------------------------------------ lines
    def _line_started(self) -> bool:
        return any(p.strip() for p in self.parts) or any(t.strip() for t, _, _ in self.lines)

    def add_text(self, text: str) -> None:
        if not text:
            return
        text = _collapse(text)   # source newlines are just whitespace; only <br> breaks lines
        self.parts.append(text)
        n = len(_WORD_CHAR.findall(text))
        if self.bold_depth:
            self.bold_chars += n
        else:
            self.plain_chars += n

    def line_break(self) -> None:
        self.lines.append(("".join(self.parts), self.bold_chars, self.plain_chars))
        self.parts, self.bold_chars, self.plain_chars = [], 0, 0

    def note_anchor(self, anchor: str, empty: bool) -> None:
        """Remember an id/name for the block being built (or the next one)."""
        if not anchor:
            return
        if self._line_started():
            if not self.anchor:
                self.anchor, self.anchor_marker = anchor, False
        elif not self.anchor:
            self.anchor, self.anchor_marker = anchor, empty

    def flush(self) -> None:
        """Turn the buffered lines into blocks.

        A short bold-only line ("<strong>Objective</strong><br>The policy ...") is
        split off as its own block marked ``strong``: it is a pseudo-heading.
        """
        self.line_break()
        lines = [(_TRAILING_ARROW.sub("", clean_text(t).replace("\n", " ")), b, p)
                 for t, b, p in self.lines]
        self.lines = []
        lines = [line for line in lines if line[0]]
        if not lines:
            return  # keep a pending anchor for the next block
        groups: List[List[Tuple[str, int, int]]] = []
        for line in lines:
            lone = not self.in_li and _is_bold_line(line)
            if not groups or lone or (not self.in_li and _is_bold_line(groups[-1][-1])):
                groups.append([line])
            else:
                groups[-1].append(line)
        kind = "list_item" if self.in_li else "text"
        level = self.list_depth if self.in_li else 0
        for group in groups:
            text = "\n".join(t for t, _, _ in group)
            if self.bullet:
                text = self.bullet + text
                self.bullet = ""
            strong = all(b > 0 and p == 0 for _, b, p in group)
            self.blocks.append(Block(kind, text, level=level, anchor=self.anchor,
                                     marker=self.anchor_marker and bool(self.anchor), strong=strong))
            self.anchor, self.anchor_marker = "", False

    def emit(self, block: Block) -> None:
        self.flush()
        if not block.anchor and self.anchor:
            block.anchor, block.marker = self.anchor, self.anchor_marker
        self.anchor, self.anchor_marker = "", False
        self.blocks.append(block)

    # ------------------------------------------------------------------ walk
    def walk(self, node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, _STRING_SKIP):
                    self.add_text(str(child))
            elif isinstance(child, Tag):
                self.visit(child)

    def _own_anchor(self, el: Tag) -> str:
        for key in ("id", "name"):
            if key == "name" and el.name != "a":
                continue
            value = el.get(key)
            if isinstance(value, str) and value.strip() in self.ids:
                return value.strip()
        return ""

    def visit(self, el: Tag) -> None:
        name = el.name
        if name in _SKIP_TAGS or id(el) in self.skip or _is_hidden(el):
            return
        cls = _classes(el)
        if (cls and _CHROME_CLASSES.intersection(cls)) or el.get("id") in _CHROME_IDS:
            return
        anchor = self._own_anchor(el)

        if name in _HEADING_LEVELS:
            self.heading(el, _HEADING_LEVELS[name], anchor)
            return
        if name == "a":
            text = el.get_text(" ", strip=True)
            if _BACK_TO_TOP.match(text or "x") or ("Button" in cls and _CALL_TO_ACTION.match(text)):
                return  # "Back to Top" links and call-to-action buttons
            if "simple-toggle" in cls:
                if text.lower() not in _TOGGLE_NOISE:
                    level = min(6, self.last_heading_level + 1) if self.last_heading_level else 2
                    self.heading(el, level, anchor, toggle=True)
                return
        if name == "br":
            self.line_break()
            return
        if name == "hr":
            self.flush()
            return
        if name == "sup":
            self.sup(el)
            return
        if name == "table":
            self.table(el, anchor)
            return
        if name in ("ul", "ol"):
            self.list(el, anchor)
            return
        if name == "li":  # stray <li> outside a list
            self.list_item(el, "- ", anchor)
            return
        if name == "div" and any(_LAYOUT_ROW.match(c) for c in cls) and self._layout_row(el, anchor):
            return
        if name in _BLOCK_TAGS:
            self.flush()
            self.note_anchor(anchor, empty=not el.get_text(strip=True))
            self.walk(el)
            self.flush()
            return
        # inline element
        if anchor:
            self.note_anchor(anchor, empty=not el.get_text(strip=True))
        bold = name in _BOLD_TAGS or bool(_BOLD_STYLE.search(el.get("style") or ""))
        if bold:
            self.bold_depth += 1
        self.walk(el)
        if bold:
            self.bold_depth -= 1

    # ------------------------------------------------------------------ elements
    def heading(self, el: Tag, level: int, anchor: str, toggle: bool = False) -> None:
        self.flush()
        sub = _Walker(self.ids)
        sub.walk(el)
        sub.flush()
        text = clean_text(" ".join(b.text.replace("\n", " ") for b in sub.blocks))
        inner = next((b.anchor for b in sub.blocks if b.anchor), "") or sub.anchor
        anchor = anchor or inner
        if not text:
            self.note_anchor(anchor, empty=True)
            return
        if not toggle and (text[0] in "*†‡([" or word_count(text) > 25
                           or _TOTAL_LINE.match(text)):
            # footnotes, totals and sentences formatted as headings: keep them as text
            self.emit(Block("text", text, anchor=anchor))
            return
        if anchor == "" and self.anchor and not self._line_started():
            anchor = self.anchor
        self.anchor, self.anchor_marker = "", False
        self.blocks.append(Block("heading", text, level=level, anchor=anchor))
        if not toggle:
            self.last_heading_level = level

    def sup(self, el: Tag) -> None:
        text = clean_text(el.get_text(" ", strip=True))
        if not text:
            if el.get_text():
                self.add_text(" ")                     # <sup> </sup> still separates words
            return
        if _ORDINAL_SUP.match(text):
            self.add_text(text)                        # 1<sup>st</sup> -> 1st
        elif _FOOTNOTE_SUP.match(text):
            core = text.strip("[]()")
            self.parts.append(f" [{core}]")            # footnote marker -> " [1]"
        else:
            # ®, TM, ... or whole sentences set in superscript: walked like any inline
            # element, so the spaces at its edges survive ("<sup>contributed by </sup>
            # <strong><sup>Sophie</sup></strong>" -> "contributed by Sophie", not "bySophie")
            self.walk(el)

    def list(self, el: Tag, anchor: str) -> None:
        self.flush()
        self.note_anchor(anchor, empty=False)
        ordered = el.name == "ol"
        try:
            number = int(el.get("start", 1)) if ordered else 1
        except (TypeError, ValueError):
            number = 1
        saved_bullet, self.bullet = self.bullet, ""
        self.list_depth += 1
        for child in el.children:
            if isinstance(child, Tag):
                if child.name == "li":
                    if _is_hidden(child):
                        continue
                    self.list_item(child, f"{number}. " if ordered else "- ", self._own_anchor(child))
                    number += 1
                else:
                    self.visit(child)
            elif isinstance(child, NavigableString) and not isinstance(child, _STRING_SKIP):
                self.add_text(str(child))
        self.flush()
        self.list_depth -= 1
        self.bullet = saved_bullet

    def list_item(self, li: Tag, bullet: str, anchor: str) -> None:
        self.flush()
        self.note_anchor(anchor, empty=False)
        self.in_li += 1
        self.bullet = bullet
        self.walk(li)
        self.flush()
        self.bullet = ""
        self.in_li -= 1

    # ------------------------------------------------------------------ tables
    def _cell(self, cell: Tag) -> Tuple[str, bool, bool, str]:
        """Flatten one cell to a single line -> (text, bold, only_headings, anchor)."""
        sub = _Walker(self.ids, self.skip)
        sub.walk(cell)
        sub.flush()
        pieces: List[str] = []
        items: List[str] = []
        for b in sub.blocks:
            text = b.text.replace("\n", " ")
            if b.kind == "list_item":
                items.append(re.sub(r"^(- |\d+\. )", "", text))
                continue
            if items:
                pieces.append("; ".join(items))
                items = []
            pieces.append(text)
        if items:
            pieces.append("; ".join(items))
        text = clean_text(" ".join(pieces))
        text = _CELL_LEADING_SEP.sub("", text)
        bold = bool(sub.blocks) and (cell.name == "th" or all(
            b.strong or b.kind == "heading" for b in sub.blocks))
        only_headings = bool(sub.blocks) and all(b.kind == "heading" for b in sub.blocks)
        anchor = next((b.anchor for b in sub.blocks if b.anchor), "") or sub.anchor
        return text, bold, only_headings, anchor

    def table(self, el: Tag, anchor: str) -> None:
        self.flush()
        self.note_anchor(anchor, empty=False)
        self.tables += 1
        number = self.tables
        for tr in el.find_all("tr"):
            if tr.find_parent("table") is not el or _is_hidden(tr) \
                    or _COLLAPSED_ROW_CLASSES.intersection(_classes(tr)):
                continue
            cells = [c for c in tr.find_all(["td", "th"], recursive=False) if not _is_hidden(c)]
            in_head = tr.parent is not None and tr.parent.name == "thead"
            self.row(cells, number, in_head, self._own_anchor(tr))
        self.flush()

    def row(self, cells: Sequence[Tag], table_no: int, in_head: bool, anchor: str) -> None:
        filled = [c for c in cells if c.get_text(strip=True)]
        self.note_anchor(anchor, empty=False)
        if not filled:
            for c in cells:  # an empty row can still carry an anchor
                for a in c.find_all(True):
                    self.note_anchor(self._own_anchor(a), empty=True)
            return
        if len(filled) == 1:
            # one-cell rows are boxes (notes, footnotes, sub-headings): walk them
            # like normal content so their paragraphs, lists and headings survive
            self.flush()
            first = len(self.blocks)
            self.visit(filled[0])
            self.flush()
            for b in self.blocks[first:]:
                if b.kind != "heading":
                    b.table = b.table or table_no
            return
        rendered = [self._cell(c) for c in filled]
        rendered = [r for r in rendered if r[0]]
        if not rendered:
            return
        row_anchor = next((r[3] for r in rendered if r[3]), "")
        if all(r[2] for r in rendered):
            # a row made only of headings (e.g. the same title over two columns)
            texts = list(dict.fromkeys(r[0] for r in rendered))
            self.emit(Block("heading", " / ".join(texts), level=4, anchor=row_anchor))
            self.last_heading_level = 4
            return
        texts = [r[0] for r in rendered]
        first_bold = rendered[0][1]
        others_ok = all(r[1] or _GROUP_LABEL.match(r[0]) for r in rendered[1:])
        header = in_head or all(c.name == "th" for c in filled) or (first_bold and others_ok)
        self.emit(Block("table_row", " | ".join(texts), anchor=row_anchor,
                        strong=header, table=table_no))

    def _layout_row(self, el: Tag, anchor: str) -> bool:
        """``div.layout-4-8-of-12`` (label | value columns, e.g. the academic calendar)."""
        cols = [c for c in el.find_all("div", recursive=False) if "column" in _classes(c)]
        if len(cols) < 2 or any(c.find(["h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"])
                                for c in cols):
            return False
        self.flush()
        self.row(cols, 0, False, anchor)
        return True


def walk_content(element: Optional[Tag]) -> Tuple[List[Block], int]:
    """Walk a content element -> (blocks, number of tables rendered).

    Any content root works (site_parser passes the <main>/<article> of other sites,
    after deleting their site-specific noise). Noise is skipped, not deleted, so
    ``element`` itself is left untouched.
    """
    if element is None:
        return [], 0
    walker = _Walker(_document_ids(element), _in_page_tocs(element))
    walker.visit(element)
    walker.flush()
    return walker.blocks, walker.tables


_walk_content = walk_content  # old name, kept for existing callers


def html_to_blocks(element: Tag) -> List[Block]:
    """Convert the main-content element of a page into a list of :class:`Block`.

    Pass the content element (``content_root(soup)``), not the whole document: on this
    site lxml puts <main> under <head>, which the walker skips. Noise is skipped, not
    deleted, so ``element`` is left untouched.
    """
    return walk_content(element)[0]


def blocks_to_text(blocks: Sequence[Block]) -> str:
    """Join blocks into clean text: one block per line, a blank line before headings."""
    lines: List[str] = []
    for b in blocks:
        if b.kind == "heading" and lines:
            lines.append("")
        lines.append(b.text)
    return clean_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
@dataclass
class Section:
    """A heading-delimited group of blocks (internal shape of :func:`split_sections`)."""

    heading: str
    level: int
    heading_path: List[str]
    anchor: str
    blocks: List[Block] = field(default_factory=list)
    part: int = 1

    @property
    def text(self) -> str:
        return blocks_to_text(self.blocks)

    @property
    def words(self) -> int:
        return sum(b.words for b in self.blocks)


def _raw_sections(blocks: Sequence[Block]) -> List[Section]:
    sections = [Section("", 0, [], "")]
    stack: List[Tuple[int, str]] = []
    for b in blocks:
        if b.kind == "heading":
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            stack.append((b.level, b.text))
            sections.append(Section(b.text, b.level, [h for _, h in stack], b.anchor))
        else:
            sections[-1].blocks.append(b)
    return sections


def _attach_markers(sections: List[Section]) -> None:
    """``<a name="x"></a>`` placed shortly *before* a heading marks that heading.

    On program pages the pattern is ``<a name="admissions"></a> <p>short note</p>
    <h2>Admission Requirements</h2>``: the anchor (and the short note) belong to
    the next section, not to the end of the previous one.
    """
    for prev, nxt in zip(sections, sections[1:]):
        tail = prev.blocks[-3:]
        offset = len(prev.blocks) - len(tail)
        for i, b in enumerate(tail):
            if not b.marker or b.kind == "table_row" or offset + i == 0:
                continue  # a marker opening its own section already belongs there
            moved = tail[i:]
            if sum(x.words for x in moved) > 40:
                continue
            del prev.blocks[len(prev.blocks) - len(moved):]
            nxt.blocks[:0] = moved
            if not nxt.anchor:
                nxt.anchor = b.anchor
            break


def _split_oversized(block: Block, limit: int) -> List[Block]:
    """Split one very long block at line breaks, then sentence ends."""
    if block.words <= limit or block.kind in ("heading", "table_row"):
        return [block]
    units = [ln for ln in block.text.split("\n") if ln.strip()]
    if len(units) == 1:
        units = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"“])", block.text)
    out: List[Block] = []
    cur: List[str] = []
    cur_words = 0
    for unit in units:
        w = word_count(unit)
        if cur and cur_words + w > limit:
            out.append(Block(block.kind, " ".join(cur), block.level, "", False, False, block.table))
            cur, cur_words = [], 0
        cur.append(unit)
        cur_words += w
    if cur:
        out.append(Block(block.kind, " ".join(cur), block.level, "", False, False, block.table))
    if out:
        out[0].anchor, out[0].marker, out[0].strong = block.anchor, block.marker, block.strong
    return out


def _is_break_point(b: Block) -> bool:
    """Preferred places to start a new part: pseudo-headings and table group headers."""
    if not b.strong:
        return False
    if b.kind == "table_row":
        return True
    letters = re.sub(r"[^A-Za-z]", "", b.text)
    return b.kind == "text" and len(letters) >= 3 and b.words <= 12 and letters.lower() not in ("or", "and")


# Lines that close the group above them and should not start a new part.
_STICKY = re.compile(r"^(total\b|\[\d+\]|\*)", re.I)


def _soft_break_ok(prev: Block, b: Block) -> bool:
    """May a part end after ``prev`` and the next one start at ``b``?"""
    if prev.text.rstrip().endswith(":"):     # "... the ability to:" + the list
        return False
    if _STICKY.match(b.text):                # "Total: 9 Credits", "[1] footnote"
        return False
    return not _is_break_point(prev)         # keep a pseudo-heading with its body


def _continues_run(prev: Block, b: Block) -> bool:
    """``b`` continues the table or list that ``prev`` belongs to."""
    if b.table and b.table == prev.table:
        return True
    return b.kind == "list_item" and prev.kind == "list_item"


def _group_blocks(blocks: Sequence[Block], target: float, min_words: int,
                  part_max: int, max_words: int) -> List[List[Block]]:
    """Greedy grouping into parts of about ``target`` words (hard cap ``part_max``).

    Preferred cut points: pseudo-headings and table group headers, then paragraph
    boundaries; tables and lists are only cut inside when they run long, and table
    rows are never split.
    """
    groups: List[List[Block]] = []
    cur: List[Block] = []
    cur_w = 0
    for b in blocks:
        w = b.words
        if cur:
            prev = cur[-1]
            if cur_w + w > part_max and cur_w >= min_words * 0.5:
                start_new = True
            elif _is_break_point(b) and cur_w >= target * 0.6 and _soft_break_ok(prev, b):
                start_new = True
            elif cur_w >= target and _soft_break_ok(prev, b):
                start_new = not _continues_run(prev, b) or cur_w >= target * 1.25
            else:
                start_new = False
            if start_new:
                carry: List[Block] = []
                while len(cur) > 1 and _is_break_point(cur[-1]):
                    carry.insert(0, cur.pop())   # never end a part with a pseudo-heading
                groups.append(cur)
                cur = carry
                cur_w = sum(x.words for x in cur)
        cur.append(b)
        cur_w += w
    if cur:
        groups.append(cur)
    if len(groups) > 1:
        tail_w = sum(b.words for b in groups[-1])
        if tail_w < min_words * 0.6 and tail_w + sum(b.words for b in groups[-2]) <= max_words:
            groups[-2].extend(groups.pop())      # fold a small tail into the previous part
    return groups


def _split_long(sec: Section, max_words: int, min_words: int, part_max: int) -> List[Section]:
    if sec.words <= max_words:
        return [sec]
    blocks: List[Block] = []
    for b in sec.blocks:
        blocks.extend(_split_oversized(b, part_max))
    n_parts = max(2, math.ceil(sec.words / part_max))
    target = max(min_words, min(part_max, sec.words / n_parts))
    groups = _group_blocks(blocks, target, min_words, part_max, max_words)

    parts: List[Section] = []
    last_anchor = ""
    seen: List[Block] = []
    for k, group in enumerate(groups, 1):
        body = list(group)
        anchor = sec.anchor
        if k > 1:
            # the closest anchor at or above the start of this part
            anchor = group[0].anchor or last_anchor or sec.anchor
            first = group[0]
            prev = seen[-1] if seen else None
            if (first.kind == "table_row" and not first.strong and first.table
                    and prev is not None and prev.table == first.table):
                header = next((x for x in reversed(seen)
                               if x.kind == "table_row" and x.strong and x.table == first.table), None)
                if header is not None:
                    body.insert(0, Block("table_row", header.text, strong=True, table=header.table))
        heading = sec.heading
        if k > 1:
            heading = f"{sec.heading} (part {k})" if sec.heading else f"(part {k})"
        parts.append(Section(heading, sec.level, list(sec.heading_path), anchor, body, k))
        for x in group:
            if x.anchor:
                last_anchor = x.anchor
        seen.extend(group)
    return parts


def split_sections(blocks: Sequence[Block], max_words: int = MAX_SECTION_WORDS,
                   min_words: int = PART_MIN_WORDS, part_max: int = PART_MAX_WORDS) -> List[Section]:
    """Group blocks into sections at headings, drop empty ones, split long ones.

    The intro before the first heading is a section with heading "". A section
    without body text is dropped, but its heading stays in the ``heading_path`` of
    the sections below it.
    """
    sections = _raw_sections(blocks)
    _attach_markers(sections)
    out: List[Section] = []
    for sec in sections:
        if not any(b.text.strip() for b in sec.blocks):
            continue
        out.extend(_split_long(sec, max_words, min_words, part_max))
    return out


# ---------------------------------------------------------------------------
# Page metadata
# ---------------------------------------------------------------------------
_YEAR_AFTER = re.compile(r"Catalogs?\D{0,12}?(20\d\d)\s*[–—-]\s*(20\d\d)", re.I)
_YEAR_BEFORE = re.compile(r"(20\d\d)\s*[–—-]\s*(20\d\d)\D{0,60}Catalog", re.I)
_SCHOOL_NAMES = set(config.SCHOOL_BY_PATH.values())


def _text_of(el: Optional[Tag]) -> str:
    return clean_text(el.get_text(" ", strip=True)).replace("\n", " ") if el is not None else ""


def extract_breadcrumbs(soup) -> List[str]:
    """``div.Breadcrumbs`` -> ["New York Tech Catalog 2026–2027", "Schools and Colleges", ...]."""
    box = soup.find("div", class_="Breadcrumbs")
    if box is None:
        return []
    crumbs: List[str] = []
    for child in box.children:
        if isinstance(child, Tag):
            crumbs.append(_text_of(child))
        elif isinstance(child, NavigableString) and not isinstance(child, _STRING_SKIP):
            crumbs.extend(str(child).split(">"))
    crumbs = [re.sub(r"\s+", " ", clean_text(c)).strip(" >") for c in crumbs]
    return [c for c in crumbs if c]


def extract_titles(soup) -> Tuple[str, str]:
    """-> (title, page_title): the <h1> heading and the full <title>."""
    page_title = re.sub(r"\s+", " ", clean_text(soup.title.get_text(" "))) if soup.title else ""
    title = _text_of(soup.find("h1", class_="Hero__heading"))
    if not title and page_title:
        title = page_title.split("|")[0].strip()
    if not title:
        title = _text_of(soup.find("h1"))
    return title, page_title


def extract_catalog_year(breadcrumbs: Sequence[str], page_title: str, title: str = "") -> str:
    """"New York Tech Catalog 2026–2027" -> "2026-2027" ("" when the page does not say)."""
    for text in (breadcrumbs[0] if breadcrumbs else "", page_title, title):
        for pattern in (_YEAR_AFTER, _YEAR_BEFORE):
            m = pattern.search(text or "")
            if m and int(m.group(2)) == int(m.group(1)) + 1:
                return f"{m.group(1)}-{m.group(2)}"
    return ""


def extract_area(breadcrumbs: Sequence[str]) -> Optional[str]:
    if len(breadcrumbs) < 2:
        return None
    area = breadcrumbs[1]
    # a few medicine pages skip the "Schools and Colleges" crumb
    return "Schools and Colleges" if area in _SCHOOL_NAMES else area


def extract_school(url: str, breadcrumbs: Sequence[str]) -> Optional[str]:
    segs = path_segments(url)
    if segs and segs[0] in config.SCHOOL_BY_PATH:
        return config.SCHOOL_BY_PATH[segs[0]]
    if len(breadcrumbs) > 2 and breadcrumbs[1] == "Schools and Colleges":
        return breadcrumbs[2]
    return next((c for c in breadcrumbs if c in _SCHOOL_NAMES), None)


def find_campuses(text: str) -> List[str]:
    return [name for name, pattern in config.CAMPUS_PATTERNS.items() if re.search(pattern, text or "")]


# ---- degrees ---------------------------------------------------------------
_DEGREE_ABBR = re.compile(
    r"(?<![A-Za-z])("
    r"Ph\.\s?D\.?|Psy\.\s?D\.?|Au\.\s?D\.?|D\.\s?P\.\s?T\.?|DPT|O\.\s?T\.\s?D\.?|OTD|"
    r"D\.\s?N\.\s?P\.?|DNP|D\.\s?O\.|M\.\s?B\.\s?A\.?|MBA|M\.\s?Arch\.?|B\.\s?Arch\.?|"
    r"M\.\s?F\.\s?A\.?|MFA|B\.\s?F\.\s?A\.?|BFA|M\.\s?A\.\s?T\.|M\.\s?P\.\s?H\.?|MPH|"
    r"M\.\s?P\.\s?S\.|B\.\s?P\.\s?S\.|M\.\s?S\.\s?Ed\.?|M\.\s?S\.|M\.\s?A\.|B\.\s?S\.|"
    r"B\.\s?A\.|A\.\s?A\.\s?S\.|A\.\s?S\.|A\.\s?C\."
    r")(?![A-Za-z])")
_ABBR_CANON = {
    "PHD": "Ph.D.", "PSYD": "Psy.D.", "AUD": "Au.D.", "DPT": "D.P.T.", "OTD": "O.T.D.",
    "DNP": "D.N.P.", "DO": "D.O.", "MBA": "M.B.A.", "MARCH": "M.Arch.", "BARCH": "B.Arch.",
    "MFA": "M.F.A.", "BFA": "B.F.A.", "MAT": "M.A.T.", "MPH": "M.P.H.", "MPS": "M.P.S.",
    "BPS": "B.P.S.", "MSED": "M.S.Ed.", "MS": "M.S.", "MA": "M.A.", "BS": "B.S.", "BA": "B.A.",
    "AAS": "A.A.S.", "AS": "A.S.", "AC": "Advanced Certificate",
}
# Degree names written out, most specific first (used when no abbreviation is present).
_DEGREE_NAMES: List[Tuple[re.Pattern, str]] = [(re.compile(p, re.I), d) for p, d in (
    (r"Doctor of Osteopathic Medicine", "D.O."),
    (r"Doctor(?:ate)? (?:of|in) Physical Therapy", "D.P.T."),
    (r"Doctor(?:ate)? (?:of|in) Occupational Therapy", "O.T.D."),
    (r"Doctor of Philosophy", "Ph.D."),
    (r"Doctor of Nursing Practice", "D.N.P."),
    (r"Master of Business Administration", "M.B.A."),
    (r"Master of Architecture", "M.Arch."),
    (r"Bachelor of Architecture", "B.Arch."),
    (r"Master of Fine Arts", "M.F.A."),
    (r"Bachelor of Fine Arts", "B.F.A."),
    (r"Master of Arts in Teaching", "M.A.T."),
    (r"Master of Public Health", "M.P.H."),
    (r"Master of Professional Studies", "M.P.S."),
    (r"Bachelor of Professional Studies", "B.P.S."),
    (r"Master of Science", "M.S."),
    (r"Master of Arts", "M.A."),
    (r"Bachelor of Science", "B.S."),
    (r"Bachelor of Arts", "B.A."),
)]
# Non-degree credentials, checked last.
_CREDENTIALS: List[Tuple[re.Pattern, str]] = [(re.compile(p, re.I), d) for p, d in (
    (r"\bAdvanced Certificates?\b", "Advanced Certificate"),
    (r"\bMinors?\b", "Minor"),
    (r"\bCertificates?\b", "Certificate"),
)]
_DEGREE_LEVEL = {
    "B.S.": "undergraduate", "B.A.": "undergraduate", "B.F.A.": "undergraduate",
    "B.Arch.": "undergraduate", "B.P.S.": "undergraduate", "A.A.S.": "undergraduate",
    "A.S.": "undergraduate", "Minor": "undergraduate",
    "M.S.": "graduate", "M.A.": "graduate", "M.B.A.": "graduate", "M.Arch.": "graduate",
    "M.F.A.": "graduate", "M.A.T.": "graduate", "M.P.H.": "graduate", "M.P.S.": "graduate",
    "M.S.Ed.": "graduate", "Advanced Certificate": "graduate",
    "Ph.D.": "doctoral", "Psy.D.": "doctoral", "Au.D.": "doctoral", "D.P.T.": "doctoral",
    "O.T.D.": "doctoral", "D.N.P.": "doctoral", "D.O.": "professional",
}
# URL slug endings that name a degree (/engineering/computer_science_phd)
_SLUG_DEGREE = [
    (re.compile(r"(^|_)phd$"), "Ph.D."), (re.compile(r"(^|_)ms$"), "M.S."),
    (re.compile(r"(^|_)bs$"), "B.S."), (re.compile(r"(^|_)ma$"), "M.A."),
    (re.compile(r"(^|_)mba$"), "M.B.A."), (re.compile(r"(^|_)otd$"), "O.T.D."),
    (re.compile(r"(^|_)dpt$"), "D.P.T."), (re.compile(r"(^|_)ac$"), "Advanced Certificate"),
    (re.compile(r"(^|_)minor(_|$)"), "Minor"), (re.compile(r"(^|_)certificate$"), "Certificate"),
]


def _abbr_degrees(text: str) -> List[str]:
    found = []
    for m in _DEGREE_ABBR.finditer(text or ""):
        key = re.sub(r"[\s.]", "", m.group(1)).upper()
        canon = _ABBR_CANON.get(key)
        if canon and canon not in found:
            found.append(canon)
    return found


def find_degree(text: str) -> Optional[str]:
    """Normalised degree named in a title: "Computer Science, B.S." -> "B.S.",
    "Medical and Biological Sciences, D.O./Ph.D." -> "D.O./Ph.D.",
    "Minor in Physics" -> "Minor", "Physical Therapy, DPT" -> "D.P.T."."""
    if not text:
        return None
    found = _abbr_degrees(text)
    if found:
        return "/".join(found)
    for pattern, degree in _DEGREE_NAMES + _CREDENTIALS:
        if pattern.search(text):
            return degree
    return None


def degree_level(degree: Optional[str]) -> Optional[str]:
    if not degree:
        return None
    parts = degree.split("/")
    if "D.O." in parts:
        return "professional"
    return _DEGREE_LEVEL.get(parts[0])


def _slug_degree(url: str) -> Optional[str]:
    segs = path_segments(url)
    slug = segs[-1] if segs else ""
    for pattern, degree in _SLUG_DEGREE:
        if pattern.search(slug):
            return degree
    return None


_TRAILING_TRACK = re.compile(r",\s*[^,]*\b(concentration|option|track|emphasis|specialization)\b[^,]*$", re.I)
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
_DEGREE_IN_FIELD = re.compile(
    r"^(?:Undergraduate |Graduate )?(?P<deg>(?:Bachelor|Master|Doctor(?:ate)?) of [A-Z][A-Za-z ]*?|Doctorate"
    r"|Advanced Certificates?(?: Programs?)?|Certificates?(?: Programs?)?|Minor"
    r"|Ph\.D\.|B\.S\.|M\.S\.|B\.A\.|M\.A\.|B\.F\.A\.|M\.F\.A\.|M\.B\.A\.) in (?P<field>.+)$")
_CREDENTIAL_WORDS = re.compile(r"(advanced )?certificates?( programs?)?|minors?", re.I)
# "D.O. Program" -> "Doctor of Osteopathic Medicine, D.O."
_DEGREE_LONG_NAME = {
    "D.O.": "Doctor of Osteopathic Medicine", "D.P.T.": "Doctor of Physical Therapy",
    "O.T.D.": "Doctor of Occupational Therapy", "Ph.D.": "Doctor of Philosophy",
    "M.B.A.": "Master of Business Administration", "M.Arch.": "Master of Architecture",
    "B.Arch.": "Bachelor of Architecture",
}


def _is_degree_label(text: str) -> bool:
    """True for "B.S.", "D.O./Ph.D.", "OTD", "Minor", "Advanced Certificates" ..."""
    if _CREDENTIAL_WORDS.fullmatch(text.strip()):
        return True
    rest = _DEGREE_ABBR.sub("", text)
    return bool(_abbr_degrees(text)) and not re.sub(r"[\s/,&]|and", "", rest)


def canonical_program_name(raw: str) -> str:
    """Normalise a program name to the "<Field>, <Degree>" form used by the breadcrumbs.

    "Curriculum Requirements for Bachelor of Science in Computer Science" -> "Computer Science, B.S."
    "Minor in Mathematics" -> "Mathematics, Minor"
    "Civil Engineering B.S." -> "Civil Engineering, B.S."
    "Physical Therapy, DPT" -> "Physical Therapy, D.P.T."
    "Global Health Certificate" -> "Global Health, Certificate"
    "Master of Architecture" -> "Master of Architecture, M.Arch."
    Names of any other shape are returned unchanged.
    """
    name = re.sub(r"\s+", " ", raw or "").strip()
    name = re.sub(r"^(Curriculum Requirements for|Academic Program:)\s*", "", name, flags=re.I)
    if not name:
        return ""
    # "<Degree> in <Field>[ (notes)][, Some Concentration]"
    m = _DEGREE_IN_FIELD.match(name)
    if m and not _abbr_degrees(m.group("field")):
        degree = find_degree(m.group("deg")) or find_degree(name)
        field_ = _TRAILING_PAREN.sub("", _TRAILING_TRACK.sub("", m.group("field"))).strip(" ,")
        if degree and field_:
            return f"{field_}, {degree}"
    # "<Field>, <Degree>[ (notes)][, Some Concentration]", "<Field> B.S.", "<Field> Certificate"
    core = _TRAILING_TRACK.sub("", _TRAILING_PAREN.sub("", name)).strip(" ,")
    m = re.match(r"^(?P<field>.+),\s*(?P<deg>[^,]+)$", core) or \
        re.match(r"^(?P<field>\S+(?: \S+)+?) (?P<deg>(?:Advanced )?Certificates?(?: Programs?)?)$", core) or \
        re.match(r"^(?P<field>\S+(?: \S+)+) (?P<deg>\S+)$", core)
    if m and _is_degree_label(m.group("deg")) and not find_degree(m.group("field")):
        return f"{m.group('field').strip()}, {find_degree(m.group('deg'))}"
    # a bare degree: "Master of Architecture", "D.O. Program"
    for pattern, degree in _DEGREE_NAMES:
        if pattern.fullmatch(name):
            return f"{name}, {degree}"
    m = re.fullmatch(r"(?P<deg>\S+)(?: (?:Degree )?Program)?", name)
    if m and _is_degree_label(m.group("deg")):
        degree = find_degree(m.group("deg"))
        if degree in _DEGREE_LONG_NAME:
            return f"{_DEGREE_LONG_NAME[degree]}, {degree}"
    return name


# ---- page type ----------------------------------------------------------------
_PROGRAM_LIST_SLUGS = {
    "undergraduate", "graduate", "doctoral", "doctorate", "undergraduate_programs",
    "graduate_programs", "doctoral_programs", "undergraduate_minors", "graduate_minors",
    "graduate_and_special_programs", "concurrent_programs", "programs",
}
_PROGRAM_LIST_TITLE = re.compile(
    r"^((school of [a-z ]+: )?(undergraduate|graduate|doctoral)( departments and)? programs\b"
    r"|(undergraduate|graduate) minors$|concurrent programs$|additional graduate degrees and special programs$"
    r"|schools and colleges$)", re.I)
_PROGRAM_LIST_CRUMB = re.compile(
    r"^((undergraduate|graduate|doctoral)( departments and)? programs|(undergraduate|graduate) minors"
    r"|additional graduate .*programs|concurrent programs)$", re.I)
_FACULTY_SLUGS = {"faculty", "administration", "trustees", "advisory_board", "staff", "deans"}
# titles of school pages that are about admissions/fees/policies even when they name a degree
# ("Application Procedure: D.O. Program")
_INFO_TITLE = re.compile(r"^(application|admissions?|tuition|financial|polic(y|ies)|requirements for graduation|"
                         r"academic calendar)\b", re.I)


def _keyword_type(text: str) -> Optional[str]:
    t = text.lower()
    if re.search(r"faculty|administration|advisory board|trustees", t):
        return "faculty"
    if re.search(r"financial aid|scholarship|loan", t):
        return "financial_aid"
    if re.search(r"tuition|fees\b|refund|payment|financial information|withdrawal procedure", t):
        return "tuition"
    if re.search(r"polic|ferpa|privacy|requirements for graduation|graduation requirements|"
                 r"background check|code of conduct|readmission|transfer applicants", t):
        return "policy"
    if re.search(r"calendar|admissions?\b|application|facilities|institutional information", t):
        return "general_information"
    return None


def classify_page(url: str, breadcrumbs: Sequence[str], title: str) -> str:
    """Page type (one of ``models.PAGE_TYPES``) from the URL, breadcrumbs and <h1>."""
    segs = path_segments(url)
    if not segs:
        return "home"
    first, last = segs[0], segs[-1]
    title = title or ""
    last_crumb = breadcrumbs[-1] if breadcrumbs else ""
    query = dict(parse_qsl(urlsplit(url).query))
    if first == "courses":
        return "course_listing" if len(segs) == 1 and query.get("cs") and query.get("cal") else "course_index"
    if first == "help":
        return "help"
    if first in ("semester-map", "semester_map") or "curriculum" in segs or \
            re.match(r"curriculum requirements\b", title, re.I):
        return "curriculum"
    if first == "administration_and_faculty" or last in _FACULTY_SLUGS:
        return "faculty"
    if first.startswith("financial_aid") or last == "financial_aid":
        return "financial_aid"
    if first == "tuition":
        return "tuition"
    if first == "policies_and_rules":
        return "policy"
    if first == "student_services":
        return "student_services"
    if first == "general_information":
        return "general_information"
    if first == "schools_and_colleges":
        return "program_list"
    if first in config.SCHOOL_BY_PATH:
        if len(segs) == 1:
            return "school"
        if last in _PROGRAM_LIST_SLUGS or _PROGRAM_LIST_TITLE.match(title) or \
                _PROGRAM_LIST_CRUMB.match(last_crumb) or (len(segs) == 2 and last == "academics"):
            return "program_list"
        if _INFO_TITLE.match(title):
            return _keyword_type(title) or "general_information"
        if "admissions" in segs[1:] and not (find_degree(title) or _slug_degree(url)):
            return "policy" if re.search(r"background check", title, re.I) else "general_information"
        if find_degree(title) or find_degree(last_crumb) or _slug_degree(url) or (
                len(breadcrumbs) >= 2 and _PROGRAM_LIST_CRUMB.match(breadcrumbs[-2])):
            return "program"
        return _keyword_type(f"{title} | {last_crumb}") or "other"
    return "other"


def _has_degree_suffix(name: str) -> bool:
    """True for names in the breadcrumb style "<Field>, <Degree>" ("Physics, B.S.")."""
    field_, sep, degree = name.rpartition(",")
    return bool(sep and field_.strip()) and _is_degree_label(degree)


def program_info(url: str, breadcrumbs: Sequence[str], title: str, page_type: str,
                 intro: str = "") -> Tuple[Optional[str], Optional[str]]:
    """-> (program, degree) for program pages and their sub-pages, else (None, None)."""
    if page_type == "program":
        degree = (find_degree(title) or find_degree(breadcrumbs[-1] if breadcrumbs else "")
                  or _slug_degree(url))
        if not degree and intro:
            # department pages such as "Physics": "This Bachelor of Science in Physics program ..."
            words = " ".join(intro.split()[:120])
            found = _abbr_degrees(words) or [d for p, d in _DEGREE_NAMES if p.search(words)]
            if len(set(found)) == 1:
                degree = found[0]
        name = canonical_program_name(title)
        crumb = canonical_program_name(breadcrumbs[-1]) if breadcrumbs else ""
        if degree and not _has_degree_suffix(name):
            if _has_degree_suffix(crumb) and find_degree(crumb) == degree:
                # "Master of Arts in Teaching (M.A.T.), Adolescence Education (Grades 7-12 ...)"
                # -> the crumb "Adolescence Education, M.A.T." (as on its curriculum pages)
                name = crumb
            elif name and not find_degree(name):
                # "Chemistry" (/chemistry_phd) -> "Chemistry, Ph.D."
                name = f"{name}, {degree}"
        return name or None, degree
    if page_type != "curriculum":
        return None, None
    candidates: List[str] = []
    if re.match(r"curriculum requirements for .+ in ", title, re.I):
        candidates.append(title)
    if len(breadcrumbs) >= 2 and re.search(r"curriculum|semester map", breadcrumbs[-1], re.I):
        candidates.append(breadcrumbs[-2])
    candidates.append(title)
    for raw in candidates:
        degree = find_degree(raw)
        if degree and not _PROGRAM_LIST_CRUMB.match(raw) and raw not in _SCHOOL_NAMES:
            return canonical_program_name(raw), degree
    return None, None


def page_level(url: str, breadcrumbs: Sequence[str], degree: Optional[str], title: str = "") -> Optional[str]:
    """undergraduate | graduate | doctoral | professional | None."""
    from_degree = degree_level(degree)
    if from_degree in ("doctoral", "professional"):
        return from_degree
    for crumb in reversed(breadcrumbs[1:]):
        m = re.match(r"(undergraduate|graduate|doctoral)\b", crumb, re.I)
        if m:
            return m.group(1).lower()
    if from_degree:
        return from_degree
    query = dict(parse_qsl(urlsplit(url).query))
    cal = (query.get("cal") or "").upper()
    if cal in ("UG", "GR", "PROF"):
        return {"UG": "undergraduate", "GR": "graduate", "PROF": "professional"}[cal]
    segs = path_segments(url)
    for seg in segs:
        for token in re.split(r"[_-]", seg):
            if token in ("undergraduate", "undergrad", "ug"):
                return "undergraduate"
            if token in ("graduate", "grad"):
                return "graduate"
            if token in ("doctoral", "doctorate", "phd"):
                return "doctoral"
    m = re.search(r"\b(undergraduate|graduate) students\b", title or "", re.I)
    if m:
        return m.group(1).lower()
    if segs and segs[0] == "medicine":
        return "professional"
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def content_root(soup) -> Optional[Tag]:
    """``div.Main__content .Common`` -> ``div.Main__content`` -> ``main`` -> ``body``.

    (bs4's ``find`` is used instead of CSS selectors: much faster on big pages.)
    """
    main_content = soup.find("div", class_="Main__content")
    if main_content is not None:
        return main_content.find(class_="Common") or main_content
    return soup.find("main") or soup.find("body")


def _inside_skipped(el: Tag, root: Tag) -> bool:
    """True if ``el`` sits inside a script/form/hidden element below ``root``.

    Only ancestors up to ``root`` count: lxml puts <main> under <head> on this site.
    """
    for parent in el.parents:
        if parent is root or parent is None:
            return False
        if parent.name in _SKIP_TAGS or _is_hidden(parent):
            return True
    return False


def extract_links(root: Optional[Tag], base: str) -> List[Dict[str, str]]:
    """Links inside ``root`` -> [{"text", "url"}], normalised, javascript:/mailto: dropped."""
    found: Dict[str, str] = {}
    if root is None:
        return []
    for a in root.find_all("a", href=True):
        if _inside_skipped(a, root):
            continue
        url = normalize_url(a["href"], base=base)
        if not url:
            continue
        text = re.sub(r"\s+", " ", clean_text(a.get_text(" ", strip=True)))
        if url not in found or (not found[url] and text):
            found[url] = text
    return [{"text": t, "url": u} for u, t in found.items()]


_links = extract_links  # old name, kept for existing callers

_GENERIC_TITLES = {"semester map", "curriculum", "curriculum requirements"}


def _fragment(anchor: str) -> str:
    return quote(anchor, safe="-._~!$&'()*+,;=:@/?")


def section_records(page: PageRecord, blocks: Sequence[Block]) -> List[SectionRecord]:
    """Split ``blocks`` into sections (:func:`split_sections`) -> SectionRecords that
    carry the page's metadata. A section with an anchor links to ``url#anchor``."""
    sections: List[SectionRecord] = []
    for order, sec in enumerate(split_sections(blocks)):
        body = sec.text
        sections.append(SectionRecord(
            section_id=f"{page.doc_id}-s{order:03d}",
            doc_id=page.doc_id,
            url=f"{page.url}#{_fragment(sec.anchor)}" if sec.anchor else page.url,
            page_title=page.title,
            heading=sec.heading,
            heading_path=list(sec.heading_path),
            text=body,
            word_count=word_count(body),
            order=order,
            page_type=page.page_type,
            school=page.school,
            program=page.program,
            degree=page.degree,
            level=page.level,
            catalog_year=page.catalog_year,
            breadcrumbs=list(page.breadcrumbs),
            site=page.site,
        ))
    return sections


def parse_page(html: str, url: str, fetched_at: str = "") -> Tuple[PageRecord, List[SectionRecord]]:
    """Parse one catalog page into its PageRecord and its SectionRecords (``site`` = the
    URL's host; other NYIT websites go through site_parser.parse_site_page)."""
    soup = make_soup(html)
    title, page_title = extract_titles(soup)
    breadcrumbs = extract_breadcrumbs(soup)
    root = content_root(soup)
    blocks, num_tables = walk_content(root)
    text = blocks_to_text(blocks)
    if not text:
        log.debug("no main-content text on %s", url)

    page_type = classify_page(url, breadcrumbs, title)
    if page_type not in PAGE_TYPES:  # defensive: never write an unknown type
        page_type = "other"
    program, degree = program_info(url, breadcrumbs, title, page_type, intro=text)
    if program and title.lower() in _GENERIC_TITLES:
        # /semester-map/... and /curriculum/<slug> pages are all titled the same way
        title = f"{title}: {program}"
    doc_id = "page-" + short_hash(url)
    page = PageRecord(
        doc_id=doc_id,
        url=url,
        title=title,
        page_title=page_title,
        breadcrumbs=breadcrumbs,
        catalog_year=extract_catalog_year(breadcrumbs, page_title, title),
        page_type=page_type,
        area=extract_area(breadcrumbs),
        school=extract_school(url, breadcrumbs),
        program=program,
        degree=degree,
        level=page_level(url, breadcrumbs, degree, title),
        campuses=find_campuses(f"{title}\n{text}"),
        headings=[b.text for b in blocks if b.kind == "heading" and 2 <= b.level <= 4],
        text=text,
        word_count=word_count(text),
        num_tables=num_tables,
        links=extract_links(root, url),
        content_sha1=sha1(text),
        fetched_at=fetched_at,
        site=url_host(url),
    )
    return page, section_records(page, blocks)
