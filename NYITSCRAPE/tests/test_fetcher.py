"""fetcher.py: PoliteFetcher with a fake session and an injected clock/sleep."""

from __future__ import annotations

import pytest
import requests

from askbear_scraper import fetcher as fetcher_mod
from askbear_scraper.config import Settings
from askbear_scraper.fetcher import FetchResult, PoliteFetcher

from testkit import FakeClock, FakeWeb, Reply, make_fetcher

WWW = "https://www.nyit.edu"


def test_user_agent_is_honest_and_can_name_a_contact(settings, web, fetcher):
    web.add(WWW + "/", "<html><title>Home</title></html>")
    fetcher.get(WWW + "/")
    ua = web.request_headers[-1]["User-Agent"]
    assert ua.startswith("AskBearBot/1.0") and "CSCI 426" in ua and "contact" not in ua
    with_contact = Settings(data_dir=settings.data_dir, contact="you@nyit.edu")
    assert with_contact.user_agent.endswith("; contact: you@nyit.edu)")


def test_successful_get_decodes_utf8_when_no_charset_is_sent(fetcher, web):
    web.add(WWW + "/a/", "<html><title>Catalog 2026–2027 – “ok”</title></html>".encode("utf-8"),
            content_type="text/html")
    res = fetcher.get(WWW + "/a/")
    assert res.ok and res.is_html and res.status == 200
    assert "2026–2027 – “ok”" in res.text
    assert res.final_url == WWW + "/a/"
    assert res.content.startswith(b"<html>")


def test_binary_responses_have_no_text(fetcher, web):
    web.add(WWW + "/f.pdf", b"%PDF-1.4 ...", content_type="application/pdf")
    res = fetcher.get(WWW + "/f.pdf")
    assert res.ok and not res.is_html and res.text == "" and res.content == b"%PDF-1.4 ..."


def test_redirects_are_followed_and_reported(fetcher, web):
    web.redirect("https://nyit.edu/admissions", WWW + "/admissions/")
    web.add(WWW + "/admissions/", "<html>Admissions</html>")
    res = fetcher.get("https://nyit.edu/admissions")
    assert res.ok and res.url == "https://nyit.edu/admissions" and res.final_url == WWW + "/admissions/"


def test_retries_5xx_with_exponential_backoff_then_succeeds(fetcher, web, clock):
    web.routes[WWW + "/b/"] = [Reply("busy", 503), Reply("busy", 502), Reply("<html>ok</html>")]
    res = fetcher.get(WWW + "/b/")
    assert res.ok
    assert web.count(lambda u: u == WWW + "/b/") == 3
    assert clock.sleeps == [2.0, 4.0]            # backoff ** attempt, no politeness wait needed


def test_gives_up_after_max_retries_and_returns_the_last_status(settings, web, clock):
    settings.max_retries = 3
    f = make_fetcher(settings, web, clock)
    web.add(WWW + "/down/", "down", status=500)
    res = f.get(WWW + "/down/")
    assert res.status == 500 and not res.ok and res.error == ""
    assert web.count(lambda u: u.endswith("/down/")) == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_4xx_errors_are_not_retried(fetcher, web, clock):
    web.add(WWW + "/missing/", "nope", status=404)
    res = fetcher.get(WWW + "/missing/")
    assert res.status == 404 and not res.ok
    assert web.count(lambda u: u.endswith("/missing/")) == 1
    assert clock.sleeps == []


def test_retry_after_is_honoured_but_capped(fetcher, web, clock):
    web.routes[WWW + "/slow/"] = [Reply("x", 429, {"Retry-After": "5"}),
                                  Reply("x", 503, {"Retry-After": "300"}), Reply("<html>ok</html>")]
    assert fetcher.get(WWW + "/slow/").ok
    assert clock.sleeps == [5.0, fetcher_mod.MAX_RETRY_AFTER]
    assert fetcher_mod.MAX_RETRY_AFTER == 30.0


def test_retry_after_http_date_falls_back_to_backoff(fetcher, web, clock):
    web.routes[WWW + "/d/"] = [Reply("x", 503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                               Reply("<html>ok</html>")]
    assert fetcher.get(WWW + "/d/").ok
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize("value", ["-5", "nan"])
def test_malformed_retry_after_does_not_crash(fetcher, web, value):
    web.routes[WWW + "/odd/"] = [Reply("x", 503, {"Retry-After": value}), Reply("<html>ok</html>")]
    assert fetcher.get(WWW + "/odd/").ok


def test_fatal_errors_are_not_retried(fetcher, web, clock):
    web.routes[WWW + "/loop/"] = requests.exceptions.TooManyRedirects("Exceeded 30 redirects.")
    res = fetcher.get(WWW + "/loop/")
    assert res.status == 0 and not res.ok and res.error.startswith("TooManyRedirects")
    assert web.count(lambda u: u.endswith("/loop/")) == 1
    assert clock.sleeps == []


def test_connection_errors_get_at_most_two_attempts(settings, web, clock):
    settings.max_retries = 4
    f = make_fetcher(settings, web, clock)
    web.routes[WWW + "/x/"] = requests.exceptions.ConnectionError("Connection refused")
    res = f.get(WWW + "/x/")
    assert res.status == 0 and "ConnectionError" in res.error
    assert web.count(lambda u: u.endswith("/x/")) == 2
    assert clock.sleeps == [2.0]


def test_read_timeouts_use_all_retries(settings, web, clock):
    settings.max_retries = 2
    f = make_fetcher(settings, web, clock)
    web.routes[WWW + "/t/"] = requests.exceptions.ReadTimeout("read timed out")
    res = f.get(WWW + "/t/")
    assert "ReadTimeout" in res.error
    assert web.count(lambda u: u.endswith("/t/")) == 3


def test_a_network_error_then_success(fetcher, web):
    web.routes[WWW + "/flaky/"] = [requests.exceptions.ChunkedEncodingError("cut"), Reply("<html>ok</html>")]
    assert fetcher.get(WWW + "/flaky/").ok


def test_timeouts_use_a_short_connect_timeout(settings):
    settings.timeout = 30.0
    assert PoliteFetcher(settings)._timeout() == (fetcher_mod.CONNECT_TIMEOUT, 30.0)
    settings.timeout = 5.0
    assert PoliteFetcher(settings)._timeout() == (5.0, 5.0)


# ---------------------------------------------------------------------------
# politeness
# ---------------------------------------------------------------------------
def test_same_website_waits_for_the_delay_other_websites_do_not(fetcher, web, clock):
    for host in ("www.nyit.edu", "catalog.nyit.edu"):
        for path in ("/1", "/2"):
            web.add(f"https://{host}{path}", "<html>x</html>")
    fetcher.get(WWW + "/1")
    assert fetcher.ready_in("www.nyit.edu") == pytest.approx(1.0)
    assert fetcher.ready_in(WWW + "/anything") == pytest.approx(1.0)
    assert fetcher.ready_in("catalog.nyit.edu") == 0.0
    fetcher.get("https://catalog.nyit.edu/1")       # another website: no waiting
    assert clock.sleeps == []
    fetcher.get(WWW + "/2")                         # same website: waits 1 s
    assert clock.sleeps == [pytest.approx(1.0)]
    clock.advance(5)
    fetcher.get("https://catalog.nyit.edu/2")       # the delay has passed already
    assert len(clock.sleeps) == 1
    assert fetcher.requests_made == 4


def test_jitter_adds_a_random_extra_gap(settings, web, clock):
    settings.jitter = 0.5
    f = make_fetcher(settings, web, clock)
    web.add(WWW + "/1", "x")
    f.get(WWW + "/1")
    assert 1.0 <= f.ready_in("www.nyit.edu") <= 1.5


LIBGUIDES_ROBOTS = """User-agent: Amazonbot
Disallow: /

User-agent: Twitterbot
Crawl-delay: 0
Disallow:

User-agent: *
Crawl-delay: 10
    Disallow: /az.php?
    Disallow: /srch.php

    Sitemap: https://libguides.nyit.edu/sitemap.xml
"""


def test_robots_txt_disallow_and_crawl_delay(fetcher, web, clock):
    web.robots("libguides.nyit.edu", LIBGUIDES_ROBOTS)
    web.add("https://libguides.nyit.edu/ai", "<html>AI</html>")
    assert fetcher.allowed("https://libguides.nyit.edu/ai")
    assert not fetcher.allowed("https://libguides.nyit.edu/srch.php?q=x")
    assert not fetcher.allowed("https://libguides.nyit.edu/az.php?a=1")
    assert fetcher.delay_for("libguides.nyit.edu") == 10.0
    assert fetcher.robots_sitemaps("https://libguides.nyit.edu/") == ["https://libguides.nyit.edu/sitemap.xml"]
    # robots.txt is downloaded once per website
    assert web.requests.count("https://libguides.nyit.edu/robots.txt") == 1
    web.add("https://libguides.nyit.edu/apa", "<html>APA</html>")
    fetcher.get("https://libguides.nyit.edu/ai")
    assert fetcher.ready_in("libguides.nyit.edu") == pytest.approx(10.0)
    fetcher.get("https://libguides.nyit.edu/apa")
    assert clock.sleeps[-1] == pytest.approx(10.0)     # Crawl-delay between two pages


def test_first_request_after_robots_txt_honours_crawl_delay(fetcher, web, clock):
    web.robots("libguides.nyit.edu", LIBGUIDES_ROBOTS)
    web.add("https://libguides.nyit.edu/ai", "<html>AI</html>")
    assert fetcher.allowed("https://libguides.nyit.edu/ai")
    fetcher.get("https://libguides.nyit.edu/ai")
    assert clock.sleeps == [pytest.approx(10.0)]


def test_crawl_delay_is_capped(fetcher, web):
    web.robots("slow.nyit.edu", "User-agent: *\nCrawl-delay: 600\n")
    assert fetcher.allowed("https://slow.nyit.edu/x")
    assert fetcher.delay_for("slow.nyit.edu") == fetcher_mod.MAX_CRAWL_DELAY == 60.0


def test_crawl_delay_never_shortens_our_own_delay(fetcher, web):
    web.robots("fast.nyit.edu", "User-agent: *\nCrawl-delay: 0\n")
    fetcher.allowed("https://fast.nyit.edu/")
    assert fetcher.delay_for("fast.nyit.edu") == 1.0


@pytest.mark.parametrize("robots", [
    Reply("not found", 404),
    Reply("<!DOCTYPE html><html><title>301 Moved Permanently</title></html>", 200),
    Reply("", 200, content_type="text/plain"),
])
def test_missing_or_html_robots_txt_allows_everything(fetcher, web, robots):
    web.routes["https://catalog.nyit.edu/robots.txt"] = robots
    assert fetcher.allowed("https://catalog.nyit.edu/engineering")
    assert fetcher.robots_sitemaps("https://catalog.nyit.edu/") == []


def test_ignoring_robots_skips_the_download(settings, web, clock):
    settings.respect_robots = False
    f = make_fetcher(settings, web, clock)
    web.robots("www.nyit.edu", "User-agent: *\nDisallow: /\n")
    assert f.allowed(WWW + "/private/")
    assert web.requests == []


def test_unreachable_website_is_remembered_and_allowed(fetcher, web):
    """allowed() only answers "does robots.txt forbid it?"; the unreachable website is
    remembered and nothing is requested from it (get() returns a network-style error)."""
    web.routes["https://bearcat.nyit.edu/robots.txt"] = requests.exceptions.ConnectTimeout("timed out")
    assert fetcher.allowed("https://bearcat.nyit.edu/x")
    assert fetcher.unreachable_hosts == {"bearcat.nyit.edu"}
    res = fetcher.get("https://bearcat.nyit.edu/x")
    assert res.status == 0 and "robots.txt" in res.error and not res.ok
    assert "https://bearcat.nyit.edu/x" not in web.requests


def test_robots_read_timeout_blocks_the_website_until_a_later_try(fetcher, web, clock):
    """RFC 9309: a robots.txt that cannot be read (here: it times out) means "assume
    complete disallow" for now; it is tried again later instead of being ignored."""
    web.routes["https://www.nyit.edu/robots.txt"] = requests.exceptions.ReadTimeout("slow")
    assert fetcher.check(WWW + "/x/") == fetcher_mod.WAIT
    assert fetcher.unreachable_hosts == {"www.nyit.edu"}
    assert fetcher.allowed(WWW + "/x/")                  # not a robots.txt refusal...
    assert not fetcher.get(WWW + "/x/").ok               # ...but nothing is fetched
    assert WWW + "/x/" not in web.requests
    web.robots("www.nyit.edu", "User-agent: *\nDisallow: /private/\n")
    clock.advance(fetcher_mod.ROBOTS_REPROBE_DELAYS[0])
    assert fetcher.check(WWW + "/x/") == fetcher_mod.ALLOWED
    assert fetcher.check(WWW + "/private/a/") == fetcher_mod.DISALLOWED
    assert fetcher.unreachable_hosts == set()


def test_redirect_hop_to_another_website_respects_its_delay(fetcher, web, clock):
    times = {}

    def stamp(url, reply):
        def answer(request):
            times.setdefault(url, []).append(clock())
            return reply
        return answer

    web.routes[WWW + "/a/"] = stamp(WWW + "/a/", Reply("<html>a</html>"))
    web.routes["https://site.nyit.edu/a"] = Reply("", 301, {"Location": WWW + "/a/"}, None)
    fetcher.get(WWW + "/a/")
    fetcher.get("https://site.nyit.edu/a")
    first, second = times[WWW + "/a/"]
    assert second - first >= fetcher.settings.delay


def test_content_type_is_case_insensitive(fetcher, web):
    web.add(WWW + "/upper/", "<html><title>Upper</title></html>", content_type="Text/HTML; charset=UTF-8")
    res = fetcher.get(WWW + "/upper/")
    assert res.is_html
    assert "Upper" in res.text


# ---------------------------------------------------------------------------
# download()
# ---------------------------------------------------------------------------
def test_download_streams_to_disk(fetcher, web, tmp_path):
    body = b"%PDF-1.7\n" + b"x" * 200_000
    web.add(WWW + "/files/big.pdf", body, content_type="application/pdf")
    dest = tmp_path / "sub" / "big.pdf"
    res = fetcher.download(WWW + "/files/big.pdf", dest)
    assert res.ok and res.status == 200 and dest.read_bytes() == body
    assert not dest.with_suffix(".pdf.part").exists()


def test_download_error_leaves_no_file(fetcher, web, tmp_path):
    dest = tmp_path / "missing.pdf"
    res = fetcher.download(WWW + "/files/missing.pdf", dest)
    assert res.status == 404 and res.error == "HTTP 404" and not res.ok
    assert not dest.exists()


def test_download_retries_server_errors(fetcher, web, clock, tmp_path):
    web.routes[WWW + "/files/f.pdf"] = [Reply("busy", 503), Reply(b"%PDF-1.4", 200, content_type="application/pdf")]
    res = fetcher.download(WWW + "/files/f.pdf", tmp_path / "f.pdf")
    assert res.ok and (tmp_path / "f.pdf").read_bytes() == b"%PDF-1.4"
    assert clock.sleeps == [2.0]


def test_download_network_error(settings, web, clock, tmp_path):
    settings.max_retries = 1
    f = make_fetcher(settings, web, clock)
    web.routes[WWW + "/files/f.pdf"] = requests.exceptions.ConnectionError("reset")
    res = f.download(WWW + "/files/f.pdf", tmp_path / "f.pdf")
    assert res.status == 0 and "ConnectionError" in res.error
    assert not (tmp_path / "f.pdf").exists() and not (tmp_path / "f.pdf.part").exists()


def test_fetch_result_ok_needs_2xx_and_no_error():
    assert FetchResult("u", "u", 204).ok
    assert not FetchResult("u", "u", 301).ok
    assert not FetchResult("u", "u", 200, error="boom").ok
    assert FetchResult("u", "u", 200, content_type="application/xhtml+xml").is_html


def test_default_session_is_a_real_requests_session(settings):
    f = PoliteFetcher(settings)
    assert isinstance(f.session, requests.Session)
    assert f.session.headers["User-Agent"] == settings.user_agent
    assert isinstance(FakeClock()(), float) and isinstance(FakeWeb(), requests.adapters.BaseAdapter)
