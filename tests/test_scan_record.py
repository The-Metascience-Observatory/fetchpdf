"""What `scan_record` will and will not read off disk.

The file-locating half of `fulltext_scan` had no coverage at all, and it is
where a whole class of records went missing: `_SCANNABLE` listed only markup,
so a PDF-only record was never scanned -- and reported as one where nothing was
found. Measured 2026-08-22 across four corpora, **606 of 1,491 records have a
PDF and no XML or HTML**.

PDF fixtures are BUILT here rather than shipped, following
tests/test_extract_images.py: a binary in the tree is a fixture nobody can read
the diff of. `importorskip` is inside each test, never at module level, so the
no-engine test still runs on a machine without PyMuPDF.
"""

import os

import pytest

from fetchpdf.retrieval import fulltext_scan
from fetchpdf.retrieval.fulltext_scan import ACCEPT, REFUSE, scan_record

_DEPOSIT = ("Data availability\n"
            "All data are deposited at https://osf.io/abcde/.\n")

_WITH_BIBLIOGRAPHY = _DEPOSIT + (
    "\nReferences\n1. Someone. A paper. https://osf.io/zzzzz/. 2020.\n")


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


#: Filler so a fixture clears pdf_text.MIN_USEFUL_CHARS. That floor is what
#: separates "this document is a stack of page images" from "this document has
#: prose", and a real paper runs 30-36k characters (measured over 580 corpus
#: PDFs), so padding the fixture is more faithful than lowering the floor.
_BODY = [("Participants completed the task in a single session and the "
          "resulting measures were analysed with a mixed-effects model. ")
         * 2] * 6


def _pdf(tmp_path, name, lines):
    """A text-bearing PDF, built rather than shipped."""
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    y = 72
    for line in list(_BODY) + list(lines):
        page.insert_text((36, y), line, fontsize=8)
        y += 12
    path = tmp_path / name
    document.save(str(path))
    document.close()
    return path


def test_no_files_is_not_an_error(tmp_path):
    assert scan_record(str(tmp_path / "10.1234--nothing")) == []


def test_markup_is_read(tmp_path):
    _write(tmp_path, "rec.xml", _DEPOSIT)
    found = scan_record(str(tmp_path / "rec"))
    assert [(c.ident, c.verdict) for c in found] == [("abcde", ACCEPT)]


def test_suffixed_markup_names_are_reached(tmp_path):
    """`.fulltext.html` and a converted body are how real records are named.

    The path is built by literal concatenation, so these only work because the
    suffixes are listed -- a plain `.html` entry does not reach them.
    """
    for name in ("rec.fulltext.html", "rec_from_pdf_body.md",
                 "rec_from_xml.md", "rec_from_html.md"):
        directory = tmp_path / name.replace(".", "_")
        directory.mkdir()
        _write(directory, name, _DEPOSIT)
        found = scan_record(str(directory / "rec"))
        assert [c.ident for c in found] == ["abcde"], name


def test_a_pdf_only_record_is_scanned(tmp_path):
    """The 606-record hole: a PDF with no markup beside it."""
    _pdf(tmp_path, "rec.pdf", _DEPOSIT.splitlines())
    found = scan_record(str(tmp_path / "rec"))
    assert [(c.ident, c.verdict) for c in found] == [("abcde", ACCEPT)]


def test_a_pdf_reference_list_does_not_become_a_candidate(tmp_path):
    """Precision, not recall, is what decides whether PDFs may be read at all.

    Without the plain-text reference guard every bibliography URL in every
    PDF-only record would be downloaded and filed under this paper.
    """
    _pdf(tmp_path, "rec.pdf", _WITH_BIBLIOGRAPHY.splitlines())
    verdicts = {c.ident: c.verdict for c in scan_record(str(tmp_path / "rec"))}
    assert verdicts["abcde"] == ACCEPT
    assert verdicts["zzzzz"] == REFUSE


def test_markup_wins_and_the_pdf_is_not_also_read(tmp_path):
    """The PDF is a fallback, not a second vote.

    It is the same prose with the citation structure stripped, so scanning both
    would only give the weaker reading a chance to overturn the stronger one.
    """
    _write(tmp_path, "rec.xml", _DEPOSIT)
    _pdf(tmp_path, "rec.pdf", ["Deposited at https://osf.io/pdfid/."])
    assert [c.ident for c in scan_record(str(tmp_path / "rec"))] == ["abcde"]


def test_no_pdf_engine_says_so_rather_than_finding_nothing(tmp_path, monkeypatch):
    """The difference between "no reader here" and "no deposit there".

    Silence would report a machine misconfiguration as a fact about the paper,
    which is the failure this whole subsystem exists to avoid.
    """
    (tmp_path / "rec.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pymupdf", lambda: None)
    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pypdf", lambda: None)
    messages = []
    assert scan_record(str(tmp_path / "rec"), log=messages.append) == []
    assert any("no PDF text engine" in m for m in messages)


def test_a_pdf_without_a_text_layer_says_so_too(tmp_path):
    """A different fact from the one above, and it needs a different sentence."""
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page()          # a page, no text on it
    path = tmp_path / "rec.pdf"
    document.save(str(path))
    document.close()
    messages = []
    assert scan_record(str(tmp_path / "rec"), log=messages.append) == []
    assert any("no text layer" in m for m in messages)


def test_an_unreadable_pdf_is_not_an_exception(tmp_path):
    """Corpora contain .pdf files that are HTML error pages. Measured."""
    (tmp_path / "rec.pdf").write_bytes(b"Successfully logged out<html>")
    assert scan_record(str(tmp_path / "rec"), log=lambda m: None) == []
