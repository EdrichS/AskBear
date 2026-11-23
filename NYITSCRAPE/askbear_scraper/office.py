"""Word, Excel and PowerPoint files linked from the NYIT websites (Office Open XML: .docx,
.xlsx, .pptx): their text, read with the standard library only (zipfile + ElementTree).

An Office Open XML file is a ZIP "package" of XML parts: ``[Content_Types].xml``, the main
part (``word/document.xml``, ``xl/workbook.xml`` or ``ppt/presentation.xml``), the parts it
points to through its relationships (``_rels/*.rels``: styles, worksheets, slides...) and
the document properties (``docProps/core.xml``: title, last modification). This module
turns one file into "pages" for documents.py, which cleans their text like PDF text
(``documents.tidy_text``) and writes them to documents.jsonl:

* .docx: the body in reading order (paragraphs, tables with their cells joined by " | ",
  one row per line, text boxes), tracked insertions kept and deletions left out, field
  codes, table-of-contents lines and the prompts of empty content controls ("Click or tap
  here to enter text.") left out; headers, footers, footnotes and comments are
  not read. Paragraphs in a heading style (or with an outline level) give the ``toc_path``.
  The text is cut into pages of about PAGE_MIN_WORDS to PAGE_MAX_WORDS words, at a heading
  or paragraph boundary: the corpus treats each page as one passage;
* .xlsx: one sheet gives one or more pages (rows of at most PAGE_MAX_WORDS words), the
  sheet name as ``toc_path`` (not a default name such as "Sheet1"). Hidden sheets are
  skipped. Cells give their shared or inline string, number, TRUE/FALSE or the cached
  value of their formula; a number formatted as a date, a time or a percentage is shown
  as one ("2025-09-10", "14:30", "33%"). Under column headings found in the sheet (a row
  of short text cells that the rows below fit, with the group-label rows above it) a row
  is written "Heading: value | ..." (empty cells left out), else "a | b | c". A later
  page repeats the last one-cell line (a section or question) and the leading cells of
  the row group it starts in;
* .pptx: one slide = one page, in presentation order (``ppt/_rels/presentation.xml.rels``,
  not the file names), with the text of its text frames, grouped shapes and tables; the
  slide title (title placeholder) is the first line and the ``toc_path``, the other shapes
  follow top to bottom, then left to right. Slide numbers, dates and footers are left out.

The files come from the web, so they are read defensively (``OfficeError`` says why a file
is refused): only ZIP members that are stored or deflated and not encrypted are read, each
inflated in bounded chunks (a member that inflates to more than its declared size is
refused, not inflated in memory); a file may have at most MAX_PARTS members and at most
MAX_UNCOMPRESSED bytes are read from it in total. Parts are decoded as UTF-8/UTF-16 here
(other declared encodings are refused) and XML with a DOCTYPE or ENTITY declaration is
refused, so no entity is ever expanded ("billion laughs", external entities). The big
parts (the Word body, worksheets, shared strings) are parsed incrementally, a paragraph,
table or row at a time. Both namespace flavours of the standard are understood:
Transitional (schemas.openxmlformats.org, what Office writes) and Strict (purl.oclc.org).
"""

from __future__ import annotations

import posixpath
import re
import struct
from bisect import bisect_right
import zipfile
import zlib
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import unquote
from xml.etree import ElementTree as ET

DOCX, XLSX, PPTX = "docx", "xlsx", "pptx"
OFFICE_TYPES = (DOCX, XLSX, PPTX)
# the main part each kind of file must have (checked when it is downloaded)
MAIN_PARTS = {DOCX: "word/document.xml", XLSX: "xl/workbook.xml", PPTX: "ppt/presentation.xml"}
TYPE_LABELS = {DOCX: "Word", XLSX: "Excel", PPTX: "PowerPoint"}

MAX_UNCOMPRESSED = 50 * 1024 * 1024   # bytes read from one file, all its parts together
MAX_TREE_PART = 20 * 1024 * 1024      # parts parsed as one tree (slides, styles, workbook...)
MAX_PARTS = 10_000                    # ZIP members in one file (a real one has 10-2,000)
PAGE_MIN_WORDS = 150                  # .docx: a heading starts a new page after this many words
PAGE_MAX_WORDS = 400                  # .docx/.xlsx: pages are not longer than this (if possible)
MAX_HEADING_WORDS = 25                # longer "headings" (a heading style on a paragraph) are text
MAX_LABEL_WORDS = 8                   # .xlsx: a longer cell is no column heading
MAX_LABEL_CHARS = 60
MAX_HEADER_LEVELS = 3                 # .xlsx: group-label rows read above the column headings

# outcomes of check_file (statuses in docs_manifest.jsonl)
NOT_OFFICE = "not_office"     # an HTML page, a legacy binary file, a damaged ZIP, another kind
ENCRYPTED = "encrypted"       # password-protected: an OLE2 container with an EncryptedPackage

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_STREAM = "EncryptedPackage".encode("utf-16-le")
_CHUNK = 1 << 16


class OfficeError(ValueError):
    """The file cannot be read as the Office document it should be (encrypted, damaged,
    unsafe or too large): the message says why."""


# ---------------------------------------------------------------------------
# namespaces (Transitional, Strict)
# ---------------------------------------------------------------------------
W = frozenset({"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
               "http://purl.oclc.org/ooxml/wordprocessingml/main"})
S = frozenset({"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
               "http://purl.oclc.org/ooxml/spreadsheetml/main"})
P = frozenset({"http://schemas.openxmlformats.org/presentationml/2006/main",
               "http://purl.oclc.org/ooxml/presentationml/main"})
A = frozenset({"http://schemas.openxmlformats.org/drawingml/2006/main",
               "http://purl.oclc.org/ooxml/drawingml/main"})
R = frozenset({"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
               "http://purl.oclc.org/ooxml/officeDocument/relationships"})
MATH = frozenset({"http://schemas.openxmlformats.org/officeDocument/2006/math",
                  "http://purl.oclc.org/ooxml/officeDocument/math"})
MC = frozenset({"http://schemas.openxmlformats.org/markup-compatibility/2006"})
PKG_REL = frozenset({"http://schemas.openxmlformats.org/package/2006/relationships"})
DC = frozenset({"http://purl.org/dc/elements/1.1/"})
DCTERMS = frozenset({"http://purl.org/dc/terms/"})
APP = frozenset({"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
                 "http://purl.oclc.org/ooxml/officeDocument/extendedProperties"})


@lru_cache(maxsize=4096)
def _split_tag(tag: str) -> Tuple[str, str]:
    """"{namespace}local" -> (namespace, local); "" namespace for a tag without one."""
    if not tag.startswith("{"):
        return "", tag
    ns, _, local = tag[1:].partition("}")
    return ns, local


def _local(elem, namespaces: FrozenSet[str]) -> str:
    """The local name of ``elem`` when its namespace is one of ``namespaces``, else ""."""
    tag = elem.tag
    if not isinstance(tag, str):
        return ""                        # a comment or processing instruction
    ns, local = _split_tag(tag)
    return local if ns and ns in namespaces else ""


def _qnames(namespaces: FrozenSet[str], local: str) -> Tuple[str, ...]:
    return tuple("{%s}%s" % (ns, local) for ns in sorted(namespaces))


def _attr(elem, namespaces: FrozenSet[str], local: str) -> Optional[str]:
    """A namespaced attribute (w:val, r:id...) in either flavour."""
    for name in _qnames(namespaces, local):
        value = elem.get(name)
        if value is not None:
            return value
    return None


def _child(elem, namespaces: FrozenSet[str], local: str):
    """The first child ``local`` of ``elem`` (``None`` if there is none)."""
    if elem is None:
        return None
    return next((c for c in elem if _local(c, namespaces) == local), None)


def _children_named(elem, namespaces: FrozenSet[str], local: str) -> List:
    return [c for c in elem if _local(c, namespaces) == local] if elem is not None else []


def _first_choice(alternate):
    """mc:AlternateContent holds the same content several times (mc:Choice for newer
    readers, mc:Fallback for older ones): only the first one is read."""
    return next((c for c in alternate if _local(c, MC) in ("Choice", "Fallback")), None)


# ---------------------------------------------------------------------------
# the download check
# ---------------------------------------------------------------------------
def check_file(path: Path, file_type: str) -> Optional[Tuple[str, str]]:
    """``None`` when ``path`` is an Office Open XML file of ``file_type``: a ZIP file (it
    starts with PK\\x03\\x04) with [Content_Types].xml and the main part (MAIN_PARTS).
    Otherwise (status, reason): ENCRYPTED for a password-protected file (Office stores
    those in an OLE2 container), NOT_OFFICE for anything else (an HTML sign-in page, a
    legacy .doc/.xls behind a new extension, a damaged ZIP, another kind of Office file)."""
    label = TYPE_LABELS.get(file_type, "Office")
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(len(OLE2_MAGIC))
            if head == OLE2_MAGIC:
                encrypted = _contains(fh, _ENCRYPTED_STREAM)
                if encrypted:
                    return ENCRYPTED, (f"password-protected {label} file (encrypted: an OLE2 "
                                       "container instead of a ZIP package)")
                return NOT_OFFICE, f"a legacy binary Office file (OLE2), not a .{file_type} file"
    except OSError as exc:
        return NOT_OFFICE, f"cannot read the downloaded file ({exc})"
    if not head.startswith(ZIP_MAGIC):
        return NOT_OFFICE, f"not a {label} file"
    try:
        with zipfile.ZipFile(path) as package:
            infos = package.infolist()
            if len(infos) > MAX_PARTS:
                return NOT_OFFICE, f"a ZIP file with {len(infos):,} members (over {MAX_PARTS:,})"
            names = {_part_key(i.filename) for i in infos}
    except Exception as exc:  # BadZipFile, EOFError, struct.error...: whatever it is, no package
        return NOT_OFFICE, f"a damaged ZIP file ({type(exc).__name__}: {exc})"
    if MAIN_PARTS.get(file_type) in names and "[content_types].xml" in names:
        return None
    other = next((t for t, part in MAIN_PARTS.items() if part in names), None)
    if other and other != file_type:
        return NOT_OFFICE, f"a {TYPE_LABELS[other]} file (.{other}), not a .{file_type} file"
    return NOT_OFFICE, (f"a ZIP file without {MAIN_PARTS.get(file_type)} and "
                        "[Content_Types].xml, not a " + label + " file")


def _contains(fh, needle: bytes) -> bool:
    """True when the rest of the open file contains ``needle`` (read in chunks)."""
    tail = b""
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        if needle in tail + chunk:
            return True
        tail = chunk[-len(needle):]
    return False


def looks_like_office(path: Path) -> bool:
    """A quick check of a cached file: it starts like a ZIP package."""
    try:
        with Path(path).open("rb") as fh:
            return fh.read(len(ZIP_MAGIC)) == ZIP_MAGIC
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the package: parts read with a budget, relationships, safe XML
# ---------------------------------------------------------------------------
def _part_key(name: str) -> str:
    """Part names are compared without case (OPC) and without a leading "/"."""
    return name.replace("\\", "/").lstrip("/").lower()


class Relationship(NamedTuple):
    type: str       # ends with "/officeDocument", "/worksheet", "/slide", "/styles"...
    target: str     # part name, e.g. "xl/worksheets/sheet1.xml"


class Package:
    """The parts of one Office file. Every read counts against ``budget`` (bytes)."""

    def __init__(self, path: Path, budget: int = MAX_UNCOMPRESSED, max_parts: int = MAX_PARTS):
        self.path = Path(path)
        self.budget = budget
        self.used = 0
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError) as exc:
            raise OfficeError(f"not a readable ZIP package ({type(exc).__name__}: {exc})")
        self._fh = None
        try:
            infos = self._zip.infolist()
            if len(infos) > max_parts:
                raise OfficeError(f"{len(infos):,} ZIP members (more than {max_parts:,})")
            self.parts: Dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                self.parts.setdefault(_part_key(info.filename), info)
            self._fh = self.path.open("rb")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._zip.close()

    def __enter__(self) -> "Package":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def info(self, name: str) -> Optional[zipfile.ZipInfo]:
        key = _part_key(name)
        return self.parts.get(key) or self.parts.get(_part_key(unquote(name)))

    def read(self, name: str) -> Optional[bytes]:
        """The bytes of part ``name`` (``None`` when the package has no such part)."""
        info = self.info(name)
        if info is None:
            return None
        if info.flag_bits & 0x1:
            raise OfficeError(f"{name}: encrypted ZIP member")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise OfficeError(f"{name}: unsupported compression method {info.compress_type}")
        left = self.budget - self.used
        if info.file_size > left:
            raise OfficeError(f"{name}: {info.file_size:,} bytes uncompressed, over the limit of "
                              f"{self.budget:,} bytes read from one file")
        data = self._inflate(info, name)
        self.used += len(data)
        return data

    def _inflate(self, info: zipfile.ZipInfo, name: str) -> bytes:
        """Read one member from its local header on, inflating at most ``file_size`` + 1
        bytes: a member whose data inflates to more than it declares is refused."""
        fh = self._fh
        fh.seek(info.header_offset)
        header = fh.read(30)
        if len(header) != 30 or header[:4] != ZIP_MAGIC:
            raise OfficeError(f"{name}: damaged ZIP member (bad local header)")
        name_len, extra_len = struct.unpack("<HH", header[26:30])
        fh.seek(info.header_offset + 30 + name_len + extra_len)
        size, remaining = info.file_size, info.compress_size
        if info.compress_type == zipfile.ZIP_STORED:
            if remaining != size:
                raise OfficeError(f"{name}: damaged ZIP member (stored sizes differ)")
            data = fh.read(size)
            if len(data) != size:
                raise OfficeError(f"{name}: damaged ZIP member (cut off)")
        else:
            inflater = zlib.decompressobj(-15)
            chunks: List[bytes] = []
            produced = 0
            pending = b""
            try:
                while not inflater.eof:
                    if not pending:
                        if remaining <= 0:
                            raise OfficeError(f"{name}: damaged ZIP member (cut off)")
                        pending = fh.read(min(_CHUNK, remaining))
                        if not pending:
                            raise OfficeError(f"{name}: damaged ZIP member (cut off)")
                        remaining -= len(pending)
                    out = inflater.decompress(pending, size + 1 - produced)
                    pending = inflater.unconsumed_tail
                    produced += len(out)
                    if produced > size:
                        raise OfficeError(f"{name}: inflates to more than its declared "
                                          f"{size:,} bytes")
                    chunks.append(out)
            except zlib.error as exc:
                raise OfficeError(f"{name}: damaged ZIP member ({exc})")
            data = b"".join(chunks)
        if len(data) != size or (zlib.crc32(data) & 0xFFFFFFFF) != info.CRC:
            raise OfficeError(f"{name}: damaged ZIP member (size or CRC does not match)")
        return data

    def xml_text(self, name: str) -> Optional[str]:
        data = self.read(name)
        return None if data is None else decode_xml(data, name)

    def tree(self, name: str, required: bool = True):
        """The parsed root element of part ``name``. A missing or malformed part raises
        when ``required``, else gives ``None``; unsafe XML and a part over MAX_TREE_PART
        bytes (only the big parts are parsed incrementally: stream_children) always raise."""
        info = self.info(name)
        if info is not None and info.file_size > MAX_TREE_PART:
            raise OfficeError(f"{name}: {info.file_size:,} bytes, too big to parse as a whole "
                              f"(limit {MAX_TREE_PART:,})")
        text = self.xml_text(name)
        if text is None:
            if required:
                raise OfficeError(f"the package has no part {name}")
            return None
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            if required:
                raise OfficeError(f"{name}: malformed XML ({exc})")
            return None

    def relationships(self, part: str) -> Dict[str, Relationship]:
        """The internal relationships of ``part`` ("" = the package itself) by id."""
        folder, base = posixpath.split(_part_key(part))
        rels_name = posixpath.join(folder, "_rels", base + ".rels")
        root = self.tree(rels_name, required=False)
        out: Dict[str, Relationship] = {}
        for rel in root if root is not None else ():
            if _local(rel, PKG_REL) != "Relationship" or rel.get("TargetMode") == "External":
                continue
            target = resolve_target(folder, rel.get("Target") or "")
            if rel.get("Id") and target:
                out[rel.get("Id")] = Relationship(rel.get("Type") or "", target)
        return out

    def related(self, part: str, rel_type: str, default: str) -> str:
        """The first part related to ``part`` by a relationship of ``rel_type`` (the end of
        the type URI, e.g. "/styles"), else ``default``."""
        return next((r.target for r in self.relationships(part).values()
                     if r.type.endswith(rel_type)), default)


def resolve_target(folder: str, target: str) -> Optional[str]:
    """A relationship target as a part name: relative to the folder of the source part,
    or absolute ("/xl/worksheets/sheet1.xml"); ``None`` when it leaves the package."""
    target = target.strip().split("#", 1)[0]
    if not target:
        return None
    path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
    path = posixpath.normpath(path)
    if path in (".", "") or path == ".." or path.startswith("../"):
        return None
    return path


_XML_DECLARATION = re.compile(r"^\s*<\?xml\b[^>]*?\?>")
_DECLARED_ENCODING = re.compile(rb"""^\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._-]+)""")
_ALLOWED_ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "us-ascii": "utf-8", "ascii": "utf-8",
                      "iso-8859-1": "latin-1", "latin-1": "latin-1", "latin1": "latin-1",
                      "windows-1252": "cp1252", "cp1252": "cp1252"}


def decode_xml(data: bytes, name: str = "part") -> str:
    """The text of an XML part, decoded here (UTF-8 or UTF-16 as the standard requires, or
    an ASCII-compatible encoding it declares) without its XML declaration, so the parser
    gets exactly what was checked. XML that declares a DOCTYPE or an ENTITY is refused."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"<\x00") or data.startswith(b"\x00<"):
        encoding = "utf-16-le" if data[:1] == b"<" else "utf-16-be"
    else:
        declared = _DECLARED_ENCODING.match(data[3:] if data.startswith(b"\xef\xbb\xbf") else data)
        encoding = "utf-8-sig"
        if declared:
            key = declared.group(1).decode("ascii").lower()
            if key.startswith("utf-16") or key == "utf16":
                key = "utf-8"            # (says UTF-16, but its bytes are not: read what is there)
            if key not in _ALLOWED_ENCODINGS:
                raise OfficeError(f"{name}: unsupported XML encoding {key!r}")
            encoding = _ALLOWED_ENCODINGS[key]
            encoding = "utf-8-sig" if encoding == "utf-8" else encoding
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise OfficeError(f"{name}: not valid {encoding} text ({exc.reason})")
    text = text.lstrip("\ufeff")
    declaration = _XML_DECLARATION.match(text)
    if declaration:
        text = text[declaration.end():]
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise OfficeError(f"{name}: XML with a DOCTYPE or ENTITY declaration (refused)")
    return text


def stream_children(text: str, path: Sequence[Tuple[FrozenSet[str], str]]) -> Iterator:
    """Parse XML incrementally and yield every complete child of the element at ``path``
    (from the root: ``[(W, "document"), (W, "body")]``), dropping it from the tree once the
    caller is done with it, so a big part never sits in memory as a whole tree."""
    parser = ET.XMLPullParser(events=("start", "end"))
    stack: list = []
    depth = len(path)
    for pos in range(0, len(text), 1 << 20):
        parser.feed(text[pos:pos + (1 << 20)])
        for event, elem in parser.read_events():
            if event == "start":
                stack.append(elem)
                continue
            stack.pop()
            if len(stack) == depth and all(_local(e, ns) == local
                                           for e, (ns, local) in zip(stack, path)):
                yield elem
                stack[-1].remove(elem)
    parser.close()


# ---------------------------------------------------------------------------
# pages: blocks of text cut at headings/paragraphs
# ---------------------------------------------------------------------------
class Block(NamedTuple):
    """A paragraph, table row or spreadsheet row."""

    text: str
    path: Tuple[str, ...]   # heading path in effect (with this block, when it is a heading)
    heading: bool = False


def word_count(text: str) -> int:
    return sum(1 for w in text.split() if w != "|")


_SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+|\n")


def split_long(text: str, max_words: int = PAGE_MAX_WORDS) -> List[str]:
    """A paragraph longer than ``max_words`` in pieces of at most ``max_words`` words, cut
    after a sentence when possible."""
    if word_count(text) <= max_words:
        return [text]
    pieces: List[str] = []
    current: List[str] = []
    for sentence in (s for s in _SENTENCE_END.split(text) if s and s.strip()):
        words = sentence.split()
        if current and len(current) + len(words) > max_words:
            pieces.append(" ".join(current))
            current = []
        if len(words) > max_words:               # one huge "sentence": cut by words
            cut = len(words) - len(words) % max_words
            pieces.extend(" ".join(words[i:i + max_words]) for i in range(0, cut, max_words))
            words = words[cut:]
        current += words
    if current:
        pieces.append(" ".join(current))
    return pieces


def pack_blocks(blocks: Iterable[Block], min_words: int = PAGE_MIN_WORDS,
                max_words: int = PAGE_MAX_WORDS) -> List[Tuple[str, List[str]]]:
    """Group blocks into pages of (text, toc_path): a heading starts a new page once the
    page has ``min_words`` words; a block that would take the page over ``max_words``
    starts a new one (headings at the end of the full page move along with it). A block
    longer than ``max_words`` is cut (split_long). A short last page (< 50 words) joins
    the page before it when the two stay within 1.25 x ``max_words``. ``toc_path`` is the
    heading path at the start of the page."""
    pages: List[List[Block]] = []
    current: List[Block] = []
    count = 0
    for block in blocks:
        for piece in split_long(block.text, max_words):
            n = word_count(piece)
            full = bool(current) and count + n > max_words
            if current and ((block.heading and count >= min_words) or full):
                carry: List[Block] = []
                if full and not block.heading:
                    while current and current[-1].heading:
                        carry.insert(0, current.pop())
                if current:              # (a page of headings only would be no passage)
                    pages.append(current)
                current, count = carry, sum(word_count(b.text) for b in carry)
            current.append(Block(piece, block.path, block.heading))
            count += n
    if current:
        pages.append(current)
    if len(pages) > 1:
        last = sum(word_count(b.text) for b in pages[-1])
        before = sum(word_count(b.text) for b in pages[-2])
        if last < 50 and last + before <= 1.25 * max_words:
            # (not "pages[-2] += pages.pop()": its target index is taken after the pop)
            tail = pages.pop()
            pages[-1] += tail
    return [("\n".join(b.text for b in page), list(page[0].path)) for page in pages]


def _line(text: str) -> str:
    """Text of one paragraph: tabs and runs of spaces become one space, every line trimmed."""
    lines = (" ".join(part.split()) for part in text.split("\n"))
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# .docx
# ---------------------------------------------------------------------------
# deleted text (tracked changes), field codes, properties, note references, ruby guides
_W_SKIP = {"del", "moveFrom", "delText", "instrText", "delInstrText", "pPr", "rPr",
           "footnoteReference", "endnoteReference", "commentReference", "annotationRef",
           "fldData", "sectPr", "tblPr", "tblGrid", "trPr", "tcPr", "rt"}
_HEADING_STYLE = re.compile(r"^heading\s*([1-9])$", re.I)
_TOC_STYLE = re.compile(r"^(toc\s*\d|table of figures)$", re.I)
# a paragraph in a heading style that is no heading: a sentence ("Complete all course
# readings.") or a list item (a bullet or checkbox glyph first: "\uf0a8 Until you revoke it")
_NOT_A_HEADING = re.compile(r"[.!?;,]$|"
                            r"^[\uf000-\uf0ff\u2022\u25aa\u25e6\u2023\u2043\u25a1\u2610-\u2612*-]")


class _Style(NamedTuple):
    name: str
    based_on: Optional[str]
    outline: Optional[int]


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _docx_styles(root) -> Dict[str, Tuple[str, Optional[int]]]:
    """Paragraph style id -> (style name, heading level 1-9 or None). The level comes from
    the style's outline level, else its name ("heading 2"), else the style it is based on."""
    styles: Dict[str, _Style] = {}
    for style in root if root is not None else ():
        if _local(style, W) != "style" or _attr(style, W, "type") not in (None, "paragraph"):
            continue
        style_id = _attr(style, W, "styleId")
        if not style_id:
            continue
        name_elem = _child(style, W, "name")
        name = _attr(name_elem, W, "val") if name_elem is not None else ""
        based = _child(style, W, "basedOn")
        outline = _child(_child(style, W, "pPr"), W, "outlineLvl")
        styles[style_id] = _Style(name or style_id,
                                  _attr(based, W, "val") if based is not None else None,
                                  _int(_attr(outline, W, "val")) if outline is not None else None)

    def level(style_id: Optional[str], depth: int = 0) -> Optional[int]:
        style = styles.get(style_id or "")
        if style is None or depth > 20:
            return None
        if style.outline is not None:
            return style.outline + 1 if 0 <= style.outline <= 8 else None
        named = _HEADING_STYLE.match(style.name.strip())
        if named:
            return int(named.group(1))
        return level(style.based_on, depth + 1)

    return {sid: (st.name, level(sid)) for sid, st in styles.items()}


class _DocxReader:
    """Turns the body of a Word document into Blocks, in reading order."""

    def __init__(self, styles: Dict[str, Tuple[str, Optional[int]]]):
        self.styles = styles
        self.path: List[str] = []
        self.blocks: List[Block] = []
        self.headings = 0
        self.title: Optional[str] = None           # first paragraph in the Title style
        self.first_heading: Optional[str] = None

    # -- block level ----------------------------------------------------------
    def container(self, elem) -> None:
        for child in elem:
            self.block(child)

    def block(self, elem) -> None:
        local = _local(elem, W)
        if local == "p":
            self.paragraph(elem)
        elif local == "tbl":
            self.table(elem)
        elif local == "sdt":
            content = _child(elem, W, "sdtContent")
            if content is not None and not _placeholder(elem):
                self.container(content)
        elif local in ("customXml", "ins", "moveTo", "smartTag", "txbxContent", "body"):
            self.container(elem)
        elif _local(elem, MC) == "AlternateContent":
            choice = _first_choice(elem)
            if choice is not None:
                self.container(choice)

    def paragraph(self, p) -> None:
        props = _child(p, W, "pPr")
        style_ref = _child(props, W, "pStyle")
        style_id = _attr(style_ref, W, "val") if style_ref is not None else None
        name, level = self.styles.get(style_id or "", (style_id or "", None))
        if style_id and style_id not in self.styles:   # no styles.xml: "Heading2" is a heading
            named = _HEADING_STYLE.match(style_id)
            level = int(named.group(1)) if named else None
        if _TOC_STYLE.match((name or "").strip()):
            return                       # a line of a table of contents: the headings repeat it
        own = _child(props, W, "outlineLvl")
        if own is not None and _int(_attr(own, W, "val")) is not None:
            value = _int(_attr(own, W, "val"))
            level = value + 1 if 0 <= value <= 8 else None
        boxes: list = []
        text = _line(paragraph_text(p, boxes))
        if text:
            heading = (level is not None and word_count(text) <= MAX_HEADING_WORDS
                       and not _NOT_A_HEADING.search(" ".join(text.split())))
            is_title = (name or "").strip().lower() == "title"
            if is_title and self.title is None:
                self.title = " ".join(text.split())
            if heading:
                entry = " ".join(text.split())[:200]
                self.path = self.path[:level - 1] + [entry]
                self.headings += 1
                if self.first_heading is None:
                    self.first_heading = entry
            self.blocks.append(Block(text, tuple(self.path), heading))
        for box in boxes:                 # text boxes anchored in the paragraph follow it
            self.container(box)

    def table(self, tbl) -> None:
        for row in _table_rows(tbl):
            self.row(row)

    def row(self, tr) -> None:
        cells = [cell_text(tc) for tc in _row_cells(tr)]
        line = " | ".join(c for c in cells if c)
        if line:
            self.blocks.append(Block(line, tuple(self.path)))


def _unwrap(elem, wanted: str) -> Iterator:
    """The ``wanted`` children of a table element, also inside content controls."""
    for child in elem:
        local = _local(child, W)
        if local == wanted:
            yield child
        elif local == "sdt":
            content = _child(child, W, "sdtContent")
            if content is not None and not _placeholder(child):
                yield from _unwrap(content, wanted)
        elif local in ("customXml", "ins", "moveTo"):
            yield from _unwrap(child, wanted)


def _deleted_row(row) -> bool:
    """A table row deleted with tracked changes on."""
    props = _child(row, W, "trPr")
    return props is not None and _child(props, W, "del") is not None


def _table_rows(tbl) -> Iterator:
    return (row for row in _unwrap(tbl, "tr") if not _deleted_row(row))


def _row_cells(row) -> Iterator:
    return _unwrap(row, "tc")


def cell_text(tc) -> str:
    """The text of a table cell on one line: its paragraphs, nested tables and text boxes."""
    parts: List[str] = []

    def walk(container) -> None:
        for child in container:
            local = _local(child, W)
            if local == "p":
                boxes: list = []
                text = " ".join(paragraph_text(child, boxes).split())
                if text:
                    parts.append(text)
                for box in boxes:
                    walk(box)
            elif local == "tbl":
                for row in _table_rows(child):
                    for cell in _row_cells(row):
                        walk(cell)
            elif local == "sdt":
                content = _child(child, W, "sdtContent")
                if content is not None and not _placeholder(child):
                    walk(content)
            elif local in ("customXml", "ins", "moveTo"):
                walk(child)
            elif _local(child, MC) == "AlternateContent":
                choice = _first_choice(child)
                if choice is not None:
                    walk(choice)

    walk(tc)
    return " ".join(parts)


def _on(flag) -> bool:
    """A Word on/off property that is present: on unless its w:val says otherwise."""
    return flag is not None and (_attr(flag, W, "val") or "true").lower() not in ("0", "false",
                                                                                 "off")


def _hidden_run(run) -> bool:
    """A run formatted as hidden text (w:vanish), or the gray prompt of a form field left
    empty (character style PlaceholderText: "Click or tap here to enter text.")."""
    props = _child(run, W, "rPr")
    style = _child(props, W, "rStyle")
    if style is not None and (_attr(style, W, "val") or "").lower() == "placeholdertext":
        return True
    return _on(_child(props, W, "vanish"))


def _placeholder(sdt) -> bool:
    """A content control left empty: it shows its placeholder prompt (w:showingPlcHdr),
    e.g. "Click or tap here to enter text." or "Click or tap to enter a date."."""
    return _on(_child(_child(sdt, W, "sdtPr"), W, "showingPlcHdr"))


def paragraph_text(p, boxes: list) -> str:
    """The text of one Word paragraph: its runs (also inside hyperlinks, fields, tracked
    insertions, content controls), without deleted text, field codes and hidden text.
    Text boxes found in it are appended to ``boxes`` (read as paragraphs of their own)."""
    parts: List[str] = []

    def walk(elem) -> None:
        for child in elem:
            local = _local(child, W)
            if local:
                if local == "t":
                    parts.append(child.text or "")
                elif local in ("tab", "ptab"):
                    parts.append(" ")
                elif local in ("br", "cr"):
                    parts.append("\n")
                elif local == "noBreakHyphen":
                    parts.append("-")
                elif local == "sym":
                    parts.append(_symbol(child))
                elif local == "txbxContent":
                    boxes.append(child)
                elif (local in _W_SKIP or (local == "r" and _hidden_run(child))
                      or (local == "sdt" and _placeholder(child))):
                    continue
                else:
                    walk(child)
            elif _local(child, MC) == "AlternateContent":
                choice = _first_choice(child)
                if choice is not None:
                    walk(choice)
            elif _local(child, MATH) == "t":
                parts.append(child.text or "")
            else:
                walk(child)              # drawings, VML shapes, unknown wrappers

    walk(p)
    return "".join(parts)


def _symbol(sym) -> str:
    """w:sym: a character of a symbol font, e.g. F0B7 (a Symbol-font bullet: tidy_text turns
    the private-use bullets into "\N{BULLET}" and drops the other private-use glyphs)."""
    code = _int_hex(_attr(sym, W, "char"))
    if code is None or code < 0x20 or code > 0x10FFFF:
        return ""
    return chr(code)


def _int_hex(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value), 16) if value else None
    except ValueError:
        return None


def _read_body(text: str, reader: _DocxReader) -> None:
    """Feed the body of document.xml to ``reader`` incrementally: every child of w:body once
    it is complete, and the rows of a table that is a child of w:body as soon as each row is
    complete (a document that is one huge table never sits in memory as a whole tree)."""
    parser = ET.XMLPullParser(events=("start", "end"))
    stack: list = []
    body = None
    for pos in range(0, len(text), 1 << 20):
        parser.feed(text[pos:pos + (1 << 20)])
        for event, elem in parser.read_events():
            if event == "start":
                stack.append(elem)
                if (body is None and len(stack) == 2 and _local(elem, W) == "body"
                        and _local(stack[0], W) == "document"):
                    body = elem
                continue
            stack.pop()
            depth = len(stack)
            if depth == 2 and stack[1] is body:      # a paragraph, table, content control
                reader.block(elem)
                body.remove(elem)
            elif (depth == 3 and stack[1] is body and _local(elem, W) == "tr"
                  and _local(stack[2], W) == "tbl" and not _deleted_row(elem)):
                reader.row(elem)                       # (the table's other rows come later)
                stack[2].remove(elem)
    parser.close()


def read_docx(package: Package, main: str) -> Tuple[List[Tuple[str, List[str]]], dict]:
    styles_part = package.related(main, "/styles", posixpath.join(posixpath.dirname(main),
                                                                  "styles.xml"))
    styles = _docx_styles(package.tree(styles_part, required=False))
    reader = _DocxReader(styles)
    text = package.xml_text(main)
    if text is None:
        raise OfficeError(f"the package has no part {main}")
    try:
        _read_body(text, reader)
    except ET.ParseError as exc:
        raise OfficeError(f"{main}: malformed XML ({exc})")
    pages = pack_blocks(reader.blocks)
    return pages, {"printed_title": reader.title or reader.first_heading,
                   "toc_entries": reader.headings}


# ---------------------------------------------------------------------------
# .xlsx
# ---------------------------------------------------------------------------
# built-in number formats that show a date, a date and time, or a time of day (ECMA-376
# 18.8.30; 27-36 and 50-58 are the East Asian date formats)
_BUILTIN_DATES = set(range(14, 18)) | set(range(27, 37)) | set(range(50, 59))
_BUILTIN_DATETIMES = {22}
_BUILTIN_TIMES = set(range(18, 22)) | {45, 46, 47}
_BUILTIN_PERCENTS = {9: 0, 10: 2}                 # "0%", "0.00%": numFmtId -> decimals
_FORMAT_LITERALS = re.compile(r'"[^"]*"|\\.|_.|\*.')
_FORMAT_BRACKETS = re.compile(r"\[[^\]]*\]")
_EXCEL_EPOCH = datetime(1899, 12, 30)
_EXCEL_1904 = datetime(1904, 1, 1)


def date_format_kind(code: str) -> Optional[str]:
    """"date", "datetime" or "time" when a number format shows a date and/or a time of day
    ("m/d/yyyy", "m/d/yy h:mm", "h:mm AM/PM"), else ``None`` (numbers, text, elapsed times
    such as "[h]:mm")."""
    if re.search(r"\[(h+|m+|s+)\]", code or "", re.I):
        return None                      # a duration: the number of hours is clearer
    # quoted text, escaped characters, padding and [colour]/[$-locale] codes are no date parts
    section = _FORMAT_BRACKETS.sub("", _FORMAT_LITERALS.sub("", code or "")).split(";")[0]
    has_date = bool(re.search(r"[dy]", section, re.I))
    has_time = bool(re.search(r"[hs]", section, re.I))
    if has_date or has_time:
        return "datetime" if has_date and has_time else "date" if has_date else "time"
    return "date" if re.search(r"m", section, re.I) else None


def percent_decimals(code: str) -> Optional[int]:
    """The decimals a percentage format shows ("0%" -> 0, "0.00%" -> 2), ``None`` when the
    number format is no percentage (a quoted or escaped "%" is only text)."""
    section = _FORMAT_BRACKETS.sub("", _FORMAT_LITERALS.sub("", code or "")).split(";")[0]
    if "%" not in section:
        return None
    decimals = re.search(r"\.([0#?]+)", section)
    return len(decimals.group(1)) if decimals else 0


def percent_text(value: str, decimals: int = 0) -> str:
    """A fraction as Excel shows it in a percentage format: 0.333 -> "33%" (0 decimals),
    "33.30%" (2 decimals), rounded half away from zero as Excel does."""
    try:
        number = Decimal(str(value).strip()) * 100
        if not number.is_finite():
            raise InvalidOperation
        shown = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return number_text(value)
    return f"{shown if shown else abs(shown):f}%"          # (no "-0%")


def _xlsx_number_styles(package: Package, part: str) -> Dict[int, str]:
    """Cell style index (the ``s`` attribute of a cell) -> "date"/"datetime"/"time" for the
    styles whose number format shows a date or a time, "%0", "%2"... for a percentage with
    that many decimals (styles.xml: numFmts and cellXfs; read incrementally, as some
    workbooks carry tens of thousands of styles)."""
    text = package.xml_text(part)
    if text is None:
        return {}
    custom: Dict[int, str] = {}
    xfs: list = []
    try:
        for section in stream_children(text, [(S, "styleSheet")]):
            local = _local(section, S)
            if local == "numFmts":
                for fmt in _children_named(section, S, "numFmt"):
                    num_id = _int(fmt.get("numFmtId"))
                    if num_id is not None:
                        custom[num_id] = fmt.get("formatCode") or ""
            elif local == "cellXfs":
                xfs = [_int(xf.get("numFmtId")) for xf in _children_named(section, S, "xf")]
    except ET.ParseError:
        return {}                        # damaged styles: numbers stay numbers
    out: Dict[int, str] = {}
    for index, num_id in enumerate(xfs):
        if num_id is None:
            continue
        if num_id in custom:
            decimals = percent_decimals(custom[num_id])
            kind = date_format_kind(custom[num_id]) if decimals is None else f"%{decimals}"
        else:
            kind = ("date" if num_id in _BUILTIN_DATES else
                    "datetime" if num_id in _BUILTIN_DATETIMES else
                    "time" if num_id in _BUILTIN_TIMES else
                    f"%{_BUILTIN_PERCENTS[num_id]}" if num_id in _BUILTIN_PERCENTS else None)
        if kind:
            out[index] = kind
    return out


def number_text(value: str) -> str:
    """A number as Excel shows it by default: "3" (not "3.0"), "0.3", "1.5E-05" -> "1.5e-05"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (value or "").strip()
    if number != number or number in (float("inf"), float("-inf")):
        return value.strip()
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return format(number, ".15g")


def excel_date(value: str, kind: str, date1904: bool = False) -> str:
    """A date/time serial number as ISO text, as much of it as the format ``kind`` shows:
    45910 -> "2025-09-10" (date), 45910.75 -> "2025-09-10 18:00" (datetime), 0.5 -> "12:00"
    (time, or a serial below 1); an impossible serial stays a number."""
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return (value or "").strip()
    if not 0 <= serial < 2958466:
        return number_text(value)
    if date1904:
        base = _EXCEL_1904
    else:   # Excel counts a 29 February 1900 that never was: serials before it are 1 day off
        base = _EXCEL_EPOCH if serial >= 61 else _EXCEL_EPOCH + timedelta(days=1)
    stamp = base + timedelta(seconds=round(serial * 86400))
    clock = stamp.strftime("%H:%M:%S" if stamp.second else "%H:%M")
    if kind == "time" or serial < 1:
        return clock
    day = stamp.date().isoformat()
    midnight = (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0)
    return f"{day} {clock}" if kind == "datetime" and not midnight else day


def rich_text(elem) -> str:
    """Text of a shared string or inline string: <t>, or the <t> of its runs (<r>), without
    phonetic guides (<rPh>)."""
    if elem is None:
        return ""
    parts: List[str] = []
    for child in elem:
        local = _local(child, S)
        if local == "t":
            parts.append(child.text or "")
        elif local == "r":
            parts.extend(t.text or "" for t in _children_named(child, S, "t"))
    return "".join(parts)


def _column(ref: Optional[str]) -> Optional[int]:
    letters = re.match(r"([A-Za-z]{1,3})\d*$", ref or "")
    if not letters:
        return None
    number = 0
    for ch in letters.group(1).upper():
        number = number * 26 + ord(ch) - 64
    return number


class _SheetContext(NamedTuple):
    shared: List[str]
    number_styles: Dict[int, str]      # _xlsx_number_styles
    date1904: bool


def cell_value(cell, ctx: _SheetContext) -> str:
    kind = cell.get("t") or "n"
    v = _child(cell, S, "v")
    raw = v.text if v is not None and v.text is not None else None
    if kind == "s":
        index = _int(raw)
        return ctx.shared[index] if index is not None and 0 <= index < len(ctx.shared) else ""
    if kind == "inlineStr":
        return rich_text(_child(cell, S, "is"))
    if raw is None:
        return ""                        # empty, or a formula without a cached value
    if kind == "str":
        return raw
    if kind == "b":
        return "TRUE" if raw.strip() in ("1", "true") else "FALSE"
    if kind == "e":
        return ""                        # #N/A, #REF!...
    if kind == "d":
        return re.sub(r"T00:00(:00)?(\.0+)?Z?$", "", raw.strip())
    style = ctx.number_styles.get(_int(cell.get("s")) or 0)
    if not style:
        return number_text(raw)
    if style.startswith("%"):
        return percent_text(raw, int(style[1:]))
    return excel_date(raw, style, ctx.date1904)


Cells = List[Tuple[int, str]]     # the non-empty cells of a row: (column number, text)


def row_cells(row, ctx: _SheetContext) -> Cells:
    """The non-empty cells of a row in column order (a cell without a reference follows
    the one before it)."""
    out: Cells = []
    column = 0
    for c in _children_named(row, S, "c"):
        column = _column(c.get("r")) or column + 1
        text = " ".join(cell_value(c, ctx).split())
        if text:
            out.append((column, text))
    return sorted(out, key=lambda item: item[0])


def _shared_strings(package: Package, part: str) -> List[str]:
    text = package.xml_text(part)
    if text is None:
        return []
    try:
        return [rich_text(si) for si in stream_children(text, [(S, "sst")])
                if _local(si, S) == "si"]
    except ET.ParseError as exc:
        raise OfficeError(f"{part}: malformed XML ({exc})")


# "Sheet1" and its translations: the name Excel gives a new sheet says nothing
_DEFAULT_SHEET_NAME = re.compile(r"^(sheet|feuil|tabelle|hoja|foglio|planilha|blad|arkusz)\s*\d*$",
                                 re.I)


def _numeric(text: str) -> bool:
    """A number as a cell shows it: "3", "1,200", "$40", "33%", "(5)"."""
    bare = text.replace(",", "").strip().strip("$%() ")
    if not any(ch.isdigit() for ch in bare):
        return False
    try:
        float(bare)
    except ValueError:
        return False
    return True


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _label_like(cells: Cells) -> bool:
    """A row that can hold column headings: several short text cells, few of them with
    digits ("2026FA Enrl" may be one; "Room 101 | 9 am" is data)."""
    if len(cells) < 2:
        return False
    texts = [t for _c, t in cells]
    if any(_numeric(t) or len(t) > MAX_LABEL_CHARS or len(t.split()) > MAX_LABEL_WORDS
           for t in texts):
        return False
    return sum(_has_digit(t) for t in texts) <= len(texts) // 4


def _coverage(columns, rows: Sequence[Cells]) -> float:
    """The share of the cells of ``rows`` that lie in one of ``columns``."""
    cols = [c for row in rows for c, _t in row]
    return sum(c in columns for c in cols) / len(cols) if cols else 0.0


class _Header(NamedTuple):
    base: int                  # index of the column-headings row
    uppers: Tuple[int, ...]    # rows of group labels right above it ("Women | Men" over "n | %")
    strong: bool               # its labels name the values; else it is only repeated on pages


def _header_test(rows: List[Cells], k: int, current: Optional[Cells]) -> Optional[bool]:
    """Whether row ``k`` holds the column headings of the rows below it (True: label their
    values, False: a two-column heading only repeated on later pages, None: no headings).
    The rows below must lie mostly in its columns; ``current`` (the headings in effect)
    is replaced only by a digit-free row over numbers that fits the rows at least as well."""
    following = [r for r in rows[k + 1:k + 30] if len(r) >= 2][:8]
    columns = {c for c, _t in rows[k]}
    coverage = _coverage(columns, following)
    if not following or coverage < 0.75:
        return None
    numbers = any(_numeric(t) for row in following for c, t in row if c in columns)
    if current is not None:
        if (any(_has_digit(t) for _c, t in rows[k]) or not numbers
                or coverage < _coverage({c for c, _t in current}, following)):
            return None
        return True
    return len(columns) >= 3


def table_headers(rows: List[Cells]) -> Dict[int, _Header]:
    """Where the column headings of a sheet are (row index -> _Header): the first run of
    heading-like rows (_label_like) whose last-tested row fits the rows below it
    (_header_test; earlier rows of the run are group labels), then any later headings."""
    out: Dict[int, _Header] = {}
    current: Optional[Cells] = None     # the strong headings in effect
    weak = False
    i = 0
    while i < len(rows):
        if not _label_like(rows[i]):
            i += 1
            continue
        end = i
        while end + 1 < len(rows) and end - i < MAX_HEADER_LEVELS and _label_like(rows[end + 1]):
            end += 1
        for k in range(i, end + 1):
            if current is not None and rows[k] == current:
                found: Optional[bool] = True             # the headings repeated
            elif current is None and weak:
                found = None                             # (a two-column list: keep the first)
            else:
                found = _header_test(rows, k, current)
            if found is not None:
                uppers = tuple(range(max(i, k - MAX_HEADER_LEVELS), k)) if found else ()
                out[k] = _Header(k, uppers, found)
                if found:
                    current = rows[k]
                else:
                    weak = True
                end = k
                break
        i = end + 1
    return out


def _labels(base: Cells, uppers: Sequence[Cells]) -> Dict[int, Tuple[str, ...]]:
    """Column -> its label path: the group labels above it (a group label covers its column
    and the next ones up to the next label of its row, as a merged cell does) and its
    heading."""
    out: Dict[int, Tuple[str, ...]] = {}
    for column, heading in base:
        path: List[str] = []
        for upper in uppers:
            j = bisect_right([c for c, _t in upper], column) - 1
            if j >= 0 and (not path or path[-1] != upper[j][1]):
                path.append(upper[j][1])
        if not path or path[-1] != heading:
            path.append(heading)
        out[column] = tuple(path)
    return out


def labelled_parts(cells: Cells, labels: Dict[int, Tuple[str, ...]]) -> Cells:
    """The cells of a data row as "label: value" (the group labels shared with the cell
    before are not repeated: "Women, n: 25 | %: 33%"); a cell without heading, a value
    that is a label itself ("Term Credits:") or the heading itself stays as it is."""
    parts: Cells = []
    previous: Tuple[str, ...] = ()
    for column, text in cells:
        path = labels.get(column)
        if not path or text.endswith(":") or text == path[-1]:
            parts.append((column, text))
            previous = ()
            continue
        k = 0
        while k < len(path) - 1 and k < len(previous) and path[k] == previous[k]:
            k += 1
        label = ", ".join(path[k:])
        parts.append((column, f"{label} {text}" if label.endswith(":") else f"{label}: {text}"))
        previous = path
    return parts


class SheetLine(NamedTuple):
    text: str
    context: Tuple[str, ...] = ()   # lines repeated above it when it starts a page
    section: bool = False           # a one-cell text line (a title, section or question)


def sheet_lines(rows: List[Cells]) -> List[SheetLine]:
    """The rows of one sheet as lines. Under strong column headings (table_headers) every
    value is written with its label and the heading rows themselves are left out; other
    rows are "a | b | c". A page that starts inside a table repeats, as context, the last
    one-cell line (a section or a question), a weak heading row and the leading cells of
    the row that opened the group (a label in a merged cell spanning several rows)."""
    headers = table_headers(rows)
    skipped = {j for h in headers.values() if h.strong for j in h.uppers + (h.base,)}
    labels: Dict[int, Tuple[str, ...]] = {}
    heading = ""
    section = ""
    group: Optional[Tuple[int, Cells]] = None     # (first column, parts) of the group row
    out: List[SheetLine] = []
    for i, cells in enumerate(rows):
        header = headers.get(i)
        if header is not None:
            group = None
            labels, heading = {}, ""
            if header.strong:
                labels = _labels(rows[header.base], [rows[j] for j in header.uppers])
        if i in skipped:
            continue
        if len(cells) == 1 and not (_numeric(cells[0][1]) and cells[0][0] in labels):
            out.append(SheetLine(cells[0][1], tuple(c for c in (heading,) if c), True))
            section, group = cells[0][1], None
            continue
        parts = labelled_parts(cells, labels)
        first = cells[0][0]
        lead = ""
        if group is not None and group[0] < first:
            lead = " | ".join(t for c, t in group[1] if c < first)
        elif len(cells) > 1:
            group = (first, parts)
        text = " | ".join(t for _c, t in parts)
        out.append(SheetLine(text, tuple(c for c in (section, heading, lead) if c)))
        if header is not None and not header.strong:
            heading = text                 # (repeated on the next pages, not on its own)
    return out


def sheet_pages(lines: Sequence[SheetLine], name: str, max_words: int = PAGE_MAX_WORDS
                ) -> List[Tuple[str, List[str]]]:
    """The lines of one sheet in pages of at most ``max_words`` words. A page starts with
    the context of its first line (SheetLine.context); one-cell lines at the end of a full
    page move to the next one. A short last page (< 50 words) joins the page before when
    the two stay within 1.25 x ``max_words``. ``toc_path``: the sheet name, unless it is
    a default name ("Sheet1")."""
    pages: List[Tuple[List[str], int]] = []          # (lines, how many are context)
    current: List[Tuple[str, bool]] = []
    n_context = count = 0
    for line in lines:
        for piece in split_long(line.text, max_words):
            n = word_count(piece)
            if current and count + n > max_words:
                carry: List[Tuple[str, bool]] = []
                if not line.section:
                    while len(current) > n_context and current[-1][1]:
                        carry.insert(0, current.pop())
                    if len(current) == n_context:        # (only one-cell lines: they stay)
                        current, carry = current + carry, []
                if len(current) > n_context:
                    pages.append(([t for t, _s in current], n_context))
                kept = {t for t, _s in carry}
                context = [c for c in line.context if c not in kept]
                if sum(word_count(c) for c in context) + sum(word_count(t) for t, _s in carry) \
                        + n > max_words:
                    context = []
                current = [(c, False) for c in context] + carry
                n_context = len(context)
                count = sum(word_count(t) for t, _s in current)
            current.append((piece, line.section))
            count += n
    if len(current) > n_context:
        pages.append(([t for t, _s in current], n_context))
    if len(pages) > 1:
        tail_lines, tail_context = pages[-1]
        tail = tail_lines[tail_context:]
        before = sum(word_count(t) for t in pages[-2][0])
        last = sum(word_count(t) for t in tail)
        if last < 50 and last + before <= 1.25 * max_words:
            pages.pop()
            pages[-1] = (pages[-1][0] + tail, pages[-1][1])
    path = [name] if name and not _DEFAULT_SHEET_NAME.match(name) else []
    return [("\n".join(page), list(path)) for page, _n in pages]


def read_xlsx(package: Package, main: str) -> Tuple[List[Tuple[str, List[str]]], dict]:
    workbook = package.tree(main)
    rels = package.relationships(main)
    folder = posixpath.dirname(main)
    props = _child(workbook, S, "workbookPr")
    date1904 = props is not None and (props.get("date1904") or "").lower() in ("1", "true")
    shared_part = next((r.target for r in rels.values() if r.type.endswith("/sharedStrings")),
                       posixpath.join(folder, "sharedStrings.xml"))
    styles_part = next((r.target for r in rels.values() if r.type.endswith("/styles")),
                       posixpath.join(folder, "styles.xml"))
    ctx = _SheetContext(_shared_strings(package, shared_part),
                        _xlsx_number_styles(package, styles_part), date1904)
    pages: List[Tuple[str, List[str]]] = []
    hidden = sheets = 0
    for sheet in _children_named(_child(workbook, S, "sheets"), S, "sheet"):
        if (sheet.get("state") or "visible") != "visible":
            hidden += 1
            continue
        rel = rels.get(_attr(sheet, R, "id") or "")
        if rel is None or not rel.type.endswith("/worksheet"):
            continue                     # a chart sheet, a dialog or macro sheet
        text = package.xml_text(rel.target)
        if text is None:
            continue
        name = " ".join((sheet.get("name") or "").split())
        try:
            rows = [row_cells(row, ctx) for row in
                    stream_children(text, [(S, "worksheet"), (S, "sheetData")])
                    if _local(row, S) == "row"]
        except ET.ParseError as exc:
            raise OfficeError(f"{rel.target}: malformed XML ({exc})")
        sheets += 1
        pages += sheet_pages(sheet_lines([r for r in rows if r]), name)
    return pages, {"printed_title": None, "toc_entries": len({tuple(p) for _t, p in pages if p}),
                   "sheets": sheets, "hidden_sheets": hidden}


# ---------------------------------------------------------------------------
# .pptx
# ---------------------------------------------------------------------------
_TITLE_PLACEHOLDERS = {"title", "ctrTitle"}
_SKIPPED_PLACEHOLDERS = {"sldNum", "dt", "ftr", "hdr"}


def drawing_paragraphs(body) -> List[str]:
    """The paragraphs of a DrawingML text body (a:p: runs, fields, line breaks)."""
    out = []
    for para in _children_named(body, A, "p"):
        parts: List[str] = []
        for child in para:
            local = _local(child, A)
            if local in ("r", "fld"):
                t = _child(child, A, "t")
                parts.append((t.text or "") if t is not None else "")
            elif local == "br":
                parts.append("\n")
        text = _line("".join(parts))
        if text:
            out.append(text)
    return out


def _drawing_table(tbl) -> List[str]:
    rows = []
    for tr in _children_named(tbl, A, "tr"):
        cells = []
        for tc in _children_named(tr, A, "tc"):
            if (tc.get("hMerge") or tc.get("vMerge") or "").lower() in ("1", "true"):
                continue                 # covered by a merged cell
            text = " ".join(" ".join(drawing_paragraphs(_child(tc, A, "txBody"))).split())
            if text:
                cells.append(text)
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _position(shape) -> Optional[Tuple[int, int]]:
    """(top, left) of a shape from its own transform (a:xfrm in p:spPr or p:grpSpPr, p:xfrm
    of a graphic frame); ``None`` when it has none (a placeholder placed by its layout)."""
    xfrm = _child(shape, P, "xfrm")
    for props in ("spPr", "grpSpPr"):
        xfrm = xfrm if xfrm is not None else _child(_child(shape, P, props), A, "xfrm")
    off = _child(xfrm, A, "off")
    if off is None:
        return None
    top, left = _int(off.get("y")), _int(off.get("x"))
    return (top, left) if top is not None and left is not None else None


def _reading_order(items: List[Tuple[Optional[Tuple[int, int]], List[str]]]) -> List[str]:
    """The lines of the shapes of one shape tree, top to bottom then left to right: the
    shapes with a known position are sorted among their own places in the tree (z-order),
    the others stay where they are (ties keep the z-order)."""
    known = [i for i, (pos, _lines) in enumerate(items) if pos is not None]
    ordered = list(items)
    for i, item in zip(known, sorted((items[i] for i in known), key=lambda it: it[0])):
        ordered[i] = item
    return [line for _pos, lines in ordered for line in lines]


def slide_text(root) -> Tuple[Optional[str], List[str]]:
    """(title, lines) of one slide: the title placeholder first, then the other shapes in
    reading order (_reading_order; a group is read as one block at its own position, its
    shapes in their reading order; tables included)."""
    titles: List[str] = []

    def walk(tree) -> List[Tuple[Optional[Tuple[int, int]], List[str]]]:
        items: List[Tuple[Optional[Tuple[int, int]], List[str]]] = []
        for shape in tree:
            local = _local(shape, P)
            if local == "sp":
                ph = _child(_child(_child(shape, P, "nvSpPr"), P, "nvPr"), P, "ph")
                kind = (ph.get("type") or "body") if ph is not None else ""
                if kind in _SKIPPED_PLACEHOLDERS:
                    continue
                paras = drawing_paragraphs(_child(shape, P, "txBody"))
                if kind in _TITLE_PLACEHOLDERS:
                    titles.extend(paras)
                elif paras:
                    items.append((_position(shape), paras))
            elif local == "grpSp":
                lines = _reading_order(walk(shape))
                if lines:
                    items.append((_position(shape), lines))
            elif local == "graphicFrame":
                for tbl in shape.iter():
                    if _local(tbl, A) == "tbl":
                        rows = _drawing_table(tbl)
                        if rows:
                            items.append((_position(shape), rows))
                        break
            elif _local(shape, MC) == "AlternateContent":
                choice = _first_choice(shape)
                if choice is not None:
                    items.extend(walk(choice))
        return items

    tree = _child(_child(root, P, "cSld"), P, "spTree")
    lines = _reading_order(walk(tree)) if tree is not None else []
    title = " ".join(" ".join(titles).split())[:200] or None
    return title, titles + lines


def read_pptx(package: Package, main: str) -> Tuple[List[Tuple[str, List[str]]], dict]:
    presentation = package.tree(main)
    rels = package.relationships(main)
    pages: List[Tuple[str, List[str]]] = []
    first_title = None
    titled = 0
    for slide_id in _children_named(_child(presentation, P, "sldIdLst"), P, "sldId"):
        rel = rels.get(_attr(slide_id, R, "id") or "")
        if rel is None or not rel.type.endswith("/slide"):
            continue
        if package.info(rel.target) is None:
            pages.append(("", []))       # a slide the package lacks: an empty page (numbering)
            continue
        title, lines = slide_text(package.tree(rel.target))
        if title:
            titled += 1
            first_title = first_title or title
        pages.append(("\n".join(lines), [title] if title else []))
    return pages, {"printed_title": first_title, "toc_entries": titled, "slides": len(pages)}


# ---------------------------------------------------------------------------
# document properties and the whole file
# ---------------------------------------------------------------------------
_W3C_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
                       r"\s*(Z|[+-]\d{2}:?\d{2})?)?$")


def w3c_date(value) -> Optional[str]:
    """dcterms:modified -> ISO 8601: "2021-03-04T15:22:00Z" -> "2021-03-04T15:22:00+00:00";
    ``None`` when missing or implausible (before 1990, in the future: template defaults)."""
    match = _W3C_DATE.match(str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    try:
        stamp = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                         int(second or 0))
    except ValueError:
        return None
    if not 1990 <= stamp.year <= datetime.now().year + 1:
        return None
    if hour is None:
        return stamp.date().isoformat()
    if zone in ("Z", None):
        return stamp.isoformat() + ("+00:00" if zone else "")
    zone = zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    return stamp.isoformat() + zone


def _properties(package: Package) -> dict:
    """Title and modification date (docProps/core.xml) and the application (app.xml)."""
    rels = package.relationships("")
    core_part = next((r.target for r in rels.values() if r.type.endswith("/core-properties")),
                     "docProps/core.xml")
    app_part = next((r.target for r in rels.values() if r.type.endswith("/extended-properties")),
                    "docProps/app.xml")
    out = {"metadata_title": "", "modified": None, "producer": ""}
    core = package.tree(core_part, required=False)
    for child in core if core is not None else ():
        if _local(child, DC) == "title":
            out["metadata_title"] = " ".join((child.text or "").split())
        elif _local(child, DCTERMS) == "modified":
            out["modified"] = w3c_date(child.text)
    app = package.tree(app_part, required=False)
    application = _child(app, APP, "Application") if app is not None else None
    if application is not None:
        out["producer"] = " ".join((application.text or "").split())
    return out


def main_part(package: Package, file_type: str) -> str:
    """The main part named by the package relationships (_rels/.rels), else the usual name."""
    default = MAIN_PARTS[file_type]
    target = next((r.target for r in package.relationships("").values()
                   if r.type.endswith("/officeDocument")), None)
    return target if target and package.info(target) is not None else default


_READERS = {DOCX: read_docx, XLSX: read_xlsx, PPTX: read_pptx}


def extract_office(path: Path, file_type: str,
                   budget: int = MAX_UNCOMPRESSED) -> Tuple[List[str], List[List[str]], dict]:
    """(page texts, toc_path of every page, facts) of a .docx/.xlsx/.pptx file. The texts
    are raw (documents.py cleans them like PDF text). A file without any text gives one
    empty page. Raises OfficeError when the file cannot be read safely."""
    if file_type not in _READERS:
        raise OfficeError(f"not an Office Open XML type: {file_type!r}")
    with Package(path, budget=budget) as package:
        main = main_part(package, file_type)
        if package.info(main) is None:
            raise OfficeError(f"the package has no {MAIN_PARTS[file_type]}")
        try:
            pages, facts = _READERS[file_type](package, main)
            facts.update(_properties(package))
        except RecursionError:
            raise OfficeError("XML nested too deeply to read") from None
        facts["bytes_read"] = package.used
    if not pages:
        pages = [("", [])]
    return [t for t, _p in pages], [list(p) for _t, p in pages], facts


__all__ = ["DOCX", "XLSX", "PPTX", "OFFICE_TYPES", "MAIN_PARTS", "TYPE_LABELS", "NOT_OFFICE",
           "ENCRYPTED", "OfficeError", "Package", "check_file", "looks_like_office",
           "extract_office", "decode_xml", "pack_blocks", "split_long", "date_format_kind",
           "excel_date", "number_text", "w3c_date", "Block"]
