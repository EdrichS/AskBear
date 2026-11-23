"""Course directory parser: one ``/courses?cs=<SUBJECT>&cal=<UG|GR|PROF>`` page -> CourseRecords.

catalog.nyit.edu lists every course of a subject on a single page::

    <select class="courseDD">                       the subject dropdown ("CSCI - Computer Science")
    <table id="course_catalog_table">               Name | Title | Credits | School
      <tr>  a.viewCourseInfo[data-course="CSCI-110"] | title | "3.0" | "College of Eng & Comp Sciences"
      <tr class="hideCourseInfo CSCI-110"><td>      the hidden detail row:
              description<br><br>
              <strong>Prerequisite Course(s): </strong>Prerequisite: MATH 136 ...<br><br>
              <strong>Classroom Hours - Laboratory and/or Studio Hours – Course Credits: </strong>3-0-3
              <strong>Inst Cnst</strong>            (a bare flag: instructor consent required)

Quirks handled (all of them occur in the 2026-2027 catalog):

* A page can list other subjects too (the undergraduate ARCH page also lists AAID and DSGN
  courses), so the subject is always taken from the course code, never from the URL.
* The same code can appear several times on one page. Identical rows are dropped, rows with the
  same title are merged (e.g. "LDB 505" listed once with 4.0 and once with 4.5 credits), rows with
  different titles are different courses ("MTIM 893" is "Cardiology IV", "Infectious Disease IV",
  ...) and get the course ids "MTIM 893|PROF", "MTIM 893|PROF|2", ...
* There is no corequisite field: corequisites are written inside the prerequisite value
  ("Prerequisite: ARCH 602. Corequisite: ARCH 705"), with many spellings ("Co-requisite",
  "Corequisties", "Coerequisites", ...), so that value is split in two.
* Over 200 descriptions end with the hours sentence ("Classroom Hours- Laboratory and/or Studio
  Hours- Course Credits: 3-0-3"). It is removed from the description and used as ``hours`` when the
  labelled field is empty.
* Empty descriptions (most College of Osteopathic Medicine courses), labels with empty values, and
  codes such as "ARCH 701B", "OM 9600", "OM AP01", "BLFA BILLN1" or "NURS RNLC".
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .models import CourseRecord
from .urls import normalize_url
from .utils import clean_text, make_soup

log = logging.getLogger("askbear.courses")

# the ``cal`` URL parameter -> CourseRecord.level
LEVELS: Dict[str, str] = {"UG": "undergraduate", "GR": "graduate", "PROF": "professional"}

# School names as printed in the course table -> official names (as in config.SCHOOL_BY_PATH).
# Only used in the searchable text, so a query for "engineering" also finds "Eng & Comp" courses.
SCHOOL_NAMES: Dict[str, str] = {
    "College of Arts & Sciences": "College of Arts and Sciences",
    "College of Eng & Comp Sciences": "College of Engineering and Computing Sciences",
    "College of Osteopathic Med": "College of Osteopathic Medicine",
    "School of Arch & Design": "School of Architecture and Design",
    "School of Health Professions": "School of Health Professions",
    "School of Management": "School of Management",
}

# Labels printed without a value, and what they mean. The courses flagged "Inst Cnst" say
# "Instructor approval/permission is required" in their descriptions.
FLAG_LABELS: Dict[str, str] = {"inst cnst": "Instructor consent required"}

_KNOWN_LABEL_WORDS = ("prerequisite", "corequisite", "classroom hours", "credits")
_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "select", "button", "form"}

_HOURS_VALUE = r"\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?"
_HOURS_RE = re.compile(_HOURS_VALUE)
# The hours sentence repeated inside descriptions, e.g. "Classroom Hours- Laboratory and/or Studio
# Hours- Course Credits: 3-0-3", "... Studio Hours 3-0-3" or "...protection.- Course Credits: 3-0-3"
_HOURS_SENTENCE = re.compile(
    r"(?:\bClassroom\s*Hours?\b.{0,80}?|(?<=[.;])\s*[-–—]\s*Course\s+)"
    r"(?:\bCredits?\b|\bHours\b)\s*:?\s*(?P<value>" + _HOURS_VALUE + r")\.*",
    re.IGNORECASE,
)
_HOURS_SENTENCE_NO_VALUE = re.compile(r"\bClassroom\s*Hours?\b.{0,80}?\bCredits?\b\s*:?\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?|\.\d+")

# Requisite markers. Prerequisite/corequisite spellings seen on the site include "Prerequsites",
# "Prerequistes", "Prerequisite;", "Co-requisite", "Co- requisite :", "Corequisties",
# "Corequsite", "Corequiste" and "Coerequisites".
_PRE_MARK = re.compile(r"\bpre[\s-]*req[a-z]*\s*[:;]", re.IGNORECASE)
_PRE_LEAD = re.compile(r"^\s*pre[\s-]*req[a-z]*(?:\s*[:;]\s*|\s+(?=[A-Z]{2,6}\s*\d))", re.IGNORECASE)
_CO_MARK = re.compile(r"\bco[\s-]*e?req[a-z]*\s*:", re.IGNORECASE)
_PRE_OR_CO = re.compile(r"\bpre[\s-]*(?:or|and|/)[\s-]*$", re.IGNORECASE)  # "Pre- or Co-requisite:"
# An enrollment restriction written after the corequisites ("Co-requisite: NURS 360. Course is
# limited to Nursing students.") is a requirement, not a corequisite: it goes to prerequisites.
_RESTRICTION = re.compile(
    r"(?<=[.;])\s+(?=(?:Course is|This course is|Only|All others|Students (?:in|who|must)|"
    r"Limited to|Open to|Restricted to|Permission|Instructor)\b)"
)
_DANGLING = re.compile(r"(?:\s*(?:,|;|:|&|\band\b|\bor\b))+\s*$", re.IGNORECASE)
_SPACE_BEFORE_COLON = re.compile(r"\s+:\s*")


# ---------------------------------------------------------------------------
# URLs and subjects
# ---------------------------------------------------------------------------
def _query(url: str) -> Dict[str, str]:
    return {k.lower(): v.strip() for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)}


def is_course_listing(url: str) -> bool:
    """True for a subject listing such as ``https://catalog.nyit.edu/courses?cal=UG&cs=CSCI``."""
    if not url:
        return False
    path = (urlsplit(url).path or "/").rstrip("/").lower()
    query = _query(url)
    return path == "/courses" and bool(query.get("cs")) and bool(query.get("cal"))


def listing_params(url: str) -> Tuple[str, str]:
    """``(subject, level code)`` of a listing URL, e.g. ``("CSCI", "UG")``; empty strings if absent."""
    query = _query(url)
    return query.get("cs", "").upper(), query.get("cal", "").upper()


def canonical_listing_url(url: str) -> str:
    """The listing URL with only its ``cal`` and ``cs`` parameters. Semester maps link to
    ``/courses?cal=GR&cc=PHAS-601&cs=PHAS``; ``cc`` only unfolds one course in the browser, so all
    those URLs are the same page as ``/courses?cal=GR&cs=PHAS``."""
    parts = urlsplit(url)
    params = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k.lower() in ("cal", "cs"))
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
    return normalize_url(rebuilt) or rebuilt


def course_link(course: CourseRecord) -> str:
    """Deep link that opens the listing page with this course's description unfolded and scrolled
    into view (the page's JavaScript reads ``cc``), e.g.
    ``https://catalog.nyit.edu/courses?cal=UG&cc=CSCI-260&cs=CSCI``.

    Built by hand: ``normalize_url`` drops ``cc`` (config.TRACKING_PARAMS) because for crawling
    every ``cc`` value is the same page."""
    if not course.url or not course.code:
        return course.url
    parts = urlsplit(canonical_listing_url(course.url))
    params = parse_qsl(parts.query, keep_blank_values=True)
    params.append(("cc", course.code.replace(" ", "-")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(params)), ""))


def subject_names(soup_or_html: Union[str, bytes, BeautifulSoup, Tag]) -> Dict[str, str]:
    """``{"CSCI": "Computer Science", ...}`` from the ``select.courseDD`` dropdown of a listing page."""
    soup = make_soup(soup_or_html) if isinstance(soup_or_html, (str, bytes)) else soup_or_html
    select = soup.select_one("select.courseDD")
    names: Dict[str, str] = {}
    if select is None:
        return names
    for opt in select.find_all("option"):
        code = (opt.get("value") or "").strip().upper()
        if not code:
            continue
        text = _one_line(opt.get_text(" "))
        if text.upper().startswith(code):  # "CSCI - Computer Science"
            text = text[len(code):].lstrip(" -–:")
        names[code] = text.strip()
    return names


def canonical_school(printed: str) -> str:
    """Official school name for a name printed in the course table ("School of Arch & Design" ->
    "School of Architecture and Design"); unknown names are returned unchanged."""
    if not printed:
        return ""
    for short, full in SCHOOL_NAMES.items():
        if printed.casefold() == short.casefold():
            return full
    return printed


# ---------------------------------------------------------------------------
# field parsers (public: small, pure, easy to unit-test)
# ---------------------------------------------------------------------------
def _one_line(text: str) -> str:
    return " ".join(clean_text(text).split())


def parse_credits(text: str) -> Tuple[Optional[float], Optional[float]]:
    """``"3.0"`` -> (3.0, 3.0); ``"1.0-3.0"``, ``"1.0 - 6.0"``, ``"1 to 3"`` -> (1.0, 3.0);
    ``"Variable"`` or ``""`` -> (None, None)."""
    numbers = [float(n) for n in _NUMBER.findall(text or "")]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def normalize_hours(value: str) -> Optional[str]:
    """Classroom-lab-credits hours: ``"3-0-3"``, ``"1 - 2 - 1"`` and ``"3-0-3."`` -> "3-0-3".
    Text without a class-lab-credit triple is returned cleaned, empty text gives None."""
    value = _one_line(value or "")
    if not value:
        return None
    match = _HOURS_RE.search(value)
    if match:
        return re.sub(r"\s*[-–—]\s*", "-", match.group(0))
    return value


def _tidy(text: str) -> str:
    """Strip separators and dangling "and"/"or" left over after cutting at a marker."""
    text = " ".join(text.split()).strip(" ,;:")
    return _DANGLING.sub("", text).strip(" ,;:")


def split_requisites(value: str) -> Tuple[Optional[str], Optional[str]]:
    """Split the "Prerequisite Course(s)" value into (prerequisites, corequisites).

    ``"Prerequisite: ARCH 602. Corequisite: ARCH 705"`` -> ("ARCH 602.", "ARCH 705")
    ``"Corequisite: MATH 141 or higher"``               -> (None, "MATH 141 or higher")
    ``"Course is limited to juniors and seniors"``     -> ("Course is limited to juniors and seniors", None)
    ``"Co-requisite: NURS 351. Course is limited to Nursing students."``
                                    -> ("Course is limited to Nursing students.", "NURS 351.")
    """
    text = _one_line(value or "")
    if not text:
        return None, None
    text = _PRE_LEAD.sub("", text, count=1)
    marks = []
    for m in _CO_MARK.finditer(text):
        if not _PRE_OR_CO.search(text[max(0, m.start() - 12):m.start()]):
            marks.append((m.start(), m.end(), "co"))
    marks.extend((m.start(), m.end(), "pre") for m in _PRE_MARK.finditer(text))
    marks.sort()

    parts: Dict[str, List[str]] = {"pre": [], "co": []}
    kind, pos = "pre", 0
    for start, end, mark in marks:
        if start < pos or (mark == "pre" and kind == "pre"):
            continue  # a "Prerequisite:" inside prerequisite text stays as written
        parts[kind].append(text[pos:start])
        kind, pos = mark, end
    parts[kind].append(text[pos:])
    pre = _tidy(" ".join(parts["pre"]))
    co = _tidy(" ".join(parts["co"]))
    restriction = _RESTRICTION.search(co)
    if restriction:
        pre = " ".join(filter(None, (pre, co[restriction.end():].strip())))
        co = _tidy(co[:restriction.start()])
    return pre or None, co or None


def strip_hours_sentence(description: str) -> Tuple[str, Optional[str]]:
    """Remove the "Classroom Hours- Laboratory and/or Studio Hours- Course Credits: 3-0-3" sentence
    that many descriptions repeat. Returns (description, hours found in it or None)."""
    found = [normalize_hours(m.group("value")) for m in _HOURS_SENTENCE.finditer(description)]
    text = _HOURS_SENTENCE.sub(" ", description)
    text = _HOURS_SENTENCE_NO_VALUE.sub(" ", text)  # the same sentence without a value, at the end
    return " ".join(text.split()), (found[0] if found else None)


def _trailing_requisites(description: str) -> Tuple[str, Optional[str]]:
    """A description whose last sentences are "Prerequisite: ..." / "Corequisite: ..." (used
    only when the course has no prerequisite field): move that tail out of the description.
    Returns (description, requisite text or None)."""
    head, sep, last = description.rpartition("\n")
    marks = sorted(list(_PRE_MARK.finditer(last)) + list(_CO_MARK.finditer(last)),
                   key=lambda m: m.start())
    for m in marks:
        before, tail = last[:m.start()].rstrip(), last[m.start():]
        if before and before[-1] in ".!?;)" and len(tail.split()) <= 40:
            return head + sep + before, tail
    return description, None


# ---------------------------------------------------------------------------
# detail cell
# ---------------------------------------------------------------------------
def _flatten(node: Tag, out: List[Tuple[str, str]]) -> None:
    """Flatten the detail cell into ("text", s), ("br", "") and ("strong", s) tokens."""
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            out.append(("text", str(child)))
        elif isinstance(child, Tag):
            name = (child.name or "").lower()
            if name in _SKIP_TAGS:
                continue
            if name == "br":
                out.append(("br", ""))
            elif name in ("strong", "b"):
                out.append(("strong", child.get_text(" ")))
            elif name in _BLOCK_TAGS:
                out.append(("br", ""))
                _flatten(child, out)
                out.append(("br", ""))
            else:
                _flatten(child, out)


def _is_label(tokens: List[Tuple[str, str]], i: int, after_label: bool) -> bool:
    """A <strong> is a field label when it starts a line and either ends with ":", is a known
    label, stands alone on its line (a flag such as "Inst Cnst") or follows another field.
    Bold words inside a sentence or at the start of the description stay part of the text."""
    for kind, text in reversed(tokens[:i]):
        if kind == "br":
            break
        if text.strip():
            return False
    label = _one_line(tokens[i][1])
    if not label:
        return False
    if after_label or label.endswith(":") or any(w in label.lower() for w in _KNOWN_LABEL_WORDS):
        return True
    if label.lower() in FLAG_LABELS:
        return True
    for kind, text in tokens[i + 1:]:
        if kind == "br" or kind == "strong":
            return True
        if text.strip():
            return text.strip().startswith(":")
    return True


def parse_detail(td: Tag) -> Tuple[str, List[Tuple[str, str]]]:
    """Split a detail cell into (description, [(label, value), ...]).

    The description is the text before the first label; each value runs until the next label.
    Labels lose their trailing colon, values are cleaned and kept on one line. A bare flag (a
    label without colon and value, e.g. "Inst Cnst") gets its meaning from FLAG_LABELS as value
    ("Yes" for unknown flags); a label printed with a colon but no value keeps the value ""."""
    tokens: List[Tuple[str, str]] = []
    _flatten(td, tokens)
    description: List[str] = []
    fields: List[Tuple[str, List[str]]] = []
    for i, (kind, text) in enumerate(tokens):
        if kind == "strong" and _is_label(tokens, i, bool(fields)):
            fields.append((_one_line(text), []))
            continue
        target = fields[-1][1] if fields else description
        target.append("\n" if kind == "br" else text)

    pairs: List[Tuple[str, str]] = []
    for raw, parts in fields:
        label = raw.rstrip(":").strip()
        value = _one_line("".join(parts)).lstrip(":").strip()
        is_flag = not value and not raw.endswith(":")
        if is_flag and label.lower() in FLAG_LABELS:
            value = FLAG_LABELS[label.lower()]
        elif is_flag and not any(w in label.lower() for w in _KNOWN_LABEL_WORDS):
            value = "Yes"
        pairs.append((label, value))
    return _one_line("".join(description)), pairs


# ---------------------------------------------------------------------------
# listing page
# ---------------------------------------------------------------------------
@dataclass
class _Row:
    """One summary row (+ its detail row) before it becomes a CourseRecord."""

    key: str                      # data-course, e.g. "CSCI-110"
    code: str
    subject: str
    number: str
    title: str
    credits: str
    school: str
    description: str = ""
    fields: List[Tuple[str, str]] = field(default_factory=list)


def _split_code(code_text: str, key: str) -> Tuple[str, str]:
    """("CSCI", "110") from the printed code "CSCI 110", falling back to data-course "CSCI-110"."""
    for text, sep in ((code_text, None), (key, "-")):
        parts = text.split(sep, 1) if text else []
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip().upper(), parts[1].strip().upper()
    m = re.match(r"^([A-Za-z]+)\s*(\S+)$", code_text or key or "")
    if m:
        return m.group(1).upper(), m.group(2).upper()
    return (code_text or key or "").upper(), ""


def _column_index(table: Tag) -> Dict[str, int]:
    """Column positions from the table header (Name | Title | Credits | School)."""
    index = {"name": 0, "title": 1, "credits": 2, "school": 3}
    headers = [_one_line(th.get_text(" ")).lower() for th in table.select("thead th")]
    for i, h in enumerate(headers):
        for col in index:
            if h.startswith(col) or (col == "name" and h in ("code", "course")):
                index[col] = i
    return index


def _summary_row(tr: Tag, link: Tag, cols: Dict[str, int]) -> _Row:
    cells = tr.find_all("td", recursive=False)

    def cell(col: str) -> str:
        i = cols[col]
        return _one_line(cells[i].get_text(" ")) if i < len(cells) else ""

    key = (link.get("data-course") or "").strip()
    subject, number = _split_code(cell("name"), key)
    title = _SPACE_BEFORE_COLON.sub(": ", cell("title"))  # "Clinical Clerkship :Surgery"
    return _Row(key=key, code=f"{subject} {number}".strip(), subject=subject, number=number,
                title=title, credits=cell("credits"), school=cell("school"))


def _detail_key(tr: Tag) -> Optional[str]:
    classes = tr.get("class") or []
    if "hideCourseInfo" not in classes:
        return None
    keys = [c for c in classes if c != "hideCourseInfo"]
    keys.sort(key=lambda c: "-" not in c)  # the course key looks like "CSCI-110"
    return keys[0] if keys else ""


def _table_rows(table: Tag, url: str) -> List[_Row]:
    """Pair every summary row with its detail row (normally the next row, else the first
    unused detail row with the same data-course key)."""
    cols = _column_index(table)
    items: List[Tuple[str, str, Tag, Optional[Tag]]] = []  # (kind, key, tr, summary link)
    for tr in table.find_all("tr"):
        dkey = _detail_key(tr)
        if dkey is not None:
            items.append(("detail", dkey, tr, None))
            continue
        link = tr.select_one("a.viewCourseInfo[data-course]")
        if link is not None:
            items.append(("summary", (link.get("data-course") or "").strip(), tr, link))

    used = set()
    rows: List[_Row] = []
    for i, (kind, key, tr, link) in enumerate(items):
        if kind != "summary":
            continue
        row = _summary_row(tr, link, cols)
        detail = None
        nxt = items[i + 1] if i + 1 < len(items) else None
        if nxt is not None and nxt[0] == "detail" and nxt[1] == key and (i + 1) not in used:
            detail = i + 1
        else:
            detail = next((j for j, it in enumerate(items)
                           if it[0] == "detail" and it[1] == key and j not in used), None)
        if detail is None:
            log.debug("%s: no detail row for %s", url, key)
        else:
            used.add(detail)
            td = items[detail][2].find("td") or items[detail][2]
            row.description, row.fields = parse_detail(td)
        rows.append(row)
    orphans = [it[1] for j, it in enumerate(items) if it[0] == "detail" and j not in used]
    if orphans:
        log.warning("%s: %d detail rows without a summary row: %s", url, len(orphans), orphans[:5])
    return rows


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()


def _similar(a: str, b: str) -> bool:
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9


def _credit_sort_key(credits: str) -> Tuple[float, str]:
    low, _ = parse_credits(credits)
    return (low if low is not None else float("inf"), credits)


def _merge(rows: List[_Row]) -> _Row:
    """Merge rows that share code and title (the same course listed twice, e.g. with 4.0 and
    with 4.5 credits). The row with the longest description comes first; other descriptions are
    added as extra paragraphs unless they are (nearly) the same; every distinct labelled value is
    kept; distinct credit values are joined with "or" ("4.0 or 4.5")."""
    primary = max(rows, key=lambda r: len(r.description))  # first one on ties
    ordered = [primary] + [r for r in rows if r is not primary]
    descriptions: List[str] = []
    fields: List[Tuple[str, str]] = []
    for r in ordered:
        if r.description and not any(_similar(r.description, d) for d in descriptions):
            descriptions.append(r.description)
        fields += [f for f in r.fields if f[1] and f not in fields]
    credits = sorted({r.credits for r in rows if r.credits}, key=_credit_sort_key)
    return _Row(key=primary.key, code=primary.code, subject=primary.subject,
                number=primary.number, title=primary.title, credits=" or ".join(credits),
                school=primary.school, description="\n".join(descriptions), fields=fields)


def _group_variants(rows: List[_Row]) -> List[List[_Row]]:
    """Group rows by code, then by title: one group per distinct course, in page order."""
    by_code: "OrderedDict[str, OrderedDict[str, List[_Row]]]" = OrderedDict()
    for row in rows:
        titles = by_code.setdefault(row.code, OrderedDict())
        titles.setdefault(_norm_title(row.title), []).append(row)
    return [group for titles in by_code.values() for group in titles.values()]


def _attributes(fields: List[Tuple[str, str]]) -> Dict[str, str]:
    """Labelled fields -> {label: value}. Labels printed without a value are dropped; a label
    that occurs twice keeps both values ("a; b")."""
    attrs: Dict[str, str] = {}
    for label, value in fields:
        if not label or not value:
            continue
        if label in attrs and value not in attrs[label].split("; "):
            attrs[label] = f"{attrs[label]}; {value}"
        else:
            attrs[label] = value
    return attrs


def course_text(c: CourseRecord) -> str:
    """The ready-to-index text block of a course, e.g.::

        CSCI 260: Data Structures
        Computer Science (CSCI), undergraduate course, 3.0 credits, College of Engineering and Computing Sciences.
        <description>
        Prerequisites: CSCI 185 ...
        Hours (class-lab-credits): 3-0-3
    """
    subject = f"{c.subject_name} ({c.subject})" if c.subject_name else c.subject
    facts = [subject, f"{c.level} course" if c.level else "course"]
    if c.credits:
        facts.append(f"{c.credits} credits")
    if c.school:
        facts.append(canonical_school(c.school))
    lines = [f"{c.code}: {c.title}" if c.title else c.code, ", ".join(facts) + "."]
    if c.description:
        lines.append(c.description)
    if c.prerequisites:
        lines.append(f"Prerequisites: {c.prerequisites}")
    if c.corequisites:
        lines.append(f"Corequisites: {c.corequisites}")
    if c.hours:
        lines.append(f"Hours (class-lab-credits): {c.hours}")
    for label, value in c.attributes.items():
        low = label.lower()
        if "requisite" in low or "classroom hours" in low:
            continue  # already shown above
        if low in FLAG_LABELS:
            lines.append(value + ".")
        else:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _is_hours_label(label: str) -> bool:
    low = label.lower()
    return "classroom hours" in low or ("hours" in low and "credit" in low)


def _join_unique(values: List[str], sep: str) -> Optional[str]:
    return sep.join(OrderedDict.fromkeys(v for v in values if v)) or None


def _to_record(row: _Row, course_id: str, subjects: Dict[str, str], level_code: str,
               url: str, catalog_year: str) -> CourseRecord:
    prereq_values = [v for k, v in row.fields if v and "prerequisite" in k.lower()]
    coreq_values = [v for k, v in row.fields
                    if v and "corequisite" in k.lower() and "prerequisite" not in k.lower()]
    hours_values = [v for k, v in row.fields if v and _is_hours_label(k)]

    paragraphs = [strip_hours_sentence(p) for p in row.description.split("\n") if p.strip()]
    description = "\n".join(text for text, _ in paragraphs if text)
    if not prereq_values and not coreq_values and description:
        description, tail = _trailing_requisites(description)
        if tail:
            prereq_values.append(tail)

    pres: List[str] = []
    cos = [_tidy(_CO_MARK.sub("", _one_line(v), count=1)) for v in coreq_values]
    for value in prereq_values:
        pre, co = split_requisites(value)
        pres.append(pre or "")
        cos.append(co or "")

    # "3-0-3"; two listings of the same course with different hours give "0-4-2 or 0-8-2"
    triples = [normalize_hours(m.group(0)) or "" for v in hours_values for m in _HOURS_RE.finditer(v)]
    hours = (_join_unique(triples, " or ")
             or next((h for h in (normalize_hours(v) for v in hours_values) if h), None)
             or next((h for _, h in paragraphs if h), None))
    low, high = parse_credits(row.credits)
    course = CourseRecord(
        course_id=course_id,
        code=row.code,
        subject=row.subject,
        subject_name=subjects.get(row.subject, ""),
        number=row.number,
        title=row.title,
        credits=row.credits,
        credits_min=low,
        credits_max=high,
        school=row.school,
        level=LEVELS.get(level_code, level_code.lower()),
        level_code=level_code,
        description=description,
        prerequisites=_join_unique(pres, "; "),
        corequisites=_join_unique(cos, "; "),
        hours=hours,
        attributes=_attributes(row.fields),
        text="",
        url=url,
        catalog_year=catalog_year,
    )
    course.text = course_text(course)
    return course


def parse_course_listing(html: str, url: str, catalog_year: str = "") -> List[CourseRecord]:
    """Parse one subject listing page into CourseRecords (in page order, duplicates merged).

    ``CourseRecord.url`` is the listing URL without client-side parameters
    (see canonical_listing_url); the level comes from the ``cal`` parameter."""
    soup = make_soup(html)
    table = soup.select_one("table#course_catalog_table")
    if table is None:
        log.warning("%s: no course table found", url)
        return []
    _, level_code = listing_params(url)
    url = canonical_listing_url(url)
    if level_code not in LEVELS:
        log.warning("%s: unknown course level %r (expected one of %s)",
                    url, level_code, ", ".join(LEVELS))
    subjects = subject_names(soup)

    rows = _table_rows(table, url)
    courses: List[CourseRecord] = []
    seen_codes: Counter = Counter()
    for group in _group_variants(rows):
        row = _merge(group) if len(group) > 1 else group[0]
        seen_codes[row.code] += 1
        course_id = f"{row.code}|{level_code}"
        if seen_codes[row.code] > 1:  # same code, different title: a different course
            course_id += f"|{seen_codes[row.code]}"
        courses.append(_to_record(row, course_id, subjects, level_code, url, catalog_year))
    if not courses:
        log.warning("%s: no courses found", url)
    else:
        log.debug("%s: %d courses (%d rows)", url, len(courses), len(rows))
    return courses


# ---------------------------------------------------------------------------
# helpers for the pipeline / statistics
# ---------------------------------------------------------------------------
def dedupe_courses(courses: Sequence[CourseRecord]) -> List[CourseRecord]:
    """Keep one record per course_id (a course can be listed on several subject pages, e.g. ARCH
    courses also appear on the AAID and DSGN pages). Prefers the record scraped from the course's
    own subject page (so its link opens the right page), then the longest description, then the
    first one seen. Order of first appearance is kept."""
    def rank(c: CourseRecord) -> Tuple[bool, int, int]:
        return (listing_params(c.url)[0] == c.subject, len(c.description), len(c.attributes))

    best: Dict[str, CourseRecord] = {}
    for c in courses:
        if c.course_id not in best or rank(c) > rank(best[c.course_id]):
            best[c.course_id] = c
    order = list(OrderedDict.fromkeys(c.course_id for c in courses))
    return [best[cid] for cid in order]


def summarize_courses(courses: Sequence[Union[CourseRecord, dict]]) -> Dict[str, object]:
    """Counts for reports: records, unique codes/subjects (overall and per level), field coverage
    in percent, every attribute label with its frequency, and records per school. Accepts
    CourseRecords or the dicts read back from courses.jsonl."""
    rows = [c if isinstance(c, dict) else c.to_dict() for c in courses]

    def pct(field_name: str) -> float:
        hits = sum(1 for c in rows if c.get(field_name))
        return round(100.0 * hits / len(rows), 1) if rows else 0.0

    per_level: Dict[str, Dict[str, int]] = {}
    for level in sorted({c.get("level") or "" for c in rows}):
        subset = [c for c in rows if (c.get("level") or "") == level]
        per_level[level] = {
            "records": len(subset),
            "unique_codes": len({c.get("code") for c in subset}),
            "unique_subjects": len({c.get("subject") for c in subset}),
        }
    labels: Counter = Counter(label for c in rows for label in (c.get("attributes") or {}))
    return {
        "records": len(rows),
        "unique_course_ids": len({c.get("course_id") for c in rows}),
        "unique_codes": len({c.get("code") for c in rows}),
        "unique_subjects": len({c.get("subject") for c in rows}),
        "per_level": per_level,
        "with_description_pct": pct("description"),
        "with_prerequisites_pct": pct("prerequisites"),
        "with_corequisites_pct": pct("corequisites"),
        "with_hours_pct": pct("hours"),
        "labels": dict(labels.most_common()),
        "schools": dict(Counter(c.get("school") for c in rows).most_common()),
    }
