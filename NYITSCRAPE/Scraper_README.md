# AskBear scraper: Stage 1 (Collect)

This folder holds the web scraper and the finished dataset for **AskBear**, the CSCI 426
Information Retrieval group project at NYIT. It scrapes every public NYIT website, downloads
the PDF, Word, Excel and PowerPoint files those websites link to, and turns all of it into
clean, de-duplicated files that the search engine stages can index.

A complete dataset, crawled on **2026-11-23**, is already in `data/`. You can start using it
right away. This guide also explains how to rebuild it, step by step, on Windows, macOS and
Linux.

Three companion documents live in [`docs/`](docs/):

- [`docs/TEAM_FAQ.md`](docs/TEAM_FAQ.md): short answers to the questions teammates ask (where the data came from, how it was extracted and stored, whether it fits the project, limitations).
- [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md): every field of every output file, with types and real examples.
- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md): how the data was collected and cleaned, written so you can adapt it for the project report.

## Contents

1. [What this is](#1-what-this-is)
2. [Your checklist](#2-your-checklist)
3. [Requirements](#3-requirements)
4. [Setup, step by step](#4-setup-step-by-step)
5. [Quick test run (2-3 minutes)](#5-quick-test-run-2-3-minutes)
6. [Full run](#6-full-run)
7. [Which websites are scraped, and why some are not](#7-which-websites-are-scraped-and-why-some-are-not)
8. [Output files](#8-output-files)
9. [Hand-off to the indexing, BM25 and reranker teammates](#9-hand-off-to-the-indexing-bm25-and-reranker-teammates)
10. [Results compared with the proposal](#10-results-compared-with-the-proposal)
11. [Troubleshooting](#11-troubleshooting)
12. [Project structure, module tour, extending, FAQ](#12-project-structure-module-tour-extending-faq)

---

## 1. What this is

AskBear answers student questions about NYIT, for example *"What are the requirements for my
major?"*. BM25 retrieves the top ~100 candidate passages, an ML reranker re-scores them, and
AskBear shows the top 3 passages with links to their sources. The team evaluates it with
Precision@3, MRR, nDCG@10 and Recall@100. This folder is **stage 1, Collect**: a polite web
scraper, the `askbear_scraper` Python package, that crawls the academic catalog
(catalog.nyit.edu), the main website (www.nyit.edu) and the other public `*.nyit.edu` websites
(18 with pages in the dataset). It also downloads the documents those pages link to (PDF,
Word, Excel and PowerPoint files) and the full catalog PDFs. Everything is cleaned, split into
passages, de-duplicated and checked by 46 quality checks. The file the next stages index is
**`data/processed/corpus.jsonl`**: 41,725 passages, each with an id, a link, a title, the text
and metadata such as site, school, program, level and campus.

```
 STAGE 1: COLLECT  (this folder)               one command: python -m askbear_scraper all
 ===========================================================================================
   sites.py: which websites and which URLs are in scope
        |
        v
   [crawl] ----> data/raw/html/<host>/*.html  + crawl_manifest.jsonl, external_links.jsonl
        |                         |
        |                         +--> [docs] --> data/raw/docs/ (PDF, Office) -> documents.jsonl
        |
   [pdf] ------> data/raw/pdf/*.pdf (the full catalog PDFs) -----------> pdf_pages.jsonl
        |
        v
   [parse] ----> pages.jsonl, sections.jsonl, courses.jsonl/.csv, corpus.jsonl
        |        (uses the HTML cache + documents.jsonl; + pdf_pages.jsonl if you ask for it)
        v
   [stats] ----> STATS.md, stats.json        [validate] ----> validation_report.json
 ===========================================================================================
        |
        |   corpus.jsonl: 41,725 passages (id, url, title, text, metadata)
        v
 STAGE 2 INDEX --> STAGE 3 RETRIEVE (BM25, top ~100) --> STAGE 4 RERANK (ML) --> STAGE 5 RETURN
                                                                                (top 3 + links)
```

All processed files are in `data/processed/` and all downloaded raw material is in `data/raw/`.

---

## 2. Your checklist

Everything you have to do, from start to finish. Each step links to the details.

1. **Get the folder onto your computer.** On Windows, keep the path short, for example
   `C:\NYITSCRAPE`, and do not put it inside OneDrive. See [4.1](#41-get-the-folder).
2. **Install Python 3.11 or newer** (3.9 is the minimum). See [3](#3-requirements) and [4.2](#42-install-python).
3. **Create a virtual environment and install the packages** with `pip install -r requirements.txt`
   (add `-c constraints.txt` for the exact versions that built the data). See [4.3-4.5](#43-open-a-terminal-in-the-project-folder).
4. **Check the installation**: run `python -m askbear_scraper --version`, then
   `python -m askbear_scraper sites`, then the test suite `python -m pytest -q`. All tests should pass.
   See [4.6](#46-check-the-installation).
5. **Do the quick test** (2-3 minutes, in a separate `quicktest` folder). It proves that crawling,
   PDF extraction, parsing, statistics and validation work on your machine and network. See [5](#5-quick-test-run-2-3-minutes).
6. **Decide which data to use:**
   - **Use the included dataset (recommended).** It is complete and validated: the crawl
     fetched every page its rules allow on the hosts that could be reached, and what is still
     missing is missing on NYIT's side (see [7.4](#74-known-gaps-and-coverage-decisions)).
     Skip to step 7.
   - **Rebuild it** (about 4.5-5 hours and about 3.1 GB of disk; the crawl alone takes about
     4 hours because the library guides ask for 10 seconds between requests). See [6](#6-full-run).
     Always pass `--contact` with your NYIT e-mail so NYIT's web team can reach you.
7. **Verify the dataset:** `python -m askbear_scraper validate --strict` must end with
   `Result: PASSED with warnings  (39 PASS, 7 WARN, 0 FAIL, 0 SKIP)` (on a copy without the
   cached HTML, such as a git clone: `37 PASS, 7 WARN, 0 FAIL, 2 SKIP`). Then read
   `data/processed/STATS.md`. The 7 warnings are expected and explained in [10.3](#103-the-7-validation-warnings-explained).
8. **Look at the data yourself.** Open `data/processed/courses.csv` in Excel, print a few
   records of `corpus.jsonl` (see [8.3](#83-how-to-load-the-files)) and compare one or two
   passages with the live NYIT pages they link to.
9. **Share the data with your teammates.** The indexing, BM25 and reranker teammates need
   `corpus.jsonl` (75 MB, about 15 MB zipped). See [8.4](#84-sharing-the-data-with-teammates).
   Send them [section 9](#9-hand-off-to-the-indexing-bm25-and-reranker-teammates) and `docs/DATA_DICTIONARY.md`.
10. **Help the indexing teammate run the BM25 example** in [9.3](#93-a-working-bm25-example). It
    takes about 5 seconds on the full corpus.
11. **Agree on ids for the evaluation.** Labeled questions should point to corpus `id`s or URLs,
    and the team must keep the same copy of `corpus.jsonl` from labeling to final evaluation.
    Ids contain no spaces, so they work in TREC run and qrels files. Show one result per
    `metadata["dup_group"]`. See [9.5](#95-tips-for-the-search-and-evaluation-teammates).
12. **Write your part of the report and presentation.** Use the tables in `STATS.md`, the
    proposal comparison in [10](#10-results-compared-with-the-proposal) and the text of
    `docs/METHODOLOGY.md` (scope, method, politeness, ethics, limitations). Mention the known
    gaps of [7.4](#74-known-gaps-and-coverage-decisions) and that contact details printed on
    NYIT's pages stay in the text ([7.3](#73-politeness-and-ethics)). Do not show sensitive
    passages (contact details, the default password of one slide deck) in slides or screenshots
    without checking them first.
13. **Keep the data fresh.** Before the final demo, decide whether to re-crawl with `--refresh`
    (see [6.4](#64-keeping-the-data-fresh)), then validate again and share the new files with
    the crawl date in the file name.

---

## 3. Requirements

| What | Needed |
|---|---|
| Python | **3.9 or newer; 3.11, 3.12 or 3.13 recommended.** The dataset was built with Python 3.14 and the code also runs on 3.12. It is written for Windows, macOS and Linux, but it has only been run on Linux. |
| Python packages | `requests`, `beautifulsoup4`, `lxml`, `pymupdf`, `tqdm`, `pytest`, all installed by `pip install -r requirements.txt` (about 330 MB with PyMuPDF's own dependencies; `constraints.txt` pins the versions that built the data). The BM25 example additionally needs `rank_bm25`, which is optional and not in `requirements.txt`. |
| Internet | Only for `crawl`, `pdf`, `docs` and `pip`. `parse`, `stats`, `validate` and the tests work offline. |
| RAM | 4 GB or more. `parse` peaks at about 0.8 GB, the BM25 example at about 0.7 GB, and loading all processed files with pandas (section 8.3) at about 1.3 GB. |
| Disk | Using the included dataset: 222 MB for `data/processed` alone, 3.1 GB for all of `data/`. Rebuilding from scratch: about 3.5 GB free (5 GB to be comfortable), plus about 0.5 GB for the Python packages. |

Disk use of the included data (1 MB = 1,000,000 bytes; `du -sh` and Windows Explorer show
slightly smaller numbers because they count in MiB):

| Folder | Contents | Size |
|---|---|---:|
| `data/processed/` | the finished dataset (13 files) | 222 MB |
| `data/raw/html/` | cached HTML pages (9,835 files) | 911 MB |
| `data/raw/docs/` | PDF, Word, Excel and PowerPoint files linked from NYIT pages (1,742 files) | 1,865 MB |
| `data/raw/pdf/` | the 6 full catalog PDFs | 29 MB |
| `data/raw/` (other files) | manifests, link graph, external links, sitemap lists | 41 MB |
| `data/logs/` | logs of the runs that built the dataset | 0.1 MB |

Run times measured on the 2026-11-23 build. Yours depend on your network and computer.

| Step | First run | Run again (cache present) | Needs internet |
|---|---|---|---|
| `crawl` | **about 4 hours** with the default 1-second delay, set by the library guides: about 1,375 URLs at 10 s each (3.8 hours or more), while everything else is crawled in between. The included data took 5 h 14 min of crawling (18,893 s, 13,566 requests) in five runs, because it was crawled in two passes: a main crawl of 2 h 08 min with `--delay 0.6` and libguides still capped at 400 pages, then a catch-up crawl of 3 h 04 min for what the new rules added (see 6.2). | Resumes where it stopped. On a complete cache it requests only network errors and HTTP 5xx pages again (once), plus pages that are new: the last run of the included data, `crawl --site www.nyit.edu` on its complete cache, took 2 minutes and 42 requests. | yes |
| `pdf` | under 1 minute (2 PDFs, about 11 MB) | about 5 s (extraction only) | yes |
| `docs` | about 30 minutes (1,767 document links, 1.87 GB) | about 2 minutes (extraction only; `--max-docs 0` needs no network) | yes |
| `parse` | about 8.5 minutes (516 s for the included data) | the same, because it always rebuilds | no |
| `stats` | about 5 s | | no |
| `validate` | about 10 s | | no |

---

## 4. Setup, step by step

Once the virtual environment is active (step 4.4), every command in this README is the same on
Windows, macOS and Linux, and `python` means the environment's Python. When a command differs
between systems, both versions are shown.

### 4.1 Get the folder

Copy or unzip the `NYITSCRAPE` folder to your computer, or clone it if your team put it on
GitHub (`git clone <your-repo-url>`). The folder must contain `askbear_scraper/`,
`requirements.txt` and `data/`.

- **Windows:** keep the folder path short, for example `C:\NYITSCRAPE` or `C:\askbear\NYITSCRAPE`.
  Windows refuses paths longer than 260 characters unless "long paths" are enabled. Cache files
  saved by the current crawler have names of at most 77 characters (about 115 characters
  counted from the project folder), but some files of the included 2026-11-23 cache, saved by
  the earlier version that made the main crawl, keep their longer names (up to about 170 characters inside the project
  folder). So keep the project path under about 80 characters, or enable long paths
  (`LongPathsEnabled`, Windows 10 and 11). If a page still cannot be saved, the crawl does not
  stop: the page is recorded with status `error` ("could not save the page ...") and tried again
  by the next run. Avoid OneDrive folders: syncing 3.1 GB of small files is slow and OneDrive
  can lock files.
- **macOS/Linux:** any folder works, for example `~/askbear/NYITSCRAPE`.

### 4.2 Install Python

Check whether you already have a suitable Python:

Windows (PowerShell):
```powershell
py --version
python --version
```

macOS / Linux (Terminal):
```bash
python3 --version
```

If you see `Python 3.11` or newer you are ready. Otherwise:

- **Windows:** install Python 3.12 or 3.13 from <https://www.python.org/downloads/>. Tick **"Add
  python.exe to PATH"** in the installer. Then close and reopen PowerShell.
- **macOS:** install from <https://www.python.org/downloads/> or with Homebrew (`brew install python@3.12`).
- **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-venv python3-pip`.

### 4.3 Open a terminal in the project folder

All commands must be run **from the project folder** (the one that contains `askbear_scraper/`).
The package is not installed into Python, so `python -m askbear_scraper` only works there
(unless you install it with `pip install -e .`, see 4.5).

Windows (PowerShell):
```powershell
cd C:\askbear\NYITSCRAPE
```

macOS / Linux:
```bash
cd ~/askbear/NYITSCRAPE
```

### 4.4 Create and activate a virtual environment

A virtual environment keeps this project's packages separate from the rest of your computer.
Create it once, then activate it in every new terminal window.

Windows (PowerShell):
```powershell
py -3.12 -m venv .venv          # or: python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell answers *"running scripts is disabled on this system"*, allow scripts for the
current window only and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Alternatives: use `cmd.exe` and run `.venv\Scripts\activate.bat`, or skip activation and call
the environment's Python directly, for example `.\.venv\Scripts\python.exe -m askbear_scraper sites`.

macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

When the environment is active, your prompt starts with `(.venv)`. To leave it later, type `deactivate`.

### 4.5 Install the packages

Same on every system (with the environment active):

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This installs `requests`, `beautifulsoup4`, `lxml`, `pymupdf`, `tqdm` and `pytest`, plus what
they depend on. On Python 3.11 and newer, PyMuPDF 2.x also pulls in its layout-analysis
packages (`onnxruntime`, `numpy`, `networkx`, `protobuf`, `pymupdf4llm`, `pymupdf_layout`), so
pip installs about 26 packages and the environment takes about 330 MB. Allow a few minutes on
a slow network, and about 0.5 GB of free disk.

**Exact versions (optional).** `requirements.txt` only sets minimum versions, so pip picks the
newest ones. To install exactly the versions that built the included dataset (Python 3.11 or
newer), add the constraints file:

```bash
python -m pip install -r requirements.txt -c constraints.txt
```

Use this when you need byte-identical outputs and passage ids, for example to re-run `parse`
and compare with a teammate's copy. On Python 3.9 and 3.10 the pinned versions do not exist,
so pip ignores `constraints.txt` and installs the newest compatible ones (PyMuPDF 1.26.x on 3.9);
the tests pass with them.

**Installing the package (optional).** `python -m pip install -e .` installs the scraper into
the environment in "editable" mode (from `pyproject.toml`) and adds an `askbear-scraper`
command that you can run from any folder. Use `-e` (editable) only: the default data folder is
the `data` folder next to the `askbear_scraper` package, so a regular install would look for
the data inside Python's `site-packages`. Without installing, `python -m askbear_scraper` from
the project folder does the same.

### 4.6 Check the installation

```bash
python -m askbear_scraper --version
python -m askbear_scraper sites
python -m pytest -q
```

Expected:

- `--version` prints `python -m askbear_scraper 1.0.0`.
- `sites` prints the table of websites described in [section 7](#7-which-websites-are-scraped-and-why-some-are-not).
- `pytest` runs the test suite in `tests/`: 1,132 tests in under a minute (about 10 seconds on
  the machine that built the data). The last line should show `1131 passed, 1 skipped`, no
  `failed`: the skipped one is the optional live test.
  There are no `xfail` tests; every known defect the tests once documented is fixed. The tests
  never touch `data/` and never use the network: a guard in `tests/conftest.py` blocks every
  connection. `tests/test_compat.py` also checks that every module still parses with the
  Python 3.9 grammar and opens text files with `encoding="utf-8"`. The live smoke test talks to
  the real NYIT websites and only runs when you set `ASKBEAR_LIVE=1`:

  Windows (PowerShell):
  ```powershell
  $env:ASKBEAR_LIVE = "1"; python -m pytest -q -m live; Remove-Item Env:ASKBEAR_LIVE
  ```
  macOS / Linux:
  ```bash
  ASKBEAR_LIVE=1 python -m pytest -q -m live
  ```

`python -m askbear_scraper --help` lists all commands, and `python -m askbear_scraper <command> --help`
shows the options of each one.

---

## 5. Quick test run (2-3 minutes)

This test runs every step on a tiny sample: 3 pages per website, 1 PDF document and 1 catalog
PDF. It writes into a separate folder called `quicktest`, so the real `data/` folder is never
touched.

> **Use a separate `--data-dir` for test crawls.** A run limited by `--max-pages` or `--site`,
> or one you stop with Ctrl+C, no longer loses anything: every cached page it did not visit
> stays in the manifest, and `link_graph.jsonl`, `external_links.jsonl` and
> `crawl_summary.json` are merged with the earlier results. But a test run on the real `data/`
> folder would still add its sample pages and its run to the included dataset, so keep tests
> in a folder of their own.

Run these six commands one after another, from the project folder with the environment active:

```bash
python -m askbear_scraper --data-dir quicktest crawl --site catalog.nyit.edu --site www.nyit.edu --max-pages 3
python -m askbear_scraper --data-dir quicktest docs --max-docs 2
python -m askbear_scraper --data-dir quicktest pdf --kind undergraduate
python -m askbear_scraper --data-dir quicktest parse
python -m askbear_scraper --data-dir quicktest stats
python -m askbear_scraper --data-dir quicktest validate
```

What you should see (abbreviated; the counts of sitemap URLs change as NYIT edits its site):

```text
INFO    crawling catalog.nyit.edu, www.nyit.edu with a 1.0s delay per website (cache: ...quicktest/raw/html)
INFO    no robots.txt at https://catalog.nyit.edu/robots.txt (HTTP 404): all paths allowed
INFO    loaded robots.txt from https://www.nyit.edu/robots.txt
INFO    sitemap https://www.nyit.edu/sitemap_index.xml: 0 pages, 6 child sitemaps
INFO    www.nyit.edu: 3334 pages listed in sitemaps
WARNING catalog.nyit.edu: reached its limit of 3 pages; 307 queued URLs skipped
WARNING www.nyit.edu: reached its limit of 3 pages; 3352 queued URLs skipped
INFO    crawl finished: 6 pages OK on 2 sites, 0 soft-404, 0 login pages, 0 errors, 15 network requests in 23s

INFO    downloads: 1 new (0.1 MB), 0 already on disk, 0 failed
INFO    wrote 1 document pages to ...quicktest/processed/documents.jsonl

INFO    saved NYITUndergraduateCatalog_AY2627.pdf (5.9 MB)
INFO    NYITUndergraduateCatalog_AY2627.pdf: 329 pages, 136,002 words (raw 135,974), section paths from headings

INFO    parsed 6 pages of 3 websites in 0.7s (exact duplicates: 0, noindex: 0, failures: 0)
INFO    -> 38 sections, 0 courses in 0 subjects, 32 corpus records (section 31, document 1)

Result: PASSED with warnings  (35 PASS, 4 WARN, 0 FAIL, 7 SKIP)
```

The WARN and SKIP lines in a quick test are normal. The sample stops at 3 pages per site, so the
checks complain that the crawl hit its page limit, that the sitemaps' pages and www.nyit.edu's
paginated listings were not crawled, and that no school or course pages were saved. "3
websites" counts files.nyit.edu, which serves the one PDF document. A quick test has **0
courses** because the crawler only reaches the course directory (`/courses?cs=...`) after the
catalog's 13 start pages. The six commands take about 30 seconds in total (32 s when this
guide was last checked, most of it reading www.nyit.edu's sitemaps); with typing the commands
and reading the output, allow 2-3 minutes.

Look at the result in `quicktest/processed/`, then delete the folder:

Windows (PowerShell):
```powershell
Remove-Item -Recurse -Force quicktest
```
macOS / Linux:
```bash
rm -rf quicktest
```

---

## 6. Full run

### 6.1 Everything in one command

```bash
python -m askbear_scraper all --contact you@nyit.edu --log-file data/logs/all.log
```

Replace `you@nyit.edu` with **your own NYIT e-mail**. It is added to the crawler's
User-Agent so NYIT's web administrators can contact you. `all` runs
`crawl -> pdf -> docs -> parse -> stats -> validate` and takes about 4.5-5 hours from scratch:
about 4 hours for the crawl (the library guides alone are about 1,375 URLs at their 10-second
crawl delay, 3.8 hours or more, while the other sites are crawled in between), about 30
minutes for `docs` and about 9 minutes for `parse`. It stops after `crawl` (exit code 1) if the crawl was interrupted or if
an enabled website of `sites.py` saved no page at all (for example because its robots.txt
could not be read), so it never builds a dataset without, say, the catalog. Add `--strict` to
make the command exit with code 2 when a validation check fails.

Things to know before you start:

- **On the included `data/` folder, `all` resumes rather than starting over.** Pages and
  documents already on disk are not downloaded again. It fetches only pages that are new
  (pages NYIT added to its sitemaps or links since 2026-11-23), retries network errors and HTTP
  5xx answers once, and rebuilds the processed files. The included cache is complete for the
  current rules of `sites.py`, so nothing else is fetched. To download everything again, add
  `--refresh` (see 6.4).
- **To rebuild in a separate folder** and keep the included dataset untouched, add a data
  directory, for example `python -m askbear_scraper --data-dir ../askbear-data-fresh all --contact you@nyit.edu`.
- **By default `pdf` downloads only the 2026-2027 undergraduate and graduate catalogs.** To get
  all six catalog PDFs of the included dataset, including the 2025-2026 edition that the
  proposal's 581 pages / ~249K words come from, add
  `--edition 2025-2026 --edition 2026-2027 --kind undergraduate --kind graduate --kind osteopathic`.
- You can stop any step with **Ctrl+C**. Progress is saved line by line, so running the same command again continues where it stopped.

### 6.2 The same, one step at a time

Running the steps separately is easier to follow and to restart. These are the same steps that
built the included dataset. Please use the default delay of 1 second and add your contact.

The included crawl was made in five runs on 2026-11-23, all listed under `runs` in
`crawl_summary.json` (5 h 14 min and 13,566 requests in total). None of them passed `--contact`.

| Run | What it did | Time | Requests |
|---|---|---:|---:|
| 1. main crawl | every website, with `--delay 0.6`; libguides was still capped at 400 pages and the listings' page 2, 3, ... were not followed yet | 2 h 08 min (7,692 s) | 6,520 |
| 2. LibCal top-up | `crawl --site libcal.nyit.edu --max-pages 100` (a safety cap; 4 pages were in scope then), after the library-hours site was enabled | 52 s | 6 |
| 3. catch-up crawl, interrupted | a plain `crawl` (1 s delay) to fetch what the new crawl rules added; it was interrupted and resumed in run 4 | 11 min 43 s (703 s) | 815 |
| 4. catch-up crawl, resumed | the same command again, run to the end. Runs 3 and 4 together fetched the older blog posts behind the listings' `?p=N` pages, the library guides beyond the old cap of 400 (1,321 guide pages in all), all 128 faculty and staff accomplishments pages (`?fsa_page=N`), online.nyit.edu's blog listings and the LibCal event pages | 2 h 52 min (10,324 s) | 6,183 |
| 5. www.nyit.edu top-up | `crawl --site www.nyit.edu`, after `?mrp_page=` (NYITCOM research-project listings) was added to `sites.py` | 2 min 02 s (122 s) | 42 |

A crawl of everything from scratch in one pass takes about 4 hours instead: the other websites
are crawled while the crawler waits between two library-guide requests (see the `crawl` step
below).

```bash
python -m askbear_scraper sites
python -m askbear_scraper crawl --contact you@nyit.edu --log-file data/logs/crawl.log
python -m askbear_scraper pdf --edition 2025-2026 --edition 2026-2027 --kind undergraduate --kind graduate --kind osteopathic
python -m askbear_scraper docs --contact you@nyit.edu --log-file data/logs/docs.log
python -m askbear_scraper parse --log-file data/logs/parse.log
python -m askbear_scraper stats
python -m askbear_scraper validate --strict
```

Global options (`--data-dir`, `-v`/`--verbose`, `--log-file`) can go before or after the command name.

#### `sites`: which websites are crawled

- **What it does:** prints every website profile from `askbear_scraper/sites.py`: crawled or
  not, and why. It uses no network and writes no files.
- **Success looks like:** the table in [section 7](#7-which-websites-are-scraped-and-why-some-are-not).

#### `crawl`: download the web pages

- **What it does:** for every crawled website it reads robots.txt, starts from the site's start
  pages and the URLs listed in its XML sitemaps, and follows every in-scope link. The site
  profiles in `sites.py` decide the scope. Each page is saved once. Different websites are
  crawled in turn, and each website gets at most one request per `--delay` seconds, or the
  robots.txt `Crawl-delay` if that is longer (10 s on the three library sites libguides,
  libanswers and libcal). Other `*.nyit.edu` hosts found through links are crawled with a
  conservative default profile (at most 300 pages each).
- **Time:** about 4 hours from scratch with the default 1-second delay. The library sites set
  the pace: `libguides.nyit.edu` has about 1,375 URLs (1,321 guide pages, 27 redirects, 27
  broken links), and its robots.txt asks for 10 seconds between requests, so it alone needs at
  least 3.8 hours. In the catch-up run it averaged 11.1 seconds per URL, which would be 4.2
  hours. libanswers (287 pages) and libcal (14 pages) also wait 10 seconds per request, and
  all other websites are crawled in between, at the same time. The included data took 5 h 14
  min of crawling in total because it was crawled in two passes (see the table above).
- **Writes:** `data/raw/html/<host>/*.html` (the cache), `data/raw/crawl_manifest.jsonl` (one line
  per URL: status, cache file, title, ...; the last line for a URL wins),
  `data/raw/crawl_summary.json`, `data/raw/link_graph.jsonl`, `data/raw/external_links.jsonl`
  and `data/raw/sitemaps/<host>.json`.
- **Success looks like:** the last complete crawl of every website for the included data (run
  4 above) ended with
  `crawl finished: 8353 pages OK on 19 sites, 11 soft-404, 0 login pages, 1871 errors, 6183 network requests in 10324s`,
  followed by one line per website. `crawl_summary.json` has `"interrupted": false`,
  `"still_queued": 0` and `"failed_sites": []`. The "errors" are almost all broken links on
  NYIT's own pages (1,852 HTTP 404 in that run). They are expected; see 10.3.
- **Resuming and re-running:** progress is appended to the manifest after every page. After
  Ctrl+C, a lost connection or a closed laptop lid, run the same command again. A re-run reads
  from disk everything that is final: saved pages, redirects to pages already saved, 404s,
  redirect loops, login and off-site redirects. It requests again only what failed for a
  passing reason: network errors (retried normally) and HTTP 5xx answers (one attempt per run).
  Sitemap lists are cached in `data/raw/sitemaps/` and downloaded again when they are older
  than 7 days, or at once with `--refresh-sitemaps`. Exit code 1 means the crawl was
  interrupted or an enabled website saved no page (`failed_sites` in `crawl_summary.json`).
- **Partial runs are safe.** A run limited by `--site` or `--max-pages`, or an interrupted run,
  keeps every cached page it did not visit in the manifest, and merges `link_graph.jsonl`,
  `external_links.jsonl` and `crawl_summary.json` with the earlier results. In the summary,
  `sites` then lists every website covered, `last_run` describes the latest run, the status
  counts of the websites of a `--site` run are counted again from the merged manifest, and
  `runs` lists every run whose pages are in the cache (their `seconds` and `network_requests`
  add up). A full crawl that reuses the cache (a resumed or repeated `crawl`) is added to
  `runs` too; only a `crawl --refresh` of every website starts a new run history. Each page's
  `fetched_at` is the time of its request, taken after the politeness wait. So topping up one
  website is simply:
  ```bash
  python -m askbear_scraper crawl --site blogs.nyit.edu --contact you@nyit.edu
  ```
  Only a complete `crawl --refresh` of every website (no `--site`, no `--max-pages`) drops the
  manifest entries of pages that are no longer linked from anywhere. A backup of the manifest
  before a crawl is no longer needed, but it does no harm.
- **Useful options:**
  - `--site HOST` crawls only this website (repeat the option for several sites).
  - `--max-pages N` lowers each website's page limit to N (it can never raise the limit of
    `sites.py`). Pages cached earlier are kept, but use a separate `--data-dir` for quick tests.
  - `--delay SECONDS` sets the wait between two requests to the same site (default 1.0; values below 0.5 are raised to 0.5).
  - `--contact EMAIL` puts your e-mail in the User-Agent.
  - `--refresh` ignores the cache and downloads everything again.
  - `--refresh-sitemaps` downloads the sitemap lists again even if they are less than 7 days old.
  - `--timeout` (default 30 s) and `--retries` (default 4, with backoff of 2, 4, 8 and 16 s).
  - `--include-print` also crawls the catalog's `/print/` pages. They duplicate content, so this is off by default.

#### `pdf`: the full catalog PDFs

- **What it does:** downloads the printed catalogs from
  `https://catalog.nyit.edu/files/catalog/archives/` (only once) and extracts one record per
  page. Each record has a section path from the PDF bookmarks; the 2026-2027 PDFs have no
  bookmarks, so the path is rebuilt from their printed headings. `pdf_pages.jsonl` always
  covers **every** catalog PDF present in `data/raw/pdf/`, so runs for different editions add up.
- **Time:** under a minute.
- **Writes:** `data/raw/pdf/NYIT<Kind>Catalog_AY<yy><yy>.pdf`, `data/processed/pdf_pages.jsonl`, `data/processed/pdf_summary.json`.
- **Success looks like:** `wrote 1297 PDF pages from 6 PDFs ...`. When the 2025-2026 edition is
  present, it also prints `2025-2026 undergraduate + graduate: 581 pages, 249,274 raw words (proposal: 581 pages, ~249K words)`.
- **Re-running:** files that are already downloaded are reused. `--refresh` downloads them again.
- **Useful options:**
  - `--edition` is one of 2021-2022 ... 2026-2027 (default 2026-2027); repeat it for several editions.
  - `--kind` is `undergraduate`, `graduate` or `osteopathic` (default: undergraduate and graduate); repeat it for several kinds.
- **Note:** catalog PDF pages are **not** in `corpus.jsonl` by default because they repeat the
  catalog website. `parse --corpus-with-pdf` adds them.

#### `docs`: PDF, Word, Excel and PowerPoint files linked from NYIT pages

- **What it does:** takes every PDF link the crawler found (in `external_links.jsonl`) that is
  served by a crawled NYIT website: forms, handbooks, brochures, reports, semester maps. It
  does the same for links to Word, Excel and PowerPoint files in the current formats (`.docx`,
  `.xlsx`, `.pptx`: proposal templates, class-scheduling sheets, how-to slides; the crawler
  lists them as `asset` links). This
  includes links without `.pdf` at the end that the crawler found to answer with a PDF
  (`served_as_pdf` in `external_links.jsonl`, or status `non_html` with a PDF type in the crawl
  manifest; in the included data, online.nyit.edu/documents/masters-degree-or-bootcamp). The
  full catalog PDFs are left to `pdf` and other organisations' PDFs are skipped. It downloads
  each file once, respecting robots.txt and the delay, skips files over 40 MB, and extracts
  every PDF page with the same cleaning as the catalog PDFs. A Word, Excel or PowerPoint file
  is read from its XML with Python's standard library and cut into "pages" that the corpus
  treats like PDF pages:
  - Word: about 150-400 words, cut at headings or paragraphs; headings give the `toc_path`.
    The prompts of empty form fields ("Click or tap here to enter text.") are left out.
  - Excel: rows of one sheet. The sheet name is the `toc_path`, except a default name such as
    "Sheet1"; hidden sheets are skipped. Under column headings that `docs` finds in the sheet
    (not simply the first row), each row is written as `Heading: value | Heading: value`;
    rows without headings as `a | b | c`. Dates, times and percentages are shown as such
    (`2025-09-10`, `14:30`, `33%`).
  - PowerPoint: one slide. Its title is the first line and the `toc_path`; the other text
    boxes follow from top to bottom.

  `file_type` says which kind of file a page comes from. A download must really be the promised kind of file:
  an HTML sign-in page served instead of a PDF is `not_pdf`, instead of an Office file
  `not_office`, and a password-protected Office file is `encrypted`. Redirects are followed hop
  by hop, only to crawled NYIT websites. Identical files published under several URLs are
  extracted once. Each document gets a title of its own within its file type (documents that
  would share a title are told apart by edition, link text or file name; a form published both
  as PDF and as Word keeps one title, and the corpus title names the format). The title
  comes from the document's printed title, its metadata, the link text or the file name
  (qualifiers in file names are split into words: "CS to IT"). Link-text fragments and "This
  link opens in a new window" are never used, and a Word, Excel or PowerPoint file's own title
  property only when it matches the document. Each
  document also gets a category (`doc_category`: form, policy_handbook, semester_map,
  financial_filing, ...; what a file is, such as a form, template, affidavit, survey or
  report, counts more than the folder it is in) and, when its file name says so, an edition
  (`2024-25`, `Fall 2020`). An older edition of a yearly document is marked `superseded`.
  Run `crawl` first.
- **Time:** about 30 minutes the first time (1,767 document links, 1.87 GB). The included data
  took 26 minutes for its first 1,595 files and 5 minutes for 147 more after the catch-up
  crawl. About 2 minutes when every file is already on disk (re-extraction only).
- **Writes:** `data/raw/docs/<host>/*.pdf` (and `*.docx`, `*.xlsx`, `*.pptx`),
  `data/raw/docs_manifest.jsonl` (one line per document URL), `data/processed/documents.jsonl`,
  `data/processed/documents_summary.json`.
- **Success looks like:** `documents: 1682 (1567 pdf, 52 docx, 60 xlsx, 3 pptx; +60 links to identical files), 17648 pages, 4,383,801 words, 1947 empty pages (146 scanned PDFs without text), 1802.4 MB`
  and `wrote 17648 document pages to .../documents.jsonl`. A first run also reports `25 failed`:
  20 links answer HTTP 404 and 5 PDFs are over 40 MB. On the included data `docs` (with or
  without `--max-docs 0`) says `downloads: 0 new (0.0 MB), 1742 already on disk, 0 failed`.
- **Re-running:** only missing files are downloaded, and every file on disk is extracted again.
  Permanent failures (404, "not a PDF", "not an Office file", a password-protected Office file,
  robots.txt, a redirect loop, a redirect to a sign-in page or another website) are not retried
  unless you use `--refresh`. Network errors are retried on the next run, so a run after a
  complete one makes no request at all.
- **Useful options:**
  - `--max-docs N` downloads at most N files this time; the next run continues.
    `--max-docs 0` downloads nothing and only re-extracts the files already in `data/raw/docs`
    (no network), for example after a change to `documents.py` or `office.py`.
  - `--max-mb MB` sets the size limit (default 40; 0 means no limit).
  - `--refresh` downloads all files again (1.87 GB).
- **Not read:** legacy binary Office files (`.doc`, `.xls`, `.ppt`), password-protected or
  damaged Office files, and the text of the 146 scanned PDFs (no OCR). The Office files that
  are linked but not extracted are listed in `documents_summary.json` under
  `not_extracted_files`, each with its reason. In the included data that is 4 files, all in a
  legacy format (3 `.doc`, 1 `.xls`); every linked `.docx`, `.xlsx` and `.pptx` file was
  read. See 7.4.

#### `parse`: build the datasets

- **What it does:** reads the cached HTML (no network) and parses each page with the parser of the
  website where its content lives. The catalog has its own parser and course parser; every other
  site uses a template-aware generic parser that also removes per-site boilerplate. Each page is
  split into heading-based passages. Then `parse`:
  - skips error pages served with HTTP 200 ("404 | ...", empty catalog templates, web-server
    welcome pages, application error pages, placeholders and test pages);
  - marks exact duplicate pages;
  - merges passages of fewer than 5 words into their neighbour;
  - removes passages a website repeats word for word on several of its pages. The copy kept
    is not on page 2, 3, ... of a listing; then it is on the page whose title best matches
    the passage's heading; then on a page that is not a home or listing page; then on the
    shortest page. On a blog's list of posts, excerpts that repeat a post it links to are
    dropped too;
  - extracts the course directory;
  - collapses near-duplicate passages across the whole corpus (word 5-shingles, Jaccard of
    0.9 or more): a copy that belongs to the same program and level as a better copy is
    left out; copies for other programs, courses or entering classes stay and share
    `metadata["dup_group"]`;
  - adds the document pages from `documents.jsonl` (PDF, Word, Excel, PowerPoint; without
    empty, very short, repeated and superseded pages), and the catalog PDF pages only with
    `--corpus-with-pdf`.
- **Time:** about 8.5 minutes (516 s for the included data, 226 s of it learning each
  website's boilerplate); peak memory about 0.8 GB.
- **Writes:** `pages.jsonl`, `sections.jsonl`, `courses.jsonl`, `courses.csv`, `corpus.jsonl`, `parse_summary.json` (all in `data/processed/`).
- **Success looks like:**
  ```text
  parsed 8231 pages of 19 websites in 516.2s (exact duplicates: 646, noindex: 81, failures: 0)
  -> 29100 sections, 3389 courses in 208 subjects, 41725 corpus records (section 28858, course 3045, document 9822)
  ```
  (19 websites: 18 with pages, plus nyit.edu, which only serves one PDF document.)
- **Re-running:** always rebuilds every output from the cache. It is safe to run as often as you
  like, for example after changing a parser. Re-running it on the same cache gives byte-identical
  `pages.jsonl`, `sections.jsonl`, `courses.jsonl`, `courses.csv` and `corpus.jsonl` (a test checks this).
- **Useful options:**
  - `--corpus-with-pdf` adds the catalog PDF pages (see 9.1 for the count).
  - `--corpus-without-docs` leaves the 9,822 document pages (PDF, Word, Excel, PowerPoint) out.
  - `--corpus-with-superseded` also adds the pages of older editions of yearly documents
    (last year's handbooks, past term calendars): up to 4,780 pages, the ones that are not
    empty, very short or a word-for-word repeat of another document's page.
  - `--corpus-with-near-duplicates` skips the near-duplicate collapse and keeps every copy.

#### `stats`: statistics for the report

- **What it does:** prints a report and saves it as `data/processed/STATS.md` (Markdown, ready to
  paste into the report) and `data/processed/stats.json` (every number). The report covers the
  websites, the comparison with the proposal, the crawl (with every run), pages, passages,
  courses, catalog PDFs, documents and corpus.
  Inputs that do not exist yet show as `n/a` together with the command that creates them.
- **Time:** about 5 seconds. **Success looks like:** `wrote stats.json and STATS.md to ...`.

#### `validate`: quality checks

- **What it does:** runs 46 checks over `data/raw` and `data/processed`, grouped as crawl, pages,
  catalog, passages, text hygiene, catalog PDFs, PDF documents and corpus. It prints one line per
  check (PASS, WARN, FAIL or SKIP) and up to 3 examples per problem, and saves
  `data/processed/validation_report.json` with up to 10 examples per check.
- **Time:** about 10 seconds.
- **Success looks like:** `Result: PASSED with warnings  (39 PASS, 7 WARN, 0 FAIL, 0 SKIP)`.
  The warnings are explained in [10.3](#103-the-7-validation-warnings-explained). On a copy
  without `data/raw/html` (for example a git clone, see 8.4) checks 25 "Course deep links" and
  28 "Section #anchor links" cannot read the cached pages and are skipped, so
  `37 PASS, 7 WARN, 0 FAIL, 2 SKIP` is also correct there.
- **What some checks test:** "Paginated listings crawled" looks at each page-number parameter
  that `sites.py` follows (`?p=`, `?fsa_page=`, `?mrp_page=`, ...) on its own, so a missing
  `?mrp_page=` is noticed even when `?fsa_page=` pages are cached. "PDF documents: titles"
  compares titles within one file type, because a file published both as PDF and as Word or
  Excel may share its title (the corpus title names the format).
- **Useful options:** `--strict` exits with code 2 when a check FAILs, which is useful in scripts.

#### `prune`: clean up the cache (optional)

- **What it does:** lists the manifest entries of websites or paths that `sites.py` no longer
  crawls and the HTML files that no manifest entry refers to. On the included data that is 333
  entries (309 of them on catalog-dev.nyit.edu, a development copy; the others on hosts
  disabled after the main crawl and on paths that `sites.py` now skips) and 372 files
  (36.9 MB). `parse` ignores them anyway, so pruning only saves disk space. Without `--yes`
  nothing is changed.
- **Run:** `python -m askbear_scraper prune` (report only), then
  `python -m askbear_scraper prune --yes` to remove them. This cannot be undone.

### 6.3 Option reference

| Option | Commands | Default | Meaning |
|---|---|---|---|
| `--data-dir DIR` | all | `data` (next to `askbear_scraper/`) | where `raw/` and `processed/` live |
| `-v`, `--verbose` | all | off | show debug messages |
| `--log-file FILE` | all | none | also write the log to this file |
| `--site HOST` | crawl, all | every crawled site | crawl only this website; repeat for several (results are merged with the earlier crawl) |
| `--max-pages N` | crawl, all | each site's own limit (`sites.py`) | stop after N saved pages per website; can only lower a site's limit (tests) |
| `--include-print` | crawl, all | off | also crawl catalog `/print/` pages |
| `--refresh-sitemaps` | crawl, all | off | download the sitemap lists again even if they are less than 7 days old |
| `--delay S` | crawl, pdf, docs, all | 1.0 | seconds between two requests to the same website (minimum 0.5) |
| `--timeout S` | crawl, pdf, docs, all | 30 | request timeout |
| `--retries N` | crawl, pdf, docs, all | 4 | retries on timeouts, HTTP 429 and 5xx |
| `--contact EMAIL` | crawl, pdf, docs, all | none | e-mail shown in the User-Agent (recommended) |
| `--refresh` | crawl, pdf, docs, all | off | ignore the cache, download again |
| `--edition YYYY-YYYY` | pdf, all | 2026-2027 | catalog PDF edition(s): 2021-2022 ... 2026-2027 |
| `--kind KIND` | pdf, all | undergraduate + graduate | `undergraduate`, `graduate`, `osteopathic` |
| `--max-docs N` | docs | all | download at most N documents (PDF or Office files) this run |
| `--max-mb MB` | docs | 40 | skip files larger than this (0 = no limit) |
| `--corpus-with-pdf` | parse, all | off | add the catalog PDF pages to `corpus.jsonl` |
| `--corpus-without-docs` | parse, all | off | leave the document pages (PDF and Office files) out of `corpus.jsonl` |
| `--corpus-with-superseded` | parse, all | off | also add older editions of yearly documents (`superseded` in documents.jsonl) |
| `--corpus-with-near-duplicates` | parse, all | off | keep every near-duplicate copy of a passage (no `dup_group` collapse) |
| `--skip-pdf`, `--skip-docs` | all | off | skip the `pdf` / `docs` step |
| `--strict` | validate, all | off | exit code 2 if a check fails |
| `--yes` | prune | off | really remove the listed cache entries and files |

`--max-docs 0` means "download nothing, re-extract what is on disk".

### 6.4 Keeping the data fresh

The included dataset is a snapshot of 2026-11-23: its pages were fetched between 07:05 and
20:25 UTC that day (`fetched_at` of each page); the older blog posts, the library guides beyond
the first 400 and the accomplishments pages come from the catch-up crawl (16:11-20:03 UTC).
NYIT's pages change: news, events
and deadlines change weekly, library hours every week, and the catalog once a year. A normal
re-run **does not** notice changed pages, because cached pages are reused; it only adds new
pages (sitemap lists are downloaded again after 7 days, so pages NYIT adds to its sitemaps are
found) and retries network errors and HTTP 5xx answers.

**To top up one website** (for example after you change its rules in `sites.py`), crawl only
that website; the results are merged with the existing crawl and the run is added to `runs` in
`crawl_summary.json`:

```bash
python -m askbear_scraper crawl --site libcal.nyit.edu --contact you@nyit.edu
python -m askbear_scraper docs --contact you@nyit.edu
python -m askbear_scraper parse
python -m askbear_scraper stats
python -m askbear_scraper validate --strict
```

**To update everything** before the final demo, make a backup first, then re-download (about
4 hours for the crawl; a `--refresh` crawl of every website starts a new run history in
`crawl_summary.json`):

Windows (PowerShell):
```powershell
Compress-Archive -Path data\processed -DestinationPath askbear-processed-2026-11-23.zip
python -m askbear_scraper crawl --refresh --contact you@nyit.edu --log-file data/logs/crawl-refresh.log
python -m askbear_scraper docs --contact you@nyit.edu
python -m askbear_scraper parse
python -m askbear_scraper stats
python -m askbear_scraper validate --strict
```

macOS / Linux:
```bash
(cd data && zip -qr ../askbear-processed-2026-11-23.zip processed)
python -m askbear_scraper crawl --refresh --contact you@nyit.edu --log-file data/logs/crawl-refresh.log
python -m askbear_scraper docs --contact you@nyit.edu
python -m askbear_scraper parse
python -m askbear_scraper stats
python -m askbear_scraper validate --strict
```

`docs` without `--refresh` downloads only documents that are new since the last run. Add
`--refresh` if you also want changed files; that downloads 1.87 GB again. Re-crawling changes the passages and
their ids, so **do not re-crawl between labeling evaluation questions and computing the final
metrics**, or label again afterwards. When NYIT publishes a new catalog edition (for example
2027-2028), add it to `PDF_EDITIONS` in `askbear_scraper/config.py`, for example
`"2027-2028": "AY2728"`, and run `pdf --edition 2027-2028`.

---

## 7. Which websites are scraped, and why some are not

The single source of truth is `askbear_scraper/sites.py`. Print it with
`python -m askbear_scraper sites`. The page counts below are from the 2026-11-23 crawl (all five runs).

### 7.1 Crawled

| Website | What it is | How it is crawled | Pages saved |
|---|---|---|---:|
| catalog.nyit.edu | Academic catalog 2026-2027: programs, curricula, courses, policies (the core of AskBear) | 13 start pages + links + the course-directory dropdown; own parser | 983 |
| www.nyit.edu | Main website: admissions, degrees, tuition, student life, news, faculty bios, offices | sitemap (3,333 URLs) + links; the page numbers of two listings are followed: all 128 pages of faculty and staff accomplishments (`?fsa_page=N`) and NYITCOM's research projects (`?mrp_page=N`, 6 pages) | 3,529 |
| vancouver.nyit.edu | Vancouver campus | sitemap + links | 101 |
| magazine.nyit.edu | NYIT Magazine | sitemap + links (max 2,000) | 59 |
| events.nyit.edu | Events calendar, only `/event/` pages (calendar views are endless) | sitemap + links from other NYIT pages; links on event pages are not followed | 99 |
| apps.nyit.edu | Policy library, only `/policies` (student handbook, code of conduct, ...) | `/policies` + links (max 2,000) | 205 |
| libanswers.nyit.edu | Library FAQ, only `/faq/` pages | sitemap; links on FAQ pages are not followed; 10 s between requests (robots.txt) | 287 |
| libguides.nyit.edu | Library research guides | sitemap + links; 10 s between requests; max 1,500 pages (1,321 found, so the limit was not reached) | 1,321 |
| blogs.nyit.edu | NYIT blogs | `/` + links (max 4,000); every listing page `?p=2`, `?p=3`, ... is followed so older posts are found (306 listing pages) | 1,500 |
| online.nyit.edu | Online programs | sitemap + links (max 1,500); blog listing pages `?p=N` are followed; the marketing vendor's `/privacy-policy` and `/cookies` notices are skipped | 67 |
| libcal.nyit.edu | Library opening hours and library events (LibCal) | the home page, `/hours` and `/event/<id>` only; room booking, appointment and reservation forms are skipped; 10 s between requests. The hours are a weekly snapshot: check `fetched_at` | 14 |
| deseversky.nyit.edu | de Seversky Mansion (event venue) | `/` + links (max 300) | 35 |
| nyit.edu, site.nyit.edu | legacy hosts | links are followed. Their 14 saved URLs redirect to other NYIT sites (catalog.nyit.edu 6, blogs.nyit.edu 6, www.nyit.edu 1, deseversky.nyit.edu 1), where they are counted; 97 more redirect to pages already saved (`duplicate`) | 1 + 13 |
| files.nyit.edu | file server: PDF forms and documents (see `docs`); its 15 HTML pages are e-mail announcements (page type `communication`) | links | 15 |
| eportfolios.nyit.edu | Faculty ePortfolios (Mahara) | the home page only; the rest is the Mahara software (sign-in, error and placeholder pages) | 6 (1 parsed) |
| other `*.nyit.edu` found through links | default profile, at most 300 pages each | links | 132 on 4 hosts |

The four other hosts with saved pages are comapp.nyit.edu (100), biggive.nyit.edu (29),
aaupatnyit.nyit.edu (2) and wifi.nyit.edu (1). LibCal was enabled after the main crawl: a
separate run on the same day (`crawl --site libcal.nyit.edu --max-pages 100`: 4 pages, 6
requests, 52 seconds) fetched its home and hours pages, and the catch-up crawl added its event
pages (14 pages in all). All five runs are merged in `crawl_summary.json` (`runs`, see 6.2).

In total, 8,677 pages were saved on 26 hosts, and `parse` keeps 8,231 page records from 18
websites. The difference:
- 310 pages are on 6 hosts that the main crawl found through links and that `sites.py` now
  disables, because they hold no public information: catalog-dev.nyit.edu (300, a development
  copy of the catalog), virtualtour (5), nyitetic (2), comresearchdata, iccjb and learningspace
  (1 each). See 7.2.
- 7 are pages that `sites.py` now skips on enabled hosts: 5 eportfolios.nyit.edu software
  pages and online.nyit.edu's `/privacy-policy` and `/cookies`.
- 28 are second URLs of a page that was also saved under another URL (17 on www.nyit.edu, 5
  on the legacy hosts nyit.edu and site.nyit.edu). The other 9 saved URLs of the legacy hosts
  are the only copy of their page and are parsed as pages of the website they redirect to.
  That is why those two hosts have no pages of their own (nyit.edu still serves one PDF
  document).
- 101 are error pages served with HTTP 200: 59 empty catalog program templates (`/curriculum/`
  and `/semester-map/` pages that www.nyit.edu links to), 39 blog "page not found" pages
  (mostly mistyped links in older posts, such as e-mail addresses or other websites written
  as blog paths) and 3 test pages.

### 7.2 Not crawled, and why

| Host(s) | Why not | Links seen |
|---|---|---:|
| my.nyit.edu, campusgroups.nyit.edu, alumni.nyit.edu | login portals: the content needs an NYIT account | 6,543 / 64 / 8,568 |
| nyitcs.nyit.edu, identity.nyit.edu, pwreset.nyit.edu, nyit.okta.com, and any host with a sign-in word as any part of its name (`sso`, `login`, `idp`, `okta`, `adfs`, `saml`, `shibboleth`, `oauth`, `vpn`, ...), for example oaproxy-sso.nyit.edu | single sign-on, password and account systems | |
| workday.nyit.edu, onecard.nyit.edu, engage.nyit.edu, apps2.nyit.edu and other back-office hosts | self-service and back-office systems behind a login | 4 / 21 / 45 / 33 |
| web.nyit.edu, registrations.nyit.edu | their robots.txt disallows all crawling | 25 / 51 |
| nyit.service-now.com | IT knowledge base: robots.txt says `Disallow: /` and it requires an Okta sign-in. It is not a `*.nyit.edu` host, so it is out of scope anyway (209 different URLs on it are linked from NYIT pages). | |
| `*.arktos.nyit.edu` (for example www-oxfordreference-com.arktos.nyit.edu), nyit.idm.oclc.org | the library's EZproxy: licensed databases behind a library login | |
| catalog-dev.nyit.edu, any host with `dev`, `staging`, `test`, `qa`, `sandbox`, ... in its name | development and test copies of real sites (duplicate content) | |
| learningspace.nyit.edu, iccjb.nyit.edu | the simulation centers' Elevate LearningSpace application: a sign-in screen, no public text | |
| comresearchdata.nyit.edu | a REDCap survey and data-capture tool (forms, no text) | |
| nyitetic.nyit.edu | a research-robot web application: a default IIS server page and script-only pages | |
| virtualtour.nyit.edu | a JavaScript-only virtual tour player (the HTML only says the browser is unsupported) | |
| eportfolios.nyit.edu, except its home page | the Mahara ePortfolio software: sign-in screens, error and placeholder pages | |
| apps.nyit.edu outside `/policies` (people directory, course search, forms) | personal contact data and dynamic search tools | |
| apply.nyit.edu | application and event-registration forms, not information pages | 649 |
| libcal.nyit.edu booking, appointment and reservation pages | forms (the home page, opening hours and event pages are crawled, see 7.1) | |
| cediploma.nyit.edu, comresearchapp.nyit.edu | third-party vendor tools: diploma verification, research intake forms | 2 / 5 |
| content.campaign.nyit.edu | robots.txt disallowed the one URL linked from NYIT's pages | |
| bearcat.nyit.edu, camstream.nyit.edu, hub.nyit.edu, iris.nyit.edu, nyitonline.nyit.edu, tbls.nyit.edu, www.imodules.nyit.edu | could not be reached during the crawl: the name does not resolve (bearcat, hub, iris), the connection times out (camstream, nyitonline, tbls) or the secure connection fails (www.imodules). 14 URLs on them, linked from NYIT pages, could not be fetched | |
| Facebook, Instagram, LinkedIn, X/Twitter, YouTube, TikTok and every other non-NYIT website | not NYIT websites. Their links are recorded in `external_links.jsonl` (21,861 different off-site URLs) but never fetched | |

"Links seen" is the number of links to that host found on crawled pages
(`linked_but_not_crawled_sites` in `crawl_summary.json`). The hosts disabled after the main crawl
(learningspace, iccjb, comresearchdata, nyitetic, virtualtour) were crawled once, and their 10
pages are skipped by `parse`; `python -m askbear_scraper prune` lists them (see 6.2).

### 7.3 Politeness and ethics

- **robots.txt is obeyed.** The crawler downloads it for every website and follows its `Disallow`
  rules and `Crawl-delay`. The three library sites (libguides, libanswers, libcal) ask for **10
  seconds** between requests, and the crawler waits that long, also before the first page after
  reading robots.txt. Websites without a robots.txt (catalog.nyit.edu, apps, blogs, site,
  files and comapp answer HTTP 404; eportfolios and wifi answer with an HTML page) get our own
  delay. A robots.txt that answers 401 or 403 means that
  nothing is fetched from that website. A robots.txt that cannot be read (network error, HTTP
  5xx) blocks the website and is tried again after 60 and 300 seconds; meanwhile the other
  websites go on (RFC 9309).
- **Redirects are checked hop by hop.** Each redirect is followed by hand, at most 10 hops, and
  each hop waits for its own website's delay and is checked against that website's robots.txt.
  A redirect to a sign-in portal, a disabled NYIT host or a non-NYIT website is recorded and
  never requested, and a redirect loop is stopped at the first repeated URL. The `docs` step
  follows the redirects of PDF links the same way.
- **Slow and single-threaded.** The crawler makes one request at a time. Two requests to the same
  website are at least `--delay` seconds apart (default 1.0 s, never below 0.5 s), plus a random
  0-0.25 s. Different websites are visited in turn, so no server gets more than one request per delay.
- **Back-off on errors.** Timeouts, HTTP 429 and 5xx are retried at most 4 times, waiting 2, 4, 8 and
  16 seconds. A `Retry-After` header is honoured (up to 30 s). A website that fails 3 times in a row
  is left alone for the rest of the run. A page that failed with HTTP 5xx in an earlier run gets
  a single attempt in later runs.
- **Honest identity.** The User-Agent is `AskBearBot/1.0 (NYIT CSCI 426 student information-retrieval
  project; polite single-threaded crawler; contact: you@nyit.edu)`. The contact part only appears
  if you pass `--contact`.
- **Everything is downloaded once.** The cache means re-runs read from disk instead of the server.
  The included crawl made 13,566 requests in five runs (6,520 in 2 h 08 min for the main crawl,
  6,998 for the catch-up crawl, 6 and 42 for two top-ups; see 6.2). The catch-up crawl only
  requested pages the cache did not have yet, network errors, HTTP 5xx pages and robots.txt
  files.
- **No login areas.** The crawler never sends credentials. Login portals are disabled. Pages
  with a password field, and pages titled "Log in" or "Sign in" that have hardly any text (some
  sign-in screens build their form with JavaScript), are discarded as `login_page`. Web-server
  default pages, script-only shells and application error pages are discarded as `soft404`.
- **The main crawl was made by an earlier version of the crawler.** That version followed
  redirects automatically: about 12 requests reached pages whose robots.txt forbids them and
  about 80 reached sign-in pages or other organisations' websites through redirects of NYIT
  links. None of those pages was kept (they are recorded as `offsite_redirect` or
  `robots_disallowed` and never parsed). The later runs (LibCal, catch-up and www.nyit.edu
  top-ups) were made by the current crawler, described above, which no longer makes such
  requests.
- **Search engines' wishes are respected.** Pages marked `noindex` are kept on disk but never
  indexed, links on `nofollow` pages are not followed, and PDFs sent with `X-Robots-Tag: noindex` are not extracted.
- **No harvesting of personal data, but contact details stay in the text.** The people
  directory and the student and employee systems are not crawled, `mailto:` links are never
  followed, and no list of e-mail addresses is built. The text of the public pages is kept as
  NYIT publishes it, though, and that includes the contact details printed on them: 2,696 corpus
  records contain an e-mail address (1,602 distinct addresses, 1,219 of them `@nyit.edu`;
  mostly office and staff contacts on office, bio and policy pages and in documents), and
  1,485 records contain a phone number. The 422 faculty and staff profiles that NYIT publishes on
  www.nyit.edu and the faculty lists in the catalog are included because they are public
  information pages. On the library guides, each librarian's profile box is a passage of its
  own ("Librarian: <name>", with job title, subjects, e-mail, phone and address). `stats`
  reports these counts (`corpus.contacts` in `stats.json`). If your use of the data needs it,
  remove the addresses before sharing passages outside the team.
- **Handle sensitive passages with care.** Public pages and files sometimes contain things
  that should not be shown around without thought. For example, a public how-to slide deck on
  files.nyit.edu (the UMB Commercial Card first-time log-in guide, corpus id
  `doc-a20a98e1ad-p0003`) prints a default first-time password, and the contact details above
  stay in the text. Do not put such passages on slides, in screenshots or in demo answers
  without checking them, and never try a password found in the data.
- **Use the data for the course project only.** The content belongs to NYIT. Do not publish the
  dataset; link to the source pages instead (every record has its URL).

### 7.4 Known gaps and coverage decisions

What the included dataset does **not** contain, and why. The crawl itself is complete: it
finished with nothing left in the queue, no website reached its page limit, and every
page-number parameter that `sites.py` follows was crawled (the older blog posts, the library
guides beyond the old cap of 400, all 128 accomplishments pages and online.nyit.edu's blog
listings were fetched by the catch-up crawl, see 6.2). What is still missing is listed below.
`validate` reports most of it as warnings (see 10.3).

**Gaps on NYIT's side** (nothing to fix in the scraper):

| Gap | Size | Why it is missing |
|---|---|---|
| NYIT hosts that could not be reached | 7 hosts, 14 linked URLs | bearcat, hub and iris do not resolve; camstream, nyitonline and tbls time out; www.imodules fails the secure connection. The crawler tried robots.txt 3 times per host and gave up on iris.nyit.edu's 3 queued URLs |
| broken links | 1,879 URLs answer HTTP 404 | 1,662 are on www.nyit.edu, mostly old `/news/...` and `/bio/...` addresses (1,206 of the 1,662 were found on older blog posts). The others: blogs.nyit.edu 127, libguides 27, site.nyit.edu 16, vancouver 15, apps 10, nyit.edu 10 and 12 on four more hosts |
| catalog faculty pages | 7 pages (`/administration_and_faculty/faculty`, `/architecture/faculty`, `/medicine/faculty`, ...) | NYIT's server answers HTTP 500 |
| redirect loops | 19 URLs (www.nyit.edu 16) | they redirect in a circle |
| empty catalog templates | 59 catalog `/curriculum/<slug>` and `/semester-map/<slug>` links on www.nyit.edu | they lead to empty catalog templates (skipped by `parse`). Among them are the Public Health (M.P.H.) and International Business (B.S.) curricula. The M.P.H. curriculum appears in the catalog only in the 2025-2026 osteopathic medicine PDF; www.nyit.edu has its degree page |
| "page not found" pages | 39 blog URLs (skipped by `parse`) and 6 event pages (kept, without text) | served with HTTP 200: the blog URLs come from mistyped links in older posts; the 6 event pages say the event is not found |
| pages without text | 116 pages (www 24, comapp 25, libguides 22, biggive 12, blogs 12, deseversky 8, events 6, vancouver 5, libanswers 2) | image galleries, video and script-only pages, empty templates: their content is not in the HTML. They get no passages |
| broken document links | 20 document links answer HTTP 404 | links to files that are gone |
| other domains | for example the IT knowledge base on nyit.service-now.com | out of scope (7.2) |

**Deliberate limits:**
- Every website has a page limit (`max_pages` in `sites.py`; 300 for hosts found through
  links). `--max-pages` can only lower it. To crawl more of a website, raise its `max_pages`.
  No website of the included crawl reached its limit.
- JavaScript-only applications are not crawled or give no text: virtualtour.nyit.edu (a tour
  player), nyitetic.nyit.edu, the Mahara pages of eportfolios.nyit.edu (see 7.2).
- 81 page records (72 distinct pages) ask search engines not to index them (`<meta
  name="robots" content="noindex">`): 50 on www.nyit.edu, 18 on libguides and 13 on
  magazine.nyit.edu. They stay in `pages.jsonl` but are not indexed. 12 of the magazine pages
  (back issues: spring 2024, fall 2024, fall 2025 and the 2024 annual report) are also
  `nofollow`, so the 49 back-issue articles that only they link to were never fetched.
- Listing pages 2, 3, ... (`?p=N` on the blogs and online.nyit.edu; 288 pages) are kept in
  `pages.jsonl` as `listing` but get no passages, because they only repeat the first lines of
  posts that are pages of their own. The accomplishments pages (`?fsa_page=N`) and the research
  projects pages (`?mrp_page=N`) do get passages: they hold the items themselves.
- PDFs over 40 MB (5 portfolio and magazine PDFs) are not downloaded.
- The text of scanned PDFs is not read (146 PDFs, no OCR). Document pages without text (1,947
  in all, most of them in these scans) stay in `documents.jsonl` but not in the corpus.
- Office files in the legacy binary formats (`.doc`, `.xls`, `.ppt`) or protected by a password
  are not read: 4 linked files (3 `.doc`, 1 `.xls`), listed in `documents_summary.json` under
  `not_extracted_files` with their reason. Every linked `.docx`, `.xlsx` and `.pptx` file (115
  documents) is read.
- Older editions of yearly documents (241 documents, 5,594 pages) are in `documents.jsonl` with
  `superseded: true` but not in the corpus; `parse --corpus-with-superseded` adds them.

**Catalog PDFs and the corpus.** The catalog PDF pages are not in `corpus.jsonl` by default:
96-98% of the 5-word phrases of the 2026-2027 PDFs also appear on the catalog website, and the
2025-2026 PDFs are the previous edition (84-91% of their phrases are on the website). Add them
with `parse --corpus-with-pdf` only if you want the printed catalogs as separate sources, and
then filter on `metadata["edition"]` so that two editions do not compete. Use
`parse --corpus-without-docs` for a web-only corpus (no PDF, Word, Excel or PowerPoint documents).

---

## 8. Output files

### 8.1 What is in `data/`

`data/processed/`: the finished dataset (222 MB):

| File | What it holds | Records | Size |
|---|---|---:|---:|
| `corpus.jsonl` | **The search corpus. Index this.** Sections + courses + document pages (PDF, Word, Excel, PowerPoint), each with `id`, `source`, `url`, `title`, `text`, `metadata` | 41,725 | 74.7 MB |
| `pages.jsonl` | one record per web page: clean full text and metadata. Exact copies are marked with `duplicate_of` | 8,231 | 44.8 MB |
| `sections.jsonl` | heading-based passages of the distinct, indexable pages (near-duplicates marked with `duplicate_of`) | 29,100 | 41.4 MB |
| `courses.jsonl` | one record per catalog course | 3,389 | 5.0 MB |
| `courses.csv` | the same courses as a spreadsheet (UTF-8 with BOM, opens in Excel) | 3,389 rows | 4.0 MB |
| `documents.jsonl` | one record per page of each PDF, Word, Excel or PowerPoint file linked from NYIT pages (`file_type`; empty scanned pages and older editions included) | 17,648 | 45.4 MB |
| `pdf_pages.jsonl` | one record per page of the 6 full catalog PDFs | 1,297 | 4.4 MB |
| `parse_summary.json` | `parse` counts per website, skipped pages, near-duplicates, timings | | 0.1 MB |
| `documents_summary.json` | `docs` facts per document (file type, title, category, edition), downloads, failures, linked Office files not extracted (with the reason) | 1,682 documents | 2.2 MB |
| `pdf_summary.json` | `pdf` facts per catalog PDF | 6 PDFs | 6 KB |
| `stats.json` | every statistic, machine-readable | | 0.1 MB |
| `STATS.md` | the statistics report in Markdown, for the project report | | 26 KB |
| `validation_report.json` | the 46 quality checks with examples | 46 checks | 24 KB |

`data/raw/`: the downloaded material (2.8 GB):

| File / folder | What it holds | Records | Size |
|---|---|---:|---:|
| `html/<host>/*.html` | cached HTML of every fetched page. Of the 9,835 files, 9,780 are referenced by the manifest; 55 are leftovers of early test crawls | 9,835 files | 911 MB |
| `crawl_manifest.jsonl` | one line per visited URL: status, HTTP code, cache file, title, final URL after redirects, ... (last line per URL wins) | 11,864 | 8.6 MB |
| `crawl_summary.json` | summary of the crawl: statuses per site, capped/unreachable/failed sites, settings, and `runs` (the five runs of 6.2) | | 13 KB |
| `link_graph.jsonl` | the in-scope links of every saved page | 8,677 | 24.2 MB |
| `external_links.jsonl` | links that were not crawled: PDFs (2,106), other websites (21,861), assets (361, among them the 121 linked Office files), disabled hosts (240), excluded paths (212) | 24,780 | 6.7 MB |
| `sitemaps/<host>.json` | cached sitemap URL lists | 8 files | 0.5 MB |
| `pdf/*.pdf` | the 6 full catalog PDFs (2025-2026 and 2026-2027; undergraduate, graduate, osteopathic) | 6 | 28.8 MB |
| `docs/<host>/*.pdf` (`.docx`, `.xlsx`, `.pptx`) | PDFs and Office files linked from NYIT pages (1,625 PDF, 53 Word, 61 Excel, 3 PowerPoint files) | 1,742 | 1,865 MB |
| `docs_manifest.jsonl` | one line per document URL: download status, file, SHA-1, where it is linked from | 1,767 | 1.4 MB |

`data/logs/` holds the logs of the runs that built the dataset: the main crawl (`crawl_all_sites.log`; `crawl.log` is an earlier catalog-only run), the LibCal, catch-up and www.nyit.edu crawls (`crawl_libcal.log`, `crawl_gapfill.log`, `crawl_mrp.log`), `pdf`, `docs` with its re-extractions (`docs.log`, `docs_reextract.log`, `docs_gapfill.log`) and `parse` (`parse.log`, `parse_gapfill.log`). Their times are local (EST), while the data files use UTC.

Every field of every file is described in [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).

### 8.2 Example records

These are real records, shortened (`...`). One JSON object per line in the real files.

`corpus.jsonl`, a section:
```json
{"id": "page-0c112f43a5-s006", "source": "section",
 "url": "https://catalog.nyit.edu/engineering/computer_science#admissions_csbs_ecebs",
 "title": "Computer Science, B.S. — Admission Requirements",
 "text": "Computer Science, B.S.\nAdmission Requirements\nThis program has specific admission requirements in addition to our general requirements.\nFirst Year\n- If the student took the SAT, a minimum combined SAT score of 1080 ...",
 "metadata": {"site": "catalog.nyit.edu", "doc_id": "page-0c112f43a5", "page_type": "program",
   "school": "College of Engineering and Computing Sciences", "program": "Computer Science, B.S.",
   "degree": "B.S.", "level": "undergraduate", "catalog_year": "2026-2027",
   "heading_path": ["Admission Requirements"],
   "breadcrumbs": ["New York Tech Catalog 2026–2027", "Schools and Colleges", "College of Engineering and Computing Sciences", "Undergraduate Programs", "Computer Science, B.S."],
   "campuses": ["Long Island", "New York City"], "published": null, "modified": null,
   "event_start": null, "event_end": null, "dup_group": "page-a4a8143522-s005", "dup_count": 3}}
```

`dup_group` / `dup_count` appear only on passages that other programs repeat nearly word for
word (here the same admission rules on three engineering programs): show one result per group.

`corpus.jsonl`, a course:
```json
{"id": "course-CSCI-260-UG", "source": "course",
 "url": "https://catalog.nyit.edu/courses?cal=UG&cc=CSCI-260&cs=CSCI",
 "title": "CSCI 260: Data Structures",
 "text": "CSCI 260: Data Structures\nComputer Science (CSCI), undergraduate course, 3.0 credits, College of Engineering and Computing Sciences.\nThe classic data structures, such as stacks, queues, linked lists, binary trees, etc. are studied. ...\nPrerequisites: CSCI 185.\nHours (class-lab-credits): 3-0-3",
 "metadata": {"site": "catalog.nyit.edu", "course_id": "CSCI 260|UG", "code": "CSCI 260", "subject": "CSCI",
   "subject_name": "Computer Science", "level": "undergraduate", "credits": "3.0",
   "school": "College of Engineering and Computing Sciences", "catalog_year": "2026-2027"}}
```

`corpus.jsonl`, a PDF document page:
```json
{"id": "doc-6076d48a8a-p0001", "source": "document",
 "url": "https://aaupatnyit.nyit.edu/wp-content/uploads/sites/3/2026/10/AAUP_DLClassroomTaskForceReportandRecommendationsJuly2018.pdf#page=1",
 "title": "DL task force report, June 2018 (v2) (PDF, p. 1)",
 "text": "DL task force report, June 2018 (v2)\nDL Classroom Task Force Report\nand Recommendations\nJuly 2018\nHistory of the Task Force\n...",
 "metadata": {"site": "aaupatnyit.nyit.edu", "pdf_url": "https://aaupatnyit.nyit.edu/wp-content/uploads/sites/3/2026/10/AAUP_DLClassroomTaskForceReportandRecommendationsJuly2018.pdf",
   "page": 1, "page_count": 6, "found_on": ["https://aaupatnyit.nyit.edu/"], "toc_path": [],
   "modified": "2018-10-10T15:01:47-04:00", "doc_category": "report", "edition": "2018-07",
   "doc_year": 2018, "cohort_year": null, "superseded": false, "scanned": false, "source_weight": 0.5,
   "file_type": "pdf"}}
```

`pages.jsonl`:
```json
{"doc_id": "page-0c112f43a5", "url": "https://catalog.nyit.edu/engineering/computer_science",
 "title": "Computer Science, B.S.",
 "page_title": "Computer Science, B.S. | College of Engineering and Computing Sciences | New York Tech Catalog 2026–2027",
 "breadcrumbs": ["New York Tech Catalog 2026–2027", "Schools and Colleges", "College of Engineering and Computing Sciences", "Undergraduate Programs", "Computer Science, B.S."],
 "catalog_year": "2026-2027", "page_type": "program", "area": "Schools and Colleges",
 "school": "College of Engineering and Computing Sciences", "program": "Computer Science, B.S.",
 "degree": "B.S.", "level": "undergraduate", "campuses": ["Long Island", "New York City"],
 "headings": ["Concentrations in Artificial Intelligence, Big Data Management and Analytics, or Network Security", "Co-op (Cooperative Education) Option", "..."],
 "text": "... Computer science is the stimulating force at the center of the information revolution ...",
 "word_count": 1814, "num_tables": 0,
 "links": [{"text": "work full-time for six months", "url": "https://catalog.nyit.edu/policies_and_rules/academic_policies"}, "..."],
 "content_sha1": "90631dab1fac8910e2197e8c6ec499221a4c98fa", "fetched_at": "2026-11-23T07:14:57+00:00",
 "duplicate_of": null, "site": "catalog.nyit.edu", "description": null, "published": null,
 "modified": null, "event_start": null, "event_end": null, "noindex": false}
```

`sections.jsonl`:
```json
{"section_id": "page-0c112f43a5-s006", "doc_id": "page-0c112f43a5",
 "url": "https://catalog.nyit.edu/engineering/computer_science#admissions_csbs_ecebs",
 "page_title": "Computer Science, B.S.", "heading": "Admission Requirements",
 "heading_path": ["Admission Requirements"],
 "text": "This program has specific admission requirements in addition to our general requirements.\nFirst Year\n- If the student took the SAT, ...",
 "word_count": 245, "order": 6, "page_type": "program",
 "school": "College of Engineering and Computing Sciences", "program": "Computer Science, B.S.",
 "degree": "B.S.", "level": "undergraduate", "catalog_year": "2026-2027",
 "breadcrumbs": ["New York Tech Catalog 2026–2027", "..."], "site": "catalog.nyit.edu",
 "campuses": ["Long Island", "New York City"], "duplicate_of": null}
```

`courses.jsonl`:
```json
{"course_id": "CSCI 260|UG", "code": "CSCI 260", "subject": "CSCI", "subject_name": "Computer Science",
 "number": "260", "title": "Data Structures", "credits": "3.0", "credits_min": 3.0, "credits_max": 3.0,
 "school": "College of Eng & Comp Sciences", "level": "undergraduate", "level_code": "UG",
 "description": "The classic data structures, such as stacks, queues, linked lists, binary trees, etc. are studied. Sorting and searching are stressed. Computational analysis is also studied.",
 "prerequisites": "CSCI 185.", "corequisites": null, "hours": "3-0-3",
 "attributes": {"Prerequisite Course(s)": "Prerequisite: CSCI 185.", "Classroom Hours - Laboratory and/or Studio Hours – Course Credits": "3-0-3"},
 "text": "CSCI 260: Data Structures\nComputer Science (CSCI), undergraduate course, 3.0 credits, ...",
 "url": "https://catalog.nyit.edu/courses?cal=UG&cs=CSCI", "catalog_year": "2026-2027"}
```

`documents.jsonl`:
```json
{"doc_id": "doc-5460c182eb-p0001", "site": "files.nyit.edu",
 "url": "https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20242025.pdf#page=1",
 "pdf_url": "https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20242025.pdf",
 "title": "Student Handbook 2024–2025",
 "found_on": ["https://www.nyit.edu/medicine/college-of-osteopathic-medicine/student-resources/"],
 "page": 1, "page_count": 124, "toc_path": ["Introduction", "Outcomes"],
 "text": "Introduction\nUpdate Effective June 1, 2024\nThe information contained in this handbook is subject to change ...",
 "word_count": 290, "file": "www.nyit.edu/nyitcom_studenthandbook20242025__4564682643.pdf",
 "file_type": "pdf", "modified": "2025-06-02T15:48:11-04:00", "doc_category": "policy_handbook", "edition": "2024-25",
 "doc_year": 2025, "series": "files.nyit.edu nyitcom student handbook", "superseded": true,
 "superseded_by": "https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20252026.pdf",
 "cohort_year": null, "scanned": false}
```

The page records of a Word, Excel or PowerPoint file look the same, with `"file_type": "docx"`
(`"xlsx"`, `"pptx"`; `"pdf"` for a PDF, placed after `file`): `pdf_url` is the document's URL
whatever its type, `toc_path` holds the heading path of a Word page, the sheet name of an Excel
page (empty for a default name such as "Sheet1") or the title of a PowerPoint slide, and
`scanned` is always `false`. An Excel row under column headings reads, for example (from a
class schedule, shortened at the end): `Acad Group: ASCI | Acad Org: Biological & Chemical
Sciences | Term: 2026FA | Session: S | Subject: BIOL | Catalog: 110 | Section: M03 | Campus: MA |
Descr: General Biology I | DAYS: M | Mtg Start: 17:30 | ...`.

`pdf_pages.jsonl`:
```json
{"doc_id": "pdf-2025-2026-undergraduate-p0120", "edition": "2025-2026", "catalog": "undergraduate",
 "page": 120, "page_count": 318,
 "url": "https://catalog.nyit.edu/files/catalog/archives/NYITUndergraduateCatalog_AY2526.pdf#page=120",
 "toc_path": ["Policies and Rules", "Financial Aid: Undergraduate Students", "Transfer of Post-9/11 GI BILL Benefits to Dependents"],
 "section": "Transfer of Post-9/11 GI BILL Benefits to Dependents",
 "text": "Financial Aid Undergraduate\nTransfer of Post-9/11 GI BILL® Benefits to Dependents\n...",
 "word_count": 699, "pdf_file": "NYITUndergraduateCatalog_AY2526.pdf"}
```

### 8.3 How to load the files

All files are UTF-8. **Always pass `encoding="utf-8"`** when you open them yourself; on Windows
Python's default encoding is not UTF-8.

Plain Python (no extra packages):

```python
import json

with open("data/processed/corpus.jsonl", encoding="utf-8") as fh:
    corpus = [json.loads(line) for line in fh]

print(len(corpus))                      # 41725
catalog = [r for r in corpus if r["metadata"]["site"] == "catalog.nyit.edu"]
print(len(catalog))                     # 4893
cs_bs = [r for r in corpus if r["metadata"].get("program") == "Computer Science, B.S."]
print(len(cs_bs))                       # 55 passages (21 catalog + 34 www.nyit.edu)
```

pandas (`pip install pandas`; loading everything needs about 1.3 GB of RAM):

```python
import pandas as pd

sections = pd.read_json("data/processed/sections.jsonl", lines=True)    # 29,100 rows x 19 columns
pages = pd.read_json("data/processed/pages.jsonl", lines=True)          # 8,231 rows x 28 columns
courses = pd.read_csv("data/processed/courses.csv")                     # 3,389 rows x 20 columns
corpus = pd.read_json("data/processed/corpus.jsonl", lines=True)
meta = pd.json_normalize(corpus["metadata"])                            # metadata as columns

print(sections["site"].value_counts().head())
print(courses[courses["subject"] == "CSCI"][["code", "title", "credits", "level"]].head())
print(corpus["source"].value_counts())    # section 28858, document 9822, course 3045
```

With Python's `csv` module, open `courses.csv` with `encoding="utf-8-sig"` because the file
starts with a byte-order mark for Excel. In Excel, just double-click the file.

### 8.4 Sharing the data with teammates

Who needs what:

| Teammate | Files |
|---|---|
| Indexing / BM25 / reranker | `corpus.jsonl` (required; it carries all the metadata they need); optionally `pages.jsonl` for the full page text |
| Evaluation (labeled questions) | `corpus.jsonl` (ids and URLs to label against); `courses.csv` to browse courses |
| Report and presentation | `STATS.md`, `stats.json`, `validation_report.json`, `docs/METHODOLOGY.md` |
| Somebody who wants to change the parsers | the whole folder including `data/raw/html` (911 MB, about 235 MB compressed). The documents (`data/raw/docs`, 1.87 GB) are only needed to re-extract them. |

Sizes against GitHub's limits: GitHub warns about files over 50 MiB and **rejects files over
100 MiB**. The largest file, `corpus.jsonl`, is 74.7 MB (71.3 MiB): it gets a warning but is
accepted. With `--corpus-with-pdf` it grows to 78.4 MB (74.7 MiB), still under the limit. Every
other file is under 50 MB (the next largest, `documents.jsonl`, is 45.4 MB). The `.gitignore` already keeps the big caches (`data/raw/html`,
`data/raw/pdf`, `data/raw/docs`) and `data/logs` out of git.

Options:

1. **A zip file on Google Drive, OneDrive or Teams (simplest).** `data/processed` compresses from
   222 MB to about 47 MB, and `corpus.jsonl` alone to about 15 MB.

   Windows (PowerShell):
   ```powershell
   Compress-Archive -Path data\processed -DestinationPath askbear-processed-2026-11-23.zip
   ```
   macOS / Linux:
   ```bash
   (cd data && zip -qr ../askbear-processed-2026-11-23.zip processed)
   ```
   If `zip` is not installed (some Linux systems), Python's own zip tool makes the same archive
   on every system: `python -m zipfile -c askbear-processed-2026-11-23.zip data/processed`
   (and `python -m zipfile -e askbear-processed-2026-11-23.zip data` unpacks it).
   Teammates unzip it into the `data` folder of their copy, so the files end up in
   `data/processed/`. In PowerShell: `Expand-Archive askbear-processed-2026-11-23.zip -DestinationPath data`.
   On macOS/Linux: `unzip askbear-processed-2026-11-23.zip -d data`.
2. **A GitHub repository with the code and `data/processed`.** The folder is not a git
   repository yet. Create an empty repository on GitHub (private is best), then:
   ```bash
   git init
   git add .
   git commit -m "AskBear scraper and dataset (crawl of 2026-11-23)"
   git branch -M main
   git remote add origin https://github.com/<your-team>/<your-repo>.git
   git push -u origin main
   ```
   The push includes about 41 MB of crawl manifests from `data/raw/`. They are small enough, and
   they let a teammate re-run `parse` if they also get `data/raw/html`. On such a clone,
   `validate --strict` ends with `37 PASS, 7 WARN, 0 FAIL, 2 SKIP`: the two checks that read the
   cached HTML ("Course deep links", "Section #anchor links") are skipped, which is expected.
3. **Git LFS**, if your team wants versioned large files. Run `git lfs install` and
   `git lfs track "data/processed/*.jsonl"` *before* `git add`, and commit `.gitattributes`.
   GitHub's free LFS storage and bandwidth are limited, so option 1 or 2 is usually enough.

---

## 9. Hand-off to the indexing, BM25 and reranker teammates

### 9.1 Which file to index

Index **`data/processed/corpus.jsonl`**. It holds one passage per line, 41,725 in total:

| `source` | Records | What a record is | Typical length |
|---|---:|---|---|
| `section` | 28,858 | one heading-based part of a web page (18 NYIT websites) | median 82 words, max 480 |
| `document` | 9,822 | one page of a document linked from an NYIT page: 8,894 PDF pages, 746 Excel, 121 Word and 61 PowerPoint pages from 1,289 documents (1,177 PDFs, 59 Excel, 50 Word, 3 PowerPoint files) | median 290 words, max 4,256 |
| `course` | 3,045 | one course of the catalog's course directory | median 74 words, max 387 |
| `pdf_page` | 0 (1,015 with `parse --corpus-with-pdf`) | one page of a full catalog PDF; 282 of the 1,297 pages are near-copies of pages kept (mostly the 2025-2026 copy of a page that the 2026-2027 edition repeats) and are left out | |

Fields of every record:

| Field | Use it for |
|---|---|
| `id` | unique, stable passage id. Use it in your index and in the evaluation labels (qrels). Formats: `page-<hash>-s006` (section), `course-CSCI-260-UG` (course), `doc-<hash>-p0001` (PDF page), `pdf-2025-2026-undergraduate-p0120` (catalog PDF page). Ids never contain spaces, so they go into TREC run and qrels files (trec_eval, Pyserini, ir_measures, ranx) as they are. |
| `url` | the link shown next to the answer. It points to the passage itself when possible: `#anchor` for sections, `#page=N` for PDFs (on a Word, Excel or PowerPoint file `#page=N` only names the passage: the browser downloads the file), `?cc=CSCI-260` opens the course description. |
| `title` | a short display title, for example "Computer Science, B.S. — Admission Requirements" or "CSCI 260: Data Structures". |
| `text` | what BM25 and the reranker should read. It starts with context: the page title and heading path for sections, the course header for courses, the document title and bookmark path for PDF pages (heading path, sheet name or slide title for Word, Excel and PowerPoint pages). |
| `metadata` | a dict of filters and extra features (below). |

### 9.2 Useful metadata filters

| Filter | Where | Values |
|---|---|---|
| `metadata["site"]` | every record | 19 websites, for example `catalog.nyit.edu` (official catalog), `www.nyit.edu`, `apps.nyit.edu` (policies), `libanswers.nyit.edu` |
| `source` (top level) | every record | `section`, `course`, `document` (`pdf_page` with `--corpus-with-pdf`) |
| `metadata["page_type"]` | sections | `program`, `curriculum`, `degree`, `policy`, `financial_aid`, `tuition`, `faq`, `news`, `event`, `bio`, `library_guide`, `library_hours`, `communication`, `page`, ... (27 types in the corpus; `pages.jsonl` has 29, with `course_index` and `course_listing`, whose pages get no passages; see the data dictionary) |
| `metadata["school"]` | sections, courses | one of the 6 schools, for example `College of Engineering and Computing Sciences`, or `null` |
| `metadata["level"]` | sections, courses | `undergraduate`, `graduate`, `doctoral`, `professional`, or `null` (not level-specific) |
| `metadata["program"]`, `["degree"]` | sections | for example `Computer Science, B.S.` / `B.S.` |
| `metadata["catalog_year"]` | catalog sections, courses | `2026-2027` |
| `metadata["campuses"]` | sections | the campuses the page *mentions*: `Long Island`, `New York City`, `Jonesboro`, `Vancouver` (empty list when none) |
| `metadata["published"]`, `["modified"]` | sections; `modified` on documents | ISO dates. `published` is a publication date only (never an import or update date): news, blog posts, magazine issues (the first day of the issue's season), e-mail announcements, and most WordPress pages of www.nyit.edu and vancouver.nyit.edu (the date the page was first published). Bio and event pages have none. |
| `metadata["event_start"]`, `["event_end"]` | sections of event pages | when the event takes place (ISO, local time); `event_end` is `null` for all-day events. Prefer upcoming events with it. |
| `metadata["dup_group"]`, `["dup_count"]` | any record whose passage is repeated nearly word for word for other programs, courses or entering classes | the group id and its size. **Show one result per `dup_group`.** 2,207 records in 704 groups. |
| `metadata["doc_category"]`, `["source_weight"]` | documents | the kind of PDF (`policy_handbook`, `form`, `semester_map`, `schedule_calendar`, `transfer_agreement`, `brochure`, `report`, `course_material`, `event_program`, `newsletter_magazine`, `financial_filing`, `training`, `other`) and a weight from 0.2 (financial filings) to 1.0 (policies and handbooks) |
| `metadata["edition"]`, `["doc_year"]`, `["cohort_year"]`, `["superseded"]`, `["scanned"]` | documents | the year or term the file name gives (`2024-25`, `Fall 2020`), the last year it covers, the entering class of semester maps and class calendars, whether a newer edition exists (only with `--corpus-with-superseded`), whether it is a scan |
| `metadata["file_type"]` | documents | `pdf`, `docx` (Word), `xlsx` (Excel) or `pptx` (PowerPoint); the title says it too: "... (PDF, p. 3)", "... (Word, part 2)", "... (Excel, part 1)", "... (PowerPoint, slide 4)" |
| `metadata["edition"]`, `["catalog"]` | `pdf_page` only | `2025-2026` or `2026-2027`; `undergraduate`, `graduate`, `osteopathic` |

**Recommended use:**
- Collapse results by `dup_group` before you show the top 3 (or count them once in the metrics).
- Use `source_weight` (or `doc_category`) as a reranker feature or a tie-breaker, so that a
  handbook page beats an old newsletter or a tax filing with similar words.
- For "which courses do I take" questions about semester maps, match `cohort_year` to the
  student's entering class.
- `published` and `event_start` help to prefer recent news and upcoming events.

Campus filter example:

```python
import json

with open("data/processed/corpus.jsonl", encoding="utf-8") as fh:
    corpus = [json.loads(line) for line in fh]

vancouver = [r for r in corpus if "Vancouver" in r["metadata"].get("campuses", [])]
print(len(vancouver))   # 2756 passages from pages that mention the Vancouver campus
```

### 9.3 A working BM25 example

This is an optional extra. Install one package into your environment:

```bash
python -m pip install rank_bm25
```

Save this as `bm25_demo.py` in the project folder and run `python bm25_demo.py`:

```python
"""AskBear hand-off demo: BM25 search over data/processed/corpus.jsonl.

Run it from the project folder:  python bm25_demo.py
Needs one extra package:         pip install rank_bm25
"""
import json
import re

from rank_bm25 import BM25Okapi

CORPUS = "data/processed/corpus.jsonl"
STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
             "from", "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "what",
             "when", "where", "which", "who", "with"}


def tokenize(text):
    """Lower-case words and numbers, without very common words."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


with open(CORPUS, encoding="utf-8") as fh:
    records = [json.loads(line) for line in fh]
print(f"indexing {len(records):,} records ...")
bm25 = BM25Okapi([tokenize(r["text"]) for r in records])


def search(query, k=3, **filters):
    """Top-k records for the query; filters match metadata, e.g. site="catalog.nyit.edu"."""
    scores = bm25.get_scores(tokenize(query))
    hits = [i for i, r in enumerate(records)
            if all(r["metadata"].get(key) == value for key, value in filters.items())]
    hits.sort(key=lambda i: scores[i], reverse=True)
    return [(records[i], scores[i]) for i in hits[:k]]


query = "What are the requirements for the Computer Science B.S.?"
for filters in ({}, {"site": "catalog.nyit.edu"}):
    print(f"\nquery: {query}  filters: {filters or 'none'}")
    for rank, (rec, score) in enumerate(search(query, **filters), 1):
        print(f"{rank}. [{rec['source']}] {rec['title']}  (BM25 {score:.1f})")
        print(f"   {rec['url']}")
```

Real output on the included corpus (rank_bm25 0.2.2; about 5 seconds and 0.7 GB of RAM):

```text
indexing 41,725 records ...

query: What are the requirements for the Computer Science B.S.?  filters: none
1. [section] Admission Requirements: Computer Science, B.S. — How To Apply  (BM25 17.3)
   https://www.nyit.edu/academics/degrees/computer-science-bs/admission-requirements/#h-how-to-apply
2. [section] Admission Requirements: Computer Science, B.S. — Transfer  (BM25 16.1)
   https://www.nyit.edu/academics/degrees/computer-science-bs/admission-requirements/#h-transfer
3. [section] Computer Science, B.S. — Admission Requirements  (BM25 16.1)
   https://www.nyit.edu/academics/degrees/computer-science-bs/

query: What are the requirements for the Computer Science B.S.?  filters: {'site': 'catalog.nyit.edu'}
1. [section] Computer Science, B.S. — Five-Year Accelerated Option  (BM25 15.6)
   https://catalog.nyit.edu/engineering/computer_science
2. [section] Electrical and Computer Engineering, B.S. — Five-Year Accelerated Option  (BM25 14.9)
   https://catalog.nyit.edu/engineering/electrical_and_computer_engineering
3. [section] Information Technology, B.S. — Five-Year Accelerated Option  (BM25 14.5)
   https://catalog.nyit.edu/engineering/information_technology
```

The second query shows why AskBear needs a reranker. Restricted to the catalog, plain BM25
prefers short passages that repeat "computer", "science" and "requirements", rather than the
catalog's actual "Admission Requirements" and curriculum passages. In the full system, take the
top ~100 from BM25 (`search(query, k=100)`), let the reranker re-score them, and show the best 3.

### 9.4 How the passages were made (what the indexer can rely on)

- **One passage = one heading section.** Pages are split at their h2-h6 headings. A "heading"
  that reads like content (a sentence, 6 or more words, a numbered step) stays text, even when
  another heading follows it directly. Sections longer than 450 words are split into parts of
  about 150-400 words, titled "... (part 2)". A section of fewer than 5 words ("TOTAL CREDITS:
  30") is merged into its neighbour (514 merged), so only a page's one and only section can be
  shorter than 5 words (there is 1), and the longest section has 464 words.
- **Special passages.** Each item on www.nyit.edu's faculty and staff accomplishments pages is a
  passage with its date ("March 2, 2021"). On the library guides, each librarian's profile box
  is a passage of its own, titled "Librarian: <name>" (name, job title, subjects, e-mail, phone,
  address). A title gets "(page N)" only when the page's URL has the page number as its one
  query parameter (`?p=3`, `?fsa_page=3`); LibGuides' `?g=...&p=...` names a tab, not a page.
- **Clean text.** No menus, footers, cookie banners, share buttons or "Request Information" blocks.
  Tables become one `cell | cell | cell` line per row. Footnote markers become `[1]`. Unicode is
  normalised (NFKC) and broken characters and HTML entities are repaired; control characters
  are dropped, and Windows-1252 punctuation stored as C1 control codes becomes the character it
  stands for (quotes, dashes); `validate` checks this. The "Previous Next" buttons of blog
  photo galleries are removed. Content that a page shows behind a drop-down, a tab or a
  "more..." toggle is included.
- **De-duplicated:**
  - Exact copies of pages (646) get no passages.
  - Passages repeated word for word on several pages of one website are kept once (2,153
    copies removed). The copy kept is on the most fitting page: not page 2, 3, ... of a
    listing, then the page whose title best matches the passage's heading, then a page that is
    not a home or listing page, then the shortest page. Excerpts on a blog's list of posts that
    repeat a post it links to are removed too.
  - Near-duplicates across the whole corpus (Jaccard of 0.9 or more over word 5-shingles) are
    kept once when they are about the same program and level: 360 copies left out (236
    sections, 124 document pages), always keeping the most authoritative source (catalog, then the
    policy library, www.nyit.edu, ..., documents last). Copies for different programs, courses or
    entering classes stay and share `dup_group` (2,207 records in 704 groups).
  - A document page repeated word for word in another document is kept once (713 copies left
    out), and older editions of yearly documents are left out (4,780 pages).
  - Pages marked `noindex` (81 records), pages with fewer than 5 words (167) and listing pages
    2, 3, ... (288) get no passages.
  - **No two corpus records have the same text** (`validate` checks this).
- **Courses:** the 344 legacy "Conversion - OM" registrar codes (0 credits, no description) stay
  in `courses.jsonl` but are left out of the corpus.

### 9.5 Tips for the search and evaluation teammates

- **Show one result per `dup_group`.** A passage that several programs repeat (accreditation
  statements, "STEM OPT" notes, deadline tables) stays once per program so that a program
  filter finds it, but the copies share `metadata["dup_group"]`. Collapse them in the top 3.
- **Catalog editions:** the web catalog and its courses are the 2026-2027 edition
  (`catalog_year`). The catalog PDFs are opt-in and include the 2025-2026 edition. If you add
  them with `--corpus-with-pdf`, filter on `metadata["edition"]` so that two editions do not compete.
- **Catalog PDF pages are opt-in** (`parse --corpus-with-pdf`, +1,015 records) because their text
  repeats the catalog website (96-98% of the 2026-2027 PDFs' phrases are on the website).
- **Long document pages:** 3,176 document records have more than 400 words, up to 4,256. Rerankers with a
  512-token limit should truncate them or split them into windows. Sections and courses are at
  most 480 words.
- **Prefer official sources when scores tie.** catalog.nyit.edu is the official catalog, and
  apps.nyit.edu holds the official policies. `metadata["site"]`, `page_type` and, for PDFs,
  `source_weight` make good reranker features.
- **Dates:** `metadata["published"]` is a publication date (news, blog posts, e-mail
  announcements, most WordPress pages of www.nyit.edu and vancouver.nyit.edu; magazine articles
  get the first day of their issue's season). Event pages have
  `event_start` / `event_end` instead, and bio pages have no date. Library opening hours
  (`page_type` `library_hours`) are a snapshot of the crawl date: their title says "as of
  November 23, 2026".
- **Freeze the dataset for evaluation.** Section ids are `<doc_id>-s<NNN>`, where `doc_id` is a
  hash of the page URL. They stay the same as long as the page and its sections do not change.
  Keep one copy of `corpus.jsonl` (with the crawl date in the name) for labeling and evaluation.
  If you must re-crawl, also store the `url` and a text snippet with each label so you can map
  labels to new ids. Course ids changed format in this version (`course-CSCI-260-UG`, formerly
  `course:CSCI 260|UG`); `metadata["course_id"]` keeps the old `CSCI 260|UG` form.
- **Be careful what the demo shows.** Passages are NYIT's text as published: staff e-mail
  addresses and phone numbers (2,696 records with an e-mail address, the librarian profile
  passages) and, in one public slide deck, a default first-time password (see
  [7.3](#73-politeness-and-ethics)). Check the passages before you put them on slides or in
  screenshots, and consider leaving `doc-a20a98e1ad-p0003` out of demo answers.

---

## 10. Results compared with the proposal

### 10.1 What was collected

| Measure | Value |
|---|---|
| Websites with parsed pages | 18 (26 hosts with saved pages: 6 hosts disabled after the main crawl are not parsed, and the legacy hosts nyit.edu and site.nyit.edu redirect to other sites) |
| URLs visited / pages saved | 11,864 / 8,677 (crawl of 2026-11-23 in five runs: 5 h 14 min and 13,566 requests in total, see 6.2) |
| Page records / distinct pages | 8,231 / 7,585 (646 exact copies; 81 noindex records) |
| Words on distinct pages | 4,017,908 (median 331 per page) |
| Search passages (sections) | 29,100 (median 72 words, max 464; 10,938 with their own `#anchor` link) |
| Courses | 3,389 records = 3,384 unique codes in 208 subjects (UG 1,274, GR 1,095, professional 1,020) |
| Catalog programs | 127 programs on 91 program pages + 252 curriculum pages; 272 degree pages on other sites |
| Documents from the sites | 1,682 distinct documents (1,567 PDFs, 52 Word, 60 Excel, 3 PowerPoint files; 1,742 downloaded links), 17,648 pages, 4,383,801 words; 146 scanned PDFs; 241 older editions |
| Full catalog PDFs | 6 PDFs, 1,297 pages (2025-2026: 318 + 263 + 61; 2026-2027: 329 + 262 + 64) |
| Search corpus | 41,725 records: 28,858 sections, 9,822 document pages, 3,045 courses |
| Quality checks | 46 checks: 39 PASS, 7 WARN, 0 FAIL |

Pages, passages and corpus records per website:

| Website | Pages | Distinct | Words | Passages | Corpus records |
|---|---:|---:|---:|---:|---:|
| catalog.nyit.edu | 1,067 | 756 | 528,289 | 1,881 | 4,893 |
| www.nyit.edu | 3,358 | 3,292 | 1,702,253 | 16,093 | 17,150 |
| blogs.nyit.edu | 1,467 | 1,437 | 559,361 | 2,250 | 2,341 |
| libguides.nyit.edu | 1,321 | 1,091 | 473,442 | 3,917 | 3,867 |
| libanswers.nyit.edu | 287 | 287 | 29,000 | 298 | 297 |
| apps.nyit.edu | 205 | 205 | 501,139 | 2,639 | 2,634 |
| events.nyit.edu | 108 | 108 | 12,589 | 102 | 100 |
| comapp.nyit.edu | 100 | 100 | 20,309 | 223 | 224 |
| vancouver.nyit.edu | 97 | 95 | 46,748 | 612 | 592 |
| online.nyit.edu | 65 | 64 | 71,854 | 683 | 691 |
| magazine.nyit.edu | 59 | 58 | 33,806 | 138 | 136 |
| deseversky.nyit.edu | 35 | 32 | 9,697 | 81 | 83 |
| biggive.nyit.edu | 29 | 29 | 6,832 | 77 | 77 |
| files.nyit.edu | 15 | 13 | 16,774 | 55 | 8,280 |
| libcal.nyit.edu | 14 | 14 | 3,900 | 29 | 29 |
| aaupatnyit.nyit.edu | 2 | 2 | 1,786 | 17 | 324 |
| eportfolios.nyit.edu, wifi.nyit.edu | 2 | 2 | 129 | 5 | 5 |
| nyit.edu (documents only) | 0 | 0 | 0 | 0 | 2 |
| **total** | **8,231** | **7,585** | **4,017,908** | **29,100** | **41,725** |

Corpus records include the document pages served by each host. That is why files.nyit.edu
(1,498 documents) and aaupatnyit.nyit.edu (46 PDFs) have many more corpus records than passages.
1,067 catalog page records come from the catalog's own navigation (649) and from
`/curriculum/<slug>` (243) and `/semester-map/<slug>` (175) pages that www.nyit.edu links to.
Most `/curriculum/` pages are word-for-word copies of the navigation's curriculum pages and are
marked `duplicate_of`. A few websites have fewer corpus records than passages: those passages
are near-copies of a passage kept on a more authoritative page (`duplicate_of` in
sections.jsonl). blogs.nyit.edu and libguides.nyit.edu grew the most compared with the main
crawl alone (200 and 400 pages): the catch-up crawl added the older blog posts and the library
guides beyond the old cap.

### 10.2 Proposal vs. scraped

The proposal (slide 9) planned "one public source: the NYIT catalog", measured on the 2025-26
undergraduate and graduate catalog PDFs. The scope was later widened to all public NYIT websites
and the documents they link to.

| Metric | Proposal | Scraped | Note |
|---|---:|---:|---|
| Catalog PDF pages, 2025-26 (UG + GR) | 581 | **581** (318 + 263) | exact |
| Words in those PDFs | ~249K | **249,274** | +0.1%; 249,308 after cleaning |
| Course codes | 1,200+ | **2,369** UG + GR | 3,384 including professional (medicine) courses; from the 2026-27 website |
| Subject areas | 100 | **159** UG + GR | 208 including professional |
| Schools and colleges | 6 | **6** | all six have catalog pages |
| Campuses | 4 | **4** | Long Island, New York City, Jonesboro, Vancouver |
| Websites | 1 | **18** | catalog + www + 16 more |
| Documents | n/a | **1,682** | PDF, Word, Excel and PowerPoint files linked from the websites |
| Searchable passages | n/a | **41,725** | sections + courses + document pages |

### 10.3 The 7 validation warnings explained

`validate` ends with `39 PASS, 7 WARN, 0 FAIL`. None of the warnings is a bug in the data;
each one describes the websites or a known gap on NYIT's side (7.4):

1. **Website page limits and failures:** no website reached its page limit, but 7 NYIT hosts
   could not be reached: bearcat.nyit.edu, camstream.nyit.edu, hub.nyit.edu, iris.nyit.edu,
   nyitonline.nyit.edu, tbls.nyit.edu and www.imodules.nyit.edu. The crawler gave up on
   iris.nyit.edu after repeated failures (3 queued URLs not crawled).
2. **Crawl errors:** 1,928 URLs could not be fetched:
   - 1,879 are HTTP 404 broken links on NYIT's own pages (www.nyit.edu 1,662, blogs.nyit.edu
     127, libguides 27, ...). Most of the www.nyit.edu ones are old news and bio addresses
     found on older blog posts.
   - 7 catalog faculty pages (`/administration_and_faculty/faculty`, `/medicine/faculty`, ...)
     return HTTP 500 on NYIT's side.
   - 19 redirect in a circle (status `redirect_loop`, cached and not retried).
   - The other 23 are 14 connection failures to the unreachable hosts, 7 HTTP 500 pages on the
     disabled catalog-dev copy, 1 HTTP 503 (comapp.nyit.edu/viscenter) and 1 HTTP 400
     (oaproxy-sso.nyit.edu, a sign-in host reached by the main crawl).
   Skipped on purpose and not counted: 125 redirects to non-NYIT or sign-in sites and 13 empty
   "soft 404" pages.
3. **Page titles and text:** 116 pages have no text (www 24, comapp 25, libguides 22, biggive
   12, blogs 12, deseversky 8, events 6, vancouver 5, libanswers 2): image galleries, video and
   script-only pages, empty templates. They get no passages.
4. **Specific page titles:** 11 pages on comapp.nyit.edu share the title "Updates from the OMC".
5. **Short pages (< 20 words):** 237 pages (catalog 50, libguides 35, www 32, comapp 28, blogs
   25, libanswers 21, events 16, biggive 12, deseversky 8, vancouver 7, apps 2, magazine 1),
   mostly empty semester-map placeholders, thank-you pages and link hubs.
6. **Course listings produced courses:** 8 of 267 subject pages list no courses on NYIT's site
   (for example ARCHB, ARCHC, PHRM).
7. **Text hygiene, PDF documents:** 1 of 17,648 document pages literally contains a `<sup>`
   tag, as printed in the PDF.

The earlier warnings about magazine articles without dates and about paginated listings that
were not crawled are gone: magazine articles carry their issue's date (48 of 50 articles; the
two annual reports name no season), and the catch-up crawl fetched every listing page.

---

## 11. Troubleshooting

| Problem | What to do |
|---|---|
| `No module named askbear_scraper` | You are not in the project folder. `cd` into the folder that contains `askbear_scraper/`, and activate the environment. |
| `No module named requests` (or `bs4`, `lxml`, `pymupdf`, `tqdm`) | The environment is not active, or the packages are not installed: activate `.venv`, then run `python -m pip install -r requirements.txt`. |
| PowerShell: `Activate.ps1 cannot be loaded because running scripts is disabled` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` (current window only), or use `cmd.exe` with `.venv\Scripts\activate.bat`, or call `.\.venv\Scripts\python.exe` directly. |
| Windows: `python` opens the Microsoft Store or "Python was not found" | Use the `py` launcher (`py -3.12 -m venv .venv`) or reinstall Python with "Add python.exe to PATH" ticked. Inside the activated environment `python` always works. |
| Wrong Python version (`SyntaxError`, `TypeError: 'type' object is not subscriptable`, packages refuse to install) | Check with `python --version`. You need 3.9 or newer; 3.11-3.13 is best. Delete `.venv`, then recreate it with the right Python (`py -3.12 -m venv .venv` or `python3.12 -m venv .venv`). |
| `pip` fails with SSL or certificate errors | Usually a campus or company network intercepts HTTPS. Try another network (for example a phone hotspot), or ask IT for the proxy and use `python -m pip install --proxy http://proxy.example:8080 -r requirements.txt`. Upgrading pip (`python -m pip install --upgrade pip certifi`) also helps. |
| `pip` tries to compile `lxml` or `pymupdf` and fails | Upgrade pip first so it can use the ready-made wheels. Very new Python versions sometimes have no wheels yet, so use Python 3.12 or 3.13. |
| `pip` is slow, or fails with "No space left on device" / "Disk quota exceeded" | PyMuPDF 2.x pulls in about 330 MB of packages (`onnxruntime`, `numpy`, ...). Free about 0.5 GB, or point pip's temporary folder to a bigger drive (`TMPDIR` on macOS/Linux, `TEMP` on Windows). |
| You need the exact outputs of the included dataset (same text, same ids) | Install the pinned versions: `python -m pip install -r requirements.txt -c constraints.txt` (Python 3.11 or newer), then run `parse`. |
| The crawler stops or you pressed Ctrl+C | Run **the same command again**. Every finished page is saved, and the crawl resumes. Exit code 1 means "interrupted" (or see the next row), and `all` stops after such a crawl. |
| `crawl` ends with `the crawl saved no page of <host>` and exit code 1 | That website could not be reached, or its robots.txt could not be read (the crawler then fetches nothing from it, and tries again after 60 and 300 seconds). See `failed_sites` and `unreachable_reasons` in `crawl_summary.json`, wait, and run `crawl --site <host>` later; cached pages are not downloaded again. `validate` FAILs until it has pages. |
| The crawl seems stuck on libguides, libanswers or libcal | That is the robots.txt `Crawl-delay` of 10 seconds, honoured on purpose. libguides needs about 4 hours for its ~1,375 URLs (10-11 seconds each), but the other sites are crawled in between. Do not lower it. |
| `HTTP 500` on 7 catalog faculty pages | NYIT's server fails on those pages (known; `validate` labels them "known NYIT-side problem"). Nothing to fix. |
| Hundreds of `not_found` (404) URLs | Broken links on NYIT's own pages (1,879 in the included crawl, most of them old www.nyit.edu news and bio addresses found on older blog posts). Normal. |
| `TooManyRedirects` errors, or status `redirect_loop` | Redirect loops on NYIT's side (19 URLs in the included crawl, all recorded as `redirect_loop`). The crawler stops a loop at the first repeated URL, records `redirect_loop` and does not ask again. An older crawler version reported them as `error` with `TooManyRedirects`. Normal. |
| A test crawl ran on the real `data/` folder by mistake | Nothing is lost: other cached pages stay in the manifest, and the link graph, external links and `crawl_summary.json` are merged. The test only added its pages, which belong to the real websites anyway, and its run to `runs` in `crawl_summary.json`. (An older version of the crawler did rewrite the manifest; that no longer happens.) |
| `crawl_summary.json` shows the settings or the time of a small run only | After `crawl --site X`, `last_run` and `settings` describe that run, while `sites`, `status_by_site` and the totals cover every website (the counts of X are taken from the merged manifest); `runs` lists every run with its time and requests, and `seconds` and `network_requests` add them up. |
| The log says `cannot read robots.txt of iris.nyit.edu` (or bearcat, camstream, hub, nyitonline, tbls, www.imodules) | These NYIT hosts could not be reached on 2026-11-23 either: their names do not resolve, they time out or their secure connection fails. The crawler tries 3 times, waits, then leaves them alone; the other websites go on. Normal. |
| A page could not be saved (`could not save the page ...` in the manifest) | Usually a path that is too long on Windows (see 4.1) or a full disk. The crawl goes on; fix the cause and run `crawl` again, which retries the page. |
| Not enough disk space | A full rebuild needs about 3.1 GB. Use `docs --max-mb 10` to skip big files, `all --skip-docs` to skip the documents (PDF, Word, Excel, PowerPoint) entirely, or `--data-dir` on a bigger drive. |
| You changed a parser or want to rebuild only the processed files | Run `python -m askbear_scraper parse`, then `stats` and `validate`. No network is needed (about 9 minutes). |
| You want fresh copies of the pages | Use `--refresh` with `crawl` (about 4 hours) and optionally `docs` (1.87 GB). See 6.4. |
| `validate` shows 2 SKIP on a copy of the project | Expected on a copy without `data/raw/html` (for example a git clone): "Course deep links" and "Section #anchor links" need the cached pages. |
| `PermissionError` writing `courses.csv` | The file is open in Excel. Close it and run `parse` again; `courses.jsonl` is complete anyway. |
| `UnicodeEncodeError` when saving output to a file on Windows (`... > out.txt`) | Set UTF-8 mode for the window: `$env:PYTHONUTF8 = "1"`. In your own scripts, open files with `encoding="utf-8"`. |
| Windows: `FileNotFoundError` or "path too long" inside `data\raw` | The project folder path is too long. Move it to a short path such as `C:\NYITSCRAPE`, or enable long paths (`LongPathsEnabled`, see 4.1). |
| OneDrive or antivirus locks files during a run | Pause syncing, or move the project out of OneDrive. |
| Loading the corpus with pandas runs out of memory | Read it line by line with `json` (see 8.3), or load only the columns you need. |
| `validate` shows a FAIL | Read the line and its examples. The most common cause is outputs from different runs, for example `pdf_summary.json` without `pdf_pages.jsonl`. Re-run the step it names, then `parse`, `stats` and `validate`. |

Timestamps in all files are UTC (`+00:00`). Add `-v` to any command for debug messages and
`--log-file FILE` to keep a log.

---

## 12. Project structure, module tour, extending, FAQ

### 12.1 Project structure

```text
NYITSCRAPE/
|-- README.md                       this guide
|-- requirements.txt                Python packages (pip install -r requirements.txt)
|-- constraints.txt                 the exact versions that built the dataset (pip ... -c constraints.txt)
|-- pyproject.toml                  packaging metadata (optional: pip install -e .)
|-- pytest.ini                      test settings (python -m pytest -q)
|-- .gitignore                      keeps the 2.8 GB of caches and logs out of git
|-- CSCI 426 IR GROUP PROJECT.pdf   the project proposal slides
|-- docs/
|   |-- DATA_DICTIONARY.md          every field of every output file
|   |-- METHODOLOGY.md              how the data was collected (for the report)
|   `-- TEAM_FAQ.md                 short answers to teammates' questions
|-- askbear_scraper/                the Python package: python -m askbear_scraper ...
|   |-- __init__.py                 package description and version (1.0.0)
|   |-- __main__.py                 entry point of python -m askbear_scraper
|   |-- cli.py                      command line: sites, crawl, pdf, docs, parse, stats, validate, all, prune
|   |-- config.py                   global settings: delays, data folders, catalog PDF editions, schools, campuses, proposal numbers
|   |-- sites.py                    which NYIT websites are crawled, their URL rules and selectors, and why others are not
|   |-- urls.py                     URL normalisation, scope (page/pdf/asset/excluded/disabled/external), cache file names
|   |-- fetcher.py                  polite HTTP client: per-site delay, robots.txt, retries, User-Agent
|   |-- sitemaps.py                 XML sitemap reader (sitemap indexes, gzip)
|   |-- crawler.py                  multi-site crawler with the on-disk cache and the manifest
|   |-- html_parser.py              catalog page parser + the shared DOM walker and passage splitter
|   |-- site_parser.py              parser for every other NYIT website (templates, boilerplate, dates)
|   |-- course_parser.py            catalog course directory -> courses
|   |-- pdf_catalog.py              full catalog PDFs -> pdf_pages.jsonl
|   |-- documents.py                PDFs and Office files linked from NYIT pages -> documents.jsonl
|   |-- office.py                   text of Word/Excel/PowerPoint files (.docx/.xlsx/.pptx, stdlib only)
|   |-- pipeline.py                 the parse step: pages, sections, courses, corpus, de-duplication
|   |-- models.py                   record types (PageRecord, SectionRecord, CourseRecord, ...)
|   |-- stats.py                    statistics report (STATS.md, stats.json)
|   |-- validate.py                 the 46 quality checks (validation_report.json)
|   `-- utils.py                    text cleaning, hashing, JSONL reading/writing, logging
|-- tests/                          pytest suite (1,132 tests, all offline except 1 live test)
|                                   + fixtures/ with real (trimmed) and synthetic NYIT pages;
|                                   test_compat.py guards Python 3.9 compatibility
`-- data/
    |-- raw/                        downloaded material (see 8.1)
    |-- processed/                  the finished dataset (see 8.1)
    `-- logs/                       logs of the runs that built the dataset
```

### 12.2 Module tour (how a page travels through the code)

1. **`sites.py`** has one `SiteProfile` per host. A profile says whether the host is crawled,
   where the crawl starts (`seeds`), which sitemaps to read, which paths are allowed or excluded
   (regular expressions), the page limit (`max_pages`) and the CSS selectors of the main content.
   `profile_for(host)` also covers unknown `*.nyit.edu` hosts: sign-in and proxy hosts are
   disabled, `dev`/`staging` copies are disabled, and anything else gets `DEFAULT_PROFILE` (max 300 pages).
2. **`urls.py`** turns every link into one canonical URL per page. On the catalog, URLs are made
   lower-case, lose their trailing slash and keep only the `cs`/`cal` parameters. On WordPress
   sites they get a trailing slash and lose all parameters except those `sites.py` keeps
   (`keep_query_params`, for example the page numbers `?p=` of the blogs and `?fsa_page=` and
   `?mrp_page=` of www.nyit.edu). Tracking parameters are dropped everywhere. `link_kind()` then classifies the URL as a page to crawl, a PDF, an asset, an
   excluded path, a disabled host or an external site.
3. **`fetcher.py`** (`PoliteFetcher`) sends the requests: robots.txt, the delay per host,
   redirects followed hop by hop (each hop checked against robots.txt and the crawl scope),
   retries, timeouts and the User-Agent. **`sitemaps.py`** reads XML sitemaps through it.
4. **`crawler.py`** runs the queues: one per host, served in turn and preferring cached pages.
   It saves the HTML, appends to the manifest, recognises soft-404s, login pages, redirect loops
   and off-site redirects, follows links, and writes the link graph and the summary (merged with
   the earlier results after a partial run). `prune_cache()` behind the `prune` command cleans up
   entries `sites.py` no longer crawls.
5. **`pdf_catalog.py`** and **`documents.py`** download PDFs and extract their pages with PyMuPDF.
   They join table cells with ` | `, remove running headers, footers and page numbers, and
   repair hyphenation. They also detect scanned or unreadable pages and choose a title.
   `documents.py` also downloads the linked Word, Excel and PowerPoint files; **`office.py`**
   reads them (ZIP + XML, standard library only, with size and safety limits) and cuts them
   into pages that `documents.py` cleans like PDF text.
6. **`pipeline.py`** (`parse`) selects the cached pages from the manifest, routed by the host
   where their content lives. It then:
   - learns each site's boilerplate,
   - parses every page with **`html_parser.py`** (catalog) or **`site_parser.py`** (other sites),
   - skips error pages served with HTTP 200,
   - fills in missing catalog years and schools,
   - parses courses with **`course_parser.py`**,
   - marks duplicates, merges tiny passages and removes repeated passages,
   - adds the document pages (PDF, Word, Excel, PowerPoint), collapses near-duplicates across the corpus (`dup_group`),
   - and writes the corpus.

   **`models.py`** defines the records it writes.
7. **`stats.py`** and **`validate.py`** read the outputs and report on them.

### 12.3 How to extend

- **Add a website or change one:** edit `SITES` in `askbear_scraper/sites.py`. A minimal
  profile is enough:
  ```python
  SiteProfile(
      host="newsite.nyit.edu",
      name="New site",
      reason="what it contains",
      sitemaps=("https://newsite.nyit.edu/sitemap.xml",),  # optional
      exclude_patterns=(r"^/search",),                       # paths never to crawl
      max_pages=500,
  ),
  ```
  Then check it with `python -m askbear_scraper sites`, test it with
  `python -m askbear_scraper --data-dir quicktest crawl --site newsite.nyit.edu --max-pages 5`
  and inspect `quicktest/processed/pages.jsonl` after `parse`. Then run
  `python -m askbear_scraper crawl --site newsite.nyit.edu --contact you@nyit.edu` on the real
  data; the result is merged with the existing crawl. Finish with `docs`, `parse`, `stats` and
  `validate`. For a new kind of page (a new template), add its rules to `TEMPLATES` in
  `site_parser.py` and a small fixture page with a test in `tests/`.
- **Exclude a website:** add a profile with `enabled=False` and a `reason`. It then appears under
  "not crawled" in `sites` and is skipped by `crawl`, `docs` and `parse`.
- **Fix the main-content area or remove noise on a site:** use `content_selectors` and
  `remove_selectors` in the site's profile (CSS selectors). For a whole template family
  (WordPress "nyit" theme, LibGuides, LibAnswers, ...) use `TEMPLATES` in `site_parser.py`.
  Re-run `parse` (no network) and compare the text in `pages.jsonl`.
- **Crawl more of a capped site:** raise `max_pages` in its profile and run
  `crawl --site <host>` (or the full `crawl`). No website of the included crawl reached its
  limit; libguides, for example, is capped at 1,500 and has 1,321 pages.
- **Follow a listing's page numbers:** add the parameter to the site's `keep_query_params`
  (as done for `?p=` on the blogs and `?fsa_page=` and `?mrp_page=` on www.nyit.edu), then run
  `crawl --site <host>`. `validate`'s "Paginated listings crawled" check warns when such links
  were normalised away (`dropped_query_self_links` in `crawl_summary.json`) and when a
  page-number parameter that `sites.py` keeps occurs in no cached URL.
- **Change the passage size:** `MAX_SECTION_WORDS`, `PART_MIN_WORDS` and `PART_MAX_WORDS` in
  `html_parser.py` (450 / 150 / 400 words). Then `parse`.
- **Add a catalog edition:** add it to `PDF_EDITIONS` in `config.py` (see 6.4).
- After any change, run the tests (`python -m pytest -q`), `parse`, `stats` and `validate --strict`.

### 12.4 FAQ

**Do I have to run the crawl?** No. The included dataset is validated and ready to use, and its
crawl is complete for the current rules of `sites.py`. Rebuild it only if you need fresher data
or changed the code.

**Why does the corpus have 41,725 records but `pages.jsonl` only 8,231?** A page is split into
several passages (28,858 in the corpus), each course is its own record (3,045), and each page of
a document (PDF page, Word part, Excel part, PowerPoint slide) is a record too (9,822).

**Why did the corpus size change during 2026-11-23?** An earlier version had 41,257 records.
The first build of 2026-11-23 (after the main crawl) had 35,268, mostly because more copies
were left out: older editions of yearly PDF documents (4,779 pages), near-duplicate passages
(295), very short PDF pages (259) and passages merged into their neighbours (537), plus pages
that are now skipped on purpose (error pages served with HTTP 200, the sign-in and placeholder
pages of six web applications, online.nyit.edu's vendor privacy and cookie notices). The final
build has 41,725: the catch-up crawl added the older blog posts (blogs.nyit.edu passages 504 →
2,250), the library guides beyond the old cap of 400 (1,546 → 3,917) and the accomplishments
pages (www.nyit.edu 14,782 → 16,093), and `docs` downloaded 147 more files: the 117 linked
Word, Excel and PowerPoint files (928 corpus records) and 30 PDFs linked from the newly crawled
pages (document records 8,755 → 9,822).

**What is the difference between `pages`, `sections` and `corpus`?** `pages.jsonl` has whole
pages, including duplicates, with full metadata. `sections.jsonl` has the passages of the
indexable pages. `corpus.jsonl` is the uniform search input: sections, courses and document pages in
one schema.

**Why are the catalog PDFs not in the corpus?** They repeat the catalog website, and the
2025-2026 edition is older. Use `parse --corpus-with-pdf` if you want them.

**Why do some records have `school` or `level` = null?** Most pages outside the academic
programs (news, library, events, general offices) belong to no single school or level, so `null`
usually means "not specific". It can also mean that the rules could not tell (see Limitations in
`docs/METHODOLOGY.md`).

**Why is a page I know missing?** Check in this order:
1. Is its website crawled (`sites`)?
2. Is the path excluded in `sites.py`?
3. Is the page behind a login?
4. What does the manifest say? Search `crawl_manifest.jsonl` for its URL and read `status`.
5. Did it end up as a duplicate (`duplicate_of` in `pages.jsonl`)?
6. Is it one of the known gaps in 7.4 (an unreachable host, a broken link on NYIT's side, a
   page whose content is not in its HTML)?

**Is scraping NYIT allowed?** The crawler only reads public pages, obeys robots.txt and its
Crawl-delay, identifies itself and stays slow. That is accepted practice for academic projects,
but it is not legal advice. Keep the data inside the course project.

**Can the reranker be trained on this data?** Yes, for the course project. Use the passages as
candidates, and use the labeled questions from the evaluation plan as training and test data.
Keep the held-out test questions separate.

**How do I cite a passage?** Use its `url`. Sections link to their `#anchor`, PDF pages to
`#page=N` (on a Word, Excel or PowerPoint file `#page=N` only names the part: the browser
downloads the file), and courses open their description.

**Which Python version built the data?** The project's development environment on Linux:
Python 3.14 with PyMuPDF 2.1 (`pymupdf_version` in `pdf_summary.json`), on 2026-11-23. The other
package versions are in `constraints.txt`. The code is tested on Python 3.12 and 3.14 and checked
against the Python 3.9 grammar; it has not been run on Windows or macOS yet, but it uses only
portable paths and UTF-8 file access.
