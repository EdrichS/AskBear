"""Small helpers shared by every module: text cleaning, hashing, JSONL I/O, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from html import unescape as html_unescape
from html.entities import html5 as html5_entities
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from bs4 import BeautifulSoup

log = logging.getLogger("askbear")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with lxml (fast), falling back to Python's built-in parser.

    Note: catalog.nyit.edu pages are slightly malformed, so lxml places the <main>
    element under <head>. Always locate content with CSS selectors such as
    ``soup.select_one("div.Main__content")`` instead of walking ``soup.body``.
    """
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml not installed or failed: use the stdlib parser
        return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_SPACES = re.compile(r"[ \t\f\v  -   　]+")
_BLANK_LINES = re.compile(r"\n{3,}")
# C1 control characters that are Windows-1252 punctuation decoded as Latin-1 ("don\x92t"):
# quotes, dashes, bullets and ellipses; the other C1 codes are glyphs of symbol fonts (the
# Vancouver flyers use \x88 as a bullet) or junk and are dropped with the control characters
CP1252_PUNCTUATION = {c: bytes([c]).decode("cp1252") for c in (
    0x82, 0x84, 0x85, 0x8B, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x9B)}
# control characters left in a source (NYIT's HTML has a stray U+009D where a quote was
# mangled); tab, newline, \v, \f and \r are white space and handled below
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")


def clean_text(text: str) -> str:
    """Normalise text for indexing.

    * control characters are dropped, except the C1 ones that are Windows-1252 punctuation
      ("\\x92" -> "’", CP1252_PUNCTUATION); run fix_mojibake first: it needs them
    * NFKC unicode normalisation (turns PDF ligatures such as "ﬁ" into "fi")
    * removes zero-width characters and soft hyphens
    * collapses runs of spaces/tabs/non-breaking spaces into one space
    * strips every line and keeps at most one blank line between paragraphs
    """
    if not text:
        return ""
    text = _CONTROLS.sub("", text.translate(CP1252_PUNCTUATION))
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", text).strip()


# HTML entities escaped twice in the source ("Researcher &amp;amp; Author" shows as "&amp;",
# "New York Tech&amp;rsquo;s" as "&rsquo;"): any named HTML entity, or a numeric one
_ENTITY_CANDIDATE = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{2,6}|#[xX][0-9a-fA-F]{2,5});")
# misspelt entities in NYIT's pages ("r&eaute;sumé" in the student handbook)
_ENTITY_TYPOS = {"&eaute;": "&eacute;"}


def _is_entity(ref: str) -> bool:
    ref = _ENTITY_TYPOS.get(ref, ref)
    return ref[1] == "#" or ref[1:] in html5_entities


def has_leftover_entity(text: str) -> bool:
    return "&" in text and any(_is_entity(m.group(0)) for m in _ENTITY_CANDIDATE.finditer(text))


def unescape_leftovers(text: str) -> str:
    """Decode HTML entities left in the text ("&rsquo;" -> "’"); "AT&T;" stays as it is."""
    def decode(m) -> str:
        ref = m.group(0)
        return html_unescape(_ENTITY_TYPOS.get(ref, ref)) if _is_entity(ref) else ref

    for _ in range(2):  # "&amp;amp;" was escaped three times
        new = _ENTITY_CANDIDATE.sub(decode, text)
        if new == text:
            break
        text = new
    return text


# UTF-8 text that was decoded as Windows-1252 somewhere before it was published
# ("pp. 109â€‘127" for "pp. 109‑127", "Ã©" for "é"): a lead byte followed by continuation bytes
_MOJIBAKE = re.compile("[\u00c2-\u00f4][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d"
                       "\u017e\u0192\u02c6\u02dc\u2013\u2014\u2018-\u201e\u2020-\u2022"
                       "\u2026\u2030\u2039\u203a\u20ac\u2122]{1,3}")


def has_mojibake(text: str) -> bool:
    return (("\u00c3" in text or "\u00c2" in text or "\u00e2\u20ac" in text)
            and _MOJIBAKE.search(text) is not None)


def fix_mojibake(text: str) -> str:
    """Re-decode Windows-1252 mojibake of UTF-8 ("â€™" -> "’"); anything that does not
    decode to valid UTF-8 is left as it is. Run it before clean_text (NFKC turns "™" into
    "TM", after which "â€TM" can no longer be repaired)."""
    def repair(m) -> str:
        found = m.group(0)
        lead = ord(found[0])
        size = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        try:
            # characters below 256 are their own byte (Latin-1, which also covers the C1
            # controls a PDF may hold); the others must be Windows-1252 punctuation ("€", "™")
            raw = b"".join(bytes([ord(c)]) if ord(c) < 256 else c.encode("cp1252")
                           for c in found[:size])
            return raw.decode("utf-8") + found[size:]
        except UnicodeError:
            return found

    return _MOJIBAKE.sub(repair, text)


def word_count(text: str) -> int:
    """Whitespace word count (the same method used for the proposal's numbers)."""
    return len(text.split()) if text else 0


# ---------------------------------------------------------------------------
# Hashing / ids / time
# ---------------------------------------------------------------------------
def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 10) -> str:
    return sha1(text)[:length]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# JSON Lines
# ---------------------------------------------------------------------------
def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield one dict per non-empty line. Skips a truncated last line (crash-safe)."""
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping unreadable line %d in %s", lineno, path)


def write_jsonl(path: Path, records: Iterable[Any]) -> int:
    """Atomically write records (dicts or objects with ``to_dict()``). Returns the count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            if hasattr(rec, "to_dict"):
                rec = rec.to_dict()
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1
    os.replace(tmp, path)
    return count


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def read_json_file(path: Path) -> Any:
    """Parsed JSON file, or None when it is missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False, log_file: Path = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # requests/urllib3 are noisy at DEBUG level
    logging.getLogger("urllib3").setLevel(logging.WARNING)
