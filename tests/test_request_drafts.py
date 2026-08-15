"""Drafting request emails: the gate, the grouping, the wording.

The gate is the important half. Asking an author for a file that was never
published wastes their time, and it is the same false-alarm failure that once
made this tool warn about five records when only one had anything withheld.
"""

import json
import os

from fetchpdf.retrieval.corresponding import SOURCE_JATS, SOURCE_PDF, Contact
from fetchpdf.retrieval.request_drafts import (
    BLOCKED_HARD,
    MANUAL_LIKELY,
    NOT_PUBLISHED,
    WANT_PDF,
    WANT_SUPPLEMENT,
    Ask,
    _Group,
    classify_block,
    collect_asks,
    group_by_author,
    render,
    write_drafts,
)


def _manifest(directory, stem, doi, status="none_found", skipped=None,
              declared=None):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, stem + "_supplementary_info.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"identifiers_resolved": {"doi": doi}, "status": status,
                   "skipped": skipped or [], "declared": declared or {},
                   "files": []}, handle)
    return path


class TestTheGate:
    """Only material we can show exists earns an email."""

    def test_withheld_supplements_qualify(self, tmp_path):
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/withheld",
                  skipped=[{"reason": "epmc_not_open_access"}])
        (record / "x.xml").write_text(
            "<corresp><email>a@uni.edu</email></corresp>", encoding="utf-8")
        pairs, unresolved = collect_asks(str(tmp_path))
        assert [a.doi for _, a in pairs] == ["10.1/withheld"]
        assert pairs[0][1].wants == [WANT_SUPPLEMENT]

    def test_declared_but_unobtained_files_qualify(self, tmp_path):
        """The article's own JATS names it -- the strongest possible ask."""
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/declared",
                  declared={"missing": ["Table S1", "Appendix A"]})
        (record / "x.xml").write_text(
            "<corresp><email>a@uni.edu</email></corresp>", encoding="utf-8")
        pairs, _ = collect_asks(str(tmp_path))
        assert "Table S1" in pairs[0][1].detail

    def test_nothing_missing_earns_nothing(self, tmp_path):
        """THE REGRESSION. An address is available, but there is nothing to ask for.

        Drafting here would repeat the false-alarm mistake: telling the user
        material is missing when the record simply has none.
        """
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/fine", status="none_found",
                  skipped=[{"reason": "no-bundle"}])
        (record / "x.xml").write_text(
            "<corresp><email>a@uni.edu</email></corresp>", encoding="utf-8")
        (record / "x.pdf").write_bytes(b"%PDF-1.4 stub")
        pairs, unresolved = collect_asks(str(tmp_path))
        assert pairs == [] and unresolved == []

    def test_a_failed_paper_qualifies_as_a_reprint_request(self, tmp_path):
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/nopdf")
        pairs, unresolved = collect_asks(str(tmp_path),
                                         failed_dois=["10.1/nopdf"])
        assert (unresolved and unresolved[0].wants == [WANT_PDF])

    def test_a_failed_paper_that_did_land_is_not_requested(self, tmp_path):
        """failed_dois can be stale; the artifact on disk is the ground truth."""
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/landed")
        (record / "x.pdf").write_bytes(b"%PDF-1.4 stub")
        pairs, unresolved = collect_asks(str(tmp_path),
                                         failed_dois=["10.1/landed"])
        assert pairs == [] and unresolved == []

    def test_news_items_are_excluded(self, tmp_path):
        """No paper to request, and where extraction is least reliable."""
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1038/d41586-x")
        pairs, unresolved = collect_asks(
            str(tmp_path), failed_dois=["10.1038/d41586-x"],
            news_dois=["10.1038/d41586-x"])
        assert pairs == [] and unresolved == []

    def test_a_gap_with_no_address_goes_to_unresolved(self, tmp_path):
        record = tmp_path / "rec"
        _manifest(str(record), "x", "10.1/noaddr",
                  skipped=[{"reason": "epmc_not_open_access"}])
        pairs, unresolved = collect_asks(str(tmp_path))
        assert pairs == [] and [a.doi for a in unresolved] == ["10.1/noaddr"]


class TestGrouping:
    def test_one_file_per_author_not_per_paper(self):
        contact = Contact("nmazar@bu.edu", SOURCE_PDF, paper_year=2022)
        pairs = [(contact, Ask(doi=f"10.1/{i}", year=2020 + i,
                               wants=[WANT_SUPPLEMENT])) for i in range(3)]
        groups = group_by_author(pairs)
        assert len(groups) == 1
        assert [a.year for a in groups["nmazar@bu.edu"].asks] == [2022, 2021, 2020]

    def test_the_newest_address_wins_and_older_ones_are_kept(self):
        """One author had four addresses in a real corpus, two of them dead."""
        old = Contact("nina.mazar@utoronto.ca", SOURCE_PDF, paper_year=2013)
        new = Contact("nmazar@bu.edu", SOURCE_PDF, paper_year=2022)
        groups = group_by_author([
            (old, Ask(doi="10.1/old", year=2013, wants=[WANT_SUPPLEMENT])),
            (new, Ask(doi="10.1/new", year=2022, wants=[WANT_SUPPLEMENT])),
        ])
        # Different addresses are different groups; the alternates surface in
        # the draft rather than being silently merged.
        assert len(groups) == 2


class TestRendering:
    def _group(self, **kw):
        contact = Contact(kw.pop("email", "a@uni.edu"), kw.pop("source", SOURCE_JATS),
                          name=kw.pop("name", None), paper_doi="10.1/x",
                          paper_year=2022, cue="corresp")
        return _Group(contact=contact,
                      asks=[Ask(doi="10.1/x", year=2022, wants=[WANT_SUPPLEMENT])])

    def test_a_known_name_is_used_and_an_unknown_one_is_not_faked(self):
        assert "Dear Dr. Mara Mather," in render(self._group(name="Mara Mather"))
        assert "Dear Author," in render(self._group())

    def test_provenance_and_the_never_sent_notice_are_present(self):
        text = render(self._group(name="X"))
        assert "Address source:" in text and "10.1/x" in text
        assert "Nothing has been emailed" in text

    def test_the_purpose_sentence_is_included(self):
        assert "reproducibility" in render(self._group())

    def test_written_into_the_given_directory(self, tmp_path):
        written = write_drafts(
            group_by_author([(Contact("a@uni.edu", SOURCE_JATS),
                              Ask(doi="10.1/x", wants=[WANT_SUPPLEMENT]))]),
            [], directory=str(tmp_path))
        assert len(written) == 1
        name = os.path.basename(written[0][0])
        assert name.startswith("email_request_") and name.endswith(".md")


class TestBlockClassification:
    """Measured on three real publisher pages, headed browser, same VPN."""

    def test_a_cleared_page_with_links_is_not_a_block(self):
        assert classify_block(200, "RETRACTED: Signing at the beginning", 1) == ""

    def test_an_uncleared_interstitial_is_a_hard_block(self):
        assert classify_block(403, "Just a moment...", 0) == BLOCKED_HARD

    def test_a_plain_403_is_worth_a_human_click(self):
        assert classify_block(403, "Some Article Title", 0) == MANUAL_LIKELY

    def test_a_clean_page_with_no_links_has_nothing_to_offer(self):
        """Not a block. Reporting it as one sends the user chasing a ghost."""
        assert classify_block(200, "Some Article", 0) == NOT_PUBLISHED
