"""Polite HTTP client: one request at a time, a delay between two requests to the same
website, retries with exponential backoff, robots.txt support and an honest User-Agent.

* Redirects are followed one hop at a time, so every hop waits for its own website's
  delay, is checked against that website's robots.txt and can be refused by the caller
  (the crawler never requests a sign-in portal or another organisation's site it was
  redirected to). Redirect loops are detected after the first repeated URL instead of
  sending 30 requests in a burst.
* robots.txt (RFC 9309): a missing file (HTTP 404 and other 4xx) allows everything;
  HTTP 401/403 disallows everything; a file that cannot be read (network error, HTTP 5xx
  or 429, timeout) blocks the website for now and is tried again later (see
  :data:`ROBOTS_REPROBE_DELAYS`) before the website is reported as unreachable.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from requests.utils import requote_uri

from .config import Settings

log = logging.getLogger("askbear.fetch")

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_CRAWL_DELAY = 60.0   # longest robots.txt Crawl-delay we honour (seconds)
MAX_RETRY_AFTER = 30.0   # longest "Retry-After" we wait for; the crawl is single-threaded
CONNECT_TIMEOUT = 10.0   # seconds to establish a connection (a dead host fails fast)
MAX_REDIRECTS = 10       # redirect hops followed for one page (requests' default is 30)
MAX_ROBOTS_REDIRECTS = 5  # RFC 9309: follow at least five redirects for robots.txt
# A robots.txt that cannot be read is tried again after these delays (seconds); after the
# last failed try the website counts as unreachable for the rest of the run.
ROBOTS_REPROBE_DELAYS = (60.0, 300.0)
MAX_ROBOTS_PROBES = len(ROBOTS_REPROBE_DELAYS) + 1

# robots.txt verdicts returned by PoliteFetcher.check()
ALLOWED = "allowed"            # robots.txt lets us fetch the URL
DISALLOWED = "disallowed"      # robots.txt forbids it
WAIT = "wait"                  # robots.txt could not be read yet; it is tried again later
UNREACHABLE = "unreachable"    # robots.txt could not be read after every try

# why a redirect was not followed (FetchResult.blocked)
BLOCKED_SCOPE = "scope"            # the caller refused the target (sign-in portal, other site...)
BLOCKED_ROBOTS = "robots"          # the target's robots.txt forbids it
BLOCKED_UNAVAILABLE = "robots_unavailable"  # the target website's robots.txt cannot be read now
BLOCKED_LOOP = "loop"              # the target was already requested in this chain
BLOCKED_TOO_MANY = "too_many"      # more than MAX_REDIRECTS hops

# Errors that can never succeed on a retry.
FATAL_ERRORS = (requests.exceptions.TooManyRedirects, requests.exceptions.InvalidURL,
                requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema,
                requests.exceptions.InvalidHeader)
# Errors meaning the server is unreachable: retry once at most.
CONNECT_ERRORS = (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError,
                  requests.exceptions.ConnectionError)


@dataclass
class FetchResult:
    url: str                       # URL that was requested
    final_url: str                 # URL after redirects (the refused target when blocked)
    status: int                    # HTTP status (0 = network error)
    content_type: str = ""
    text: str = ""                 # decoded body for HTML/text responses
    content: bytes = b""           # raw body (empty when the body was not read)
    error: str = ""                # error message when the request failed
    elapsed: float = 0.0           # seconds spent, including retries
    blocked: str = ""              # why a redirect was not followed (BLOCKED_*), "" otherwise
    redirects: List[str] = field(default_factory=list)  # every hop after ``url``, in order
    size: int = 0                  # body size: bytes read, or Content-Length when not read
    body_read: bool = True         # False when ``read_body`` declined the body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error and not self.blocked

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()


@dataclass
class _Opened:
    """The last response of a redirect chain (or the hop that was not followed)."""
    resp: requests.Response
    chain: List[str]
    blocked: str = ""
    target: str = ""


def is_html_type(content_type: str) -> bool:
    """True for an HTML (or XHTML) Content-Type, in any letter case."""
    return "html" in (content_type or "").lower()


def _content_length(resp: requests.Response) -> int:
    try:
        return max(0, int(str(resp.headers.get("Content-Length") or "0").strip()))
    except ValueError:
        return 0


class PoliteFetcher:
    """A thin wrapper around ``requests.Session`` that never hammers the server."""

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        self.session.max_redirects = MAX_REDIRECTS  # only used if someone calls the session directly
        self._sleep = sleep            # injectable so tests run instantly
        self._clock = clock
        self._last_request: Dict[str, float] = {}   # host -> time its last request ended
        self._next_gap: Dict[str, float] = {}       # host -> delay + jitter before its next request
        self._crawl_delay: Dict[str, float] = {}    # host -> "Crawl-delay" asked for in robots.txt
        self._robots: Dict[str, robotparser.RobotFileParser] = {}  # root -> verdict source
        self._robots_failures: Dict[str, int] = {}  # root -> failed robots.txt tries so far
        self.robots_errors: Dict[str, str] = {}     # host -> why its robots.txt could not be read
        self.requests_made = 0
        # hosts whose robots.txt cannot be read right now (network error, HTTP 5xx, ...);
        # a host leaves the set again when a later try succeeds
        self.unreachable_hosts = set()

    # ------------------------------------------------------------------ helpers
    def _timeout(self):
        return (min(CONNECT_TIMEOUT, self.settings.timeout), self.settings.timeout)

    def _attempts_for(self, exc: Exception) -> int:
        """How many attempts in total an error deserves."""
        if isinstance(exc, FATAL_ERRORS):
            return 1
        if isinstance(exc, CONNECT_ERRORS) and not isinstance(exc, requests.exceptions.ReadTimeout):
            return min(2, self.settings.max_retries + 1)
        return self.settings.max_retries + 1

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    @staticmethod
    def _root(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def ready_in(self, url_or_host: str) -> float:
        """Seconds until the next request to this website is allowed (0 = now)."""
        host = self._host(url_or_host) if "/" in url_or_host else url_or_host.lower()
        last = self._last_request.get(host)
        if last is None:
            return 0.0
        gap = self._next_gap.get(host, self.settings.delay)
        return max(0.0, gap - (self._clock() - last))

    def _wait_turn(self, url: str) -> None:
        """Sleep so two requests to the same website are at least ``delay`` (+ jitter)
        seconds apart. Different websites do not slow each other down."""
        remaining = self.ready_in(url)
        if remaining > 0:
            self._sleep(remaining)

    def _pause(self, seconds: float) -> None:
        """Back-off sleep; never negative or NaN (time.sleep raises on both)."""
        if seconds > 0 and math.isfinite(seconds):
            self._sleep(seconds)

    def delay_for(self, host: str) -> float:
        """Our delay, or the site's robots.txt Crawl-delay if that is longer."""
        return max(self.settings.delay, self._crawl_delay.get(host, 0.0))

    def _set_gap(self, host: str) -> None:
        jitter = random.uniform(0, max(self.settings.jitter, 0.0))
        self._next_gap[host] = self.delay_for(host) + jitter

    def _mark_request(self, url: str) -> None:
        host = self._host(url)
        self._last_request[host] = self._clock()
        self._set_gap(host)
        self.requests_made += 1

    @staticmethod
    def _retry_after(resp: requests.Response) -> Optional[float]:
        """Seconds asked for by a "Retry-After: <seconds>" header (capped); None when the
        header is missing, an HTTP date, negative or not a finite number."""
        value = resp.headers.get("Retry-After", "")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, MAX_RETRY_AFTER)

    # ------------------------------------------------------------------ redirects
    def _open(self, url: str, *, stream: bool, follow: Optional[Callable[[str], bool]] = None,
              check_robots: bool = True, max_hops: int = MAX_REDIRECTS) -> _Opened:
        """GET ``url`` and follow its redirects by hand, one hop at a time.

        Before each hop the target website's delay is waited for; the hop is not requested
        when ``follow(target)`` returns False, when the target's robots.txt forbids it (or
        cannot be read now), when the chain comes back to a URL it already requested (a
        loop) or after ``max_hops`` hops. Network errors propagate (the caller retries)."""
        current = url
        chain = [url]
        while True:
            self._wait_turn(current)
            try:
                resp = self.session.get(current, timeout=self._timeout(), allow_redirects=False,
                                        stream=stream)
            finally:
                self._mark_request(current)
            location = self.session.get_redirect_target(resp) if resp.is_redirect else None
            if not location:
                return _Opened(resp, chain)
            target = urldefrag(requote_uri(urljoin(resp.url or current, location)))[0]
            resp.close()
            if target in chain:
                return _Opened(resp, chain, BLOCKED_LOOP, target)
            if len(chain) > max_hops:
                return _Opened(resp, chain, BLOCKED_TOO_MANY, target)
            if follow is not None and not follow(target):
                return _Opened(resp, chain, BLOCKED_SCOPE, target)
            if check_robots and self.settings.respect_robots:
                verdict = self.check(target)
                if verdict == DISALLOWED:
                    return _Opened(resp, chain, BLOCKED_ROBOTS, target)
                if verdict != ALLOWED:
                    return _Opened(resp, chain, BLOCKED_UNAVAILABLE, target)
            chain.append(target)
            current = target

    @staticmethod
    def _blocked_message(opened: _Opened) -> str:
        why = {
            BLOCKED_SCOPE: "outside the crawl scope",
            BLOCKED_ROBOTS: "disallowed by its robots.txt",
            BLOCKED_UNAVAILABLE: "its website's robots.txt cannot be read right now",
            BLOCKED_LOOP: "redirect loop",
            BLOCKED_TOO_MANY: f"more than {MAX_REDIRECTS} redirects",
        }.get(opened.blocked, opened.blocked)
        hops = " -> ".join(opened.chain + [opened.target])
        return f"redirect not followed ({why}): {hops}"[:500]

    # ------------------------------------------------------------------ robots
    def _probe_robots(self, root: str) -> Optional[robotparser.RobotFileParser]:
        """Download and parse ``root``/robots.txt once (with a retry on the first try).

        Returns the parser, or None when the file could not be read (network error, HTTP 5xx
        or 429, read timeout); the host then joins ``unreachable_hosts`` and its next try is
        scheduled ``ROBOTS_REPROBE_DELAYS`` seconds later (``ready_in`` reports the wait)."""
        robots_url = root + "/robots.txt"
        host = self._host(robots_url)
        failures = self._robots_failures.get(root, 0)
        attempts = min(2, self.settings.max_retries + 1) if failures == 0 else 1
        error = ""
        for attempt in range(1, attempts + 1):
            self._wait_turn(robots_url)
            try:
                opened = self._open(robots_url, stream=False, check_robots=False,
                                    max_hops=MAX_ROBOTS_REDIRECTS)
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, FATAL_ERRORS):
                    break
                if attempt < attempts:
                    self._pause(self.settings.backoff ** attempt)
                continue
            resp = opened.resp
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = "utf-8"  # RFC 9309: robots.txt is UTF-8
            rp = robotparser.RobotFileParser(robots_url)
            if opened.blocked:
                # RFC 9309: more than five redirects (or a loop) = no robots.txt
                rp.parse([])
                log.info("robots.txt of %s redirects too often (%s): all paths allowed", host,
                         opened.blocked)
            elif resp.status_code in RETRY_STATUSES or resp.status_code >= 500:
                error = f"HTTP {resp.status_code}"
                if attempt < attempts:
                    self._pause(self._retry_after(resp) or self.settings.backoff ** attempt)
                continue
            elif resp.status_code in (401, 403):
                rp.disallow_all = True
                log.warning("robots.txt of %s answers HTTP %s: nothing on it is fetched", host,
                            resp.status_code)
            elif resp.status_code != 200 or not resp.text.strip() \
                    or resp.text.lstrip().startswith("<"):
                rp.parse([])  # missing (404...), empty, or an HTML page instead of a text file
                log.info("no robots.txt at %s (HTTP %s): all paths allowed", robots_url,
                         resp.status_code)
            else:
                rp.parse(resp.text.splitlines())
                log.info("loaded robots.txt from %s", robots_url)
                wanted = rp.crawl_delay(self.settings.user_agent)
                if wanted:
                    try:
                        self._crawl_delay[host] = min(max(float(wanted), 0.0), MAX_CRAWL_DELAY)
                    except (TypeError, ValueError):
                        pass
                    else:
                        log.info("%s asks for %ss between requests (robots.txt Crawl-delay); "
                                 "honouring it", host, self._crawl_delay[host])
            # the gap after this request must already include the Crawl-delay just learnt
            self._set_gap(host)
            self._robots[root] = rp
            self._robots_failures.pop(root, None)
            self.robots_errors.pop(host, None)
            if host in self.unreachable_hosts:
                self.unreachable_hosts.discard(host)
                log.info("%s is reachable again", host)
            return rp

        failures += 1
        self._robots_failures[root] = failures
        self.robots_errors[host] = error or "failed"
        self.unreachable_hosts.add(host)
        if failures < MAX_ROBOTS_PROBES:
            wait = ROBOTS_REPROBE_DELAYS[failures - 1]
            self._last_request[host] = self._clock()
            self._next_gap[host] = wait
            log.warning("cannot read robots.txt of %s (%s); nothing is fetched from it for now, "
                        "trying again in %.0fs", host, error or "failed", wait)
        else:
            log.warning("cannot read robots.txt of %s after %d tries (%s); skipping this website",
                        host, failures, error or "failed")
        return None

    def check(self, url: str, wait: bool = False) -> str:
        """robots.txt verdict for ``url``: ALLOWED, DISALLOWED, WAIT or UNREACHABLE.

        robots.txt is downloaded once per website. When it cannot be read the website is
        blocked (RFC 9309) and WAIT is returned until the next try is due; ``wait=True``
        sleeps until then and tries again. UNREACHABLE means every try failed."""
        if not self.settings.respect_robots:
            return ALLOWED
        root = self._root(url)
        rp = self._robots.get(root)
        if rp is None:
            failures = self._robots_failures.get(root, 0)
            if failures >= MAX_ROBOTS_PROBES:
                return UNREACHABLE
            if failures and not wait and self.ready_in(root + "/") > 0:
                return WAIT
            rp = self._probe_robots(root)
            if rp is None:
                return UNREACHABLE if self._robots_failures.get(root, 0) >= MAX_ROBOTS_PROBES \
                    else WAIT
        return ALLOWED if rp.can_fetch(self.settings.user_agent, url) else DISALLOWED

    def allowed(self, url: str) -> bool:
        """False only when robots.txt forbids our User-Agent to fetch ``url``.

        A website whose robots.txt cannot be read is not "disallowed" here (callers record a
        retryable error instead of a final robots.txt refusal), but nothing is requested from
        it: :meth:`get` and :meth:`download` then return a network-style error. Use
        :meth:`check` to tell the cases apart."""
        return self.check(url) != DISALLOWED

    def _unavailable(self, url: str) -> Optional[FetchResult]:
        """A ready-made error result when ``url``'s website has a robots.txt that could not
        be read (RFC 9309: assume complete disallow until it can be read)."""
        if not self.settings.respect_robots:
            return None
        root = self._root(url)
        if root in self._robots or not self._robots_failures.get(root):
            return None
        host = self._host(url)
        return FetchResult(url=url, final_url=url, status=0, blocked=BLOCKED_UNAVAILABLE,
                           error=f"website unreachable: robots.txt of {host} could not be read "
                                 f"({self.robots_errors.get(host) or 'failed'})")

    def robots_sitemaps(self, url: str) -> List[str]:
        """Sitemap URLs announced in the website's robots.txt ("Sitemap: ..." lines)."""
        root = self._root(url)
        if root not in self._robots:
            self.check(url)
        rp = self._robots.get(root)
        return list(rp.site_maps() or []) if rp is not None else []

    # ------------------------------------------------------------------ fetching
    def get(self, url: str, follow: Optional[Callable[[str], bool]] = None,
            read_body: Optional[Callable[[str], bool]] = None,
            max_attempts: Optional[int] = None) -> FetchResult:
        """GET ``url`` with retries. Never raises for HTTP/network errors.

        ``follow(target)`` may refuse a redirect target (it is then not requested and the
        result has ``blocked == BLOCKED_SCOPE`` and ``final_url == target``).
        ``read_body(content_type)`` may decline the body of the final response (e.g. a PDF
        when only HTML is wanted): the connection is closed after the headers.
        ``max_attempts`` lowers the number of attempts (e.g. 1 for a page that already
        failed with HTTP 5xx in an earlier run)."""
        refused = self._unavailable(url)
        if refused is not None:
            return refused
        start = self._clock()
        last_error = ""
        attempts = self.settings.max_retries + 1
        if max_attempts is not None:
            attempts = max(1, min(attempts, int(max_attempts)))
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                opened = self._open(url, stream=read_body is not None, follow=follow)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts = min(attempts, self._attempts_for(exc))
                if attempt < attempts:
                    wait = self.settings.backoff ** attempt
                    log.warning("%s on %s (attempt %d/%d), retrying in %.0fs",
                                type(exc).__name__, url, attempt, attempts, wait)
                    self._pause(wait)
                    continue
                break
            resp = opened.resp
            hops = opened.chain[1:]
            if opened.blocked:
                return FetchResult(url=url, final_url=opened.target, status=resp.status_code,
                                   content_type=resp.headers.get("Content-Type", ""),
                                   error=self._blocked_message(opened), blocked=opened.blocked,
                                   redirects=hops + [opened.target],
                                   elapsed=self._clock() - start)

            if resp.status_code in RETRY_STATUSES and attempt < attempts:
                wait = self._retry_after(resp) or self.settings.backoff ** attempt
                resp.close()
                log.warning("HTTP %d on %s (attempt %d/%d), retrying in %.0fs",
                            resp.status_code, url, attempt, attempts, wait)
                self._pause(wait)
                continue

            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.url or (hops[-1] if hops else url)
            if read_body is not None and not read_body(content_type):
                size = _content_length(resp)
                resp.close()
                return FetchResult(url=url, final_url=final_url, status=resp.status_code,
                                   content_type=content_type, redirects=hops, size=size,
                                   body_read=False, elapsed=self._clock() - start)
            ct = content_type.lower()
            is_text = "html" in ct or ct.startswith("text/")
            if is_text and (resp.encoding is None or resp.encoding.lower() == "iso-8859-1"):
                # the catalog is UTF-8; requests guesses ISO-8859-1 when no charset is sent
                resp.encoding = "utf-8"
            try:
                content = resp.content
                text = resp.text if is_text else ""
            except requests.RequestException as exc:  # connection lost while reading the body
                last_error = f"{type(exc).__name__}: {exc}"
                attempts = min(attempts, self._attempts_for(exc))
                if attempt < attempts:
                    self._pause(self.settings.backoff ** attempt)
                    continue
                break
            return FetchResult(
                url=url,
                final_url=final_url,
                status=resp.status_code,
                content_type=content_type,
                text=text,
                content=content,
                redirects=hops,
                size=len(content),
                elapsed=self._clock() - start,
            )
        return FetchResult(url=url, final_url=url, status=0, error=last_error or "failed",
                           elapsed=self._clock() - start)

    def download(self, url: str, dest: Path, chunk_size: int = 1 << 16,
                 follow: Optional[Callable[[str], bool]] = None) -> FetchResult:
        """Stream a (large) file such as a PDF to ``dest`` with the same politeness rules
        (redirects are followed hop by hop, like :meth:`get`)."""
        refused = self._unavailable(url)
        if refused is not None:
            return refused
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        start = self._clock()
        last_error = ""
        attempts = self.settings.max_retries + 1
        attempt = 0
        while attempt < attempts:
            attempt += 1
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                opened = self._open(url, stream=True, follow=follow)
                with opened.resp as resp:
                    hops = opened.chain[1:]
                    if opened.blocked:
                        return FetchResult(url=url, final_url=opened.target,
                                           status=resp.status_code,
                                           content_type=resp.headers.get("Content-Type", ""),
                                           error=self._blocked_message(opened),
                                           blocked=opened.blocked,
                                           redirects=hops + [opened.target],
                                           elapsed=self._clock() - start)
                    if resp.status_code in RETRY_STATUSES and attempt < attempts:
                        wait = self._retry_after(resp) or self.settings.backoff ** attempt
                        log.warning("HTTP %d downloading %s, retrying in %.0fs",
                                    resp.status_code, url, wait)
                        self._pause(wait)
                        continue
                    if resp.status_code != 200:
                        return FetchResult(url=url, final_url=resp.url, status=resp.status_code,
                                           content_type=resp.headers.get("Content-Type", ""),
                                           error=f"HTTP {resp.status_code}", redirects=hops,
                                           elapsed=self._clock() - start)
                    size = 0
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size):
                            fh.write(chunk)
                            size += len(chunk)
                    tmp.replace(dest)
                    return FetchResult(url=url, final_url=resp.url, status=resp.status_code,
                                       content_type=resp.headers.get("Content-Type", ""),
                                       redirects=hops, size=size,
                                       elapsed=self._clock() - start)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts = min(attempts, self._attempts_for(exc))
                if tmp.exists():
                    tmp.unlink()
                if attempt < attempts:
                    wait = self.settings.backoff ** attempt
                    log.warning("%s downloading %s, retrying in %.0fs", type(exc).__name__, url, wait)
                    self._pause(wait)
        return FetchResult(url=url, final_url=url, status=0, error=last_error or "failed",
                           elapsed=self._clock() - start)
