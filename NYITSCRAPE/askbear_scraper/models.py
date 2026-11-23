"""Record types written to ``data/processed/*.jsonl``.

Every record is a plain dataclass so the files are easy to read from pandas, the
BM25 indexer or the reranker:

    import pandas as pd
    sections = pd.read_json("data/processed/sections.jsonl", lines=True)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class PageRecord:
    """One HTML page of an NYIT website (-> pages.jsonl)."""

    doc_id: str                       # "page-<10 hex chars of sha1(url)>"
    url: str                          # canonical URL of the page
    title: str                        # the page heading (<h1>), e.g. "Computer Science, B.S."
    page_title: str                   # the full <title> text
    breadcrumbs: List[str]            # ["New York Tech Catalog 2026–2027", "Schools and Colleges", ...]
    catalog_year: str                 # "2026-2027" on catalog pages, "" elsewhere
    page_type: str                    # see PAGE_TYPES below
    area: Optional[str]               # top-level part of the site, e.g. "Schools and Colleges", "Admissions"
    school: Optional[str]             # e.g. "College of Engineering and Computing Sciences"
    program: Optional[str]            # e.g. "Computer Science, B.S." (program pages and their sub-pages)
    degree: Optional[str]             # e.g. "B.S.", "M.S.", "Ph.D.", "Minor", "Advanced Certificate"
    level: Optional[str]              # "undergraduate" | "graduate" | "doctoral" | "professional" | None
    campuses: List[str]               # campuses mentioned on the page ("Long Island", "New York City", ...)
    headings: List[str]               # all h2-h4 headings in reading order
    text: str                         # clean main-content text (tables kept as "a | b | c" rows)
    word_count: int
    num_tables: int
    links: List[Dict[str, str]]       # links inside the main content: {"text": ..., "url": ...}
    content_sha1: str                 # sha1 of ``text`` (duplicate detection)
    fetched_at: str
    duplicate_of: Optional[str] = None  # url of an earlier page with identical text
    site: str = ""                    # website (host) the page belongs to, e.g. "www.nyit.edu"
    description: Optional[str] = None # <meta name="description"> / og:description, if any
    # ISO date the page was first published (news, blog posts, magazine issues, e-mail
    # announcements), if the site says; never set on bios and events (see event_start)
    published: Optional[str] = None
    modified: Optional[str] = None    # ISO date of the last update, if the site says
    # events: when the event starts, ISO date-time in its local time zone
    event_start: Optional[str] = None
    # events: when it ends (None: an all-day event or no end time given)
    event_end: Optional[str] = None
    noindex: bool = False             # the page asks search engines not to index it

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SectionRecord:
    """A heading-delimited part of a page (-> sections.jsonl): the natural passage unit."""

    section_id: str                   # "<doc_id>-s<NNN>"
    doc_id: str                       # the PageRecord it comes from
    url: str                          # page URL, plus "#anchor" when the section has one
    page_title: str                   # PageRecord.title
    heading: str                      # this section's heading ("" for the intro before the first heading)
    heading_path: List[str]           # e.g. ["Admission Requirements", "Application Materials"]
    text: str                         # section body, without the heading line
    word_count: int
    order: int                        # 0-based position within the page
    # metadata copied from the page so each section is self-contained
    page_type: str = ""
    school: Optional[str] = None
    program: Optional[str] = None
    degree: Optional[str] = None
    level: Optional[str] = None
    catalog_year: str = ""
    breadcrumbs: List[str] = field(default_factory=list)
    site: str = ""                    # website (host) of the page
    campuses: List[str] = field(default_factory=list)  # PageRecord.campuses of its page
    # corpus id of the passage that corpus.jsonl holds instead of this one: the same text
    # (or a near-copy) on a more authoritative page, see pipeline.collapse_near_duplicates
    duplicate_of: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CourseRecord:
    """One course from the course directory (-> courses.jsonl / courses.csv)."""

    course_id: str                    # "CSCI 260|UG" (the same code can exist at several levels)
    code: str                         # "CSCI 260"
    subject: str                      # "CSCI"
    subject_name: str                 # "Computer Science"
    number: str                       # "260"
    title: str                        # "Data Structures"
    credits: str                      # as printed, e.g. "3.0" or "1.0-3.0"
    credits_min: Optional[float]
    credits_max: Optional[float]
    school: str                       # as printed, e.g. "College of Eng & Comp Sciences"
    level: str                        # "undergraduate" | "graduate" | "professional"
    level_code: str                   # "UG" | "GR" | "PROF"
    description: str
    prerequisites: Optional[str]
    corequisites: Optional[str]
    hours: Optional[str]              # "Classroom - Lab/Studio - Credits", e.g. "3-0-3"
    attributes: Dict[str, str]        # every labelled field of the course, verbatim
    text: str                         # ready-to-index text block for the course
    url: str                          # the course listing page it was scraped from
    catalog_year: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PdfPageRecord:
    """One page of a full catalog PDF (-> pdf_pages.jsonl)."""

    doc_id: str                       # "pdf-2025-2026-undergraduate-p0007"
    edition: str                      # "2025-2026"
    catalog: str                      # "undergraduate" | "graduate" | "osteopathic"
    page: int                         # 1-based page number
    page_count: int                   # pages in this PDF
    url: str                          # PDF URL + "#page=N" (opens the PDF at that page)
    toc_path: List[str]               # bookmark path covering this page, outermost first
    section: Optional[str]            # deepest bookmark title (toc_path[-1]) or None
    text: str
    word_count: int
    pdf_file: str                     # file name under data/raw/pdf/

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentPageRecord:
    """One page of a document linked from an NYIT website (-> documents.jsonl): a PDF, or a
    Word, Excel or PowerPoint file (``file_type``).

    Handbooks, forms, reports... found by the crawler (the full catalog PDFs are handled
    separately by the `pdf` step and are not repeated here). The "pages" of an Office file
    are cut by the `docs` step: about 150-400 words of a .docx (at a heading or paragraph),
    rows of one .xlsx sheet, one .pptx slide. ``pdf_url`` keeps its name for compatibility:
    it is the document's URL whatever its type."""

    doc_id: str                       # "doc-<10 hex chars of sha1(pdf_url)>-p0003"
    site: str                         # host serving the document, e.g. "www.nyit.edu"
    url: str                          # document URL + "#page=N" (a PDF opens at that page)
    pdf_url: str                      # document URL (PDF or Office file) without the page anchor
    title: str                        # title metadata, else heading/printed title, link text, file name
    found_on: List[str]               # pages that link to this document (at most 5)
    page: int                         # 1-based page number (.pptx: slide number)
    page_count: int                   # pages in this document
    toc_path: List[str]               # PDF bookmarks / .docx headings / .xlsx sheet / .pptx slide title
    text: str
    word_count: int
    file: str                         # path of the file under data/raw/docs/
    file_type: str = "pdf"            # "pdf" | "docx" | "xlsx" | "pptx"
    modified: Optional[str] = None    # modification date (ISO) recorded in the file, else HTTP Last-Modified
    # what kind of document it is (documents.DOC_CATEGORIES): "form", "policy_handbook",
    # "semester_map", "financial_filing", ...
    doc_category: str = "other"
    edition: Optional[str] = None     # year/term/date the file name gives: "2023-24", "Fall 2020"
    doc_year: Optional[int] = None    # last year the edition covers, else the year modified
    # the file name minus its year/term/date ("files.nyit.edu nyitcom student handbook")
    # when other editions of the same document were downloaded too, else None
    series: Optional[str] = None
    superseded: bool = False          # an older edition: a newer one of the series exists
    superseded_by: Optional[str] = None   # pdf_url of the newest edition
    cohort_year: Optional[int] = None  # semester maps / class calendars: the entering class
    scanned: bool = False             # most pages are images without a text layer (PDFs only)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CorpusRecord:
    """Uniform record for the search index (-> corpus.jsonl): sections + courses (+ documents,
    + catalog PDF pages on request)."""

    # section_id, "course-<code>-<level>" ("course-CSCI-260-UG") or a PDF/document doc_id;
    # never contains whitespace, so it can go into TREC run and qrels files as it is
    id: str
    source: str                       # "section" | "course" | "document" | "pdf_page"
    url: str                          # link shown to the user next to the answer
    title: str                        # short display title
    text: str                         # what BM25 / the reranker should read
    metadata: Dict[str, object]       # site, school, program, level, page_type, heading_path, ...

    def to_dict(self) -> dict:
        return asdict(self)


# Allowed values of PageRecord.page_type
PAGE_TYPES = (
    # catalog.nyit.edu
    "home",             # catalog homepage
    "school",           # a school/college landing page (/engineering)
    "program_list",     # lists of programs (/engineering/undergraduate, /schools_and_colleges)
    "program",          # a degree/minor/certificate page (/engineering/computer_science)
    "curriculum",       # requirement tables (/engineering/computer_science/curriculum)
    "faculty",          # faculty / administration listings
    "course_index",     # /courses
    "course_listing",   # /courses?cs=CSCI&cal=UG
    "policy",           # /policies_and_rules/...
    "financial_aid",    # /financial_aid_undergraduate, /financial_aid_graduate
    "tuition",          # /tuition/...
    "student_services", # /student_services/...
    "general_information",  # /general_information/...
    "help",             # /help/...
    # other NYIT websites
    "page",             # an ordinary information page (offices, admissions, student life, ...)
    "degree",           # a degree/program page on www.nyit.edu (/academics/degrees/...)
    "listing",          # a hub page that is mostly a list of links
    "news",             # news article
    "media_coverage",   # short note about NYIT in the media
    "expert_insight",   # expert commentary
    "bio",              # faculty/staff profile
    "event",            # events.nyit.edu event
    "magazine_article", # magazine.nyit.edu
    "faq",              # library FAQ answer (libanswers.nyit.edu)
    "library_guide",    # library research guide page (libguides.nyit.edu)
    "library_hours",    # the libraries' opening hours (libcal.nyit.edu/hours): a snapshot, see fetched_at
    "blog_post",        # blogs.nyit.edu
    "communication",    # an e-mail announcement saved as a web page (files.nyit.edu/files/...html); dated by published
    # (policies on apps.nyit.edu use "policy", like the catalog's policy pages)
    "other",
)
