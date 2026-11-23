"""Test helpers shared by the test modules (imported as ``testkit``; see pytest.ini).

Nothing here talks to the network:

* :class:`FakeWeb` is a ``requests`` transport adapter that answers from a dict of
  canned responses, so the real ``requests.Session`` / ``PoliteFetcher`` code runs
  (redirects, streaming, response hooks) without opening a socket.
* :class:`FakeClock` replaces ``time.monotonic`` / ``time.sleep`` in PoliteFetcher, so
  politeness delays and retry back-off are recorded instead of waited for.
* :func:`html_page`, :func:`make_pdf`, :func:`manifest_entry`, :func:`cache_page` and
  :func:`build_fixture_crawl` build small inputs (a crawl cache made of the real pages in
  tests/fixtures, whose origin is listed in tests/fixtures/sources.json).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

HTML = "text/html; charset=UTF-8"
NOT_FOUND_HTML = ("<!DOCTYPE html><html><head><title>Page not found | New York Tech</title></head>"
                  "<body><main><h1>Page not found</h1></main></body></html>")


# ---------------------------------------------------------------------------
# fixtures on disk
# ---------------------------------------------------------------------------
def fixture_sources() -> Dict[str, dict]:
    """tests/fixtures/sources.json: fixture file -> where the page came from."""
    return json.loads((FIXTURES_DIR / "sources.json").read_text(encoding="utf-8"))


def fixture_html(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def fixture_url(name: str) -> str:
    """The URL the page's content lives at (its final URL after redirects)."""
    src = fixture_sources()[name]
    return src.get("final_url") or src["url"]


# ---------------------------------------------------------------------------
# fake network
# ---------------------------------------------------------------------------
@dataclass
class Reply:
    """One canned HTTP response."""

    body: Union[bytes, str] = b""
    status: int = 200
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: Optional[str] = HTML

    def content(self) -> bytes:
        return self.body.encode("utf-8") if isinstance(self.body, str) else self.body


class _Body(io.BytesIO):
    """``Response.raw`` stand-in that supports ``stream()`` like urllib3's response."""

    def stream(self, chunk_size=1024, decode_content=None):
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def release_conn(self):
        pass


Route = Union[Reply, Exception, Callable[[requests.PreparedRequest], Union[Reply, Exception]],
              List[Union[Reply, Exception]]]


class FakeWeb(BaseAdapter):
    """In-memory websites for ``requests``: ``url -> Reply`` (or an exception to raise, a
    callable, or a list of those served one after the other; the last one repeats).

    Unknown URLs get a 404 "Page not found" page. Every request is logged in ``requests``.
    """

    def __init__(self, routes: Optional[Dict[str, Route]] = None):
        super().__init__()
        self.routes: Dict[str, Route] = dict(routes or {})
        self.requests: List[str] = []
        self.request_headers: List[dict] = []

    # -- building the web ---------------------------------------------------
    def add(self, url: str, body: Union[bytes, str] = b"", status: int = 200,
            content_type: Optional[str] = HTML, **headers: str) -> "FakeWeb":
        self.routes[url] = Reply(body, status, dict(headers), content_type)
        return self

    def redirect(self, url: str, target: str, status: int = 301) -> "FakeWeb":
        self.routes[url] = Reply(b"", status, {"Location": target}, None)
        return self

    def robots(self, host: str, text: str) -> "FakeWeb":
        return self.add(f"https://{host}/robots.txt", text, content_type="text/plain")

    # -- queries ------------------------------------------------------------
    def count(self, predicate: Callable[[str], bool] = lambda u: True) -> int:
        return sum(1 for u in self.requests if predicate(u))

    def page_requests(self) -> List[str]:
        """Requests other than robots.txt."""
        return [u for u in self.requests if not u.endswith("/robots.txt")]

    # -- requests adapter interface ----------------------------------------
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        url = request.url
        self.requests.append(url)
        self.request_headers.append(dict(request.headers))
        route = self.routes.get(url)
        if isinstance(route, list):
            item = route.pop(0) if len(route) > 1 else route[0]
        else:
            item = route
        if callable(item) and not isinstance(item, Reply):
            item = item(request)
        if item is None:
            item = Reply(NOT_FOUND_HTML, 404)
        if isinstance(item, BaseException):
            raise item
        return self._response(request, item)

    def close(self):
        pass

    def _response(self, request, reply: Reply) -> requests.Response:
        resp = requests.Response()
        resp.status_code = reply.status
        headers = dict(reply.headers)
        if reply.content_type and "Content-Type" not in headers:
            headers["Content-Type"] = reply.content_type
        resp.headers = CaseInsensitiveDict(headers)
        resp.raw = _Body(reply.content())
        resp.url = request.url
        resp.request = request
        resp.reason = "OK" if reply.status < 400 else "Error"
        resp.encoding = get_encoding_from_headers(resp.headers)
        resp.connection = self
        return resp


def fake_session(web: FakeWeb) -> requests.Session:
    session = requests.Session()
    session.trust_env = False          # no proxy/netrc lookups from the environment
    session.mount("https://", web)
    session.mount("http://", web)
    return session


class FakeClock:
    """Monotonic clock + sleep for PoliteFetcher: sleeping advances the clock."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:   # like time.sleep
            raise ValueError("sleep length must be non-negative")
        if seconds != seconds:  # NaN, like time.sleep
            raise ValueError("Invalid value NaN (not a number)")
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_fetcher(settings, web: FakeWeb, clock: Optional[FakeClock] = None):
    from askbear_scraper.fetcher import PoliteFetcher

    clock = clock or FakeClock()
    return PoliteFetcher(settings, session=fake_session(web), sleep=clock.sleep, clock=clock)


# ---------------------------------------------------------------------------
# small inputs
# ---------------------------------------------------------------------------
def html_page(title: str, body: str = "", links: Sequence[str] = (), head: str = "",
              body_class: str = "") -> str:
    """A small, valid HTML page with an <h1>, some text and links inside <main>."""
    anchors = "".join(f'<li><a href="{href}">Link {i}</a></li>' for i, href in enumerate(links))
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{title}</title>"
            f"{head}</head><body class=\"{body_class}\"><main><h1>{title}</h1>"
            f"<p>{body or 'Some words about ' + title + ' for the test.'}</p><ul>{anchors}</ul>"
            f"</main></body></html>")


def make_pdf(path: Path, pages: Sequence[Union[str, Sequence[str]]],
             toc: Optional[List[list]] = None, metadata: Optional[dict] = None,
             header: Optional[str] = None, footer_numbers: bool = False,
             title_size: Optional[float] = None) -> Path:
    """Write a small PDF with PyMuPDF: one string (or list of lines) per page, optional
    bookmarks (``[[level, title, page], ...]``), metadata, a running header and page-number
    footers. ``title_size`` prints the first line of page 1 in that font size."""
    try:  # PyMuPDF >= 1.24.3
        import pymupdf
    except ImportError:  # older PyMuPDF only has the legacy module name
        import fitz as pymupdf  # type: ignore[no-redef]

    doc = pymupdf.open()
    for pno, content in enumerate(pages, 1):
        page = doc.new_page(width=612, height=792)
        lines = [content] if isinstance(content, str) else list(content)
        if header:
            page.insert_text((72, 30), header, fontsize=9)
        y = 100.0
        for i, line in enumerate(lines):
            size = title_size if (title_size and pno == 1 and i == 0) else 11
            if line:
                page.insert_text((72, y), line, fontsize=size)
            y += size * 1.6
        if footer_numbers:
            page.insert_text((300, 770), str(pno), fontsize=9)
    if toc:
        doc.set_toc(toc)
    if metadata:
        doc.set_metadata(metadata)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def pdf_bytes(tmp_dir: Path, name: str = "doc.pdf", **kwargs) -> bytes:
    pages = kwargs.pop("pages", ["Hello PDF world."])
    return make_pdf(Path(tmp_dir) / name, pages, **kwargs).read_bytes()


def manifest_entry(url: str, file: Optional[str], status: str = "ok", final_url: Optional[str] = None,
                   **extra) -> dict:
    """A crawl_manifest.jsonl line like the crawler writes it."""
    from askbear_scraper.urls import url_host

    entry = {
        "url": url, "site": url_host(url), "final_url": final_url or url, "status": status,
        "http_status": 200 if status == "ok" else 404, "content_type": HTML, "file": file,
        "bytes": 0, "sha1": None, "title": "", "noindex": False, "nofollow": False,
        "canonical": None, "depth": 0, "referrer": None,
        "fetched_at": "2026-11-23T07:00:00+00:00", "elapsed": 0.1, "error": "",
        "from_cache": False,
    }
    entry.update(extra)
    return entry


def write_jsonl(path: Path, records: Iterable[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def read_jsonl(path: Path) -> List[dict]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cache_page(settings, url: str, html: str, **extra) -> dict:
    """Save ``html`` in the crawl cache of ``settings`` the way the crawler does and return
    its manifest entry (not yet written to the manifest)."""
    from askbear_scraper.urls import cache_filename

    fname = cache_filename(url)
    path = settings.html_dir / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return manifest_entry(url, fname, **extra)


# special entries of the fixture crawl (see build_fixture_crawl)
DUPLICATE_URL = "https://www.nyit.edu/news/articles/ranking-copy/"         # same HTML as www_news
DEV_COPY_URL = "https://catalog-dev.nyit.edu/engineering/computer_science"  # disabled host
LEGACY_URL = "https://site.nyit.edu/bio/amilanif"                           # redirects to www_bio
NOT_FOUND_URL = "https://catalog.nyit.edu/engineering/gone"
MISSING_FILE_URL = "https://www.nyit.edu/lost/"
NOINDEX_FIXTURE = "magazine_article.html"


def build_fixture_crawl(settings, extras: bool = True) -> List[dict]:
    """A crawl cache + manifest made of every HTML fixture (as if the crawler had saved
    them), plus, with ``extras``: an exact copy of the news article under another www URL,
    a copy of the catalog program page on catalog-dev.nyit.edu (a disabled development
    host), a legacy-host URL that redirected to the bio page, a 404 entry, an entry whose
    cache file is missing, and the magazine article flagged noindex."""
    settings.ensure_dirs()
    entries = []
    for name, src in fixture_sources().items():
        if not src.get("cache_file"):
            continue  # the soft-404 template is never saved by the crawler
        entries.append(cache_page(settings, src["url"], fixture_html(name),
                                  final_url=src.get("final_url") or src["url"],
                                  noindex=(name == NOINDEX_FIXTURE)))
    if extras:
        entries.append(cache_page(settings, DUPLICATE_URL, fixture_html("www_news.html")))
        entries.append(cache_page(settings, DEV_COPY_URL, fixture_html("catalog_program.html")))
        entries.append(cache_page(settings, LEGACY_URL, fixture_html("www_bio.html"),
                                  final_url=fixture_url("www_bio.html")))
        entries.append(manifest_entry(NOT_FOUND_URL, None, status="not_found"))
        entries.append(manifest_entry(MISSING_FILE_URL, "www.nyit.edu/lost__0000000000.html"))
    write_jsonl(settings.manifest_path, entries)
    return entries
