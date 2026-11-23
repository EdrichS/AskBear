"""Regression tests for the pipeline/documents/stats/validate/cli fixes of the review round:
TREC-safe course ids, canonical copies of duplicate pages, repeated policy passages, short
sections, near-duplicate passages, PDF titles, editions, categories and text hygiene,
STATS.md wording and writing, validate messages and checks, CLI options."""

from __future__ import annotations

import json
import os
import random
from collections import Counter
from datetime import date

import pytest

from askbear_scraper import cli, documents as dc, pdf_catalog, pipeline, stats, validate
from askbear_scraper.config import Settings
from askbear_scraper.models import CorpusRecord, CourseRecord, PageRecord, SectionRecord

from testkit import (build_fixture_crawl, cache_page, fixture_html, read_jsonl,
                     write_jsonl)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def page(url, text="same text " * 10, **kw) -> PageRecord:
    from askbear_scraper.urls import url_host
    from askbear_scraper.utils import sha1, short_hash

    values = dict(doc_id="page-" + short_hash(url), url=url, title="T", page_title="T", breadcrumbs=[],
                  catalog_year="", page_type="page", area=None, school=None, program=None, degree=None,
                  level=None, campuses=[], headings=[], text=text, word_count=len(text.split()),
                  num_tables=0, links=[], content_sha1=sha1(text), fetched_at="", site=url_host(url))
    values.update(kw)
    return PageRecord(**values)


def section(pg: PageRecord, i: int, text: str, heading: str = "", **kw) -> SectionRecord:
    values = dict(section_id=f"{pg.doc_id}-s{i:03d}", doc_id=pg.doc_id, url=pg.url, page_title=pg.title,
                  heading=heading, heading_path=[heading] if heading else [], text=text,
                  word_count=len(text.split()), order=i, page_type=pg.page_type, program=pg.program,
                  level=pg.level, site=pg.site)
    values.update(kw)
    return SectionRecord(**values)


def course(code="CSCI 260", level_code="UG", description="Data structures and algorithms.", **kw):
    subject, number = code.split(" ")
    values = dict(course_id=f"{code}|{level_code}", code=code, subject=subject, subject_name="Computer Science",
                  number=number, title="Data Structures", credits="3.0", credits_min=3.0, credits_max=3.0,
                  school="College of Eng & Comp Sciences", level="undergraduate", level_code=level_code,
                  description=description, prerequisites=None, corequisites=None, hours=None, attributes={},
                  text=f"{code}: Data Structures\n{description}",
                  url=f"https://catalog.nyit.edu/courses?cal={level_code}&cs={subject}")
    values.update(kw)
    return CourseRecord(**values)


LONG = ("Students who believe a grade was assigned in error may appeal it in writing to the "
        "instructor within ten business days after grades are posted, and then to the chair.")


# ---------------------------------------------------------------------------
# corpus ids (TREC run/qrels files are split on whitespace)
# ---------------------------------------------------------------------------
def test_course_corpus_ids_have_no_whitespace():
    assert pipeline.course_corpus_id("CSCI 260|UG") == "course-CSCI-260-UG"
    assert pipeline.course_corpus_id("MTIM 893|PROF|2") == "course-MTIM-893-PROF-2"
    rec = pipeline.course_to_corpus(course())
    assert rec.id == "course-CSCI-260-UG" and rec.metadata["course_id"] == "CSCI 260|UG"
    run_line = f"q1 Q0 {rec.id} 1 9.5 bm25"
    assert len(run_line.split()) == 6                          # a valid TREC run line
    assert len(f"q1 0 {rec.id} 1".split()) == 4                # a valid qrels line


def test_validate_fails_corpus_ids_with_whitespace(settings):
    settings.ensure_dirs()
    write_jsonl(settings.processed_dir / "corpus.jsonl", [
        {"id": "course:CSCI 260|UG", "source": "course", "url": "https://catalog.nyit.edu/courses?cal=UG&cs=CSCI",
         "title": "CSCI 260", "text": "Data structures.", "metadata": {"site": "catalog.nyit.edu"}},
        {"id": "page-abc-s000", "source": "section", "url": "https://www.nyit.edu/a/", "title": "A",
         "text": "Some text.", "metadata": {"site": "www.nyit.edu"}}])
    result = validate.check_corpus_ids(validate.load_inputs(settings))
    assert result.status == "FAIL" and result.count == 1 and "course:CSCI 260|UG" in result.examples[0]


# ---------------------------------------------------------------------------
# exact-duplicate pages: the canonical copy
# ---------------------------------------------------------------------------
def test_malformed_percent20_url_never_wins_over_the_clean_copy():
    clean = page("https://www.nyit.edu/academics/engineering-and-computing-sciences/electrical-and-computer-engineering/")
    typo = page("https://www.nyit.edu/academics/%20engineering-and-computing-sciences%20/electrical-and-computer-engineering/")
    assert pipeline.mark_duplicates([clean, typo]) == 1
    assert clean.duplicate_of is None and typo.duplicate_of == clean.url


def test_declared_rel_canonical_wins_and_canonicals_outside_the_group_are_ignored():
    upper = page("https://vancouver.nyit.edu/Give/")
    lower = page("https://vancouver.nyit.edu/give/")
    bio_alias, bio = page("https://www.nyit.edu/bio/Anu.Raj", text="bio " * 20), page("https://www.nyit.edu/bio/anu-raj/", text="bio " * 20)
    # apps.nyit.edu policies all declare https://www.nyit.edu/ (outside their group): no effect
    p1, p2 = page("https://apps.nyit.edu/policies/b", text="pol " * 20), page("https://apps.nyit.edu/policies/a", text="pol " * 20)
    canonical = {upper.url: lower.url, lower.url: lower.url, bio_alias.url: bio.url,
                 p1.url: "https://www.nyit.edu/", p2.url: "https://www.nyit.edu/"}
    pipeline.mark_duplicates([upper, lower, bio_alias, bio, p1, p2], canonical_urls=canonical)
    assert upper.duplicate_of == lower.url and lower.duplicate_of is None
    assert bio_alias.duplicate_of == bio.url
    assert p1.duplicate_of == p2.url                         # the usual rule decides


def test_sitemap_url_and_lower_case_are_preferred():
    listed, other = page("https://www.nyit.edu/news/Articles/"), page("https://www.nyit.edu/news/articles/x/")
    pipeline.mark_duplicates([listed, other], sitemap_urls={listed.url})
    assert other.duplicate_of == listed.url
    a, b = page("https://www.nyit.edu/student-life/long-Island/"), page("https://www.nyit.edu/student-life/long-island/")
    pipeline.mark_duplicates([a, b])
    assert a.duplicate_of == b.url


def test_parse_uses_the_manifest_canonical(settings):
    settings.ensure_dirs()
    html = fixture_html("www_news.html")
    good = "https://www.nyit.edu/news/articles/clean-url/"
    alias = "https://www.nyit.edu/news/articles/Clean-URL/"
    write_jsonl(settings.manifest_path, [
        cache_page(settings, alias, html, canonical=good),
        cache_page(settings, good, html, canonical=good),
    ])
    pipeline.run_parse(settings, corpus_with_docs=False)
    pages = {p["url"]: p for p in read_jsonl(settings.processed_dir / "pages.jsonl")}
    assert pages[good]["duplicate_of"] is None
    assert pages[alias]["duplicate_of"] == good


# ---------------------------------------------------------------------------
# passages repeated on one website (policy page + handbook)
# ---------------------------------------------------------------------------
def test_policy_passage_reprinted_in_a_handbook_is_kept_once():
    policy = page("https://apps.nyit.edu/policies/appeals", text="x " * 120, page_type="policy", title="Appeals")
    handbook = page("https://apps.nyit.edu/policies/collection/student_code_of_conduct", text="x " * 9000,
                    page_type="policy", title="Student Code of Conduct")
    sections = [section(handbook, 64, LONG, heading="Appeals"), section(policy, 0, LONG),
                # a short passage needs the same heading: page title (+ part) when it has none
                section(policy, 1, "Appeals are decided within thirty days.", heading="(part 2)"),
                section(handbook, 65, "Appeals are decided within thirty days.", heading="Appeals (part 2)")]
    kept, removed = pipeline.drop_repeated_sections(sections, [policy, handbook])
    assert [s.section_id for s in kept] == [f"{policy.doc_id}-s000", f"{policy.doc_id}-s001"]
    assert removed == {"apps.nyit.edu": 2}


def test_short_passages_under_different_headings_stay():
    a = page("https://www.nyit.edu/a/", text="x " * 50)
    b = page("https://www.nyit.edu/b/", text="x " * 60)
    sections = [section(a, 0, "Call us at 516.686.1000 today.", heading="Admissions"),
                section(b, 0, "Call us at 516.686.1000 today.", heading="Registrar")]
    kept, removed = pipeline.drop_repeated_sections(sections, [a, b])
    assert len(kept) == 2 and removed == {}


# ---------------------------------------------------------------------------
# short sections
# ---------------------------------------------------------------------------
def test_tiny_sections_are_merged_into_a_neighbour():
    pg = page("https://catalog.nyit.edu/semester-map/x", text="x " * 50)
    first = section(pg, 0, "Updated: 2026-09-01", heading="")
    year = section(pg, 1, "CSCI 101 Intro 3 credits CSCI 102 Lab 1 credit", heading="Year One")
    total = section(pg, 2, "TOTAL CREDITS: 30", heading="Fall")
    lonely_page = page("https://www.nyit.edu/lonely/", text="x " * 10)
    lonely = section(lonely_page, 0, "Coming soon")
    kept, merged = pipeline.merge_tiny_sections([first, year, total, lonely])
    assert merged == 2 and [s.section_id for s in kept] == [year.section_id, lonely.section_id]
    assert year.text == "Updated: 2026-09-01\nCSCI 101 Intro 3 credits CSCI 102 Lab 1 credit\nFall\nTOTAL CREDITS: 30"
    assert year.word_count == len(year.text.split())


def test_short_document_pages_are_not_indexed():
    docs = [{"doc_id": "d1", "pdf_url": "https://www.nyit.edu/files/a.pdf", "text": "Page 3", "page": 1},
            {"doc_id": "d2", "pdf_url": "https://www.nyit.edu/files/a.pdf", "text": "A real page of text here.", "page": 2}]
    left_out: Counter = Counter()
    records, _ = pipeline.build_corpus([], [], documents=docs, left_out=left_out)
    assert [r.id for r in records] == ["d2"] and left_out == {"too_short": 1}


# ---------------------------------------------------------------------------
# near-duplicate passages
# ---------------------------------------------------------------------------
def _sec_rec(rid, site, text, title="T", program=None, doc_id=None, source="section", **meta):
    metadata = {"site": site, "doc_id": doc_id or rid.rsplit("-s", 1)[0], "program": program, "level": None}
    metadata.update(meta)
    return CorpusRecord(rid, source, f"https://{site}/{rid}", title, f"{title}\n{text}", metadata)


BLOCK = ("The Family Educational Rights and Privacy Act affords students certain rights with respect "
         "to their education records, including the right to inspect and review them within 45 days "
         "of the day the university receives a request for access, and the right to request the "
         "amendment of records that the student believes are inaccurate or misleading.")


def test_near_duplicates_keep_the_authoritative_copy_and_group_other_programs():
    recs = [
        _sec_rec("page-www-s001", "www.nyit.edu", BLOCK, title="Privacy"),
        _sec_rec("page-cat-s003", "catalog.nyit.edu", BLOCK + " Contact the registrar.", title="FERPA"),
        _sec_rec("page-mba-s004", "www.nyit.edu", "STEM OPT " + BLOCK, title="MBA", program="Management, M.B.A."),
        _sec_rec("page-cs-s004", "www.nyit.edu", "STEM OPT " + BLOCK, title="CS", program="Computer Science, B.S."),
        CorpusRecord("doc-x-p0001", "document", "https://files.nyit.edu/x.pdf#page=1", "X (PDF, p. 1)",
                     "X\n" + BLOCK, {"site": "files.nyit.edu", "modified": "2025-01-01"}),
        _sec_rec("page-other-s000", "www.nyit.edu", "A completely different passage about parking permits "
                                                     "and where to buy them on the Long Island campus."),
    ]
    bodies = {r.id: r.text.split("\n", 1)[1] for r in recs}
    kept, dropped, summary = pipeline.collapse_near_duplicates(recs, bodies)
    ids = [r.id for r in kept]
    assert "page-cat-s003" in ids and dropped["page-www-s001"] == "page-cat-s003"   # catalog > www
    assert dropped["doc-x-p0001"] == "page-cat-s003"                                 # web page > PDF
    mba, cs = (next(r for r in kept if r.id == i) for i in ("page-mba-s004", "page-cs-s004"))
    assert mba.metadata["dup_group"] == cs.metadata["dup_group"] and mba.metadata["dup_count"] == 3
    assert "page-other-s000" in ids and "dup_group" not in next(r for r in kept if r.id == "page-other-s000").metadata
    assert summary["left_out"] == 2 and summary["left_out_ids"] == {"document": ["doc-x-p0001"]}


def test_sections_of_one_page_and_cohort_documents_are_not_collapsed():
    sem_a = _sec_rec("page-map-s001", "catalog.nyit.edu", "CHEM 900 Dissertation Research " * 3, title="Fall, Year Four")
    sem_b = _sec_rec("page-map-s002", "catalog.nyit.edu", "CHEM 900 Dissertation Research " * 3, title="Summer, Year Two")
    map25 = CorpusRecord("doc-a-p0001", "document", "https://files.nyit.edu/m25.pdf#page=1", "Map 2025",
                         "Map 2025\n" + BLOCK, {"site": "files.nyit.edu", "cohort_year": 2025})
    map26 = CorpusRecord("doc-b-p0001", "document", "https://files.nyit.edu/m26.pdf#page=1", "Map 2026",
                         "Map 2026\n" + BLOCK, {"site": "files.nyit.edu", "cohort_year": 2026})
    recs = [sem_a, sem_b, map25, map26]
    bodies = {r.id: r.text.split("\n", 1)[1] for r in recs}
    kept, dropped, _ = pipeline.collapse_near_duplicates(recs, bodies)
    assert not dropped and len(kept) == 4
    assert sem_a.metadata["dup_group"] == sem_b.metadata["dup_group"]


def test_identical_full_text_is_kept_once_even_for_different_programs():
    a = _sec_rec("page-a-s017", "www.nyit.edu", BLOCK, title="Management, M.B.A. — Accreditation", program="MBA Finance")
    b = _sec_rec("page-b-s017", "www.nyit.edu", BLOCK, title="Management, M.B.A. — Accreditation", program="MBA Marketing")
    kept, dropped, _ = pipeline.collapse_near_duplicates([a, b], {a.id: BLOCK, b.id: BLOCK})
    assert len(kept) == 1 and len(dropped) == 1


def test_similar_pairs_is_exact():
    rng = random.Random(4)
    vocab = [f"w{i}" for i in range(60)]
    base = [rng.choice(vocab) for _ in range(80)]
    texts = []
    for i in range(40):
        words = list(base) if i % 3 else [rng.choice(vocab) for _ in range(80)]
        for _ in range(rng.randint(0, 6)):
            words[rng.randrange(len(words))] = rng.choice(vocab)
        texts.append(" ".join(words))
    texts.append("too short")
    texts.append(texts[1])                                   # an exact copy
    sets = pipeline.shingle_sets(texts)
    assert sets[-2] is None
    expected = set()
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if sets[i] is None or sets[j] is None:
                continue
            a, b = set(sets[i]), set(sets[j])
            if len(a & b) / len(a | b) >= 0.9:
                expected.add((i, j))
    found = pipeline.similar_pairs(sets, 0.9)
    assert expected and set(found) <= expected                 # never a false pair
    # ... and the same groups as the brute-force join (copies are linked to their first copy)
    assert pipeline._groups(len(sets), found) == pipeline._groups(len(sets), expected)


def test_parse_marks_left_out_sections_and_summarises(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    build_fixture_crawl(settings, extras=False)
    summary = pipeline.run_parse(settings)
    near = summary["near_duplicates"]
    assert near["enabled"] and near["threshold"] == pipeline.NEAR_DUP_JACCARD
    sections = read_jsonl(settings.processed_dir / "sections.jsonl")
    corpus_ids = {r["id"] for r in read_jsonl(settings.processed_dir / "corpus.jsonl")}
    for s in sections:
        assert "campuses" in s and "duplicate_of" in s
        if s["duplicate_of"]:
            assert s["section_id"] not in corpus_ids and s["duplicate_of"] in corpus_ids
    assert summary["sections_left_out_as_near_duplicates"] == sum(1 for s in sections if s["duplicate_of"])
    off = pipeline.run_parse(settings, corpus_with_near_duplicates=True)
    assert off["near_duplicates"] == {"enabled": False}


# ---------------------------------------------------------------------------
# campuses, per-site counts
# ---------------------------------------------------------------------------
def test_section_corpus_records_carry_the_page_campuses(parsed_dataset):
    corpus = read_jsonl(parsed_dataset.processed_dir / "corpus.jsonl")
    sections = [r for r in corpus if r["source"] == "section"]
    assert all(isinstance(r["metadata"]["campuses"], list) for r in sections)
    pages = {p["doc_id"]: p for p in read_jsonl(parsed_dataset.processed_dir / "pages.jsonl")}
    for r in sections:
        assert r["metadata"]["campuses"] == pages[r["metadata"]["doc_id"]]["campuses"]
    assert any(r["metadata"]["campuses"] for r in sections)


def test_parse_summary_counts_every_corpus_record_per_site(parsed_dataset):
    summary = json.loads((parsed_dataset.processed_dir / "parse_summary.json").read_text(encoding="utf-8"))
    corpus = read_jsonl(parsed_dataset.processed_dir / "corpus.jsonl")
    catalog = summary["by_site"]["catalog.nyit.edu"]
    assert catalog["corpus_records"] == sum(1 for r in corpus if r["metadata"]["site"] == "catalog.nyit.edu")
    assert catalog["corpus_by_source"]["course"] == 7
    assert sum(r["corpus_records"] for r in summary["by_site"].values()) == summary["corpus"]


# ---------------------------------------------------------------------------
# PDF documents: titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, clean, quality", [
    ("M A G A Z I N E", "MAGAZINE", "weak"),
    ("Hear\\", "Hear", "junk"),
    ("HEAR\\", "HEAR", "junk"),
    ("Page 1 of 2", "Page 1 of 2", "junk"),
    ("Name", "Name", "junk"),
    ("maid", "maid", "junk"),
    ("NEW YORK INSTITUTE OF TECHNOLOGY", "NEW YORK INSTITUTE OF TECHNOLOGY", "junk"),
    ("School of Management", "School of Management", "weak"),
    ("Agenda", "Agenda", "weak"),
    ("2018 Program", "2018 Program", "weak"),
    ("Suffolk County NEW York Institute OF Technology", "Suffolk County", "good"),
    ("Invite YOU to Join \x03 the Legacy", "Invite YOU to Join the Legacy", "good"),
    ("Marketing Plan", "Marketing Plan", "good"),
    ("Permission to Take Courses at Another College at New York Institute of Technology",
     "Permission to Take Courses at Another College at New York Institute of Technology", "good"),
])
def test_clean_title_and_title_quality(raw, clean, quality):
    assert dc.clean_title(raw) == clean
    assert dc.title_quality(clean) == quality


def test_pick_title_skips_junk_and_prefers_real_titles():
    url = "https://files.nyit.edu/files/magazine/NYITMagazine_V07N02_Spring2009.pdf"
    assert dc.pick_title("", "M A G A Z I N E", [], url) == ("NYIT Magazine V07N02 Spring 2009", "file_name")
    assert dc.pick_title("", None, ["Agenda"], "https://files.nyit.edu/files/CPIDay_Agenda_2019.pdf") == \
        ("CPI Day Agenda 2019", "file_name")
    assert dc.pick_title("NEW YORK INSTITUTE OF TECHNOLOGY", None, ["Grade Appeals Procedure"],
                         "https://files.nyit.edu/files/x.pdf") == ("Grade Appeals Procedure", "link_text")
    assert dc.pick_title("", "Hear\\", [], "https://files.nyit.edu/files/Grand-River-Training-Day-One.pdf")[0] == \
        "Grand River Training Day One"
    # a link that only names a month is added to the title
    assert dc.pick_title("Spring 22 Enrichment News Letter", None, ["April 2022"],
                         "https://files.nyit.edu/files/April_22_EnrichmentNewsletter.pdf")[0] == \
        "Spring 22 Enrichment News Letter (April 2022)"


def test_shared_titles_are_told_apart():
    def doc(name, title, edition=None, anchors=()):
        return {"pdf_url": f"https://files.nyit.edu/files/{name}.pdf", "title": title, "edition": edition,
                "_anchors": list(anchors), "_alt_titles": []}
    docs = [doc("NYITCOM_Placement_2016", "Internship Placement by Specialty", "2016"),
            doc("NYITCOM_Placement_2017", "Internship Placement by Specialty", "2017"),
            doc("NYITCOM_PlacementAR_2017", "Internship Placement by Specialty", "2017"),
            doc("ADM_QCC_BioToBio", "Queensborough Community College", None,
                ["Transfer Credits and Required Courses for B.S. in Biology"]),
            doc("ADM_QCC_MathToMath", "Queensborough Community College", None,
                ["Transfer Credits and Required Courses for B.S. in Mathematics"]),
            doc("SGA_Agenda_20250910", "Student Government Association", "2025-09-10", ["Agenda"]),
            doc("SGA_Minutes_20250910", "Student Government Association", "2025-09-10", ["Minutes"]),
            doc("SGA_Agenda_20251022", "Student Government Association", "2025-10-22", ["Agenda"]),
            doc("SGA_Minutes_20251022", "Student Government Association", "2025-10-22", ["Minutes"]),
            doc("Unique_Thing", "A Unique Title")]
    counts = dc.disambiguate_titles(docs)
    titles = [d["title"] for d in docs]
    assert titles[0] == "Internship Placement by Specialty (2016)"
    assert titles[1] == "Internship Placement by Specialty (2017)"
    assert titles[2] == "Internship Placement by Specialty (2017, AR)"
    assert titles[3] == "Queensborough Community College (Transfer Credits and Required Courses for B.S. in Biology)"
    assert titles[5] == "Student Government Association (2025-09-10, Agenda)"
    assert titles[8] == "Student Government Association (2025-10-22, Minutes)"
    assert titles[9] == "A Unique Title" and "title_qualifier" not in docs[9]
    assert counts["pdfs_sharing_a_title"] == 0 and len(set(titles)) == len(titles)


def test_one_file_in_two_formats_is_no_title_clash(settings):
    base = "https://files.nyit.edu/files/academic_affairs/AA_OSPAR_IRB_ProtocolRenewalForm"
    assert dc.one_file_in_other_formats([base + ".pdf", base + ".docx"])
    assert not dc.one_file_in_other_formats([base + ".pdf", base + "2.pdf"])
    assert not dc.one_file_in_other_formats([base + ".pdf", base + ".PDF"])   # two copies, one format
    assert not dc.one_file_in_other_formats([base + ".pdf", "https://files.nyit.edu/files/x/Form.docx"])
    docs = [{"pdf_url": base + ext, "title": "AA OSPAR IRB Protocol Renewal Form"} for ext in (".pdf", ".docx")]
    docs += [{"pdf_url": f"https://files.nyit.edu/files/{n}.pdf", "title": "Minutes"} for n in "ab"]
    assert dc.shared_titles(docs) == [["https://files.nyit.edu/files/a.pdf", "https://files.nyit.edu/files/b.pdf"]]

    settings.ensure_dirs()
    write_jsonl(settings.processed_dir / "documents.jsonl", [
        {"doc_id": f"doc-{ext}-p0001", "pdf_url": base + "." + ext, "url": f"{base}.{ext}#page=1",
         "site": "files.nyit.edu", "title": "AA OSPAR IRB Protocol Renewal Form", "toc_path": [],
         "text": "Protocol renewal form text", "page": 1, "page_count": 1, "word_count": 4,
         "file_type": ext} for ext in ("pdf", "docx")])
    check = validate.check_document_titles(validate.load_inputs(settings))
    assert check.status == "PASS" and "2 files published both as PDF and as Word" in check.message


# ---------------------------------------------------------------------------
# PDF documents: editions, series, categories
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, label", [
    ("NYITCOM_StudentHandbook20232024", "2023-24"),
    ("REGISTRAR_AcademicSenateCalendar_FA2020", "Fall 2020"),
    ("NYITCOM_SGA_Agenda_20250910", "2025-09-10"),
    ("PLAN_InstitutionalAssessment_GlobalFactSheet_AY1718", "2017-18"),
    ("AY_2024-25_Global_Fact_Sheet", "2024-25"),
    ("NYITAAUP_2015FiscalYear", "FY2015"),
    ("NYITAAUP_fy1998", "FY1998"),
    ("Academic_Calendar_-_Year_3_-_Class_of_2025", "Class of 2025"),
    ("EnrichmentEdition_032021", "2021-03"),
    ("NYITMagazine_V07N02_Spring2009", "Spring 2009"),
    ("FACT_SHEET_FALL_17_US", "Fall 2017"),
    ("GIVING_PresidentsReport_08-09", "2008-09"),
    ("2020-NAAB-Procedures_for_Accreditation_Rev_May_2025", "2025-05"),
    ("NYITAAUPCBA_2022-2027", "2022-2027"),
    ("FA_2025-26_Verification_Worksheet", "2025-26"),      # "FA" = financial aid here, not fall
    ("ADM_Agencies", None),
])
def test_edition_of(name, label):
    edition = dc.edition_of(name)
    assert (edition.label if edition else None) == label


def test_series_name():
    assert dc.series_name("NYITCOM_StudentHandbook20242025") == "nyitcom student handbook"
    assert dc.series_name("NYITMagazine_V07N02_Spring2009") == dc.series_name("NYIT_Magazine_Fall_2022_16_LLR")
    assert dc.series_name("AA_OSPAR_ISRCTLT_ApplicationGuidelines_2027-1") == \
        dc.series_name("AA_OSPAR_ISRCTLT_ApplicationGuidelines")
    assert dc.series_name("SOM_MP_MGMT650_A1Module") != dc.series_name("SOM_MP_MGMT650_A10Module")


def test_older_editions_are_superseded_but_current_terms_and_semester_maps_are_not():
    base = "https://files.nyit.edu/files/"

    def doc(name, modified=None):
        return {"pdf_url": base + name + ".pdf", "site": "files.nyit.edu", "title": name, "modified": modified}
    docs = [doc("NYITCOM_StudentHandbook20232024"), doc("NYITCOM_StudentHandbook20242025"),
            doc("NYITCOM_StudentHandbook20252026"),
            doc("REGISTRAR_AcademicSenateCalendar_FA2020"), doc("REGISTRAR_AcademicSenateCalendar_FA2026"),
            doc("REGISTRAR_AcademicSenateCalendar_SP2027"),
            doc("advising/semester_maps_2025/ADV_DegreeMap_COECS_ComputerScienceBS_2025"),
            doc("advising/semester_maps_2026/ADV_DegreeMap_COECS_ComputerScienceBS_2026"),
            doc("CareerGuide", modified="2025-03-01"), doc("CareerGuide2024"),
            doc("Campus_Map")]
    counts = dc.annotate_documents(docs, reference=date(2026, 11, 23))
    by = {d["pdf_url"].rsplit("/", 1)[-1][:-4]: d for d in docs}
    assert by["NYITCOM_StudentHandbook20232024"]["superseded"]
    assert by["NYITCOM_StudentHandbook20242025"]["superseded_by"] == base + "NYITCOM_StudentHandbook20252026.pdf"
    assert not by["NYITCOM_StudentHandbook20252026"]["superseded"]
    assert by["REGISTRAR_AcademicSenateCalendar_FA2020"]["superseded"]
    assert not by["REGISTRAR_AcademicSenateCalendar_FA2026"]["superseded"]     # this term is not over
    assert not by["REGISTRAR_AcademicSenateCalendar_SP2027"]["superseded"]
    maps = [by["ADV_DegreeMap_COECS_ComputerScienceBS_2025"], by["ADV_DegreeMap_COECS_ComputerScienceBS_2026"]]
    assert [m["cohort_year"] for m in maps] == [2025, 2026] and not any(m["superseded"] for m in maps)
    assert by["CareerGuide2024"]["superseded"] and not by["CareerGuide"]["superseded"]   # dated by modification
    assert by["Campus_Map"]["series"] is None and by["Campus_Map"]["doc_year"] is None
    assert by["NYITCOM_StudentHandbook20232024"]["doc_year"] == 2024
    assert counts["superseded_documents"] == 4 and counts["cohort_documents"] == 2


@pytest.mark.parametrize("url, title, category", [
    ("https://files.nyit.edu/files/advising/semester_maps_2026/ADV_DegreeMap_X_2026.pdf", "", "semester_map"),
    ("https://files.nyit.edu/files/admissions/ADM_QCCArticulationWeb_BioToBio.pdf", "", "transfer_agreement"),
    ("https://aaupatnyit.nyit.edu/wp-content/uploads/NYITAAUP_fy2001.pdf", "Fiscal Year 2001", "financial_filing"),
    ("https://files.nyit.edu/files/registrar/REGISTRAR_AcademicSenateCalendar_FA2026.pdf", "", "schedule_calendar"),
    ("https://files.nyit.edu/files/medicine/NYITCOM_StudentHandbook20252026.pdf", "", "policy_handbook"),
    ("https://files.nyit.edu/files/registrar/REGISTRAR_Forms_ApplicationToDeclareUGMinor.pdf", "", "form"),
    ("https://files.nyit.edu/files/management/SOM_MP_ACCT101_MasterSyllabus.pdf", "", "course_material"),
    ("https://files.nyit.edu/files/magazine/NYITMagazine_V07N02_Spring2009.pdf", "", "newsletter_magazine"),
    ("https://files.nyit.edu/files/x/Campus_Map.pdf", "", "brochure"),
])
def test_doc_category(url, title, category):
    assert dc.doc_category(url, title) == category


def test_annotate_page_records_for_an_old_documents_file():
    recs = [{"doc_id": f"doc-a-p000{i}", "pdf_url": "https://files.nyit.edu/files/Handbook_2023-24.pdf", "site": "files.nyit.edu",
             "title": "Handbook", "word_count": 10, "text": "x"} for i in (1, 2)]
    recs += [{"doc_id": "doc-b-p0001", "pdf_url": "https://files.nyit.edu/files/Handbook_2025-26.pdf", "site": "files.nyit.edu",
              "title": "Handbook", "word_count": 0, "text": ""}]
    dc.annotate_page_records(recs, reference=date(2026, 11, 23))
    assert [r["superseded"] for r in recs] == [True, True, False]
    assert recs[2]["scanned"] and not recs[0]["scanned"]
    assert all(r["doc_category"] == "policy_handbook" for r in recs) and recs[0]["edition"] == "2023-24"


def test_superseded_document_pages_are_left_out_unless_asked():
    docs = [{"doc_id": "old-p1", "pdf_url": "https://files.nyit.edu/files/H_2023-24.pdf", "page": 1,
             "text": "The old handbook says something about parking.", "superseded": True,
             "doc_category": "policy_handbook"},
            {"doc_id": "new-p1", "pdf_url": "https://files.nyit.edu/files/H_2025-26.pdf", "page": 1,
             "text": "The new handbook says something else about parking.", "doc_category": "policy_handbook"}]
    left: Counter = Counter()
    records, _ = pipeline.build_corpus([], [], documents=docs, left_out=left)
    assert [r.id for r in records] == ["new-p1"] and left == {"superseded": 1}
    assert records[0].metadata["doc_category"] == "policy_handbook" and records[0].metadata["source_weight"] == 1.0
    records, _ = pipeline.build_corpus([], [], documents=docs, with_superseded=True)
    old = next(r for r in records if r.id == "old-p1")
    assert old.metadata["superseded"] and old.metadata["source_weight"] == 0.5


# ---------------------------------------------------------------------------
# PDF text hygiene
# ---------------------------------------------------------------------------
def test_tidy_text_removes_glyph_runs_and_joins_letter_spaced_words():
    raw = ('If "Yes," complete Schedule C, Part I m m m m m m m m m\n'
           "a | m m m m m m m m\nS P R I N G 2 0 0 9\ns e r v i n g suggestions\n"
           " | Faculty innovation\nRate it: 1 2 3 4 5 6 7\nPart V | m m m m m Yes | No")
    assert dc.tidy_text(raw) == ('If "Yes," complete Schedule C, Part I\na\nSPRING 2009\nserving suggestions\n'
                                 "\N{BULLET} Faculty innovation\nRate it: 1 2 3 4 5 6 7\nPart V | Yes | No")


def test_bookmarks_and_titles_lose_control_and_private_use_characters():
    toc = dc.clean_toc([[1, "II. \x07Presenters at Meetings", 1], [1, " Faculty innovation in the usa", 2]])
    assert toc == [[1, "II. Presenters at Meetings", 1], [1, "Faculty innovation in the usa", 2]]
    entries = pdf_catalog._toc_entries([[1, "General\x07 Information", 1]], 3)
    assert entries == [(1, "General Information", 1)]


def test_validate_flags_pdf_leftovers_and_shared_titles(settings):
    settings.ensure_dirs()
    pdf = "https://files.nyit.edu/files/"
    write_jsonl(settings.processed_dir / "documents.jsonl", [
        {"doc_id": "doc-a-p0001", "pdf_url": pdf + "a.pdf", "url": pdf + "a.pdf#page=1", "site": "files.nyit.edu",
         "title": "Presentation", "toc_path": ["II. \x07Presenters"], "text": "Total m m m m m m", "page": 1,
         "page_count": 1, "word_count": 7},
        {"doc_id": "doc-b-p0001", "pdf_url": pdf + "b.pdf", "url": pdf + "b.pdf#page=1", "site": "files.nyit.edu",
         "title": "presentation", "toc_path": [], "text": " S P R I N G", "page": 1, "page_count": 1,
         "word_count": 6},
        {"doc_id": "doc-c-p0001", "pdf_url": pdf + "c.pdf", "url": pdf + "c.pdf#page=1", "site": "files.nyit.edu",
         "title": "Page 1 of 2", "toc_path": [], "text": "fine text", "page": 1, "page_count": 1, "word_count": 2},
    ])
    data = validate.load_inputs(settings)
    hygiene = validate.check_hygiene_documents(data)
    found = " ".join(hygiene.examples)
    for what in ("control character", "single-glyph run", "private-use glyph", "letter-spaced word"):
        assert what in found, what
    titles = validate.check_document_titles(data)
    assert titles.status == "WARN" and any("2 PDFs titled" in e for e in titles.examples)
    assert any("Page 1 of 2" in e for e in titles.examples)


# ---------------------------------------------------------------------------
# docs end to end: new fields, unread Office files
# ---------------------------------------------------------------------------
def test_docs_step_writes_editions_titles_and_unread_files(settings, tmp_path):
    from testkit import FakeWeb, make_fetcher, make_pdf

    base = "https://files.nyit.edu/files/medicine/"
    web = FakeWeb()
    links = []
    for year in ("20232024", "20242025"):
        url = f"{base}NYITCOM_StudentHandbook{year}.pdf"
        body = make_pdf(tmp_path / f"h{year}.pdf", [["M A G A Z I N E", f"Rules for {year} students are here."]],
                        metadata={"title": "Microsoft Word - handbook.docx"}).read_bytes()
        web.add(url, body, content_type="application/pdf")
        links.append({"url": url, "kind": "pdf", "anchor_text": "Student Handbook", "found_on": "https://www.nyit.edu/a/"})
    links.append({"url": "https://files.nyit.edu/files/admissions/ADM_Map.xlsx", "kind": "asset",
                  "anchor_text": "Map", "found_on": "https://www.nyit.edu/a/"})
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, links)
    summary = dc.run_docs(settings, fetcher=make_fetcher(settings, web))
    assert summary["not_extracted_files"]["by_type"] == {".xlsx": 1}
    assert summary["titles"]["pdfs_sharing_a_title"] == 0
    assert summary["editions"]["superseded_documents"] == 1 and summary["editions"]["series"] == 1
    records = read_jsonl(settings.processed_dir / "documents.jsonl")
    by_year = {r["edition"]: r for r in records}
    assert by_year["2023-24"]["superseded"] and not by_year["2024-25"]["superseded"]
    assert by_year["2023-24"]["title"] == "Student Handbook (2023-24)"
    assert {r["doc_category"] for r in records} == {"policy_handbook"}
    web.requests.clear()
    again = dc.run_docs(settings, max_docs=0, fetcher=make_fetcher(settings, web))   # re-extract only
    assert web.requests == [] and again["records"] == len(records)


# ---------------------------------------------------------------------------
# stats: STATS.md writing and wording
# ---------------------------------------------------------------------------
def test_stats_md_is_written_atomically_and_never_through_a_symlink(parsed_dataset, tmp_path):
    import shutil

    data = tmp_path / "data"
    shutil.copytree(parsed_dataset.data_dir, data)
    real = tmp_path / "real_STATS.md"
    real.write_text("the real file\n", encoding="utf-8")
    link = data / "processed" / "STATS.md"
    if link.exists():
        link.unlink()
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    stats.run_stats(Settings(data_dir=data))
    assert real.read_text(encoding="utf-8") == "the real file\n"
    assert not link.is_symlink() and link.read_text(encoding="utf-8").startswith("# AskBear dataset statistics")
    assert not (data / "processed" / "STATS.md.tmp").exists()


def test_stats_wording_hosts_noindex_and_plurals():
    crawl = {"available": True, "urls": 10, "pages_ok": 9, "hosts_with_saved_urls": 4, "sites_with_pages": 3,
             "hosts_not_parsed": ["catalog-dev.nyit.edu"], "first_fetch": None, "last_fetch": None,
             "summary": None, "sites": [], "status_counts": {"ok": 9}}
    kv = dict(next(b for b in stats._crawl_blocks(crawl) if b[0] == "kv")[1])
    assert kv["Pages saved (status ok)"] == ("9 on 4 hosts (3 websites after redirects; catalog-dev.nyit.edu "
                                             "not parsed (disabled in sites.py))")
    pages = {"noindex": 56, "noindex_records": 65}
    assert stats._records_note(pages) == " (65 page records including copies)"
    st = {"crawl": {}, "pages": {}, "sections": {}, "sites": [],
          "courses": {"available": True, "records": 1, "unique_codes": 1, "unique_subjects": 1},
          "documents": {"available": True, "pdfs": 1, "pages": 1, "words": {"total": 129}},
          "pdf": {}, "corpus": {}}
    text = " ".join(stats._overview(st))
    assert "1 PDF document linked from the sites (forms, handbooks, reports...) adds 1 page (129 words)." in text
    assert "adds 1 course (1 unique code in 1 subject)" in text


def test_stats_and_validate_on_the_fixture_dataset_mention_the_new_numbers(parsed_dataset, tmp_path, capsys):
    import shutil

    data = tmp_path / "data"
    shutil.copytree(parsed_dataset.data_dir, data)
    st = stats.run_stats(Settings(data_dir=data))
    assert st["corpus"]["near_duplicates_collapsed"] is True
    assert st["pages"]["noindex"] == 1 and st["pages"]["noindex_records"] == 1
    report = validate.run_validate(Settings(data_dir=data))
    checks = {c["name"]: c for c in report["checks"]}
    for name in ("Corpus ids usable in TREC run/qrels files", "Corpus matches sections and courses",
                 "Corpus: duplicate and near-duplicate passages", "Short passages (< 5 words)"):
        assert checks[name]["status"] == "PASS", (name, checks[name]["message"])
    capsys.readouterr()


# ---------------------------------------------------------------------------
# validate: messages
# ---------------------------------------------------------------------------
def test_page_limit_advice_does_not_suggest_max_pages(settings):
    settings.ensure_dirs()
    (settings.raw_dir / "crawl_summary.json").write_text(json.dumps({
        "sites": ["libguides.nyit.edu"], "capped_sites": {"libguides.nyit.edu": 914}}), encoding="utf-8")
    result = validate.check_site_limits(validate.load_inputs(settings))
    assert result.status == "WARN"
    assert "raise max_pages for that site in sites.py" in result.message
    assert "--max-pages can only lower" in result.message


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_ignore_robots_flag_is_gone(capsys):
    with pytest.raises(SystemExit) as info:
        cli.build_parser().parse_args(["crawl", "--ignore-robots"])
    assert info.value.code == 2
    capsys.readouterr()


def test_data_dir_help_names_the_real_default(capsys):
    with pytest.raises(SystemExit):
        cli.main(["parse", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "./data" not in out and "next to the askbear_scraper package" in out
    assert "--corpus-with-superseded" in out and "--corpus-with-near-duplicates" in out


def test_new_corpus_flags_reach_run_parse(monkeypatch, tmp_path):
    seen = {}

    def fake_run_parse(settings, **kwargs):
        seen.update(kwargs)
        return {}
    monkeypatch.setattr(pipeline, "run_parse", fake_run_parse)
    assert cli.main(["parse", "--corpus-with-superseded", "--corpus-with-near-duplicates",
                     "--data-dir", str(tmp_path / "d")]) == 0
    assert seen["corpus_with_superseded"] and seen["corpus_with_near_duplicates"]


def test_external_links_docstring_is_accurate():
    doc = Settings.external_links_path.__doc__
    assert "e-mail" not in doc and "mailto:" in doc
