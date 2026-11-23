# AskBear data: questions your teammates will ask (and the answers)

A briefing for the person who owns stage 1 (Collect). Each answer is short enough to say out
loud. Details are in [`README.md`](../README.md), [`METHODOLOGY.md`](METHODOLOGY.md) and
[`DATA_DICTIONARY.md`](DATA_DICTIONARY.md).

> Numbers are from the final dataset of 2026-11-23 (pages fetched 07:05-20:25 UTC; processed
> files built by `parse` at 16:13 EST; statistics and validation last run at 16:40 EST). The same
> numbers are in `data/processed/STATS.md`. Sizes use 1 MB = 1,000,000 bytes, as in the README.

---

## The 30-second version

> "I wrote a Python scraper that politely crawled every public NYIT website: the catalog,
> www.nyit.edu, Vancouver, the policy library, the library sites, blogs, events, the magazine
> and more. It also downloaded the PDF, Word, Excel and PowerPoint files those pages link to,
> and the full catalog PDFs. The pipeline cleans every page, splits it into passages at its
> headings, removes duplicates and writes one file, `corpus.jsonl`, with **41,725 passages**.
> Each passage has an id, a link to its source page, a title, the text and metadata such as
> site, school, program, level and campus. BM25 can index it directly, and the reranker gets
> the passage text. 46 automatic quality checks run without a single failure, and the
> proposal's numbers (581 catalog PDF pages, about 249K words) are reproduced exactly."

---

## 1. Where did the data come from?

**Which websites?**
Every public `*.nyit.edu` website that NYIT's own pages link to. That means 18 websites with
content in the dataset:
- **catalog.nyit.edu** (programs, curricula, courses, policies)
- **www.nyit.edu** (admissions, tuition, degrees, offices, news, faculty bios, faculty and staff accomplishments)
- **vancouver.nyit.edu**
- **apps.nyit.edu/policies** (the policy library and handbooks)
- the library sites: **libguides**, **libanswers** and **libcal** (hours and library events)
- **blogs.nyit.edu**, **events.nyit.edu**, **magazine.nyit.edu**, **online.nyit.edu**
- **deseversky**, **comapp**, **biggive**, **files.nyit.edu**, **aaupatnyit**, **eportfolios** (home page) and **wifi**

It also covers:
- **Documents linked from those pages:** 1,682 distinct files (1,567 PDFs, 52 Word, 60 Excel
  and 3 PowerPoint files) with 17,648 pages.
- **The official catalog PDFs:** 2025-26 and 2026-27, undergraduate, graduate and osteopathic
  medicine (6 PDFs, 1,297 pages).

**Is anything left out on purpose?**
Yes:
- Login portals and single sign-on, such as my.nyit.edu, campusgroups, alumni and Okta.
- Application and booking forms, such as apply.nyit.edu and LibCal's room booking.
- Sites whose robots.txt forbids crawling: web.nyit.edu, registrations.nyit.edu and the IT
  knowledge base on nyit.service-now.com.
- The library's EZproxy database gateway.
- Development copies such as catalog-dev.
- Survey and sign-in tools, social media, and non-NYIT domains.

The full list, with a reason for each, is in README section 7.2, or run
`python -m askbear_scraper sites`.

**How did you find all the pages?**
1. Start from each site's home page plus its **sitemap** (the list of pages a site publishes
   for search engines).
2. Follow links breadth-first (BFS), staying inside NYIT websites.
3. Lists that are split into pages are followed page by page: the blogs' `?p=2`, `?p=3`, ...
   (the only links to older posts), the 128 pages of faculty and staff accomplishments
   (`?fsa_page=N`) and the medical school's research projects (`?mrp_page=N`).
4. A new `*.nyit.edu` host found through a link is crawled too, with a page limit.

**When was it collected?**
2026-11-23. It is a snapshot: news, events and library hours change. For evaluation the team
should freeze one copy of `corpus.jsonl`.

---

## 2. How did you collect it? (the scraper)

**What tools?**
Python 3, with:
- `requests` for downloading,
- `BeautifulSoup` + `lxml` for HTML parsing,
- `PyMuPDF` for PDF text,
- the standard library for Word, Excel and PowerPoint files.

It is all in the `askbear_scraper` package and runs as one command:
`python -m askbear_scraper all`.

**Was it allowed / polite?**
Yes. The scraper follows the usual rules for well-behaved crawlers:
- It reads and obeys every site's **robots.txt**, including its **Crawl-delay**. The library
  sites ask for 10 seconds between pages, and we wait 10 seconds.
- It sends **one request at a time**. Two requests to the same site are at least 0.6 s apart
  (the main crawl) or 1 s apart (the later runs, the default), plus a little random jitter.
- It identifies itself with an honest User-Agent (`AskBearBot/1.0`, optionally with a team
  contact e-mail).
- It never logs in and never submits forms. It does not follow links on pages marked
  `nofollow`, and pages marked `noindex` stay out of the search corpus.
- Every page is **cached**, so re-runs don't download it again.
- Every URL and every redirect hop is checked against robots.txt before it is requested. The
  one URL robots.txt forbids is recorded as `robots_disallowed` and was never saved. (The main
  crawl was made by an earlier version that followed redirects automatically: about 12 of its
  requests reached robots.txt-forbidden pages that way, and none of them was kept; README 7.3.)

**How long did it take?**
- The crawl ran on one day in five runs, 5 h 14 min and 13,566 requests in all:
  - the main crawl: 2 h 08 min, 6,520 requests;
  - the library-hours site (LibCal): 52 s, 6 requests;
  - a catch-up crawl for the older blog posts, the rest of the library guides and the faculty
    accomplishments: 3 h 04 min, 6,998 requests (interrupted once, then resumed);
  - a short www.nyit.edu run for the research-projects listing: 2 min, 42 requests.
- In total 11,864 URLs were checked and 8,677 pages saved.
- The linked documents took about 26 minutes for the first 1,595 files and about 5 minutes
  for 147 more (1,742 downloads, 1,865 MB).
- Parsing everything takes about 8.5 minutes.

**What if it crashes or the internet drops?**
Run the same command again. It resumes from the cache and only fetches what is missing. (The
catch-up crawl was interrupted once and resumed exactly like that.)

---

## 3. How did you extract and clean the text?

**From web pages:**
1. Keep only the main content area of each page.
2. Remove navigation menus, footers, cookie banners, "Read more" buttons, share widgets,
   photo-gallery buttons and other text that repeats across a site's pages.
3. Turn tables into rows like `Course | Title | Credits`.
4. Fix encoding problems (`Ã©`, `&rsquo;`, stray control characters).
5. Keep headings that are really content (a sentence, "Step 5. Access your article.") as text.
6. Turn each library guide's librarian box into a passage of its own ("Librarian: <name>",
   with job title, subjects, e-mail, phone and address).
7. Pull out metadata: page title, breadcrumbs, page type (degree, policy, news, bio, event,
   FAQ, library guide, ...), school, program, degree, level (undergraduate/graduate) and campus.
   For news and events it also takes the publication date and event dates.

**From the catalog courses:**
Each course becomes its own record: code, title, credits, description, prerequisites and
school. That gives 3,389 courses (3,384 codes, 208 subjects).

**From PDFs:**
- Text is extracted page by page, with repeated headers, footers and page numbers removed.
- Titles come from the PDF's metadata, its first page or the link text; two documents of the
  same file type never share a title.
- Each document gets a category (policy/handbook, form, semester map, report, ...), its
  edition/year, and a flag for older editions that a newer one replaces.
- Scanned PDFs (images only, 146 files) have no text; there is no OCR.

**From Word/Excel/PowerPoint files:**
- Word files are split into parts of 150-400 words. Empty form prompts ("Click or tap here to
  enter text.") are skipped.
- Spreadsheets give one or more parts per sheet. A row under the column headings reads
  `Column: value | Column: value`, and percentages read `33%`.
- Presentations give one passage per slide, read top to bottom.
- The old binary formats (`.doc`, `.xls`: 4 linked files) are not read.

**What is a "passage"?**
A page is split at its headings (h2-h6) into sections. Sections over about 450 words are split
into parts of 150-400 words, never inside a table row. Each passage starts with its page title
and heading path, for example *"Computer Science, B.S. > Admission Requirements"*, so it makes
sense on its own when it is retrieved. The median passage is about 72 words and the longest is
464. A document page (a PDF page, a part of a Word file or spreadsheet, a slide) or a course is
one passage.

**How did you handle duplicates?**
NYIT publishes a lot of things more than once, so there are seven levels of de-duplication:
1. Redirects and URL aliases.
2. Identical pages (the catalog copy wins).
3. Tiny fragments, merged into their neighbours.
4. The same paragraph repeated across one site (e.g. handbooks that reprint policies, a
   librarian box on many guides), and post excerpts on a blog's list of posts. The copy kept
   is on the page it belongs to: not page 2+ of a list, the page whose title matches the
   passage's heading, not a hub page, then the shortest page.
5. Identical document files.
6. Identical document pages, plus older editions of yearly documents.
7. Near-duplicates across the whole corpus: 90% or more word overlap.

Near-duplicates that stay share a `dup_group` id, so the search can show one result per group.

---

## 4. How is it stored?

**Format:** plain-text **JSON Lines** (`.jsonl`): one JSON object per line, UTF-8. Any language
can read it, and it streams line by line. There is no database to install. `courses.csv` also
opens in Excel.

**Where:** in `data/processed/` (about 222 MB; about 47 MB zipped):

| File | What it is | Who needs it |
|---|---|---|
| `corpus.jsonl` | **the search corpus**: 41,725 passages (28,858 web sections, 9,822 document pages, 3,045 courses) | indexing / BM25 / reranker |
| `pages.jsonl` | every parsed web page with its full text and metadata (8,231 records) | anyone checking sources |
| `sections.jsonl` | the passages of each page, before they are merged into the corpus (29,100) | debugging |
| `courses.jsonl` / `courses.csv` | the course directory | anyone; easy to read in Excel |
| `documents.jsonl` | every page of every linked PDF, Word, Excel and PowerPoint file (17,648), with category and edition | optional sources |
| `pdf_pages.jsonl` | the official catalog PDFs, page by page | proposal numbers |
| `STATS.md`, `stats.json` | statistics tables for the report | the report |
| `validation_report.json` | results of the 46 quality checks | proof of quality |

The raw material (cached HTML, downloaded documents, crawl manifests, link graph) is in
`data/raw/`, about 2.8 GB; the run logs are in `data/logs/`. It is only needed to rebuild the
processed files.

**What does one corpus record look like?**

```json
{"id": "course-CSCI-260-UG",
 "source": "course",
 "url": "https://catalog.nyit.edu/courses?cal=UG&cc=CSCI-260&cs=CSCI",
 "title": "CSCI 260: Data Structures",
 "text": "CSCI 260: Data Structures\nComputer Science (CSCI), undergraduate course, 3.0 credits, ...",
 "metadata": {"site": "catalog.nyit.edu", "code": "CSCI 260", "level": "undergraduate",
              "school": "College of Engineering and Computing Sciences", "...": "..."}}
```

- Web passages have ids like `page-6657b7ff8f-s000`; document pages have ids like
  `doc-6076d48a8a-p0001`.
- Every record has the same 6 fields: `id`, `source`, `url`, `title`, `text`, `metadata`.

**How do I load it?**

```python
import json
corpus = [json.loads(line) for line in open("data/processed/corpus.jsonl", encoding="utf-8")]
```

or with pandas: `pd.read_json("data/processed/corpus.jsonl", lines=True)`.

---

## 5. Is it usable for our project?

**Yes, it was built for exactly this pipeline:**
- **Indexing / BM25:** index the `text` field of `corpus.jsonl`, and add `title` if you want.
  42K passages index in a few seconds; README section 9.3 has a working `rank_bm25` example.
- **Reranker:** score `(question, passage text)` pairs for BM25's top ~100 candidates. Web
  passages and courses are short (median 82 and 74 words, at most 480 with their title
  lines). Document pages can be longer (median 290 words, at most 4,256), so truncate or
  window them to the reranker's input limit.
- **Top 3 with source links:** every passage has a `url`, and catalog passages often have an
  `#anchor` that jumps to the exact spot on the page.
- **Filters:** `metadata` lets the search restrict or boost results. Fields include `site`,
  `source`, `page_type`, `school`, `program`, `level`, `campuses`, `catalog_year`, dates,
  `doc_category`, `file_type`, `superseded` and `source_weight` (a suggested down-weight for
  documents).
- **Evaluation (P@3, MRR, nDCG@10, Recall@100):** label each test question with the corpus `id`s
  (or URLs) of correct passages. Ids contain no spaces, so they work in TREC run/qrels files.
  Use one frozen copy of the corpus from labeling to final metrics.

**Does it match the proposal?**

| Proposal | Scraped |
|---|---|
| 581 catalog PDF pages (318 UG + 263 GR) | 581, exact |
| ~249K words | 249,274 |
| 1,200+ course codes | 2,369 UG+GR codes (3,384 with professional programs) |
| 100 subject areas | 159 UG+GR subjects |
| 6 schools, 4 campuses | 6 and 4 |
| 1 source (the catalog) | 18 NYIT websites + 1,682 linked documents |

**How do we know the data is good?**
- `python -m askbear_scraper validate --strict` runs 46 automatic checks: crawl completeness
  (including every page of paginated lists), unique ids, no login or error pages, no leaked
  boilerplate, text hygiene, valid links and anchors, metadata coverage, corpus in sync with
  its sources. Result: 39 pass, 7 warnings, 0 failures. The warnings describe NYIT's websites
  (broken links, unreachable hosts, pages without text, empty course subjects), not defects
  of the data.
- A suite of 1,132 tests covers the code: 1,131 pass offline on Python 3.12 and 3.14, and one
  live test against the real websites is skipped unless `ASKBEAR_LIVE=1`.
- Passages of every kind (web sections, courses, PDF pages, Word, Excel and PowerPoint parts)
  were spot-checked against the cached source pages and files they came from.

---

## 6. Limitations (say these before someone else finds them)

- **Snapshot:** the data is from 2026-11-23. Events, news and library hours go stale, so
  re-crawl before a final demo if freshness matters, then re-freeze the corpus.
- **Unreachable hosts:** 7 NYIT hosts that NYIT pages link to could not be reached at all
  (bearcat, camstream, hub, iris, nyitonline, tbls and www.imodules); their content is missing.
- **NYIT-side problems:**
  - 1,879 links on NYIT's own pages are broken (HTTP 404); most are links in older blog posts
    to www.nyit.edu news articles that no longer exist.
  - 7 catalog faculty pages return HTTP 500.
  - 59 catalog curriculum links lead to empty templates.
- **JavaScript-only content is missing:** pages that only render in a browser (some event
  widgets, the virtual tour) have little or no text.
- **No OCR:** 146 scanned PDFs have no text.
- **Old Office formats:** 4 linked `.doc`/`.xls` files are not read.
- **Public contact details stay in the text:** staff e-mails and phone numbers that NYIT
  prints on its pages, in about 2,700 passages (2,696 with an e-mail address, 1,485 with a
  phone number). This includes the 23 librarian profile passages of the library guides (name,
  job title, e-mail, phone, office address). Nothing private was collected; nothing behind a
  login was touched.
- **Sensitive text in public documents:** NYIT's public slide deck "UMB Commercial Card How-To
  Guide: First-Time User Log-In and Setup" prints a default first-login password (corpus id
  `doc-a20a98e1ad-p0003`; never try it), and one library FAQ question quotes a patron's library
  user ID (`page-1c20dc0edb-s000`). Don't surface such passages carelessly: filter or
  down-rank them in demos and screenshots.
- **Old documents:** older editions of yearly documents are marked `superseded` and kept out
  of the corpus by default. Some documents are low value for students (financial filings,
  accreditation reports); filter with `doc_category` or use `source_weight`.

---

## 7. Practical questions

**How do I give the data to the team?**
Zip `data/processed/` (about 47 MB zipped), or just `corpus.jsonl` (about 15 MB zipped), and
share it on Google Drive/OneDrive. In git, `data/processed/` can be committed: every file is
under GitHub's 100 MB limit. `data/raw/` is gitignored and too big for git.

**Can someone else re-run it?**
Yes: follow README sections 4-6. You need Python 3.9+ (3.11+ recommended) and
`pip install -r requirements.txt`. The 2-3 minute quick test (README section 5) proves the
setup works. A full rebuild takes several hours, mostly waiting for the polite delays.

**How do I add a website or change what is crawled?**
Edit the site profiles in `askbear_scraper/sites.py`, then run `crawl`, `parse`, `stats` and
`validate`. README section 12.3 explains how.

**What should go in the report?**
- `docs/METHODOLOGY.md` is written to be adapted: scope, crawling strategy, politeness and
  ethics, cleaning, passages, de-duplication, validation, results and limitations.
- `data/processed/STATS.md` has the tables.
- README section 10 has the comparison with the proposal.
