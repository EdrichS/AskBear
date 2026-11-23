"""sitemaps.py: XML sitemaps (index + urlset, gzip, CDATA, entities) read politely."""

from __future__ import annotations

import gzip

from askbear_scraper import sitemaps
from askbear_scraper.sitemaps import collect_sitemap_urls, parse_sitemap

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="//www.nyit.edu/main-sitemap.xsl"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
\t<sitemap>
\t\t<loc>https://www.nyit.edu/post-sitemap.xml</loc>
\t\t<lastmod>2026-11-20T14:19:13+00:00</lastmod>
\t</sitemap>
\t<sitemap>
\t\t<loc>https://www.nyit.edu/page-sitemap.xml.gz</loc>
\t</sitemap>
</sitemapindex>
"""

POSTS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
\t<url>
\t\t<loc>https://www.nyit.edu/news/articles/</loc>
\t\t<lastmod>2026-11-20T14:19:13+00:00</lastmod>
\t</url>
\t<url>
\t\t<loc><![CDATA[ https://www.nyit.edu/news/articles/student-profile-pahini-shah/ ]]></loc>
\t\t<image:image><image:loc>https://www.nyit.edu/wp-content/uploads/a.jpg</image:loc></image:image>
\t</url>
\t<URL><LOC>https://www.nyit.edu/news/articles/q-a-with-r&amp;d-team/?a=1&amp;b=2</LOC>
\t<LASTMOD> 2024-12-02 </LASTMOD></URL>
</urlset>
"""

PAGES = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.nyit.edu/about/</loc><lastmod>2026-10-07T13:52:33Z</lastmod></url>
  <url><loc>https://www.nyit.edu/admissions/</loc></url>
</urlset>
"""


def test_parse_urlset_with_cdata_entities_and_any_tag_case():
    pages, children = parse_sitemap(POSTS)
    assert children == []
    assert pages == [
        ("https://www.nyit.edu/news/articles/", "2026-11-20T14:19:13+00:00"),
        ("https://www.nyit.edu/news/articles/student-profile-pahini-shah/", None),
        ("https://www.nyit.edu/news/articles/q-a-with-r&d-team/?a=1&b=2", "2024-12-02"),
    ]


def test_parse_sitemap_index_lists_child_sitemaps_only():
    pages, children = parse_sitemap(INDEX)
    assert pages == []
    assert children == ["https://www.nyit.edu/post-sitemap.xml", "https://www.nyit.edu/page-sitemap.xml.gz"]


def test_parse_sitemap_ignores_garbage_and_html():
    assert parse_sitemap("") == ([], [])
    assert parse_sitemap("<html><body><h1>Moved Permanently</h1></body></html>") == ([], [])
    assert parse_sitemap("<urlset><url><lastmod>2020</lastmod></url></urlset>") == ([], [])


def test_no_xml_entity_expansion():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            "<urlset><url><loc>https://www.nyit.edu/&lol2;</loc></url></urlset>")
    pages, _ = parse_sitemap(bomb)
    assert pages == [("https://www.nyit.edu/&lol2;", None)]   # left alone, never expanded


def test_decode_gzip_and_plain_bytes():
    assert sitemaps._decode(gzip.compress(PAGES.encode("utf-8"))) == PAGES
    assert sitemaps._decode(PAGES.encode("utf-8")) == PAGES
    assert sitemaps._decode(b"\x1f\x8b not really gzip") == ""


def test_collect_follows_the_index_and_reads_gzip_sitemaps(fetcher, web):
    web.add("https://www.nyit.edu/sitemap_index.xml", INDEX, content_type="application/xml")
    web.add("https://www.nyit.edu/post-sitemap.xml", POSTS, content_type="application/xml")
    web.add("https://www.nyit.edu/page-sitemap.xml.gz", gzip.compress(PAGES.encode("utf-8")),
            content_type="application/x-gzip")
    found = collect_sitemap_urls(fetcher, ["https://www.nyit.edu/sitemap_index.xml"])
    assert [u for u, _ in found] == [
        "https://www.nyit.edu/news/articles/",
        "https://www.nyit.edu/news/articles/student-profile-pahini-shah/",
        "https://www.nyit.edu/news/articles/q-a-with-r&d-team/?a=1&b=2",
        "https://www.nyit.edu/about/",
        "https://www.nyit.edu/admissions/",
    ]
    # robots.txt once, then each sitemap once
    assert web.page_requests() == ["https://www.nyit.edu/sitemap_index.xml",
                                   "https://www.nyit.edu/post-sitemap.xml",
                                   "https://www.nyit.edu/page-sitemap.xml.gz"]


def test_collect_skips_missing_disallowed_and_repeated_sitemaps(fetcher, web):
    web.robots("www.nyit.edu", "User-agent: *\nDisallow: /private-sitemap.xml\n")
    loop = ('<sitemapindex><sitemap><loc>https://www.nyit.edu/a.xml</loc></sitemap>'
            '<sitemap><loc>https://www.nyit.edu/sitemap_index.xml</loc></sitemap>'
            '<sitemap><loc>https://www.nyit.edu/private-sitemap.xml</loc></sitemap>'
            '<sitemap><loc>https://www.nyit.edu/gone.xml</loc></sitemap></sitemapindex>')
    web.add("https://www.nyit.edu/sitemap_index.xml", loop, content_type="application/xml")
    web.add("https://www.nyit.edu/a.xml", PAGES, content_type="application/xml")
    found = collect_sitemap_urls(fetcher, ["https://www.nyit.edu/sitemap_index.xml", ""])
    assert [u for u, _ in found] == ["https://www.nyit.edu/about/", "https://www.nyit.edu/admissions/"]
    assert "https://www.nyit.edu/private-sitemap.xml" not in web.requests
    assert web.requests.count("https://www.nyit.edu/sitemap_index.xml") == 1
    assert "https://www.nyit.edu/gone.xml" in web.requests          # a 404 is skipped


def test_collect_stops_after_max_sitemaps(fetcher, web):
    index = "<sitemapindex>" + "".join(
        f"<sitemap><loc>https://www.nyit.edu/s{i}.xml</loc></sitemap>" for i in range(10)) + "</sitemapindex>"
    web.add("https://www.nyit.edu/sitemap_index.xml", index, content_type="application/xml")
    for i in range(10):
        web.add(f"https://www.nyit.edu/s{i}.xml",
                f"<urlset><url><loc>https://www.nyit.edu/p{i}/</loc></url></urlset>")
    found = collect_sitemap_urls(fetcher, ["https://www.nyit.edu/sitemap_index.xml"], max_sitemaps=3)
    assert len(found) == 2          # the index + 2 child sitemaps
    assert len(web.page_requests()) == 3
