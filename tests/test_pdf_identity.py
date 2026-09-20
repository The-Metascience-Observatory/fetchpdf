"""Offline tests for "is this PDF the paper we asked for?".

Every PDF here is built, not shipped -- see `conftest.text_pdf`. The cases are
not invented: each one is a shape that showed up when the verifier was measured
against 500 real main-article PDFs from the corpora on this machine, and the
numbers in the comments are from that run.
"""

import pytest

from conftest import text_pdf
from fetchpdf.retrieval import pdf_identity
from fetchpdf.retrieval.pdf_identity import (
    NO_ENGINE,
    UNREADABLE,
    VERIFIED,
    WRONG,
    verify_pdf_identity,
)

TITLE = ("The maternal diet index in pregnancy is associated with offspring "
         "allergic diseases: the Healthy Start study")
DOI = "10.1111/all.14949"

#: Enough prose to clear the legibility floor, so these tests exercise the
#: signal they name rather than the "nothing readable here" branch.
PROSE = ["Mothers were enrolled during the second trimester and offspring "
         "outcomes were ascertained at age four by parental report and "
         "physician diagnosis across the full cohort."] * 8


def _pdf(tmp_path, name, lines):
    path = tmp_path / name
    path.write_bytes(text_pdf(lines))
    return str(path)


# --------------------------------------------------------------------------
# Accepting
# --------------------------------------------------------------------------

def test_version_of_record_is_accepted_on_its_printed_doi(tmp_path):
    path = _pdf(tmp_path, "vor.pdf", [TITLE, f"https://doi.org/{DOI}"] + PROSE)
    verdict = verify_pdf_identity(path, DOI, TITLE)
    assert verdict.state == VERIFIED
    assert verdict.signals == ["doi-in-text"]


def test_accepted_manuscript_is_accepted_on_its_title_alone(tmp_path):
    """The false-reject guard, and the most important test in this file.

    NIH author manuscripts (nihms-*.pdf) print the title and NOT the
    publisher's DOI -- that is what an accepted manuscript is. If title
    matching did not carry them, reject-always would delete every one.
    """
    path = _pdf(tmp_path, "nihms.pdf", [TITLE] + PROSE)
    verdict = verify_pdf_identity(path, DOI, TITLE)
    assert verdict.state == VERIFIED
    assert verdict.signals == ["title-on-page"]


def test_title_behind_a_cover_and_a_contents_page_is_accepted(tmp_path):
    """A chapter deposited with its book's front matter prints the title on page 3.

    10.31234/osf.io/2tqep: a cover image, a contents list, then the citation
    line carrying the title. Nothing on it prints the preprint's DOI.
    """
    cover_and_contents = ["Contents"] + ["1 INTRODUCTION 1"] * 103   # pages 1-2
    path = _pdf(tmp_path, "chapter.pdf", cover_and_contents + [TITLE] + PROSE)
    verdict = verify_pdf_identity(path, DOI, TITLE)
    assert verdict.state == VERIFIED
    assert verdict.signals == ["title-on-page"]


def test_doi_broken_across_a_line_still_matches(tmp_path):
    """PDF text extraction wraps lines wherever the typesetter did."""
    path = _pdf(tmp_path, "wrapped.pdf", [TITLE, "https://doi.org/10.1111/", "all.14949"] + PROSE)
    assert verify_pdf_identity(path, DOI, TITLE).state == VERIFIED


def test_title_broken_across_lines_and_hyphenated_still_matches(tmp_path):
    path = _pdf(tmp_path, "hyphen.pdf", [
        "The maternal diet index in preg-",
        "nancy is associated with offspring",
        "allergic diseases: the Healthy Start study",
    ] + PROSE)
    verdict = verify_pdf_identity(path, "10.9999/absent", TITLE)
    assert verdict.state == VERIFIED


def test_greek_letter_typeset_as_latin_still_matches(tmp_path):
    """Measured on a real record: a title carrying "V717F β-Amyloid" is typeset
    "V717F b-Amyloid". That one substitution scores 0.62 by longest run and
    0.99 by matched total, which is why the check sums matching blocks."""
    title = ("Comparison of Neurodegenerative Pathology in Transgenic Mice "
             "Overexpressing V717F β-Amyloid Precursor Protein")
    on_page = title.replace("β", "b")
    path = _pdf(tmp_path, "greek.pdf", [on_page] + PROSE)
    assert verify_pdf_identity(path, "10.1523/jneurosci.16-18", title).state == VERIFIED


def test_html_entity_in_the_crossref_title_still_matches(tmp_path):
    """Crossref hands back "salience &amp; message framing"; the page says "&"."""
    path = _pdf(tmp_path, "entity.pdf", [
        "Moving citizens online: Using salience & message framing to motivate "
        "behavior change"] + PROSE)
    verdict = verify_pdf_identity(
        path, "10.1177/absent",
        "Moving citizens online: Using salience &amp; message framing to "
        "motivate behavior change")
    assert verdict.state == VERIFIED


def test_ligature_and_accent_do_not_break_the_match(tmp_path):
    path = _pdf(tmp_path, "lig.pdf", ["Identiﬁcation of the café effect"] + PROSE)
    assert verify_pdf_identity(
        path, "10.1/x", "Identification of the cafe effect").state == VERIFIED


@pytest.mark.parametrize("title", [
    "Материнская диета при беременности",   # Cyrillic
    "母体食事指数と妊娠",                        # CJK
    "Ελληνικός τίτλος μελέτης",             # Greek
])
def test_non_latin_titles_survive_normalisation(title):
    """`[^a-z0-9]` squashed these to the EMPTY STRING, and an empty title
    matches nothing -- so under reject-always every paper with a non-Latin
    title and no printed DOI would have been deleted as somebody else's.

    Asserted on the normaliser rather than through a built PDF because the
    single-font PDF builder in conftest writes WinAnsi and cannot carry these
    scripts at all; the defect was never in the PDF layer.
    """
    from fetchpdf.retrieval._util import squash, titles_match

    assert squash(title), "title squashed away to nothing"
    assert titles_match(title, title)
    assert not titles_match(title, "An unrelated English title about mice")


# --------------------------------------------------------------------------
# Refusing
# --------------------------------------------------------------------------

def test_a_cited_document_is_refused(tmp_path):
    """The reported bug: the USDA report a paper cites, saved as the paper."""
    path = _pdf(tmp_path, "usda.pdf", [
        "Dietary Guidelines for Americans 2020-2025",
        "Make Every Bite Count With the Dietary Guidelines",
    ] + ["Consume a healthy dietary pattern that accounts for all foods and "
         "beverages within an appropriate calorie level at every life stage."] * 8)
    verdict = verify_pdf_identity(path, DOI, TITLE)
    assert verdict.state == WRONG
    assert not verdict.ok


def test_a_publisher_boilerplate_page_is_refused(tmp_path):
    """Seven of seventeen real rejections in the corpus audit were this exact
    document: a permissions page served where the article should be."""
    path = _pdf(tmp_path, "perms.pdf", [
        "Lippincott Journal Portfolio Author Permission Guidelines",
    ] + ["All requests to reuse your Final Published Article must be submitted "
         "in writing through the publisher's rights and permissions portal."] * 8)
    assert verify_pdf_identity(path, DOI, TITLE).state == WRONG


def test_another_doi_on_the_page_is_not_evidence_of_a_different_article(tmp_path):
    """A published paper routinely prints its own preprint's DOI, and that is
    the same work. Measured before this branch was removed: it convicted 0 of
    36 known-bad files and 3 of 706 known-good ones -- all three papers naming
    their own medRxiv/bioRxiv/chemRxiv posting.
    """
    path = _pdf(tmp_path, "preprint.pdf",
                [TITLE, "previously posted at doi:10.1101/2022.01.07.22268854"] + PROSE)
    assert verify_pdf_identity(path, DOI, TITLE).state == VERIFIED


def test_an_unknown_doi_does_not_convict_every_document(tmp_path):
    """With `doi=None` the old guards short-circuited and every DOI printed on
    the page became "foreign" -- so a PMID-only record deleted its own PDF."""
    path = _pdf(tmp_path, "any.pdf", ["Some paper", "doi:10.1234/whatever"] + PROSE)
    verdict = verify_pdf_identity(path, None, "")
    assert verdict.state != WRONG
    assert not verdict.ok


def test_a_doi_truncated_by_a_line_break_does_not_convict(tmp_path):
    """"10.1038/s44160-" is a wrapped DOI, not a different article."""
    path = _pdf(tmp_path, "trunc.pdf", [TITLE, "cited: 10.1038/s44160-"] + PROSE)
    assert verify_pdf_identity(path, DOI, TITLE).state == VERIFIED


# --------------------------------------------------------------------------
# Saying WHY, honestly
# --------------------------------------------------------------------------

def test_a_scan_with_no_text_layer_is_unreadable_not_wrong(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(text_pdf([""]))
    verdict = verify_pdf_identity(str(path), DOI, TITLE)
    assert verdict.state == UNREADABLE
    assert "wrong" not in verdict.reason.lower()


def test_a_download_stamp_is_not_evidence_of_another_article(tmp_path):
    """Two correct 1990s JNEN scans in the corpus extracted to nothing but a
    library stamp. That is text, but not text that could have carried a title,
    so it must read as unreadable rather than as a retrieval bug."""
    path = _pdf(tmp_path, "stamp.pdf", [
        "by guest on June 5, 2016 http://jnen.oxfordjournals.org/ Downloaded from"])
    verdict = verify_pdf_identity(path, "10.1097/00005072-199211000-00003", TITLE)
    assert verdict.state == UNREADABLE
    assert not verdict.ok


def test_the_three_refusals_do_not_share_a_sentence(tmp_path):
    """A wrong article, an illegible scan and a broken install are three
    different facts. Collapsing them hides two behind the third."""
    wrong = _pdf(tmp_path, "w.pdf", ["Entirely unrelated document"] + PROSE)
    blank = tmp_path / "b.pdf"
    blank.write_bytes(text_pdf([""]))
    reasons = {
        verify_pdf_identity(wrong, DOI, TITLE).reason,
        verify_pdf_identity(str(blank), DOI, TITLE).reason,
    }
    assert len(reasons) == 2


def test_no_engine_is_reported_as_a_broken_install(tmp_path, monkeypatch):
    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pymupdf", lambda: None)
    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pypdf", lambda: None)
    path = _pdf(tmp_path, "any.pdf", [TITLE] + PROSE)
    verdict = verify_pdf_identity(path, DOI, TITLE)
    assert verdict.state == NO_ENGINE
    assert not verdict.ok


def test_the_cli_refuses_to_start_without_an_engine(monkeypatch):
    """The failure mode this guards is a 5,000-DOI run ending with zero PDFs
    and 5,000 individually reasonable warnings -- indistinguishable from
    "nothing was available", which is the confusion this change exists to end.
    """
    import sys

    import fetchpdf.fetchpdf as fpd

    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pymupdf", lambda: None)
    monkeypatch.setattr("fetchpdf.retrieval.pdf_text._pypdf", lambda: None)
    monkeypatch.setattr(sys, "argv", ["fetchpdf", "10.1111/all.14949"])
    with pytest.raises(SystemExit):
        fpd.main()


# --------------------------------------------------------------------------
# The title the check runs on
# --------------------------------------------------------------------------

def test_title_comes_from_the_memo_rather_than_the_network():
    from fetchpdf.retrieval.identifiers import IdentifierSet

    ids = IdentifierSet(doi=DOI)
    ids.memo["crossref"] = {"title": [TITLE]}
    assert pdf_identity.article_title(ids) == TITLE


def test_unpaywall_supplies_the_title_when_crossref_did_not_run():
    from fetchpdf.retrieval.identifiers import IdentifierSet

    ids = IdentifierSet(doi=DOI)
    ids.memo["unpaywall"] = {"title": TITLE}
    assert pdf_identity.article_title(ids) == TITLE


class TestAnArxivPaperIsCheckedAgainstTheIdItPrints:
    """The class of record that had no signal at all until this existed.

    arXiv mints its DOIs at DataCite, so Crossref -- the only place
    `article_title` and `article_pages` read from -- has no record of one, and
    the PDF arXiv serves is stamped with `arXiv:2605.04265v1` down the margin
    and never with `10.48550/arXiv.2605.04265`. Every signal was therefore
    unavailable, the verdict was NO_REFERENCE, and the engine reads that as a
    refusal: `--get-xml-or-html 10.48550/arXiv.2605.04265` fetched the PDF,
    dropped it, and kept only the HTML.
    """

    ARXIV_DOI = "10.48550/arXiv.2605.04265"
    ARXIV_ID = "2605.04265"
    #: How arXiv stamps a served PDF: id, version, category, date.
    STAMP = "arXiv:2605.04265v1  [q-bio.BM]  6 May 2026"

    def test_the_stamped_id_verifies_the_pdf(self, tmp_path):
        path = _pdf(tmp_path, "arxiv.pdf", [self.STAMP, "Some Preprint"] + PROSE)
        verdict = verify_pdf_identity(path, self.ARXIV_DOI, "",
                                      arxiv_id=self.ARXIV_ID)
        assert verdict.state == VERIFIED
        assert verdict.signals == ["arxiv-id-in-text"]

    def test_a_different_version_of_the_same_paper_still_verifies(self, tmp_path):
        """v1 and v3 are the same paper, so the suffix is not compared."""
        path = _pdf(tmp_path, "v3.pdf", ["arXiv:2605.04265v3  [q-bio.BM]"] + PROSE)
        verdict = verify_pdf_identity(path, self.ARXIV_DOI, "",
                                      arxiv_id=self.ARXIV_ID + "v1")
        assert verdict.state == VERIFIED

    def test_pre_2007_ids_carry_their_archive_name(self, tmp_path):
        path = _pdf(tmp_path, "old.pdf", ["arXiv:hep-th/9901001v2"] + PROSE)
        verdict = verify_pdf_identity(path, "10.48550/arXiv.hep-th/9901001", "",
                                      arxiv_id="hep-th/9901001")
        assert verdict.state == VERIFIED

    def test_somebody_elses_arxiv_paper_is_not_accepted(self, tmp_path):
        path = _pdf(tmp_path, "other.pdf", ["arXiv:2408.01234v1"] + PROSE)
        verdict = verify_pdf_identity(path, self.ARXIV_DOI, "",
                                      arxiv_id=self.ARXIV_ID)
        assert verdict.state != VERIFIED

    def test_the_bare_number_is_not_enough(self, tmp_path):
        """Squashing strips the dot, so the id alone is nine digits -- the shape
        of a grant number, an accession or a phone number. Only the `arXiv:`
        prefix in front of it makes the match mean anything."""
        path = _pdf(tmp_path, "grant.pdf", ["Funded under award 2605.04265"] + PROSE)
        verdict = verify_pdf_identity(path, self.ARXIV_DOI, "",
                                      arxiv_id=self.ARXIV_ID)
        assert verdict.state != VERIFIED

    def test_the_id_is_derived_from_the_doi_without_a_network_call(self):
        from fetchpdf.retrieval.identifiers import IdentifierSet

        assert pdf_identity.article_arxiv_id(
            IdentifierSet(doi=self.ARXIV_DOI)) == self.ARXIV_ID
        assert pdf_identity.article_arxiv_id(IdentifierSet(doi=DOI)) == ""


# --------------------------------------------------------------------------
# Scans, which can only offer structural evidence
# --------------------------------------------------------------------------

class TestPageCountCorroboration:
    """A scanned paper has no text to check, but it still has a shape.

    Deliberately NOT "old papers are probably fine". A publication year is a
    fact about the DOI we asked for, not about the file we got, so believing it
    would wave through exactly the mis-fetch this module exists to catch. A
    page count is a property of the FILE. Measured on the corpus: four scans
    from 1992-1995 match their printed page ranges exactly, while a publisher
    advertisement standing in for a six-page 2023 article is one page long and
    is still refused.
    """

    @pytest.mark.parametrize("page_range,expected", [
        ("107-122", 16), ("585-593", 9), ("314", 1),
        ("1049-58", 10),            # abbreviated second half of the range
        ("S1-S8", 8), ("e12345", 1),
        ("1-500", None),            # implausible; declines to guess
        ("", None), (None, None), ("12, 15, 18", None),
    ])
    def test_page_range_is_read_the_way_crossref_writes_it(self, page_range, expected):
        assert pdf_identity.expected_page_count(page_range) == expected

    def test_a_scan_whose_length_matches_the_record_is_accepted(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(text_pdf([""] * (52 * 16)))          # 16 pages, no text layer
        verdict = verify_pdf_identity(str(path), DOI, TITLE, pages="107-122")  # 16 pages
        assert verdict.state == VERIFIED
        assert verdict.signals == ["page-count-corroborated"]

    def test_a_scan_whose_length_contradicts_the_record_is_still_refused(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(text_pdf([""]))            # one page ...
        verdict = verify_pdf_identity(str(path), DOI, TITLE, pages="295-300")  # ... of six
        assert verdict.state == UNREADABLE
        assert not verdict.ok

    def test_a_single_page_record_cannot_corroborate_anything(self, tmp_path):
        """Crossref reports an e-locator ("e12345", "1342") as the page field
        for most modern OA journals, which reads as one page. With the slack
        that let any 1-to-3-page file vouch for itself, so a one-page publisher
        advertisement was accepted as a twenty-page article."""
        path = tmp_path / "ad.pdf"
        path.write_bytes(text_pdf([""]))
        for page_field in ("e12345", "1342", "314"):
            assert not verify_pdf_identity(
                str(path), DOI, TITLE, pages=page_field).ok, page_field

    def test_corroboration_never_rescues_a_readable_wrong_article(self, tmp_path):
        """The dangerous direction. A document that says what it is, and says
        something else, is refused however well its length happens to line up.
        """
        path = _pdf(tmp_path, "usda.pdf", [
            "Dietary Guidelines for Americans 2020-2025",
        ] + ["Consume a healthy dietary pattern that accounts for all foods and "
             "beverages within an appropriate calorie level at every stage."] * 8)
        assert verify_pdf_identity(path, DOI, TITLE, pages="1").state == WRONG

    def test_no_page_range_means_no_corroboration(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(text_pdf([""]))
        assert verify_pdf_identity(str(path), DOI, TITLE, pages=None).state == UNREADABLE


class TestTwoWeakSignalsTogether:
    """A shredded OCR title plus an exact page count is enough; neither is alone.

    Measured across 47 hand-confirmed wrong files from two corpora: the highest
    partial title match was 0.62, and the highest that ALSO matched on page
    count was 0.47. The margin is why both halves are required.
    """

    OCR = ["Journal of Affectit~e Disorders, 27 ( 1993) 35-38 JAD 00957",
           "eatures associat e attempts in rtial replication l Alec Roy",
           "Summary Clinical and symptomatic features were examined in relation "
           "to suicide atterilpts in a beterogenous group of depressed patients."]
    WANT = "Features associated with suicide attempts in depression: A partial replication"

    def test_shredded_ocr_title_plus_matching_length_is_accepted(self, tmp_path):
        # 4 pages of shredded OCR against a record that spans 4 printed pages
        # (conftest.text_pdf paginates at 52 lines; 3 x 69 = 207 lines = 4 pages).
        path = _pdf(tmp_path, "ocr.pdf", self.OCR * 69)
        verdict = verify_pdf_identity(path, "10.1016/0165-0327(93)90094-z",
                                      self.WANT, pages="35-38")
        assert verdict.state == VERIFIED
        assert verdict.signals == ["title-partial-with-page-count"]

    def test_a_partial_title_alone_is_not_enough(self, tmp_path):
        """A registry entry scored 0.62 across six pages where the article
        spans twenty-six. Without the length agreeing, it stays refused."""
        path = _pdf(tmp_path, "ocr.pdf", self.OCR * 3)
        assert not verify_pdf_identity(
            path, "10.1016/x", self.WANT, pages="35-60").ok

    def test_a_matching_length_alone_is_not_enough(self, tmp_path):
        """A one-page permissions notice standing in for a one-page 1968
        abstract agrees on length exactly, and is still somebody else's."""
        path = _pdf(tmp_path, "perms.pdf", [
            "Lippincott Journal Portfolio Author Permission Guidelines",
        ] + ["All requests to reuse your Final Published Article must be "
             "submitted through the rights and permissions portal."] * 8)
        assert not verify_pdf_identity(
            path, "10.1097/00006199-196811000-00101",
            "599. A replication of rectal thermometer placement studies",
            pages="574").ok


class TestFragmentAgainstStructuredFullText:
    """A first-page preview carries the article's real DOI and real title, so
    every identity signal passes it. Its LENGTH gives it away.

    Measured on records where a PDF and a structured copy were both retrieved:
    complete PDFs scored 0.98, 1.02 and 1.04 against the structured text, while
    a two-page Brill preview of a thirty-two-page article scored 0.07.

    This signal needs no page metadata at all, which is why it is checked
    first: it compares the document with ITSELF in another format, rather than
    with somebody's metadata about it.
    """

    def test_a_preview_is_refused_even_though_it_is_the_right_article(self, tmp_path):
        preview = _pdf(tmp_path, "preview.pdf", [TITLE, f"doi:{DOI}"] + PROSE[:2])
        verdict = verify_pdf_identity(preview, DOI, TITLE, reference_chars=60000)
        assert verdict.state == pdf_identity.TRUNCATED
        assert not verdict.ok
        assert "fragment" in verdict.reason

    def test_a_complete_pdf_passes_the_same_check(self, tmp_path):
        full = tmp_path / "full.pdf"
        full.write_bytes(text_pdf([TITLE, f"doi:{DOI}"] + PROSE * 30))
        verdict = verify_pdf_identity(str(full), DOI, TITLE, reference_chars=8000)
        assert verdict.state == VERIFIED

    def test_no_structured_copy_means_no_comparison(self, tmp_path):
        """Most records never get a structured copy; they must not all be
        refused for failing a comparison that never happened."""
        preview = _pdf(tmp_path, "preview.pdf", [TITLE, f"doi:{DOI}"] + PROSE[:2])
        assert verify_pdf_identity(preview, DOI, TITLE, reference_chars=None).ok

    def test_a_tiny_structured_copy_is_not_used_as_a_yardstick(self, tmp_path):
        """An abstract-only JATS stub is not evidence that a full PDF is short.
        Below the guard, the comparison is skipped rather than inverted."""
        pdf = _pdf(tmp_path, "ok.pdf", [TITLE, f"doi:{DOI}"] + PROSE[:2])
        assert verify_pdf_identity(pdf, DOI, TITLE, reference_chars=900).ok


def test_truncation_is_reported_as_its_own_failure(tmp_path):
    """Not "wrong article": it IS the right article, just not all of it. The
    distinction decides what the caller should do next -- keep walking the
    chain for a complete copy, rather than conclude the source was bad.
    """
    preview = _pdf(tmp_path, "preview.pdf", [TITLE, f"doi:{DOI}"] + PROSE[:2])
    verdict = verify_pdf_identity(preview, DOI, TITLE, reference_chars=60000)
    assert verdict.state == pdf_identity.TRUNCATED
    assert verdict.state != WRONG


class TestEmbeddedTitleCannotConvictOnLength:
    """S4 had zero test coverage and a rigged comparison.

    `matched_fraction` divides by the length of the ARTICLE title, so the best
    score a short `/Title` can reach is `len(embedded)/len(title)`. A
    17-character value against an 80-character title tops out at 0.21 — below
    `TITLE_BLOCK_FOREIGN` no matter what it says. That turned two of the
    commonest `/Title` values in the wild into evidence of a different paper.
    """

    @pytest.mark.parametrize("embedded", [
        "Allergy - Wiley Online Library",   # the journal, not the article
        "untitled document",               # the single commonest junk title
        "Manuscript ID ALL-2020-01234",    # a submission system's leftovers
    ])
    def test_a_short_embedded_title_is_not_evidence(self, tmp_path, monkeypatch, embedded):
        """The file may still be refused by a later branch -- what must not
        happen is S4 convicting it on a value that could never have matched.
        Asserted on the reason, because the state alone cannot tell the two
        branches apart."""
        monkeypatch.setattr(pdf_identity._Reader, "embedded_title", lambda self: embedded)
        path = _pdf(tmp_path, "x.pdf", ["Some running head"] + PROSE)
        verdict = verify_pdf_identity(path, DOI, TITLE)
        assert "calls itself" not in verdict.reason, (
            f"S4 convicted on {embedded!r}, which is too short to have matched")

    def test_a_full_length_disagreeing_title_still_convicts(self, tmp_path, monkeypatch):
        """The signal must survive: a document long enough to have matched, and
        saying something else, is still somebody else's."""
        monkeypatch.setattr(
            pdf_identity._Reader, "embedded_title",
            lambda self: "Dietary Guidelines for Americans 2020-2025, "
                         "Make Every Bite Count With the Dietary Guidelines")
        path = _pdf(tmp_path, "x.pdf", ["Some running head"] + PROSE)
        assert verify_pdf_identity(path, DOI, TITLE).state == WRONG

    def test_an_embedded_title_cannot_convict_an_illegible_scan(self, tmp_path, monkeypatch):
        """A scanner's `/Title` on a page with no readable text is not a
        confession. Before this, it jumped over the page-count rescue below."""
        monkeypatch.setattr(
            pdf_identity._Reader, "embedded_title",
            lambda self: "Scanned document from the departmental copier, batch 7")
        path = tmp_path / "scan.pdf"
        path.write_bytes(text_pdf([""] * (52 * 16)))
        verdict = verify_pdf_identity(str(path), DOI, TITLE, pages="107-122")
        assert verdict.state == VERIFIED
        assert verdict.signals == ["page-count-corroborated"]
