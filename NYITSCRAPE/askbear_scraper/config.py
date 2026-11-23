"""Central configuration for the AskBear scraper.

Global settings live here (politeness, data folders, the catalog PDFs, labels used in
the records). WHICH websites are crawled, and the URL rules for each of them, live in
sites.py. Most values can also be overridden from the command line (see
``python -m askbear_scraper --help``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Websites (per-site rules: see sites.py)
# ---------------------------------------------------------------------------
BASE_URL = "https://catalog.nyit.edu"   # the academic catalog, the core of AskBear

# Links ending in these extensions are files, not web pages.
NON_HTML_EXTENSIONS: Tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".css", ".js", ".json", ".xml", ".txt", ".csv",
    ".mp3", ".mp4", ".mov", ".avi", ".wmv", ".webm",
    ".woff", ".woff2", ".ttf", ".eot",
)

# Query parameters that never change page content (dropped during normalisation).
# cc: /courses?cal=GR&cc=PHAS-601&cs=PHAS only unfolds one course in the browser; the
#     page itself is the same as /courses?cal=GR&cs=PHAS (semester maps link to 29 of them)
TRACKING_PARAMS: Tuple[str, ...] = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "r", "cc",
)

# Identify ourselves honestly. Put a team contact email in --contact (or here) so the
# site administrators can reach you if they need to.
USER_AGENT_TEMPLATE = (
    "AskBearBot/1.0 (NYIT CSCI 426 student information-retrieval project; "
    "polite single-threaded crawler{contact})"
)

# ---------------------------------------------------------------------------
# Catalog PDFs (full printed editions)
# ---------------------------------------------------------------------------
PDF_ARCHIVE_URL = BASE_URL + "/files/catalog/archives/"

# catalog kind -> file-name prefix used by NYIT
PDF_KINDS: Dict[str, str] = {
    "undergraduate": "NYITUndergraduateCatalog",
    "graduate": "NYITGraduateCatalog",
    "osteopathic": "NYITOsteopathicMedicineCatalog",
}

# edition -> file-name suffix used by NYIT (NYITGraduateCatalog_AY2526.pdf, ...)
PDF_EDITIONS: Dict[str, str] = {
    "2026-2027": "AY2627",
    "2025-2026": "AY2526",
    "2024-2025": "AY2425",
    "2023-2024": "AY2324",
    "2022-2023": "AY2223",
    "2021-2022": "AY2122",
}

# The live website is the 2026-2027 edition, so the PDF step defaults to the same
# edition. The project proposal's numbers (581 pages, ~249K words) were measured on
# the 2025-2026 undergraduate + graduate PDFs: run `pdf --edition 2025-2026` to
# reproduce them.
DEFAULT_PDF_EDITIONS: Tuple[str, ...] = ("2026-2027",)
DEFAULT_PDF_KINDS: Tuple[str, ...] = ("undergraduate", "graduate")


def pdf_url(kind: str, edition: str) -> str:
    """Return the download URL of one catalog PDF, e.g. ('graduate', '2025-2026')."""
    if kind not in PDF_KINDS:
        raise ValueError(f"unknown catalog kind {kind!r}; choose from {sorted(PDF_KINDS)}")
    if edition not in PDF_EDITIONS:
        raise ValueError(f"unknown edition {edition!r}; choose from {sorted(PDF_EDITIONS)}")
    return f"{PDF_ARCHIVE_URL}{PDF_KINDS[kind]}_{PDF_EDITIONS[edition]}.pdf"


# ---------------------------------------------------------------------------
# Domain knowledge used to label records
# ---------------------------------------------------------------------------
# First URL segment -> school/college name (the six schools and colleges).
SCHOOL_BY_PATH: Dict[str, str] = {
    "arts_and_sciences": "College of Arts and Sciences",
    "engineering": "College of Engineering and Computing Sciences",
    "medicine": "College of Osteopathic Medicine",
    "architecture": "School of Architecture and Design",
    "health_professions": "School of Health Professions",
    "management": "School of Management",
}

# Campus name -> regular expression that detects a mention of it in page text.
CAMPUS_PATTERNS: Dict[str, str] = {
    "Long Island": r"\b(Long Island|Old Westbury)\b",
    "New York City": r"\b(New York City|Manhattan)\b",
    "Jonesboro": r"\b(Jonesboro|Arkansas)\b",
    "Vancouver": r"\bVancouver\b",
}

# Numbers from the project proposal (slide 9), printed next to ours by `stats`.
PROPOSAL_TARGETS: Dict[str, object] = {
    "pdf_pages_2025_2026": 581,
    "pdf_pages_undergraduate_2025_2026": 318,
    "pdf_pages_graduate_2025_2026": 263,
    "pdf_words_2025_2026": 249_000,
    "course_codes": 1200,
    "subject_areas": 100,
    "schools": 6,
    "campuses": 4,
}


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    """Runtime settings shared by every command (CLI flags override the defaults)."""

    base_url: str = BASE_URL
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    # politeness
    delay: float = 1.0          # seconds between requests (one request at a time)
    jitter: float = 0.25        # extra random 0..jitter seconds so requests are not robotic
    timeout: float = 30.0       # seconds before a request is abandoned
    max_retries: int = 4        # retries for timeouts, 429 and 5xx responses
    backoff: float = 2.0        # exponential backoff base (2, 4, 8, 16 s)
    respect_robots: bool = True   # always True from the command line (only tests change it)
    contact: str = ""           # e-mail shown in the User-Agent (optional, recommended)

    # crawl scope
    max_pages: int = 0          # extra cap on pages per website (0 = each site's own limit in sites.py)
    include_print_pages: bool = False
    refresh: bool = False       # True = ignore the cache and download everything again
    # download the sitemap lists again even if the cached copy is newer than 7 days
    # (crawler.SITEMAP_MAX_AGE_DAYS); --refresh implies it
    refresh_sitemaps: bool = False

    @property
    def user_agent(self) -> str:
        contact = f"; contact: {self.contact}" if self.contact else ""
        return USER_AGENT_TEMPLATE.format(contact=contact)

    # ---- data layout -------------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def html_dir(self) -> Path:
        return self.raw_dir / "html"

    @property
    def pdf_dir(self) -> Path:
        return self.raw_dir / "pdf"

    @property
    def docs_dir(self) -> Path:
        """PDF documents linked from NYIT pages (the `docs` step), one folder per host."""
        return self.raw_dir / "docs"

    @property
    def docs_manifest_path(self) -> Path:
        """One JSON line per PDF document URL (download status, file, SHA-1)."""
        return self.raw_dir / "docs_manifest.jsonl"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def manifest_path(self) -> Path:
        """One JSON line per fetched URL (status, cache file, timestamps)."""
        return self.raw_dir / "crawl_manifest.jsonl"

    @property
    def link_graph_path(self) -> Path:
        """One JSON line per page with its outgoing in-scope links."""
        return self.raw_dir / "link_graph.jsonl"

    @property
    def external_links_path(self) -> Path:
        """Links found while crawling that are not crawled as pages, one JSON line each, by
        ``kind``: "pdf" (read by the `docs` step), "asset" (Word/Excel/images...),
        "external" (other websites), "disabled" (NYIT hosts that sites.py skips) and
        "excluded" (paths sites.py leaves out). mailto:, tel: and javascript: links are
        ignored, so they are not in this file."""
        return self.raw_dir / "external_links.jsonl"

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.html_dir, self.pdf_dir, self.processed_dir):
            d.mkdir(parents=True, exist_ok=True)
