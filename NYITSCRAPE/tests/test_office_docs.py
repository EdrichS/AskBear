"""office.py and the Office part of documents.py: Word, Excel and PowerPoint files linked from
NYIT pages (the `docs` step), built here with zipfile and served by FakeWeb.

The small builders below write real Office Open XML packages ([Content_Types].xml, the
package and part relationships, docProps/core.xml and the main part), so the reader is tested
on the same structure Office writes, plus crafted files: entity bombs, inflation bombs,
encrypted (OLE2) files, HTML pages served as .docx, Strict-namespace files.
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union
from xml.sax.saxutils import escape

import pytest

from askbear_scraper import cli, office, pipeline, stats, validate
from askbear_scraper import documents as dc

from testkit import FakeWeb, make_fetcher, read_jsonl, write_jsonl

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CORE_TYPE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

STRICT = {W_NS: "http://purl.oclc.org/ooxml/wordprocessingml/main",
          S_NS: "http://purl.oclc.org/ooxml/spreadsheetml/main",
          R_NS: "http://purl.oclc.org/ooxml/officeDocument/relationships",
          REL_TYPE: "http://purl.oclc.org/ooxml/officeDocument/relationships/"}

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

Part = Union[str, bytes]


def rels(*items: Tuple[str, str, str], strict: bool = False) -> str:
    """A .rels part: (id, type, target); ``type`` is the end of the type URI ("styles")."""
    base = STRICT[REL_TYPE] if strict else REL_TYPE
    out = [DECL, '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for rid, rtype, target in items:
        full = rtype if rtype.startswith("http") else base + rtype
        out.append(f'<Relationship Id="{rid}" Type="{full}" Target="{target}"/>')
    return "".join(out) + "</Relationships>"


def content_types(*overrides: Tuple[str, str]) -> str:
    return (DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(f'<Override PartName="/{name}" ContentType="{ct}"/>' for name, ct in overrides)
            + "</Types>")


def core_xml(title: str = "", modified: str = "") -> str:
    return (DECL + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
            'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            + (f"<dc:title>{escape(title)}</dc:title>" if title else "")
            + (f'<dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified>' if modified else "")
            + "</cp:coreProperties>")


def write_package(path: Path, parts: Dict[str, Part], compression: int = zipfile.ZIP_DEFLATED) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def package_parts(main: str, main_xml: Part, main_ct: str, rels_of_main: str = "",
                  extra: Optional[Dict[str, Part]] = None, title: str = "", modified: str = "",
                  strict: bool = False) -> Dict[str, Part]:
    folder, name = main.rsplit("/", 1)
    parts: Dict[str, Part] = {
        "[Content_Types].xml": content_types((main, main_ct)),
        "_rels/.rels": rels(("rId1", "officeDocument", main), ("rId2", CORE_TYPE, "docProps/core.xml"),
                            strict=strict),
        "docProps/core.xml": core_xml(title, modified),
        main: main_xml,
    }
    if rels_of_main:
        parts[f"{folder}/_rels/{name}.rels"] = rels_of_main
    parts.update(extra or {})
    return parts


# -- .docx ------------------------------------------------------------------
def run(text: str, extra: str = "") -> str:
    return f'<w:r>{extra}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def para(text: str = "", style: Optional[str] = None, runs: str = "", props: str = "") -> str:
    ppr = (f'<w:pPr><w:pStyle w:val="{style}"/>{props}</w:pPr>' if style else
           (f"<w:pPr>{props}</w:pPr>" if props else ""))
    return f"<w:p>{ppr}{run(text) if text else ''}{runs}</w:p>"


def table(rows: Sequence[Sequence[str]]) -> str:
    cells = "".join("<w:tr>" + "".join(f"<w:tc>{para(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows)
    return f'<w:tbl><w:tblPr/><w:tblGrid/>{cells}</w:tbl>'


STYLES_XML = (DECL + f'<w:styles xmlns:w="{W_NS}">'
              '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
              '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="MyChapter"><w:name w:val="My Chapter"/>'
              '<w:basedOn w:val="Heading1"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/></w:style>'
              '</w:styles>')


def docx_parts(body: str, title: str = "", modified: str = "2024-03-04T15:22:00Z",
               styles: str = STYLES_XML, extra: Optional[Dict[str, Part]] = None,
               strict: bool = False) -> Dict[str, Part]:
    document = (DECL + f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}" xmlns:mc="{MC_NS}">'
                f'<w:body>{body}<w:sectPr/></w:body></w:document>')
    doc_rels = rels(("rId1", "styles", "styles.xml"), ("rId2", "header", "header1.xml"), strict=strict)
    parts = package_parts("word/document.xml", document, DOCX_CT, doc_rels,
                          {"word/styles.xml": styles,
                           "word/header1.xml": DECL + f'<w:hdr xmlns:w="{W_NS}">{para("RUNNING HEADER")}</w:hdr>',
                           **(extra or {})}, title, modified, strict)
    if strict:
        parts = {k: _strict(v) for k, v in parts.items()}
    return parts


def _strict(xml: Part) -> Part:
    if isinstance(xml, bytes) or xml.startswith(DECL + "<Types"):
        return xml
    for transitional, strict in STRICT.items():
        xml = xml.replace(transitional, strict)
    return xml


def make_docx(path: Path, body: str, **kwargs) -> Path:
    return write_package(path, docx_parts(body, **kwargs))


# -- .xlsx ------------------------------------------------------------------
def cell(ref: str, value: str = "", t: Optional[str] = None, s: Optional[int] = None, formula: str = "",
         inline: str = "") -> str:
    attrs = f' r="{ref}"' + (f' t="{t}"' if t else "") + (f' s="{s}"' if s is not None else "")
    inner = (f"<f>{escape(formula)}</f>" if formula else "") + (f"<v>{escape(value)}</v>" if value else "")
    if inline:
        inner = f"<is>{inline}</is>"
    return f"<c{attrs}>{inner}</c>"


def sheet_xml(rows: Sequence[str]) -> str:
    body = "".join(f'<row r="{i}">{cells}</row>' for i, cells in enumerate(rows, 1))
    return DECL + f'<worksheet xmlns="{S_NS}" xmlns:r="{R_NS}"><sheetData>{body}</sheetData></worksheet>'


XLSX_STYLES = (DECL + f'<styleSheet xmlns="{S_NS}"><numFmts count="3">'
               '<numFmt numFmtId="164" formatCode="mm/dd/yy;@"/><numFmt numFmtId="165" formatCode="0.0%"/>'
               '<numFmt numFmtId="166" formatCode="0.00&quot;%&quot;"/></numFmts>'
               '<cellXfs count="8"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/>'
               '<xf numFmtId="20"/><xf numFmtId="9"/><xf numFmtId="10"/><xf numFmtId="165"/>'
               '<xf numFmtId="166"/></cellXfs></styleSheet>')
PCT0, PCT2, PCT1, PCT_TEXT = 4, 5, 6, 7          # styles: 0%, 0.00%, 0.0%, 0.00"%" (a literal "%")


def xlsx_parts(sheets: Sequence[Tuple[str, Sequence[str], str]], shared: Sequence[str] = (),
               order: Optional[Sequence[int]] = None, title: str = "", date1904: bool = False,
               strict: bool = False) -> Dict[str, Part]:
    """``sheets``: (name, rows (cells XML), state); sheet i is stored as sheet{i+1}.xml and
    listed in the workbook in ``order``."""
    order = list(order) if order is not None else list(range(len(sheets)))
    listed = "".join(f'<sheet name="{escape(sheets[i][0])}" sheetId="{i + 1}" r:id="rId{i + 1}"'
                     + (f' state="{sheets[i][2]}"' if sheets[i][2] != "visible" else "") + "/>"
                     for i in order)
    workbook = (DECL + f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}">'
                + ('<workbookPr date1904="1"/>' if date1904 else "")
                + f"<sheets>{listed}</sheets></workbook>")
    sst = (DECL + f'<sst xmlns="{S_NS}" count="{len(shared)}">'
           + "".join(s if s.startswith("<si>") else f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    wb_rels = rels(*[(f"rId{i + 1}", "worksheet", f"worksheets/sheet{i + 1}.xml") for i in range(len(sheets))],
                   ("rIdS", "sharedStrings", "sharedStrings.xml"), ("rIdT", "styles", "styles.xml"),
                   strict=strict)
    extra: Dict[str, Part] = {f"xl/worksheets/sheet{i + 1}.xml": sheet_xml(rows)
                              for i, (_name, rows, _state) in enumerate(sheets)}
    extra.update({"xl/sharedStrings.xml": sst, "xl/styles.xml": XLSX_STYLES})
    parts = package_parts("xl/workbook.xml", workbook, XLSX_CT, wb_rels, extra, title, strict=strict)
    if strict:
        parts = {k: _strict(v) for k, v in parts.items()}
    return parts


def make_xlsx(path: Path, sheets, **kwargs) -> Path:
    return write_package(path, xlsx_parts(sheets, **kwargs))


# -- .pptx ------------------------------------------------------------------
def xfrm(pos: Optional[Tuple[int, int]], tag: str = "a:xfrm") -> str:
    """A transform at (top, left) in EMU (none when ``pos`` is None)."""
    return f'<{tag}><a:off x="{pos[1]}" y="{pos[0]}"/><a:ext cx="100" cy="100"/></{tag}>' if pos else ""


def shape(text_paras: Sequence[str], ph: Optional[str] = None, pos: Optional[Tuple[int, int]] = None) -> str:
    ph_xml = f'<p:ph type="{ph}"/>' if ph else ""
    paras = "".join(f"<a:p><a:r><a:t>{escape(t)}</a:t></a:r></a:p>" for t in text_paras)
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr>{ph_xml}</p:nvPr></p:nvSpPr>'
            f"<p:spPr>{xfrm(pos)}</p:spPr><p:txBody><a:bodyPr/>{paras}</p:txBody></p:sp>")


def slide_xml(*shapes: str) -> str:
    return (DECL + f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:mc="{MC_NS}">'
            f'<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>{"".join(shapes)}</p:spTree></p:cSld></p:sld>')


def pptx_table(rows: Sequence[Sequence[str]]) -> str:
    trs = "".join("<a:tr>" + "".join(f"<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{escape(c)}</a:t></a:r></a:p>"
                                     f"</a:txBody></a:tc>" for c in row) + "</a:tr>" for row in rows)
    return ('<p:graphicFrame><p:nvGraphicFramePr/><a:graphic><a:graphicData uri="table">'
            f"<a:tbl>{trs}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")


def pptx_parts(slides: Sequence[str], order: Sequence[int], title: str = "") -> Dict[str, Part]:
    """slide i is stored as slide{i+1}.xml; the presentation lists them in ``order``."""
    ids = "".join(f'<p:sldId id="{256 + n}" r:id="rId{i + 10}"/>' for n, i in enumerate(order))
    presentation = (DECL + f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
                    f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>")
    pres_rels = rels(*[(f"rId{i + 10}", "slide", f"slides/slide{i + 1}.xml") for i in range(len(slides))],
                     ("rId1", "slideMaster", "slideMasters/slideMaster1.xml"))
    extra = {f"ppt/slides/slide{i + 1}.xml": xml for i, xml in enumerate(slides)}
    return package_parts("ppt/presentation.xml", presentation, PPTX_CT, pres_rels, extra, title)


def make_pptx(path: Path, slides, order, **kwargs) -> Path:
    return write_package(path, pptx_parts(slides, order, **kwargs))


def ole2_bytes(encrypted: bool = True) -> bytes:
    """The start of an OLE2 compound file; an encrypted OOXML file has an EncryptedPackage
    stream (its directory entry names are UTF-16)."""
    head = office.OLE2_MAGIC + b"\x00" * 504
    names = ("EncryptionInfo", "EncryptedPackage") if encrypted else ("WordDocument", "1Table")
    return head + b"".join(n.encode("utf-16-le").ljust(64, b"\x00") for n in names) + b"\x00" * 1024


def lying_zip(path: Path, name: str, payload: bytes, declared: int) -> Path:
    """A ZIP whose member ``name`` inflates to ``payload`` but declares ``declared`` bytes
    (an inflation bomb that lies about its size)."""
    buf = Path(path)
    write_package(buf, {"[Content_Types].xml": content_types(), name: payload})
    data = bytearray(buf.read_bytes())
    # patch the uncompressed size in the local header and in the central directory
    target = name.encode()
    pos = data.find(b"PK\x03\x04" + b"")
    while pos != -1:
        n_len = struct.unpack("<H", data[pos + 26:pos + 28])[0]
        if bytes(data[pos + 30:pos + 30 + n_len]) == target:
            data[pos + 22:pos + 26] = struct.pack("<I", declared)
        pos = data.find(b"PK\x03\x04", pos + 4)
    pos = data.find(b"PK\x01\x02")
    while pos != -1:
        n_len = struct.unpack("<H", data[pos + 28:pos + 30])[0]
        if bytes(data[pos + 46:pos + 46 + n_len]) == target:
            data[pos + 24:pos + 28] = struct.pack("<I", declared)
        pos = data.find(b"PK\x01\x02", pos + 4)
    buf.write_bytes(bytes(data))
    return buf


def words(n: int, word: str = "word") -> str:
    return " ".join(f"{word}{i}" for i in range(n))


# ---------------------------------------------------------------------------
# .docx
# ---------------------------------------------------------------------------
def test_docx_reading_order_headings_tables_and_tracked_changes(tmp_path):
    body = "".join([
        para("Graduate Assistantship Guide", "Title"),
        para("Table of Contents ........ 1", "TOC1"),                          # TOC lines are left out
        para("Eligibility", "Heading1"),
        para(runs=run("Students must be ") + "<w:del><w:r><w:delText>part-time </w:delText></w:r></w:del>"
             + "<w:ins><w:r><w:t>full-time </w:t></w:r></w:ins>" + run("graduate students.")),
        para(runs='<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "x" </w:instrText>'
             '</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>' + run("Apply online")
             + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
        para("Stipends", "Heading2"),
        table([["Program", "Stipend"], ["M.S. Computer Science", "$10,000"], ["", ""]]),
        para(runs=run("Visible") + run(" hidden words", "<w:rPr><w:vanish/></w:rPr>") + run(" text.")),
        para(runs=f'<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
             f'{para("Text box note")}</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict>'
             f'<w:txbxContent>{para("Text box note")}</w:txbxContent></w:pict></mc:Fallback>'
             f'</mc:AlternateContent></w:r>'),
        para("Deadlines", "MyChapter"),                                        # a heading by its basedOn
        para("Outline level paragraph", props='<w:outlineLvl w:val="1"/>'),
        f'<w:sdt><w:sdtPr/><w:sdtContent>{para("Inside a content control.")}</w:sdtContent></w:sdt>',
        '<w:p><w:r><w:t>Before</w:t><w:br/><w:t>after</w:t><w:tab/><w:t>tabbed</w:t>'
        '<w:sym w:font="Symbol" w:char="F0B7"/></w:r></w:p>',
    ])
    path = make_docx(tmp_path / "guide.docx", body, title="Microsoft Word - guide.docx")
    texts, paths, info = office.extract_office(path, "docx")
    assert len(texts) == 1
    lines = texts[0].split("\n")
    assert lines == ["Graduate Assistantship Guide", "Eligibility",
                     "Students must be full-time graduate students.", "Apply online", "Stipends",
                     "Program | Stipend", "M.S. Computer Science | $10,000", "Visible text.", "Text box note",
                     "Deadlines", "Outline level paragraph", "Inside a content control.", "Before",
                     "after tabbed\uf0b7"]
    assert "RUNNING HEADER" not in texts[0] and "HYPERLINK" not in texts[0] and "part-time" not in texts[0]
    assert paths == [[]]                       # the page starts before the first heading
    assert info["printed_title"] == "Graduate Assistantship Guide" and info["toc_entries"] == 4
    assert info["metadata_title"] == "Microsoft Word - guide.docx"
    assert info["modified"] == "2024-03-04T15:22:00+00:00"
    # cleaned like PDF text: the Symbol-font bullet becomes a real bullet
    texts, _paths, info = dc.extract_office_document(path, "docx")
    assert texts[0].endswith("after tabbed\N{BULLET}")
    assert info["printed_title"] == "Graduate Assistantship Guide" and not info["scanned"]
    assert dc.pick_title(info["metadata_title"], info["printed_title"], ["here"], "https://x.nyit.edu/g.docx") == \
        ("Graduate Assistantship Guide", "printed_title")


def test_docx_pages_break_at_headings_and_stay_under_400_words(tmp_path):
    body = "".join([para("Handbook", "Heading1"), para(words(120)), para("Rules", "Heading2"),
                    para(words(100, "rule")), para(words(250, "more")), para("Conduct", "Heading1"),
                    para(words(900, "long") + ". "), para("Appeals", "Heading2"), para(words(10, "end"))])
    texts, paths, info = office.extract_office(make_docx(tmp_path / "h.docx", body), "docx")
    counts = [office.word_count(t) for t in texts]
    assert all(n <= 400 + 1 for n in counts), counts     # (+ a heading kept with its text)
    assert texts[0].startswith("Handbook\n") and paths[0] == ["Handbook"]
    # "Rules" did not start a page (the first one had < 150 words); the 250 words did not fit
    assert "Rules" in texts[0] and paths[1] == ["Handbook", "Rules"]
    conduct = next(i for i, t in enumerate(texts) if t.startswith("Conduct"))
    assert paths[conduct] == ["Conduct"]
    # the 900-word paragraph is cut in pieces; the short last section joins the page before
    assert texts[-1].endswith("end9") and paths[-1][0] == "Conduct"
    assert sum(counts) == info.get("words", sum(counts)) and len(texts) >= 5


def test_pack_blocks_moves_trailing_headings_and_never_makes_a_heading_only_page():
    blocks = [office.Block(words(390), ("A",)), office.Block("Next section", ("A", "B"), True),
              office.Block(words(50, "b"), ("A", "B"))]
    pages = office.pack_blocks(blocks, 150, 400)
    assert [p for _t, p in pages] == [["A"], ["A", "B"]]
    assert pages[1][0].startswith("Next section\n")
    pages = office.pack_blocks([office.Block("Lonely heading", ("H",), True), office.Block(words(450), ("H",))])
    assert all(t.split("\n")[0] != t for t, _p in pages[:1])        # the heading stays with its text
    assert pages[0][0].startswith("Lonely heading\n")


@pytest.mark.parametrize("full_pages", [1, 2, 3])
def test_pack_blocks_short_last_page_joins_the_one_before_and_loses_nothing(full_pages):
    blocks = [office.Block(words(390, f"p{i}"), (f"S{i}",), False) for i in range(full_pages)]
    blocks.append(office.Block(words(20, "tail"), ("T",), False))
    pages = office.pack_blocks(blocks, 150, 400)
    assert len(pages) == full_pages
    text = "\n".join(t for t, _p in pages)
    for i in range(full_pages):            # every page once, in order, nothing duplicated
        assert text.count(f"p{i}0 ") == 1 and pages[i][1] == [f"S{i}"]
    assert pages[-1][0].endswith("tail19")


def test_docx_in_strict_namespaces(tmp_path):
    path = write_package(tmp_path / "strict.docx",
                         docx_parts(para("Strict Heading", "Heading1") + para("Strict body text."), strict=True))
    texts, paths, info = office.extract_office(path, "docx")
    assert texts == ["Strict Heading\nStrict body text."] and paths == [["Strict Heading"]]
    assert office.check_file(path, "docx") is None


def test_nested_tables_and_merged_cells(tmp_path):
    nested = table([["inner a", "inner b"]])
    body = ("<w:tbl><w:tr><w:tc>" + para("Outer") + "</w:tc><w:tc>" + nested + "</w:tc></w:tr>"
            '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>' + para("Merged") + "</w:tc>"
            "<w:tc>" + para("x") + '</w:tc></w:tr><w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
            "<w:tc>" + para("y") + "</w:tc></w:tr>"
            '<w:tr><w:trPr><w:del w:id="1"/></w:trPr><w:tc>' + para("deleted row") + "</w:tc></w:tr></w:tbl>")
    texts, _paths, _info = office.extract_office(make_docx(tmp_path / "t.docx", body), "docx")
    assert texts[0] == "Outer | inner a inner b\nMerged | x\ny"


def test_docx_content_control_placeholders_are_left_out(tmp_path):
    def sdt(content: str, placeholder: bool = True) -> str:
        flag = "<w:showingPlcHdr/>" if placeholder else ""
        return f'<w:sdt><w:sdtPr><w:id w:val="1"/>{flag}<w:text/></w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>'

    gray = '<w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr>'
    body = "".join([
        para("To Whom It May Concern:"),
        sdt(para("Click or tap here to enter text.")),                            # a block content control
        para(runs=run("Start date: ") + sdt(run("Click or tap to enter a date."))),   # an inline one
        para(runs=run("Signatory: ") + run("Click or tap here to enter text.", gray)),  # its gray style only
        "<w:tbl><w:tr>" + sdt("<w:tc>" + para("Click or tap here to enter text.") + "</w:tc>")
        + "<w:tc>" + para("has been offered on-campus employment.") + "</w:tc></w:tr></w:tbl>",  # a cell
        sdt(para("Jane Doe"), placeholder=False),                                 # filled in: kept
        para(runs=run("Rate: ") + sdt(run("$20"), placeholder=False)),
    ])
    texts, _paths, _info = office.extract_office(make_docx(tmp_path / "letter.docx", body), "docx")
    assert texts == ["To Whom It May Concern:\nStart date:\nSignatory:\nhas been offered on-campus employment.\n"
                     "Jane Doe\nRate: $20"]


# ---------------------------------------------------------------------------
# .xlsx
# ---------------------------------------------------------------------------
def test_xlsx_cells_shared_strings_hidden_sheets_and_order(tmp_path):
    shared = ["Course", "Room",
              "<si><r><t>Rich </t></r><r><rPr><b/></rPr><t>text</t></r><rPh><t>PHONETIC</t></rPh></si>",
              "CSCI 125"]
    schedule = [cell("A1", "0", "s") + cell("B1", "1", "s") + cell("C1", "Starts", "str"),
                cell("A2", "3", "s") + cell("B2", "3.0") + cell("C2", "45910", s=1),
                "",                                                                  # an empty row
                cell("C4", "1", "b") + cell("A4", "2", "s") + cell("B4", "0.30000000000000004"),
                cell("A5", "#N/A", "e") + cell("B5", "", formula="SUM(A1:A2)") + cell("C5", "7", formula="3+4"),
                cell("A6", inline="<t>inline</t>", t="inlineStr") + cell("B6", "45910.75", s=2)
                + cell("C6", "0.5", s=3)]
    path = make_xlsx(tmp_path / "s.xlsx", [("Hidden data", [cell("A1", "secret value", "str")], "hidden"),
                                           ("Schedule", schedule, "visible"),
                                           ("Notes", [cell("A1", "Second sheet", "str")], "visible")],
                     shared=shared, order=[2, 1, 0])
    texts, paths, info = office.extract_office(path, "xlsx")
    assert paths == [["Notes"], ["Schedule"]]                  # workbook order, hidden sheet left out
    assert texts[0] == "Second sheet"
    # (three column headings over the rows: each value is written with its heading)
    assert texts[1].split("\n") == ["Course: CSCI 125 | Room: 3 | Starts: 2025-09-10",
                                    "Course: Rich text | Room: 0.3 | Starts: TRUE", "Starts: 7",
                                    "Course: inline | Room: 2025-09-10 | Starts: 12:00"]
    assert "secret" not in " ".join(texts) and "PHONETIC" not in texts[1]
    assert info["hidden_sheets"] == 1 and info["sheets"] == 2


def test_xlsx_large_sheet_is_cut_into_pages_with_the_column_headings(tmp_path):
    rows = [cell("A1", "Course list", "str"),
            cell("A2", "Code", "str") + cell("B2", "Title", "str") + cell("C2", "Credits", "str")]
    rows += [cell(f"A{i}", f"CSCI {i}", "str") + cell(f"B{i}", words(12, f"t{i}x"), "str") + cell(f"C{i}", "3")
             for i in range(3, 140)]
    path = make_xlsx(tmp_path / "big.xlsx", [("Courses", rows, "visible")])
    texts, paths, _info = office.extract_office(path, "xlsx")
    assert len(texts) > 3 and all(p == ["Courses"] for p in paths)
    assert all(office.word_count(t) <= 400 + 5 for t in texts)
    assert texts[0].startswith("Course list\nCode: CSCI 3 | Title: t3x0 t3x1 ")
    assert "Code | Title" not in "".join(texts)                     # the headings go with every value
    # every later page starts with the last one-cell line (the sheet's title here)
    assert all(t.startswith("Course list\nCode: CSCI ") and t.count("Course list") == 1 for t in texts[1:])
    assert sum(t.count("Credits: 3") for t in texts) == 137


def test_xlsx_date_formats_and_numbers():
    assert office.date_format_kind("mm/dd/yy;@") == "date"
    assert office.date_format_kind("m/d/yy h:mm") == "datetime"
    assert office.date_format_kind('h:mm AM/PM') == "time"
    assert office.date_format_kind("[h]:mm") is None                 # a duration
    assert office.date_format_kind('#,##0.00 "days"') is None
    assert office.date_format_kind("[Red]0.00;[Blue]-0.00") is None
    assert office.date_format_kind("General") is None and office.date_format_kind("@") is None
    assert office.excel_date("45910", "date") == "2025-09-10"
    assert office.excel_date("45910.5", "datetime") == "2025-09-10 12:00"
    assert office.excel_date("45910.5", "date") == "2025-09-10"      # the format shows no time
    assert office.excel_date("1", "date") == "1900-01-01" and office.excel_date("61", "date") == "1900-03-01"
    assert office.excel_date("0", "date", date1904=True) == "00:00"
    assert office.excel_date("1", "date", date1904=True) == "1904-01-02"
    assert office.excel_date("-5", "date") == "-5" and office.excel_date("abc", "date") == "abc"
    assert office.number_text("3.0") == "3" and office.number_text("1E-3") == "0.001"
    assert office.number_text("12345678901234567890") == "1.23456789012346e+19"


def test_empty_workbook_gives_one_empty_page(tmp_path):
    texts, paths, _info = office.extract_office(make_xlsx(tmp_path / "e.xlsx", [("Sheet1", [], "visible")]), "xlsx")
    assert texts == [""] and paths == [[]]


def test_xlsx_strict_namespaces(tmp_path):
    path = write_package(tmp_path / "strict.xlsx",
                         xlsx_parts([("Data", [cell("A1", "0", "s") + cell("B1", "42")], "visible")],
                                    shared=["Answer"], strict=True))
    assert office.extract_office(path, "xlsx")[0] == ["Answer | 42"]


def test_xlsx_empty_cells_keep_every_value_with_its_column(tmp_path):
    # a class schedule: some cells of a row are empty (no Section, no Instructor)
    heads = ["Subject", "Catalog", "Section", "Descr", "Capacity", "Instructor"]
    rows = ["".join(cell(f"{col}1", h, "str") for col, h in zip("ABCDEF", heads)),
            cell("A2", "ACCT", "str") + cell("B2", "102", "str") + cell("D2", "Accounting II", "str")
            + cell("E2", "23") + cell("F2", "Smith,Ann", "str"),
            cell("A3", "BUSI", "str") + cell("C3", "F01", "str") + cell("E3", "30")]
    path = make_xlsx(tmp_path / "s.xlsx", [("Sheet1", rows, "visible")])
    texts, paths, _info = office.extract_office(path, "xlsx")
    assert texts == ["Subject: ACCT | Catalog: 102 | Descr: Accounting II | Capacity: 23 | Instructor: Smith,Ann\n"
                     "Subject: BUSI | Section: F01 | Capacity: 30"]
    assert paths == [[]]                                   # "Sheet1" says nothing: no context line


@pytest.mark.parametrize("name, kept", [("Sheet1", False), ("Sheet 12", False), ("Tabelle2", False),
                                        ("Sheet", False), ("Fall 2025", True), ("Sheet music", True)])
def test_xlsx_default_sheet_names_are_no_context(tmp_path, name, kept):
    path = make_xlsx(tmp_path / "n.xlsx", [(name, [cell("A1", "Some value", "str")], "visible")])
    assert office.extract_office(path, "xlsx")[1] == ([[name]] if kept else [[]])


def survey_rows(questions: int = 6):
    """A survey frequency report: a title row, group labels over "n | %" headings, a
    question line, then one statement per merged cell over five answer rows."""
    rows = [cell("A1", "HEDS Campus Climate Survey 2023 Undergraduate Frequency Report New York Tech", "str")
            + cell("U1", "Back to Table of Contents", "str"),
            cell("A2", "Campus Climate", "str"),
            cell("C4", "New York Tech", "str") + cell("G4", "Other Institutions", "str"),
            cell("C5", "Women", "str") + cell("E5", "Men", "str") + cell("G5", "Women", "str") + cell("I5", "Men", "str"),
            "".join(cell(f"{col}6", h, "str") for col, h in zip("CDEFGHIJ", ["n", "%"] * 4)),
            cell("A7", "Q1. Please indicate the extent to which you agree or disagree with each.", "str")]
    r = 8
    for q in range(questions):
        for a, answer in enumerate(["Strongly agree", "Agree", "Neutral", "Disagree", "Total"]):
            row = cell(f"A{r}", f"Statement number {q} about the campus.", "str") if a == 0 else ""
            row += cell(f"B{r}", answer, "str") + cell(f"C{r}", "25") + cell(f"D{r}", "0.333", s=PCT0)
            row += (cell(f"F{r}", "0.46899999999999997", s=PCT0) if a != 1 else            # E empty
                    cell(f"E{r}", "30") + cell(f"F{r}", "0.5", s=PCT0))
            row += "".join(cell(f"{col}{r}", v, s=st) for col, v, st in
                           [("G", "1388", None), ("H", "0.298", PCT0), ("I", "641", None), ("J", "0.379", PCT0)])
            rows.append(row)
            r += 1
    return rows


def test_xlsx_headings_are_the_real_column_headings_and_context_carries_over(tmp_path):
    path = make_xlsx(tmp_path / "heds.xlsx", [("1. Campus Climate", survey_rows(12), "visible")])
    texts, paths, _info = office.extract_office(path, "xlsx")
    assert len(texts) > 2 and all(p == ["1. Campus Climate"] for p in paths)
    assert all(office.word_count(t) <= 400 for t in texts)
    first = texts[0].split("\n")
    assert first[:4] == ["HEDS Campus Climate Survey 2023 Undergraduate Frequency Report New York Tech | "
                         "Back to Table of Contents", "Campus Climate",
                         "Q1. Please indicate the extent to which you agree or disagree with each.",
                         "Statement number 0 about the campus. | Strongly agree | New York Tech, Women, n: 25 | %: 33% "
                         "| Men, %: 47% | Other Institutions, Women, n: 1388 | %: 30% | Men, n: 641 | %: 38%"]
    assert first[4] == ("Agree | New York Tech, Women, n: 25 | %: 33% | Men, n: 30 | %: 50% | Other Institutions, "
                        "Women, n: 1388 | %: 30% | Men, n: 641 | %: 38%")
    # the title row is no heading: it is not repeated; the heading rows are not printed at all
    assert sum("Back to Table of Contents" in t for t in texts) == 1
    assert not any(line.startswith(("n | %", "Women | Men")) for t in texts for line in t.split("\n"))
    for text in texts[1:]:           # later pages: the question line, then the statement of the group
        lines = text.split("\n")
        assert lines[0].startswith("Q1. Please indicate")
        if not lines[1].startswith("Statement number"):
            raise AssertionError(lines[:3])
        assert "0.333" not in text and "| %: 33%" in text


def test_xlsx_percent_formats():
    assert [office.percent_decimals(f) for f in ("0%", "0.00%", "0.0%;[Red]-0.0%", '0.00"%"', "0.0\\%", "General")] \
        == [0, 2, 1, None, None, None]
    assert office.percent_text("0.333", 0) == "33%" and office.percent_text("0.333", 2) == "33.30%"
    assert office.percent_text("0.125", 0) == "13%"                    # rounded half up, as Excel does
    assert office.percent_text("9.2999999999999999E-2", 1) == "9.3%" and office.percent_text("1", 0) == "100%"
    assert office.percent_text("-0.001", 0) == "0%" and office.percent_text("n/a", 0) == "n/a"


def test_xlsx_percent_cells_show_as_percentages(tmp_path):
    row = (cell("A1", "0.333", s=PCT0) + cell("B1", "0.333", s=PCT2) + cell("C1", "0.333", s=PCT1)
           + cell("D1", "0.333", s=PCT_TEXT) + cell("E1", "0.333"))
    path = make_xlsx(tmp_path / "p.xlsx", [("Rates", [row], "visible")])
    assert office.extract_office(path, "xlsx")[0] == ["33% | 33.30% | 33.3% | 0.333 | 0.333"]


# ---------------------------------------------------------------------------
# .pptx
# ---------------------------------------------------------------------------
def test_pptx_slides_in_presentation_order_with_titles_groups_and_tables(tmp_path):
    first = slide_xml(shape(["UMB First-Time Log-In"], "ctrTitle"), shape(["How-to guide for new users"], "subTitle"))
    second = slide_xml(shape(["Step 1: Open the portal"], "title"),
                       "<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/>" + shape(["Grouped text"]) + "</p:grpSp>",
                       pptx_table([["Field", "Value"], ["User", "NYIT e-mail"]]),
                       shape(["7"], "sldNum"), shape(["Confidential footer"], "ftr"))
    # slide1.xml holds the SECOND slide: the order comes from presentation.xml(.rels)
    path = make_pptx(tmp_path / "umb.pptx", [second, first], order=[1, 0], title="PowerPoint Presentation")
    texts, paths, info = office.extract_office(path, "pptx")
    assert paths == [["UMB First-Time Log-In"], ["Step 1: Open the portal"]]
    assert texts[0] == "UMB First-Time Log-In\nHow-to guide for new users"
    assert texts[1] == "Step 1: Open the portal\nGrouped text\nField | Value\nUser | NYIT e-mail"
    assert info["printed_title"] == "UMB First-Time Log-In" and info["slides"] == 2
    assert dc.metadata_title(info["metadata_title"], "https://files.nyit.edu/files/x/UMB.pptx") is None


def test_pptx_runs_without_text_fields_and_line_breaks(tmp_path):
    body = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:txBody><a:p>'
            '<a:r><a:rPr/></a:r><a:r><a:t>Line one</a:t></a:r><a:br/><a:fld type="datetime"><a:t>May 2024</a:t>'
            '</a:fld></a:p></p:txBody></p:sp>')
    path = make_pptx(tmp_path / "r.pptx", [slide_xml(body)], [0])
    assert office.extract_office(path, "pptx")[0] == ["Line one\nMay 2024"]


def test_pptx_shapes_without_title_are_read_top_to_bottom(tmp_path):
    group = ('<p:grpSp><p:nvGrpSpPr/><p:grpSpPr>' + xfrm((2000, 0)) + "</p:grpSpPr>"
             + shape(["Group lower"], pos=(900, 0)) + shape(["Group upper"], pos=(100, 0)) + "</p:grpSp>")
    table = pptx_table([["Field", "Value"]]).replace("<p:nvGraphicFramePr/>",
                                                      "<p:nvGraphicFramePr/>" + xfrm((5000, 0), "p:xfrm"))
    slide = slide_xml(shape(["Body text at the bottom"], pos=(4000, 0)), table, group,
                      shape(["Right column"], pos=(1000, 5000)), shape(["Left column"], pos=(1000, 100)),
                      shape(["Remote Work Arrangements"], pos=(300, 0)),
                      shape(["Placeholder in its layout place"], "body"))
    texts, paths, _info = office.extract_office(make_pptx(tmp_path / "o.pptx", [slide], [0]), "pptx")
    assert texts[0].split("\n") == ["Remote Work Arrangements", "Left column", "Right column", "Group upper",
                                    "Group lower", "Body text at the bottom", "Field | Value",
                                    "Placeholder in its layout place"]
    assert paths == [[]]
    # a title placeholder still comes first, wherever it is
    titled = slide_xml(shape(["Text"], pos=(100, 0)), shape(["The title"], "title", pos=(3000, 0)))
    assert office.extract_office(make_pptx(tmp_path / "t.pptx", [titled], [0]), "pptx")[0] == ["The title\nText"]


def test_docx_without_styles_part_still_finds_heading_style_ids(tmp_path):
    parts = docx_parts(para("Overview of the Program", "Heading2") + para("Some text."))
    del parts["word/styles.xml"]
    texts, paths, _info = office.extract_office(write_package(tmp_path / "n.docx", parts), "docx")
    assert paths == [["Overview of the Program"]]


def test_pptx_slide_without_text_is_an_empty_page(tmp_path):
    path = make_pptx(tmp_path / "p.pptx", [slide_xml(), slide_xml(shape(["Only slide text"], "title"))], [0, 1])
    texts, paths, _info = dc.extract_office_document(path, "pptx")
    assert texts == ["", "Only slide text"] and paths == [[], ["Only slide text"]]


# ---------------------------------------------------------------------------
# hostile and broken files
# ---------------------------------------------------------------------------
BILLION_LAUGHS = (DECL + '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;">'
                  '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
                  f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&lol3;</w:t></w:r></w:p></w:body></w:document>')


@pytest.mark.parametrize("part, xml", [
    ("word/document.xml", BILLION_LAUGHS),
    ("word/document.xml", BILLION_LAUGHS.encode("utf-16")),                     # the same in UTF-16
    ("word/styles.xml", DECL + '<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]><w:styles '
                        f'xmlns:w="{W_NS}">&ext;</w:styles>'),
    ("docProps/core.xml", DECL + '<!DOCTYPE x [<!ENTITY a "b">]><x/>'),
    ("word/document.xml", '<?xml version="1.0" encoding="cp037"?><x/>'),          # EBCDIC hides markup
])
def test_doctype_and_entity_declarations_are_refused(tmp_path, part, xml):
    parts = docx_parts(para("Fine text."))
    parts[part] = xml
    path = write_package(tmp_path / "bomb.docx", parts)
    with pytest.raises(office.OfficeError):
        office.extract_office(path, "docx")


def test_parts_over_the_budget_and_lying_sizes_are_refused(tmp_path):
    big = docx_parts(para(words(20000)))
    path = write_package(tmp_path / "big.docx", big)
    with pytest.raises(office.OfficeError, match="over the limit"):
        office.extract_office(path, "docx", budget=50_000)
    assert office.extract_office(path, "docx")[2]["bytes_read"] > 50_000          # fine with the real budget
    # 20 MB of zeros declared as 100 bytes: refused after 101 bytes, never inflated in memory
    bomb = lying_zip(tmp_path / "lie.xlsx", "xl/workbook.xml", b"\x00" * 20_000_000, declared=100)
    with office.Package(bomb) as package:
        with pytest.raises(office.OfficeError, match="inflates to more"):
            package.read("xl/workbook.xml")


def test_too_many_members_and_damaged_packages(tmp_path):
    parts = docx_parts(para("x"))
    parts.update({f"customXml/item{i}.xml": "<x/>" for i in range(30)})
    path = write_package(tmp_path / "many.docx", parts)
    with pytest.raises(office.OfficeError, match="ZIP members"):
        office.Package(path, max_parts=20)
    good = make_docx(tmp_path / "good.docx", para("Some text here."))
    cut = tmp_path / "cut.docx"
    cut.write_bytes(good.read_bytes()[:-200])                     # the central directory is gone
    assert office.check_file(cut, "docx")[0] == office.NOT_OFFICE
    with pytest.raises(office.OfficeError):
        office.extract_office(cut, "docx")
    # a member whose bytes were damaged (CRC mismatch)
    data = bytearray(write_package(tmp_path / "crc.docx", docx_parts(para("Some text here.")),
                                   zipfile.ZIP_STORED).read_bytes())
    pos = data.find(b"Some text here.")
    data[pos] = ord("X")
    (tmp_path / "crc.docx").write_bytes(bytes(data))
    with pytest.raises(office.OfficeError, match="CRC"):
        office.extract_office(tmp_path / "crc.docx", "docx")
    # missing main part / malformed body
    parts = docx_parts(para("x"))
    parts["word/document.xml"] = DECL + f'<w:document xmlns:w="{W_NS}"><w:body><w:p>'
    with pytest.raises(office.OfficeError, match="malformed"):
        office.extract_office(write_package(tmp_path / "bad.docx", parts), "docx")


def test_check_file_tells_what_a_download_is(tmp_path):
    docx = make_docx(tmp_path / "a.docx", para("Hello there."))
    xlsx = make_xlsx(tmp_path / "a.xlsx", [("S", [cell("A1", "x", "str")], "visible")])
    html = tmp_path / "login.docx"
    html.write_text("<!DOCTYPE html><html><title>Sign in</title></html>", encoding="utf-8")
    (tmp_path / "enc.docx").write_bytes(ole2_bytes(encrypted=True))
    (tmp_path / "old.docx").write_bytes(ole2_bytes(encrypted=False))
    plain_zip = write_package(tmp_path / "z.docx", {"readme.txt": "not office"})
    assert office.check_file(docx, "docx") is None and office.check_file(xlsx, "xlsx") is None
    assert office.check_file(html, "docx") == (office.NOT_OFFICE, "not a Word file")
    assert office.check_file(tmp_path / "enc.docx", "docx")[0] == office.ENCRYPTED
    assert "legacy binary" in office.check_file(tmp_path / "old.docx", "docx")[1]
    assert "Excel file (.xlsx), not a .docx" in office.check_file(xlsx, "docx")[1]
    assert office.check_file(plain_zip, "docx")[0] == office.NOT_OFFICE
    assert office.check_file(tmp_path / "missing.docx", "docx")[0] == office.NOT_OFFICE
    assert office.looks_like_office(docx) and not office.looks_like_office(html)


def test_relationship_targets_and_xml_decoding():
    assert office.resolve_target("xl", "worksheets/sheet1.xml") == "xl/worksheets/sheet1.xml"
    assert office.resolve_target("xl", "/xl/worksheets/sheet1.xml") == "xl/worksheets/sheet1.xml"
    assert office.resolve_target("ppt", "../docProps/x.xml") == "docProps/x.xml"
    assert office.resolve_target("ppt", "../../etc/passwd") is None and office.resolve_target("", "") is None
    assert office.decode_xml(b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?><a>\xc3\xa9</a>') == "<a>é</a>"
    assert office.decode_xml('<?xml version="1.0" encoding="UTF-16"?><a>x</a>'.encode("utf-16")) == "<a>x</a>"
    assert office.decode_xml(b'<?xml version="1.0" encoding="windows-1252"?><a>\x93q\x94</a>') == "<a>“q”</a>"
    with pytest.raises(office.OfficeError):
        office.decode_xml(b"<a>\xff\xfe broken \xc3</a>")


@pytest.mark.parametrize("anchor, clean", [
    ("UMB How-To Reconcile How-To Guide (Powerpoint download)", "UMB How-To Reconcile How-To Guide"),
    ("Missing Receipt Affidavit (Word document download)", "Missing Receipt Affidavit"),
    ("Five-year budget plan [XLSX, 30 KB]", "Five-year budget plan"), ("Coversheet - docx", "Coversheet"),
    ("Word", "Word"), ("Proposal for Minor", "Proposal for Minor"),
])
def test_office_link_text_loses_its_format_note(anchor, clean):
    assert dc.office_anchor(anchor) == clean


@pytest.mark.parametrize("heading, kept", [
    ("Course Title", False), ("[Insert Program Name]", False), ("Click here to enter text.", False),
    ("Presentation Title Here", False), ("Type of Leave Request", True), ("Enter Grades in Banner", True),
    ("Course Modification Form", True),
])
def test_template_placeholders_are_no_title(tmp_path, heading, kept):
    path = make_docx(tmp_path / "t.docx", para(heading, "Heading1") + para("Body text of the form."))
    assert (dc.extract_office_document(path, "docx")[2]["printed_title"] == heading) is kept


def test_w3c_dates():
    assert office.w3c_date("2021-03-04T15:22:00Z") == "2021-03-04T15:22:00+00:00"
    assert office.w3c_date("2021-03-04T15:22:00.123-05:00") == "2021-03-04T15:22:00-05:00"
    assert office.w3c_date("2021-03-04") == "2021-03-04"
    assert office.w3c_date("1601-01-01T00:00:00Z") is None and office.w3c_date("soon") is None


# ---------------------------------------------------------------------------
# the `docs` step with Office files (FakeWeb)
# ---------------------------------------------------------------------------
FILES = "https://files.nyit.edu/files/academic_affairs/"
GUIDE = FILES + "AA_Program_Proposal_Guide_2025.docx"
GUIDE_COPY = "https://site.nyit.edu/files/Program_Proposal_Guide.docx"
SCHEDULE = FILES + "AA_ClassSchedulingResources_RF_COAS_MA.xlsx"
SLIDES = "https://files.nyit.edu/files/administrative_offices/UMBFirstTimeLogOn.pptx"
LOGIN = FILES + "Login_Protected.docx"
LOCKED = FILES + "Encrypted_Budget.xlsx"
GONE = FILES + "Gone.pptx"
LEGACY = FILES + "2021_NewProgramProposalCoverSheet.doc"
PDF_FORM = "https://www.nyit.edu/files/registrar/Form.pdf"


def asset(url: str, anchor: str = "", found_on: str = "https://www.nyit.edu/academics/") -> dict:
    return {"url": url, "kind": "asset", "anchor_text": anchor, "found_on": [found_on]}


@pytest.fixture
def office_site(settings, tmp_path):
    from testkit import pdf_bytes

    src = tmp_path / "src"
    guide = make_docx(src / "guide.docx", para("Program Proposal Guide", "Title") + para("Purpose", "Heading1")
                      + para("Every new program needs a proposal approved by the Academic Senate.")
                      + table([["Step", "Office"], ["Needs analysis", "Academic Affairs"]]),
                      title="Untitled", modified="2025-01-10T09:00:00Z").read_bytes()
    fall = [cell("A1", "Course", "str") + cell("B1", "Room", "str"),
            cell("A2", "BIOL 110", "str") + cell("B2", "Theobald 405", "str")]
    schedule = make_xlsx(src / "s.xlsx", [("Fall 2025", fall, "visible"),
                                          ("Lookup", [cell("A1", "x", "str")], "hidden")],
                         title="Class Scheduling Resources").read_bytes()
    slides = make_pptx(src / "p.pptx", [slide_xml(shape(["UMB First-Time Log-In"], "ctrTitle"),
                                                  shape(["Open the portal and sign in with your NYIT account."]))],
                       [0]).read_bytes()
    web = FakeWeb()
    web.add(GUIDE, guide, content_type=DOCX_CT, **{"Last-Modified": "Wed, 15 May 2024 14:03:22 GMT"})
    web.add(GUIDE_COPY, guide, content_type=DOCX_CT)                               # the same file
    web.add(SCHEDULE, schedule, content_type=XLSX_CT, **{"Last-Modified": "Wed, 15 May 2024 14:03:22 GMT"})
    web.add(SLIDES, slides, content_type=PPTX_CT)
    web.add(LOGIN, "<!DOCTYPE html><html><head><title>Sign in</title></head></html>")
    web.add(LOCKED, ole2_bytes(), content_type="application/vnd.ms-office")
    web.add(GONE, "nope", status=404)
    web.add(PDF_FORM, pdf_bytes(tmp_path, "form.pdf", pages=["Registration form for new and returning students."]),
            content_type="application/pdf")
    settings.ensure_dirs()
    settings.max_retries = 0
    write_jsonl(settings.external_links_path, [
        asset(GUIDE, "Program Proposal Guide (Word)"), asset(GUIDE_COPY, "here"),
        asset(SCHEDULE, "Class Scheduling Resources: COAS"), asset(SLIDES, "UMB First-Time User Log-In How-To Guide"),
        asset(LOGIN, "Protected"), asset(LOCKED, "Budget"), asset(GONE, "Gone"), asset(LEGACY, "cover page"),
        asset(FILES + "picture.png", "a picture"), asset("https://my.nyit.edu/x.docx", "portal"),
        {"url": PDF_FORM, "kind": "pdf", "anchor_text": "Registration Form", "found_on": ["https://www.nyit.edu/r/"]},
    ])
    return web


def run_docs(settings, web, **kwargs):
    return dc.run_docs(settings, fetcher=make_fetcher(settings, web), **kwargs)


def test_docs_step_downloads_and_extracts_office_files(settings, office_site):
    summary = run_docs(settings, office_site)
    assert summary["input"]["office_links"] == 8 and summary["input"]["pdf_links"] == 1
    assert summary["input"]["candidates_by_type"] == {"pdf": 1, "docx": 3, "xlsx": 2, "pptx": 2}
    assert summary["status_counts"] == {"ok": 5, "not_office": 1, "encrypted": 1, "not_found": 1}
    totals = summary["totals"]
    assert totals["documents"] == 4 and totals["pdfs"] == 1 and totals["duplicate_links"] == 1
    assert {t: v["documents"] for t, v in totals["by_file_type"].items()} == \
        {"pdf": 1, "docx": 1, "xlsx": 1, "pptx": 1}
    assert totals["scanned_pdfs"] == 0 and totals["pdfs_with_bookmarks"] == 0

    unread = summary["not_extracted_files"]
    assert unread["by_type"] == {".docx": 1, ".doc": 1, ".pptx": 1, ".xlsx": 1}
    assert unread["by_reason"] == {"encrypted": 1, "legacy_binary_format": 1, "not_found": 1, "not_office": 1}
    assert {d["url"]: d["reason"] for d in unread["details"]}[LEGACY] == "legacy_binary_format"
    assert GUIDE not in unread["examples"] and GUIDE_COPY not in unread["examples"]

    docs = {d["pdf_url"]: d for d in summary["documents"]}
    guide = docs[GUIDE]
    assert guide["file_type"] == "docx" and guide["title"] == "Program Proposal Guide"
    assert guide["title_source"] == "printed_title" and guide["aliases"] == [GUIDE_COPY]
    assert guide["modified"] == "2025-01-10T09:00:00+00:00"                   # dcterms:modified
    assert guide["doc_category"] == "brochure" and guide["edition"] == "2025"
    sheet = docs[SCHEDULE]
    assert sheet["title"] == "Class Scheduling Resources" and sheet["title_source"] == "metadata"
    assert sheet["modified"] == "2024-05-15T14:03:22+00:00"                    # no dcterms: HTTP date
    assert sheet["doc_category"] == "schedule_calendar"
    assert docs[SLIDES]["title"] == "UMB First-Time Log-In" and docs[SLIDES]["pages"] == 1

    failures = {f["url"]: (f["status"], f["file_type"]) for f in summary["failures"]}
    assert failures == {LOGIN: ("not_office", "docx"), LOCKED: ("encrypted", "xlsx"), GONE: ("not_found", "pptx")}

    records = read_jsonl(settings.processed_dir / "documents.jsonl")
    by_type = {}
    for r in records:
        by_type.setdefault(r["file_type"], []).append(r)
    assert sorted(by_type) == ["docx", "pdf", "pptx", "xlsx"]
    word = by_type["docx"][0]
    assert word["url"] == GUIDE + "#page=1" and word["pdf_url"] == GUIDE and word["file"].endswith(".docx")
    assert word["toc_path"] == [] and "Needs analysis | Academic Affairs" in word["text"]
    assert by_type["xlsx"][0]["toc_path"] == ["Fall 2025"] and "Lookup" not in by_type["xlsx"][0]["text"]
    assert by_type["xlsx"][0]["text"] == "Course | Room\nBIOL 110 | Theobald 405"
    assert by_type["pptx"][0]["toc_path"] == ["UMB First-Time Log-In"]
    assert by_type["pdf"][0]["file_type"] == "pdf"
    for r in records:
        assert (settings.docs_dir / r["file"]).is_file() and r["word_count"] > 0

    manifest = {e["url"]: e for e in read_jsonl(settings.docs_manifest_path)}
    assert manifest[LOGIN]["file"] is None and "not a Word file" in manifest[LOGIN]["error"]
    assert "text/html" in manifest[LOGIN]["error"]
    assert manifest[LOCKED]["status"] == "encrypted" and manifest[LOCKED]["file"] is None
    assert not any(p.suffix in (".download", ".part") for p in settings.docs_dir.rglob("*"))


def test_rerun_with_a_complete_manifest_makes_no_request(settings, office_site):
    first = run_docs(settings, office_site)
    office_site.requests.clear()
    again = run_docs(settings, office_site)
    assert office_site.requests == []                     # not_office / encrypted / 404 are final
    assert again["downloads"]["cached"] == 5 and again["records"] == first["records"]
    office_site.requests.clear()
    offline = run_docs(settings, office_site, max_docs=0)
    assert office_site.requests == [] and offline["records"] == first["records"]
    assert read_jsonl(settings.processed_dir / "documents.jsonl") == \
        read_jsonl(settings.processed_dir / "documents.jsonl")


def test_office_files_left_for_later_and_size_limit(settings, office_site):
    summary = run_docs(settings, office_site, max_docs=0)
    assert summary["not_extracted_files"]["by_reason"] == {"max_docs": 7, "legacy_binary_format": 1}
    summary = run_docs(settings, office_site, max_mb=0.001)                      # 1,000 bytes
    statuses = {f["url"]: f["status"] for f in summary["failures"]}
    assert statuses[GUIDE] == "too_large" and statuses[SLIDES] == "too_large"
    assert summary["not_extracted_files"]["by_reason"]["too_large"] == 5      # the encrypted file too


def test_an_unreadable_office_file_is_reported_not_fatal(settings, tmp_path):
    parts = docx_parts(para("x"))
    parts["word/document.xml"] = BILLION_LAUGHS
    bomb = write_package(tmp_path / "bomb.docx", parts).read_bytes()
    web = FakeWeb()
    web.add(GUIDE, bomb, content_type=DOCX_CT)
    web.add(SLIDES, make_pptx(tmp_path / "p.pptx", [slide_xml(shape(["A slide title"], "title"))], [0]).read_bytes(),
            content_type=PPTX_CT)
    settings.ensure_dirs()
    write_jsonl(settings.external_links_path, [asset(GUIDE, "Guide"), asset(SLIDES, "Slides")])
    summary = run_docs(settings, web)
    assert summary["status_counts"] == {"ok": 2}
    assert [(f["url"], f["status"]) for f in summary["failures"]] == [(GUIDE, "extract_error")]
    assert "DOCTYPE" in summary["failures"][0]["error"]
    assert summary["not_extracted_files"]["by_reason"] == {"extract_error": 1}
    assert summary["totals"]["documents"] == 1


def test_office_pages_in_the_corpus(settings, office_site):
    run_docs(settings, office_site)
    documents = pipeline.load_documents(settings)
    corpus, _clashes = pipeline.build_corpus([], [], documents=documents)
    titles = {r.metadata["file_type"]: r.title for r in corpus}
    assert titles == {"pdf": "Registration Form (PDF, p. 1)", "docx": "Program Proposal Guide (Word, part 1)",
                      "xlsx": "Class Scheduling Resources (Excel, part 1)",
                      "pptx": "UMB First-Time Log-In (PowerPoint, slide 1)"}
    sheet = next(r for r in corpus if r.metadata["file_type"] == "xlsx")
    assert sheet.text.startswith("Class Scheduling Resources\nFall 2025\nCourse | Room")
    # a page left without a title still gets a readable one
    assert pipeline.document_title({"pdf_url": SCHEDULE, "file_type": "xlsx"}) == \
        "AA ClassSchedulingResources RF COAS MA"
    assert pipeline.document_title({"pdf_url": "https://x.nyit.edu/.pptx", "file_type": "pptx"}) == \
        "PowerPoint document"


def test_validate_and_stats_count_pdfs_apart(settings, office_site, tmp_path):
    # an image-only slide deck: every page empty, but it is no "scanned PDF"
    blank = make_pptx(tmp_path / "blank.pptx", [slide_xml(), slide_xml()], [0, 1]).read_bytes()
    office_site.add(GONE, blank, content_type=PPTX_CT)
    run_docs(settings, office_site)
    data = validate.load_inputs(settings)
    pages = validate.check_document_pages(data)
    assert pages.status == "PASS", pages.message
    text = validate.check_document_text(data)
    assert "0 PDFs are scanned" in text.message
    st = stats.document_stats(data.documents, data.documents_summary, data.docs_manifest)
    assert st["documents"] == 5 and st["pdfs"] == 1 and st["scanned_pdfs"] == 0
    assert st["by_file_type"]["pptx"] == {"documents": 2, "pages": 3, "words": 13}
    report = "\n".join(str(b) for b in stats._document_blocks(st, {}))
    assert "pptx" in report and "legacy" in report


def test_cli_docs_end_to_end(tmp_path, monkeypatch, office_site, settings):
    def fake_fetcher(s, *args, **kwargs):
        return make_fetcher(s, office_site)

    monkeypatch.setattr(dc, "PoliteFetcher", fake_fetcher)
    data = settings.data_dir
    assert cli.main(["docs", "--data-dir", str(data)]) == 0
    summary = json.loads((data / "processed" / "documents_summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["documents"] == 4
    records = read_jsonl(data / "processed" / "documents.jsonl")
    assert {r["file_type"] for r in records} == {"pdf", "docx", "xlsx", "pptx"}
    office_site.requests.clear()
    assert cli.main(["docs", "--max-docs", "0", "--data-dir", str(data)]) == 0
    assert office_site.requests == []
    assert read_jsonl(data / "processed" / "documents.jsonl") == records
