"""The package must run on Python 3.9 to 3.14 and on Windows, macOS and Linux.

* Every module must parse with the Python 3.9 grammar (``ast.parse(feature_version=(3, 9))``).
* ``ast`` does not see the f-string syntax of Python 3.12 (PEP 701: the same quotes or a
  backslash inside a replacement field, comments in f-strings), so on 3.12+ the tokens are
  checked too; on older interpreters such code does not even parse, which the AST check
  catches.
* Every text file is opened with an explicit encoding (Windows would use cp1252 otherwise).
* requirements.txt lists every third-party package the package and the tests import, so
  that `pip install -r requirements.txt` is enough on a fresh computer.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(ROOT.glob("askbear_scraper/*.py")) + sorted(ROOT.glob("tests/*.py"))
PACKAGE = sorted(ROOT.glob("askbear_scraper/*.py"))


def _ids(paths):
    return [p.relative_to(ROOT).as_posix() for p in paths]


@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_parses_with_the_python_39_grammar(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


def pep701_problems(source: str) -> List[str]:
    """f-string features that need Python 3.12 (PEP 701), found from the 3.12+ tokens:
    a quote of the enclosing single-quoted f-string reused inside a replacement field, a
    backslash or a comment inside a replacement field."""
    problems: List[str] = []
    stack: List[dict] = []          # open f-strings: their quote and the {} depth inside
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_START:
            quote = tok.string.lstrip("rRbBfFuU")
            if stack and stack[-1]["depth"] and len(stack[-1]["quote"]) == 1 \
                    and quote[0] == stack[-1]["quote"]:
                problems.append(f"line {tok.start[0]}: nested f-string reuses the quote {quote[0]}")
            stack.append({"quote": quote, "depth": 0})
            continue
        if not stack:
            continue
        top = stack[-1]
        if tok.type == tokenize.FSTRING_END:
            stack.pop()
        elif tok.type == tokenize.OP and tok.string == "{":
            top["depth"] += 1
        elif tok.type == tokenize.OP and tok.string == "}":
            top["depth"] -= 1
        elif top["depth"] and tok.type == tokenize.STRING:
            quote = tok.string.lstrip("rRbBfFuU")
            if len(top["quote"]) == 1 and quote[0] == top["quote"]:
                problems.append(f"line {tok.start[0]}: {tok.string} reuses the f-string's quote")
            if "\\" in tok.string:
                problems.append(f"line {tok.start[0]}: backslash inside an f-string expression")
        elif top["depth"] and tok.type == tokenize.COMMENT:
            problems.append(f"line {tok.start[0]}: comment inside an f-string")
    return problems


needs_312 = pytest.mark.skipif(sys.version_info < (3, 12),
                               reason="before 3.12 such f-strings do not parse at all "
                                      "(test_parses_with_the_python_39_grammar catches them)")


@needs_312
def test_pep701_checker_finds_the_312_only_forms():
    assert pep701_problems('d = {}\nx = f"{d["k"]}"\n')
    assert pep701_problems("x = f'{\"\\n\".join(a)}'\n")
    assert not pep701_problems('x = f"{d[\'k\']}" + f"""{d["k"]}"""\n')
    assert not pep701_problems('x = f"{a!r:>{width}} {{literal}}"\n')


@needs_312
@pytest.mark.parametrize("path", SOURCES, ids=_ids(SOURCES))
def test_no_python_312_only_f_strings(path):
    assert pep701_problems(path.read_text(encoding="utf-8")) == []


# modules whose open() reads a document, not a text file
_NOT_TEXT_OPENERS = {"pymupdf", "fitz", "zipfile", "tarfile", "gzip", "webbrowser"}


def _text_mode(call: ast.Call, method: bool) -> bool:
    """open(file, mode) / Path(...).open(mode): True unless the mode is binary."""
    position = 0 if method else 1
    mode = call.args[position] if len(call.args) > position else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return True
    return not (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value)


def unencoded_opens(source: str) -> List[str]:
    """open()/Path.open()/read_text()/write_text() in text mode without encoding=."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        method = isinstance(func, ast.Attribute)
        name = func.attr if method else (func.id if isinstance(func, ast.Name) else "")
        if any(k.arg == "encoding" or k.arg is None for k in node.keywords):
            continue
        if name in ("read_text", "write_text"):
            found.append(f"line {node.lineno}: {name}() without encoding")
        elif name == "open":
            if method and isinstance(func.value, ast.Name) and func.value.id in _NOT_TEXT_OPENERS:
                continue
            if _text_mode(node, method):
                found.append(f"line {node.lineno}: open() without encoding")
    return found


def test_unencoded_open_detector():
    assert unencoded_opens("open('a.txt')\nPath('a').read_text()\nopen('b', 'w')\n") == [
        "line 1: open() without encoding", "line 2: read_text() without encoding",
        "line 3: open() without encoding"]
    assert unencoded_opens("open('a', 'rb')\nPath('a').open('wb')\npymupdf.open('x.pdf')\n"
                           "Path('a').open('r', encoding='utf-8')\n") == []


@pytest.mark.parametrize("path", PACKAGE, ids=_ids(PACKAGE))
def test_text_files_are_opened_with_an_encoding(path):
    assert unencoded_opens(path.read_text(encoding="utf-8")) == []


# import name -> the name of the package on PyPI (and in requirements.txt)
_DISTRIBUTION = {"bs4": "beautifulsoup4", "fitz": "pymupdf"}
# this project's own modules (tests/testkit.py is importable because of pytest.ini)
_LOCAL_MODULES = {"askbear_scraper", "testkit", "conftest"}


def required_packages() -> set:
    """Package names listed in requirements.txt (comments and version pins removed)."""
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def imported_top_modules(source: str) -> set:
    """Top-level names of the absolute imports of a module (also those inside functions)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.skipif(not hasattr(sys, "stdlib_module_names"),
                    reason="sys.stdlib_module_names needs Python 3.10+")
def test_requirements_cover_every_third_party_import():
    required = required_packages()
    missing = []
    for path in SOURCES:
        for module in sorted(imported_top_modules(path.read_text(encoding="utf-8"))):
            if module in sys.stdlib_module_names or module in _LOCAL_MODULES:
                continue
            if _DISTRIBUTION.get(module, module).lower().replace("_", "-") not in required:
                missing.append(f"{path.relative_to(ROOT).as_posix()} imports {module}")
    assert missing == []
    # BeautifulSoup(html, "lxml") needs lxml without importing it
    assert "lxml" in required
