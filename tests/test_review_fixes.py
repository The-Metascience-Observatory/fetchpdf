"""Regression tests for the fixes from the October 2026 code review."""
import os

import pytest

import fetchpdf.fetchpdf as fpd
from fetchpdf.retrieval import supplement_graph, supplementary
from fetchpdf.retrieval.fulltext_scan import scan_text
from fetchpdf.retrieval.validate import _normalise_doi, _parse_html


@pytest.mark.parametrize("bad", ["https://x.org/a\nb", "https://x.org/a\rb",
                                 "https://x.org/a\tb"])
def test_url_with_control_characters_is_rejected(bad):
    # The list held "\\n" (backslash, n), so real newlines passed.
    assert not fpd._is_plausible_http_url(bad)


def test_mixed_case_dataverse_doi_routes():
    # Real Dataverse DOIs are written 10.7910/DVN/XXXX; routing compared the
    # un-lowercased DOI against "10.7910/dvn" and dropped them.
    assert supplement_graph._repo_provider("10.7910/DVN/ABCDEF") == "dataverse_files"
    assert supplement_graph._has_repo_enumerator("10.7910/DVN/ABCDEF")
    assert supplement_graph._repo_provider("10.1000/not-a-deposit") is None


def test_dx_doi_org_prefix_is_normalised():
    assert _normalise_doi("https://dx.doi.org/10.1/X") == "10.1/x"


def test_html_with_xml_encoding_declaration_parses():
    text = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<html><body><p>hello</p></body></html>')
    assert _parse_html(text) is not None


def test_github_ident_drops_sentence_period_and_keeps_github_io():
    found = {c.ident for c in scan_text(
        "Code: github.com/owner/repo. Site: github.com/owner/name.github.io")}
    assert "owner/repo" in found
    assert "owner/name.github.io" in found


def test_derive_status_ladder():
    lost = [{"reason": next(iter(supplementary._LOST_REASONS))}]
    assert supplementary._derive_status(["a.csv"], [], [], []) == "incomplete"
    assert supplementary._derive_status([], lost, [{"f": 1}], []) == "partial"
    assert supplementary._derive_status([], [], [{"f": 1}], []) == "ok"
    assert supplementary._derive_status([], [], [], [{"status": "error"}]) == "error"
    assert supplementary._derive_status([], [], [], []) == "none_found"


def test_record_path_resolves_linked_deposit_under_artifact_root(tmp_path):
    # Linked-deposit files record "<slug>/<name>" relative to
    # <stem>_data_artifacts/, not to the manifest's directory.
    flat = supplementary.record_path(str(tmp_path), "paper", "paper_si_1.csv")
    assert flat == os.path.join(str(tmp_path), "paper_si_1.csv")
    nested = supplementary.record_path(str(tmp_path), "paper", "zenodo_1/data.csv")
    assert nested == os.path.join(str(tmp_path), "paper" + supplementary.DATA_ARTIFACT_INFIX,
                                  "zenodo_1/data.csv")


class _Stream:
    def __init__(self, chunks, fail=False):
        self._chunks, self._fail = chunks, fail

    def iter_content(self, n=8192):
        yield from self._chunks
        if self._fail:
            raise ConnectionError("cut off")


def test_interrupted_stream_leaves_no_partial_pdf(tmp_path):
    save = str(tmp_path / "rec.pdf")
    with pytest.raises(ConnectionError):
        fpd._stream_pdf_to(_Stream([b"%PDF-1.4 ", b"x" * 100], fail=True),
                           save, "https://p.example/a.pdf", require_magic=False)
    assert not os.path.exists(save)
    assert not os.path.exists(save + ".part")


def test_octet_stream_without_magic_writes_nothing(tmp_path):
    save = str(tmp_path / "rec.pdf")
    assert not fpd._stream_pdf_to(_Stream([b"PK\x03\x04zip"]), save,
                                  "https://p.example/a", require_magic=True)
    assert not os.path.exists(save)


def test_html_saved_as_pdf_is_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(fpd, "_print_yellow_warning", lambda *a, **k: None)
    save = tmp_path / "rec.pdf"
    save.write_bytes(b"<!DOCTYPE html><html>Please sign in</html>")
    assert not fpd._accept_downloaded_pdf(str(save), "10.1/x", "https://p.example/a")
    assert not save.exists()


def test_xml_at_xml_path_is_still_accepted(tmp_path):
    save = tmp_path / "rec.xml"
    save.write_bytes(b"<article/>")
    assert fpd._accept_downloaded_pdf(str(save), "10.1/x")
