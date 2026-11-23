"""Read XML sitemaps to discover every page a site wants search engines to index.

Sitemaps are announced in robots.txt ("Sitemap: https://www.nyit.edu/sitemap_index.xml")
or listed in the site's profile (sites.py). A sitemap is either a list of pages
(<urlset>) or a list of further sitemaps (<sitemapindex>); both are handled, as are
gzip-compressed sitemaps.

Sitemaps are parsed with a small regular expression instead of an XML parser: they
have a trivial structure, and this avoids XML-parser attacks (entity expansion) on
content downloaded from the web.
"""

from __future__ import annotations

import gzip
import html
import logging
import re
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger("askbear.sitemaps")

_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_SITEMAP_BLOCK = re.compile(r"<sitemap>(.*?)</sitemap>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)
_CDATA = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)


def _clean_loc(raw: str) -> str:
    raw = raw.strip()
    m = _CDATA.match(raw)
    if m:
        raw = m.group(1).strip()
    return html.unescape(raw)


def parse_sitemap(text: str) -> Tuple[List[Tuple[str, Optional[str]]], List[str]]:
    """Return ``(pages, child_sitemaps)``; pages are ``(url, lastmod)`` pairs."""
    pages: List[Tuple[str, Optional[str]]] = []
    children: List[str] = []
    for block in _SITEMAP_BLOCK.findall(text):
        loc = _LOC.search(block)
        if loc:
            children.append(_clean_loc(loc.group(1)))
    for block in _URL_BLOCK.findall(text):
        loc = _LOC.search(block)
        if loc:
            mod = _LASTMOD.search(block)
            pages.append((_clean_loc(loc.group(1)), mod.group(1).strip() if mod else None))
    return pages, children


def _decode(body: bytes) -> str:
    if body[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            log.warning("could not decompress sitemap: %s", exc)
            return ""
    return body.decode("utf-8", errors="replace")


def _robots_verdict(fetcher, url: str) -> str:
    """"allowed", "disallowed", or another verdict meaning "cannot tell right now"."""
    check = getattr(fetcher, "check", None)
    if callable(check):
        return check(url)
    return "allowed" if fetcher.allowed(url) else "disallowed"


def collect_sitemap_urls(fetcher, sitemap_urls: Iterable[str], max_sitemaps: int = 200,
                         failures: Optional[List[str]] = None) -> List[Tuple[str, Optional[str]]]:
    """Download sitemaps (following sitemap indexes) and return every page URL listed.

    ``fetcher`` is a :class:`askbear_scraper.fetcher.PoliteFetcher`, so sitemap
    downloads obey the same delay, retry and robots.txt rules as page downloads.
    Sitemaps that could not be read because of a temporary problem (network error,
    HTTP 5xx or 429, a robots.txt that cannot be read) are appended to ``failures``, so
    the caller can avoid caching an incomplete list; a sitemap that is gone (HTTP 404/410)
    or that robots.txt forbids is not a failure.
    """
    queue = [u for u in sitemap_urls if u]
    seen = set()
    pages: List[Tuple[str, Optional[str]]] = []
    while queue and len(seen) < max_sitemaps:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        verdict = _robots_verdict(fetcher, url)
        if verdict == "disallowed":
            log.info("robots.txt disallows sitemap %s", url)
            continue
        if verdict != "allowed":
            log.warning("sitemap %s skipped for now: the website's robots.txt cannot be read", url)
            if failures is not None:
                failures.append(url)
            continue
        res = fetcher.get(url)
        if not res.ok:
            log.warning("sitemap %s -> HTTP %s %s", url, res.status, res.error)
            temporary = (res.status == 0 or res.status >= 500 or res.status in (408, 425, 429)
                         or getattr(res, "blocked", "") == "robots_unavailable")
            if failures is not None and temporary:
                failures.append(url)
            continue
        found, children = parse_sitemap(_decode(res.content))
        pages.extend(found)
        queue.extend(c for c in children if c not in seen)
        log.info("sitemap %s: %d pages, %d child sitemaps", url, len(found), len(children))
    if queue:
        log.warning("stopped after %d sitemaps (%d not read)", len(seen), len(queue))
    return pages
