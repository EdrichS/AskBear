"""Parse a page of ANY NYIT website into a PageRecord and its SectionRecords.

* catalog.nyit.edu keeps its dedicated parser (:func:`html_parser.parse_page`).
* Every other site (www.nyit.edu, vancouver.nyit.edu, apps.nyit.edu/policies, the
  magazine, the events calendar, the library's LibAnswers/LibGuides/LibCal, blogs.nyit.edu,
  online.nyit.edu and any other ``*.nyit.edu`` host) goes through
  :func:`parse_site_page`, which reuses html_parser's DOM walker, sectioning and
  long-section splitting, so passages look the same on every site.

What differs between the sites is the page *template*: where the main content is,
which parts of it are chrome (menus, share buttons, "Request Information" forms,
"Read more" teasers, "Was this helpful?" widgets ...), where the title, breadcrumbs and
dates are. Those rules live in :data:`TEMPLATES` (one entry per template family), on
top of the per-host :class:`sites.SiteProfile` (content/remove selectors).

Content that only shows when a control is used (the panels of a drop-down, tabs, the
"more..." descriptions of LibGuides) is page text; a page-wide ASP.NET <form> is layout
(:func:`clean_content`). Web-server welcome pages, application error pages and CMS
placeholders get no sections (:func:`soft_404_reason`). Dates: ``published`` is the
publication date only (bios have none, an event's time goes to ``event_start`` /
``event_end``, magazine articles get their issue's season, see :func:`_page_dates`).
LibCal's opening hours keep only the weeks whose hours are published and are titled with
the date they were fetched; page 2, 3, ... of a paginated listing gets "(page N)" in its
title (:func:`_special_title`).

Boilerplate that survives the template rules -- the same paragraph on a large share of
a site's pages -- is found by :func:`find_boilerplate` over all pages of a site and
dropped by :func:`parse_site_page` (two passes, see pipeline.py)::

    boilerplate = find_boilerplate((url, html) for url, html in pages)
    page, sections = parse_any(html, url, fetched_at, entry, boilerplate)
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from html import unescape as html_unescape
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlsplit

from bs4.element import NavigableString, Tag

from . import html_parser, sites
from .html_parser import Block
from .models import PAGE_TYPES, PageRecord, SectionRecord
from .urls import normalize_url, path_segments, url_host
from .utils import (clean_text, fix_mojibake, has_leftover_entity, has_mojibake, make_soup, sha1,
                    short_hash, unescape_leftovers, word_count)

log = logging.getLogger("askbear.site_parser")


# ---------------------------------------------------------------------------
# Templates: where things are on each family of pages
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Template:
    """Page-template rules (used on top of the host's SiteProfile).

    content     : content roots, tried before ``profile.content_selectors``
    remove      : noise inside the content root (added to the common + profile rules)
    titles      : elements holding the page title, tried before the first <h1>
    breadcrumbs : breadcrumb containers, tried before ``profile.breadcrumb_selector``
    """

    name: str
    content: Tuple[str, ...] = ()
    remove: Tuple[str, ...] = ()
    titles: Tuple[str, ...] = ()
    breadcrumbs: Tuple[str, ...] = ()


TEMPLATES: Dict[str, Template] = {t.name: t for t in [
    # www.nyit.edu, vancouver.nyit.edu (WordPress, theme "nyit") and apps.nyit.edu/policies
    # (same design system): Hero (h1, byline, breadcrumbs), Subnav "Explore", Common and
    # Section blocks, CTA "Keep Exploring" + buttons, "Request Information" forms
    Template(
        "nyit",
        content=("main",),
        remove=(
            ".Hero", ".Main__subnav", "nav.Subnav", ".Subnav", ".Breadcrumbs", ".Secnav",
            ".wp-block-buttons", ".wp-block-button", ".CTA", ".Form", ".Feed", ".Share",
            ".social-share", ".addtoany_share_save_container", ".Section__footer",
            "#table-loader", ".Hero__next", ".Explore__control", ".Slider__controls",
            ".Card__footer", ".Card__meta", ".Card__date", ".table-of-content",
            ".Slider__main",    # carousels of teaser cards ("Meet Our Doers": name + major)
            ".Hub",             # "New York Tech News" teaser hub (latest headlines + dates)
            # curriculum tables (www .../curriculum/, generated from the catalog): the course
            # description rows are collapsed (class hidecd) and repeat the course directory
            "tr.hidecd", "tr.hideCourseInfo",
        ),
        titles=(".Hero h1", "h1.Hero__heading"),
        breadcrumbs=(".yoast-breadcrumbs", "div.Breadcrumbs", "nav.Breadcrumbs"),
    ),
    # blogs.nyit.edu (older NYIT design): one post = div.blog-item-detail
    Template(
        "blogs",
        content=(".blog-item-detail", "main", "[role=main]"),
        remove=(".breadcrumb-wrap-blog", ".blog-more", "ul.nav-main", ".program",
                ".blog-item-cta", ".blog-item-meta", ".share", ".social", ".pagination",
                ".news_hero", ".blog-item .monospace-regular-xsmall",
                # the "Previous Next" buttons of a post's photo gallery (captions stay)
                ".carousel-gallery-navigation", ".carousel-navigation", ".flex-direction-nav"),
        titles=(".blog-item-detail .blog-item-header h2", ".blog-item-detail h2"),
        breadcrumbs=("ul.breadcrumbs", ".breadcrumb-wrap-blog"),
    ),
    # libanswers.nyit.edu (Springshare LibAnswers): the answer is in column 1
    Template(
        "libanswers",
        content=("#s-la-page-column-1", "#s-la-page-maincontent", "[role=main]"),
        remove=(".s-la-faq-action-bar", "#s-la-faq-commentadd", "#s-la-page-column-2",
                ".s-la-faq-topics", ".s-la-searchform", "#s-la-vote"),
        titles=("h1.s-la-public-header-title", "#s-la-public-header h1"),
        breadcrumbs=("#s-la-bc ol", "ol.breadcrumb"),
    ),
    # libguides.nyit.edu (Springshare LibGuides): boxes in #s-lg-guide-main
    Template(
        "libguides",
        content=("#s-lg-guide-main", "#s-lg-main-content", "#s-lib-public-main"),
        remove=("#s-lg-page-prevnext", ".s-lg-tabs", "#s-lg-guide-tabs", ".s-lib-box-title-hidden",
                ".s-lg-guide-search", "#s-lg-guide-search-box", ".s-lib-profile-email",
                ".s-lib-profile-social", ".s-lg-rss", ".s-lib-jump-link", "#s-lib-footer-public",
                "#s-lg-public-skiplink", "#s-lg-sb-search-bar", ".hide"),
        titles=("#s-lg-guide-name",),
        breadcrumbs=("#s-lib-bc-list", "ol.breadcrumb"),
    ),
    # events.nyit.edu (LiveWhale): the visible page is a "featured event" widget; the
    # event itself is only in og:/JSON-LD metadata (see _event_blocks)
    Template("events", content=("main",)),
    # magazine.nyit.edu (Foleon): no headings, everything in <article>
    Template("magazine", content=("article", "[role=main]", "main")),
    # online.nyit.edu (Brightspot, run by Everspring): lead forms everywhere
    Template(
        "online",
        content=("main",),
        remove=("ps-form", ".Form", ".PromoFullwidthC-cta", ".return-blog-url-link",
                ".ArticlePage-byline", ".Page-breadcrumbs", ".ArticlePage-breadcrumbs",
                ".LinkList", "ps-modal", ".Modal", ".PromoContentFeatureShowcase",
                "a.button-secondary", "a.button-primary", ".SocialBar",
                # the "Nov / 13" calendar tile of the deadline widget (its text says
                # "Priority Deadline / November 13 / Spring 2027" anyway)
                ".PromoDateBlock-date"),
        titles=("h1.ArticlePage-headline", "h1.ModuleHeader-title", "h1"),
        breadcrumbs=("nav.Page-breadcrumbs", "nav.ArticlePage-breadcrumbs"),
    ),
    # libcal.nyit.edu (Springshare LibCal): the home page (how to book a room or a librarian),
    # /hours (the weekly view: one table per week for the six libraries; the monthly view
    # repeats it as a calendar grid) and /event/<id> (an event, also described in JSON-LD)
    Template(
        "libcal",
        content=("#s-lc-page-maincontent", "#s-lc-public-main", "main"),
        remove=("ul.nav-tabs", "[id^=s-lc-hm-m]", ".s-lc-hm-tb", "#s-lc-public-footer-brand",
                ".s-lc-public-footer-actions", "#s-lc-language", ".s-lc-event-add-to-cal",
                ".s-lc-event-reg-btn", "form"),
        titles=("#s-lc-public-header-title", "h1"),
        breadcrumbs=("#s-lc-public-bc ol", "ol.breadcrumb"),
    ),
    # anything else (newly discovered *.nyit.edu hosts); Bootstrap's navigation bar is never
    # content (on nyitetic.nyit.edu it sits inside the page-wide ASP.NET <form>)
    Template("generic", remove=(".navbar",)),
]}

_HOST_TEMPLATES = {
    "www.nyit.edu": "nyit", "vancouver.nyit.edu": "nyit", "apps.nyit.edu": "nyit",
    "blogs.nyit.edu": "blogs", "libanswers.nyit.edu": "libanswers",
    "libguides.nyit.edu": "libguides", "events.nyit.edu": "events",
    "magazine.nyit.edu": "magazine", "online.nyit.edu": "online",
    "libcal.nyit.edu": "libcal",
}


def detect_template(host: str, soup) -> Template:
    """Template family of a page: by host, else by tell-tale markup (newly discovered
    NYIT hosts often serve one of the known templates)."""
    name = _HOST_TEMPLATES.get(host)
    if name is None:
        body = soup.find("body")
        classes = " ".join(_classes(body))
        if "wp-theme-nyit" in classes or soup.select_one("main .Hero") is not None:
            name = "nyit"
        elif soup.select_one(".page-wrapper-blog") is not None:
            name = "blogs"
        elif "s-la-page" in classes:
            name = "libanswers"
        elif "s-lg-guide-body" in classes or soup.find(id="s-lg-guide-main") is not None:
            name = "libguides"
        else:
            name = "generic"
    return TEMPLATES[name]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def _classes(el) -> List[str]:
    if el is None or not isinstance(el, Tag):
        return []
    cls = el.get("class") or []
    return cls if isinstance(cls, list) else str(cls).split()


def _one_line(text: str) -> str:
    text = text or ""
    if "&" in text:  # titles and descriptions escaped twice ("Tech&amp;rsquo;s")
        text = unescape_leftovers(text)
    return _WS.sub(" ", clean_text(text)).strip()


def _text(el) -> str:
    return _one_line(el.get_text(" ", strip=True)) if el is not None else ""


def _select_one(root, selector: str):
    try:
        return root.select_one(selector)
    except Exception:  # an invalid selector in a profile must not break parsing
        log.debug("invalid CSS selector %r", selector)
        return None


def _select(root, selector: str) -> List[Tag]:
    try:
        return root.select(selector)
    except Exception:
        log.debug("invalid CSS selector %r", selector)
        return []


def _first_text(root, selectors: Sequence[str], inline: bool = False) -> str:
    for sel in selectors:
        el = _select_one(root, sel) if root is not None else None
        t = _inline_text(el) if inline else _text(el)
        if t:
            return t
    return ""


_COPYRIGHT_YEARS = re.compile(r"(©|\(c\)|\bcopyright\b)\s*(?:©\s*)?(?:19|20)\d\d(?:\s*[-–/]\s*(?:19|20)?\d\d)?")


def block_key(text: str) -> str:
    """Normalised text of a block, used to recognise the same block on other pages.

    The year of a copyright notice does not count: "... Magazine © 2024 All Rights
    Reserved" and "... © 2025 ..." are the same footer."""
    t = _one_line(text).casefold()
    t = re.sub(r"^(?:[-•*]|\d{1,3}\.)\s+", "", t)  # list bullets
    t = _COPYRIGHT_YEARS.sub(r"\1 <year>", t)
    return t.strip(" .")


def _same_page(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalize_url(a or ""), normalize_url(b or "")
    return bool(na and nb) and na.rstrip("/") == nb.rstrip("/")


# ---------------------------------------------------------------------------
# Metadata: <meta>, JSON-LD, dates
# ---------------------------------------------------------------------------
def read_meta(soup) -> Dict[str, str]:
    """<meta name|property|itemprop=... content=...> -> {lowercased key: content} (the
    first non-empty value wins)."""
    out: Dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        content = m.get("content")
        if key and isinstance(content, str) and content.strip() and key not in out:
            out[key] = content.strip()
    return out


_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_CDATA = re.compile(r"/\*\s*<!\[CDATA\[\s*\*/|/\*\s*\]\]>\s*\*/|<!\[CDATA\[|\]\]>")


def read_json_ld(soup) -> List[dict]:
    """Every JSON-LD object on the page (``@graph`` and lists flattened).

    LiveWhale wraps its JSON in ``/*<![CDATA[*/ ... /*]]>*/`` and some pages have
    trailing commas or raw newlines inside strings: those are repaired; anything else
    that is not valid JSON is skipped.
    """
    items: List[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = _CDATA.sub("", script.string or script.get_text() or "").strip()
        if not raw:
            continue
        data = None
        for candidate in (raw, _TRAILING_COMMA.sub(r"\1", raw)):
            try:
                data = json.loads(candidate, strict=False)
                break
            except ValueError:
                continue
        if data is None:
            log.debug("unreadable JSON-LD block skipped")
            continue
        stack = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                graph = node.get("@graph")
                if graph is not None:
                    stack.extend(graph if isinstance(graph, list) else [graph])
                items.append(node)
    return items


def _ld_types(item: dict) -> List[str]:
    t = item.get("@type")
    if isinstance(t, list):
        return [str(x) for x in t]
    return [str(t)] if t else []


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_RE = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE_MDY = re.compile(r"\b" + _MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
_DATE_DMY = re.compile(r"\b(\d{1,2})\s+" + _MONTH_RE + r",?\s+(\d{4})\b", re.I)
_DATE_MY = re.compile(r"\b" + _MONTH_RE + r",?\s+(\d{4})\b", re.I)


def _ymd(year: str, month: int, day: str) -> Optional[str]:
    try:
        return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def iso_date(value: Optional[str], allow_month: bool = False) -> Optional[str]:
    """ISO 8601 date from a metadata value or a visible date.

    "2024-05-15T19:33:00+00:00" (kept as is) | "May 15, 2024" / "15 Sep 2026" ->
    "2024-05-15" | "September 2024" -> "2024-09" (only with ``allow_month``) | else None.
    """
    v = _one_line(value or "")
    if not v:
        return None
    if _ISO.match(v):
        return v.replace(" ", "T", 1)
    m = _DATE_MDY.search(v)
    if m:
        return _ymd(m.group(3), _MONTHS[m.group(1).lower()[:3]], m.group(2))
    m = _DATE_DMY.search(v)
    if m:
        return _ymd(m.group(3), _MONTHS[m.group(2).lower()[:3]], m.group(1))
    if allow_month:
        m = _DATE_MY.search(v)
        if m and 1990 <= int(m.group(2)) <= 2100:
            return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()[:3]]:02d}"
    return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not _ISO.match(value.strip()):
        return None
    v = value.strip().replace(" ", "T", 1)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)  # +0000 -> +00:00 (Python < 3.11)
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Titles and breadcrumbs
# ---------------------------------------------------------------------------
# h1 texts that name a section of the site, not the page ("Staff & Faculty Directory" on
# every bio, "Events Calendar" on every event)
GENERIC_TITLES = {
    "staff & faculty directory", "staff and faculty directory", "events calendar",
    "new york tech", "new york institute of technology", "nyit", "page not found", "home",
    "event detail", "libcal", "blogs nyit", "untitled", "-",
    "event box",        # the label of LibCal's event box, set as an <h1>
}
_SITE_SUFFIX = re.compile(
    r"\s*[|\-–—:]\s*(New York Tech(?:\s*-\s*Vancouver| Online)?|NY Tech Online|NYIT Online|"
    r"NYIT Magazine|Blogs NYIT|LibGuides at New York Institute of Technology|Ask a Librarian|"
    r"LibCal\s*-\s*New York Institute of Technology|LibCal|New York Institute of Technology|"
    r"Events Calendar|NYIT)\s*$", re.I)


def title_from_page_title(page_title: str) -> str:
    """ "Research Facilities | New York Tech" -> "Research Facilities";
    "First Byte - 2025 Spring - NYIT Magazine" -> "First Byte - 2025 Spring"."""
    t = _one_line(page_title)
    for _ in range(4):
        new = _SITE_SUFFIX.sub("", t).strip()
        if new == t:
            break
        t = new
    return t.split(" | ")[0].strip(" |-–—")


_CRUMB_SEP = re.compile(r"\s*(?:>|»|›|→)\s*")
_HOME_CRUMBS = {"home", "new york tech", "new york institute of technology", "nyit", "library",
                "libraries", "new york tech libraries", "nyit libraries"}
# typos in the apps.nyit.edu breadcrumbs ("Policie & Guidelines", "Policies & Guildelines")
_CRUMB_FIXES = ((re.compile(r"^Policie &"), "Policies &"), (re.compile(r"Guildelines"), "Guidelines"))


def _crumbs_of(el: Tag) -> List[str]:
    items = [li for li in el.find_all("li") if li.find_parent(["ol", "ul"]) is not None]
    full = _one_line(el.get_text(" ", strip=True))
    if items:
        crumbs = [_text(li) for li in items]
    elif _CRUMB_SEP.search(full):
        crumbs = _CRUMB_SEP.split(full)
    else:
        # crumbs as sibling links/spans without separators (online.nyit.edu)
        crumbs = [_text(c) for c in el.find_all(["a", "span"], recursive=False)] or [full]
    out: List[str] = []
    for c in crumbs:
        c = _one_line(c).strip(" >»›|/")
        for pattern, fixed in _CRUMB_FIXES:
            c = pattern.sub(fixed, c)
        if c and (not out or out[-1] != c):
            out.append(c)
    return out


def extract_breadcrumbs(soup, selectors: Sequence[str]) -> List[str]:
    for sel in selectors:
        el = _select_one(soup, sel) if sel else None
        if el is not None:
            crumbs = _crumbs_of(el)
            if crumbs:
                return crumbs
    return []


def _area(breadcrumbs: Sequence[str]) -> Optional[str]:
    """First meaningful crumb after "Home": "Home > Research > Facilities" -> "Research"."""
    rest = list(breadcrumbs)
    while rest and rest[0].casefold() in _HOME_CRUMBS:
        rest.pop(0)
    return rest[0] if rest else None


def page_area(breadcrumbs: Sequence[str], title: str, page_title: str) -> Optional[str]:
    """The part of the site a page belongs to (``PageRecord.area``).

    From the breadcrumbs (:func:`_area`); when they hold nothing but the page itself, from
    the middle of its <title>: blogs.nyit.edu's breadcrumb link to the blog has no text, but
    "Build the Relevance of Your Course | CFE Weekly Teaching Notes | Blogs NYIT" names it."""
    area = _area(breadcrumbs)
    if len(breadcrumbs) > 1 or (area is not None and block_key(area) != block_key(title)):
        return area
    parts = [p.strip() for p in _one_line(page_title).split(" | ") if p.strip()]
    if len(parts) >= 3 and _SITE_SUFFIX.match(" | " + parts[-1]):
        middle = parts[-2]
        if block_key(middle) not in (block_key(title), block_key(parts[0])):
            return middle
    return area


# ---------------------------------------------------------------------------
# Schools, programs
# ---------------------------------------------------------------------------
_SCHOOL_PATTERNS: List[Tuple[re.Pattern, str]] = [(re.compile(p, re.I), s) for p, s in (
    (r"\bengineering (and|&) computing sciences\b", "College of Engineering and Computing Sciences"),
    (r"\bcollege of arts (and|&) sciences\b", "College of Arts and Sciences"),
    (r"\bcollege of osteopathic medicine\b|\bNYITCOM\b", "College of Osteopathic Medicine"),
    (r"\bschool of architecture (and|&) design\b", "School of Architecture and Design"),
    (r"\bschool of health professions\b", "School of Health Professions"),
    (r"\bschool of management\b", "School of Management"),
)]
# www.nyit.edu / vancouver.nyit.edu URL segments of the six schools
SCHOOL_BY_SLUG: Dict[str, str] = {
    "arts-and-sciences": "College of Arts and Sciences",
    "engineering-and-computing-sciences": "College of Engineering and Computing Sciences",
    "architecture-and-design": "School of Architecture and Design",
    "health-professions": "School of Health Professions",
    "management": "School of Management",
    "medicine": "College of Osteopathic Medicine",
}


def school_in_text(text: str) -> Optional[str]:
    """The one school named in a short text ("Associate Professor; ..., College of Arts &
    Sciences" -> "College of Arts and Sciences"); None if none or several."""
    found = []
    for pattern, school in _SCHOOL_PATTERNS:
        if pattern.search(text or "") and school not in found:
            found.append(school)
    return found[0] if len(found) == 1 else None


def school_from_url(url: str) -> Optional[str]:
    """www/vancouver URLs: /academics/<school>/... and /medicine/... belong to a school."""
    if url_host(url) not in ("www.nyit.edu", "vancouver.nyit.edu"):
        return None
    segs = path_segments(url)
    if len(segs) >= 2 and segs[0] == "academics" and segs[1] in SCHOOL_BY_SLUG:
        return SCHOOL_BY_SLUG[segs[1]]
    if segs and segs[0] == "medicine":
        return SCHOOL_BY_SLUG["medicine"]
    return None


# Degrees of the Vancouver campus that the catalog vocabulary does not know
_EXTRA_DEGREES = ((re.compile(r"\bMaster of Data Science\b|\bM\.?D\.?S\.?\b"), "M.D.S."),
                  (re.compile(r"\bMaster of Energy Management\b|\bM\.?E\.?M\.?\b"), "M.E.M."),
                  (re.compile(r"\bMaster of Computer Science\b|\bM\.?C\.?S\.?\b"), "M.C.S."))
_SLUG_DEGREES = {"ms": "M.S.", "ma": "M.A.", "bs": "B.S.", "ba": "B.A.", "bfa": "B.F.A.", "mba": "M.B.A.",
                 "phd": "Ph.D.", "dpt": "D.P.T.", "mph": "M.P.H.", "otd": "O.T.D.", "march": "M.Arch.",
                 "barch": "B.Arch.", "mat": "M.A.T.", "mps": "M.P.S.", "mds": "M.D.S.", "mem": "M.E.M.",
                 "mcs": "M.C.S.",
                 "minor": "Minor", "do": "D.O.", "aas": "A.A.S.", "mfa": "M.F.A."}


def find_degree(text: str) -> Optional[str]:
    return html_parser.find_degree(text) or next((d for p, d in _EXTRA_DEGREES if p.search(text or "")), None)


def slug_degree(slug: str) -> Optional[str]:
    """ "artificial-intelligence-ms" -> "M.S."; "master-of-cybersecurity-ms-online" -> "M.S."."""
    tokens = [t for t in slug.lower().split("-") if t and not t.isdigit() and t not in ("online", "2")]
    if "advanced" in tokens and "certificate" in tokens:
        return "Advanced Certificate"
    for token in reversed(tokens[-3:]):
        if token in _SLUG_DEGREES:
            return _SLUG_DEGREES[token]
    return None


# ---------------------------------------------------------------------------
# Content cleaning
# ---------------------------------------------------------------------------
# Links that only navigate ("Read More", "View All News", "All Facilities & Resources",
# "Meet the Rest of the Faculty ➔").
_NAV_LINK = re.compile(
    r"^(read|learn|find out|discover|see|view|show|browse|explore) (more|all)\b.*|^more( info(rmation)?)?$"
    r"|^more about\b.*|^(view|see|browse|show) (the )?(full|complete|all|more)\b.*|^all [\w&,'’ -]{3,60}$"
    r"|^back( to\b.{0,40})?$|^(next|previous|prev)\b.{0,40}$|^return to\b.*"
    r"|^(continue reading|click here|go to entry|load more|share( this)?|skip to (main )?content)$"
    r"|^[^.!?]{2,60}\s*[➔→»›]$", re.I)
# Lines that are page furniture wherever they appear: slideshow counters ("3 of 6"), photo
# credits ("PHOTO: KEVIN THAI"), letter-spaced labels ("F E A T U R E").
_JUNK_LINE = re.compile(r"^(?:[-•]\s*)?\d{1,3} of \d{1,3}$"
                        r"|^(?:photos?|photograph(?:y|s)?|image|illustrations?)(?: by| credits?)?\s*:.{0,80}$"
                        r"|^(?:[A-Z] ){3,}[A-Z]$"
                        # widget labels and call-to-action buttons standing alone
                        r"|^(?:pause|play|stop) (?:animation|video|slideshow)$|^(?:close|menu|loading\W*)$"
                        r"|^(?:[-•]\s*)?(?:register|register now|register here|rsvp|sign up|donate now|give now"
                        r"|apply now|apply today|request info(?:rmation)?|subscribe|schedule (?:a |your )?tour"
                        r"|download background)\W*$"
                        # the empty staff-directory widget of www.nyit.edu bios
                        r"|^no directory data found\W*$", re.I)
# "Copyright © 2020 New York Institute of Technology", "New York Institute of Technology
# Magazine © 2024 All Rights Reserved": a copyright notice standing alone is page furniture
_COPYRIGHT_YEAR = re.compile(r"(?:©|\(c\)|\bcopyright\b)\s*(?:©\s*)?(?:19|20)\d\d", re.I)
_COPYRIGHT_START = re.compile(r"^(?:[-•]\s*)?(?:©|\(c\)|copyright\b)", re.I)


def _is_copyright_line(line: str) -> bool:
    if word_count(line) > 15 or not _COPYRIGHT_YEAR.search(line):
        return False
    return ("©" in line or "(c)" in line.lower() or re.search(r"all rights reserved", line, re.I) is not None) \
        and (_COPYRIGHT_START.match(line) is not None
             or re.search(r"all rights reserved|new york (?:institute of )?tech|\bnyit\b", line, re.I) is not None)


def _is_junk_line(line: str) -> bool:
    line = line.strip()
    return bool(_JUNK_LINE.match(line)) or _is_copyright_line(line)


def _is_junk(block: Block) -> bool:
    if block.kind == "heading":                     # "PHOTO: ALISTAIR DUNCAN" set as a heading
        return _is_junk_line(block.text)
    if not re.search(r"\w", block.text):           # ";" left over from an empty layout cell
        return True
    lines = [ln for ln in block.text.split("\n") if ln.strip()]
    return bool(lines) and all(_is_junk_line(ln) for ln in lines)


_INLINE_SKIP = {"script", "style", "noscript", "template", "button", "svg", "img", "select", "option"}


def _inline_text(el) -> str:
    """Text of a heading-like element with inline spacing preserved ("“<em>Match</em>”
    Madness" -> "“Match” Madness", not "“ Match ” Madness")."""
    if el is None:
        return ""
    parts: List[str] = []
    for node in el.descendants:
        if isinstance(node, Tag):
            if node.name == "br" or node.name in html_parser.BLOCK_TAGS:
                parts.append(" ")
        elif isinstance(node, NavigableString) and not isinstance(node, html_parser.NON_TEXT_STRINGS):
            skip = False
            for p in node.parents:              # only the ancestors inside ``el``
                if p is el or p is None:
                    break
                if p.name in _INLINE_SKIP:
                    skip = True
                    break
            if not skip:
                parts.append(str(node))
    return _one_line("".join(parts))


def _unwrap_text_buttons(root: Tag) -> None:
    """Accordion headings put their text in a <button>, which is otherwise dropped as UI."""
    for button in root.find_all("button"):
        parent = button.parent
        if parent is not None and (parent.name in ("h1", "h2", "h3", "h4", "h5", "h6", "summary", "dt")
                                   or any("accordion" in c.lower() and "head" in c.lower()
                                          for c in _classes(parent))):
            button.unwrap()


def _document_of(el: Tag):
    while el.parent is not None:
        el = el.parent
    return el


# ---- panels a control shows (drop-downs, tabs, accordions) ---------------------------
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_HIDDEN_DECL = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!\s*important)?\s*;?", re.I)
_HEADING_TAG = re.compile(r"^h[1-6]$")
_PANEL_TOGGLES = {"tab", "pill", "collapse"}            # data-(bs-)toggle of a control that shows a panel
# hidden things that are not page content even when a control opens them
_NOT_A_PANEL = re.compile(r"modal|dialog|popup|pop-up|popover|lightbox|tooltip|menu|\bnav|navigation", re.I)
_PLACEHOLDER_OPTION = re.compile(r"^(?:-+\s*)?(?:select|choose|pick)\b|^[-–—\s]*$", re.I)


def _is_hidden_el(el: Tag) -> bool:
    """Hidden until a script shows it: inline display:none / visibility:hidden, the ``hidden``
    attribute, aria-hidden="true"."""
    style = el.get("style")
    return (el.has_attr("hidden") or str(el.get("aria-hidden") or "").strip().lower() == "true"
            or (isinstance(style, str) and _HIDDEN_STYLE.search(style) is not None))


def _unhide(el: Tag) -> None:
    style = el.get("style")
    if isinstance(style, str):
        rest = _HIDDEN_DECL.sub("", style).strip()
        if rest:
            el["style"] = rest
        else:
            del el["style"]
    for attr in ("hidden", "aria-hidden"):
        if el.has_attr(attr):
            del el[attr]


# Account details that some pages keep behind a "more..." toggle ("Username: ... /
# Password: ...", "U: ... / P: ..." on LibGuides): such a panel stays hidden, so that no
# password ever reaches the datasets.
_PASSWORD_FIELD = re.compile(r"\b(?:password|passcode|passwort|pwd|pin code)\s*[:=]", re.I)
_SHORT_PASSWORD_LINE = re.compile(r"^\s*(?:p|pw)\s*[:=]\s*\S+\s*$", re.I | re.M)
_USER_LINE = re.compile(r"^\s*(?:u|user|user ?name|user ?id|login|log-?in|id)\s*[:=]\s*\S+\s*$", re.I | re.M)


def has_credentials(el: Tag) -> bool:
    """Does the element show a password ("Password: ...", or a "U: ..." line with a
    "P: ..." line)?"""
    text = el.get_text("\n")
    return bool(_PASSWORD_FIELD.search(text)) or (
        _SHORT_PASSWORD_LINE.search(text) is not None and _USER_LINE.search(text) is not None)


def _is_content_panel(el: Tag) -> bool:
    """A hidden panel that is page content: not a modal / pop-up / menu, no form inside,
    no password in it."""
    names = " ".join(_classes(el) + [str(el.get("role") or ""), str(el.get("id") or "")])
    if _NOT_A_PANEL.search(names) or el.has_attr("aria-modal"):
        return False
    return el.find(["form", "input", "select", "textarea"]) is None and not has_credentials(el)


def _is_control(el: Tag) -> bool:
    return el.has_attr("aria-controls") or el.has_attr("data-toggle") or el.has_attr("data-bs-toggle")


def _control_targets(control: Tag) -> List[str]:
    """ids of the panels a tab / accordion / collapse control shows."""
    ids: List[str] = []
    if control.has_attr("aria-controls") and (control.get("role") == "tab" or control.has_attr("aria-expanded")):
        ids += str(control.get("aria-controls") or "").split()
    toggle = str(control.get("data-toggle") or control.get("data-bs-toggle") or "").strip().lower()
    if toggle in _PANEL_TOGGLES:
        target = str(control.get("data-target") or control.get("data-bs-target") or "").strip()
        if not target and control.name == "a":
            target = str(control.get("href") or "").strip()
        if target.startswith("#"):
            ids.append(target)
    return [i.lstrip("#") for i in ids if i.lstrip("#")]


def _reveal_toggled_panels(root: Tag) -> None:
    """Show the content panels that a control on the page shows when it is used.

    www.nyit.edu's "Find an Advisor" page has ``<select id="dropdown1">`` with one ``<option
    value="contentN">`` per program and, below it, one ``<div id="contentN" style="display:
    none;">`` per program (the chair, the advisor of each campus, rooms, emails); a script
    shows the panel of the chosen option. That is page content (about 2,000 words on that
    page), not noise: each such panel is shown under a heading made of its option's label,
    one level below the heading above the drop-down, and the headings inside the panel
    become bold lines, so each program is one passage ("Architecture, B.ARCH ...").

    Panels that tabs, accordions or collapse buttons open (``aria-controls`` on a tab or an
    ``aria-expanded`` button, ``data-(bs-)toggle="tab|pill|collapse"`` with a target) are
    shown as they are: their label is on the page already. Modals, pop-ups, menus, panels
    holding a form and panels no control points at stay hidden.
    """
    hidden: Dict[str, Tag] = {}
    for el in root.find_all(id=True):
        eid = el.get("id")
        if isinstance(eid, str) and eid.strip() and eid.strip() not in hidden and _is_hidden_el(el):
            hidden[eid.strip()] = el
    if not hidden:
        return
    doc = _document_of(root)
    labels: Dict[str, List[str]] = defaultdict(list)     # panel id -> labels of its options
    select_of: Dict[str, Tag] = {}                        # panel id -> its drop-down
    shown: List[str] = []                                 # panel ids, in the order found
    for option in doc.find_all("option"):
        value = str(option.get("value") or "").strip().lstrip("#")
        if value not in hidden:
            continue
        label = _one_line(option.get_text(" ", strip=True))
        if not label or _PLACEHOLDER_OPTION.match(label):
            continue                                      # "Select One"
        if label not in labels[value]:
            labels[value].append(label)
        select_of.setdefault(value, option.find_parent("select") or option)
        shown.append(value)
    for control in doc.find_all(_is_control):
        shown += [i for i in _control_targets(control) if i in hidden]
    # heading levels first: the headings added below must not change them
    levels: Dict[str, int] = {}
    for pid, control in select_of.items():
        above = control.find_previous(_HEADING_TAG)
        levels[pid] = min(6, int(above.name[1]) + 1) if above is not None else 2
    for pid in dict.fromkeys(shown):
        panel = hidden[pid]
        if not _is_content_panel(panel) or any(_is_hidden_el(a) for a in panel.parents
                                               if isinstance(a, Tag) and a is not doc):
            continue        # a panel inside another hidden element stays as hidden as it is
        _unhide(panel)
        if labels.get(pid) and hasattr(doc, "new_tag"):
            for h in panel.find_all(_HEADING_TAG):      # "Long Island" -> a bold line
                strong = doc.new_tag("strong")
                for child in list(h.contents):
                    strong.append(child.extract())
                h.name = "p"
                h.append(strong)
            heading = doc.new_tag(f"h{levels.get(pid, 2)}")
            heading.string = " / ".join(labels[pid])
            panel.insert(0, heading)


# ---- forms that are page layout ---------------------------------------------------------
_ASPNET_STATE = re.compile(r"^__(?:VIEWSTATE|VIEWSTATEGENERATOR|EVENTVALIDATION)$")


def _unwrap_layout_forms(root: Tag) -> None:
    """ASP.NET WebForms pages (nyitetic.nyit.edu/errsela) wrap their whole body in one
    ``<form id="ctl01">`` with ``__VIEWSTATE`` inputs: such a form is page layout, not a
    form. It is unwrapped instead of being removed as noise; its inputs, selects and
    buttons are still removed. A form that holds the page's <main> is layout too (but not
    one that holds <article> cards: comapp.nyit.edu's newsletter form lists teasers)."""
    for form in root.find_all("form"):
        if form.find("input", attrs={"name": _ASPNET_STATE}) is not None \
                or form.find("main") is not None or form.find(attrs={"role": "main"}) is not None:
            form.unwrap()


def _remove(root: Tag, selectors: Sequence[str]) -> None:
    selectors = [s for s in selectors if s]
    if not selectors:
        return
    try:
        found = root.select(", ".join(selectors))
    except Exception:  # one bad selector: apply them one by one
        found = [el for s in selectors for el in _select(root, s)]
    for el in found:
        if el is not root:
            el.extract()


_BLOCKISH = {"li", "p", "div", "td", "dd", "dt", "h2", "h3", "h4", "h5", "h6", "section"}
_SHORT_CTA = re.compile(r"^(read|learn|see|view|show) (more|all)\W*$|^more( info(rmation)?)?\W*$", re.I)


_ARROW_TAIL = re.compile(r"\s*[➔→»›>]+\s*$")
_RETURN_TO = re.compile(r"^(return|back|go back) to\b", re.I)


def _drop_nav_links(root: Tag) -> None:
    """Remove "Read More" / "View All ..." / "Visit the Blog ➔" links that stand alone in
    their block (a list item, a paragraph). A link inside running text is kept -- "...
    prepare <a>all of the required documents</a>." -- except a bare "Read more" at the
    very end of a paragraph or a bare "More Information..." in front of a description."""
    for a in root.find_all("a"):
        text = _ARROW_TAIL.sub("", _text(a))
        if not text:
            continue
        holder = None
        for parent in a.parents:
            if parent is root or parent is None or _ARROW_TAIL.sub("", _text(parent)) != text:
                break
            if parent.name in _BLOCKISH:
                holder = parent
                break
        arrow = holder is not None and _ARROW_TAIL.search(_text(holder)) is not None
        nav = _NAV_LINK.match(text.rstrip(".… ")) is not None       # "More Information..."
        if holder is not None and (nav or (arrow and word_count(text) <= 8)):
            holder.extract()            # "Meet the Rest of the Faculty ➔"
        elif holder is None and _SHORT_CTA.match(text):
            a.extract()                 # "Read more" / "More Information..." next to other text
        elif holder is None:
            # "Return to > About Title IX" (apps.nyit.edu policies): a short paragraph that is
            # only a way back to the parent page
            block = a.find_parent(["p", "li", "div"])
            if block is not None and block is not root and word_count(_text(block)) <= 12 \
                    and _RETURN_TO.match(_text(block)):
                block.extract()


def _drop_in_page_tocs(root: Tag) -> None:
    """ "Table of contents" boxes that only link to anchors on the same page (the
    handbooks on apps.nyit.edu); html_parser skips only label-less ones."""
    for box in root.find_all(["table", "ul", "ol", "div", "nav"]):
        links = box.find_all("a", href=True)
        if len(links) < 4 or not all(a["href"].strip().startswith("#") for a in links):
            continue
        link_chars = sum(len(a.get_text(strip=True)) for a in links)
        if len(box.get_text(strip=True)) - link_chars <= 40:
            box.extract()


def _flatten_stats(root: Tag) -> None:
    """Stat tiles (h3 "7,946" + p "full-time students across all campuses") -> one line,
    instead of a heading per number."""
    items = root.select(".Stats__item")
    if not items:
        return
    soup = root
    while soup.parent is not None:
        soup = soup.parent
    for item in items:
        text = _inline_text(item)
        if text and hasattr(soup, "new_tag"):
            p = soup.new_tag("p")
            p.string = text
            item.clear()
            item.append(p)


def _drop_collection_links(root: Tag) -> None:
    """apps.nyit.edu policies start with "Belongs To Collection(s) > STUDENT HANDBOOK ...":
    navigation, not part of the policy text."""
    for h in root.find_all(["h4", "h5", "h6", "p", "strong"]):
        if not re.match(r"^belongs to collection", _text(h), re.I):
            continue
        sib = h.next_sibling
        while sib is not None:
            nxt = sib.next_sibling
            if isinstance(sib, NavigableString):
                if re.sub(r"[\s>»|]+", "", str(sib)):
                    break
                sib.extract()
            elif isinstance(sib, Tag) and sib.name in ("a", "br", "span"):
                sib.extract()
            else:
                break
            sib = nxt
        h.extract()


_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
# a superscript number right after punctuation is a footnote marker ("lost revenue.²")
_FOOTNOTE_CHARS = re.compile(r"(?<=[.,;:!?”\"’)])([" + _SUPERSCRIPT_DIGITS + r"]+)")


# "59½" would become "591⁄2" after NFKC (utils.clean_text): keep a space before the fraction
_MIXED_FRACTION = re.compile(r"(?<=\d)([¼-¾⅐-⅞])")
def _needs_fix(t: str) -> bool:
    return bool(t) and (_FOOTNOTE_CHARS.search(t) is not None or _MIXED_FRACTION.search(t) is not None
                        or has_leftover_entity(t) or has_mojibake(t))


def _fix_text_nodes(root: Tag) -> None:
    """Text fixes NFKC normalisation would get wrong: "revenue.²" -> "revenue. [2]" (a
    footnote marker, rendered like html_parser renders <sup>2</sup>, not "revenue.2");
    "age 59½" -> "age 59 ½" (-> "59 1⁄2", not "591⁄2"); double-escaped "&amp;" -> "&";
    mojibake ("â€™" -> "’")."""
    for s in root.find_all(string=_needs_fix):
        if isinstance(s, NavigableString) and not isinstance(s, html_parser.NON_TEXT_STRINGS):
            fixed = fix_mojibake(unescape_leftovers(str(s)))
            fixed = _FOOTNOTE_CHARS.sub(
                lambda m: " [" + "".join(str(_SUPERSCRIPT_DIGITS.index(c)) for c in m.group(1)) + "]", fixed)
            fixed = _MIXED_FRACTION.sub(r" \1", fixed)
            s.replace_with(NavigableString(fixed))


def clean_content(root: Tag, selectors: Sequence[str]) -> None:
    # before the noise is removed: drop-downs ("select") and hidden elements ("[hidden]")
    # are noise, the panels they show and a page-wide ASP.NET <form> are not
    _reveal_toggled_panels(root)
    _unwrap_layout_forms(root)
    _unwrap_text_buttons(root)
    _remove(root, selectors)
    _drop_nav_links(root)
    _drop_in_page_tocs(root)
    _drop_collection_links(root)
    _flatten_stats(root)
    _fix_text_nodes(root)


def _link_share(root: Optional[Tag]) -> Tuple[int, int, int, int]:
    """(words inside links, all words, headings, headings that are links) of the cleaned
    content. Teaser lists (news archives, blog indexes) are headings that are links."""
    if root is None:
        return 0, 0, 0, 0
    total = word_count(root.get_text(" ", strip=True))
    linked = sum(word_count(a.get_text(" ", strip=True)) for a in root.find_all("a"))
    heads = root.find_all(["h2", "h3", "h4"])
    linked_heads = 0
    for h in heads:
        a = h.find("a")
        if a is not None and _text(a) and _text(a) == _text(h):
            linked_heads += 1
    return linked, total, len(heads), linked_heads


# ---------------------------------------------------------------------------
# Extraction (shared by find_boilerplate and parse_site_page)
# ---------------------------------------------------------------------------
@dataclass
class Extracted:
    """Everything parse_site_page needs from one page, before boilerplate removal."""

    url: str
    site: str
    profile: sites.SiteProfile
    template: Template
    page_title: str = ""
    title: str = ""
    # og:title / <title> when it is the title plus a qualifier ("Management, M.B.A." ->
    # "Management, M.B.A. – Finance"), see _fuller_title; "" otherwise
    full_title: str = ""
    breadcrumbs: List[str] = field(default_factory=list)
    body_classes: List[str] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)
    json_ld: List[dict] = field(default_factory=list)
    root: Optional[Tag] = None
    blocks: List[Block] = field(default_factory=list)
    synthetic: Set[int] = field(default_factory=set)   # id() of blocks built from metadata
    num_tables: int = 0
    published: Optional[str] = None
    modified: Optional[str] = None
    event_start: Optional[str] = None   # events: start / end, ISO, in the event's local time
    event_end: Optional[str] = None
    description: Optional[str] = None
    school: Optional[str] = None
    facts: List[str] = field(default_factory=list)      # degree snapshot ("30 Credits", ...)
    profile_headings: Set[str] = field(default_factory=set)    # LibGuides: "Librarian: <name>"
    links_words: int = 0
    total_words: int = 0
    headings: int = 0
    linked_headings: int = 0
    is_event: bool = False
    meta_is_page: bool = True   # False when the <meta> tags describe another page


def _is_generic_title(t: str) -> bool:
    return not t or t.casefold() in GENERIC_TITLES or not re.search(r"\w", t)   # "..." is no title


def _resolve_title(ex: Extracted, h1s: Sequence[str]) -> str:
    """h1 (template title, content h1, any h1) -> og:title -> <title> first segment.
    Homepages use <title> (their h1 is a slogan); bios use og:title (their h1 is "Staff &
    Faculty Directory"); magazine articles use <title> ("<Article> - <Issue>")."""
    og = _one_line(ex.meta.get("og:title", "")) if ex.meta_is_page else ""
    from_tag = title_from_page_title(ex.page_title)
    home = "home" in ex.body_classes or not path_segments(ex.url)
    if home or ex.template.name == "magazine":
        order = [from_tag, title_from_page_title(og)] + list(h1s)
    elif ex.template.name == "events":
        # the event's own name, "|" included: "Graduate Admissions Info Session | Spotlight:
        # School of Health Professions" (three sessions differ only after the "|")
        order = [_SITE_SUFFIX.sub("", og)] + list(h1s) + [from_tag]
    elif "single-bio" in ex.body_classes:
        order = [og, ex.breadcrumbs[-1] if ex.breadcrumbs else "", from_tag]
    else:
        order = list(h1s) + [title_from_page_title(og), from_tag]
    for t in order:
        t = re.sub(r"^Q\.\s+", "", t or "")            # LibAnswers: "Q. How do I ...?"
        t = re.sub(r"\(\s+", "(", t)                    # "Master of Cybersecurity ( In-Person"
        if ex.template.name == "libguides":
            t = re.sub(r"\s*:\s*(Home|Welcome!?|:)?\s*$", "", t)   # "APA Boot Camp: Home"
        if t and (home or not _is_generic_title(t)):   # a homepage may be called "New York Tech"
            return t
    from_url = _title_from_url(ex.url)
    return from_url or next((t for t in order if t), "")


_DASHES = str.maketrans({"–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-"})
_NO_QUALIFIER = re.compile(r"^(?:archives?|\(?page \d+\)?)$", re.I)     # WordPress listing suffixes


def _title_norm(t: str) -> str:
    return _WS.sub(" ", (t or "").translate(_DASHES)).strip().casefold()


def _fuller_title(title: str, candidates: Sequence[str]) -> str:
    """The first candidate that is ``title`` plus a qualifier, else "".

    www.nyit.edu degree pages put only the base name in their <h1>, the hero, og:title and
    <title> name the concentration or the credential: "Management, M.B.A." ->
    "Management, M.B.A. – Finance", "Business Analytics" -> "Business Analytics, Advanced
    Certificate", "Message from the Chair" -> "Message from the Chair: Department of
    Physics". A candidate is not used when what it adds is only words of the title again
    ("Faculty Profile: Rosemary Gallagher: Rosemary Gallagher") or a WordPress listing
    suffix ("School of Management Archives", "(Page 2)"), or when the title does not end
    at a word boundary of the candidate."""
    base = _title_norm(title)
    if not base:
        return ""
    base_words = set(re.findall(r"\w+", base))
    for cand in candidates:
        cand = re.sub(r"\(\s+", "(", _one_line(cand or ""))
        full = _title_norm(cand)
        if len(full) <= len(base) or not full.startswith(base):
            continue
        rest = full[len(base):]
        if rest[0].isalnum() and base[-1].isalnum():
            continue                                    # "Business" -> "Businesses"
        rest_words = re.findall(r"\w+", rest)
        if not rest_words or set(rest_words) <= base_words:
            continue
        if _NO_QUALIFIER.match(rest.strip(" -:|,")):
            continue
        return cand
    return ""


_SMALL_WORDS = {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with"}


def _title_from_url(url: str) -> str:
    """Last resort when a page has no usable title ("..."): its URL slug,
    "/omm-center/become-trained-in-the-perrin-technique" -> "Become Trained in the Perrin Technique"."""
    segs = path_segments(url)
    if not segs:
        return ""
    words = [w for w in re.split(r"[-_\s]+", re.sub(r"\.\w{2,5}$", "", segs[-1])) if w]
    if words and words[-1].isdigit() and len(words[-1]) <= 2 and len(words) > 1:
        words = words[:-1]                       # "...-technique-2" -> "...-technique" (not "cork-2025")
    out = [w if (k and w.lower() in _SMALL_WORDS) else w[:1].upper() + w[1:] for k, w in enumerate(words)]
    return " ".join(out)


# ---- template-specific facts (read before the noise is removed) ---------------------
_NAV_GROUPS = re.compile(r"^(program resources|related links|resources|quick links|next steps|"
                         r"downloads?|learn more|contact|apply|connect)\b", re.I)


_LAST_REVISION = re.compile(r"^\s*last revis(ion|ed)\b", re.I)
# a "\n" typed into the text as two characters ('co-authored an article,\n<a ...>', 72 pages
# of accomplishments) -- not the "\n" of a Windows path ("C:\new\file")
_LITERAL_NEWLINE = re.compile(r"(?:\s*\\n(?![\w.-]+\\))+\s*")
# an accomplishment's date: "03.02.21" (month.day.year)
_CARD_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})$")


def _accomplishment_dates(soup) -> None:
    """www.nyit.edu/faculty-and-staff/accomplishments/: each item's date ("03.02.21") is
    in the card footer, removed with the dates of teaser cards everywhere else; it goes
    under the item's heading as "March 2, 2021" (66 of 1,269 items name no year)."""
    for el in _select(soup, ".Card__date"):
        m = _CARD_DATE.match(_text(el))
        card = el.find_parent(class_="Card")
        content = _select_one(card, ".Card__content") if card is not None else None
        if m is None or content is None:
            continue
        year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        try:
            day = datetime(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        line = soup.new_tag("p")
        line.string = f"{day.strftime('%B')} {day.day}, {day.year}"
        heading = content.find(["h2", "h3", "h4", "h5", "h6"])
        if heading is not None:
            heading.insert_after(line)
        else:
            content.insert(0, line)


def _nyit_facts(ex: Extracted, soup) -> None:
    """Hero byline date; apps.nyit.edu "Last Revision: September 2024" lines (-> modified,
    removed from the text); degree pages: the "Program Snapshot" (credits, locations) and
    the school (its "Related Links" point at the school's pages); a literal "\\n" in the
    text becomes a space; the dates of the faculty & staff accomplishments."""
    for s in soup.find_all(string=lambda t: "\\n" in t):
        if type(s) is NavigableString and not s.find_parent(["script", "style", "pre", "code", "textarea"]):
            s.replace_with(NavigableString(_LITERAL_NEWLINE.sub(" ", str(s))))
    if path_segments(ex.url)[:2] == ["faculty-and-staff", "accomplishments"]:
        _accomplishment_dates(soup)
    byline = _first_text(soup, (".Hero__meta",))
    if byline:
        ex.published = ex.published or iso_date(byline)
    revisions = []
    for s in soup.find_all(string=_LAST_REVISION):
        holder = s.parent
        while holder is not None and holder.name not in ("p", "div", "li", "td", "main", "body"):
            holder = holder.parent
        if holder is None or holder.name not in ("p", "li") or word_count(_text(holder)) > 8:
            continue
        date = iso_date(re.sub(_LAST_REVISION, "", _text(holder)), allow_month=True)
        if date:
            revisions.append(date)
        holder.extract()
    if revisions and not ex.modified:
        ex.modified = max(revisions)
    secnav = _select_one(soup, ".Secnav")
    if secnav is None:
        return
    for p in _select(secnav, ".Secnav__header p"):
        t = _text(p)
        if t and word_count(t) <= 12:
            ex.facts.append(t)
    for group in _select(secnav, ".Secnav__group"):
        for a in group.find_all("a", href=True):
            school = school_from_url(normalize_url(a["href"], ex.url) or "")
            if school and ex.school is None:
                ex.school = school
        heading = _text(group.find(["h2", "h3", "h4", "h5"]))
        items = [_text(li) for li in group.find_all("li")] or [_text(p) for p in group.find_all("p")]
        items = [i for i in items if i]
        if heading and items and not _NAV_GROUPS.match(heading):
            ex.facts.append(f"{heading}: {', '.join(items)}")


def _blogs_facts(ex: Extracted, soup) -> None:
    ex.published = ex.published or iso_date(_first_text(soup, (".blog-item-detail .blog-item-meta",)))


def _libanswers_facts(ex: Extracted, soup) -> None:
    """The answer box title "Answered By: X  Last Updated: Oct 13, 2015  Views: 246" ->
    modified; the box title itself is dropped. The details of the question (shown under
    the question in the page header) open the text."""
    details = _first_text(soup, (".s-la-public-header-description",))
    if details:
        ex.facts.append(details)
    for title in _select(soup, "#s-la-page-column-1 .s-la-box-title"):
        t = _text(title)
        if re.search(r"answered by|last updated|views:", t, re.I):
            m = re.search(r"last updated:?\s*(.+?)(?:views:|$)", t, re.I)
            if m:
                ex.modified = iso_date(m.group(1)) or ex.modified
            title.extract()


_MORE_LESS = re.compile(r"^(?:(?:more|less)\s*(?:\.\.\.|…)?\s*)+$", re.I)


def _libguides_facts(ex: Extracted, soup) -> None:
    """ "Last Updated" -> modified; the guide description; resource descriptions that
    LibGuides only shows in a pop-over (data-bs-content) or behind a "more..." toggle
    (div.s-lg-content-moreinfo, display:none until clicked) become visible text, and the
    "more..." / "less..." toggles go. A "more..." panel that holds account details ("U: ...
    / P: ...", "Username: ... / Password: ...") stays hidden."""
    for more in soup.select(".s-lg-content-moreinfo, .s-lg-content-more-info"):
        if not has_credentials(more):
            _unhide(more)
    for toggle in soup.select(".s-lg-label-moreinfo, .s-lg-label-more-info"):
        if _MORE_LESS.match(_text(toggle) or "more"):
            toggle.extract()
    for pop in soup.select("[data-bs-toggle=popover][data-bs-content], [data-toggle=popover][data-content]"):
        raw = pop.get("data-bs-content") or pop.get("data-content") or ""
        desc = _one_line(make_soup(raw).get_text(" ")) if "<" in raw else _one_line(raw)
        desc = re.sub(r"^this link opens in a new window\W*", "", desc, flags=re.I)
        for hidden in pop.select(".sr-only, .visually-hidden"):
            hidden.extract()            # the same description again, for screen readers
        if pop.get_text(strip=True):    # a book title link with a pop-over: keep the title
            if desc:
                pop.insert_after(NavigableString(f" – {desc}"))
        elif desc:                      # an (i) info icon: the description replaces it
            pop.replace_with(NavigableString(f" – {desc}"))
        else:
            pop.extract()
    updated = _first_text(soup, ("#s-lg-guide-header-updated", "#s-lg-page-updated"))
    if updated:
        ex.modified = iso_date(updated) or ex.modified
    desc = _first_text(soup, ("#s-lg-guide-description",))
    if desc:
        ex.description = desc
    elif ex.description and ex.description.startswith("LibGuides:"):
        ex.description = None           # "LibGuides: APA Boot Camp: Home" says nothing new
    _libguides_profiles(ex, soup)


# the title of a profile box that is a job title ("Instruction and Reference Librarian",
# "Librarian III", "Library Director") -- not "Author", "Profile", "Ask a Librarian"
_JOB_TITLE = re.compile(r"\b(?:librarian|director|assistant|specialist|coordinator|manager|dean|archivist"
                        r"|technician)\b", re.I)
_NOT_JOB_TITLE = re.compile(r"^(?:ask|contact|meet|your|my)\b", re.I)
PROFILE_ADDRESS_MAX_WORDS = 20


def _libguides_profiles(ex: Extracted, soup) -> None:
    """The guide owner's profile box (div.s-lib-profile-container: name, e-mail button,
    appointment widget, address, phone, subjects) becomes a section of its own, headed
    "Librarian: <name>" ("Library contact: NYIT Library" for the library's own account),
    with only the lines that answer "who is the nursing librarian / how do I reach them":
    name, job title (the box title, when it is one), subjects, e-mail, phone and a short
    address. Left as it is, its "Librarian / NYIT Library" was too short to be a passage
    and was merged into the end of the unrelated passage before it."""
    for container in _select(soup, ".s-lib-profile-container"):
        name = _text(_select_one(container, ".s-lib-profile-name"))
        box = container.find_parent(class_="s-lib-box")
        title_el = _select_one(box, ".s-lib-box-title") if box is not None else None
        box_title = _text(title_el)
        if not name:
            container.extract()
            continue
        # the library's own account ("NYIT Library", "NYITCOM at A-State") is not a librarian
        role = "Library contact" if re.search(r"\blibrar|\bnyit", name, re.I) else "Librarian"
        lines = [f"Name: {name}"]
        if role == "Librarian" and box_title != name and _JOB_TITLE.search(box_title) \
                and not _NOT_JOB_TITLE.match(box_title):
            lines.append(f"Job title: {box_title}")
        subjects = [_text(a) for a in _select(container, ".s-lib-profile-subjects a")]
        if any(subjects):
            lines.append("Subjects: " + ", ".join(s for s in subjects if s))
        mail = _select_one(container, ".s-lib-profile-email a[href^='mailto:']")
        if mail is not None:
            email = mail["href"][len("mailto:"):].split("?")[0].strip()
            if email:
                lines.append(f"Email: {email}")
        contact = _select_one(container, ".s-lib-profile-contact")
        links = {_text(a) for a in contact.find_all("a")} if contact is not None else set()
        place: List[str] = []
        for line in (contact.get_text("\n", strip=True).split("\n") if contact is not None else []):
            line = _one_line(line).strip(" ,")
            if not line or line.rstrip(":").casefold() == "contact" or line in links:
                continue            # the "Contact:" label, the "Website" link
            if _PHONE.fullmatch(line.replace("Phone:", "").strip()):
                lines.append(f"Phone: {line.replace('Phone:', '').strip()}")
            else:
                place.append(line)
        if place and word_count(", ".join(place)) <= PROFILE_ADDRESS_MAX_WORDS:
            lines.append("Address: " + ", ".join(place))
        heading = soup.new_tag("h2")
        heading.string = f"{role}: {name}"
        ex.profile_headings.add(f"{role}: {name}")
        new = soup.new_tag("div")
        new.append(heading)
        for line in lines:
            p = soup.new_tag("p")
            p.string = line
            new.append(p)
        container.replace_with(new)
        rest = [t for t in box.find_all(string=True) if type(t) is NavigableString and t.strip()
                and t.parent.name not in ("script", "style") and t.find_parent("div") is not new
                and not any(p is title_el for p in t.parents)] if box is not None else []
        if title_el is not None and not rest:
            title_el.extract()          # "Librarian", "Profile": the new heading replaces it


def _online_facts(ex: Extracted, soup) -> None:
    ex.published = ex.published or iso_date(_first_text(soup, (".ArticlePage-datePublished",)))


_ISSUE_SLUG = re.compile(r"^(spring|summer|fall|autumn|winter)[-_]?((?:19|20)\d\d)$", re.I)
_ISSUE_MONTH = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}


def issue_date(url: str) -> Optional[str]:
    """magazine.nyit.edu/fall2024/... -> "2024-09-01": the first day of the issue's season
    (spring 03, summer 06, fall 09, winter 12), an approximate publication date for the
    articles of that issue (Foleon pages carry no date of their own)."""
    segs = path_segments(url)
    m = _ISSUE_SLUG.match(segs[0]) if segs else None
    if m is None:
        return None
    return f"{int(m.group(2)):04d}-{_ISSUE_MONTH[m.group(1).lower()]:02d}-01"


def _magazine_facts(ex: Extracted, soup) -> None:
    """The issue's approximate date (:func:`issue_date`). Links to the other articles of the
    issue ("Strength in Leadership", "Well Wishes for President Foley") that stand alone in
    their block are navigation."""
    ex.published = ex.published or issue_date(ex.url)
    if _LISTING_TITLE.match(title_from_page_title(ex.page_title).split(" - ")[0]):
        return                          # covers and story lists ARE lists of links
    here = normalize_url(ex.url) or ex.url
    for a in soup.find_all("a", href=True):
        target = normalize_url(a["href"], ex.url)
        if not target or url_host(target) != ex.site or target.rstrip("/") == here.rstrip("/"):
            continue
        text = _text(a)
        if not text or word_count(text) > 12:
            continue
        holder = None                   # the block that holds nothing but this link
        for parent in a.parents:
            if parent is None or parent.name in ("article", "main", "body") or _text(parent) != text:
                break
            if parent.name in _BLOCKISH:
                holder = parent
        if holder is not None:
            holder.extract()


def _libcal_facts(ex: Extracted, soup) -> None:
    """LibCal events: when the event takes place comes from their JSON-LD Event
    (startDate/endDate with the local offset, e.g. "2026-11-24T12:30:00-05:00"); the
    page itself shows the description, date, time, place and organiser as text."""
    event = next((i for i in ex.json_ld if any(t.endswith("Event") for t in _ld_types(i))), None)
    if event is None:
        return
    start, end = event.get("startDate"), event.get("endDate")
    start = start if isinstance(start, str) else None
    end = end if isinstance(end, str) else None
    if start:
        ex.event_start, ex.event_end = event_times(start, end, start)


_FACTS = {"nyit": _nyit_facts, "blogs": _blogs_facts, "libanswers": _libanswers_facts,
          "libguides": _libguides_facts, "online": _online_facts, "magazine": _magazine_facts,
          "libcal": _libcal_facts}

# LibCal's weekly hours table: a week's header row ("Nov 23 Monday | Nov 24 Tuesday | ...")
# and the cell of a day without hours yet ("–")
_LIBCAL_DAY = re.compile(r"^[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]+day$")
_NO_HOURS = {"", "-", "\N{EN DASH}", "\N{EM DASH}"}


def _libcal_blocks(blocks: List[Block]) -> List[Block]:
    """LibCal clean-up: the "Event box" heading of the event template goes, and so do the
    rows of the weekly hours tables for weeks whose hours are not published yet (every
    day "–"), with the header row of a week that has no row left."""
    out: List[Block] = []
    for b in blocks:
        if b.kind == "heading" and b.text.strip().casefold() == "event box":
            continue
        if b.kind == "table_row":
            cells = [c.strip() for c in b.text.split(" | ")]
            if len(cells) > 1 and all(c in _NO_HOURS for c in cells[1:]):
                continue
        out.append(b)
    kept: List[Block] = []
    for i, b in enumerate(out):
        if b.kind == "table_row" and _is_week_header(b):
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is None or nxt.kind != "table_row" or _is_week_header(nxt):
                continue
        kept.append(b)
    return kept


def _is_week_header(block: Block) -> bool:
    cells = [c.strip() for c in block.text.split(" | ")]
    return len(cells) >= 2 and all(_LIBCAL_DAY.match(c) for c in cells)


# ---- events -----------------------------------------------------------------------
def _fmt_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "a.m." if dt.hour < 12 else "p.m."
    return f"{hour}:{dt.minute:02d} {suffix}" if dt.minute else f"{hour} {suffix}"


def _fmt_day(dt: datetime) -> str:
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def _event_local_times(end: Optional[str], local_start: Optional[str]
                       ) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(start, end) in the event's local time, or (None, None) without a local start time.
    LiveWhale gives endDate in UTC and og:start_time with the local offset."""
    s_local = _parse_iso(local_start)
    if s_local is None or s_local.utcoffset() is None:
        return None, None
    offset = s_local.utcoffset() or timedelta(0)
    e_utc = _parse_iso(end)
    e_local = None
    if e_utc is not None and e_utc.utcoffset() is not None:
        e_local = (e_utc - (e_utc.utcoffset() or timedelta(0)) + offset).replace(tzinfo=s_local.tzinfo)
    return s_local, e_local


def event_times(start: Optional[str], end: Optional[str], local_start: Optional[str]
                ) -> Tuple[Optional[str], Optional[str]]:
    """(event_start, event_end) as ISO date-times in the event's local time
    ("2026-11-30T13:00:00-05:00", "2026-11-30T14:00:00-05:00"); event_end is None for an
    all-day event or an event without an end. Without a local start time: the JSON-LD
    values as they are."""
    s_local, e_local = _event_local_times(end, local_start)
    if s_local is None:
        return iso_date(start), iso_date(end)
    return iso_date(local_start), (e_local.isoformat() if e_local is not None and e_local > s_local else None)


def event_when(start: Optional[str], end: Optional[str], local_start: Optional[str]) -> str:
    """ "Tuesday, December 1, 2026, 12:45 p.m. to 1:45 p.m." in the event's local time.

    LiveWhale gives startDate/endDate in UTC and og:start_time with the local offset; the
    end time is shifted by the same offset. All-day events (start == end at midnight)
    give the date only, and so does an event without a local start time."""
    s_local, e_local = _event_local_times(end, local_start)
    if s_local is None:
        s_utc = _parse_iso(start)
        return _fmt_day(s_utc) if s_utc is not None else ""
    if s_local.hour == 0 and s_local.minute == 0 and (e_local is None or e_local == s_local):
        return _fmt_day(s_local)
    text = f"{_fmt_day(s_local)}, {_fmt_time(s_local)}"
    if e_local is not None and e_local > s_local:
        if e_local.date() == s_local.date():
            text += f" to {_fmt_time(e_local)}"
        else:
            text += f" to {_fmt_day(e_local)}, {_fmt_time(e_local)}"
    return text


# "Register" button text and "Login Required" notes that leak into LiveWhale descriptions
# ("... graduate options.    Register", "RegisterLogin Required")
_EVENT_JUNK = r"(?:register(?:\s*(?:now|here|today))?|rsvp(?:\s*(?:now|here))?|login\s*required|buy tickets)"
_EVENT_JUNK_HEAD = re.compile(r"^(?:\s*" + _EVENT_JUNK + r"\s*[.!:]?)+", re.I)
_EVENT_JUNK_TAIL = re.compile(r"(?:\s*\b" + _EVENT_JUNK + r"\s*[.!]?)+\s*$", re.I)
# the numbers of the event's image left at the start of the JSON-LD description
# ("  1141280720      This lecture launches ...")
_EVENT_IMAGE_NUMBER = re.compile(r"^\d{8,}\s+(?=\D)")


def _event_text(desc: str) -> str:
    desc = _one_line(html_unescape(desc))     # LiveWhale's JSON-LD holds HTML entities ("&#160;")
    desc = _EVENT_IMAGE_NUMBER.sub("", desc.strip())
    # only at the ends, or alone between two sentences: "register" in a sentence is a real word
    desc = _EVENT_JUNK_TAIL.sub("", _EVENT_JUNK_HEAD.sub("", desc))
    desc = re.sub(r"(?<=[.!?])\s+Register\s+(?=[A-Z])", " ", desc)
    return desc.strip() if re.search(r"\w", desc) else ""


def _event_blocks(ex: Extracted) -> List[Block]:
    """The event (description, date and time, place) from og:/JSON-LD metadata."""
    event = next((i for i in ex.json_ld if any(t.endswith("Event") for t in _ld_types(i))), None)
    desc = event.get("description") if event is not None else None
    if not isinstance(desc, str) or not desc.strip():
        desc = ex.meta.get("og:description") or ex.meta.get("description") or ""
    desc = _event_text(desc)
    ex.description = desc or None
    blocks: List[Block] = [Block("text", desc)] if desc else []
    start = event.get("startDate") if event is not None else None
    end = event.get("endDate") if event is not None else None
    start = start if isinstance(start, str) else None
    end = end if isinstance(end, str) else None
    local_start = ex.meta.get("og:start_time") or ex.meta.get("event:start_time")
    when = event_when(start, end, local_start)
    if when:
        blocks.append(Block("text", f"When: {when}"))
    where: List[str] = []
    if event is not None:
        locs = event.get("location") or []
        for loc in (locs if isinstance(locs, list) else [locs]):
            if isinstance(loc, dict):
                name = _one_line(str(loc.get("name") or ""))
                addr = loc.get("address")
                if isinstance(addr, dict):
                    addr = ", ".join(str(addr[k]) for k in ("streetAddress", "addressLocality",
                                                            "addressRegion", "postalCode") if addr.get(k))
                addr = _one_line(str(addr or ""))
                parts = [name] if name else []
                if addr and addr not in name:
                    parts.append(addr)
                if parts:
                    where.append(", ".join(parts))
            elif isinstance(loc, str) and loc.strip():
                where.append(_one_line(loc))
        if not where and "Online" in str(event.get("eventAttendanceMode") or ""):
            where.append("Online")
    if where:
        blocks.append(Block("text", "Where: " + "; ".join(dict.fromkeys(where))))
    # when the event takes place: not a publication date (38 of 101 events are in the
    # future); parse_site_page decides where it goes (event_start/event_end, see there)
    ex.event_start, ex.event_end = event_times(start, end, local_start)
    ex.published = None
    ex.modified = None  # article:modified_time is the same template date on every event
    return blocks


def _is_event_page(ex: Extracted) -> bool:
    return any(any(t.endswith("Event") for t in _ld_types(i)) for i in ex.json_ld)


# ---- main extraction ----------------------------------------------------------------
def _profile(url: str, profile: Optional[sites.SiteProfile]) -> sites.SiteProfile:
    if profile is not None:
        return profile
    host = url_host(url)
    return sites.profile_for(host) or replace(sites.DEFAULT_PROFILE, host=host or "*")


def _read_page_meta(ex: Extracted) -> None:
    """Description and dates from <meta>/JSON-LD -- unless they describe another page:
    apps.nyit.edu copies the www homepage's <head> (og:url https://www.nyit.edu/)."""
    meta = ex.meta
    og_url = meta.get("og:url")
    if og_url and not _same_page(og_url, ex.url):
        other = normalize_url(og_url) or ""
        if not path_segments(other) and path_segments(ex.url):
            ex.meta_is_page = False
            return
    for item in ex.json_ld:
        item_url = item.get("url") or item.get("@id")
        if isinstance(item_url, str) and item_url.startswith("http") and "#" not in item_url \
                and not _same_page(item_url, ex.url):
            continue
        if not ex.published and isinstance(item.get("datePublished"), str):
            ex.published = iso_date(item["datePublished"])
        if not ex.modified and isinstance(item.get("dateModified"), str):
            ex.modified = iso_date(item["dateModified"])
    ex.published = iso_date(meta.get("article:published_time")) or ex.published
    ex.modified = iso_date(meta.get("article:modified_time")) or ex.modified
    desc = meta.get("description") or meta.get("og:description") or meta.get("twitter:description")
    ex.description = _one_line(desc or "") or None


def extract(html: str, url: str, profile: Optional[sites.SiteProfile] = None) -> Extracted:
    """Locate, clean and walk the main content of a non-catalog page (no boilerplate
    removal yet: that needs all the pages of the site, see :func:`find_boilerplate`)."""
    profile = _profile(url, profile)
    host = url_host(url)
    soup = make_soup(html or "")
    template = detect_template(host, soup)
    ex = Extracted(url=url, site=host, profile=profile, template=template)
    body = soup.find("body")
    ex.body_classes = _classes(body)
    ex.meta = read_meta(soup)
    ex.json_ld = read_json_ld(soup)
    ex.page_title = _one_line(soup.title.get_text(" ")) if soup.title else ""
    if template.name == "events":
        for alt in _select(soup, ".breadcrumb-home"):
            alt.extract()          # the calendar-home variant of the LiveWhale breadcrumb
    ex.breadcrumbs = extract_breadcrumbs(
        soup, template.breadcrumbs + ((profile.breadcrumb_selector,) if profile.breadcrumb_selector else ())
        + (".yoast-breadcrumbs", "div.Breadcrumbs", "[class*=breadcrumb i]"))
    _read_page_meta(ex)
    hook = _FACTS.get(template.name)
    if hook is not None:
        hook(ex, soup)

    # content root
    root = None
    for sel in dict.fromkeys(template.content + tuple(profile.content_selectors)):
        el = _select_one(soup, sel)
        if el is not None and el.get_text(strip=True):
            root = el
            break
    if root is None:
        root = soup.find("main") or body
    ex.root = root
    # title candidates must be read before the hero/header is removed with the noise
    h1s = [_first_text(soup, template.titles, inline=True),
           _inline_text(root.find("h1")) if root is not None else "", _inline_text(soup.find("h1"))]
    ex.title = _resolve_title(ex, h1s)
    if template.name in ("nyit", "online") and path_segments(url) and "home" not in ex.body_classes \
            and "single-bio" not in ex.body_classes:
        og = _one_line(ex.meta.get("og:title", "")) if ex.meta_is_page else ""
        ex.full_title = _fuller_title(ex.title, [title_from_page_title(og), title_from_page_title(ex.page_title)])
    if root is not None:
        extra: Tuple[str, ...] = ()
        if template.name == "nyit" and any(c.startswith("single-") for c in ex.body_classes):
            # articles and bios end with "More News" / "More Features" teaser cards
            extra = (".Section:has(.Feed)", ".Section:has(.List)", ".Section:has(.Card)")
        clean_content(root, tuple(sites.COMMON_REMOVE_SELECTORS) + tuple(profile.remove_selectors)
                      + template.remove + extra)

    if template.name == "events" or (profile.use_meta_description and _is_event_page(ex)):
        ex.is_event = True
        ex.blocks = _event_blocks(ex)
        ex.synthetic = {id(b) for b in ex.blocks}
        ex.total_words = sum(b.words for b in ex.blocks)
        return ex

    blocks, ex.num_tables = html_parser.walk_content(root)
    # the title repeated as the first heading of the content (online.nyit.edu, blogs)
    first_heading = next((b for b in blocks if b.kind == "heading"), None)
    if first_heading is not None and first_heading.level <= 2 and block_key(first_heading.text) == block_key(ex.title):
        blocks.remove(first_heading)
    blocks = [b for b in blocks if not _is_junk(b)]
    if template.name == "libanswers":
        blocks = _drop_library_contacts(blocks, ex.title)
    elif template.name == "libcal":
        blocks = _libcal_blocks(blocks)
    blocks = _drop_nav_sections(blocks, _link_only_items(root))
    if ex.template.name in ("nyit", "blogs", "magazine", "online") and \
            (any(c.startswith("single-") for c in ex.body_classes) or ex.template.name != "nyit"):
        blocks = _drop_bylines(blocks)
    if ex.profile_headings:
        # a librarian's profile repeats on every guide of that librarian: never boilerplate
        in_profile = False
        for b in blocks:
            if b.kind == "heading":
                in_profile = b.text in ex.profile_headings
            elif in_profile:
                ex.synthetic.add(id(b))
    if ex.facts:
        facts = Block("text", " | ".join(dict.fromkeys(ex.facts)))
        blocks.insert(0, facts)
        ex.synthetic.add(id(facts))
    if profile.use_meta_description and ex.description and \
            sum(b.words for b in blocks if b.kind != "heading") < 40:
        desc = Block("text", ex.description)
        blocks.insert(0, desc)
        ex.synthetic.add(id(desc))
    ex.blocks = blocks
    ex.links_words, ex.total_words, ex.headings, ex.linked_headings = _link_share(root)
    return ex


# ---------------------------------------------------------------------------
# Boilerplate
# ---------------------------------------------------------------------------
DESCRIPTION_KEY = "[description] "   # boilerplate entries for <meta> descriptions


def find_boilerplate(pages: Iterable[Tuple[str, str]], min_pages: int = 20,
                     min_share: float = 0.3) -> Dict[str, Set[str]]:
    """Blocks repeated on many pages of the same site -> {site: {block_key, ...}}.

    ``pages`` = (url, html) pairs (url = the page's final URL). A non-heading block
    (paragraph, list item, table row) is boilerplate when it occurs on at least
    ``min_share`` of the site's pages, for sites with at least ``min_pages`` pages.
    A <meta> description shared that widely is recorded too (as ``DESCRIPTION_KEY +
    text``) so that it is not used as the description of every page.
    catalog.nyit.edu is never analysed: its repeated curriculum rows are real content.
    """
    page_counts: Counter = Counter()
    block_counts: Dict[str, Counter] = defaultdict(Counter)
    for url, html in pages:
        host = url_host(url)
        profile = sites.profile_for(host)
        if profile is None or profile.parser == "catalog":
            continue
        try:
            ex = extract(html, url, profile)
        except Exception as exc:  # never let one page stop the analysis
            log.warning("boilerplate scan failed on %s: %s", url, exc)
            continue
        keys = {block_key(b.text) for b in ex.blocks
                if b.kind != "heading" and id(b) not in ex.synthetic and b.text.strip()}
        if ex.description:
            keys.add(DESCRIPTION_KEY + block_key(ex.description))
        page_counts[host] += 1
        block_counts[host].update(k for k in keys if k)
    found: Dict[str, Set[str]] = {}
    for host, n in page_counts.items():
        if n < min_pages:
            continue
        threshold = max(2, math.ceil(min_share * n))
        keys = {k for k, c in block_counts[host].items() if c >= threshold}
        if keys:
            found[host] = keys
            log.info("%s: %d boilerplate blocks (on >= %d of %d pages)", host, len(keys), threshold, n)
    return found


def _drop_boilerplate(ex: Extracted, keys: Optional[Set[str]]) -> List[Block]:
    if not keys:
        return list(ex.blocks)
    return [b for b in ex.blocks
            if b.kind == "heading" or id(b) in ex.synthetic or block_key(b.text) not in keys]


# Words that decide what a rule says: two paragraphs differing in one of them are two
# rules ("... only one course in academic Year 2" / "... only one clerkship per academic
# year", "Clinical Education I" / "II", "NIH" / "NSF"), never two copies of one paragraph.
_DECISIVE_WORD = re.compile(r"\d|^(?:[ivxlcdm]+|first|second|third|fourth|fifth|sixth|last|not|no|"
                            r"never|nor|none|cannot|can|must|may|shall|should|only|all|any|each|"
                            r"before|after|more|less|fewer|minimum|maximum)$")


def _near_copy(words: Sequence[str], other: Sequence[str]) -> bool:
    """Is one paragraph a slightly edited copy of the other (the magazine's desktop and
    mobile copies: "uphold" / "upheld")? At most 3 words differ on each side, each one
    a near-spelling of a word of the other copy, and none of them is a number, a roman
    numeral or a word that changes a rule (:data:`_DECISIVE_WORD`)."""
    a, b = set(words), set(other)
    if len(a & b) < 0.9 * len(a | b):
        return False
    only_a, only_b = a - b, b - a
    if len(only_a) > 3 or len(only_b) > 3:
        return False
    if any(_DECISIVE_WORD.search(w) for w in only_a | only_b):
        return False
    return all(any(difflib.SequenceMatcher(None, w, v).ratio() >= 0.6 for v in theirs)
               for mine, theirs in ((only_a, only_b), (only_b, only_a)) for w in mine)


def _drop_repeats(blocks: List[Block], min_words: int = 30, kinds: Tuple[str, ...] = ("text",),
                  per_section: bool = True, near_copy_words: int = 0) -> List[Block]:
    """Long paragraphs printed twice in the same place are kept once: responsive layouts
    render a mobile and a desktop copy, tabs repeat the same text under the same heading.

    Only exact copies (same :func:`block_key`) of the same section (same heading path) count
    by default: a paragraph repeated under another heading is part of that section too ("The
    building adjacent to the PARKING GARAGE is the STUDENT UNION" under each route of a
    directions page, the same advising instructions under two programs), and two policy
    rules that differ in one word are two rules. Short repeated lines are always kept: "Last
    day to withdraw" under Fall and under Spring are two facts.

    The magazine (Foleon renders every block twice) passes ``per_section=False`` and
    ``near_copy_words=50``: copies are dropped anywhere on the page, and so are paragraphs of
    50+ words that are a near-copy of an earlier one with the same first six words
    (:func:`_near_copy`)."""
    seen: Set[Tuple[Tuple[str, ...], str]] = set()
    near: Dict[Tuple[Tuple[str, ...], str], List[List[str]]] = defaultdict(list)
    path: List[Tuple[int, str]] = []
    out = []
    for b in blocks:
        if b.kind == "heading":
            if per_section:
                while path and path[-1][0] >= b.level:
                    path.pop()
                path.append((b.level, block_key(b.text)))
            out.append(b)
            continue
        if b.kind in kinds and b.words >= min_words:
            scope = tuple(h for _, h in path)
            key = (scope, block_key(b.text))
            if key in seen:
                continue
            seen.add(key)
            if near_copy_words and b.words >= near_copy_words:
                words = re.findall(r"\w+", b.text.casefold())
                head = (scope, " ".join(words[:6]))
                if any(_near_copy(words, other) for other in near[head]):
                    continue
                near[head].append(words)
        out.append(b)
    return out


_NAV_HEADING = re.compile(r"^(quick links|related links|helpful links|useful links|important links|links|"
                          r"jump to\b.*|on this page|in this section|explore( more)?|learn more|"
                          r"next steps|connect with us|more resources|see also)\s*:?$", re.I)
_BYLINE = re.compile(r"^(?:[Bb]y|[Ww]ritten by|[Ss]tory by|[Ww]ords by)\s+[A-Z][\w.'’-]*"
                     r"(?:,?\s+(?:[A-Z][\w.'’-]*|and|&))*$")     # "By Carina Storrs, Ph.D."


def _drop_nav_sections(blocks: List[Block], link_items: Set[str]) -> List[Block]:
    """ "Quick Links" / "Related Links" headings followed only by lists of links."""
    out: List[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind == "heading" and _NAV_HEADING.match(b.text):
            j = i + 1
            while j < len(blocks) and not (blocks[j].kind == "heading" and blocks[j].level <= b.level):
                j += 1
            body = blocks[i + 1:j]
            if body and all(x.kind == "list_item" and block_key(x.text) in link_items for x in body):
                i = j
                continue
        out.append(b)
        i += 1
    return out


def _drop_bylines(blocks: List[Block]) -> List[Block]:
    """ "By Alix Sobler" at the start or end of an article."""
    n = len(blocks)
    return [b for k, b in enumerate(blocks)
            if not (b.kind == "text" and (k < 3 or k >= n - 3) and _BYLINE.match(b.text.strip()))]


def _link_only_items(root: Optional[Tag]) -> Set[str]:
    """block_key of every <li> that is just one link (navigation lists)."""
    keys: Set[str] = set()
    if root is None:
        return keys
    for li in root.find_all("li"):
        links = li.find_all("a")
        if len(links) == 1 and _text(links[0]) and _text(links[0]) == _text(li):
            keys.add(block_key(_text(li)))
    return keys


def _drop_dangling_intros(blocks: List[Block]) -> List[Block]:
    """ "For further assistance:" left at the end of the page after the boilerplate it
    introduced (the library addresses on LibAnswers) was removed."""
    while blocks and blocks[-1].kind == "text" and blocks[-1].text.rstrip().endswith(":") \
            and blocks[-1].words <= 8:
        blocks = blocks[:-1]
    return blocks


# "For further assistance:", "For futher assistance, please contact your campus library:",
# "Visit our libraries": the line that introduces the library address cards
_CONTACTS_INTRO = re.compile(r"^(?:for fu\w*er ass?is\w*nce\b.{0,60}|visit our librar(?:y|ies)\b.{0,40}"
                             r"|contact (?:us|your (?:campus )?library)\b.{0,40})$", re.I)
# lines of a library's address card: "- Manhattan Library", "1855 Broadway, New York, NY
# 10023-7692", "Nelson A. Rockefeller Academic Center", "(516) 686-7657"
_CONTACT_LINE = re.compile(
    r"^(?:[-•]\s*)?(?:.{0,60}\blibrary\b.{0,40}"
    r"|.{0,60}\b(?:blvd|boulevard|broadway|street|st|avenue|ave|road|rd|center|centre|hall|building)\b\.?.{0,50}"
    r"|.{0,60}\b[A-Z]{2}\s+\d{5}(?:-\d{4})?"
    r"|(?:phone|tel|fax)?:?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]\d{4})$", re.I)
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]\d{4}")


def _is_contact_card(b: Block) -> bool:
    """A library's address card: a library name, its address and phone number, nothing else."""
    if b.kind == "heading":
        return False
    lines = [ln.strip() for ln in b.text.split("\n") if ln.strip()]
    return bool(lines) and all(word_count(ln) <= 12 and _CONTACT_LINE.match(ln) for ln in lines) \
        and re.search(r"\blibrary\b", b.text, re.I) is not None and _PHONE.search(b.text) is not None


# questions whose answer may BE the address cards ("Where is the Manhattan library?",
# "What is the library's phone number?") -- not "Where can I find articles on ...?"
_ASKS_FOR_CONTACTS = re.compile(r"\bwhere (?:is|are) (?:the |your |nyit'?s? )?(?:\w+ ){0,2}librar|\baddress(?:es)?\b"
                                r"|\blocations?\b|\bdirections\b|\b(?:tele)?phone\b|\bcontact\b|\bhours\b"
                                r"|\bin touch\b|\breach (?:a|the|your) librar", re.I)


def _is_contacts_intro(b: Block) -> bool:
    text = b.text.strip()
    return word_count(text) <= 14 and (_CONTACTS_INTRO.match(text) is not None
                                       or (b.kind != "heading" and text.endswith(":")))


def _drop_library_contacts(blocks: List[Block], question: str = "") -> List[Block]:
    """LibAnswers answers often end with "For further assistance:" and the address card of
    every library (name, address, phone): 61 of 285 answers, 3,000 of their 11,200 words,
    which would make each of those answers match any question about a library's address or
    phone number. A run of such cards that ends the answer (followed by nothing or by a
    heading such as "Comments (0)") is dropped with the line that introduces it ("For
    further assistance:", "Visit our libraries"); without such a line only a run of 2+ cards
    after 20+ words of answer. Kept: cards in the middle of an answer, addresses inside
    sentences, and the cards of a question about where / how to reach a library."""
    if _ASKS_FOR_CONTACTS.search(question or ""):
        return blocks
    out: List[Block] = []
    i, n = 0, len(blocks)
    while i < n:
        if not _is_contact_card(blocks[i]):
            out.append(blocks[i])
            i += 1
            continue
        j = i
        while j < n and _is_contact_card(blocks[j]):
            j += 1
        at_end = j == n or blocks[j].kind == "heading"
        intro = bool(out) and _is_contacts_intro(out[-1])
        before = sum(b.words for b in (out[:-1] if intro else out) if b.kind != "heading")
        if at_end and (intro or (j - i >= 2 and before >= 20)):
            if intro:
                out.pop()
        else:
            out.extend(blocks[i:j])
        i = j
    return out


# a heading that is a sentence or holds one: "Step 5. Access your article.", "Step 1. Click
# BROWSE SOURCES.", "Associate Prof. Pablo Lorenzo-Eiroa", "File Name: LastName_FirstName.pdf",
# "2. Measuring building performance" -- not "Live Chat!", "New To OER?", "A.A.A.S."
_SENTENCE_HEADING = re.compile(r"[a-z0-9)\]”\"'’]\.$|[^\W_][.:]\s+\S|^(?:[Ss]tep\s+)?\d{1,2}(?:\.\d+)*[.)]?\s")
# the title of a feed, a chat box or a social-media button, even when it is long:
# "RSS Feed / The New York Times: Science Times", "New Books at NYC Library in 2026",
# "Follow: @nyitlibraries and @nyitart_arch_library", "New York State Library - News";
# and a LibGuides profile heading (_libguides_profiles) left without its lines
_WIDGET_HEADING = re.compile(r"\brss\b|\bfeeds?\b|\blive chat\b|^follow\b|\bnew\s+(?:books|titles|arrivals)\b"
                             r"|\blatest (?:articles|news|issues?)\b|^news\b|\bnews$|^(?:librarian|library contact): ",
                             re.I)
TEXT_HEADING_MIN_WORDS = 6


def _is_text_heading(b: Block, nxt: Optional[Block] = None) -> bool:
    """Is this heading content rather than a title? Authors style sentences, steps and
    names with <h3>/<h5> (LibGuides' "Step 1. Locate the name of the PUBLICATION in your
    citation ...", the panelists of a blogs.nyit.edu lecture), and a LibGuides box of
    screenshots or a video has only its title for text: a heading of 6+ words or a
    sentence is kept, unless it names a widget (a feed, the chat box); so is a label
    ("PANELISTS:") that introduces such a heading (``nxt``)."""
    text = b.text.strip()
    if _WIDGET_HEADING.search(text):
        return False
    if text.endswith(":") and nxt is not None and nxt.kind == "heading" and _is_text_heading(nxt):
        return True
    return b.words >= TEXT_HEADING_MIN_WORDS or _SENTENCE_HEADING.search(text) is not None


def _tidy_headings(blocks: List[Block]) -> List[Block]:
    """Headings with nothing under them (their content was noise: a form, a feed, a row
    of buttons) are dropped -- except runs of 3+ such headings, which are link tiles
    ("CFE Weekly Teaching Notes", "Build Ahead", ...) and become list items, and headings
    that are text themselves (:func:`_is_text_heading`), which become a bold line of the
    section above (as headings they would end up in no section: one with no body text is
    dropped)."""
    def empty(i: int) -> bool:
        b = blocks[i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        return b.kind == "heading" and (nxt is None or (nxt.kind == "heading" and nxt.level <= b.level))

    flags = [empty(i) for i in range(len(blocks))]
    out: List[Block] = []
    i = 0
    while i < len(blocks):
        if not flags[i]:
            out.append(blocks[i])
            i += 1
            continue
        j = i
        while j < len(blocks) and flags[j] and blocks[j].level == blocks[i].level:
            j += 1
        if j - i >= 3:
            out.extend(Block("list_item", "- " + b.text, level=1) for b in blocks[i:j])
        else:
            # a label is kept with the heading it introduces when that one becomes text too
            out.extend(Block("text", blocks[k].text, anchor=blocks[k].anchor, strong=True) for k in range(i, j)
                       if _is_text_heading(blocks[k], blocks[k + 1] if k + 1 < len(blocks) and flags[k + 1] else None))
        i = max(j, i + 1)
    return out


# ---------------------------------------------------------------------------
# Page type, program, level
# ---------------------------------------------------------------------------
_WP_TYPES = (("single-post", "news"), ("single-bio", "bio"), ("single-media_coverage", "media_coverage"),
             ("single-expert_insight", "expert_insight"), ("single-event", "event"),
             ("single-tribe_events", "event"))
_LISTING_TITLE = re.compile(r"^(cover|issues|featured stories|all (news|events|posts)|news archive|"
                            r"media coverage archive|archive|site links|sitemap|degrees)$", re.I)
_NEWSY = {"news", "media_coverage", "expert_insight", "bio", "magazine_article", "blog_post", "event"}


def classify_site_page(ex: Extracted) -> str:
    """Page type (models.PAGE_TYPES) of a non-catalog page."""
    host, segs = ex.site, path_segments(ex.url)
    classes = set(ex.body_classes)
    if ex.is_event:
        return "event"
    for cls, ptype in _WP_TYPES:
        if cls in classes:
            return ptype
    if not segs or "home" in classes:
        return "home"
    if ex.template.name == "nyit" and segs[0] == "academics" and len(segs) >= 3 and segs[1] == "degrees":
        return "curriculum" if len(segs) >= 4 and re.search(r"curr+iculum|semester-map", segs[3]) else "degree"
    if host == "apps.nyit.edu" and segs[0] == "policies":
        return "policy" if len(segs) > 1 else "listing"
    if host == "magazine.nyit.edu":
        name = title_from_page_title(ex.page_title).split(" - ")[0]
        return "listing" if _LISTING_TITLE.match(name) else "magazine_article"
    if host == "libanswers.nyit.edu":
        return "faq" if segs[0] == "faq" else "listing"
    if host == "libguides.nyit.edu":
        # subject/browse pages ("General, Multi-Disciplinary") are lists of guides
        return "listing" if "s-lib-public-body" in classes else "library_guide"
    if host == "libcal.nyit.edu" and segs[0] == "event":
        return "event"
    if host == "libcal.nyit.edu" and segs[0] == "hours":
        return "library_hours"
    if host == "blogs.nyit.edu":
        return "blog_post" if len(segs) >= 2 else "listing"
    if is_communication(ex.url):
        return "communication"
    if host == "online.nyit.edu":
        if segs[0] == "blog":
            return "blog_post" if len(segs) >= 2 else "listing"
        if slug_degree(segs[0]):
            return "curriculum" if len(segs) >= 2 and segs[1] == "curriculum" else "degree"
    if ex.template.name == "nyit" and len(segs) == 2 and segs[0] == "academics" and segs[1] in SCHOOL_BY_SLUG:
        return "school"
    if host == "www.nyit.edu" and segs in (["medicine"], ["medicine", "college-of-osteopathic-medicine"]):
        return "school"
    if _LISTING_TITLE.match(ex.title or ""):
        return "listing"
    # hubs: mostly links, little prose; teaser lists: most headings are links
    prose = ex.total_words - ex.links_words
    if ex.total_words >= 15 and ex.links_words >= 0.6 * ex.total_words and prose < 150:
        return "listing"
    if ex.linked_headings >= 4 and ex.linked_headings >= 0.6 * ex.headings and prose < 350:
        return "listing"
    return "page"


_HTML_FILE = re.compile(r"\.html?$", re.I)
_YMD_IN_NAME = re.compile(r"(?<!\d)((?:19|20)\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_MONTH_YEAR_IN_NAME = re.compile(_MONTH_RE + r"[\s_-]*((?:19|20)\d\d)(?!\d)", re.I)


def is_communication(url: str) -> bool:
    """files.nyit.edu/files/... HTML files are e-mails to the university community saved
    as web pages (the provost's 2020-2021 "Faculty Update" messages, "Indoor Masks Policy
    Effective Immediately"): dated announcements, not standing information pages."""
    segs = path_segments(url)
    return url_host(url) == "files.nyit.edu" and bool(segs) and _HTML_FILE.search(segs[-1]) is not None


def communication_date(url: str, title: str) -> Optional[str]:
    """Date of such an e-mail: a full date in its title ("Faculty Update: April 20, 2020"),
    else YYYYMMDD in its file name (20210729_IndoorMasksPolicy.html), else a month and year
    in the title ("Faculty Update September 2020") or the file name (FacultyUpdateApr2021,
    Faculty_Update__October_2021) -> the first day of that month. None when neither says
    the year ("Faculty_Update__September_10.html")."""
    segs = path_segments(url)
    name = segs[-1] if segs else ""
    full = iso_date(title)
    if full:
        return full
    m = _YMD_IN_NAME.search(name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    month = iso_date(title, allow_month=True)
    if month:
        return f"{month}-01"
    m = _MONTH_YEAR_IN_NAME.search(name)
    if m:
        return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()[:3]]:02d}-01"
    return None


_ABBR_PAREN = re.compile(r"\s*\((?:[A-Z][A-Za-z.]{1,7}|[A-Z.]{2,8})\)")      # " (MBA)", " (M.S.)"
_ONLINE_SUFFIX = re.compile(r"\s*[–—-]\s*online$|\s*\(online\)$", re.I)
_DEGREE_WORDS = re.compile(r"^(the )?(online )?(master|bachelor|doctor)(['’]s)?( degree)?( of| in)?\s+", re.I)
_SUBPAGE_PREFIX = re.compile(r"^(curriculum( requirements)?|semester map|admission requirements)"
                             r"\s*(for\b|:|–|-)\s*", re.I)


def _program_name(raw: str, degree: str) -> str:
    """Program name in the catalog's "<Field>, <Degree>" style.

    "Master of Business Administration (MBA)" -> "Master of Business Administration, M.B.A.";
    "Master of Cybersecurity – Online" + M.S. -> "Cybersecurity, M.S.";
    "Artificial Intelligence M.S." -> "Artificial Intelligence, M.S."."""
    base = _ONLINE_SUFFIX.sub("", _ABBR_PAREN.sub("", raw)).strip(" ,–-")
    base = _SUBPAGE_PREFIX.sub("", base) or base       # "Semester Map: Digital Art, B.F.A."
    name = html_parser.canonical_program_name(base) or base
    if find_degree(name) == degree and (name.rstrip().endswith(degree) or not _DEGREE_WORDS.match(name)):
        return name             # "Management, M.B.A. – Marketing" (degree inside the name)
    field_name = _DEGREE_WORDS.sub("", name)
    m = re.search(r"[,\s]+([A-Za-z.]{2,10})$", field_name)
    if m and re.sub(r"\W", "", m.group(1)).upper() == re.sub(r"\W", "", degree).upper():
        field_name = field_name[:m.start()]          # "Energy Management, MEM" -> "Energy Management"
    field_name = field_name.strip(" ,–-")
    return f"{field_name or name}, {degree}"


def program_info(ex: Extracted, page_type: str) -> Tuple[Optional[str], Optional[str]]:
    """(program, degree) of degree pages and their sub-pages (curriculum, admission ...).

    The program comes from the breadcrumb after "Degrees" (www/vancouver) or the first
    crumb after "Home" (online.nyit.edu), else from the title of the degree page itself;
    the degree from that name, else from the URL slug (/artificial-intelligence-ms/)."""
    if page_type not in ("degree", "curriculum"):
        return None, None
    crumbs = ex.breadcrumbs
    segs = path_segments(ex.url)
    candidates: List[str] = []
    if "Degrees" in crumbs:                 # Home > Academics > Degrees > <program> > ...
        i = crumbs.index("Degrees")
        if i + 1 < len(crumbs):
            candidates.append(crumbs[i + 1])
    elif ex.site == "online.nyit.edu" and len(crumbs) >= 2:
        candidates.append(crumbs[1])        # Home > Data Science M.S. > Curriculum
    landing = (len(segs) == 3) if ex.template.name == "nyit" else (len(segs) == 1)
    if landing or not candidates:
        candidates.append(ex.title)
    slug = segs[2] if ex.template.name == "nyit" and len(segs) > 2 else (segs[0] if segs else "")
    from_slug = slug_degree(slug)
    for raw in candidates:
        degree = find_degree(_ABBR_PAREN.sub("", raw)) or find_degree(raw)
        if degree:
            return _program_name(raw, degree), degree
    if from_slug:
        named = [c for c in candidates if c and (landing or c is not ex.title)]
        return (_program_name(named[0], from_slug) if named else None), from_slug
    return None, None


def _mentions_program(title: str, program: str) -> bool:
    """Does the title already name the program ("Admission Requirements: Master in
    Cybersecurity" for "Cybersecurity, M.S.")?"""
    field_name, _, degree = program.rpartition(",")
    field_name = _DEGREE_WORDS.sub("", field_name)
    words = [w for w in re.findall(r"[a-z]{3,}", field_name.casefold()) if w not in ("and", "the", "for")]
    title_words = set(re.findall(r"[a-z]{2,}", title.casefold()))
    abbr = re.sub(r"\W", "", degree).casefold()
    if len(abbr) >= 3 and abbr in title_words:          # "MBA Students" names the M.B.A.
        return True
    return bool(words) and all(w in title_words for w in words)


def _level(ex: Extracted, page_type: str, degree: Optional[str]) -> Optional[str]:
    if (page_type in _NEWSY and page_type != "event") or page_type == "faq":
        return None     # an article or a reader's question is not about one level of study
    level = html_parser.degree_level(degree.split("/")[0] if degree else None)
    if level:
        return level
    if degree and degree.startswith("M."):
        return "graduate"
    for crumb in ex.breadcrumbs:
        m = re.match(r"(undergraduate|graduate|doctoral)\b", crumb, re.I)
        if m:
            return m.group(1).lower()
    # "Graduate Admissions", /admissions/first-year/ -- but not "Grad Fair" (graduation)
    text = " ".join([ex.title] + path_segments(ex.url))
    if re.search(r"\bundergrad(uate)?s?\b|\bfirst[- ]year\b", text, re.I):
        return "undergraduate"
    if re.search(r"\bgraduate\b", text, re.I):
        return "graduate"
    if ex.site == "www.nyit.edu" and path_segments(ex.url)[:1] == ["medicine"]:
        return "professional"   # the medical school (D.O.), like the catalog's /medicine pages
    return None


def _campuses(ex: Extracted, text: str) -> List[str]:
    found = html_parser.find_campuses(f"{ex.title}\n{text}")
    if ex.site == "vancouver.nyit.edu" and "Vancouver" not in found:
        found.append("Vancouver")
    return found


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
_NOT_FOUND = re.compile(r"^\s*(404\b|page not found\b|not found$)", re.I)

# Pages served with HTTP 200 that hold no content (see soft_404_reason)
_SERVER_DEFAULT_TITLE = re.compile(
    r"^(?:IIS(?:\s*\d+(?:\.\d+)?)?(?: Windows Server)?|Internet Information Services|Welcome to nginx!?"
    r"|Apache2? (?:\w+ )?Default Page\b.*|Test Page for the (?:Apache|Nginx)\b.*|It works!?)$", re.I)
_APP_ERROR = re.compile(r"\binvalid parameter\b|\breport issue to an administrator\b"
                        r"|\ba required parameter is missing\b|\bmissing parameter '", re.I)
_PLACEHOLDER_TEXT = re.compile(r"\badd your (?:terms and conditions|privacy statement) for the site\b"
                               r"|\byour browser is (?:unsupported|not supported)\b", re.I)
_STUB_TITLE = re.compile(r"^(?:test(?:ing)?(?:\s+[-/|:]\s+.*)?|cache|untitled(?: page)?|sample page"
                         r"|hello world!?|coming soon!?|under construction)$", re.I)
SOFT_404_MAX_WORDS = 60     # a page with more words than this is content, whatever its title
STUB_MAX_WORDS = 20


def soft_404_reason(title: Optional[str], page_title: Optional[str], text: Optional[str]) -> Optional[str]:
    """Why a page served with HTTP 200 is not a real page (None when it is one).

    * "web server default page": the IIS / nginx / Apache welcome page
      (nyitetic.nyit.edu/ is titled "IIS Windows Server");
    * "application error page": Mahara's "Invalid Parameter" page, titled "Report issue
      to an administrator" (eportfolios.nyit.edu/artefact/file/download.php);
    * "placeholder page": the text a CMS ships until somebody writes the page (Mahara's
      "Add your terms and conditions for the site ..." legal page) or a "Your browser is
      unsupported" wall in front of an app (virtualtour.nyit.edu);
    * "test page": a very short page titled "Test ..." or "cache"
      (blogs.nyit.edu/uxui/test_class_of_2020: "Test Stuff Here!",
      www.nyit.edu/news/articles/cache/).

    Pages with :data:`SOFT_404_MAX_WORDS` words or more are never flagged.
    :func:`parse_site_page` gives such a page no sections (it keeps its text, so that the
    caller can still recognise it): pipeline.py can call this with a PageRecord's title,
    page_title and text to leave it out of pages.jsonl like the "Page not found" pages.
    """
    words = word_count(text or "")
    if words >= SOFT_404_MAX_WORDS:
        return None
    titles = [_one_line(t) for t in (title, page_title) if t]
    if any(_SERVER_DEFAULT_TITLE.match(t) for t in titles):
        return "web server default page"
    head = " ".join((text or "").split()[:40])
    if any(_APP_ERROR.search(t) for t in titles) or _APP_ERROR.search(head):
        return "application error page"
    if _PLACEHOLDER_TEXT.search(text or ""):
        return "placeholder page"
    if words < STUB_MAX_WORDS and any(_STUB_TITLE.match(t) for t in titles):
        return "test page"
    return None


# <meta> descriptions that describe nothing: a widget's text ("Loading...", a newsletter
# sign-up form: "Subscribe * indicates required Email Address"), a bare Zoom link
_JUNK_DESCRIPTION = re.compile(r"^(?:loading\W*|zoom link:.*)$|\bindicates required", re.I)


def useful_description(desc: Optional[str]) -> Optional[str]:
    """``desc`` unless it is a fragment ("Lionel", "'", "-----") or widget text."""
    if not desc or len(re.findall(r"[^\W\d_]{2,}", desc)) < 2 or _JUNK_DESCRIPTION.search(desc):
        return None
    return desc


# PageRecord fields for the time an event takes place (models.py); with an older
# PageRecord that does not have them, an event keeps its start time in ``published``
EVENT_DATE_FIELDS = ("event_start", "event_end")


def _page_fields() -> Set[str]:
    return {f.name for f in fields(PageRecord)}


def _page_dates(ex: Extracted, page_type: str) -> Tuple[Optional[str], Dict[str, Optional[str]]]:
    """(published, extra PageRecord fields) of a page.

    * bios: no publication date. WordPress dates almost every profile 2025-10-30 (400 of
      441), the day the directory was imported; ``modified`` stays.
    * events: when the event takes place is not when it was published (38 of 101 events
      are in the future): it goes to ``event_start`` / ``event_end`` and ``published``
      stays empty (with a PageRecord that has no event fields, the start time stays in
      ``published``, as documented for older datasets).
    * e-mails saved on files.nyit.edu: the date in their title or file name
      (:func:`communication_date`).
    * magazine articles: their issue's date was set by _magazine_facts.
    """
    published = ex.published
    extra: Dict[str, Optional[str]] = {}
    if page_type == "bio":
        published = None
    if ex.is_event or (page_type == "event" and ex.event_start):
        if set(EVENT_DATE_FIELDS) <= _page_fields():
            extra = {"event_start": ex.event_start, "event_end": ex.event_end}
            published = None
        else:
            published = ex.event_start
    if published is None and is_communication(ex.url):
        published = communication_date(ex.url, ex.title)
    return published, extra


# query parameters that number the pages of a listing: blogs.nyit.edu/<blog>?p=2,
# online.nyit.edu/blog?p=3, www.nyit.edu/faculty-and-staff/accomplishments/?fsa_page=4
_PAGE_NUMBER_PARAM = re.compile(r"^(?:p|paged|page|pg|\w+_page)$", re.I)


def page_number(url: str) -> int:
    """The page number a listing URL asks for (1 when it names none): "?p=2" -> 2. Only a
    URL whose one query parameter is the page number counts: in LibGuides' "c.php?g=61858&
    p=398465", p is the id of a guide's tab."""
    params = parse_qsl(urlsplit(url).query, keep_blank_values=False)
    if len(params) == 1:
        name, value = params[0]
        if _PAGE_NUMBER_PARAM.match(name) and value.isdigit():
            return max(int(value), 1)
    return 1


def _special_title(ex: Extracted, page_type: str, title: str, fetched_at: str) -> str:
    """Titles that need more than the page heading:

    * LibCal's opening hours are a snapshot: "Library Opening Hours (as of November 23,
      2026)", from the time the page was fetched, so every passage says how old it is;
    * LibCal's home page is called "LibCal" (the product's name);
    * page 2, 3, ... of a paginated listing: "Faculty & Staff Accomplishments (page 4)", so
      the 128 pages of accomplishments do not share one title."""
    if ex.site == "libcal.nyit.edu" and page_type == "library_hours":
        day = _parse_iso(fetched_at) if fetched_at else None
        when = f" (as of {day.strftime('%B')} {day.day}, {day.year})" if day is not None else ""
        return f"Library Opening Hours{when}"
    if ex.site == "libcal.nyit.edu" and page_type == "home" and title.casefold() in ("libcal", ""):
        return "LibCal: Library Room Reservations, Librarian Appointments and Events"
    number = page_number(ex.url)
    if number > 1 and title and not re.search(r"\(page \d+\)$", title):
        return f"{title} (page {number})"
    return title


def _noindex(entry: Optional[dict], meta: Dict[str, str]) -> bool:
    if entry is not None and entry.get("noindex") is not None:
        return bool(entry.get("noindex"))
    robots = meta.get("robots", "").lower()
    return "noindex" in robots or "none" in [x.strip() for x in robots.split(",")]


def parse_site_page(html: str, url: str, profile: Optional[sites.SiteProfile] = None,
                    fetched_at: str = "", entry: Optional[dict] = None,
                    boilerplate: Optional[Dict[str, Set[str]]] = None
                    ) -> Tuple[PageRecord, List[SectionRecord]]:
    """Parse one page of a non-catalog NYIT website (see the module docstring)."""
    ex = extract(html, url, profile)
    keys = set((boilerplate or {}).get(ex.site) or ())    # a list (e.g. from JSON) works too
    # Foleon (magazine) renders every block twice (desktop + mobile): drop all repeats there
    if ex.template.name == "magazine":
        blocks = _drop_repeats(_drop_boilerplate(ex, keys), min_words=1, kinds=("text", "list_item"),
                               per_section=False, near_copy_words=50)
    else:
        blocks = _drop_repeats(_drop_boilerplate(ex, keys))
    blocks = _tidy_headings(_drop_dangling_intros(blocks))
    not_found = bool(_NOT_FOUND.match(ex.title or "")) and sum(b.words for b in blocks) < 40
    # an event that is over: LiveWhale serves the calendar homepage (og:url = the site root)
    not_found = not_found or (ex.is_event and not ex.meta_is_page)
    if not_found:
        # blogs.nyit.edu answers bad URLs with HTTP 200 + "404 | <Blog> | Blogs NYIT"
        log.warning("'page not found' page served with HTTP 200: %s", url)
        blocks = []
    text = html_parser.blocks_to_text(blocks)
    if not text:
        log.debug("no main-content text on %s", url)
    # a web-server welcome page, an app's error page, a CMS placeholder: the page keeps its
    # text (so that pipeline.py can recognise and skip it too) but gets no sections
    soft_404 = None if not_found else soft_404_reason(ex.title, ex.page_title, text)
    if soft_404:
        log.info("not a real page (%s), no sections: %s", soft_404, url)

    page_type = "other" if (not_found or soft_404) else classify_site_page(ex)
    if page_type not in PAGE_TYPES:
        page_type = "page"
    program, degree = program_info(ex, page_type)
    title = ex.title or program or ""
    if program and title and not _mentions_program(title, program):
        title = f"{title}: {program}"       # "Admission Requirements" -> "...: Data Science, M.S."
    elif program and page_type in ("degree", "curriculum") and _fuller_title(title, [program]):
        title = program                     # "Management, M.B.A." -> "Management, M.B.A. – Finance"
    elif ex.full_title:
        title = ex.full_title               # "Message from the Chair: Department of Physics"
    title = _special_title(ex, page_type, title, fetched_at)
    published, extra = _page_dates(ex, page_type)
    school = ex.school or school_from_url(url)
    if school is None and page_type == "event":
        school = school_in_text(title)      # "... Info Session | Spotlight: School of Management"
    elif school is None and page_type not in ("news", "media_coverage", "expert_insight", "event",
                                              "magazine_article", "blog_post"):
        school = school_in_text(" ".join([title] + ex.breadcrumbs[1:]))
        if school is None and page_type in ("bio", "degree", "curriculum"):
            school = school_in_text(" ".join(b.text for b in blocks[:6]))
    description = useful_description(ex.description)
    if description and DESCRIPTION_KEY + block_key(description) in keys:
        description = None              # the site's generic description, not this page's
    doc_id = "page-" + short_hash(url)
    page = PageRecord(
        doc_id=doc_id,
        url=url,
        title=title,
        page_title=ex.page_title,
        breadcrumbs=list(ex.breadcrumbs),
        catalog_year="",
        page_type=page_type,
        area=page_area(ex.breadcrumbs, ex.title, ex.page_title),
        school=school,
        program=program,
        degree=degree,
        level=_level(ex, page_type, degree),
        campuses=_campuses(ex, text),
        headings=[b.text for b in blocks if b.kind == "heading" and 2 <= b.level <= 4],
        text=text,
        word_count=word_count(text),
        num_tables=ex.num_tables,
        links=html_parser.extract_links(ex.root, url) if (ex.root is not None and not ex.is_event) else [],
        content_sha1=sha1(text),
        fetched_at=fetched_at,
        site=ex.site,
        description=description,
        published=published,
        modified=ex.modified,
        noindex=_noindex(entry, ex.meta),
        **extra,
    )
    return page, ([] if soft_404 else html_parser.section_records(page, blocks))


def parse_any(html: str, url: str, fetched_at: str = "", entry: Optional[dict] = None,
              boilerplate: Optional[Dict[str, Set[str]]] = None) -> Tuple[PageRecord, List[SectionRecord]]:
    """Parse a page of any NYIT website. ``url`` is where the content lives (the final
    URL after redirects): its host decides the parser."""
    profile = _profile(url, None)
    if profile.parser == "catalog":
        page, sections = html_parser.parse_page(html, url, fetched_at)
        if entry is not None and entry.get("noindex"):
            page.noindex = True
        return page, sections
    return parse_site_page(html, url, profile, fetched_at, entry, boilerplate)
