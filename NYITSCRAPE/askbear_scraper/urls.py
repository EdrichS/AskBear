"""URL normalisation and crawl-scope rules for the NYIT websites.

Normalising URLs is what keeps the crawler from downloading the same page many
times. Each host has its own rules (see sites.py). On catalog.nyit.edu all of these
are the same page:

    /engineering/computer_science
    /engineering/computer_science/
    /Engineering/Computer_Science
    " /engineering/computer_science"        (the menus put a space before the path)
    http://catalog.nyit.edu/engineering/computer_science#admissions

and all of them normalise to ``https://catalog.nyit.edu/engineering/computer_science``.
On the WordPress sites (www.nyit.edu, vancouver.nyit.edu) the canonical form ends
with a slash instead (``https://www.nyit.edu/research/facilities/``), because
WordPress redirects every other spelling to it.
"""

from __future__ import annotations

import posixpath
import re
from typing import List, Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from . import config, sites
from .utils import short_hash

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "sms:", "ftp:", "file:")
_MULTI_SLASH = re.compile(r"/{2,}")
_UNSAFE_FILENAME = re.compile(r"[^a-z0-9_-]+")
_LAST_SEGMENT_HAS_EXTENSION = re.compile(r"/[^/]*\.[A-Za-z0-9]{1,5}$")


def normalize_url(href: Optional[str], base: str = config.BASE_URL) -> Optional[str]:
    """Turn an href found on a page into a canonical absolute URL.

    Returns ``None`` for things that are not web pages (javascript:, mailto:,
    pure #fragments, malformed URLs).
    """
    if href is None:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith(_SKIP_SCHEMES):
        return None
    try:
        parts = urlsplit(urljoin(base, href))
        port = parts.port
    except ValueError:  # e.g. "http://[bad" or a non-numeric port
        return None

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    profile = sites.profile_for(host)  # None for non-NYIT hosts
    if profile is not None:
        scheme = "https"  # every NYIT site redirects http -> https anyway
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    raw_path = unquote(parts.path or "/").strip()
    had_slash = raw_path.endswith("/")
    path = _MULTI_SLASH.sub("/", raw_path)
    if not path.startswith("/"):
        path = "/" + path
    # resolve "." and ".." segments (normpath also drops the trailing slash)
    path = posixpath.normpath(path) if path != "/" else "/"
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    if profile is not None and profile.case_insensitive and not _LAST_SEGMENT_HAS_EXTENSION.search(path):
        path = path.lower()  # only pages: file names (/files/...X.pdf) are case-sensitive
    if path != "/":
        mode = profile.trailing_slash if profile is not None else "strip"
        if mode == "add" and not _LAST_SEGMENT_HAS_EXTENSION.search(path):
            path += "/"
        elif mode == "keep" and had_slash:
            path += "/"
    path = quote(path, safe="/:@!$&'()*+,;=-._~")

    params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in config.TRACKING_PARAMS
    ]
    if profile is not None and "*" not in profile.keep_query_params:
        keep = {k.lower() for k in profile.keep_query_params}
        params = [(k, v) for k, v in params if k.lower() in keep]
    params.sort()
    query = urlencode(params, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def url_path(url: str) -> str:
    return urlsplit(url).path or "/"


def path_segments(url: str) -> List[str]:
    """``https://catalog.nyit.edu/engineering/computer_science`` -> ['engineering', 'computer_science']"""
    return [seg for seg in url_path(url).split("/") if seg]


def is_internal(url: str) -> bool:
    """True for any NYIT host (catalog.nyit.edu, www.nyit.edu, ...)."""
    return sites.profile_for(url_host(url)) is not None


def has_non_html_extension(url: str) -> bool:
    path = url_path(url).lower()
    return path.endswith(config.NON_HTML_EXTENSIONS)


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def is_excluded_path(url: str, include_print: bool = False) -> bool:
    """True when the host's profile keeps this path out of the crawl."""
    profile = sites.profile_for(url_host(url))
    if profile is None:
        return True
    path_query = _path_and_query(url)
    if include_print and profile.parser == "catalog" and re.match(r"^/print(/|$)", path_query):
        return False
    return not profile.path_allowed(path_query)


def is_crawlable(url: str, include_print: bool = False) -> bool:
    """True if ``url`` is an HTML page of an NYIT website that the crawler should visit."""
    return link_kind(url, include_print) == "page"


def link_kind(url: str, include_print: bool = False) -> str:
    """Classify a normalised link: page | pdf | asset | excluded | disabled | external.

    ``disabled`` = an NYIT host that is deliberately not crawled (login portals, ...).
    """
    if url_path(url).lower().endswith(".pdf"):
        return "pdf"
    profile = sites.profile_for(url_host(url))
    if profile is None:
        return "external"
    if not profile.enabled:
        return "disabled"
    if has_non_html_extension(url):
        return "asset"
    if is_excluded_path(url, include_print=include_print):
        return "excluded"
    return "page"


# Longest readable part of a cache file name. The 10-character hash keeps names unique;
# a short name keeps the full path below Windows' 260-character limit even when the
# project folder is deep (data/raw/html/<host>/ + ~75 characters).
MAX_SLUG_LENGTH = 60


def cache_filename(url: str) -> str:
    """Readable, unique path (relative to data/raw/html) for a cached page.

    ``https://catalog.nyit.edu/engineering/computer_science/curriculum``
        -> ``catalog.nyit.edu/engineering__computer_science__curriculum__<hash>.html``
    ``https://catalog.nyit.edu/courses?cal=UG&cs=CSCI``
        -> ``catalog.nyit.edu/courses__cal-ug_cs-csci__<hash>.html``

    The readable part is cut to :data:`MAX_SLUG_LENGTH` characters. Pages cached by older
    versions keep their (longer) names: the manifest records each page's file.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "unknown").lower()
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        host = _UNSAFE_FILENAME.sub("_", host) or "unknown"
    slug = "__".join(seg for seg in parts.path.lower().split("/") if seg) or "index"
    if parts.query:
        q = "_".join(f"{k}-{v}" for k, v in parse_qsl(parts.query, keep_blank_values=True))
        slug += "__" + q.lower()
    slug = _UNSAFE_FILENAME.sub("_", slug).strip("_")[:MAX_SLUG_LENGTH].rstrip("_") or "page"
    return f"{host}/{slug}__{short_hash(url)}.html"
