"""Regression tests for the fifth review of the `docs` step: titles told apart only within
one file type, file-name qualifiers without "Sto"/"Mathto" fragments, title metadata left
over from a template, link-text fragments and accessibility notes, doc_category rules."""

from __future__ import annotations

import pytest

from askbear_scraper import documents as dc, validate
from testkit import write_jsonl

ADM = "https://files.nyit.edu/files/admissions/"
TRANSFER_CS = "Transfer Credits and Required Courses for B.S. in Computer Science"


def doc(url, title, file_type=None, anchors=(), edition=None):
    out = {"pdf_url": url, "title": title, "edition": edition, "_anchors": list(anchors),
           "_alt_titles": []}
    if file_type:
        out["file_type"] = file_type
    return out


# ---------------------------------------------------------------------------
# 1. titles only need to differ within one file type
# ---------------------------------------------------------------------------
def test_excel_files_titled_like_a_pdf_leave_the_pdf_title_alone():
    docs = [doc(ADM + "ADM_WCCCStoCS.pdf", TRANSFER_CS, "pdf"),
            doc(ADM + "ADM_CMSVMathtoCS.xlsx", TRANSFER_CS, "xlsx"),
            doc(ADM + "ADM_CMSVDataAnalyticstoCS.xlsx", TRANSFER_CS, "xlsx")]
    counts = dc.disambiguate_titles(docs)
    assert docs[0]["title"] == TRANSFER_CS and "title_qualifier" not in docs[0]
    # the two Excel sheets are told apart by the words their file names do not share
    assert docs[1]["title"] == TRANSFER_CS + " (Math)"
    assert docs[2]["title"] == TRANSFER_CS + " (Data Analytics)"
    assert counts["pdfs_sharing_a_title"] == 0


def test_pdf_qualifiers_stay_as_before_the_office_files_came():
    title = "Transfer Credits and Required Courses for B.S. in Construction Engineering"
    docs = [doc(ADM + "ADM_WCCCivilTechToConEng.pdf", title),
            doc(ADM + "ADM_WCCEngSciToConEng.pdf", title),
            doc(ADM + "ADM_CMSVMathtoConEng.xlsx", title, "xlsx")]
    dc.disambiguate_titles(docs)
    assert [d["title"] for d in docs] == [title + " (Civil Tech)", title + " (Sci)", title]


def test_a_number_glued_to_a_title_word_is_no_qualifier():
    title = "Think Pair Share"
    docs = [doc("https://files.nyit.edu/files/ctl/CTL_OperationEngage_ThinkPairShare1.pdf", title),
            doc("https://files.nyit.edu/files/ctl/CTL_OperationEngage_ThinkPairShare.pdf", title)]
    dc.disambiguate_titles(docs)
    assert [d["title"] for d in docs] == ["Think Pair Share (1)", "Think Pair Share"]


def test_course_codes_in_file_names_stay_whole():
    title = "A1 \N{EN DASH} Individual Paper"
    docs = [doc("https://files.nyit.edu/files/management/SOM_MP_ACCT101_A1ReadingAndSummarizing.pdf", title),
            doc("https://files.nyit.edu/files/management/SOM_MP_ACCT110_A1ReadingAndSummarizing.pdf", title)]
    dc.disambiguate_titles(docs)
    assert [d["title_qualifier"] for d in docs] == ["ACCT101", "ACCT110"]


@pytest.mark.parametrize("name, title", [
    ("ADM_WCCCStoCS", "ADM WCCCS to CS"),
    ("ADM_CMSVMathtoCS", "ADM CMSV Math to CS"),
    ("ADM_WCCCISToIT", "ADM WCCCIS To IT"),
    ("ADM_NCC_EEtoECEArticulationWeb", "ADM NCC EE to ECE Articulation Web"),
    ("ADM_BergenITtoITArticulationWeb", "ADM Bergen IT to IT Articulation Web"),
    ("Campus_PhotoGallery", "Campus Photo Gallery"),           # words that end in "to"
    ("TorontoCampusMap", "Toronto Campus Map"),
    ("ADV_DegreeMap_SOHP_ExerciseScienceBS_2025", "ADV Degree Map SOHP Exercise Science BS 2025"),
])
def test_file_title_splits_a_glued_to(name, title):
    assert dc.file_title(f"https://files.nyit.edu/files/x/{name}.pdf") == title


def test_shared_titles_and_validate_compare_titles_within_one_file_type(settings):
    docs = [{"pdf_url": ADM + "ADM_WCCCStoCS.pdf", "title": TRANSFER_CS},
            {"pdf_url": ADM + "ADM_CMSVMathtoCS.xlsx", "title": TRANSFER_CS},
            {"pdf_url": ADM + "a.xlsx", "title": "Budget", "file_type": "xlsx"},
            {"pdf_url": ADM + "b.xlsx", "title": "budget", "file_type": "xlsx"}]
    assert dc.shared_titles(docs) == [[ADM + "a.xlsx", ADM + "b.xlsx"]]

    settings.ensure_dirs()
    path = settings.processed_dir / "documents.jsonl"

    def rec(url, title, ftype):
        return {"doc_id": f"doc-{url[-12:]}-p0001", "pdf_url": url, "url": url + "#page=1",
                "site": "files.nyit.edu", "title": title, "toc_path": [], "text": "Some text here",
                "page": 1, "page_count": 1, "word_count": 3, "file_type": ftype}
    write_jsonl(path, [rec(ADM + "ADM_WCCCStoCS.pdf", TRANSFER_CS, "pdf"),
                       rec(ADM + "ADM_CMSVMathtoCS.xlsx", TRANSFER_CS, "xlsx")])
    check = validate.check_document_titles(validate.load_inputs(settings))
    assert check.status == "PASS"
    assert "within their file type" in check.message and "another file type" in check.message
    write_jsonl(path, [rec(ADM + "a.xlsx", "Budget Plan", "xlsx"),
                       rec(ADM + "b.xlsx", "Budget plan", "xlsx"),
                       rec(ADM + "c.pdf", "Budget Plan", "pdf")])
    check = validate.check_document_titles(validate.load_inputs(settings))
    assert check.status == "WARN" and "same file type" in check.message
    assert any(e.startswith("2 Excel files titled 'Budget Plan'") for e in check.examples)


# ---------------------------------------------------------------------------
# 2. title metadata left over from a template
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("meta, url, first_text, anchors, expected", [
    ("THE CAMPAIGN FOR", "https://files.nyit.edu/files/policies/GivingToNYIT_PledgeConfirmationLetter.docx",
     "[Date]\n[Donor's Address]\nDear [First Name]:\nOn behalf of New York Institute of Technology",
     [], "Giving To NYIT Pledge Confirmation Letter"),
    ("UMB VISA PROCUREMENT CARD POLICIES AND PROCEDURES",
     "https://files.nyit.edu/files/administrative_offices/FA_ProcurementCard_MissingReceiptAffidavit.docx",
     "MISSING RECEIPT AFFIDAVIT\n(Use this form to document a P-Card transaction)", ["Missing Receipt Affidavit"],
     "Missing Receipt Affidavit"),
    ("NYIT VANCOUVER FACULTY HANDBOOK",
     "https://files.nyit.edu/files/policies/ApplicationForAYVancouverResearchGrant.docx",
     "V2019118\nAPPLICATION FOR AY VANCOUVER RESEARCH GRANT\nComplete all sections of the form.", [],
     "Application For AY Vancouver Research Grant"),
    # a real title is kept: its words are in the file name or on the first lines
    ("Annual Security and Fire Safety Report 2025", "https://files.nyit.edu/files/policies/AnnualSafetyFireReport.pdf",
     "Annual Security and Fire Safety Report\nNew York Institute of Technology", [],
     "Annual Security and Fire Safety Report 2025"),
    ("Spring 22 Enrichment News Letter", "https://files.nyit.edu/files/April_22_EnrichmentNewsletter.pdf",
     "Enrichment Edition\nSpring 2022", [], "Spring 22 Enrichment News Letter"),
])
def test_template_title_metadata_is_not_trusted(meta, url, first_text, anchors, expected):
    assert dc.pick_title(meta, None, anchors, url, first_text)[0] == expected


def test_the_paragraph_after_the_title_does_not_vouch_for_the_metadata():
    url = "https://files.nyit.edu/files/policies/AnnualPersonalReviewAndProfessionalGrowthPlanVancouver.docx"
    text = ("Annual Personal Review and Professional Growth Plan\nProfessional Expectations / Preamble\n"
            "At NYIT-Vancouver, we are committed to excellence in teaching and learning. To maintain this "
            "commitment, we require all faculty members to engage in reflective practice.")
    title = dc.pick_title("NYIT VANCOUVER FACULTY HANDBOOK", "Annual Personal Review and Professional Growth Plan",
                          [], url, text)
    assert title == ("Annual Personal Review and Professional Growth Plan", "printed_title")


def test_title_metadata_is_trusted_without_page_text():
    # a scanned page (no text) or a caller that does not pass it: the metadata is used as before
    url = "https://files.nyit.edu/files/policies/GivingToNYIT_PledgeConfirmationLetter.docx"
    assert dc.pick_title("The Campaign for New York Tech", None, [], url, "")[1] == "metadata"
    assert dc.pick_title("The Campaign for New York Tech", None, [], url)[1] == "metadata"


# ---------------------------------------------------------------------------
# 3. link-text fragments and accessibility notes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("anchor, title", [
    ("NY Tech PA Competencies This link opens in a new window", "NY Tech PA Competencies"),
    ("Mod 2.0 (link)", "Mod 2.0"),
    ("LGBTQ+ Resource Guide (opens in new tab)", "LGBTQ+ Resource Guide"),
    ("Click here for a full page map of Old Westbury Campus This link opens in a new window",
     "A full page map of Old Westbury Campus"),
    ("download one here", None),            # "One here"
    ("t and Capital Form", None),           # the end of "IT Project and Capital Request Form"
    ("A Guide to Housing", "A Guide to Housing"),
])
def test_anchor_title_drops_accessibility_notes_and_fragments(anchor, title):
    assert dc.anchor_title(anchor) == title


def test_a_link_cut_in_the_middle_of_a_word_is_no_title():
    url = "https://files.nyit.edu/files/its/University%20Project%20and%20Capital%20Request%20Form%20FINAL.docx"
    assert dc.pick_title("", None, ["t and Capital Form", "University Projec"], url) == \
        ("University Project and Capital Request Form FINAL", "file_name")
    # a link that only leaves out a plural "s" is fine
    assert dc.pick_title("", None, ["Graduate Program Guide"], "https://files.nyit.edu/files/x/GraduateProgramGuides.pdf")[0] == \
        "Graduate Program Guide"


def test_office_anchor_drops_accessibility_note_and_format():
    assert dc.office_anchor("Budget plan (Excel) This link opens in a new window") == "Budget plan"


# ---------------------------------------------------------------------------
# 4. doc_category
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url, title, category", [
    # forms, templates and surveys in /policies/ are what their names say, not policies
    ("https://files.nyit.edu/files/policies/MissingReceiptAffidavit.xlsx", "Missing Receipt Affidavit", "form"),
    ("https://files.nyit.edu/files/policies/TravelAndBusinessExpensesReportingForm.xlsx",
     "Travel and Expense Reimbursement form", "form"),
    ("https://files.nyit.edu/files/policies/TravelAndBusinessExpensesReportingForm2025.pdf",
     "Date: Transaction Description", "form"),
    ("https://files.nyit.edu/files/policies/NewYorkTechGradFrequencyReport2023.xlsx",
     "2023 Advanced Degree Frequency Report", "report"),
    ("https://files.nyit.edu/files/policies/GivingToNYIT_PledgeCampaignLog.pdf", "Pledge Campaign Log", "form"),
    ("https://files.nyit.edu/files/policies/Pledge_Letter_Template.docx", "Pledge Confirmation Letter", "form"),
    ("https://files.nyit.edu/files/policies/GivingToNYIT_PledgeConfirmationLetter.docx", "Sample Letter", "form"),
    # ... the folder still decides for the rest
    ("https://files.nyit.edu/files/policies/AnnualSafetyFireReport.pdf",
     "Annual Security and Fire Safety Report 2025", "policy_handbook"),
    ("https://files.nyit.edu/files/policies/Policies_ITSDataAccess_DataClassificationMatrix.pdf",
     "Data Classification Matrix", "policy_handbook"),
    # a form is a form whatever it is about
    ("https://files.nyit.edu/files/academic_affairs/AA_CurriculumCommittee_ChangeInDegreeMapRequestForm.docx",
     "Change in Degree Map Request form", "form"),
    # a dual degree is no transfer agreement, "program" alone no brochure
    ("https://files.nyit.edu/files/academic_affairs/AA_CurriculumCommittee_CreatingDualDegreeProgram.docx",
     "Creating a Dual Degree", "other"),
    ("https://files.nyit.edu/files/academic_affairs/AA_CurriculumCommittee_PreliminaryAnalysisForNewProgramProposals.docx",
     "Preliminary Analysis for New Program Proposals", "other"),
    ("https://files.nyit.edu/files/architecture/MS_ACT_ProgramSummary.pdf", "Program Summary", "brochure"),
    # IRS Form 990 stays a financial filing; "Reporting Instructions" is no report
    ("https://aaupatnyit.nyit.edu/wp-content/uploads/NYITAAUP_990_2019.pdf", "Form 990", "financial_filing"),
    ("https://files.nyit.edu/files/x/TravelAndBusinessExpensesReportingInstructions.pdf", "Reporting Instructions",
     "training"),
    ("https://files.nyit.edu/files/x/Blackboard_Login.pdf", "How to log in to Blackboard", "training"),
])
def test_doc_category_review5(url, title, category):
    assert dc.doc_category(url, title) == category


def test_category_ignores_the_title_qualifier():
    docs = [{"pdf_url": "https://www.nyit.edu/wp-content/uploads/2026/02/eportfolios_2.4_Copy_a_Portfolio_202602.pdf",
             "site": "www.nyit.edu", "title": "Faculty e-Portfolios (2026-02, Make a copy of the template.)",
             "title_qualifier": "2026-02, Make a copy of the template."}]
    dc.annotate_documents(docs)
    assert docs[0]["doc_category"] != "form"


def test_qualifier_words_still_count_for_the_other_rules():
    url = "https://files.nyit.edu/files/admissions/BMCC_FAC_NYIT_FAFFI.pdf"
    title = "Borough of Manhattan Community (Transfer Credits and Required Courses for B.S. in Accounting)"
    qualifier = "Transfer Credits and Required Courses for B.S. in Accounting"
    assert dc.doc_category(url, title, qualifier) == "transfer_agreement"
    # ... and a timesheet calendar or a log-in guide is no form
    assert dc.doc_category("https://files.nyit.edu/files/x/StudentTimesheetDeadlinesAndPayDates.pdf",
                           "Student Employment Timesheet Deadlines and Pay Dates") == "schedule_calendar"
    assert dc.doc_category("https://files.nyit.edu/files/x/UMBFirstTimeLogOn.pptx",
                           "UMB Commercial Card How-To Guide: First-Time User Log-In") == "training"
