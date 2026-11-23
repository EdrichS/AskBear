# Data collection methodology (AskBear, stage 1: Collect)

This document describes how the AskBear dataset was collected, cleaned and checked. It is
written so that you can copy or adapt its sections for the "Data" part of the project report
and presentation. The numbers come from the crawl of **2026-11-23** and the files in
`data/processed/`: `STATS.md`, `stats.json`, `parse_summary.json`, `documents_summary.json`,
`pdf_summary.json` and `validation_report.json`, plus `data/raw/crawl_summary.json`. The
field-level description of the files is in [DATA_DICTIONARY.md](DATA_DICTIONARY.md); how to run
everything is in the [README](../README.md).

## Summary

We built a polite, single-threaded web crawler and a parsing pipeline (the Python package
`askbear_scraper`) that collects the public NYIT websites. On 2026-11-23 it crawled 16
configured websites and the NYIT hosts they link to in five runs (a main crawl, a run for the
library-hours site, a catch-up crawl that was interrupted once and resumed, and a short
www.nyit.edu run): 13,566 network requests in 5 h 14 min, 11,864 URLs checked and 8,677 HTML
pages saved. These pages yield 7,585 distinct pages from 18 websites with 4.02 million words.
The crawler also downloaded the 1,682 distinct documents those pages link to (1,567 PDFs, 52
Word, 60 Excel and 3 PowerPoint files; 17,648 pages, 4.38 million words) and the full catalog
PDFs of 2025-2026 and 2026-2027 (6 PDFs, 1,297 pages). The pages were cleaned, split into
29,100 heading-based passages and de-duplicated at seven levels, from URLs to near-duplicate
passages. Combined with 3,045 catalog courses and 9,822 document pages they form a search
corpus of 41,725 records. Each record has a source link and metadata (site, school, program,
degree, level, page type, campus, dates, and for documents the file type, the kind of
document and its edition). The proposal's numbers are reproduced exactly: 581 pages and
249,274 words in the 2025-26 undergraduate and graduate catalog PDFs. Of 46 automatic quality
checks, 39 pass and 7 give explained warnings; none fails.

## 1. Goal and scope

AskBear answers student questions about NYIT. BM25 retrieves about 100 candidate passages, an ML
reranker re-scores them, and the system returns the top 3 passages with links to their sources.
It is evaluated with Precision@3, MRR, nDCG@10 and Recall@100. Stage 1 (Collect) therefore has to
deliver **passages** rather than whole pages: each passage short enough to be a useful answer,
long enough to make sense on its own, and linked to the place on the website where a student can
read it.

The proposal (slide 9) planned one source: the NYIT academic catalog, measured on the 2025-26
undergraduate and graduate catalog PDFs (581 pages, about 249K words, 1,200+ course codes in
100 subject areas, 6 schools, 4 campuses). During the project the scope was widened to **all public NYIT
websites**, because many student questions are answered outside the catalog: deadlines, offices,
library services, policies, events and campus life.

**In scope:** every page on a `*.nyit.edu` host that can be read without logging in, the PDF,
Word, Excel and PowerPoint documents those pages link to, and the full catalog PDFs.

**Out of scope, with the reason:**

| Excluded | Why |
|---|---|
| Login portals and back-office systems: my.nyit.edu, campusgroups, alumni, workday, onecard, engage, single sign-on and password hosts (nyitcs, identity, pwreset, `sso.`, `login.`, Okta) | not public; we never log in |
| Websites whose robots.txt forbids crawling: web.nyit.edu, registrations.nyit.edu, nyit.service-now.com (IT knowledge base: `Disallow: /`, also behind an Okta sign-in) | the site owner opted out |
| The library's EZproxy (`*.arktos.nyit.edu`, nyit.idm.oclc.org) | licensed third-party databases behind a library login |
| Development and test copies such as catalog-dev.nyit.edu | duplicates of real sites |
| apps.nyit.edu outside `/policies` (people directory, course search, forms) | personal contact data and dynamic search tools |
| Application and booking tools: apply.nyit.edu, cediploma.nyit.edu, comresearchapp.nyit.edu, and the booking, appointment and reservation pages of libcal.nyit.edu (its opening hours and events are crawled) | forms, not information pages |
| Web applications without public text: the simulation centers' LearningSpace (learningspace, iccjb), a REDCap survey tool (comresearchdata), a research-robot app (nyitetic: an IIS default page and script-only pages), a JavaScript-only virtual tour (virtualtour), and the Mahara ePortfolio software except its home page (eportfolios) | sign-in screens, forms and empty shells; found through links, disabled after the crawl |
| All non-NYIT websites (social media, government, publishers) | not NYIT sources; their links are recorded but never fetched |

## 2. Sources

| Website | Content | Pages saved | Distinct pages | Passages |
|---|---|---:|---:|---:|
| catalog.nyit.edu | academic catalog 2026-2027: programs, curricula, course directory, policies, tuition, financial aid | 983 | 756 | 1,881 |
| www.nyit.edu | main website: admissions, degrees, tuition, student services, news, faculty profiles and accomplishments, offices | 3,529 | 3,292 | 16,093 |
| apps.nyit.edu/policies | official policies and handbooks | 205 | 205 | 2,639 |
| libguides.nyit.edu | library research guides | 1,321 | 1,091 | 3,917 |
| libanswers.nyit.edu | library FAQ | 287 | 287 | 298 |
| blogs.nyit.edu | university blogs | 1,500 | 1,437 | 2,250 |
| events.nyit.edu | events calendar | 99 | 108 | 102 |
| vancouver.nyit.edu | Vancouver campus | 101 | 95 | 612 |
| online.nyit.edu | online programs | 67 | 64 | 683 |
| magazine.nyit.edu | NYIT Magazine | 59 | 58 | 138 |
| deseversky.nyit.edu | de Seversky Mansion | 35 | 32 | 81 |
| libcal.nyit.edu | library opening hours and library events | 14 | 14 | 29 |
| files.nyit.edu, nyit.edu, site.nyit.edu | file server (e-mail announcements, documents) and legacy hosts that redirect to other NYIT sites | 15 + 1 + 13 | 13 | 55 |
| eportfolios.nyit.edu | ePortfolio home page | 6 | 1 | 3 |
| 10 hosts found through links (comapp, biggive, aaupatnyit, wifi, and 6 disabled after the crawl) | smaller NYIT sites, crawled with a conservative default profile | 442 | 132 | 319 |

"Pages saved" counts by the host of the requested URL. "Distinct pages" and "Passages" count by
the host where the content lives after redirects. That is why events.nyit.edu has more distinct
pages than saved pages (9 www.nyit.edu links redirect there), and why the legacy hosts have
none: their 14 saved URLs redirect to catalog.nyit.edu (6), blogs.nyit.edu (6),
deseversky.nyit.edu (1) and www.nyit.edu (1). In all, 182 saved URLs are counted on another
site, most of them www.nyit.edu links to catalog pages (156). The 442 pages of discovered
hosts include 310 pages of six hosts that were disabled after the crawl because they hold no
public information (300 of them on catalog-dev.nyit.edu, a development copy of the catalog);
they are not parsed. libcal.nyit.edu was enabled after the main crawl and crawled in a separate
run on the same day; the catch-up crawl added its event pages.

## 3. Crawling strategy

**Site profiles.** Every website has a profile in `askbear_scraper/sites.py`: whether it is
crawled, its start pages (seeds), its XML sitemaps, allowed and excluded paths (regular
expressions), whether its links are followed, a page limit, and the CSS selectors of its main
content. Unknown `*.nyit.edu` hosts found through links get a default profile (crawled, at most
300 pages). Host names that look like sign-in, proxy or development systems are disabled
automatically.

**Discovery.** The crawl starts from each site's seeds and from every URL in its sitemaps. Eight
sites publish sitemaps, listing 4,051 URLs in total (www.nyit.edu alone 3,333). The crawler then
follows every in-scope link. On the catalog it also turns the course directory's subject dropdown
into links, so that no subject is missed even if no page links to it. Each link is classified as
a page to crawl, a document (PDF or Office file, handed to the document step), an asset, an
excluded path, a disabled NYIT host or an external website.

**URL normalisation.** Each page must be fetched once, so every URL is reduced to a canonical form:
- `https`, a lower-case host, and resolved `.` and `..` segments;
- tracking parameters (`utm_*`, `fbclid`, ...) removed;
- the site's trailing-slash rule applied. WordPress sites redirect to the slashed form; the
  catalog uses none.

The catalog is case-insensitive and keeps only its `cs` and `cal` query parameters. Its course
deep-link parameter `cc` is dropped, because it only unfolds a course in the browser. Other sites
drop their query parameters too, except those that select different content
(`keep_query_params` in `sites.py`): the page numbers of the blogs' and online.nyit.edu's
listings (`?p=`), of www.nyit.edu's faculty and staff accomplishments (`?fsa_page=`) and of the
College of Osteopathic Medicine's research projects (`?mrp_page=`), and the guide and page ids
of LibGuides (`?g=`, `?p=`). The crawler records which parameters it dropped from links that
lead back to the page they are on (in this crawl only filters and overlays: `?fsa_role=`,
`?tag=`, `?overlay=`, ...), and `validate` checks each page-number parameter on its own, so a
lost pagination shows up.

**Scheduling.** Each host has its own queue and its own politeness clock. The crawler serves the
hosts in turn and prefers URLs that are already cached (no waiting), so a slow site such as the
library guides (10 s per request) never holds up the others. When a host reaches its page limit,
its remaining queue is dropped and reported. Exact HTML copies of a page already saved do not
count toward the limit.

**Redirects.** Redirects are followed by hand, one hop at a time, at most 10 hops. Every hop
waits for its own website's delay and is checked against that website's robots.txt and against
the crawl scope: a redirect to a sign-in portal, a disabled NYIT host or another organisation's
website is recorded (`offsite_redirect`) and never requested, and a loop is stopped at the first
repeated URL (`redirect_loop`). When a URL whose trailing slash was added by normalisation loops,
it is tried once without the slash (legacy www.nyit.edu URLs).

**Response handling.** Every response gets a status (counts of the crawl manifest):

| Status | Meaning | URLs |
|---|---|---:|
| ok | real page, saved and parsed | 8,677 |
| not_found | HTTP 404/410, broken links on NYIT's own pages (1,143 of them old www.nyit.edu news links, mostly in older blog posts) | 1,879 |
| duplicate | redirects to a page that was already saved | 1,106 |
| offsite_redirect | redirects to a sign-in page, a skipped host or another organisation | 125 |
| redirect_loop | the URL redirects in a circle or more than 10 times (the main crawl recorded these 19 URLs as `error`; the catch-up crawl checked them again) | 19 |
| http_error | other HTTP errors, e.g. 7 catalog faculty pages that return HTTP 500 | 16 |
| error | network error after retries: URLs on 7 hosts that could not be reached | 14 |
| non_html | the link was not an HTML page | 14 |
| soft404 | "page not found" served with HTTP 200 (detected from the page template), a web-server default page, a script-only shell or an application error page | 13 |
| robots_disallowed | robots.txt forbids the URL or its redirect target | 1 |

Pages with a password field, and pages titled "Log in" or "Sign in" with hardly any text (some
sign-in screens build their form with JavaScript), are discarded as `login_page`; none were
found among the public pages. `<meta name="robots">` is read on every page. `nofollow` pages
contribute no links, and `noindex` pages are kept but never indexed. The page types of the
saved pages are checked again on every later run, without a request.

**Cache and resumability.** Each saved page is written to `data/raw/html/<host>/`, and one line is
appended to `data/raw/crawl_manifest.jsonl` (URL, final URL, status, HTTP code, cache file, title,
time). Re-running the crawler reads every final outcome from disk (saved pages, redirects to
saved pages, 404s, redirect loops, off-site redirects), so an interrupted crawl resumes where it
stopped and later processing never needs the network. Only network errors are retried, and an
HTTP 5xx answer gets one more attempt per run. A run restricted to some websites (`--site`), a
capped run (`--max-pages`) and an interrupted run keep every cached page they did not visit, and
merge the link graph, the external links and the crawl summary with the earlier results. The
summary lists every run whose pages are in the cache (`runs`) and adds up their time and
requests: a full crawl that reuses the cache is appended to that history, and only a
`--refresh` crawl starts a new one. After a `--site` run the status counts of its websites are
recounted from the merged manifest. Sitemap lists are cached and downloaded again after 7 days.

**Crawl runs.** The crawl was made in five runs on 2026-11-23 (all times UTC; 5 h 14 min and
13,566 network requests in all):

| Run | Websites | Ended | Time | Requests | URLs visited |
|---|---|---|---:|---:|---:|
| main crawl | 14 profiled websites and the hosts they link to | 11:06 | 2 h 08 min | 6,520 | 7,829 |
| library hours | libcal.nyit.edu, enabled after the main crawl | 15:01 | 52 s | 6 | 4 |
| catch-up crawl (interrupted) | all 16 profiled websites | 16:22 | 11 min 43 s | 815 | n/a |
| catch-up crawl (resumed) | all 16 profiled websites | 20:03 | 2 h 52 min | 6,183 | 11,495 |
| www.nyit.edu only | www.nyit.edu | 20:25 | 2 min 02 s | 42 | 4,748 |

The main crawl discovered 9,147 URLs and visited 7,829; some pages were already cached by
earlier test runs. It stopped the library guides at their 400-page limit of that time and did
not reach the later pages of paginated listings, because its URL rules dropped their page
numbers. The catch-up crawl was a full crawl that reused the cache, so pages already saved
cost no request. It fetched what the main crawl had missed: the older blog posts, through the
blogs' `?p=N` listing pages (306 listing pages); the rest of the library guides (1,321 guide
pages in all; the limit is now 1,500); all 128 pages of www.nyit.edu's faculty and staff accomplishments (`?fsa_page=N`);
online.nyit.edu's listing pages (5); and libcal.nyit.edu's event pages (14 LibCal pages in
all). The last run fetched the 6 pages of the research projects listing (`?mrp_page=N`). No
page limit was reached in the end. Seven hosts found through links could not be reached
(bearcat, camstream, hub, iris, nyitonline, tbls and www.imodules: unknown host names,
timeouts and a TLS failure), and the crawler gave up on iris.nyit.edu after repeated failures
(3 queued URLs).

## 4. Politeness and ethics

- **robots.txt.** It is downloaded for every host and obeyed, including `Crawl-delay`.
  libguides, libanswers and libcal ask all crawlers for 10 seconds between requests, and the
  crawler waits that long, also before the first page after reading robots.txt. These robots.txt
  files block several commercial and AI crawlers by name. Our crawler identifies itself as
  AskBearBot and falls under the generic rules, which allow crawling at that pace. A robots.txt
  that answers 401 or 403 means nothing is fetched; one that cannot be read (network error, HTTP
  5xx) blocks its website, which is tried again after 60 and 300 seconds (RFC 9309).
- **Redirects.** Every redirect hop is checked against its website's robots.txt and the crawl
  scope and waits for that website's delay; redirects to sign-in portals, disabled hosts or
  other organisations are recorded but never requested.
- **Rate limits.** One request at a time. Two requests to the same host are at least the
  configured delay apart, plus 0-0.25 s of random jitter. The default delay is 1.0 s and the
  minimum is 0.5 s; the main crawl used 0.6 s, the later runs the default 1.0 s.
- **Back-off.** Timeouts, HTTP 429 and 5xx responses are retried at most 4 times, with waits of
  2, 4, 8 and 16 s. A `Retry-After` header is honoured up to 30 s (negative or invalid values are
  ignored). Connection failures are retried once. A host that fails 3 times in a row is skipped
  for the rest of the run.
- **Identification.** The User-Agent is `AskBearBot/1.0 (NYIT CSCI 426 student
  information-retrieval project; polite single-threaded crawler)`, optionally followed by a contact
  e-mail (`--contact`).
- **No login.** No credentials are ever sent. Sign-in pages (a password field, or a "Log in"
  title on a page with hardly any text) and redirects to them are discarded, and the people
  directory and student systems are excluded.
- **Personal data.** `mailto:` links are never followed and no list of e-mail addresses is
  built, but the text of the public pages is kept as NYIT publishes it, including the contact
  details printed on them: 2,696 corpus records contain an e-mail address (1,602 distinct
  addresses, 1,219 of them `@nyit.edu`, mostly office and staff contacts on office, bio and
  policy pages and in documents), and 1,485 contain a phone number. The librarian profile box
  of a library guide becomes a passage of its own with the librarian's name, job title,
  subjects, e-mail, phone and office address (23 such passages). The faculty and staff
  profiles that NYIT publishes on www.nyit.edu, and the faculty lists in the catalog, are
  included because they are public information pages. `stats` reports these counts.
- **Respecting `noindex`.** Pages marked `noindex` (81 page records, 72 distinct pages) are kept
  for completeness but never indexed. Documents served with `X-Robots-Tag: noindex` are not
  extracted.
- **Size limits.** Documents larger than 40 MB are not downloaded (5 portfolio and magazine
  PDFs).
- **An earlier crawler version.** The main crawl of 2026-11-23 was made by a version that let
  the HTTP library follow redirects automatically. About 12 of its requests reached pages whose
  robots.txt forbids them, and about 80 reached sign-in pages or other organisations' websites
  through redirects of NYIT links. None of those pages was kept or parsed. Tests written after
  the crawl also found that the first page request to a library site followed its robots.txt
  request after the normal delay rather than the 10-second Crawl-delay. All three issues are
  fixed in the current crawler, which made the four later runs (LibCal, the catch-up crawl and
  the www.nyit.edu run).
- **Use of the data.** The data is used only for this course project. NYIT owns the content; the
  system shows short passages with a link to the original page instead of republishing them.

## 5. Document acquisition

**Catalog PDFs.** NYIT publishes each catalog edition as PDF files
(`catalog.nyit.edu/files/catalog/archives/`). We downloaded the undergraduate, graduate and
College of Osteopathic Medicine catalogs of 2025-2026 (318, 263 and 61 pages) and 2026-2027 (329,
262 and 64 pages): 6 PDFs, 1,297 pages, 28.8 MB. The 2025-2026 editions reproduce the proposal's
measurements. The 2026-2027 editions match the live website.

**Linked documents.** The crawler recorded 2,106 links to PDF files and 117 links to Word, Excel
and PowerPoint files in the current Office Open XML formats (`.docx`, `.xlsx`, `.pptx`):
- 409 point to other organisations;
- 29 point to NYIT hosts that are not crawled;
- 18 point to the catalog PDFs above.

The other 1,767 (1,650 PDFs, 53 Word, 61 Excel and 3 PowerPoint files) were downloaded politely,
each once, in two `docs` runs: 1,595 after the main crawl and 147 more (97.6 MB) after the
catch-up crawl:
- 1,742 succeeded (1,865.4 MB);
- 20 returned HTTP 404;
- 5 were larger than the 40 MB limit.

The same rules apply to every kind of file: only crawled NYIT websites, the same politeness,
size limit, manifest and resume behaviour. Files published under several URLs were recognised
by their SHA-1 and extracted once, which leaves 1,682 distinct documents with 17,648 pages:
forms, handbooks, program brochures, semester maps, class schedules, accreditation reports,
spreadsheets and newsletters. 146 of the PDFs are scanned images without a text layer; their
pages are kept but empty, because no OCR was applied. Links without a `.pdf` ending that serve a
PDF are recognised too (`served_as_pdf` in the external links, or a `non_html` entry with a PDF
type in the crawl manifest): the crawl found one, a 2.1 MB guide on online.nyit.edu, which is
downloaded and extracted. An Office download is accepted only when it is a ZIP package (it
starts with `PK\x03\x04`) holding `[Content_Types].xml` and the main part of its kind
(`word/document.xml`, `xl/workbook.xml` or `ppt/presentation.xml`); an HTML sign-in page served
instead is recorded as `not_office`, and a password-protected file (Office stores those
encrypted in an OLE2 container) as `encrypted`. None of the 117 Office downloads failed.
Legacy binary files (`.doc`, `.xls`, `.ppt`) are not read: 4 linked files (3 `.doc`, 1 `.xls`)
stay listed, with the reason, in `not_extracted_files` of `documents_summary.json`.

## 6. Parsing and cleaning

**Routing.** Each cached page is parsed according to the website where its content lives, which is
the host of the final URL after redirects. A link on nyit.edu that redirects to www.nyit.edu is
therefore parsed as a www.nyit.edu page. The catalog has a dedicated parser. Every other site uses a
generic parser with template rules for the template families NYIT uses:
- the WordPress "nyit" theme of www, vancouver and apps;
- the blogs;
- Springshare LibAnswers and LibGuides;
- the LiveWhale events calendar;
- the Foleon magazine;
- the online-programs site;
- Springshare LibCal (library hours and events).

Newly discovered hosts are matched to a template by their markup.

**Main content.** For each page the parser takes the main content element and removes page chrome:
- navigation, headers, footers, forms and buttons;
- cookie notices, share widgets, carousels and teaser lists;
- the "Previous Next" buttons of the photo galleries in blog posts (their captions stay);
- "Request Information / Plan Your Visit / Apply Today" calls to action;
- the collapsed course-description rows of curriculum tables, which repeat the course directory.

Content that a page hides until the reader clicks is included: drop-down panels (the "Find an
Advisor" page lists one advisor block per program), tabs, collapsible sections and LibGuides'
"more..." descriptions. Each drop-down panel becomes a passage titled with its option label.
Panels that hold a form or show account details (a username and password for a library
resource) stay hidden, and so do modals, pop-ups and menus. Pages whose whole body sits inside
one ASP.NET `<form>` are unwrapped.

Some templates need their own rules:
- **LibGuides librarian profiles.** The profile box of a guide's librarian (name, e-mail
  button, appointment widget, address, phone, subjects) becomes a passage of its own, headed
  "Librarian: <name>" ("Library contact: NYIT Library" for the library's own account), with
  the lines `Name`, `Job title`, `Subjects`, `Email`, `Phone` and `Address`. Left as it was,
  its "Librarian / NYIT Library" was too short to be a passage and was merged into the
  unrelated passage before it.
- **Faculty and staff accomplishments.** Each item of www.nyit.edu's accomplishments keeps its
  date, written out under its heading ("March 2, 2021"); the theme prints it as "03.02.21" in
  a card footer that is removed with the dates of teaser cards elsewhere.

A DOM walker then turns the content into a flat list of blocks (headings, paragraphs, list items,
table rows), keeping inline text on one line. Tables become one `cell | cell | cell` line per row,
and footnote superscripts become `[1]` (other superscripts are walked like ordinary text, so
"by<sup> </sup>Name" keeps its space). A plain `get_text()` would glue words from adjacent
blocks together and lose the table structure. A heading with nothing under it (its content was
a form, a feed or a row of buttons) is dropped, and a run of three or more such headings
becomes a list of link tiles. A heading that reads like content is kept as a bold line of
text instead, also when another heading follows directly: a sentence, a heading of 6 or more
words, or a numbered step ("Step 5. Access your article."). LibGuides boxes of screenshots or
videos have only such a heading for text.

**Boilerplate.** For every non-catalog site with at least 20 pages, a first pass counts how many of
the site's pages contain each paragraph. A paragraph that appears on at least 30% of them is
boilerplate and is removed from all of them. The catalog is exempt because its repeated
curriculum rows are real content. A paragraph that appears twice within one section of a page is
kept once; under different headings both copies stay, because the paragraph belongs to both
sections. Only the magazine, whose layout prints every block twice (desktop and mobile), also
drops near-copies anywhere on the page. Stand-alone copyright notices, widget labels ("Register
Here", "Donate Now") and the runs of library address cards that end LibAnswers answers are
removed as well.

**Text normalisation.** All text is normalised to Unicode NFKC, which turns PDF ligatures such as
"ﬁ" into "fi". Zero-width characters and soft hyphens are removed, and whitespace is collapsed.
UTF-8 text that had been mis-decoded as Windows-1252 ("â€™") is repaired, and HTML entities that
NYIT escaped twice ("&amp;rsquo;") are decoded. Control characters are dropped, except the C1
codes that are Windows-1252 punctuation decoded as Latin-1: those become that punctuation
("don\x92t" becomes "don’t").

**Metadata.** Each page gets:
- a title: the page heading, or og:title / `<title>` when that is the heading plus a real
  qualifier ("Management, M.B.A. – Finance" rather than six pages called "Management, M.B.A.");
  the person's name on profile pages; the program name added to generic titles such as
  "Curriculum Requirements"; "(page N)" on page 2, 3, ... of a paginated listing, only when the
  URL's one query parameter is the page number (LibGuides' `c.php?g=...&p=...` names a guide's
  tab, not a page number); the snapshot date on the library opening hours;
- its breadcrumbs and area;
- one of 29 page types (program, curriculum, degree, policy, news, event, faq, library_hours,
  communication, ...), from the URL, the breadcrumbs and WordPress body classes;
- the school, program, degree and level of academic pages;
- the campuses mentioned;
- the catalog year;
- dates: `published` is the publication date only (from `<meta>`, JSON-LD and bylines; magazine
  articles get the first day of their issue's season; e-mail announcements the date in their
  title or file name). Bio pages get none (their date is the import date of the staff
  directory). Events get `event_start` and `event_end`, when they take place;
- the page description.

Missing values are filled where the evidence is unambiguous:
- catalog pages that do not show the catalog year get the year shown on the others (686 pages);
- catalog pages that name a program but no school get the school of the program's other pages (363);
- sub-pages of www.nyit.edu degree pages get the school of their degree (138).

**Error pages.** Pages that are really errors served with HTTP 200 are skipped (101): 59 empty
catalog program templates (`/curriculum/` and `/semester-map/` links from www.nyit.edu that the
catalog does not fill), 39 "page not found" answers of the blogs (mostly to broken relative
links in posts, such as an e-mail address written as a link) and 3 test pages (titled "Test
..." or "cache", with hardly any text). The same rule recognises web-server default pages,
application error pages and placeholder pages. Event pages that the calendar no longer shows
(it serves its home page at their URL instead) are kept as page type `other`, without text or
passages (6 pages); past events that are still published keep the type `event` and their
`event_start`.

## 7. Sectioning into passages

Each page is split at its h2-h6 headings into **sections**, the passages of AskBear:

- The text before the first heading is a section with an empty heading.
- Each section records its `heading_path` (the enclosing headings). It also records an `#anchor`
  link when the heading or a marker just before it has an id or name. 10,938 sections link
  directly to their position on the page.
- Sections longer than 450 words are split into parts of about 150-400 words. The parts break
  preferably at bold pseudo-headings and table group headers, never inside a table row. A table
  part repeats the table's header row. Parts are titled "... (part 2)".
- Sections without text are dropped; their heading stays in the paths below them.
- In the corpus, each section's text starts with the page title and the heading path. A passage
  such as "Application Materials" is then still recognisable as part of "Computer Science, B.S. >
  Admission Requirements" when it is retrieved on its own.
- A section of fewer than 5 words ("TOTAL CREDITS: 30", "Yes.") is merged into its neighbour on
  the same page, its heading kept as a line (514 merged).
- Page 2, 3, ... of a list of posts (a blog's `?p=N`; 288 pages) only repeats the opening lines
  of posts that are pages of their own: it stays in `pages.jsonl` but gets no sections.

Result: 29,100 sections from 6,764 pages, with a median of 72 words, a mean of 117, a 90th
percentile of 303 and a maximum of 464. 12.5% have fewer than 20 words, typically short facts
such as office hours or a single requirement, and 1.4% have 400 words or more.

## 8. Course extraction

The catalog lists every course of a subject on one page (`/courses?cs=CSCI&cal=UG`). The course
parser reads the course table and the hidden detail row of each course, and splits the labelled
fields: description, prerequisites, corequisites, hours, flags such as "Inst Cnst". It handles the
site's quirks:
- other subjects listed on a subject's page;
- repeated codes;
- several spellings of "Corequisite";
- hours sentences inside descriptions;
- empty descriptions;
- codes with letters (`ARCH 701B`, `OM AP01`).

267 listing pages gave 3,856 course rows. After merging courses listed on several pages (keeping
the longest description), there are **3,389 courses**: 3,384 unique codes in 208 subjects. Of
these, 1,274 are undergraduate, 1,095 graduate and 1,020 professional (College of Osteopathic
Medicine). 80.5% have a description and 34.9% list prerequisites. 344 legacy registrar codes
("Conversion - OM": 0 credits, no description) are kept in `courses.jsonl` but left out of the
search corpus. Each course in the corpus links to its listing page with the course expanded.

## 9. PDF and Office text extraction

PDF pages are extracted with PyMuPDF:
- The content stream's reading order is kept.
- Cells on the same visual row are joined with ` | `, and cells that wrap over several lines are
  kept together.
- Raised footnote digits become `[1]`.
- Running headers, footers and page numbers are removed only when they sit in the page margin and
  repeat on many pages, so a "3" in a credits column is never removed.
- A line-final hyphen is kept when two halves are joined ("full-" + "time" becomes "full-time").
- Web-page chrome printed into the PDFs ("Back to Top", button arrows) is removed.

Each page gets the bookmark path that covers it. The 2026-2027 catalog PDFs have no bookmarks, so
their outline was rebuilt from the bold printed headings.

The linked documents need extra steps:
- rotated landscape pages are read the way they are displayed;
- list markers are reattached to their items;
- form blanks become `___` and dot leaders are shortened;
- page numbers a little away from the edge are removed;
- text layers that decode to nonsense (fonts without a Unicode map) are detected and dropped:
  119 pages;
- PDFs whose pages are mostly images are flagged as scanned;
- control characters and the private-use bullet glyphs of Symbol/Wingdings fonts are mapped or
  removed, a glyph repeated as separate "words" (the dot leaders of IRS forms: "m m m m") is
  dropped, and letter-spaced words ("M A G A Z I N E", "2 0 2 2") are joined;
- each document gets a readable title: its title metadata if meaningful, else the title printed
  in large type on page 1, else the link text, else the file name. Junk titles ("Page 1 of 2",
  "Name", the university's name) are skipped, one-word titles ("Agenda") are a last resort, and
  link text that is only a fragment of a split link ("t and Capital Form", "download one
  here") or a screen-reader note ("This link opens in a new window") is not used. Documents of
  the same file type that would share a title are told apart by their edition, another title
  they carry, their link text, the distinctive words of their file name ("WCCCStoCS" reads "WCCCS
  to CS") or the whole file name (361 titles got such a qualifier). No two documents of one
  file type share a title; a PDF and an Excel file may, because the corpus titles name the
  format ("(PDF, p. 1)", "(Excel, part 1)");
- each document gets a category (`doc_category`: policy_handbook, form, semester_map,
  schedule_calendar, transfer_agreement, brochure, report, course_material, event_program,
  newsletter_magazine, financial_filing, training, other). A semester-map or magazine folder
  decides first; then words that say what a document is, whatever its subject or folder (a form,
  template, affidavit, questionnaire, worksheet, checklist, log or sample letter is a `form`; a
  survey or frequency report is a `report`); then the `/policies/` folder (`policy_handbook`);
  then rules on the file name and title;
- the year, term or date its file name gives is its `edition` (812 documents). Documents that
  differ only by that part of the name are editions of one document (125 series, 564
  documents). An older edition whose period is over is `superseded` (241 documents, 5,594
  pages): it stays in documents.jsonl but is left out of the corpus. Semester maps and class
  calendars are per entering class (`cohort_year`, 263 PDFs) and never superseded.

**Word, Excel and PowerPoint files** (52, 60 and 3 documents with 124, 748 and 66 pages) are read
with Python's standard library (`zipfile` and `xml.etree`), not with an Office installation or a
third-party package, and cleaned like PDF text afterwards (control and private-use characters,
form blanks, list markers):
- **Word (.docx):** the body is read in order: paragraphs, tables (cells joined with ` | `, one
  row per line, nested tables flattened into their cell) and text boxes (after the paragraph
  that anchors them; content that Word stores twice for old and new readers is read once).
  Tracked insertions are kept and deletions dropped, so the text is the document as it reads
  with all changes accepted; field codes, hidden text, table-of-contents lines and the prompts
  of empty content controls ("Click or tap here to enter text.") are left out, and headers,
  footers, footnotes and comments are not read. Paragraphs in a heading style (also through
  the style they are based on) or with an outline level form the heading path (`toc_path`),
  unless they read as a sentence or a list item (real files format checklist items as
  "Heading 1"). Word files have no fixed pages, so the text is cut into "pages" of about 150 to
  400 words: a heading starts a new page once the current one has 150 words, no page grows
  past 400 words if a paragraph boundary allows it (a longer paragraph is cut after a
  sentence), and a heading never ends a page. A last page of fewer than 50 words joins the
  page before it when the two stay within 500 words (Excel sheets too).
- **Excel (.xlsx):** every visible sheet in workbook order (hidden sheets are skipped) becomes
  one or more pages of rows of at most about 400 words; empty cells and rows are left out. The
  column headings are found in the sheet rather than taken from its first row: a row of short
  text cells that the rows below fit, with up to three rows of group labels above it. Under such
  headings a row is written `Heading: value | Heading: value`, else `a | b | c`. A later page
  repeats the last one-cell line (a section or question) and the leading cells of the row
  group it starts in. Cells give their shared or inline string (rich-text runs joined,
  phonetic guides left out), their number as Excel shows it by default (`3`, not `3.0`),
  `TRUE`/`FALSE`, or the cached value of their formula. A number whose cell has a date, time or
  percentage format is shown as one (`2025-09-10`, `14:30`, `33%`); elapsed-time formats stay
  numbers. The sheet name is the `toc_path`, unless it is a default name such as "Sheet1".
- **PowerPoint (.pptx):** one slide is one page, in the order of the presentation's slide list
  (`ppt/_rels/presentation.xml.rels`), not of the file names. The title placeholder comes
  first; the text frames of the other shapes, grouped shapes and tables follow top to bottom,
  then left to right. Slide numbers, dates and footers are left out. The slide title is the
  `toc_path`.
- **Title and date:** the title is chosen by the same rules as for PDFs. `dc:title` of
  `docProps/core.xml` is the title metadata, but only when it matches the document: at least
  half of its words must occur in the file name, the link text, the printed title or the first
  lines (Office files keep the title of the template they were made from: "THE CAMPAIGN FOR" on
  a pledge letter); Office defaults such as "PowerPoint Presentation" are rejected like
  "Microsoft Word - form.docx". Then come the first Title/heading paragraph or the first slide
  title (template placeholders such as "Course Title" are skipped), the link text without its
  format note ("(Word document download)"), and the file name. `modified` is
  `dcterms:modified`, else the HTTP `Last-Modified`.
- **Safety:** the files come from the web, so the reader is defensive. Only stored or deflated
  ZIP members are read, at most 10,000 members per file and 52.4 MB (50 MiB) of uncompressed
  data in total; each member is inflated in bounded chunks, so a member that inflates to more than it
  declares (a "zip bomb") is refused instead of filling memory. Every XML part is decoded by the
  reader itself (UTF-8 or UTF-16) and refused when it declares a DOCTYPE or an ENTITY, so no
  entity is ever expanded ("billion laughs", external entities). The big parts (the Word body,
  worksheets, shared strings) are parsed incrementally, a paragraph, table row or spreadsheet
  row at a time. Both namespace flavours of the standard (Transitional and Strict) are
  understood. A file that breaks a rule is reported as `extract_error` and the run goes on.

**Check against the proposal:** with the proposal's word-count method (whitespace tokens of
PyMuPDF's plain text), the 2025-26 undergraduate and graduate PDFs have exactly 581 pages (318 +
263) and 249,274 words. The proposal says "~249K".

## 10. Deduplication

NYIT publishes much of its content more than once. The pipeline removes copies at seven levels and
always keeps the most useful copy:

| Level | Rule | Copies |
|---|---|---:|
| URLs | URL normalisation; redirects to an already saved page; several URLs that end at the same page (the directly fetched URL wins) | 1,106 redirects + 28 URL aliases |
| Pages | identical text (at least 10 words) on any two pages, on any site. The canonical copy is chosen by site priority (catalog first, then www.nyit.edu, then the others), then the catalog's own navigation over its `/curriculum/<slug>` template copies, then the URL that a copy declares as `rel=canonical`, then a sitemap URL, then the cleanest URL (no `%20`, lower case, fewest parameters). Copies stay in pages.jsonl with `duplicate_of` but get no passages | 646 pages |
| Tiny passages | a section of fewer than 5 words is merged into its neighbour | 514 sections |
| Passages within a site | the same text on several pages of one website, for the same program and level (catalog exempt); passages of 20 words or more match on their text alone, shorter ones also on their heading. Examples: apps.nyit.edu's handbooks reprint whole policies that also have their own page; LibGuides repeats a librarian's profile box on every guide of that librarian. The copy kept is, in this order: not on page 2, 3, ... of a paginated listing; on the page whose title has the most words of the passage's heading (a LibGuides box stays on the guide it is about); on a page that is not a home or listing page; on the shortest page (the policy's own page rather than the handbook); then the first URL. Also dropped: the post excerpts on a blog's list of posts whose word 5-shingles are, for 80% or more, in a passage of a post the list links to | 2,153 passages |
| Document files | identical files (same SHA-1) under several URLs are extracted once | 60 links |
| Document pages | a page whose text appears in another document (unchanged pages of yearly handbooks, the same report uploaded twice) enters the corpus once, from a current edition and then the most recently modified file; pages of superseded editions are left out | 713 + 4,780 pages |
| Near-duplicates across the corpus | records whose word 5-shingles have a Jaccard similarity of 0.9 or more (8 words or more), or the same full text, form a group (an exact set-similarity join). The most authoritative copy is kept: catalog and courses, then the policy library, www.nyit.edu, vancouver/online, the other sites (a page that is not a home or listing page first, then the shortest page), then documents (current edition first, then the most recently modified). A copy is left out when it has the same scope as the kept copy (same program and level, same course, same person, same entering class); copies for other programs, courses or classes stay and share `dup_group` | 360 left out (236 sections, 124 document pages); 2,207 records in 704 groups |

Pages marked `noindex` (81 page records), pages with fewer than 5 words (167) and page 2, 3, ...
of a list of posts (288) also get no passages. After these steps no two corpus records have the
same text.

## 11. The search corpus

`corpus.jsonl` combines the sources in one schema (`id`, `source`, `url`, `title`, `text`, `metadata`):

| Source | Records | Median words | What a record is |
|---|---:|---:|---|
| section | 28,858 | 82 | a passage of a web page (18 websites) |
| document | 9,822 | 290 | a page of a PDF, Word, Excel or PowerPoint file linked from an NYIT page (1,289 documents) |
| course | 3,045 | 74 | a course of the catalog's course directory |
| **total** | **41,725** | 103 | 6.9 million words |

The catalog PDF pages can be added with `parse --corpus-with-pdf`: 1,015 of the 1,297 pages
enter, giving 42,740 records (the other 282 pages are near-copies, mostly the 2025-2026 copy of a
page the 2026-2027 edition repeats). They are off by default because they repeat the catalog
website: 96-98% of the 5-word phrases of the 2026-2027 PDFs also appear on the website, and the
2025-2026 edition is older. All ids are unique and free of whitespace (they can go into TREC run
and qrels files unchanged; course ids read `course-CSCI-260-UG`), every record has a title, text,
website and an https link to an NYIT site, and the metadata supports filtering by site, page
type, school, program, degree, level, campus, catalog year, dates, file type, document category
and edition.

## 12. Quality validation

`python -m askbear_scraper validate` runs 46 automatic checks:

| Group | Checks | What is checked |
|---|---:|---|
| Crawl | 5 | the crawl finished with an empty queue and every enabled website saved pages; page limits and unreachable hosts; fetch errors; every site with a sitemap produced pages; paginated listings were followed (each page-number parameter on its own) |
| Pages | 12 | every cached page was parsed or skipped for a stated reason; unique ids; every record names its website; no error, sign-in, web-server default or off-site pages; titles and text; no generic titles; page types and levels valid; short pages; duplicates; no site mirrors another; noindex pages kept out of the index; dates (publication dates; events by `event_start`) |
| Catalog | 8 | catalog year everywhere; all six schools present; program metadata complete (catalog) and degree metadata complete (other sites); course listings produce courses; course codes, credits and levels valid; descriptions; course deep links |
| Passages | 4 | ids and text; length (at most 1,000 words); every `#anchor` exists in the page's HTML; no paragraph repeats in more than 30% of a site's passages |
| Text hygiene | 5 | no leftover HTML, entities, navigation noise, ligatures, broken, control or private-use characters in pages, sections, courses, catalog PDF pages and documents (plus glyph runs and letter-spaced words in PDFs) |
| Catalog PDFs | 2 | every page present; no empty pages |
| PDF documents | 4 | every page of every linked document (PDF, Word, Excel, PowerPoint) present and linked; share of pages without text; every document has a title of its own within its file type; older editions identified |
| Corpus | 6 | ids unique and complete; ids usable in TREC files; every section, course and document page with text has exactly one record (or is represented by the copy kept); no two records with the same text; no passage under 5 words |

**Result:** 39 PASS, 7 WARN, 0 FAIL. Every warning describes the websites or a known coverage
gap, not a defect of the data:
- seven hosts that could not be reached, and one (iris.nyit.edu) given up after repeated
  failures with 3 URLs left; no page limit was reached;
- 1,928 URLs that could not be fetched: 1,879 broken links (HTTP 404/410), 7 catalog faculty
  pages that fail with HTTP 500 on NYIT's server, 9 other HTTP errors, 19 redirect loops and 14
  URLs on unreachable hosts;
- 116 pages without text (image galleries, script-only pages, empty templates);
- 11 comapp.nyit.edu pages sharing one title;
- 237 short pages;
- 8 empty subjects in the course directory;
- one PDF page that literally contains a `<sup>` tag.

The check of paginated listings passes: every page-number parameter that `sites.py` follows
occurs in the cache. The checks were complemented by manual spot checks of passages of every
kind (web sections, courses, PDF pages, Word, Excel and PowerPoint parts) against the cached
pages and files they came from, and by a test suite of 1,132 tests (1,131 offline tests, with
trimmed real pages as fixtures, plus one live test that is skipped by default). Parsing is
deterministic: re-running `parse` on the same cache produces byte-identical `pages.jsonl`,
`sections.jsonl`, `courses.jsonl`, `courses.csv` and `corpus.jsonl` (a test checks this).

## 13. Results

| Measure | Proposal | Collected |
|---|---:|---:|
| Catalog PDF pages, 2025-26 UG + GR | 581 | 581 (318 + 263) |
| Words in those PDFs | ~249K | 249,274 |
| Course codes | 1,200+ | 2,369 UG + GR (3,384 with professional) |
| Subject areas | 100 | 159 UG + GR (208 with professional) |
| Schools / campuses | 6 / 4 | 6 / 4 |
| Websites | 1 | 18 |
| Distinct web pages / words | n/a | 7,585 / 4,017,908 |
| Passages (sections) | n/a | 29,100 |
| Linked documents / pages / words | n/a | 1,682 / 17,648 / 4,383,801 |
| Search corpus records | n/a | 41,725 |

The course counts are higher than the proposal's estimate because they come from the complete
course directory of the 2026-27 website rather than from the course pages of the PDFs.

## 14. Limitations

- **Snapshot.** The data reflects the websites on 2026-11-23. News, events, deadlines, library
  hours and documents change; the catalog changes once a year. Re-crawling with `--refresh`
  updates everything but also changes passage ids.
- **Coverage gaps.** The catch-up crawl closed the gaps of the main crawl (the library guides,
  the older blog posts and the faculty accomplishments). Still missing: 7 NYIT hosts that could
  not be reached (bearcat, camstream, hub, iris, nyitonline, tbls and www.imodules); 49
  back-issue magazine articles that are linked only from issue pages marked
  `noindex,nofollow`; and content behind logins, in robots.txt-excluded systems such as the IT
  knowledge base and in the EZproxy databases. On NYIT's side, 1,879 links of its own pages
  are broken (HTTP 404; 1,143 of them old www.nyit.edu news links, mostly in older blog posts),
  7 catalog faculty pages could not be fetched (HTTP 500), 59 catalog program links on
  www.nyit.edu lead to empty templates (among them the M.P.H., whose curriculum appears in the
  catalog only in the 2025-2026 osteopathic medicine PDF), and 5 PDFs over 40 MB were skipped.
- **No JavaScript.** Pages are parsed from their HTML as served. Content that only JavaScript
  loads is missing, for example the event-calendar widgets and the virtual tour; 116 pages have
  no text at all. Event pages are therefore built from their metadata (title, description,
  time).
- **No OCR, no legacy Office files.** 1,947 document pages have no text, 1,831 of which hold
  only images; 146 PDFs are entirely or mostly scanned. None of these pages is in the corpus.
  The 4 linked legacy binary files (3 `.doc`, 1 `.xls`) are not read, nor would `.ppt` or
  password-protected files be. The "pages" of Word and Excel files are cut by the scraper (about
  150-400 words), not by the author.
- **Heuristic metadata.** Page types, schools, programs and levels come from URL, breadcrumb and
  title rules; document categories and editions from file names and titles. `campuses` lists
  campuses *mentioned* on a page, not where a program is offered. `null` means "not specific" as
  well as "unknown". Magazine articles are dated by their issue's season, not their exact
  publication day.
- **Near-duplicates are a judgment call.** Passages that differ by a few words (Jaccard of 0.9 or
  more) count as copies. When two pages about the same program and level share a passage, one
  copy is kept; for example two concentrations of the same chemistry degree share their general
  education passages, and only one keeps them. Copies for different programs stay, grouped by
  `dup_group`, and the search stage should collapse each group.
- **Uneven passage lengths.** Most passages are 20-400 words, but document pages can be much
  longer (median 290 words, maximum 4,256) and some sections are a single short line. Rerankers
  with a token limit need to truncate or window long document pages.
- **Contact details and other sensitive text.** E-mail addresses and phone numbers that NYIT
  prints on its pages remain in the text (2,696 corpus records with an e-mail address, 1,485
  with a phone number), including the 23 librarian profile passages of the library guides. A
  public NYIT slide deck, the "UMB Commercial Card How-To Guide: First-Time User Log-In and
  Setup", prints a default first-login password (`doc-a20a98e1ad-p0003`), and one public
  library FAQ question quotes a patron's library user ID (`page-1c20dc0edb-s000`), exactly as
  NYIT published them. The search stage should not surface such passages carelessly.
- **Language.** Almost all content is English; no language filtering or translation was applied.
- **Crawler version.** The main crawl was made by an earlier version of the crawler, which let
  the HTTP library follow redirects (see section 4). The data it kept is the same, but about 90
  of its requests went to pages that the current version would not request.

## 15. Reproducibility

The dataset can be rebuilt with:

```bash
python -m askbear_scraper crawl --contact you@nyit.edu
python -m askbear_scraper pdf --edition 2025-2026 --edition 2026-2027 --kind undergraduate --kind graduate --kind osteopathic
python -m askbear_scraper docs --contact you@nyit.edu
python -m askbear_scraper parse
python -m askbear_scraper stats
python -m askbear_scraper validate --strict
```

The included data was built in steps on the same day. The main crawl used `--delay 0.6`;
libcal.nyit.edu was added with `crawl --site libcal.nyit.edu --max-pages 100` (the cap was a
safety bound; the site had 4 pages in scope then). The catch-up crawl was a plain `crawl` with
the default delay that reused the cache (interrupted once and started again, so it is two
entries of `runs`), and the last run was `crawl --site www.nyit.edu`. `docs` ran twice with
downloads, after the main crawl and after the catch-up crawl, and the documents were last
re-extracted offline with `docs --max-docs 0`. The data was built on Linux with Python 3.14 and
PyMuPDF 2.1; `constraints.txt` pins every package version used (`pip install -r
requirements.txt -c constraints.txt`), which keeps the text and the ids byte-identical. The
code targets Python 3.9 and newer and is covered by an offline test suite (`python -m pytest
-q`: 1,131 tests pass on Python 3.12 and 3.14, and one live test against the real websites is
skipped unless `ASKBEAR_LIVE=1`; a test checks every module against the Python 3.9 grammar).
All raw downloads are kept in `data/raw/` with a manifest, so every processed record can be
traced to the cached page or document it came from: `url`/`final_url` -> `file` in the
manifests.

## 16. Ethics statement (for the report)

> We collected only publicly accessible pages of NYIT's own websites. The crawler identified
> itself, obeyed robots.txt including the crawl delays requested by the library sites, sent one
> request at a time, paced its requests to each website (0.6 seconds or longer between two
> requests), and stored every page once so that re-runs did not load NYIT's servers again. We
> excluded login-protected systems, licensed library databases, personal directories and forms,
> did not follow e-mail links or build any list of addresses, and honoured "noindex" requests.
> The pages keep the contact details that NYIT itself publishes on them (office and staff
> e-mail addresses and phone numbers). The data is used only for this course project; AskBear
> shows short passages with a link to the original NYIT page instead of republishing content.
