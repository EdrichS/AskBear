"""Which NYIT websites are crawled, and how.

Every host has a :class:`SiteProfile`: where the crawl starts, which sitemaps list its
pages, which URLs are in or out of scope, and where the main content of a page lives.
Run ``python -m askbear_scraper sites`` to print this table.

Any other ``*.nyit.edu`` host that the crawler discovers through links gets the
conservative :data:`DEFAULT_PROFILE` (crawled, at most ``max_pages`` pages) and is
listed in ``data/raw/crawl_summary.json`` so you can review it and add a profile here.

Hosts that are deliberately NOT crawled are listed too, with the reason:
login-only portals, application forms, robots.txt opt-outs, personal-data
directories, and web applications whose pages hold no public text (sign-in screens,
survey tools, script-only players). Unlisted hosts whose name marks a sign-in system
(``sso.``, ``*-sso.``, ``login.``...) or a development copy (``*-dev``, ``staging``...) are
never crawled either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

# A host belongs to NYIT when it is nyit.edu or ends with one of these suffixes.
NYIT_DOMAIN_SUFFIXES: Tuple[str, ...] = (".nyit.edu",)
NYIT_DOMAINS: Tuple[str, ...] = ("nyit.edu",)

# *.nyit.edu hosts that are never crawled even when the crawler finds links to them:
# - the library's EZproxy (arktos.nyit.edu): it rewrites licensed databases such as EBSCO as
#   "<database>.arktos.nyit.edu" and needs a library login;
# - sign-in, password, account and back-office systems (names matched below);
# - development / staging copies of real sites (e.g. catalog-dev.nyit.edu duplicates the catalog).
DISABLED_HOST_SUFFIXES: Tuple[str, ...] = (".arktos.nyit.edu",)
DEV_HOST_LABELS = {"dev", "development", "stage", "staging", "test", "testing", "qa", "uat",
                   "sandbox", "preview", "demo", "beta"}
DISABLED_HOST_PATTERN = re.compile(
    r"^(arktos|sso|login|idp|cas|auth|adfs|okta|vpn|mail|webmail|owa|outlook|smtp|ftp|"
    r"pwreset|password|identity|esapps\d*|apps\d+|workday|onecard|engage|selfservice|"
    r"banner|ssb|portal)\.", re.I)
# Words that mark a sign-in system wherever they appear as a whole label of the host name
# (oaproxy-sso.nyit.edu, sso.lib.nyit.edu, ...): matched on the parts between dots and dashes.
SIGN_IN_HOST_LABELS = {"sso", "login", "idp", "adfs", "okta", "saml", "shibboleth", "oauth",
                       "pwreset", "webmail", "vpn"}

# Noise removed from every page before extracting text (in addition to per-site rules).
COMMON_REMOVE_SELECTORS: Tuple[str, ...] = (
    "script", "style", "noscript", "template", "iframe", "svg", "canvas", "form",
    "select", "button", "input", "textarea", "nav", "header .Header", "footer",
    "[hidden]", "[aria-hidden=true]", ".screen-reader-text", ".sr-only", ".visually-hidden",
    "#cookie-notice", ".cookie-notice", ".cookies", "[class*=cookie-banner]",
)

# WordPress URLs that are never content pages.
WORDPRESS_EXCLUDES: Tuple[str, ...] = (
    r"^/wp-(admin|login\.php|json|includes|cron\.php|signup\.php|activate\.php)",
    r"^/wp-content/",            # uploads, themes, plugins (PDFs there are handled as documents)
    r"^/xmlrpc\.php",
    r"/feed/?$",
    r"/trackback/?$",
    r"/embed/?$",
    r"/amp/?$",
    r"/attachment/",
    r"/page/\d+/?$",             # paginated archive listings
    r"^/(tag|author|category)/",
    r"^/search/",
)


@dataclass(frozen=True)
class SiteProfile:
    host: str
    name: str
    enabled: bool = True                     # crawled by default?
    reason: str = ""                         # why it is (not) crawled -- shown by `sites`
    parser: str = "generic"                  # "catalog" (catalog.nyit.edu) or "generic"
    seeds: Tuple[str, ...] = ("/",)          # paths where the crawl starts
    sitemaps: Tuple[str, ...] = ()           # sitemap URLs (robots.txt "Sitemap:" lines are added automatically)
    follow_links: bool = True                # False: only seeds + sitemap URLs are crawled
    case_insensitive: bool = False           # treat /A and /a as the same page
    # "strip": /a/ -> /a ; "add": /a -> /a/ (WordPress, which redirects to the slashed URL) ;
    # "keep": leave the URL as written
    trailing_slash: str = "strip"
    keep_query_params: Tuple[str, ...] = ()  # query parameters that select different content ("*" = keep all)
    allow_patterns: Tuple[str, ...] = ()     # if set, only paths matching one of these regexes are crawled
    exclude_patterns: Tuple[str, ...] = ()   # path regexes that are never crawled
    max_pages: int = 10000                   # safety cap for this host
    content_selectors: Tuple[str, ...] = ("main", "[role=main]", "article", "#content", ".content")
    remove_selectors: Tuple[str, ...] = ()   # site-specific noise inside the content element
    breadcrumb_selector: Optional[str] = None
    use_meta_description: bool = False       # add <meta description> when the visible text is thin

    def path_allowed(self, path_and_query: str) -> bool:
        """Scope check for one URL path (+ "?query") on this host."""
        if self.allow_patterns and not any(re.search(p, path_and_query) for p in self.allow_patterns):
            return False
        return not any(re.search(p, path_and_query) for p in self.exclude_patterns)


WORDPRESS_THEME = dict(
    parser="generic",
    trailing_slash="add",
    keep_query_params=(),
    exclude_patterns=WORDPRESS_EXCLUDES,
    content_selectors=("main",),
    remove_selectors=("nav.Subnav", "nav.Breadcrumbs", ".Breadcrumbs", ".wp-block-buttons",
                      ".Share", ".social-share", ".addtoany_share_save_container"),
    breadcrumb_selector=".yoast-breadcrumbs",
)

SITES: Dict[str, SiteProfile] = {p.host: p for p in [
    # ---------------------------------------------------------------- crawled
    SiteProfile(
        host="catalog.nyit.edu",
        name="Academic catalog (2026-2027)",
        reason="official programs, courses, policies -- the core of AskBear",
        parser="catalog",
        seeds=(
            "/", "/schools_and_colleges", "/courses", "/general_information",
            "/policies_and_rules", "/student_services", "/administration_and_faculty",
            "/financial_aid_undergraduate", "/financial_aid_graduate", "/tuition",
            "/help", "/help/print", "/help/archive",
        ),
        case_insensitive=True,
        trailing_slash="strip",
        keep_query_params=("cs", "cal"),     # /courses?cal=UG&cs=CSCI
        exclude_patterns=(r"^/files(/|$)", r"^/_utilities(/|$)", r"^/favicons(/|$)", r"^/print(/|$)"),
        content_selectors=("div.Main__content .Common", "div.Main__content", "main"),
    ),
    SiteProfile(
        host="www.nyit.edu",
        name="Main website",
        reason="admissions, degrees, tuition, student life, news, faculty bios, offices; "
               "all 128 pages of faculty & staff accomplishments (?fsa_page=N) and of "
               "NYITCOM's research projects (?mrp_page=N)",
        sitemaps=("https://www.nyit.edu/sitemap_index.xml",),
        # /faculty-and-staff/accomplishments/?fsa_page=2 ... (its filters are dropped),
        # /medicine/college-of-osteopathic-medicine/about/research-at-nyitcom/projects/?mrp_page=2
        **dict(WORDPRESS_THEME, keep_query_params=("fsa_page", "mrp_page")),
    ),
    SiteProfile(
        host="vancouver.nyit.edu",
        name="Vancouver campus",
        reason="Vancouver campus programs and student services",
        sitemaps=("https://vancouver.nyit.edu/sitemap_index.xml",),
        **WORDPRESS_THEME,
    ),
    SiteProfile(
        host="magazine.nyit.edu",
        name="NYIT Magazine",
        reason="magazine articles about the university",
        sitemaps=("https://magazine.nyit.edu/sitemap.xml",),
        trailing_slash="keep",
        content_selectors=("article", "[role=main]", "main"),
        max_pages=2000,
    ),
    SiteProfile(
        host="events.nyit.edu",
        name="Events calendar",
        reason="current and upcoming events (only event pages: calendar views are endless)",
        seeds=(),
        sitemaps=("https://events.nyit.edu/sitemap.livewhale.xml",),
        follow_links=False,
        allow_patterns=(r"^/event/",),
        exclude_patterns=(r"replytocom",),
        use_meta_description=True,
        max_pages=1000,
    ),
    SiteProfile(
        host="apps.nyit.edu",
        name="Policy library",
        reason="official policies and handbooks (student handbook, code of conduct, academic "
               "integrity...); the host's other apps -- people directory, course search, "
               "forms -- are dynamic search tools / personal contact data and are skipped",
        seeds=("/policies",),
        allow_patterns=(r"^/policies(/|$)",),
        content_selectors=(".Common", "main"),
        max_pages=2000,
    ),
    SiteProfile(
        host="libanswers.nyit.edu",
        name="Library FAQ (Ask a Librarian)",
        reason="answers to common library questions; robots.txt asks for 10 s between requests",
        seeds=(),
        sitemaps=("https://libanswers.nyit.edu/sitemap.xml",),
        follow_links=False,
        allow_patterns=(r"^/faq/\d+",),
        content_selectors=("#s-la-content-main", "#s-la-public-main", "main", "#col1", "body"),
        max_pages=1000,
    ),
    SiteProfile(
        host="libguides.nyit.edu",
        name="Library research guides",
        reason="library guides (databases, citation help, subject guides) and their tabs; "
               "robots.txt asks for 10 s between requests (~1,300 pages = about 4 hours)",
        sitemaps=("https://libguides.nyit.edu/sitemap.xml",),
        keep_query_params=("g", "p"),
        # LibGuides' own tools (A-Z list, search, link resolvers, RSS, widgets, sign-in);
        # bounded so that guides such as /gold_coast, /ereservespolicy or /data/search stay in
        exclude_patterns=(r"^/(az|srch|go|er|err|friendly|ld|prf|sb|content_rss|rss|loader|"
                          r"search)(\.php)?($|[/?])", r"^/(process|widget)_",
                          r"^/blog($|[/?])", r"^/sitemap", r"^/(auth|login|admin)($|[/?.])"),
        content_selectors=("#s-lg-guide-main", "#s-lg-main-content", "main", "body"),
        max_pages=1500,
    ),
    SiteProfile(
        host="blogs.nyit.edu",
        name="NYIT blogs",
        reason="university blogs (Library e-News, campus blogs); every listing page "
               "(?p=2, ?p=3, ...) is followed so that older posts are found",
        seeds=("/",),
        keep_query_params=("p",),            # /cfe-weekly-teaching-notes?p=29; ?tag= views dropped
        max_pages=4000,
    ),
    SiteProfile(
        host="online.nyit.edu",
        name="Online programs",
        reason="online degree programs: curricula, admissions, tuition, careers; the "
               "marketing vendor's privacy and cookie notices (third-party tracking "
               "boilerplate, NYIT's own privacy statement is on www.nyit.edu) are skipped",
        sitemaps=("https://online.nyit.edu/sitemap-content.xml",),
        keep_query_params=("p",),            # /blog?p=2 ... (blog listing pages)
        exclude_patterns=(r"^/(privacy-policy|cookies)/?$",),
        max_pages=1500,
    ),
    SiteProfile(
        host="libcal.nyit.edu",
        name="Library hours and events (LibCal)",
        reason="library opening hours (a weekly snapshot: check fetched_at) and library "
               "event pages; room booking, appointment and reservation forms are skipped; "
               "robots.txt asks for 10 s between requests",
        seeds=("/", "/hours"),
        allow_patterns=(r"^/$", r"^/hours/?$", r"^/event/\d+/?$"),
        exclude_patterns=(r"(booking|appointment|scheduler|reserve|process_|/ical)",),
        max_pages=200,
    ),
    SiteProfile(
        host="deseversky.nyit.edu",
        name="de Seversky Mansion",
        reason="NYIT's event venue on the Long Island campus",
        exclude_patterns=(r"^/(images|calendar|aux|csvtest|getsocial|goldcoast-test|includes)(/|$)",),
        max_pages=300,
    ),
    # Legacy hosts: their pages redirect to www.nyit.edu (the crawler de-duplicates the
    # redirects) and they host PDF documents picked up by the `docs` step.
    SiteProfile(host="nyit.edu", name="Bare domain (redirects to www)", seeds=(),
                reason="links are followed; redirects are de-duplicated"),
    SiteProfile(host="site.nyit.edu", name="Legacy site (redirects to www)", seeds=(),
                reason="links are followed; redirects are de-duplicated; hosts PDFs"),
    SiteProfile(host="files.nyit.edu", name="File server", seeds=(),
                reason="hosts PDF forms and documents (see the `docs` step)"),
    SiteProfile(host="eportfolios.nyit.edu", name="Faculty ePortfolios (Mahara)",
                reason="only the home page (help documents and contacts); the rest is the "
                       "Mahara software: sign-in screens, error and placeholder pages",
                seeds=("/",), follow_links=False, allow_patterns=(r"^/$",), max_pages=5),
    # ---------------------------------------------------------------- not crawled
    SiteProfile(host="my.nyit.edu", name="MyNYIT portal", enabled=False,
                reason="student/employee portal: content requires login"),
    SiteProfile(host="campusgroups.nyit.edu", name="CampusGroups", enabled=False,
                reason="requires login"),
    SiteProfile(host="alumni.nyit.edu", name="Alumni community", enabled=False,
                reason="login/registration portal"),
    SiteProfile(host="apply.nyit.edu", name="Admissions application portal", enabled=False,
                reason="application and event-registration forms, not information pages"),
    SiteProfile(host="web.nyit.edu", name="Course search (legacy)", enabled=False,
                reason="robots.txt disallows all crawling"),
    SiteProfile(host="registrations.nyit.edu", name="Registrations", enabled=False,
                reason="robots.txt disallows all crawling"),
    SiteProfile(host="identity.nyit.edu", name="Identity management", enabled=False,
                reason="account system; robots.txt disallows crawling"),
    SiteProfile(host="cediploma.nyit.edu", name="Diploma verification", enabled=False,
                reason="third-party credential-validation service (forms)"),
    SiteProfile(host="comresearchapp.nyit.edu", name="COM research forms", enabled=False,
                reason="intake-form web application"),
    SiteProfile(host="nyitcs.nyit.edu", name="Single sign-on", enabled=False,
                reason="sign-in system"),
    SiteProfile(host="catalog-dev.nyit.edu", name="Catalog (development copy)", enabled=False,
                reason="development copy of catalog.nyit.edu (duplicate content)"),
    SiteProfile(host="learningspace.nyit.edu", name="Simulation center software", enabled=False,
                reason="Elevate LearningSpace sign-in application (no public text)"),
    SiteProfile(host="iccjb.nyit.edu", name="Simulation center software (Jonesboro)",
                enabled=False,
                reason="Elevate LearningSpace sign-in application (no public text)"),
    SiteProfile(host="comresearchdata.nyit.edu", name="COM research surveys (REDCap)",
                enabled=False, reason="REDCap survey and data-capture tool (forms, no text)"),
    SiteProfile(host="nyitetic.nyit.edu", name="ETIC research robot app", enabled=False,
                reason="web application: a default IIS server page and script-only pages"),
    SiteProfile(host="virtualtour.nyit.edu", name="Virtual campus tours", enabled=False,
                reason="JavaScript-only tour player (the HTML only says the browser is "
                       "unsupported)"),
]}

# Profile used for *.nyit.edu hosts that are not listed above.
DEFAULT_PROFILE = SiteProfile(
    host="*",
    name="Other NYIT host",
    reason="discovered through links; crawled conservatively",
    seeds=(),
    max_pages=300,
)


def is_nyit_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in NYIT_DOMAINS or host.endswith(NYIT_DOMAIN_SUFFIXES)


def profile_for(host: str) -> Optional[SiteProfile]:
    """The profile for ``host``; ``None`` when the host is not an NYIT website."""
    host = (host or "").lower().rstrip(".")
    if host in SITES:
        return SITES[host]
    if not is_nyit_host(host):
        return None
    labels = set(re.split(r"[.-]", host[: -len(".nyit.edu")] if host.endswith(".nyit.edu") else host))
    if (host.endswith(DISABLED_HOST_SUFFIXES) or DISABLED_HOST_PATTERN.match(host)
            or labels & SIGN_IN_HOST_LABELS):
        return replace(DEFAULT_PROFILE, host=host, enabled=False,
                       reason="library proxy or sign-in/account system (not public content)")
    if labels & DEV_HOST_LABELS:
        return replace(DEFAULT_PROFILE, host=host, enabled=False,
                       reason="development/test copy of another site (duplicate content)")
    return replace(DEFAULT_PROFILE, host=host)


def crawl_hosts(only: Optional[List[str]] = None) -> List[SiteProfile]:
    """Enabled profiles (optionally restricted to ``only``) in a stable order."""
    profiles = [p for p in SITES.values() if p.enabled]
    if only:
        wanted = {h.lower() for h in only}
        unknown = wanted - set(SITES)
        profiles = [p for p in SITES.values() if p.host in wanted]
        profiles += [replace(DEFAULT_PROFILE, host=h) for h in sorted(unknown) if is_nyit_host(h)]
    return profiles


def describe_sites() -> str:
    """Human-readable table of every profile (used by the `sites` command)."""
    rows = [("HOST", "CRAWLED", "NAME", "WHY")]
    for p in SITES.values():
        rows.append((p.host, "yes" if p.enabled else "no", p.name, p.reason))
    rows.append(("*.arktos.nyit.edu, sso., *-sso., login., ...", "no", "Proxy / sign-in systems",
                 "library EZproxy (licensed databases, login) and account/sign-in hosts"))
    rows.append(("*-dev, *staging*, test.*, ...", "no", "Development copies",
                 "test/staging copies of real sites (duplicate content)"))
    rows.append(("other *.nyit.edu", "yes (max %d pages)" % DEFAULT_PROFILE.max_pages,
                 DEFAULT_PROFILE.name, DEFAULT_PROFILE.reason))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    return "\n".join(
        f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]:<{widths[2]}}  {r[3]}" for r in rows
    )
