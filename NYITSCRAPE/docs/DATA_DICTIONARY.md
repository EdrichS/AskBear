# AskBear data dictionary

This document describes every file that the scraper writes. For each field it gives the type,
the meaning and a real example value. All counts and examples come from the final dataset of
2026-11-23 (`parse` run at 16:13 EST; statistics and validation last run at 16:40 EST); field
names were checked against the real records and the code. The record types are defined in
`askbear_scraper/models.py`. Sizes use 1 MB = 1,000,000 bytes, as in the README.

Back to the [README](../README.md).

## Conventions

- **JSON Lines (`.jsonl`):** one JSON object per line, UTF-8, no header. Read the files with
  `encoding="utf-8"` (see README section 8.3).
- **Types:**
  - `string`, `int`, `float` and `bool` are the plain JSON types.
  - `string|null` may be JSON `null`.
  - `list[string]` is a JSON array.
  - `object` is a JSON object (a dict).
- **Times** are ISO 8601. Crawl and download times are in UTC (`2026-11-23T07:14:57+00:00`).
  Dates taken from a website keep that site's offset (`2025-06-02T15:48:11-04:00`) or are a
  plain date (`2026-09-25`).
- **Hashes:** `sha1` / `content_sha1` are 40 hex characters. Ids contain the first 10 hex
  characters of the SHA-1 of a URL, for example `page-0c112f43a5` for
  `https://catalog.nyit.edu/engineering/computer_science`.
- **URLs** are normalised: `https`, lower-case host, no tracking parameters, the site's
  trailing-slash rule. Two spellings of the same page therefore give the same string.
- **Words** means whitespace-separated tokens (`len(text.split())`), the same method as the proposal's numbers.

## Overview

| File | Record type | Records | Section |
|---|---|---:|---|
| `data/processed/corpus.jsonl` | `CorpusRecord` | 41,725 | [corpus.jsonl](#corpusjsonl) |
| `data/processed/pages.jsonl` | `PageRecord` | 8,231 | [pages.jsonl](#pagesjsonl) |
| `data/processed/sections.jsonl` | `SectionRecord` | 29,100 | [sections.jsonl](#sectionsjsonl) |
| `data/processed/courses.jsonl` / `courses.csv` | `CourseRecord` | 3,389 | [courses.jsonl and courses.csv](#coursesjsonl-and-coursescsv) |
| `data/processed/documents.jsonl` | `DocumentPageRecord` | 17,648 | [documents.jsonl](#documentsjsonl) |
| `data/processed/pdf_pages.jsonl` | `PdfPageRecord` | 1,297 | [pdf_pages.jsonl](#pdf_pagesjsonl) |
| `data/processed/parse_summary.json` | summary of `parse` | 1 | [parse_summary.json](#parse_summaryjson) |
| `data/processed/documents_summary.json` | summary of `docs` | 1 | [documents_summary.json](#documents_summaryjson) |
| `data/processed/pdf_summary.json` | summary of `pdf` | 1 | [pdf_summary.json](#pdf_summaryjson) |
| `data/processed/stats.json`, `STATS.md` | statistics | 1 | [stats.json and STATS.md](#statsjson-and-statsmd) |
| `data/processed/validation_report.json` | quality checks | 46 checks | [validation_report.json](#validation_reportjson) |
| `data/raw/crawl_manifest.jsonl` | one line per URL | 11,864 | [crawl_manifest.jsonl](#crawl_manifestjsonl) |
| `data/raw/crawl_summary.json` | summary of `crawl` | 1 | [crawl_summary.json](#crawl_summaryjson) |
| `data/raw/link_graph.jsonl` | links per page | 8,677 | [link_graph.jsonl](#link_graphjsonl) |
| `data/raw/external_links.jsonl` | links not crawled | 24,780 | [external_links.jsonl](#external_linksjsonl) |
| `data/raw/sitemaps/<host>.json` | sitemap lists | 8 files | [sitemaps](#sitemapshostjson) |
| `data/raw/docs_manifest.jsonl` | one line per document URL (PDF or Office file) | 1,767 | [docs_manifest.jsonl](#docs_manifestjsonl) |
| `data/raw/html/`, `data/raw/pdf/`, `data/raw/docs/` | cached files | 9,835 / 6 / 1,742 | [cached files](#cached-files) |

---

## data/processed

### corpus.jsonl

The search corpus: the file that BM25 and the reranker use. It holds one passage per line
(`CorpusRecord`) and has the same six fields for every source.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `id` | string | unique passage id (see the formats below). It never contains whitespace, so it can be written into TREC run and qrels files as it is | `"page-0c112f43a5-s006"` |
| `source` | string | `section` (28,858), `document` (9,822), `course` (3,045); `pdf_page` (1,015 of the 1,297 catalog PDF pages) only with `parse --corpus-with-pdf` | `"section"` |
| `url` | string | link to show with the answer: `#anchor` for sections that have one, `#page=N` for PDF pages, `?cc=SUBJ-NUM` for courses (the listing opens with that course expanded) | `"https://catalog.nyit.edu/engineering/computer_science#admissions_csbs_ecebs"` |
| `title` | string | short display title | `"Computer Science, B.S. — Admission Requirements"` |
| `text` | string | text to index. It starts with context lines: page title and heading path (sections), course header (courses), document title and bookmark path (documents), bookmark path (catalog PDF pages) | `"Computer Science, B.S.\nAdmission Requirements\nThis program has specific admission requirements ..."` |
| `metadata` | object | filters and features; the keys depend on `source` (below) | |

`id` formats:

| `source` | `id` format | Example |
|---|---|---|
| `section` | `<doc_id>-s<NNN>` (the `section_id` of sections.jsonl) | `page-0c112f43a5-s006` |
| `course` | `course-` + `course_id` with every run of other characters than letters and digits replaced by `-` (`metadata.course_id` keeps the original) | `course-CSCI-260-UG`; `course-MTIM-893-PROF-2` for `MTIM 893\|PROF\|2` |
| `document` | `doc-<10 hex of sha1(pdf_url)>-p<NNNN>` | `doc-6076d48a8a-p0001` |
| `pdf_page` | `pdf-<edition>-<catalog>-p<NNNN>` | `pdf-2025-2026-undergraduate-p0120` |

`title` formats:
- section: page title + ` — ` + heading. The intro uses the page title alone, and the intro's continuation parts are titled `<page title> (part N)`. Parts of other sections read `<page title> — <heading> (part N)`.
- course: `CSCI 260: Data Structures`.
- document: `<document title> (PDF, p. N)`; for the pages of Office files `<document title> (Word, part N)`, `(Excel, part N)` or `(PowerPoint, slide N)` (see `file_type`).
- catalog PDF page: `Undergraduate catalog 2025-2026, p. 120 — <section>`.

`metadata` of a **section** (15 keys, always present, values may be `null`):

| Key | Type | Meaning | Example |
|---|---|---|---|
| `site` | string | website of the page | `"catalog.nyit.edu"` |
| `doc_id` | string | page id; join with pages.jsonl (for `description`, the full text, ...) | `"page-0c112f43a5"` |
| `page_type` | string | page type (see [page types](#page-types)); never `course_index` or `course_listing`, whose sections are left out of the corpus | `"program"` |
| `school` | string\|null | one of the six official school names | `"College of Engineering and Computing Sciences"` |
| `program` | string\|null | program the page belongs to | `"Computer Science, B.S."` |
| `degree` | string\|null | degree abbreviation | `"B.S."` |
| `level` | string\|null | `undergraduate`, `graduate`, `doctoral`, `professional`, or `null` (not level-specific) | `"undergraduate"` |
| `catalog_year` | string | `"2026-2027"` on catalog pages, `""` elsewhere | `"2026-2027"` |
| `heading_path` | list[string] | headings above and including this section | `["Admission Requirements"]` |
| `breadcrumbs` | list[string] | the page's breadcrumb trail | `["New York Tech Catalog 2026–2027", "Schools and Colleges", ...]` |
| `campuses` | list[string] | campuses **mentioned** on the page (`[]` when none) | `["Long Island", "New York City"]` |
| `published` | string\|null | publication date of the page: news, blog posts, magazine articles (the first day of the issue's season), e-mail announcements, and most WordPress pages of www.nyit.edu and vancouver.nyit.edu. Never set on bios and events | `"2024-05-28T18:52:20+00:00"` |
| `modified` | string\|null | page modification date | `"2026-07-05T18:57:01+00:00"` |
| `event_start` | string\|null | event pages: when the event starts (ISO; in the event's local time zone when the site gives it, else UTC) | `"2026-12-10T12:00:00-05:00"` (a LibCal event) |
| `event_end` | string\|null | event pages: when it ends (`null` for all-day events or without an end time) | `"2026-12-10T14:00:00-05:00"` |

Records of every source can also have:

| Key | Type | Meaning | Example |
|---|---|---|---|
| `dup_group` | string | the passage is repeated nearly word for word (Jaccard of 0.9 or more over word 5-shingles) for other programs, courses or entering classes; all copies kept share this id (the id of one of them). Show one result per group | `"page-a4a8143522-s005"` |
| `dup_count` | int | how many records of the corpus share the group | `3` |

2,207 records are in 704 groups.

`metadata` of a **course** (9 keys):

| Key | Type | Meaning | Example |
|---|---|---|---|
| `site` | string | always the catalog | `"catalog.nyit.edu"` |
| `course_id` | string | see courses.jsonl | `"CSCI 260\|UG"` |
| `code` | string | course code | `"CSCI 260"` |
| `subject` | string | subject code | `"CSCI"` |
| `subject_name` | string | subject name | `"Computer Science"` |
| `level` | string | `undergraduate`, `graduate`, `professional` | `"undergraduate"` |
| `credits` | string | credits as printed | `"3.0"` |
| `school` | string\|null | **official** school name (the course table's abbreviations are mapped, so one school filter works for sections and courses) | `"College of Engineering and Computing Sciences"` |
| `catalog_year` | string | catalog edition | `"2026-2027"` |

`metadata` of a **document** page (15 keys; the document can be a PDF or a Word, Excel or
PowerPoint file, see `file_type`):

| Key | Type | Meaning | Example |
|---|---|---|---|
| `site` | string | host that serves the document | `"aaupatnyit.nyit.edu"` |
| `pdf_url` | string | the document (PDF or Office file) without the page anchor | `"https://aaupatnyit.nyit.edu/wp-content/uploads/sites/3/2026/10/AAUP_DLClassroomTaskForceReportandRecommendationsJuly2018.pdf"` |
| `page` | int | 1-based page number | `1` |
| `page_count` | int | pages in the document | `6` |
| `found_on` | list[string] | up to 5 NYIT pages that link to the document | `["https://aaupatnyit.nyit.edu/"]` |
| `toc_path` | list[string] | PDF bookmark path of the page (often empty); Word: heading path, Excel: sheet name (not a default name such as "Sheet1"), PowerPoint: slide title | `[]` |
| `modified` | string\|null | modification date recorded in the file | `"2018-10-10T15:01:47-04:00"` |
| `doc_category` | string | what kind of document it is, from its file name, folder and title: `policy_handbook`, `form`, `semester_map`, `schedule_calendar`, `transfer_agreement`, `brochure`, `report`, `course_material`, `event_program`, `newsletter_magazine`, `financial_filing`, `training`, `other` | `"report"` |
| `edition` | string\|null | the year, term or date its file name gives | `"2018-07"`; `"2024-25"`; `"Fall 2020"` |
| `doc_year` | int\|null | the last year the edition covers, else the year the file was modified | `2018` |
| `cohort_year` | int\|null | semester maps and class calendars: the entering class they are for | `null`; `2025` |
| `superseded` | bool | a newer edition of the same document exists (only in the corpus with `--corpus-with-superseded`) | `false` |
| `scanned` | bool | most pages of the PDF are images without text (always `false` for Office files) | `false` |
| `source_weight` | float | a coarse usefulness weight of the category: 1.0 policies and handbooks, 0.9 forms, calendars and semester maps, 0.8 transfer agreements and brochures, 0.7 other, 0.6 training, 0.5 reports and course material, 0.4 event programs and newsletters, 0.2 financial filings; halved for superseded documents | `0.5` |
| `file_type` | string | the kind of file: `pdf`, `docx` (Word), `xlsx` (Excel) or `pptx` (PowerPoint) | `"pdf"` |

`metadata` of a **pdf_page** (5 keys; only with `parse --corpus-with-pdf`):

| Key | Type | Meaning | Example |
|---|---|---|---|
| `site` | string | `catalog.nyit.edu` | `"catalog.nyit.edu"` |
| `edition` | string | catalog edition | `"2025-2026"` |
| `catalog` | string | `undergraduate`, `graduate`, `osteopathic` | `"undergraduate"` |
| `page` | int | 1-based page number | `120` |
| `toc_path` | list[string] | section path of the page | `["Policies and Rules", "Financial Aid: Undergraduate Students", "Transfer of Post-9/11 GI BILL Benefits to Dependents"]` |

What is **not** in the corpus:
- exact duplicate pages (646), `noindex` pages (81 records) and pages with fewer than 5 words (167);
- pages 2, 3, ... of a list of blog posts (`?p=N`; 288 pages);
- passages repeated word for word on several pages of one website, and the post excerpts on a
  blog's list of posts (2,153 in all);
- near-duplicates of a passage kept on a more authoritative page about the same program and
  level (360: 236 sections, which keep `duplicate_of` in sections.jsonl, and 124 document pages);
- the 6 sections of the course index page `/courses` (course listing pages get no sections at all);
- the 344 legacy "Conversion - OM" course codes;
- document pages that are empty (1,947), have fewer than 5 words (262), repeat a page of
  another document (713) or belong to an older edition (4,780; `--corpus-with-superseded` adds
  them);
- the catalog PDF pages, unless you run `parse --corpus-with-pdf`. Then 1,015 of the 1,297 pages
  are added (42,740 records in all); the other 282 are near-copies of pages kept (mostly the
  2025-2026 copy of a page that the 2026-2027 edition repeats).

### pages.jsonl

One `PageRecord` per web page, 8,231 records from 18 websites (7,585 distinct). Exact copies
stay in the file with `duplicate_of` set (646). Pages marked `noindex` (81), pages with fewer
than 5 words (167) and pages 2, 3, ... of a list of blog posts (`?p=N`; 288) stay too, but get
no sections. Error pages served with HTTP 200 are not in the file (101: see
`skipped.not_found_page` in parse_summary.json). Example:
`https://catalog.nyit.edu/engineering/computer_science`, unless noted.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `doc_id` | string | `page-` + first 10 hex characters of the SHA-1 of `url` | `"page-0c112f43a5"` |
| `url` | string | canonical URL of the page, i.e. where its content lives after redirects | `"https://catalog.nyit.edu/engineering/computer_science"` |
| `title` | string | the page heading (h1 or the template's title). Generic titles get the program appended ("Curriculum Requirements: Computer Science, B.S."). When og:title or `<title>` is the heading plus a qualifier, that fuller title is used ("Management, M.B.A. – Finance" instead of "Management, M.B.A."). Profile pages carry the person's name, never "Staff & Faculty Directory". Page 2, 3, ... of a paginated listing ends in "(page N)" (420 pages), but only when the page number is the URL's one query parameter (LibGuides' `c.php?g=...&p=...` names a guide's tab); LibCal's opening hours say when they were fetched ("Library Opening Hours (as of November 23, 2026)") | `"Computer Science, B.S."` |
| `page_title` | string | full `<title>` text (`""` on 2 pages) | `"Computer Science, B.S. \| College of Engineering and Computing Sciences \| New York Tech Catalog 2026–2027"` |
| `breadcrumbs` | list[string] | breadcrumb trail, outermost first (empty on 309 pages) | `["New York Tech Catalog 2026–2027", "Schools and Colleges", "College of Engineering and Computing Sciences", "Undergraduate Programs", "Computer Science, B.S."]` |
| `catalog_year` | string | `"2026-2027"` on catalog pages, `""` on other sites | `"2026-2027"` |
| `page_type` | string | one of 29 [page types](#page-types) | `"program"` |
| `area` | string\|null | top-level part of the site, from the breadcrumbs (`null` on 278 pages) | `"Schools and Colleges"`; on other sites e.g. `"New York Tech News"`, `"Academics"`, `"Ask a Librarian"` |
| `school` | string\|null | one of the 6 official school names (`null` on 6,373 pages: news, library, offices, ...) | `"College of Engineering and Computing Sciences"` |
| `program` | string\|null | program of program, curriculum and degree pages and their sub-pages | `"Computer Science, B.S."` |
| `degree` | string\|null | degree abbreviation: `B.S.`, `M.S.`, `Advanced Certificate`, `Minor`, `M.B.A.`, `Ph.D.`, `B.F.A.`, `D.O.`, `B.S./M.S.`, ... | `"B.S."` |
| `level` | string\|null | `undergraduate`, `graduate`, `doctoral`, `professional`, or `null` (not level-specific; 6,684 pages) | `"undergraduate"` |
| `campuses` | list[string] | campuses **mentioned** in the title or text: `Long Island`, `New York City`, `Jonesboro`, `Vancouver` (empty on 5,367 pages) | `["Long Island", "New York City"]` |
| `headings` | list[string] | the h2-h4 headings in reading order | `["Concentrations in Artificial Intelligence, Big Data Management and Analytics, or Network Security", "Co-op (Cooperative Education) Option", ...]` |
| `text` | string | clean main-content text: one block per line, a blank line before each heading, tables as `cell \| cell` rows, footnotes as `[1]`. Content behind drop-downs, tabs and "more..." toggles is included (except panels that show account details) (`""` on 116 pages: galleries, script-only pages, empty templates) | `"... Computer science is the stimulating force at the center of the information revolution ..."` |
| `word_count` | int | words in `text` | `1814` |
| `num_tables` | int | tables in the main content | `0` (`3` on `.../computer_science/curriculum`) |
| `links` | list[object] | links inside the main content: `{"text": ..., "url": ...}` with normalised URLs (empty on 2,006 pages) | `[{"text": "work full-time for six months", "url": "https://catalog.nyit.edu/policies_and_rules/academic_policies"}, ...]` |
| `content_sha1` | string | SHA-1 of `text`, used to detect exact duplicates | `"90631dab1fac8910e2197e8c6ec499221a4c98fa"` |
| `fetched_at` | string | when the HTML was downloaded (UTC; `fetched_at` of the crawl manifest) | `"2026-11-23T07:14:57+00:00"` |
| `duplicate_of` | string\|null | URL of the canonical page with exactly the same text, on any site. The page then gets no sections. Canonical order: catalog, then www.nyit.edu, then other sites; non-noindex pages first; then the URL a copy declares as `rel=canonical`, a sitemap URL, a clean URL (no `%20`, lower case, fewer parameters) | `null`; for `https://catalog.nyit.edu/curriculum/computer-science-bs` it is `"https://catalog.nyit.edu/engineering/computer_science/curriculum"` |
| `site` | string | website of the page (host of `url`) | `"catalog.nyit.edu"` |
| `description` | string\|null | the page's `<meta name="description">` or `og:description`, when it is specific to the page (not the site's generic one) | `"Develop a comprehensive foundation in programming languages, systems, architecture, and data in New York Tech’s Bachelor of Science in Computer Science program. Learn more."` (www degree page) |
| `published` | string\|null | the publication date, and only that (4,146 pages): news, media coverage, blog posts, e-mail announcements (`communication`: from the date in their title or file name), magazine articles (the first day of the issue's season: spring 03-01, summer 06-01, fall 09-01, winter 12-01), and most WordPress pages of www.nyit.edu and vancouver.nyit.edu (2,715 of the 3,358 www.nyit.edu records: the date the page was first published). Bios have none (the CMS import date is dropped), and events have `event_start` instead | `"2024-05-28T18:52:20+00:00"` (www degree page), `"2024-09-01"` (a fall 2024 magazine article) |
| `modified` | string\|null | date of the last update, if the site says | `"2026-07-05T18:57:01+00:00"`; `"2026-09-25"` on a libanswers FAQ |
| `event_start` | string\|null | event pages: when the event starts (118 pages). ISO, in the event's local time zone when the site gives it (LiveWhale's `og:start_time`, LibCal's JSON-LD), else UTC | `"2026-11-30T13:00:00-05:00"` |
| `event_end` | string\|null | event pages: when the event ends (`null` for all-day events and events without an end time; set on 104 pages) | `"2026-11-30T14:00:00-05:00"` |
| `noindex` | bool | the page asks search engines not to index it (`<meta name="robots" content="noindex">`); it is kept here but not indexed (81 pages) | `false` |

### sections.jsonl

One `SectionRecord` per passage of the distinct, indexable pages: 29,100 records from 6,764
pages. A page is split at its h2-h6 headings. The text before the first heading is a section
with heading `""`. Sections longer than 450 words are split into parts of about 150-400 words
(`"<heading> (part 2)"`). Empty sections are dropped, but their heading stays in the
`heading_path` of the sections below; a heading that reads like content (a sentence, 6 or more
words, a numbered step) is kept as a line of text instead. A section of fewer than 5 words is
merged into the section before it (or after it, when it is the first): its heading becomes a
line of that section's text (514 merged). A LibGuides librarian profile box is a section of its
own, headed `Librarian: <name>` (or `Library contact: NYIT Library`), with the lines `Name:`,
`Job title:`, `Subjects:`, `Email:`, `Phone:` and `Address:`.

A passage that a website repeats word for word on several of its pages is kept once. The copy
kept is, in this order: not on page 2, 3, ... of a paginated listing; on the page whose title
has the most words of the passage's heading; on a page that is not a home or listing page; on
the shortest page; then the first URL. The post excerpts on a blog's list of posts (passages
whose word 5-shingles are, for 80% or more, in a passage of a post the list links to) are
dropped too. 2,153 passages were removed by these two rules (`repeated_sections_removed` in
parse_summary.json).

| Field | Type | Meaning | Example |
|---|---|---|---|
| `section_id` | string | `<doc_id>-s<NNN>` (NNN = `order`, 3 digits) | `"page-0c112f43a5-s006"` |
| `doc_id` | string | the page (pages.jsonl) it comes from | `"page-0c112f43a5"` |
| `url` | string | page URL, plus `#anchor` when the section starts at an element with an id or name (10,938 sections) | `"https://catalog.nyit.edu/engineering/computer_science#admissions_csbs_ecebs"` |
| `page_title` | string | the page's `title` | `"Computer Science, B.S."` |
| `heading` | string | this section's heading (`""` for the intro: 4,452 sections) | `"Admission Requirements"` |
| `heading_path` | list[string] | enclosing headings, outermost first (`[]` for intros) | `["Admission Requirements", "Application Materials"]` (section s007 of the same page) |
| `text` | string | the section body, without the heading line | `"This program has specific admission requirements in addition to our general requirements.\nFirst Year\n- If the student took the SAT, ..."` |
| `word_count` | int | words in `text` (min 4, median 72, max 464) | `245` |
| `order` | int | 0-based position in the page | `6` |
| `page_type` | string | copied from the page | `"program"` |
| `school` | string\|null | copied from the page | `"College of Engineering and Computing Sciences"` |
| `program` | string\|null | copied from the page | `"Computer Science, B.S."` |
| `degree` | string\|null | copied from the page | `"B.S."` |
| `level` | string\|null | copied from the page | `"undergraduate"` |
| `catalog_year` | string | copied from the page | `"2026-2027"` |
| `breadcrumbs` | list[string] | copied from the page | `["New York Tech Catalog 2026–2027", ...]` |
| `site` | string | website of the page | `"catalog.nyit.edu"` |
| `campuses` | list[string] | copied from the page (non-empty on 14,947 sections) | `["Long Island", "New York City"]` |
| `duplicate_of` | string\|null | the corpus id of the passage that corpus.jsonl holds instead of this one: the same text, or a near-copy, on a more authoritative page about the same program and level (236 sections); `null` otherwise. Such a section has no corpus record of its own | `null` |

The corpus `text` of a section is `page_title` + newline + `heading_path` joined with ` > ` + newline + `text`.

### courses.jsonl and courses.csv

One `CourseRecord` per course of the catalog's course directory (`/courses?cs=<SUBJECT>&cal=<UG|GR|PROF>`):
3,389 records with 3,384 unique codes in 208 subjects. A course listed on several subject pages
appears once; the record with the longest description is kept. `courses.csv` has the same 20
columns in the same order. In the CSV, `attributes` is a JSON string, `null` is an empty cell,
and the file is UTF-8 with a byte-order mark so that Excel shows accents and dashes.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `course_id` | string | `<code>\|<level_code>`. When one level lists different courses under the same code, the next ones get `\|2`, `\|3` (5 cases, e.g. `"MTIM 893\|PROF\|2"` = Cardiology IV). Its corpus id is `course-` + the same with the spaces and bars turned into `-` (`course-CSCI-260-UG`) | `"CSCI 260\|UG"` |
| `code` | string | course code | `"CSCI 260"` |
| `subject` | string | subject code, taken from the course code | `"CSCI"` |
| `subject_name` | string | subject name from the site's subject list | `"Computer Science"` |
| `number` | string | number part, may contain letters (`701B`, `ABRDG`, `AP01`) | `"260"` |
| `title` | string | course title | `"Data Structures"` |
| `credits` | string | credits as printed; merged variants read `"4.0 or 4.5"` (LDB 505) | `"3.0"` |
| `credits_min` | float | lowest credit value | `3.0` |
| `credits_max` | float | highest credit value | `3.0` |
| `school` | string | school **as printed** in the course table (6 values, e.g. `"College of Eng & Comp Sciences"`, `"College of Osteopathic Med"`); corpus.jsonl uses the official names | `"College of Eng & Comp Sciences"` |
| `level` | string | `undergraduate` (1,274), `graduate` (1,095), `professional` (1,020) | `"undergraduate"` |
| `level_code` | string | `UG`, `GR`, `PROF` (the `cal` URL parameter) | `"UG"` |
| `description` | string | course description, without the trailing hours sentence (`""` for 662 courses, mostly College of Osteopathic Medicine) | `"The classic data structures, such as stacks, queues, linked lists, binary trees, etc. are studied. Sorting and searching are stressed. Computational analysis is also studied."` |
| `prerequisites` | string\|null | prerequisite text, with any corequisite part split off | `"CSCI 185."` |
| `corequisites` | string\|null | corequisite text (166 courses) | `null`; `"AAID 140."` for AAID 101 |
| `hours` | string\|null | "classroom - lab/studio - credits" hours | `"3-0-3"` |
| `attributes` | object | every labelled field of the course detail, verbatim. A bare flag gets a readable value (`"Inst Cnst": "Instructor consent required"`, e.g. ARTM 301) | `{"Prerequisite Course(s)": "Prerequisite: CSCI 185.", "Classroom Hours - Laboratory and/or Studio Hours – Course Credits": "3-0-3"}` |
| `text` | string | ready-to-index text: code and title, subject, level, credits, official school, description, prerequisites, corequisites, hours | `"CSCI 260: Data Structures\nComputer Science (CSCI), undergraduate course, 3.0 credits, College of Engineering and Computing Sciences.\n..."` |
| `url` | string | the course listing page the record was scraped from (the corpus record links to the same page with `cc=CSCI-260`) | `"https://catalog.nyit.edu/courses?cal=UG&cs=CSCI"` |
| `catalog_year` | string | catalog edition | `"2026-2027"` |

The 344 courses of the subject "Conversion - OM" (OM 9600-9939, OM AP01-AP56, ...; 0 credits,
no description) are legacy registrar codes. They stay in this file but are left out of corpus.jsonl.

### documents.jsonl

One `DocumentPageRecord` per page of every document linked from a crawled NYIT website: 17,648
pages of 1,682 distinct documents (1,567 PDFs with 16,710 pages, 52 Word files with 124 pages,
60 Excel files with 748 pages and 3 PowerPoint files with 66 slides). Empty pages (1,947, mostly
in the 146 scanned PDFs) are kept with `word_count` 0, and so are the pages of older editions
(`superseded`). The full catalog PDFs are not here; they are in `pdf_pages.jsonl`. Records are
sorted by site, `pdf_url` and page. `docs --max-docs 0` rewrites this file from the files on
disk without any download.

Word, Excel and PowerPoint files in the current formats (`.docx`, `.xlsx`, `.pptx`) have the
same fields; `file_type` tells them apart. Their "pages" are cut by `docs`: about 150 to 400
words of a Word file, cut at a heading or paragraph; the rows of one Excel sheet, at most about
400 words per page (a page after the first repeats the last one-cell line above it, a section
or question, and the leading cells of its row group); one PowerPoint slide per page, in
presentation order. A last page of fewer than 50 words joins the page before it when the two
stay within 500 words. Hidden sheets, Word headers and footers, deleted text (tracked changes),
field codes, the prompts of empty content controls ("Click or tap here to enter text.") and
slide numbers/footers are left out. The legacy binary formats (`.doc`, `.xls`, `.ppt`) are not
read (4 linked files: `not_extracted_files` in documents_summary.json). The field names are
those of the PDF version: `pdf_url` is the document's URL whatever its type.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `doc_id` | string | `doc-<10 hex of sha1(pdf_url)>-p<NNNN>` | `"doc-5460c182eb-p0001"` |
| `site` | string | host of `pdf_url` | `"files.nyit.edu"` |
| `url` | string | `pdf_url` + `#page=N` (opens a PDF at that page; for an Office file the fragment only names the passage) | `"https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20242025.pdf#page=1"` |
| `pdf_url` | string | the URL the document (PDF or Office file; the name is kept for compatibility) is cited with. When a link redirects to another URL of the same kind of file (site.nyit.edu/files/... -> files.nyit.edu/files/...), this is the target | `"https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20242025.pdf"` |
| `title` | string | the document's title: its title metadata if that is a real title (111 documents; Office files: `dc:title` of docProps/core.xml, used only when it matches the document's file name, link text, printed title or first lines), else the title printed in large type on page 1 (Office files: the first paragraph in the Title or a heading style, or the first slide title; template placeholders such as "Course Title" are skipped) (855), else the text of a link to it (569; never a piece of a split link or a screen-reader note such as "This link opens in a new window"), else the file name (147). Junk ("Page 1 of 2", "Name", the university's name, letter-spaced "M A G A Z I N E") and one-word titles are avoided. No two documents of one file type share a title: 361 titles end with a qualifier taken from the edition, another title of the document, the link text or the file name (a PDF and an Excel file may share a title: the corpus title names the format) | `"Student Handbook 2024–2025"`; `"Internship Residency Placement by Specialty (2016)"` |
| `found_on` | list[string] | up to 5 NYIT pages that link to the document | `["https://www.nyit.edu/medicine/college-of-osteopathic-medicine/student-resources/"]` |
| `page` | int | 1-based page number | `1` |
| `page_count` | int | pages in the document | `124` |
| `toc_path` | list[string] | PDF bookmarks covering the page, outermost first (empty on 10,512 pages). Word: the heading path at the start of the page (heading styles and outline levels; a "heading" that is a sentence or a list item is text); Excel: `[sheet name]`, or `[]` for a default name such as "Sheet1"; PowerPoint: `[slide title]` (title placeholder) or `[]` | `["Introduction", "Outcomes"]` |
| `text` | string | the page text: reading order, table cells joined with ` \| `, running headers, footers and page numbers removed, wrapped lines joined, list markers kept (`- item`), form blanks as `___`. `""` when the page has no text layer or its text cannot be decoded. Office files: the same cleaning; Word paragraphs and table rows one per line (cells joined with ` \| `), text boxes after their paragraph; Excel rows `Heading: value \| Heading: value` under the column headings found in the sheet (not simply its first row), else `a \| b \| c` (empty cells and rows left out; shared and inline strings, numbers as Excel shows them by default, `TRUE`/`FALSE`, cached formula results; dates and times in ISO form and percentages as `33%` when the cell has such a format); PowerPoint: the slide title, then the text of its shapes, groups and tables, top to bottom and then left to right | `"Introduction\nUpdate Effective June 1, 2024\nThe information contained in this handbook is subject to change ..."` |
| `word_count` | int | words in `text` (median 230, max 4,231) | `290` |
| `file` | string | the downloaded file under `data/raw/docs/`, named after the URL that was downloaded, so its host folder can differ from `site` | `"www.nyit.edu/nyitcom_studenthandbook20242025__4564682643.pdf"` |
| `file_type` | string | `pdf` (16,710 pages), `docx` (Word, 124), `xlsx` (Excel, 748) or `pptx` (PowerPoint, 66) | `"pdf"` |
| `modified` | string\|null | the PDF's modification date (creation date if missing; Office files: `dcterms:modified` of docProps/core.xml), else the HTTP `Last-Modified` | `"2025-06-02T15:48:11-04:00"` |
| `doc_category` | string | what kind of document it is (see the corpus metadata above for the 13 values); words that say what a document is (form, template, affidavit, checklist, survey, frequency report, ...) decide before the `/policies/` folder; pages per category: report 4,627, policy_handbook 2,256, event_program 1,719, schedule_calendar 1,527, financial_filing 1,447, course_material 1,365, other 1,281, newsletter_magazine 957, training 748, brochure 625, form 587, transfer_agreement 259, semester_map 250 | `"policy_handbook"` |
| `edition` | string\|null | the year, term or date the file name gives (812 documents) | `"2024-25"` |
| `doc_year` | int\|null | the last year the edition covers, else the year the file was modified | `2025` |
| `series` | string\|null | when several editions of the same document were downloaded: the file name without its year/term/date (125 series, 564 documents) | `"files.nyit.edu nyitcom student handbook"` |
| `superseded` | bool | an older edition whose period is over, while a newer edition of the series exists (241 documents: 240 PDFs and 1 Excel file; 5,594 pages). Left out of the corpus unless `parse --corpus-with-superseded` | `true` |
| `superseded_by` | string\|null | `pdf_url` of the newest edition of the series | `"https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20252026.pdf"` |
| `cohort_year` | int\|null | semester maps and class calendars: the entering class they are for (263 PDFs). They are never superseded | `null` |
| `scanned` | bool | most pages of the PDF are images without a text layer (146 PDFs); always `false` for Office files | `false` |

### pdf_pages.jsonl

One `PdfPageRecord` per page of the full catalog PDFs in `data/raw/pdf/`: 1,297 pages of 6 PDFs.
Every PDF present is included, whichever editions the last `pdf` run asked for.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `doc_id` | string | `pdf-<edition>-<catalog>-p<NNNN>` | `"pdf-2025-2026-undergraduate-p0120"` |
| `edition` | string | `2025-2026` or `2026-2027` in this dataset | `"2025-2026"` |
| `catalog` | string | `undergraduate`, `graduate`, `osteopathic` | `"undergraduate"` |
| `page` | int | 1-based page number | `120` |
| `page_count` | int | pages in this PDF | `318` |
| `url` | string | PDF URL + `#page=N` | `"https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf#page=120"` |
| `toc_path` | list[string] | section path of the page. It comes from the PDF bookmarks for 2025-2026; the 2026-2027 PDFs have no bookmarks, so the path is rebuilt from their printed headings | `["Policies and Rules", "Financial Aid: Undergraduate Students", "Transfer of Post-9/11 GI BILL Benefits to Dependents"]` |
| `section` | string\|null | deepest entry of `toc_path` | `"Transfer of Post-9/11 GI BILL Benefits to Dependents"` |
| `text` | string | page text (no page in this dataset is empty) | `"Financial Aid Undergraduate\nTransfer of Post-9/11 GI BILL® Benefits to Dependents\n..."` |
| `word_count` | int | words after cleaning | `699` |
| `pdf_file` | string | file name under `data/raw/pdf/` | `"NYITUndergraduateCatalog_AY2526.pdf"` |

### Page types

`page_type` in pages.jsonl, sections.jsonl and the section metadata of corpus.jsonl. The counts
below are distinct pages.

| Value | Meaning | Distinct pages | Main websites |
|---|---|---:|---|
| `page` | an ordinary information page (offices, admissions, student life, ...) | 1,436 | www 1,207, comapp 92, vancouver 69 |
| `blog_post` | blog post | 1,170 | blogs 1,128, online 42 |
| `library_guide` | library research guide page | 1,089 | libguides |
| `news` | news article | 726 | www |
| `media_coverage` | short note about NYIT in the media | 553 | www |
| `listing` | hub page that is mostly a list of links (page 2, 3, ... of a list of posts gets no sections: 288 pages) | 438 | blogs 308, www 101, comapp 8, magazine 8 |
| `bio` | faculty or staff profile | 422 | www |
| `faq` | library FAQ answer | 287 | libanswers |
| `degree` | degree or program page outside the catalog (`/academics/degrees/...`) | 272 | www 247, vancouver 20, online 5 |
| `curriculum` | requirement tables or semester maps | 268 | catalog 252, www 14, online 2 |
| `course_listing` | catalog course listing `/courses?cs=...&cal=...` (gets no sections; its courses are in courses.jsonl) | 258 | catalog |
| `policy` | policy or handbook | 214 | apps 204, catalog 10 |
| `event` | event page | 114 | events 102, libcal 12 |
| `program` | catalog program page (degree, minor, certificate) | 91 | catalog |
| `financial_aid` | catalog financial aid pages | 73 | catalog |
| `magazine_article` | magazine article | 50 | magazine |
| `general_information` | catalog general information | 20 | catalog |
| `expert_insight` | expert commentary | 14 | www |
| `school` | school or college landing page | 13 | www 7, catalog 6 |
| `program_list` | lists of programs | 13 | catalog |
| `communication` | an e-mail announcement to the university saved as a web page (files.nyit.edu/files/...html), dated by `published` | 13 | files |
| `home` | a site's homepage | 12 | one per site |
| `tuition` | catalog tuition pages | 11 | catalog |
| `other` | none of the above: 6 event pages that the calendar no longer shows (it serves its home page instead; no text, no passages) and 3 catalog pages of the medical school (clerkships, continuing medical education, osteopathic oath) | 9 | events 6, catalog 3 |
| `student_services` | catalog student services | 8 | catalog |
| `help` | catalog help pages | 5 | catalog |
| `faculty` | catalog faculty and administration listings | 4 | catalog |
| `course_index` | the catalog's course index `/courses` (its sections are not in the corpus) | 1 | catalog |
| `library_hours` | the libraries' weekly opening hours (libcal.nyit.edu/hours): a snapshot of the crawl date, which its title names | 1 | libcal |

29 types in pages.jsonl; the corpus has the 27 other than `course_index` and `course_listing`.

### parse_summary.json

Written by `parse`. It describes what the run did.

| Key | Type | Meaning | Value in this dataset |
|---|---|---|---|
| `generated_at` | string | when `parse` finished (UTC) | `"2026-11-23T21:13:41+00:00"` |
| `sites` | list[string] | websites with page or corpus records, catalog and www first | 19 hosts (nyit.edu only has documents) |
| `catalog_year` | string | the catalog edition found on the pages | `"2026-2027"` |
| `corpus_with_pdf` / `corpus_with_docs` | bool | the corpus options of this run | `false` / `true` |
| `corpus_with_superseded` / `corpus_with_near_duplicates` | bool | the other two corpus options | `false` / `false` |
| `cached_pages` | int | manifest entries selected for parsing | `8332` |
| `pages` / `distinct_pages` / `duplicate_pages` | int | all page records, distinct pages, exact copies | `8231` / `7585` / `646` |
| `noindex_pages` | int | pages marked noindex (all records) | `81` |
| `too_short_to_index` | int | distinct, indexable pages with fewer than 5 words | `167` |
| `listing_pages_not_indexed` | int | page 2, 3, ... of lists of posts (`?p=N`, only when it is the URL's one query parameter), kept without sections | `288` |
| `pages_shared_by_levels` | int | catalog pages published identically under two levels, whose level was set to `null` | `5` |
| `schools_filled_from_program` | int | catalog pages that got their school from the program's other pages | `363` |
| `degree_page_schools_filled` | int | degree pages of other sites that got the school of their degree | `138` |
| `pages_by_type` | object | page type -> distinct pages | `{"page": 1436, "blog_post": 1170, "library_guide": 1089, ...}` |
| `sections` | int | sections written | `29100` |
| `tiny_sections_merged` | int | sections of fewer than 5 words merged into a neighbour | `514` |
| `repeated_sections_removed` | object | site -> passages removed because another page of the site repeats them word for word, plus the post excerpts on a blog's list of posts (`pipeline.listing_excerpts`) | `{"www.nyit.edu": 676, "apps.nyit.edu": 580, "blogs.nyit.edu": 62, ..., "libguides.nyit.edu": 795, ...}` (2,153 in total) |
| `sections_left_out_as_near_duplicates` | int | sections with `duplicate_of` (no corpus record of their own) | `236` |
| `course_listing_pages` | int | course listing pages parsed for courses | `267` |
| `empty_course_listings` | list[string] | listing pages without any course | 8 URLs, e.g. `"https://catalog.nyit.edu/courses?cal=GR&cs=ARCHB"` |
| `courses_parsed` / `courses` / `duplicate_courses_removed` | int | course rows found, kept, merged | `3856` / `3389` / `467` |
| `course_subjects` | int | distinct subject codes | `208` |
| `courses_not_in_corpus` | int | legacy "Conversion" codes | `344` |
| `course_summary` | object | `records`, `unique_course_ids`, `unique_codes`, `unique_subjects`, `per_level` (records/codes/subjects per level), `with_description_pct` (80.5), `with_prerequisites_pct` (34.9), `with_corequisites_pct` (4.9), `with_hours_pct` (39.1), `labels` (label -> count), `schools` (printed school -> count) | |
| `document_pages_read` | int | records read from documents.jsonl | `17648` |
| `document_pages_repeated` | int | document pages that another document repeats word for word | `772` |
| `document_pages_superseded` | int | pages of older editions | `5594` |
| `document_pages_left_out` | object | reason -> document pages not in the corpus: `empty`, `too_short` (fewer than 5 words), `repeated`, `superseded`, `site_not_crawled`; near-duplicates are counted under `near_duplicates` | `{"empty": 1947, "repeated": 713, "superseded": 4780, "too_short": 262}` |
| `documents_in_corpus` / `documents_in_corpus_pdfs` | int | document records in the corpus, and from how many documents (PDF or Office files) | `9822` / `1289` |
| `corpus` | int | corpus records | `41725` |
| `corpus_by_source` | object | source -> records | `{"section": 28858, "course": 3045, "document": 9822}` |
| `corpus_by_site` | object | site -> records (all sources) | `{"www.nyit.edu": 17150, "files.nyit.edu": 8280, "catalog.nyit.edu": 4893, ...}` |
| `corpus_id_clashes` | int | records dropped because their id was taken | `0` |
| `near_duplicates` | object | the near-duplicate collapse: `enabled`, `threshold` (0.9), `shingle_words` (5), `min_words` (8), `groups` (1,014), `records_in_groups` (2,877), `left_out` (360), `left_out_by_source` (section 236, document 124), `left_out_by_site`, `kept_with_dup_group` (2,207), `dup_groups_in_corpus` (704), `left_out_ids` (source -> ids of the document and course records left out; sections keep `duplicate_of`), `examples`, `seconds` | |
| `courses_csv_written` | bool | `false` if courses.csv could not be written (open in Excel) | `true` |
| `by_site` | object | site -> `pages`, `distinct_pages`, `duplicate_pages`, `noindex_pages`, `words`, `sections`, `section_words`, `repeated_sections_removed`, `corpus_records` (every source, like STATS.md), `corpus_by_source`, `failures`, `boilerplate_blocks`, `seconds`, `page_types` | libanswers: `{"pages": 287, "distinct_pages": 287, ..., "sections": 298, "corpus_records": 297, "corpus_by_source": {"section": 297}, ...}` |
| `boilerplate_blocks` | object | site -> number of repeated blocks removed from its pages | `{"biggive.nyit.edu": 2, "blogs.nyit.edu": 1, "online.nyit.edu": 1, ...}` (0 elsewhere) |
| `boilerplate_examples` | object | site -> up to 8 of those blocks | |
| `noindex` | list[string] | URLs of the noindex pages | 81 URLs |
| `failures` | list[object] | pages that could not be parsed: `url`, `file`, `site`, `stage`, `error` | `[]` |
| `skipped` | object | reason -> URLs not parsed: `missing_cache_file` (0), `offsite_redirect` (0), `out_of_scope` (317: 310 pages of hosts disabled after the crawl, 5 eportfolios software pages, online.nyit.edu's `/privacy-policy` and `/cookies`), `same_page_as_other_url` (28 URL aliases), `not_found_page` (101 error pages served with HTTP 200: 59 empty catalog program templates, 39 blog "page not found" pages, 3 test pages) | |
| `timings_seconds` | object | `boilerplate`, `pages`, `courses`, `corpus`, `near_duplicates`, `write`, `total` | total `516.18` |

### documents_summary.json

Written by `docs`. The `downloads` block describes the **last** run. In this dataset the last
run was `docs --max-docs 0` (re-extraction only, no network); the two download runs are in
`data/logs/docs.log` (1,595 files after the main crawl) and `data/logs/docs_gapfill.log` (147
more after the catch-up crawl).

| Key | Type | Meaning | Value in this dataset |
|---|---|---|---|
| `generated_at` | string | when `docs` finished (UTC) | `"2026-11-23T21:05:04+00:00"` |
| `seconds` | float | run time | `120.8` |
| `settings` | object | `max_docs`, `max_mb`, `refresh`, `delay` | `{"max_docs": 0, "max_mb": 40.0, "refresh": false, "delay": 1.0}` |
| `input` | object | `pdf_links` (PDF links found by the crawler), `sitemap_pdf_urls`, `manifest_pdf_urls` (URLs without `.pdf` that the crawl manifest records as a PDF), `office_links` (links to .docx/.xlsx/.pptx files, kind `asset`), `sitemap_office_urls`, `candidates` (PDFs and Office files on crawled NYIT sites), `candidates_by_type` (`pdf`, `docx`, `xlsx`, `pptx`), `candidates_by_site` | `2106`, `0`, `1`, `117`, `0`, `1767`, `{"pdf": 1650, "docx": 53, "xlsx": 61, "pptx": 3}`, `{"files.nyit.edu": 1231, "www.nyit.edu": 346, ...}` |
| `skipped` | object | reason -> PDF and Office links not downloaded: `catalog_pdf` (left to `pdf`), `other_website`, `site_not_crawled`, `max_docs` (left for a later run); also `invalid_url`, `noindex`, `over_max_mb` when they occur | `{"catalog_pdf": 18, "other_website": 409, "site_not_crawled": 29}` |
| `skipped_examples` | object | reason -> up to 10 example URLs | |
| `downloads` | object | `to_download`, `attempted`, `postponed`, `cached`, `downloaded`, `failed`, `bytes`, `network_requests` of this run | `{"to_download": 0, "attempted": 0, "postponed": 0, "cached": 1742, "downloaded": 0, "failed": 0, "bytes": 0, "network_requests": 0}` |
| `status_counts` | object | download status -> candidates (the statuses of docs_manifest.jsonl, among them `not_office` and `encrypted` for Office files; `not_downloaded_yet`: candidates without a download attempt yet) | `{"ok": 1742, "not_found": 20, "too_large": 5}` |
| `totals` | object | `documents` (1682: all file types), `pdfs` (1567: PDFs only), `by_file_type` (file type -> `documents`, `pages`, `words`: `{"pdf": {"documents": 1567, "pages": 16710, "words": 4080192}, "docx": {"documents": 52, "pages": 124, "words": 33560}, "xlsx": {"documents": 60, "pages": 748, "words": 267207}, "pptx": {"documents": 3, "pages": 66, "words": 2842}}`), `links` (1742), `duplicate_links` (60), `pages` (17648), `words` (4383801), `empty_pages` (1947), `image_only_pages` (1831), `unreadable_pages` (119), `scanned_pdfs` (146), `pdfs_with_bookmarks` (248) (both count PDFs only), `mb` (1802.4, distinct files), `failed` (25) | |
| `by_site` | object | site -> `documents`, `pdfs` (PDFs only), `pages`, `words`, `empty_pages`, `mb` | `{"www.nyit.edu": {"documents": 110, "pdfs": 106, "pages": 1759, "words": 525610, "empty_pages": 106, "mb": 139.43}, ...}` |
| `titles` | object | `with_qualifier` (361 titles told apart by edition, another title, link text or file name), `pdfs_sharing_a_title` (0: documents that share a title with another document of the same file type; the name dates from when only PDFs were read), `shared_titles` (0: such titles), `by_source` (title source -> documents: `printed_title` 855, `link_text` 569, `file_name` 147, `metadata` 111) | |
| `editions` | object | `reference_date` (the download date used to decide what is over), `series` (125), `documents_in_series` (564), `superseded_documents` (241), `superseded_pages` (5594), `cohort_documents` (263), `with_edition` (812) | |
| `categories` | object | category -> `documents`, `pdfs` (PDFs only), `pages` | `{"form": {"documents": 244, "pdfs": 204, "pages": 587}, "report": {"documents": 243, "pdfs": 239, "pages": 4627}, ...}` |
| `scanned_documents` | list[object] | the 146 scanned PDFs: `pdf_url`, `title`, `pages` (candidates for OCR) | |
| `not_extracted_files` | object | Word/Excel/PowerPoint files linked from crawled NYIT pages whose text is not in documents.jsonl: `files`, `by_type` (extension -> files), `by_reason` (reason -> files), `examples` (up to 10 URLs) and `details` (every file: `url`, `type`, `reason`). Reasons: `legacy_binary_format` (.doc, .xls, .ppt: not read), `unsupported_format` (.rtf, .odt), a download status of docs_manifest.jsonl (`not_found`, `not_office`, `encrypted`, `too_large`, `http_error`, ...), `extract_error` (downloaded but could not be read), `noindex`, `over_max_mb`, `max_docs` (left for a later run), `not_downloaded_yet` | `files` 4, `by_type` `{".doc": 3, ".xls": 1}`, `by_reason` `{"legacy_binary_format": 4}` |
| `records` | int | lines written to documents.jsonl | `17648` |
| `documents` | list[object] | one entry per distinct document (below) | 1,682 entries |
| `failures` | list[object] | documents not extracted: `url`, `site`, `file_type`, `status` (a download status or `extract_error`: a damaged file, unsafe XML, a part over the size limits), `http_status`, `error`, `found_on` | 25 entries (20 `not_found`, 5 `too_large`; all PDFs) |
| `pymupdf_version` | string | PyMuPDF version used | `"2.1"` |

Each entry of `documents`:

| Key | Type | Meaning | Example |
|---|---|---|---|
| `pdf_url` | string | URL the document is cited with | `"https://aaupatnyit.nyit.edu/wp-content/uploads/sites/3/2026/10/AAUP_DLClassroomTaskForceReportandRecommendationsJuly2018.pdf"` |
| `file_type` | string | `pdf`, `docx`, `xlsx` or `pptx` | `"pdf"` |
| `site` | string | host of `pdf_url` | `"aaupatnyit.nyit.edu"` |
| `status` | string | always `ok` | `"ok"` |
| `title` / `title_source` | string | chosen title and where it came from: `metadata`, `printed_title` (for Office files: the first heading or slide title), `link_text`, `file_name` | `"DL task force report, June 2018 (v2)"` / `"metadata"` |
| `title_qualifier` | string | only on the 361 documents whose title needed one: the part added in parentheses so that no two documents of one file type share a title (an edition, another title of the document, link text, the distinctive words of the file name or the file name) | `"COAS"` (one of the fall 2026 class schedules) |
| `doc_category`, `edition`, `doc_year`, `series`, `superseded`, `superseded_by`, `cohort_year` | | as in documents.jsonl | `"report"`, `"2018-07"`, `2018`, `null`, `false`, `null`, `null` |
| `pages`, `words`, `empty_pages`, `image_only_pages`, `unreadable_pages` | int | page and word counts; empty pages, empty pages that hold only images, and pages whose text layer did not decode to words | `6`, `2560`, `0`, `0`, `0` |
| `scanned` | bool | most pages are images without text (PDFs only) | `false` |
| `toc_entries` | int | bookmarks in the PDF; Office files: headings (Word), sheets read (Excel), slides with a title (PowerPoint) | `0` |
| `modified` | string\|null | modification date | `"2018-10-10T15:01:47-04:00"` |
| `bytes`, `sha1`, `file` | int, string, string | size, SHA-1 and path under `data/raw/docs/` | `129637`, `"58178db0..."`, `"aaupatnyit.nyit.edu/aaup_dlclassroomtaskforcereportandrecommendationsjuly2018__6076d48a8a.pdf"` |
| `links` / `aliases` | int / list[string] | how many downloaded URLs had this exact file, and the other URLs | `1` / `[]` |
| `found_on` | list[string] | pages linking to it (max 5) | `["https://aaupatnyit.nyit.edu/"]` |
| `metadata_title`, `printed_title`, `producer` | string\|null | raw title metadata, title printed on page 1 (Office files: first heading or slide title), producing software (Office files: `Application` of docProps/app.xml) | `"DL task force report, June 2018 (v2)"`, `"DL Classroom Task Force Report and Recommendations"`, `"Mac OS X 10.13.6 Quartz PDFContext"` |

### pdf_summary.json

Written by `pdf`.

| Key | Type | Meaning | Value in this dataset |
|---|---|---|---|
| `generated_at` | string | when `pdf` finished (UTC) | `"2026-11-23T15:14:11+00:00"` |
| `seconds` | float | run time | `3.7` |
| `requested` | object | `editions` and `kinds` asked for | `{"editions": ["2025-2026", "2026-2027"], "kinds": ["undergraduate", "graduate", "osteopathic"]}` |
| `downloads` | list[object] | one per requested PDF: `edition`, `catalog`, `url`, `file`, `status` (`cached`, `downloaded`, `failed`), `bytes`, `error` | 6 entries |
| `pdfs` | list[object] | one per extracted PDF: `edition`, `catalog`, `file`, `url`, `pages`, `words_clean`, `words_raw` (PyMuPDF's plain text, the proposal's method), `empty_pages`, `empty_page_numbers`, `toc_entries`, `toc_source` (`bookmarks` or `headings`), `outline_entries`, `header_footer_lines_removed`, `pdf_title`, `bytes`, `sha1` | 2025-2026 undergraduate: 318 pages, 131,389 clean / 131,350 raw words |
| `by_edition` | object | edition -> `pdfs`, `pages`, `words_clean`, `words_raw`, `empty_pages` | `{"2025-2026": {"pdfs": 3, "pages": 642, ...}, "2026-2027": {"pdfs": 3, "pages": 655, ...}}` |
| `totals` | object | the same for all PDFs | `{"pdfs": 6, "pages": 1297, "words_clean": 544847, "words_raw": 544822, "empty_pages": 0}` |
| `records` | int | lines in pdf_pages.jsonl | `1297` |
| `pymupdf_version` | string | PyMuPDF version | `"2.1"` |
| `failed` | list[object] | PDFs that could not be downloaded or read | `[]` |

### stats.json and STATS.md

Written by `stats`. `STATS.md` is the human-readable report, in Markdown so you can paste it into
the project report: an overview, "At a glance", websites, proposal vs. scraped, crawl, web pages,
sections, courses, catalog PDFs, documents from the websites (PDF, Word, Excel, PowerPoint),
search corpus, and how the numbers were measured.
`stats.json` holds every number behind it. Each of the objects `crawl`, `pages`, `sections`,
`courses`, `pdf`, `documents`, `corpus` and `parse` also has `available`: `false` when its input
file does not exist yet (the object then holds no numbers, and STATS.md shows `n/a`).

| Key | Type | Contents |
|---|---|---|
| `generated_at` | string | when `stats` ran (UTC) |
| `data_dir` | string | the data folder that was analysed |
| `inputs` | object | which input files existed (`true`/`false`; `sitemap_lists` = number of sitemap files) |
| `crawl` | object | `urls` (11,864), `pages_ok` (8,677), `status_counts`, `html_megabytes` (770.0), `parsed_html_megabytes` (740.4), `first_fetch` / `last_fetch`, `summary` (from crawl_summary.json), `summary_format`, `finished`, `newer_crawl`, `sites_in_last_run` (the websites of `last_run`), `runs` (every crawl run: `finished_at`, `seconds`, `sites`, `urls_visited` (`null` for the interrupted run), `network_requests`, `interrupted`; 5 runs), `failed_sites`, `unreachable_reasons`, `dropped_query_self_links`, `sites` (per host: `site`, `name`, `urls`, `ok`, `statuses`, `html_megabytes`, `content_on_other_site`, sitemap coverage), `hosts_with_saved_urls` (26), `hosts_not_parsed` (hosts with saved pages that `sites.py` disables now: 6), `sites_with_pages` (18), `content_on_other_site` (182), `capped_sites`, `given_up_sites`, `unreachable_sites`, `discovered_sites`, `linked_but_not_crawled_sites` |
| `pages` | object | `records`, `distinct`, `duplicates`, `noindex` (distinct pages: 72), `noindex_records` (all records: 81), `too_short_to_index`, `sites`, `words` (count/total/min/median/mean/p90/max), `words_outside_course_directory`, `tables`, `links`, `catalog_pages`, `catalog_year`, `catalog_years`, `by_type`, `by_school`, `by_school_catalog`, `by_level`, `by_level_catalog`, `schools`, `other_school_names`, `campuses`, `program_pages`, `curriculum_pages`, `programs` (127), `program_pages_by_degree`, `degree_pages` (272), `degree_pages_by_degree`, `dated_pages` (4,114 distinct pages with `published`), `first_published`, `last_published`, `events_with_time` (118), `first_event`, `last_event` |
| `sections` | object | `count`, `pages_with_sections`, `words`, `length_buckets`, `with_anchor_link`, `over_1000_words`, `repeated_text`, `repeated_text_by_site`, `by_site` |
| `courses` | object | `records`, `unique_codes`, `unique_subjects`, `ug_gr_unique_codes` (2,369), `ug_gr_unique_subjects` (159), description and prerequisite shares, `description_words`, `by_level`, `largest_subjects` |
| `pdf` | object | `catalogs` (one row per catalog PDF), `total_pages`, `proposal_edition` (the 2025-2026 UG + GR check: 581 pages, 249,274 raw words) |
| `documents` | object | `links_found`, `office_links_found`, `nyit_pdfs`, `not_downloaded`, `pdf_links` (URLs in docs_manifest.jsonl, all file types: 1,767 download attempts), `statuses`, `downloaded`, `unique_files`, `aliases`, `megabytes`, `documents` (all file types), `pdfs` (PDFs only), `by_file_type` (file type -> `documents`, `pages`, `words`), `pages`, `words`, `empty_pages`, `scanned_pdfs`, `with_bookmarks`, `pages_per_pdf` (these three for PDFs only), `by_site`, `largest`, `summary_items`, `pages_by_category`, `superseded_pdfs` (241 documents), `superseded_pages` (5,594), `cohort_pdfs` (263), `series` (125), `titles` (as in documents_summary.json), `not_extracted_files` |
| `corpus` | object | `records`, `words`, `by_source` (records and word statistics per source), `by_site` (records per site and source), `contacts` (contact details in the text: `records_with_email` 2,696, `distinct_emails` 1,602, `distinct_nyit_emails` 1,219, `records_with_email_by_source` (section 2,122, document 574), `records_with_phone` 1,485), `near_duplicates_collapsed`, `near_duplicates_left_out` (360), `near_duplicates_left_out_by_source`, `dup_groups` (704), `records_in_dup_groups` (2,207), `document_pages_left_out`, `tiny_sections_merged` (514), `corpus_with_superseded` |
| `parse` | object | `failures`, `seconds`, `corpus_with_docs`, `corpus_with_pdf` |
| `sites` | list[object] | per website: `site`, `name`, `saved`, `pages`, `distinct`, `duplicates`, `noindex`, `words`, `sections`, `corpus_records` (all sources), `page_types`, `boilerplate_blocks`, `median_page_words`, `median_section_words` |
| `schools`, `campuses` | list[string] | the 6 schools and 4 campuses found |
| `proposal_vs_scraped` | list[object] | the comparison table: `metric`, `target_key`, `proposal`, `proposal_shown`, `scraped`, `difference`, `note` |

### validation_report.json

Written by `validate`.

| Key | Type | Meaning | Value in this dataset |
|---|---|---|---|
| `generated_at` | string | when `validate` ran (UTC) | `"2026-11-23T21:40:31+00:00"` |
| `data_dir` | string | the data folder that was checked | |
| `passed` | bool | `true` when no check FAILed | `true` |
| `counts` | object | checks per status | `{"PASS": 39, "WARN": 7, "FAIL": 0, "SKIP": 0}` (on a copy without `data/raw/html`: 37 / 7 / 0 / 2: the course deep links and the section anchors cannot be checked) |
| `checks` | list[object] | the 46 checks in report order (below) | |

Each check:

| Key | Type | Meaning | Example |
|---|---|---|---|
| `name` | string | check name | `"Crawl errors"` |
| `status` | string | `PASS`, `WARN`, `FAIL`, `SKIP` (input missing) | `"WARN"` |
| `message` | string | one-paragraph result | `"1,928 URLs could not be fetched (www 1,678 (HTTP 404 1,662, redirect loop 16); ..."` |
| `count` | int | number of offending items (0 when none) | `1928` |
| `examples` | list[string] | up to 10 offending items | `["https://catalog.nyit.edu/administration_and_faculty/faculty  (HTTP 500, known NYIT-side problem)", ...]` |
| `group` | string | `Crawl`, `Pages`, `Catalog`, `Passages (sections)`, `Text hygiene`, `Catalog PDFs`, `PDF documents`, `Corpus` | `"Crawl"` |

---

## data/raw

### crawl_manifest.jsonl

One JSON line per URL the crawler visited (11,864 URLs, over all five runs). The crawler appends
a line after every download, which is what makes a crawl resumable. When a URL appears on
several lines, **the last line wins**; a finished crawl rewrites the file with one line per URL,
keeping every earlier entry it did not visit (only a complete `crawl --refresh` of every website
drops the entries of pages that are no longer linked). Example: the CS B.S. program page.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `url` | string | requested URL (normalised) | `"https://catalog.nyit.edu/engineering/computer_science"` |
| `site` | string | host of `url` | `"catalog.nyit.edu"` |
| `final_url` | string | URL after redirects (normalised) | `"https://catalog.nyit.edu/engineering/computer_science"` |
| `status` | string | outcome (table below) | `"ok"` |
| `http_status` | int | HTTP status code (0 = no response) | `200` |
| `content_type` | string | `Content-Type` header (`""` on 15 entries: no response) | `"text/html; charset=UTF-8"` |
| `file` | string\|null | cache file under `data/raw/html/`; set for `ok` pages and for redirects that turned out to be duplicates | `"catalog.nyit.edu/engineering__computer_science__0c112f43a5.html"` |
| `redirects` | int | number of redirect hops (only on entries written by the current crawler, when the URL redirected: 1,321 entries) | `1` |
| `bytes` | int | size of the response body | `93845` |
| `sha1` | string\|null | SHA-1 of the saved HTML | `"df89ae0667f4bcf1a237f00c6da9ce0549f6682d"` |
| `title` | string\|null | the page's `<title>` (`null` when no HTML page was received) | `"Computer Science, B.S. \| College of Engineering and Computing Sciences \| \nNew York Tech Catalog 2026–2027"` |
| `noindex` | bool | `<meta name="robots">` says noindex (absent on 7 entries written by an earlier crawler version) | `false` |
| `nofollow` | bool | `<meta name="robots">` says nofollow; links of such pages are not followed (absent on 7 entries) | `false` |
| `canonical` | string\|null | `<link rel="canonical">` (absent on 7 entries) | `null` |
| `depth` | int | link distance from a start page or sitemap (0 = start page or sitemap URL) | `2` |
| `referrer` | string\|null | the page where the URL was found, `"sitemap"`, or `null` for start pages | `"https://catalog.nyit.edu/engineering"` |
| `fetched_at` | string | download time (UTC); the current crawler records it after the politeness wait, when the answer came | `"2026-11-23T07:14:57+00:00"` |
| `elapsed` | float | seconds spent, including retries | `1.148` |
| `error` | string | error message, `""` if none | `""`; with the current crawler e.g. `"redirect not followed (redirect loop): ..."`, `"redirect not followed (more than 10 redirects): ..."`, `"redirect not followed (outside the crawl scope): ..."`, `"website unreachable (could not read its robots.txt: ...)"`, `"could not save the page ..."` (117 `offsite_redirect` entries of the main crawl have `""`) |
| `from_cache` | bool | `true` when the last run read this page from the cache instead of downloading it | `true` |

`status` values:

| Status | Meaning | URLs |
|---|---|---:|
| `ok` | real page, saved and parsed | 8,677 |
| `duplicate` | redirects to a page that was already saved | 1,106 |
| `not_found` | HTTP 404 or 410 (broken link on an NYIT page) | 1,879 |
| `offsite_redirect` | redirects to a login portal, a skipped host or another organisation (the current crawler records it without requesting the target) | 125 |
| `redirect_loop` | the URL redirects in a circle or more than 10 times (current crawler; final, not retried). The main crawl had recorded these 19 URLs as `error` (TooManyRedirects) | 19 |
| `http_error` | other HTTP error after retries (e.g. HTTP 500 on NYIT's side: 7 catalog faculty pages) | 16 |
| `error` | network error after all retries (in this crawl: URLs on 7 hosts that could not be reached); also a page that could not be saved to disk | 14 |
| `non_html` | the link was not an HTML page | 14 |
| `soft404` | "page not found" served with HTTP 200 (catalog: empty template), a web-server default page, a script-only shell or an application error page | 13 |
| `robots_disallowed` | robots.txt forbids it (or forbids the redirect target) | 1 |
| `login_page` | a page with a password field, or a "Log in" / "Sign in" page with hardly any text | 0 |

Statuses that are final are read from the cache by the next run: `ok`, `duplicate`,
`not_found`, `non_html`, `offsite_redirect`, `soft404`, `login_page`, `redirect_loop`. `error`
is retried; `http_error` with HTTP 5xx gets one attempt per run.

### crawl_summary.json

Written when a crawl run ends. A full crawl that reuses the cache is appended to the run history
of the earlier summary: `runs` lists every run whose pages are in the cache, oldest first, and
`seconds` and `network_requests` add them up; only a full `crawl --refresh` starts a new
history. After a run restricted to some websites (`crawl --site X`) the summary is **merged**
with the earlier one: `sites`, `status_by_site` and the totals cover every website crawled so
far, the status counts of the websites of that run are recounted from the merged manifest, and
`last_run` describes the restricted run. The included summary covers five runs: the main crawl
of 14 websites, the LibCal run, the catch-up crawl of all 16 websites (interrupted once, then
resumed: two runs) and a run for www.nyit.edu only.

| Key | Type | Meaning | Value in this dataset |
|---|---|---|---|
| `finished_at` | string | end of the last run (UTC) | `"2026-11-23T20:25:42+00:00"` |
| `seconds` | float | run time, summed over `runs` | `18892.6` (5 h 14 min) |
| `interrupted` | bool | the last run was stopped with Ctrl+C | `false` |
| `sites` | list[string] | profiled websites crawled (in any run) | 16 hosts |
| `urls_seen` / `urls_visited` / `still_queued` | int | URLs discovered, visited, left in the queue. After a `--site` run, `urls_visited` counts the URLs of the merged status counts and `urls_seen` adds that run's discoveries to the other websites' URLs, so it can be lower | `10325` / `11527` / `0` |
| `still_queued_by_site` | object | host -> URLs left in the queue | `{}` |
| `network_requests` | int | HTTP requests made (cached pages need none), summed over `runs` | `13566` |
| `status_counts` | object | status -> URLs (the crawl manifest has more: it also keeps earlier entries that the later runs did not visit, mostly the 310 pages of hosts that `sites.py` disables now) | `{"ok": 8359, "offsite_redirect": 125, "not_found": 1873, ...}` |
| `status_by_site` | object | host -> status -> URLs | `{"catalog.nyit.edu": {"ok": 983, "http_error": 7, "not_found": 2, "soft404": 9}, ...}` |
| `sitemap_pages_by_site` | object | host -> URLs listed in its sitemaps | `{"www.nyit.edu": 3333, "libanswers.nyit.edu": 288, "libcal.nyit.edu": 23, ...}` |
| `capped_sites` | object | host -> URLs skipped because the page limit was reached | `{}` (libguides.nyit.edu saved 1,321 pages under its limit of 1,500) |
| `page_limits` | object | host -> the limit each capped website reached | `{}` |
| `given_up_sites` | object | host -> URLs skipped after repeated failures | `{"iris.nyit.edu": 3}` |
| `unreachable_sites` | list[string] | hosts without a connection | `["bearcat.nyit.edu", "camstream.nyit.edu", "hub.nyit.edu", "iris.nyit.edu", "nyitonline.nyit.edu", "tbls.nyit.edu", "www.imodules.nyit.edu"]` |
| `unreachable_reasons` | object | host -> why its robots.txt could not be read | `{"bearcat.nyit.edu": "ConnectionError: ... Failed to resolve 'bearcat.nyit.edu' ...", ...}` (7 hosts: unknown host names, timeouts, a TLS failure) |
| `failed_sites` | list[string] | enabled websites of `sites.py` that saved no page in a finished run (`crawl` then exits with code 1 and `validate` FAILs) | `[]` |
| `discovered_sites` | list[string] | `*.nyit.edu` hosts without a profile, found through links | 18 hosts |
| `linked_but_not_crawled_sites` | object | disabled host -> links seen | `{"alumni.nyit.edu": 8568, "my.nyit.edu": 6543, "apply.nyit.edu": 649, ...}` |
| `dropped_query_self_links` | object | host -> query parameter -> links that normalise to the page they are on (parameters `sites.py` drops: filters, overlays, or page numbers that should be kept) | `{"blogs.nyit.edu": {"tag": 94}, "magazine.nyit.edu": {"overlay": 65}, "www.nyit.edu": {"fsa_keyword": 128, ..., "classroom": 88, "overlay": 8}}` (filters and overlays only) |
| `pdf_links` / `external_links` | int | distinct PDF links / off-site URLs found | `2106` / `21861` |
| `settings` | object | `delay`, `max_pages_per_site`, `include_print_pages`, `refresh`, `user_agent` of the last run | `{"delay": 1.0, "max_pages_per_site": 0, ..., "user_agent": "AskBearBot/1.0 (NYIT CSCI 426 student information-retrieval project; polite single-threaded crawler)"}` (the www.nyit.edu run; the main crawl used `--delay 0.6`) |
| `last_run` | object | the last run: `finished_at`, `seconds`, `interrupted`, `sites`, `urls_seen`, `urls_visited`, `still_queued`, `network_requests`, `status_counts` | the www.nyit.edu run: `{"sites": ["www.nyit.edu"], "seconds": 121.7, "network_requests": 42, ...}` |
| `runs` | list[object] | every run whose pages are in the cache, oldest first, with the keys of `last_run` (an interrupted run only has `finished_at`, `seconds`, `interrupted`, `sites` and `network_requests`) | 5 runs: the main crawl (7,692 s, 6,520 requests), the LibCal run (52 s, 6), the catch-up crawl, interrupted (703 s, 815) and resumed (10,323.9 s, 6,183), and the www.nyit.edu run (121.7 s, 42) |

### link_graph.jsonl

One line per saved page (8,677 lines, 450,156 links), merged over the runs.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `url` | string | the page | `"https://catalog.nyit.edu/"` |
| `outlinks` | list[string] | in-scope page links found on it (normalised, de-duplicated, page order; empty on 19 pages) | 74 links: `["https://catalog.nyit.edu/", "https://catalog.nyit.edu/help/undergraduate_students", ...]` |

### external_links.jsonl

Every distinct link that was **not** crawled as a page: 24,780 lines, merged over the runs.
`docs` reads its PDF links and its `asset` links to Word, Excel and PowerPoint files (`.docx`,
`.xlsx`, `.pptx`). `mailto:`, `tel:` and `javascript:` links are not recorded.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `url` | string | the link (normalised) | `"https://site.nyit.edu/files/academic_affairs/Academics_ModesOfInstruction_Web.pdf"` |
| `kind` | string | `external` (another organisation: 21,861), `pdf` (2,106), `asset` (images, Office files, ...: 361), `disabled` (NYIT host that is not crawled: 240), `excluded` (path outside a site's scope: 212). The kind is decided when the link is found; links found before a host was enabled keep `disabled` until the page that has them is crawled again | `"pdf"` |
| `anchor_text` | string | link text of the first occurrence (max 200 characters; `""` on 921) | `"Modes of Instruction at New York Tech"` |
| `found_on` | list[string] | up to 5 pages that contain the link | `["https://catalog.nyit.edu/courses", "https://catalog-dev.nyit.edu/courses"]` |
| `served_as_pdf` | bool | optional: the URL has no `.pdf` ending, but fetching it returned a PDF (kind `pdf`; `docs` downloads it) | `true` (1 URL in this dataset: `https://online.nyit.edu/documents/masters-degree-or-bootcamp`) |
| `content_type` | string | optional, with `served_as_pdf`: the `Content-Type` that was served | `"application/pdf;charset=UTF-8"` |

### sitemaps/\<host\>.json

The cached list of URLs from a website's XML sitemaps. There are 8 files: www, vancouver, magazine,
events, libanswers, libguides, online and libcal. A list is downloaded again when it is older than
7 days, or with `--refresh` / `--refresh-sitemaps`. A list that could only be read in part
(a sitemap failed temporarily) is not cached, so the next run tries again.

| Key | Type | Meaning | Example (libanswers.nyit.edu.json) |
|---|---|---|---|
| `host` | string | the website | `"libanswers.nyit.edu"` |
| `fetched_at` | string | when the sitemaps were read (UTC) | `"2026-11-23T08:17:00+00:00"` |
| `sitemaps` | list[string] | sitemap URLs read (from sites.py and robots.txt) | `["https://libanswers.nyit.edu/sitemap.xml"]` |
| `pages` | list of `[url, lastmod]` | every page URL listed, with its `<lastmod>` or `null` | `[["https://libanswers.nyit.edu/", null], ["https://libanswers.nyit.edu/faq/5654", null], ...]` (288) |

### docs_manifest.jsonl

One line per document URL (PDF, or Word/Excel/PowerPoint file) that `docs` tried to download
(1,767: 1,650 PDFs, 53 `.docx`, 61 `.xlsx`, 3 `.pptx`). As with the crawl manifest, the last
line for a URL wins, and every run rewrites the file with one line per URL. The kind of file is
that of the saved file's extension (else the URL's); an Office file is saved as `.docx`,
`.xlsx` or `.pptx`.

| Field | Type | Meaning | Example |
|---|---|---|---|
| `url` | string | the document link (normalised) | `"https://files.nyit.edu/files/policies/StudentHandbook_PhysicalTherapy_AcademicProgressionTree.pdf"` |
| `site` | string | host of `url` | `"files.nyit.edu"` |
| `status` | string | `ok` (1,742), `not_found` (20), `too_large` (5); possible too: `not_pdf` (a PDF link returned something else, e.g. an HTML page), `not_office` (a .docx/.xlsx/.pptx link returned something else: an HTML sign-in page, a legacy binary file, a damaged ZIP, another kind of Office file), `encrypted` (a password-protected Office file: an OLE2 container instead of a ZIP package), `http_error`, `error`, `robots_disallowed`, `redirect_loop` (the link redirects in a circle or more than 10 times) and `offsite_redirect` (it redirects to a sign-in page, a skipped NYIT host or another website, which is not requested). `not_pdf`, `not_office`, `encrypted`, `not_found`, `robots_disallowed`, `redirect_loop` and `offsite_redirect` are final; the others are retried by the next run | `"ok"` |
| `file` | string\|null | the file under `data/raw/docs/` (`null` unless `ok`) | `"files.nyit.edu/studenthandbook_physicaltherapy_academicprogressiontree__5fe2fe83f6.pdf"` |
| `final_url` | string | URL after redirects (the refused target when a redirect was not followed) | same as `url` |
| `redirects` | int | number of redirect hops (only written by the current version, when the link redirected: 25 entries) | `1` |
| `http_status` | int | HTTP status | `200` |
| `content_type` | string | `Content-Type` header | `"application/pdf"` |
| `bytes` | int | file size; for `too_large` the announced or partly read size | `67489`; `69327605` for a 69.3 MB PDF |
| `sha1` | string\|null | SHA-1 of the file (identical files are extracted once) | `"4d9539d1e8ba28cd3baf6def0ed215f94cb96bbe"` |
| `error` | string | error message, `""` if none | `""`; `"69.3 MB, over the 40 MB limit"` |
| `fetched_at` | string | download time (UTC) | `"2026-11-23T11:30:38+00:00"` |
| `noindex` | bool | the server sent `X-Robots-Tag: noindex` (such documents are not extracted) | `false` |
| `last_modified` | string\|null | HTTP `Last-Modified` as ISO date | `"2026-06-08T23:50:32+00:00"` |
| `found_on` | list[string] | up to 5 pages linking to the document | `["https://apps.nyit.edu/policies/collection/physical_therapy_student_handbook"]` |
| `anchor_text` | string | link text (`""` if none) | `""` |
| `source` | string | `link` (found by the crawler) or `sitemap` | `"link"` |

### Cached files

| Folder | File names | Example |
|---|---|---|
| `data/raw/html/<host>/` | the URL path segments joined with `__` (lower case, plus `__<key>-<value>` for query parameters) + `__<10 hex of sha1(url)>.html`. The slug has at most 60 characters in files saved by the current crawler (names of at most 77 characters); older files keep their longer names through the manifest. 9,835 files: 9,780 referenced by the manifest, 55 leftovers that no manifest entry refers to (an early catalog test crawl, and LibCal booking pages and other pages that are out of scope now). 310 files belong to hosts that `sites.py` disables now (300 of them catalog-dev.nyit.edu), which `parse` skips; `prune` lists them | `catalog.nyit.edu/engineering__computer_science__0c112f43a5.html`, `catalog.nyit.edu/courses__cal-ug_cs-csci__<hash>.html` |
| `data/raw/pdf/` | `NYIT<Kind>Catalog_AY<yy><yy>.pdf` with Kind = `Undergraduate`, `Graduate`, `OsteopathicMedicine` | `NYITUndergraduateCatalog_AY2526.pdf` (2025-2026) |
| `data/raw/docs/<host>/` | the file name of the downloaded URL (lower case, at most 60 characters) + `__<10 hex of sha1(url)>` + `.pdf`, `.docx`, `.xlsx` or `.pptx`. 1,742 files (1,625 PDFs, 53 Word, 61 Excel, 3 PowerPoint) | `files.nyit.edu/studenthandbook_physicaltherapy_academicprogressiontree__5fe2fe83f6.pdf` |
| `data/logs/` | logs written with `--log-file` while the dataset was built | `crawl_all_sites.log` (the main crawl), `crawl_libcal.log` (the LibCal run), `crawl_gapfill.log` (the catch-up crawl), `crawl_mrp.log` (the www.nyit.edu run), `pdf.log`, `docs.log` (the first downloads), `docs_reextract.log` (`docs --max-docs 0`), `docs_gapfill.log` (the 147 new files and the later re-extractions), `parse.log`, `parse_gapfill.log` (the parse runs of the final data), `crawl.log` (an early catalog crawl) |
