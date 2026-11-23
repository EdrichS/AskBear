"""Polite multi-site crawler for the public NYIT websites, with an on-disk cache.

* Crawls every enabled host in sites.py (catalog.nyit.edu, www.nyit.edu, ...). Each host
  starts from its seed pages and from the pages listed in its XML sitemaps, then follows
  every in-scope link (including links to other NYIT hosts).
* Each host has its own queue and its own politeness clock: a website never gets more
  than one request per ``delay`` seconds, while different websites are crawled in turn.
  Redirects are followed hop by hop (see fetcher.py): a redirect to a sign-in portal,
  another organisation or a page robots.txt forbids is recorded, never requested.
* Saves each page's HTML under ``data/raw/html/<host>/`` and records it in
  ``data/raw/crawl_manifest.jsonl`` (one JSON line per URL, the last line wins).
* Re-running is cheap: pages already in the cache (saved pages, redirects to pages we
  already have, 404s, redirect loops, ...) are read from disk, not downloaded again (use
  ``--refresh`` to force a fresh download). An interrupted crawl (Ctrl+C, lost Wi-Fi)
  therefore resumes where it stopped. Network errors are retried by the next run; a page
  that failed with HTTP 5xx gets a single attempt then.
* A run never forgets what it did not visit: a capped run (``--max-pages``), a
  site-restricted run (``--site``) or an interrupted run keeps every other cached page in
  the manifest, and ``link_graph.jsonl``, ``external_links.jsonl`` and
  ``crawl_summary.json`` are merged with the earlier results. Only a complete ``--refresh``
  crawl drops pages that are no longer linked from anywhere.
* Never stores "soft 404" pages (missing pages served with HTTP 200, default web-server
  pages, script-only shells), login pages (a password field, or a "Log in" title on a
  page with hardly any text) or redirects to other organisations' websites, and honours
  robots.txt plus <meta name="robots" content="nofollow">.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import Comment
from tqdm import tqdm

from . import config, sites
from .config import Settings
from .fetcher import (BLOCKED_LOOP, BLOCKED_ROBOTS, BLOCKED_SCOPE, BLOCKED_TOO_MANY,
                      BLOCKED_UNAVAILABLE, DISALLOWED, UNREACHABLE, WAIT, FetchResult,
                      PoliteFetcher, is_html_type)
from .sitemaps import collect_sitemap_urls
from .urls import cache_filename, link_kind, normalize_url, url_host
from .utils import (append_jsonl, dedupe_keep_order, make_soup, now_iso, read_json_file,
                    read_jsonl, sha1, write_json, write_jsonl)

log = logging.getLogger("askbear.crawl")

# Manifest statuses
OK = "ok"                    # real page, saved, will be parsed
SOFT_404 = "soft404"         # HTTP 200 but no content: "page not found" template, default
#                              web-server page, script-only shell, error template
LOGIN = "login_page"         # a sign-in form (password field) or sign-in screen
NOT_FOUND = "not_found"      # HTTP 404/410
HTTP_ERROR = "http_error"    # other 4xx/5xx after retries
NON_HTML = "non_html"        # a link that turned out not to be an HTML page
ERROR = "error"              # network error after retries (or the page could not be saved)
ROBOTS = "robots_disallowed" # robots.txt forbids the URL (or the redirect target)
DUPLICATE = "duplicate"      # redirected to a URL we already have
OFFSITE = "offsite_redirect" # redirected to a non-NYIT or deliberately skipped website
REDIRECT_LOOP = "redirect_loop"  # the URL redirects in a circle (a problem on NYIT's side)

# statuses that are final for a URL: the next run reads them from the cache
CACHEABLE = {OK, SOFT_404, LOGIN, NOT_FOUND, NON_HTML, OFFSITE, DUPLICATE, REDIRECT_LOOP}

# what crawl_summary.json says about one run ("last_run", and each item of "runs")
RUN_KEYS = ("finished_at", "seconds", "interrupted", "sites", "urls_seen", "urls_visited",
            "still_queued", "network_requests", "status_counts")

# A website that fails this many times in a row (network errors, HTTP 5xx) is given up
# for the rest of the crawl, so one broken server cannot stall the whole crawl.
MAX_CONSECUTIVE_FAILURES = 3

# Sitemap lists cached in data/raw/sitemaps/ are downloaded again when they are older than
# this (so new pages are found); --refresh (or settings.refresh_sitemaps) forces it.
SITEMAP_MAX_AGE_DAYS = 7

MAX_FOUND_ON = 5             # pages listed per link in external_links.jsonl

_NOT_FOUND_TITLE = re.compile(r"\b(page not found|404 not found|error 404)\b", re.I)
# default pages of a web server that has no site behind it
_SERVER_DEFAULT_TITLE = re.compile(
    r"^\s*(iis windows server|iis\s?\d*|internet information services|welcome to nginx!?|"
    r"apache2? (ubuntu|debian|centos)? ?default page.*|.*\bhttp server test page\b.*|"
    r"test page for the .*http server.*|it works!?)\s*$", re.I)
# script-only pages: the HTML holds nothing but a "your browser is not supported" notice
_SHELL_NOTICE = re.compile(
    r"(browser is (not supported|unsupported|out of date)|(upgrade|update) your browser|"
    r"(enable|turn on) javascript|javascript (is )?(required|disabled|must be enabled))", re.I)
# the main heading of an application's error template ("Mahara: Invalid Parameter")
_ERROR_HEADING = re.compile(
    r"(^|:\s*)(invalid parameter|page not found|404 not found|error 404|an error (has )?occurred"
    r"|access denied)\s*[.!]?\s*$", re.I)
# a sign-in screen's title: "Log in to ...", "Login | ...", "Sign in", "Sign-In - ..."
_LOGIN_TITLE = re.compile(r"^\s*(log\s?-?in|sign\s?-?in)(\s+(to|with)\b|\s*[|:–—-]|\s*$)",
                          re.I)
_INVISIBLE_TAGS = {"script", "style", "noscript", "template", "title"}
SHELL_MAX_WORDS = 40         # a script-only shell shows at most this many words
SPARSE_MAX_WORDS = 150       # sign-in screens and error templates are shorter than this


def _title(soup) -> str:
    return soup.title.get_text(" ", strip=True) if soup.title else ""


def visible_words(soup, limit: int) -> List[str]:
    """The first words of the page's visible text (scripts, styles... skipped), at most
    ``limit + 1`` of them, so callers can tell "short page" from "long page" cheaply."""
    words: List[str] = []
    for text in soup.find_all(string=True):
        if isinstance(text, Comment) or text.parent is None \
                or text.parent.name in _INVISIBLE_TAGS:
            continue
        words.extend(text.split())
        if len(words) > limit:
            break
    return words[:limit + 1]


def is_soft_404(soup) -> bool:
    """True for pages that hold no content although they were served with HTTP 200.

    * "page not found" pages: catalog.nyit.edu answers unknown URLs with an empty page
      template (no <h1> text, no main content, a <title> that starts with " | "); other
      sites use the usual "Page not found" title;
    * a web server's default page ("IIS Windows Server", "Welcome to nginx!", ...);
    * a script-only shell whose HTML only says "your browser is unsupported";
    * an application's error template whose heading says "Invalid Parameter", "Access
      denied", ... on a short page.
    """
    title = _title(soup)
    if _NOT_FOUND_TITLE.search(title) or _SERVER_DEFAULT_TITLE.match(title):
        return True
    h1 = soup.select_one("h1.Hero__heading")
    main = soup.select_one("div.Main__content")
    if h1 is not None or main is not None:  # the catalog template
        h1_text = h1.get_text(" ", strip=True) if h1 else ""
        main_text = main.get_text(" ", strip=True) if main else ""
        return not h1_text and not main_text and (not title or title.startswith("|"))
    words = visible_words(soup, SPARSE_MAX_WORDS)
    if len(words) <= SHELL_MAX_WORDS and _SHELL_NOTICE.search(" ".join(words)):
        return True
    if len(words) < SPARSE_MAX_WORDS:
        for heading in soup.find_all(["h1", "h2"], limit=3):
            if _ERROR_HEADING.search(heading.get_text(" ", strip=True)):
                return True
    return False


def is_login_page(soup) -> bool:
    """A sign-in wall, not public information: a page with a password field, or a page
    titled "Log in to ..." / "Sign in" with hardly any text (some sign-in screens build
    their form with JavaScript, e.g. Mahara's)."""
    if soup.find("input", attrs={"type": re.compile(r"^password$", re.I)}) is not None:
        return True
    return bool(_LOGIN_TITLE.match(_title(soup))) \
        and len(visible_words(soup, SPARSE_MAX_WORDS)) < SPARSE_MAX_WORDS


def meta_robots(soup) -> Tuple[bool, bool]:
    """(noindex, nofollow) from <meta name="robots" ...>; "none" means both."""
    tokens = set()
    for tag in soup.find_all("meta", attrs={"name": re.compile(r"^robots$", re.I)}):
        tokens.update(t for t in re.split(r"[\s,]+", (tag.get("content") or "").lower()) if t)
    none = "none" in tokens
    return ("noindex" in tokens or none), ("nofollow" in tokens or none)


def canonical_url(soup, base: str) -> Optional[str]:
    link = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    return normalize_url(link.get("href"), base=base) if link and link.get("href") else None


def load_manifest(path) -> Dict[str, dict]:
    """url -> latest manifest entry (later lines win)."""
    entries: Dict[str, dict] = {}
    for rec in read_jsonl(path):
        if rec.get("url"):
            entries[rec["url"]] = rec
    return entries


def course_dropdown_links(soup, page_url: str) -> List[str]:
    """Course listing pages have a <select class="courseDD"> whose JavaScript navigates to
    /courses/?cs=<SUBJECT>&cal=<LEVEL>. Turn every option into a crawlable URL so no
    subject is missed even if it is not linked from the course index."""
    select = soup.select_one("select.courseDD")
    if select is None:
        return []
    cal = dict(parse_qsl(urlsplit(page_url).query)).get("cal")
    if not cal:
        return []
    out = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        if value:
            out.append(normalize_url(f"/courses/?cs={value}&cal={cal}", page_url))
    return [u for u in out if u]


def is_pdf_type(content_type: str) -> bool:
    return "pdf" in (content_type or "").lower()


def dropped_query_params(href: str, link: str, base: str) -> List[str]:
    """Query parameters of ``href`` (as written on the page ``base``) that normalisation
    threw away in ``link``, tracking parameters excepted: ``?&p=2`` on a blog listing whose
    host keeps no parameters gives ``["p"]``."""
    raw = urlsplit(urljoin(base, href.strip())).query
    kept = {k.lower() for k, _ in parse_qsl(urlsplit(link).query, keep_blank_values=True)}
    names = {k.lower() for k, _ in parse_qsl(raw, keep_blank_values=True)}
    return sorted(n for n in names - kept
                  if n not in config.TRACKING_PARAMS and not n.startswith("utm_"))


def _as_list(found_on) -> List[str]:
    """``found_on`` is one URL in old crawl outputs and a list in newer ones."""
    if isinstance(found_on, str):
        return [found_on] if found_on else []
    if isinstance(found_on, (list, tuple)):
        return [str(u) for u in found_on if u]
    return []


def _age_days(stamp, fallback_mtime: Optional[float] = None) -> float:
    """Age in days of an ISO timestamp (or of a file modification time)."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds() / 86400
    except (TypeError, ValueError):
        if fallback_mtime is None:
            return float("inf")
        return (time.time() - fallback_mtime) / 86400


def _site_order(hosts: Iterable[str]) -> List[str]:
    """Hosts in sites.py order, then any other host alphabetically."""
    hosts = set(hosts)
    ordered = [h for h in sites.SITES if h in hosts]
    return ordered + sorted(hosts - set(ordered))


class Crawler:
    def __init__(self, settings: Settings, fetcher: Optional[PoliteFetcher] = None,
                 only_sites: Optional[Iterable[str]] = None):
        self.s = settings
        self.fetcher = fetcher or PoliteFetcher(settings)
        self.only_sites = sorted({h.lower() for h in only_sites}) if only_sites else None
        self.manifest = load_manifest(settings.manifest_path)
        self._migrate_cache()

    # ------------------------------------------------------------------ cache
    def _migrate_cache(self) -> None:
        """Bring a cache written by an older version up to date: move pages from
        data/raw/html/ into per-host folders and drop entries whose URL is not canonical
        any more (e.g. /courses?...&cc=PHAS-601, now the same page as /courses?...)."""
        changed = moved = dropped = 0
        updated: Dict[str, dict] = {}
        for url, entry in self.manifest.items():
            norm = normalize_url(url)
            if norm != url:
                dropped += 1
                continue
            entry = dict(entry)
            fname = entry.get("file")
            if fname and "/" not in fname:
                new_name = f"{url_host(url)}/{fname}"
                src, dst = self.s.html_dir / fname, self.s.html_dir / new_name
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.replace(dst)
                    moved += 1
                entry["file"] = new_name if dst.exists() else None
                if entry["file"] is None:
                    entry["status"] = ERROR  # cache file lost: download again
                changed += 1
            if "site" not in entry:
                entry["site"] = url_host(url)
                changed += 1
            updated[url] = entry
        if changed or dropped:
            self.s.ensure_dirs()
            write_jsonl(self.s.manifest_path, updated.values())
            self.manifest = updated
            log.info("updated the cache layout: %d files moved into per-site folders, "
                     "%d non-canonical URLs dropped", moved, dropped)

    def _cached(self, url: str) -> Optional[Tuple[dict, Optional[str]]]:
        """The cached outcome of ``url`` (entry, HTML of a saved page), or None when the
        URL must be downloaded (not cached, a retryable failure, --refresh, or a saved
        page whose HTML file is gone)."""
        entry = self.manifest.get(url)
        if self.s.refresh or not entry or entry.get("status") not in CACHEABLE:
            return None
        html = None
        if entry.get("status") == OK:
            fname = entry.get("file")
            if not fname:
                return None
            try:
                html = (self.s.html_dir / fname).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return dict(entry, from_cache=True), html

    def _is_cached(self, url: str) -> bool:
        entry = self.manifest.get(url)
        if self.s.refresh or not entry or entry.get("status") not in CACHEABLE:
            return False
        if entry.get("status") != OK:
            return True
        return bool(entry.get("file")) and (self.s.html_dir / entry["file"]).exists()

    # ------------------------------------------------------------------ downloads
    def _may_follow(self, target: str) -> bool:
        """Redirect policy: follow a hop only to an enabled NYIT website and a path in its
        crawl scope (pages, or files such as PDFs); never to a sign-in portal, a disabled
        host or another organisation's site."""
        norm = normalize_url(target)
        if not norm:
            return False
        profile = sites.profile_for(url_host(norm))
        if profile is None or not profile.enabled:
            return False
        return link_kind(norm, self.s.include_print_pages) in ("page", "pdf", "asset")

    @staticmethod
    def _without_added_slash(url: str) -> Optional[str]:
        """``https://www.nyit.edu/about/x/`` -> ``https://www.nyit.edu/about/x`` on hosts
        whose URLs get a trailing slash when normalised (WordPress); None elsewhere."""
        parts = urlsplit(url)
        profile = sites.profile_for(parts.hostname or "")
        if profile is None or profile.trailing_slash != "add" or parts.path in ("", "/") \
                or not parts.path.endswith("/"):
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))

    def _fetch(self, url: str, max_attempts: Optional[int]) -> FetchResult:
        res = self.fetcher.get(url, follow=self._may_follow, read_body=is_html_type,
                               max_attempts=max_attempts)
        if res.blocked in (BLOCKED_LOOP, BLOCKED_TOO_MANY):
            # Legacy www/vancouver links: the slash added by normalize_url sends some old
            # URLs into a site.nyit.edu <-> www.nyit.edu loop, while the URL as NYIT wrote
            # it still redirects to the right page.
            alt = self._without_added_slash(url)
            if alt:
                retry = self.fetcher.get(alt, follow=self._may_follow, read_body=is_html_type,
                                         max_attempts=max_attempts)
                if retry.blocked not in (BLOCKED_LOOP, BLOCKED_TOO_MANY):
                    return retry
        return res

    def _download(self, url: str, depth: int, referrer: Optional[str],
                  previous: Optional[dict] = None):
        """Fetch ``url`` and classify it. Returns ``(entry, html, soup)`` (html and soup
        only for a saved page), or None when the website's robots.txt cannot be read yet
        (the URL is tried again later in the run)."""
        entry = {
            "url": url,
            "site": url_host(url),
            "final_url": url,
            "status": ERROR,
            "http_status": 0,
            "content_type": "",
            "file": None,
            "bytes": 0,
            "sha1": None,
            "title": None,
            "noindex": False,
            "nofollow": False,
            "canonical": None,
            "depth": depth,
            "referrer": referrer,
            "fetched_at": now_iso(),
            "elapsed": 0.0,
            "error": "",
            "from_cache": False,
        }
        verdict = self.fetcher.check(url, wait=True)
        if verdict == WAIT:
            return None
        if verdict == UNREACHABLE:
            host = url_host(url)
            reason = getattr(self.fetcher, "robots_errors", {}).get(host) or "no connection"
            entry["error"] = f"website unreachable (could not read its robots.txt: {reason})"
            return entry, None, None
        if verdict == DISALLOWED:
            entry["status"] = ROBOTS
            return entry, None, None

        # a page that already failed with HTTP 5xx in an earlier run gets one attempt
        attempts = None
        if previous and previous.get("status") == HTTP_ERROR \
                and int(previous.get("http_status") or 0) >= 500:
            attempts = 1
        res = self._fetch(url, attempts)
        final = normalize_url(res.final_url) or res.final_url
        entry.update(
            final_url=final,
            http_status=res.status,
            content_type=res.content_type,
            bytes=res.size,
            fetched_at=now_iso(),        # after the politeness wait, when the answer came
            elapsed=round(res.elapsed, 3),
            error=res.error,
        )
        if res.redirects:
            entry["redirects"] = len(res.redirects)
        html = soup = None
        if res.blocked == BLOCKED_SCOPE:
            entry["status"] = OFFSITE    # a sign-in portal, another organisation, ...
        elif res.blocked == BLOCKED_ROBOTS:
            entry["status"] = ROBOTS     # the redirect target's robots.txt forbids it
        elif res.blocked in (BLOCKED_LOOP, BLOCKED_TOO_MANY):
            entry["status"] = REDIRECT_LOOP
        elif res.blocked == BLOCKED_UNAVAILABLE or res.status == 0:
            entry["status"] = ERROR
        elif res.status in (404, 410):
            entry["status"] = NOT_FOUND
        elif res.status >= 400:
            entry["status"] = HTTP_ERROR
        elif not res.is_html:
            entry["status"] = NON_HTML
        elif link_kind(final, self.s.include_print_pages) != "page":
            entry["status"] = OFFSITE
        else:
            html = res.text
            soup = make_soup(html)
            entry["title"] = _title(soup)
            entry["noindex"], entry["nofollow"] = meta_robots(soup)
            entry["canonical"] = canonical_url(soup, final)
            if is_soft_404(soup):
                entry["status"] = SOFT_404
            elif is_login_page(soup):
                entry["status"] = LOGIN
            else:
                fname = cache_filename(url)
                path = self.s.html_dir / fname
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(html, encoding="utf-8")
                except OSError as exc:  # path too long (Windows), disk full, file locked...
                    entry["status"] = ERROR
                    entry["error"] = f"could not save the page to {path}: {type(exc).__name__}: {exc}"
                    log.error("%s", entry["error"])
                else:
                    entry["status"] = OK
                    entry["file"] = fname
                    entry["sha1"] = sha1(html)
        if entry["status"] != OK:
            html = soup = None
        return entry, html, soup

    # ------------------------------------------------------------------ sitemaps
    def _sitemap_pages(self, profile: sites.SiteProfile) -> List[Tuple[str, Optional[str]]]:
        """Pages listed in the host's sitemaps; cached in data/raw/sitemaps/<host>.json
        and downloaded again after SITEMAP_MAX_AGE_DAYS days (or with --refresh)."""
        cache = self.s.raw_dir / "sitemaps" / f"{profile.host}.json"
        refresh = self.s.refresh or bool(getattr(self.s, "refresh_sitemaps", False))
        data = read_json_file(cache) if cache.exists() else None
        if isinstance(data, dict) and not refresh:
            try:
                mtime = cache.stat().st_mtime
            except OSError:
                mtime = None
            if _age_days(data.get("fetched_at"), mtime) < SITEMAP_MAX_AGE_DAYS:
                return [tuple(p) for p in data.get("pages", [])]
            log.info("%s: sitemap list is older than %d days, downloading it again",
                     profile.host, SITEMAP_MAX_AGE_DAYS)
        root = f"https://{profile.host}/"
        # robots.txt of a host that redirects elsewhere (nyit.edu -> www) announces the other
        # host's sitemaps: only keep sitemaps that belong to this host
        announced = [u for u in self.fetcher.robots_sitemaps(root) if url_host(u) == profile.host]
        sitemap_urls = dedupe_keep_order(list(profile.sitemaps) + announced)
        if not sitemap_urls:
            return []
        failures: List[str] = []
        pages = collect_sitemap_urls(self.fetcher, sitemap_urls, failures=failures)
        if failures:
            # do not cache an incomplete list: the next run tries the missing sitemaps again
            log.warning("%s: %d sitemaps could not be read now (%s); using the %d pages found",
                        profile.host, len(failures), failures[0], len(pages))
            if isinstance(data, dict) and not pages:
                return [tuple(p) for p in data.get("pages", [])]  # keep the older list
            return pages
        write_json(cache, {"host": profile.host, "fetched_at": now_iso(),
                           "sitemaps": sitemap_urls, "pages": pages})
        return pages

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        s = self.s
        s.ensure_dirs()
        started = time.time()
        include_print = s.include_print_pages
        profiles = sites.crawl_hosts(self.only_sites)
        if not profiles:
            raise ValueError(f"no crawlable site among {self.only_sites}; see `python -m "
                             f"askbear_scraper sites`")
        allowed_hosts = {p.host for p in profiles} if self.only_sites else None
        requests_before = self.fetcher.requests_made

        queues: Dict[str, Deque[Tuple[str, int, Optional[str]]]] = defaultdict(deque)
        seen = set()
        ok_per_host: Counter = Counter()          # distinct saved pages (for max_pages)
        page_hashes: Dict[str, Set[str]] = defaultdict(set)
        capped: Dict[str, int] = {}
        limits_used: Dict[str, int] = {}          # page limit of each capped website
        linked_skipped_hosts: Counter = Counter()

        def enqueue(url: Optional[str], depth: int, referrer: Optional[str]) -> None:
            if not url or url in seen:
                return
            kind = link_kind(url, include_print)
            host = url_host(url)
            if kind == "disabled":
                linked_skipped_hosts[host] += 1
            if kind != "page" or (allowed_hosts is not None and host not in allowed_hosts):
                return
            seen.add(url)
            queues[host].append((url, depth, referrer))

        # seeds, then everything the sitemaps list
        for p in profiles:
            for path in p.seeds:
                enqueue(normalize_url(path, f"https://{p.host}/"), 0, None)
        sitemap_counts: Dict[str, int] = {}
        for p in profiles:
            try:
                listed = self._sitemap_pages(p)
            except Exception as exc:  # a broken sitemap must not stop the crawl
                log.warning("could not read the sitemaps of %s: %s", p.host, exc)
                listed = []
            sitemap_counts[p.host] = len(listed)
            for loc, _lastmod in listed:
                enqueue(normalize_url(loc), 0, "sitemap")
            if listed:
                log.info("%s: %d pages listed in sitemaps", p.host, len(listed))

        visited: Dict[str, dict] = {}
        link_graph: Dict[str, List[str]] = {}
        external: Dict[str, dict] = {}
        counts: Counter = Counter()
        host_counts: Dict[str, Counter] = defaultdict(Counter)
        failures_in_a_row: Counter = Counter()
        given_up: Dict[str, int] = {}
        last_served: Dict[str, int] = {}
        dropped_self_links: Dict[str, Counter] = defaultdict(Counter)
        turn = 0
        interrupted = False

        def pick_host() -> Optional[str]:
            """A host whose next URL is cached (no waiting), else the host that may be
            contacted soonest; ties go to the host served least recently (round robin)."""
            ready = [h for h, q in queues.items() if q]
            if not ready:
                return None
            for h in ready:
                if self._is_cached(queues[h][0][0]):
                    return h
            return min(ready, key=lambda h: (round(self.fetcher.ready_in(h), 2),
                                             last_served.get(h, -1), h))

        def note_link(link: str, kind: str, anchor: str, page: str, **extra) -> None:
            if link not in external:
                external[link] = dict({"url": link, "kind": kind, "anchor_text": anchor[:200],
                                       "found_on": [page] if page else []}, **extra)
            elif page and len(external[link]["found_on"]) < MAX_FOUND_ON \
                    and page not in external[link]["found_on"]:
                external[link]["found_on"].append(page)

        bar = tqdm(total=len(seen), unit="page", desc="crawling", dynamic_ncols=True)
        try:
            while True:
                host = pick_host()
                if host is None:
                    break
                url, depth, referrer = queues[host].popleft()
                turn += 1
                last_served[host] = turn
                profile = sites.profile_for(host)
                limit = min(profile.max_pages, s.max_pages) if s.max_pages else profile.max_pages
                if ok_per_host[host] >= limit:
                    capped[host] = capped.get(host, 0) + 1 + len(queues[host])
                    limits_used[host] = limit
                    log.warning("%s: reached its limit of %d pages; %d queued URLs skipped",
                                host, limit, len(queues[host]) + 1)
                    queues[host].clear()
                    bar.update(1)
                    continue

                soup = None
                cached = self._cached(url)
                if cached is not None:
                    entry, html = cached
                    html = html if entry.get("status") == OK else None
                else:
                    result = self._download(url, depth, referrer, self.manifest.get(url))
                    if result is None:
                        # robots.txt of this website cannot be read yet: retry it later (the
                        # fetcher schedules the next try; other websites go on meanwhile)
                        queues[host].appendleft((url, depth, referrer))
                        continue
                    entry, html, soup = result
                    append_jsonl(s.manifest_path, entry)  # crash-safe progress log
                    self.manifest[url] = entry

                final = entry.get("final_url") or url
                if final != url:
                    if entry.get("status") in (OK, DUPLICATE):
                        if final in visited:
                            # a redirect to a page we already have
                            entry = dict(entry, status=DUPLICATE)
                            html = soup = None
                        elif entry.get("status") == DUPLICATE:
                            # cached duplicate whose target was not visited (yet) in this run:
                            # make sure the target page itself is crawled
                            enqueue(final, depth, url)
                    seen.add(final)
                if entry.get("status") == OK and entry.get("from_cache") \
                        and link_kind(final, include_print) != "page":
                    # cached by an older run, but sites.py no longer crawls where it lives
                    entry = dict(entry, status=OFFSITE)
                    html = soup = None

                entry["depth"] = depth
                if html is not None and entry.get("from_cache"):
                    soup = make_soup(html)
                    recheck = SOFT_404 if is_soft_404(soup) else (
                        LOGIN if is_login_page(soup) else None)
                    if recheck:
                        # detected by checks added after the page was cached
                        entry["status"] = recheck
                        html = soup = None
                    elif "nofollow" not in entry or "noindex" not in entry:
                        # entry written by an older crawler: add what it did not record
                        entry["noindex"], entry["nofollow"] = meta_robots(soup)
                        entry["canonical"] = canonical_url(soup, final)
                visited[url] = entry
                counts[entry["status"]] += 1
                host_counts[host][entry["status"]] += 1
                if entry["status"] == OK:
                    digest = entry.get("sha1")
                    if not digest or digest not in page_hashes[host]:
                        ok_per_host[host] += 1   # exact copies do not use up the page limit
                        if digest:
                            page_hashes[host].add(digest)

                # circuit breaker: give up on a website that keeps failing
                failed = entry["status"] == ERROR or (
                    entry["status"] == HTTP_ERROR and int(entry.get("http_status") or 0) >= 500)
                if failed:
                    failures_in_a_row[host] += 1
                    if failures_in_a_row[host] >= MAX_CONSECUTIVE_FAILURES and queues[host]:
                        given_up[host] = len(queues[host])
                        log.warning("%s failed %d times in a row (%s); giving up on its %d "
                                    "queued URLs", host, failures_in_a_row[host],
                                    entry.get("error") or f"HTTP {entry.get('http_status')}",
                                    len(queues[host]))
                        queues[host].clear()
                else:
                    failures_in_a_row[host] = 0
                bar.total = len(seen)
                bar.update(1)
                bar.set_postfix(ok=counts[OK], queued=sum(len(q) for q in queues.values()),
                                net=self.fetcher.requests_made, refresh=False)

                if entry["status"] == NON_HTML and is_pdf_type(entry.get("content_type")):
                    # a PDF served at a URL without ".pdf": hand it to the `docs` step
                    page = referrer if referrer and referrer != "sitemap" else ""
                    note_link(url, "pdf", "", page, content_type=entry.get("content_type"),
                              served_as_pdf=True)
                if entry["status"] != OK or html is None:
                    continue

                soup = soup if soup is not None else make_soup(html)
                follow = profile.follow_links and not entry.get("nofollow")
                outlinks: List[str] = []
                for a in soup.find_all("a", href=True):
                    link = normalize_url(a["href"], base=final)
                    if not link:
                        continue
                    if link == final and "?" in a["href"]:
                        # "?&p=2" on a listing whose host drops "p": its pages are not crawled
                        for param in dropped_query_params(a["href"], link, final):
                            dropped_self_links[host][param] += 1
                    kind = link_kind(link, include_print)
                    if kind == "page":
                        outlinks.append(link)
                        if follow:
                            enqueue(link, depth + 1, url)
                    else:
                        if kind == "disabled":
                            linked_skipped_hosts[url_host(link)] += 1
                        note_link(link, kind, a.get_text(" ", strip=True), url)
                if profile.parser == "catalog" and follow:
                    for link in course_dropdown_links(soup, url):
                        outlinks.append(link)
                        enqueue(link, depth + 1, url)
                link_graph[url] = dedupe_keep_order(outlinks)
        except KeyboardInterrupt:
            interrupted = True
            log.warning("interrupted: progress is saved, run the same command again to resume")
        finally:
            bar.close()

        # ---- write outputs -------------------------------------------------
        still_queued_by_site = {h: len(q) for h, q in queues.items() if q}
        incomplete = set(capped) | set(given_up) | set(self.fetcher.unreachable_hosts)
        final_manifest = self._final_manifest(visited, incomplete, interrupted)
        # compact the manifest (one line per URL); entries this run did not visit are kept
        write_jsonl(s.manifest_path, final_manifest.values())
        ok_urls = {u for u, e in final_manifest.items() if e.get("status") == OK}
        write_jsonl(s.link_graph_path, self._merged_link_graph(link_graph, visited, ok_urls))
        merged_external = self._merged_external(external, visited, ok_urls, final_manifest)
        write_jsonl(s.external_links_path, merged_external)

        run_hosts = [p.host for p in profiles]
        failed_sites = [] if interrupted else [
            p.host for p in profiles
            if p.host in sites.SITES and (p.seeds or sitemap_counts.get(p.host))
            and not host_counts.get(p.host, Counter())[OK]]
        unreachable = sorted(self.fetcher.unreachable_hosts)
        robots_errors = getattr(self.fetcher, "robots_errors", {})
        summary = {
            "finished_at": now_iso(),
            "seconds": round(time.time() - started, 1),
            "interrupted": interrupted,
            "sites": run_hosts,
            "urls_seen": len(seen),
            "urls_visited": len(visited),
            "still_queued": sum(still_queued_by_site.values()),
            "still_queued_by_site": still_queued_by_site,
            "network_requests": self.fetcher.requests_made - requests_before,
            "status_counts": dict(counts),
            "status_by_site": {h: dict(c) for h, c in sorted(host_counts.items())},
            "sitemap_pages_by_site": sitemap_counts,
            "capped_sites": capped,
            # the limit each capped website reached (sites.py max_pages, or --max-pages)
            "page_limits": limits_used,
            "given_up_sites": given_up,
            "unreachable_sites": unreachable,
            "unreachable_reasons": {h: robots_errors[h] for h in unreachable if h in robots_errors},
            "failed_sites": failed_sites,
            "discovered_sites": sorted(h for h in host_counts if h not in sites.SITES),
            "linked_but_not_crawled_sites": dict(linked_skipped_hosts.most_common()),
            # links like "?&p=2" that normalise to the page they are on, by query parameter:
            # pagination or filters of that site that are thrown away (sites.py
            # keep_query_params decides what is kept)
            "dropped_query_self_links": {h: dict(c.most_common())
                                         for h, c in sorted(dropped_self_links.items())},
            "pdf_links": sum(1 for e in merged_external if e.get("kind") == "pdf"),
            "external_links": sum(1 for e in merged_external if e.get("kind") == "external"),
            "settings": {
                "delay": s.delay,
                "max_pages_per_site": s.max_pages,
                "include_print_pages": include_print,
                "refresh": s.refresh,
                "user_agent": s.user_agent,
            },
        }
        summary["last_run"] = {k: summary[k] for k in RUN_KEYS}
        # every run whose pages are in the cache: only a full --refresh crawl starts a new
        # history; a resumed or repeated crawl and each `crawl --site` run are appended to it
        summary["runs"] = [summary["last_run"]]
        previous = read_json_file(s.raw_dir / "crawl_summary.json")
        if self.only_sites:
            summary = merge_summaries(previous, summary, final_manifest)
        elif not s.refresh:
            summary = with_run_history(previous, summary)
        write_json(s.raw_dir / "crawl_summary.json", summary)

        log.info("crawl finished: %d pages OK on %d sites, %d soft-404, %d login pages, "
                 "%d errors, %d network requests in %.0fs",
                 counts[OK], sum(1 for c in host_counts.values() if c[OK]), counts[SOFT_404],
                 counts[LOGIN], counts[ERROR] + counts[HTTP_ERROR] + counts[NOT_FOUND],
                 self.fetcher.requests_made - requests_before, time.time() - started)
        for host, c in sorted(host_counts.items()):
            log.info("  %-24s %5d ok  %s", host, c[OK],
                     ", ".join(f"{k} {v}" for k, v in c.most_common() if k != OK))
        if summary["discovered_sites"]:
            log.info("NYIT sites found through links (no profile in sites.py yet): %s",
                     ", ".join(summary["discovered_sites"]))
        if failed_sites:
            log.error("no page could be saved from %s: run `crawl` again later (cached pages "
                      "are not downloaded again)", ", ".join(failed_sites))
        return summary

    # ------------------------------------------------------------------ outputs
    def _final_manifest(self, visited: Dict[str, dict], incomplete: Set[str],
                        interrupted: bool) -> Dict[str, dict]:
        """Every URL's latest entry: this run's entries first, then every earlier entry
        this run did not visit. Only a complete --refresh crawl of every website drops the
        entries it did not reach (pages NYIT no longer links to), except on websites that
        were capped, given up or unreachable."""
        out: Dict[str, dict] = dict(visited)
        drop_stale = (self.s.refresh and self.only_sites is None and not self.s.max_pages
                      and not interrupted)
        dropped = 0
        for url, entry in self.manifest.items():
            if url in out:
                continue
            if drop_stale and url_host(url) not in incomplete:
                dropped += 1
                continue
            out[url] = entry
        if dropped:
            log.info("dropped %d manifest entries that the --refresh crawl no longer reached",
                     dropped)
        return out

    def _merged_link_graph(self, link_graph: Dict[str, List[str]], visited: Dict[str, dict],
                           ok_urls: Set[str]) -> List[dict]:
        """This run's link graph plus the earlier records of saved pages it did not visit."""
        kept = [rec for rec in read_jsonl(self.s.link_graph_path)
                if isinstance(rec, dict) and rec.get("url") not in visited
                and rec.get("url") not in link_graph and rec.get("url") in ok_urls]
        return kept + [{"url": u, "outlinks": links} for u, links in link_graph.items()]

    def _merged_external(self, external: Dict[str, dict], visited: Dict[str, dict],
                         ok_urls: Set[str], final_manifest: Dict[str, dict]) -> List[dict]:
        """This run's PDF/off-site links merged with the earlier ones: an earlier link is
        kept for the saved pages this run did not visit (pages it visited were read again)."""
        def unvisited(pages) -> List[str]:
            return [p for p in _as_list(pages) if p not in visited and p in ok_urls]

        old: Dict[str, dict] = {}
        for rec in read_jsonl(self.s.external_links_path):
            if isinstance(rec, dict) and rec.get("url"):
                old[rec["url"]] = rec
        out: Dict[str, dict] = {}
        for link, rec in external.items():
            prev = old.get(link)
            if prev is not None:
                rec = dict(rec, found_on=dedupe_keep_order(
                    rec["found_on"] + unvisited(prev.get("found_on")))[:MAX_FOUND_ON])
            out[link] = rec
        for link, prev in old.items():
            if link in out:
                continue
            if prev.get("served_as_pdf"):
                # found by fetching the URL itself: kept while that URL is not re-checked
                if link not in visited and link in final_manifest:
                    out[link] = prev
                continue
            pages = unvisited(prev.get("found_on"))
            if pages:
                out[link] = dict(prev, found_on=pages)
        return list(out.values())


def prune_cache(settings: Settings, dry_run: bool = True) -> dict:
    """Clean up the crawl cache (never run automatically).

    * manifest entries whose URL sites.py no longer crawls (a disabled host such as
      catalog-dev.nyit.edu, an excluded path) are removed, with their link-graph records;
    * HTML files under data/raw/html that no manifest entry refers to (left behind by older
      versions or by the entries removed above) are deleted.

    ``dry_run=True`` (the default) changes nothing and only reports what would go."""
    entries = load_manifest(settings.manifest_path)
    removed = {u: e for u, e in entries.items() if link_kind(u, include_print=True) != "page"}
    kept = {u: e for u, e in entries.items() if u not in removed}
    referenced = {e["file"] for e in kept.values() if e.get("file")}
    orphans = []
    if settings.html_dir.is_dir():
        for path in sorted(settings.html_dir.rglob("*.html")):
            rel = path.relative_to(settings.html_dir).as_posix()
            if rel not in referenced:
                orphans.append(path)
    report = {
        "dry_run": dry_run,
        "entries_removed": len(removed),
        "entries_removed_by_site": dict(Counter(url_host(u) for u in removed).most_common()),
        "files_deleted": len(orphans),
        "megabytes_freed": round(sum(p.stat().st_size for p in orphans) / 1e6, 1),
        "examples": sorted(removed)[:5] + [p.relative_to(settings.html_dir).as_posix()
                                           for p in orphans[:5]],
    }
    if dry_run:
        return report
    if removed:
        write_jsonl(settings.manifest_path, kept.values())
        write_jsonl(settings.link_graph_path,
                    [r for r in read_jsonl(settings.link_graph_path)
                     if isinstance(r, dict) and r.get("url") not in removed])
    for path in orphans:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
    log.info("pruned %d manifest entries and %d cache files (%.1f MB)", len(removed),
             len(orphans), report["megabytes_freed"])
    return report


def _run(summary: dict) -> dict:
    return summary["last_run"] if isinstance(summary.get("last_run"), dict) \
        else {k: summary.get(k) for k in RUN_KEYS if k in summary}


def with_run_history(previous: Optional[dict], current: dict) -> dict:
    """``current`` with the runs of ``previous`` before its own: ``runs`` lists every run
    whose pages are in the cache (the earlier summary's own run first), and ``seconds`` /
    ``network_requests`` add them up, so the summary still says what the whole crawl cost
    (a crawl interrupted and resumed, the main crawl and a later catch-up crawl)."""
    if not isinstance(previous, dict) or "sites" not in previous:
        return current
    history = [r for r in previous.get("runs") or [] if isinstance(r, dict)]
    if not history:   # a summary written before ``runs`` existed: its own totals are its run
        history = [_run(previous)]
    merged = dict(current)
    merged["last_run"] = _run(current)
    merged["runs"] = history + [merged["last_run"]]
    merged["seconds"] = round(sum(float(r.get("seconds") or 0) for r in merged["runs"]), 1)
    merged["network_requests"] = sum(int(r.get("network_requests") or 0)
                                     for r in merged["runs"])
    return merged


def merge_summaries(previous: Optional[dict], current: dict,
                    manifest: Optional[Dict[str, dict]] = None) -> dict:
    """crawl_summary.json after a run restricted to some websites (``crawl --site``):
    the per-site numbers of the other websites come from ``previous``; totals are
    recomputed, ``last_run`` describes the restricted run and ``runs`` keeps the history
    (with_run_history). With the merged ``manifest``, the status counts of the websites
    of this run are counted in it: the run only revisits what it reaches from inside those
    websites, the manifest also keeps their URLs that other websites link to."""
    if not isinstance(previous, dict) or "sites" not in previous:
        return current
    run = set(current.get("sites") or [])
    merged = with_run_history(previous, current)

    def per_site(key: str) -> dict:
        out = {h: v for h, v in dict(previous.get(key) or {}).items() if h not in run}
        out.update(current.get(key) or {})
        return out

    status_by_site = per_site("status_by_site")
    if manifest is not None:
        recount: Dict[str, Counter] = defaultdict(Counter)
        for url, entry in manifest.items():
            host = entry.get("site") or url_host(url)
            if host in run:
                recount[host][entry.get("status")] += 1
        status_by_site.update((h, dict(c)) for h, c in recount.items())
    merged["status_by_site"] = {h: status_by_site[h] for h in sorted(status_by_site)}
    totals: Counter = Counter()
    for c in status_by_site.values():
        totals.update(c)
    merged["status_counts"] = dict(totals)
    merged["urls_visited"] = sum(totals.values())
    merged["sites"] = _site_order(set(previous.get("sites") or []) | run)
    for key in ("sitemap_pages_by_site", "capped_sites", "page_limits", "given_up_sites",
                "still_queued_by_site", "unreachable_reasons", "dropped_query_self_links"):
        merged[key] = per_site(key)
    merged["unreachable_sites"] = sorted(
        {h for h in previous.get("unreachable_sites") or [] if h not in run}
        | set(current.get("unreachable_sites") or []))
    merged["failed_sites"] = _site_order(
        {h for h in previous.get("failed_sites") or [] if h not in run}
        | set(current.get("failed_sites") or []))
    merged["discovered_sites"] = sorted(h for h in status_by_site if h not in sites.SITES)
    linked = Counter(previous.get("linked_but_not_crawled_sites") or {})
    for host, n in (current.get("linked_but_not_crawled_sites") or {}).items():
        linked[host] = max(linked[host], n)
    merged["linked_but_not_crawled_sites"] = dict(linked.most_common())
    others_seen = sum(sum(c.values()) for h, c in status_by_site.items() if h not in run)
    merged["urls_seen"] = int(current.get("urls_seen") or 0) + others_seen
    still = sum(merged["still_queued_by_site"].values())
    previous_others = set(previous.get("sites") or []) - run
    if "still_queued_by_site" not in previous and previous_others:
        still += int(previous.get("still_queued") or 0)  # older summary: not split by site
    merged["still_queued"] = still
    merged["interrupted"] = bool(current.get("interrupted")) or (
        bool(previous.get("interrupted")) and bool(previous_others))
    return merged
