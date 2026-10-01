"""The intrinsic truncation signal: a file that stops before its own end.

Both existing truncation signals are COMPARATIVE -- one needs the record's
structured full text, the other a parseable page range -- and both decline when
their reference is absent. A record with neither received no truncation check
at all, and a real corpus PDF cut to 40% of its bytes verified as VERIFIED,
because the DOI is still in the front matter and every identity signal passes.
"""
from __future__ import annotations

import pytest

from fetchpdf.retrieval.pdf_identity import (
    TRAILER_TAIL_BYTES, TRUNCATED, VERIFIED, _Reader, verify_pdf_identity,
)


def _pdf(body: bytes = b"x" * 4096, eof: bool = True) -> bytes:
    return b"%PDF-1.7\n" + body + (b"\n%%EOF\n" if eof else b"\nstream ends here\n")


class TestTheTrailerIsReadWithoutTheEngine:
    """A truncated file is exactly where parsing may fail, so this question
    must be answerable when parsing is the thing that broke."""

    def test_bytes_source(self):
        assert _Reader(_pdf()).has_trailer()
        assert not _Reader(_pdf(eof=False)).has_trailer()

    def test_path_source(self, tmp_path):
        good = tmp_path / "good.pdf"; good.write_bytes(_pdf())
        bad = tmp_path / "bad.pdf"; bad.write_bytes(_pdf(eof=False))
        assert _Reader(str(good)).has_trailer()
        assert not _Reader(str(bad)).has_trailer()

    def test_an_incremental_update_with_several_eofs_is_intact(self):
        body = b"a" * 2048 + b"\n%%EOF\n" + b"b" * 2048
        assert _Reader(_pdf(body)).has_trailer()

    def test_a_trailer_beyond_the_window_is_not_found(self):
        body = b"\n%%EOF\n" + b"z" * (TRAILER_TAIL_BYTES + 64)
        assert not _Reader(_pdf(body, eof=False)).has_trailer()

    def test_an_unreadable_source_counts_as_INTACT(self):
        """Our failure must not be spelled the same way as a short download."""
        assert _Reader("/no/such/file.pdf").has_trailer()
        assert _Reader(None).has_trailer()

    def test_the_tail_is_read_once(self, tmp_path):
        f = tmp_path / "p.pdf"; f.write_bytes(_pdf())
        r = _Reader(str(f))
        assert r.has_trailer()
        f.unlink()                       # a second read would now fail
        assert r.has_trailer(), "the answer must be memoized like page_count"


class TestItRefusesTheFileNothingElseLookedAt:
    def test_a_short_download_is_TRUNCATED_not_VERIFIED(self):
        v = verify_pdf_identity(_pdf(eof=False), doi="10.1234/abc")
        assert v.state == TRUNCATED
        assert "%%EOF" in v.reason and "stopped before" in v.reason

    def test_it_fires_with_NO_reference_of_either_kind(self):
        """The whole point: no page range, no structured full text."""
        v = verify_pdf_identity(_pdf(eof=False), doi="10.1234/abc",
                                reference_chars=None)
        assert v.state == TRUNCATED

    def test_it_outranks_a_passing_identity_signal(self):
        """A preview carries the real DOI. Identity passing is not the question."""
        blob = b"%PDF-1.7\n" + b"doi:10.1234/abc " * 300 + b"\nno trailer\n"
        assert verify_pdf_identity(blob, doi="10.1234/abc").state == TRUNCATED

    def test_an_intact_file_is_unaffected(self):
        blob = b"%PDF-1.7\n" + b"x" * 4096 + b"\n%%EOF\n"
        assert verify_pdf_identity(blob, doi="10.1234/abc").state != TRUNCATED
