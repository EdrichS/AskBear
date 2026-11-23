"""AskBear scraper.

Stage 1 ("Collect") of the AskBear information-retrieval project: crawl the public NYIT
websites (the academic catalog at https://catalog.nyit.edu, www.nyit.edu, the policy
library, the library's FAQ and research guides, the events calendar, the magazine, blogs
and the other *.nyit.edu sites listed by ``python -m askbear_scraper sites``), cache every
page, download the PDF documents they link to, and turn all of it into clean, structured
records (pages, sections, courses, PDF document pages, catalog PDF pages, and one search
corpus) that the indexing, BM25 and reranking stages can consume.

Run ``python -m askbear_scraper --help`` for the command-line interface.
"""

__version__ = "1.0.0"
