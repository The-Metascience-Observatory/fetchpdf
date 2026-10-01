"""Is this PDF the paper we asked for?

T2 has had an identity check since the beginning: `validate._citation_doi_mismatch`
refuses a page that declares a different article. T5 had `%PDF` and a 1 KiB
floor, which is not an identity check -- it is a file-type check wearing one.

10.1111/all.14949 is what that cost. Unpaywall's only OA copy for that record
is a landing page, so the chain scraped it; the article's own PDF was behind a
bot wall, the next candidate 404ed, and the third was a genuine
`application/pdf` -- the USDA's 164-page *Dietary Guidelines for Americans*,
which the paper CITES. It was written out as an 11-page Wiley allergy paper,
alongside the correct XML for that same record, fetched in the same walk,
without either being compared to the other.

The rule this module enforces is one the codebase already states twice, and had
never applied to a PDF:

    ONLY this paper's own material. A dataset the paper CITES belongs to
    somebody else and must not be collected.
        -- llm_agent_retrieval, on datasets

    a later, less trustworthy source cannot quietly redirect retrieval to a
    different paper.
        -- IdentifierSet.learn, on identifiers

Only VERIFIED accepts. The other states all reject, but they are kept distinct
because the *reason* has to be honest: "this is a different paper" is a bug
signal, "this document has no text layer" is a corpus-quality signal, and "no
PDF reader is installed" is a broken install. Collapsing them into one refusal
would hide all three behind whichever is most common.
"""

import os
import re
from typing import List, Optional

from ._util import matched_fraction, normalise_doi, squash
from .pdf_text import (
    engine_status,
    pdf_metadata,
    pdf_page_count,
    pdf_text,
    pdf_text_from_bytes,
)

#: Verdict states. Only the first accepts.
VERIFIED = "verified"          # positive evidence this is the requested paper
WRONG = "wrong_article"        # positive evidence it is a DIFFERENT paper
UNREADABLE = "unreadable"      # engine present, no usable text layer
NO_REFERENCE = "no_reference"  # nothing to compare against -- no DOI hit, no title
NO_ENGINE = "no_engine"        # no PDF reader; the CLI refuses to start in this state
TRUNCATED = "truncated"        # this article, but only a fragment of it

#: Pages read from the front of the document. Capped in both directions: enough
#: to clear a cover sheet, few enough that a 164-page report's own bibliography
#: cannot supply the requested DOI and fake a match. (The Dietary Guidelines PDF
#: does not cite this paper, but the general shape -- a long document that
#: happens to contain the DOI somewhere -- is exactly what an uncapped read
#: would fall for.)
#:
#: The title gets the same three pages as the DOI. A book chapter or a
#: publisher's sample deposited as a preprint opens with a cover image and a
#: contents list, and prints its own title only on page 3: 10.31234/osf.io/2tqep
#: is a cover with no text layer, then "Contents", then "This is a sample
#: chapter from <title>". Read to two pages it was refused as a different
#: document; read to three it verifies on the exact title.
DOI_PAGES = 3
TITLE_PAGES = 3

#: How much of the title must survive on the page, summed across matching runs
#: of at least a few characters -- deliberately gap-tolerant, so one substituted
#: character (a Greek letter typeset in a Latin face) cannot halve the score.
#: Measured on the record that motivated this module: the real title, broken
#: across three lines and hyphenated at the break, scores 1.00 against page 1;
#: the USDA report's title scores 0.11. Nothing lands near 0.85 by accident.
TITLE_BLOCK_MIN = 0.85

#: A PDF shorter than this fraction of the pages the record spans is a preview
#: or a supplement, not the article -- even though it carries the article's own
#: DOI and title, which is exactly why the identity signals pass it.
#:
#: Measured over 456 verified-correct inbox PDFs with a usable page range: the
#: ratios run 0.22, 0.31, 0.33, 0.33, 0.42 and then jump to 0.73, so 0.5 sits in
#: open space rather than on a slope. All five below it were genuinely not the
#: article -- two publisher first-page previews cut off mid-sentence, two figure
#: supplements, and an open-practices disclosure form ending in "Signature:".
TRUNCATION_RATIO = 0.5

#: A PDF holding less than this share of the text the SAME record's structured
#: full text holds is a fragment of it. Independent of page metadata entirely,
#: which is what makes it the better of the two truncation signals when both
#: apply. Measured on records where a PDF and a structured copy were both
#: retrieved: complete PDFs scored 0.98, 1.02, 1.04, and a two-page Brill
#: preview of a thirty-two-page article scored 0.07.
FRAGMENT_TEXT_RATIO = 0.5

#: How far back from the end of the file to look for the %%EOF trailer. Every
#: conforming PDF ends with one, and an incremental update leaves several, so
#: this searches a window rather than comparing the final bytes.
#:
#: Measured over 381 real corpus PDFs above the 1 KiB floor (cerebrolysin, the
#: FMT working corpus, the Litvak benchmark): the marker sits 6 bytes from the
#: end at the median, 7 at the 95th percentile, and 7 at the worst case. 4 KiB
#: is roughly 580x that worst case, which is the room a server needs to append
#: junk after the trailer without costing us a correct paper. On the same 381
#: the check has a 0.00% false-positive rate.
TRAILER_TAIL_BYTES = 4096

#: Below this many expected pages the ratio is too noisy to act on: a 3-page
#: record delivered as 1 page may be an abstract-only DOI, a letter, or correct.
TRUNCATION_MIN_PAGES = 4

#: A title match too weak to stand alone, but not nothing. Combined with an
#: independent structural signal -- the page count -- it is enough. Both halves
#: are required precisely because either alone is unsafe: measured across 47
#: hand-confirmed wrong files from two corpora, the highest partial title match
#: was 0.62 and the highest that ALSO matched on page count was 0.47.
TITLE_BLOCK_PARTIAL = 0.55

#: Below this, the document's *embedded* title is not merely unmatched -- it is
#: affirmatively about something else, which is what lets us say WRONG rather
#: than "could not tell".
TITLE_BLOCK_FOREIGN = 0.30

#: A front-matter read is short by nature, so the whole-document floor in
#: pdf_text would misreport a legitimately brief title page as "no text layer".
MIN_FRONT_CHARS = 200

#: Below this much squashed text across the front pages, the document has not
#: said enough for "it does not match" to mean "it is a different paper".
LEGIBLE_FRONT_CHARS = 400

#: A cover sheet, a licence page or a trailing blank can legitimately pad a
#: scan beyond its printed page range. Fewer pages than the range, or many
#: more, is a different document.
PAGE_COUNT_SLACK = 2

#: Corroboration needs a page range long enough to be a fingerprint. See
#: `_page_count_corroborates`.
PAGE_COUNT_MIN_SPAN = 2

_PAGE_RANGE_RE = re.compile(r"^\s*(?:[A-Za-z]*)(\d+)\s*(?:[-–—]\s*(?:[A-Za-z]*)(\d+))?\s*$")

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)

#: Embedded titles that carry no identity: a converter's leftovers, a filename,
#: a template default. Present in a large minority of real PDFs, and treating
#: them as "declares a different article" would reject correct files.
_GENERIC_TITLE_RE = re.compile(
    r"^(untitled|microsoft word|microsoft powerpoint|document|manuscript"
    r"|paper|article|print|pdf|slide|draft|final|revised|proof|template"
    r"|no title|title)?[\s\-_.]*(document|file|manuscript|copy|version|\d+)?$",
    re.IGNORECASE,
)

#: A scanner or layout tool leaving its output filename in /Title. Says nothing
#: about identity, and reading it as "declares a different article" rejected a
#: correct 1995 scan whose embedded title was "1556.tif".
_FILENAME_TITLE_RE = re.compile(
    r"^[\w \-.]{1,60}\.(tif|tiff|pdf|doc|docx|rtf|qxd|indd|eps|ps|xps|pmd|cdr)$",
    re.IGNORECASE,
)


class IdentityVerdict:
    """A state, a sentence explaining it, and the signals that fired."""

    def __init__(self, state: str, reason: str, signals: Optional[List[str]] = None):
        self.state = state
        self.reason = reason
        self.signals = signals or []

    @property
    def ok(self) -> bool:
        return self.state == VERIFIED

    def __repr__(self) -> str:
        return "IdentityVerdict({!r}, {!r})".format(self.state, self.reason)


def article_title(ids) -> str:
    """The requested article's title from what resolution already fetched.

    Deliberately not a network call. `BatchResolver.crossref` memoizes the whole
    Crossref message and every DOI record passes through a T1 source that asks
    for it, so by the time a T5 artifact needs judging the title is already in
    hand. Unpaywall's payload carries a second free copy.
    """
    memo = getattr(ids, "memo", None) or {}
    titles = (memo.get("crossref") or {}).get("title") or []
    if titles:
        return str(titles[0])
    return str((memo.get("unpaywall") or {}).get("title") or "")


def article_pages(ids) -> str:
    """The record's printed page range, from the memoised Crossref payload."""
    memo = getattr(ids, "memo", None) or {}
    return str((memo.get("crossref") or {}).get("page") or "")


def article_arxiv_id(ids) -> str:
    """The record's arXiv id, which resolution derives from the DOI for free."""
    from .resolve import arxiv_id_from_doi

    return str(
        getattr(ids, "arxiv_id", None)
        or arxiv_id_from_doi(getattr(ids, "doi", None))
        or ""
    )


def verify_pdf_identity(source, doi: Optional[str] = None,
                        title: Optional[str] = None,
                        pages: Optional[str] = None,
                        reference_chars: Optional[int] = None,
                        arxiv_id: Optional[str] = None) -> IdentityVerdict:
    """Whether the PDF at `source` (a path, or bytes) is this DOI's paper.

    Signals are checked cheapest-first and the first hit wins. Any one of them
    is enough -- they are alternatives, not a checklist, because the documents
    that legitimately fail one are common: an accepted manuscript carries no
    publisher DOI, and a scanned-then-OCRed page may carry no clean title.
    """
    if engine_status()[0] is None:
        return IdentityVerdict(
            NO_ENGINE,
            "no PDF text engine, so this PDF cannot be checked against the "
            "record it was fetched for",
        )

    reader = _Reader(source)
    truncated = _truncation_reason(reader, pages, reference_chars)

    text = reader.text(DOI_PAGES)
    if not text:
        corroborated = _page_count_corroborates(reader, pages)
        if corroborated:
            return truncated or IdentityVerdict(
                VERIFIED, corroborated, ["page-count-corroborated"])
        # A file that stops mid-stream is usually one the engine also cannot
        # parse, and both states refuse -- but the REASONS are not equally
        # useful. "The download stopped early" says re-fetch; "no readable text
        # layer" says this is a scan and belongs to OCR. Reporting the second
        # when we can see the first hides the bug signal behind the
        # corpus-quality one, which is the collapse this module's own docstring
        # refuses for the other three states.
        return truncated or IdentityVerdict(
            UNREADABLE,
            "PDF has no readable text layer, so it cannot be checked against "
            "{}".format(doi or "the requested record"),
        )

    squashed_page = squash(text)

    # S1: the requested DOI, printed on the paper. True of essentially every
    # version of record, and squashing means a line break inside it is harmless.
    if doi:
        wanted = squash(normalise_doi(doi))
        if wanted and wanted in squashed_page:
            return truncated or IdentityVerdict(
                VERIFIED, "requested DOI found in the PDF text", ["doi-in-text"])

    # S1b: the arXiv id, stamped down the margin of every PDF arXiv serves. It
    # is S1 for a class of record that S1 cannot reach and neither can S2-S4:
    # arXiv mints 10.48550/arXiv.* DOIs at DataCite and prints the id, never the
    # DOI, while Crossref -- the only place `article_title` and `article_pages`
    # read from -- has no record of a DataCite DOI at all, so no title and no
    # page range are available either. Every arXiv PDF therefore reached the end
    # of this function with nothing checked and came back NO_REFERENCE, which the
    # engine treats as a refusal: `--get-xml-or-html` on 10.48550/arXiv.2605.04265
    # fetched the PDF, threw it away, and kept only the HTML, contradicting the
    # flag it was asked for.
    #
    # The "arxiv" prefix is required, not decoration. Squashing strips the dot,
    # so the id alone is nine digits ("260504265") and would match a phone
    # number, a grant number or an accession in the front matter; "arXiv:" in
    # front of it is what makes the match mean something. The version suffix is
    # not compared -- v1 and v3 of a paper are the same paper.
    if arxiv_id:
        unversioned = re.sub(r"v\d+$", "", str(arxiv_id).strip())
        stamped = "arxiv" + squash(unversioned)
        if stamped != "arxiv" and stamped in squashed_page:
            return truncated or IdentityVerdict(
                VERIFIED, "arXiv id {} stamped on the PDF".format(unversioned),
                ["arxiv-id-in-text"])

    # S2/S3: the title, on the page. This is what carries accepted manuscripts
    # (nihms-*.pdf), which print the title and not the publisher's DOI.
    if title:
        front = reader.text(TITLE_PAGES) or text
        squashed_front = squash(front)
        wanted_title = squash(title)
        if wanted_title and wanted_title in squashed_front:
            return truncated or IdentityVerdict(
                VERIFIED, "article title found on the front pages", ["title-on-page"])
        fraction = matched_fraction(wanted_title, squashed_front)
        if fraction >= TITLE_BLOCK_MIN:
            return truncated or IdentityVerdict(
                VERIFIED,
                "article title matches the front pages ({:.0%} of it)".format(fraction),
                ["title-near-match"],
            )
        # Two weak signals, independent of each other. A badly OCRed scan
        # shreds its own title -- a 1993 Journal of Affective Disorders page
        # extracts as "eatures associat e attempts in rtial replication",
        # scoring 0.62 against a title that is plainly there -- but it cannot
        # also fake being exactly as long as the record it claims to be. Wrong
        # documents fail one or both: a publisher's permissions page is one
        # page like the 1968 abstract it replaced, but scores 0.45, and a
        # registry entry scores 0.62 across six pages where the article spans
        # twenty-six.
        if fraction >= TITLE_BLOCK_PARTIAL and _page_count_corroborates(reader, pages):
            return truncated or IdentityVerdict(
                VERIFIED,
                "article title partly legible ({:.0%}) and the PDF is exactly as "
                "long as this record ({})".format(fraction, pages),
                ["title-partial-with-page-count"],
            )

    # S4: the title the document claims for itself, when its text layer did not
    # carry one legibly.
    embedded = reader.embedded_title()
    if title and embedded and not _is_generic_title(embedded):
        wanted_title = squash(title)
        squashed_embedded = squash(embedded)
        embedded_fraction = matched_fraction(wanted_title, squashed_embedded)
        if embedded_fraction >= TITLE_BLOCK_MIN:
            return truncated or IdentityVerdict(
                VERIFIED, "embedded PDF title matches the article title",
                ["title-in-metadata"])
        # A SHORT embedded title cannot disagree with a long one, it can only
        # fail to contain it. `matched_fraction` divides by the length of the
        # article title, so the best score a 17-character `/Title` can reach
        # against an 80-character title is 0.21 -- below the threshold no matter
        # what it says. Convicting on that made "Allergy - Wiley Online Library"
        # and "untitled document", two of the commonest `/Title` values in the
        # wild, into evidence of a different paper. So a title that could never
        # have cleared the bar is treated as saying nothing, and only a title
        # long enough to have matched is allowed to contradict.
        could_have_matched = (
            squashed_embedded
            and len(squashed_embedded) >= len(wanted_title) * TITLE_BLOCK_MIN
        )
        if (could_have_matched and embedded_fraction < TITLE_BLOCK_FOREIGN
                and len(squashed_page) >= LEGIBLE_FRONT_CHARS):
            return IdentityVerdict(
                WRONG,
                'PDF is a different document: it calls itself "{}", not "{}"'.format(
                    _clip(embedded), _clip(title)),
            )

    # Nothing matched. There used to be a branch here convicting a PDF that
    # printed some OTHER DOI in its front matter. It is gone, and the
    # measurements are why: across 36 known-bad files it convicted ZERO, and
    # across 706 known-good files it convicted THREE -- every one a published
    # paper printing its own preprint's DOI ("previously posted at medRxiv"),
    # which is the same work. It also convicted every document when `doi` was
    # None, because the guards were written `if wanted and ...` and an empty
    # `wanted` makes every DOI on the page foreign.
    #
    # Nothing was lost by removing it. A legible wrong PDF with a known title
    # still reaches WRONG through the branch below; with no title the honest
    # answer is NO_REFERENCE, because nothing was checked. Removing it also lets
    # a barely-legible scan reach the page-count rescue further down, which the
    # early return used to jump over.
    if title:
        # A page carrying almost no machine-readable text cannot be convicted of
        # being a different article -- it has not said anything. Scanned papers
        # routinely extract to nothing but a library's download stamp ("by guest
        # on June 5, 2016 ... Downloaded from"), which is text, but not text that
        # could ever have carried a title. Measured on this corpus: two JNEN
        # scans from 1992 and 1995, both the correct article, both reported as
        # somebody else's until this branch existed. The file is refused either
        # way; what changes is whether the run reports a retrieval bug or a
        # document that was never machine-readable, and those send whoever reads
        # the report to two very different places.
        if len(squashed_page) < LEGIBLE_FRONT_CHARS:
            corroborated = _page_count_corroborates(reader, pages)
            if corroborated:
                return truncated or IdentityVerdict(
                VERIFIED, corroborated, ["page-count-corroborated"])
            return IdentityVerdict(
                UNREADABLE,
                "PDF has almost no readable text ({} characters over {} pages), "
                "so it cannot be checked against {}".format(
                    len(squashed_page), DOI_PAGES, doi or "the requested record"),
            )
        return IdentityVerdict(
            WRONG,
            'PDF does not contain the requested DOI or the title "{}"'.format(_clip(title)),
        )
    return IdentityVerdict(
        NO_REFERENCE,
        "no title known for {} and its DOI is not printed in the PDF, so there "
        "is nothing to check the file against".format(doi or "this record"),
    )


def expected_page_count(page_range: Optional[str]) -> Optional[int]:
    """How many printed pages "107-122" implies. None when it implies nothing.

    Crossref's `page` is free text: "107-122", "314", "e12345", "S1-S8", and
    occasionally a comma-separated mess. Only the shapes that clearly denote a
    single page or a simple range are used; anything else declines to guess,
    because a wrong expectation here refuses a correct file.
    """
    if not page_range:
        return None
    matched = _PAGE_RANGE_RE.match(str(page_range))
    if not matched:
        return None
    first = int(matched.group(1))
    if matched.group(2) is None:
        return 1
    last = int(matched.group(2))
    # "1049-58" is 1049-1058, not a negative range.
    if last < first:
        digits = len(str(last))
        last = int(str(first)[:-digits] + str(last)) if digits < len(str(first)) else last
    span = last - first + 1
    return span if 1 <= span <= 400 else None


def _truncation_reason(reader, page_range: Optional[str],
                       reference_chars: Optional[int] = None) -> Optional[IdentityVerdict]:
    """A verdict when the file is only a fragment of the record, else None.

    A different failure from a wrong document, and invisible to every identity
    signal: a publisher's first-page preview and an article's own supplementary
    figures both carry the article's real DOI and real title, so they pass. What
    gives them away is length. Two of thirteen replacement PDFs fetched during
    the corpus repair were two-page previews of a seventeen- and a thirty-two-
    page article, each cut off mid-sentence, and both would have been stored as
    the paper.

    Only a SHORTFALL counts. An accepted manuscript is routinely longer than its
    typeset page range -- one here runs 57 double-spaced pages against a printed
    36 -- and that is a complete article, not a defect.
    """
    # Signal 0: the file's own trailer. The only INTRINSIC signal of the three,
    # and the reason it goes first: signals 1 and 2 are both comparative and
    # both return None when their reference is missing -- a record with no
    # structured copy (which this module notes is most of them) and no parseable
    # page range receives no truncation check at all.
    #
    # DEMONSTRATED on 10.1001/jamanetworkopen.2023.37679: cut to 40% of its
    # bytes, with neither reference supplied, it verifies as VERIFIED. The DOI
    # is still in the front matter, so every identity signal passes it, and both
    # truncation signals decline for want of something to compare against. A
    # download that stopped early is exactly the file this module exists to
    # refuse, and it was the one shape nothing looked at.
    #
    # It is also invisible to `fetchpdf-verify`, which confirms the bytes on
    # disk are the bytes that arrived -- and they are. The file is not corrupt
    # in transit; it is short.
    if not reader.has_trailer():
        return IdentityVerdict(
            TRUNCATED,
            "PDF has no %%EOF trailer in its last {} bytes -- the download "
            "stopped before the end of the file".format(TRAILER_TAIL_BYTES),
        )

    # Signal 1: the same record's structured full text, when the walk already
    # has it. The strongest of the two, because it compares the document with
    # ITSELF in another format rather than with metadata about it.
    if reference_chars and reference_chars > 2000:
        body = reader.text(None)
        if body is not None and len(body) < reference_chars * FRAGMENT_TEXT_RATIO:
            return IdentityVerdict(
                TRUNCATED,
                "PDF holds {} characters against {} in this record's structured "
                "full text -- a fragment of the article, not the article".format(
                    len(body), reference_chars),
            )

    # Signal 2: the printed page range. Available far more often, since most
    # records never get a structured copy at all.
    expected = expected_page_count(page_range)
    if not expected or expected < TRUNCATION_MIN_PAGES:
        return None
    actual = reader.page_count()
    if not actual:
        return None
    if actual >= expected * TRUNCATION_RATIO:
        return None
    return IdentityVerdict(
        TRUNCATED,
        "PDF is only {} of the {} pages this record spans ({}) -- a preview or "
        "a supplement, not the article".format(actual, expected, page_range),
    )


def _page_count_corroborates(reader, page_range: Optional[str]) -> Optional[str]:
    """A reason string when the PDF's page count matches the record's, else None.

    The only identity evidence an image-only scan can offer, and it is real
    evidence: it is a property of the FILE, not of our metadata about the DOI
    we asked for. That distinction is why this is not "old papers are probably
    fine" -- a wrong file fetched for a 1995 record is still wrong, and this
    still catches it. Measured on the corpus: four scanned papers from 1992-95
    match their printed page ranges exactly, while the publisher advertisement
    standing in for a six-page 2023 article is one page long and is still
    refused.
    """
    expected = expected_page_count(page_range)
    # A single expected page is not evidence. Crossref reports an e-locator
    # ("e12345", "1342") as the page field for most modern OA journals, and
    # `expected_page_count` reads that as one page -- so with PAGE_COUNT_SLACK
    # any 1-to-3-page file would corroborate, and a one-page publisher
    # advertisement would be accepted as a twenty-page article. Corroboration
    # only means something when the record spans a length worth matching.
    if not expected or expected < PAGE_COUNT_MIN_SPAN:
        return None
    actual = reader.page_count()
    if not actual:
        return None
    if expected <= actual <= expected + PAGE_COUNT_SLACK:
        return ("PDF has no readable text, but its {} pages match the {} pages "
                "this record spans ({})".format(actual, expected, page_range))
    return None


class _Reader:
    """One document, read once per question asked of it.

    A single `verify_pdf_identity` used to re-open and re-parse the same file up
    to seven times -- a 3-page read, a 2-page read, a whole-document read, the
    metadata, and the page count from as many as three call sites. On the pypdf
    path each of those slurps the whole file into a fresh BytesIO and re-parses
    the xref, so the cost is real and lands hardest on exactly the documents
    this module exists to catch: the oversized ones.
    """

    def __init__(self, source):
        self.source = source
        self._text = {}
        self._pages = _UNSET
        self._metadata = None
        self._trailer = _UNSET

    def text(self, pages: Optional[int]) -> Optional[str]:
        if pages not in self._text:
            self._text[pages] = _read_text(self.source, pages)
        return self._text[pages]

    def page_count(self) -> Optional[int]:
        if self._pages is _UNSET:
            self._pages = pdf_page_count(self.source)
        return self._pages

    def has_trailer(self) -> bool:
        """Does %%EOF appear in the last `TRAILER_TAIL_BYTES` of the file?

        Reads the tail only, from bytes or from a path, and never through the
        PDF engine: a truncated file is precisely the case where the engine may
        fail to parse, and this question has to be answerable when parsing is
        the thing that broke. Unreadable tail counts as PRESENT -- an I/O error
        is our problem, and it must not be spelled the same way as a short
        download.
        """
        if self._trailer is _UNSET:
            self._trailer = self._read_trailer()
        return self._trailer

    def _read_trailer(self) -> bool:
        try:
            if isinstance(self.source, (bytes, bytearray)):
                tail = bytes(self.source)[-TRAILER_TAIL_BYTES:]
            else:
                size = os.path.getsize(self.source)
                with open(self.source, "rb") as handle:
                    handle.seek(max(0, size - TRAILER_TAIL_BYTES))
                    tail = handle.read()
        except (OSError, TypeError):
            return True          # our failure, not the file's
        return b"%%EOF" in tail

    def metadata(self) -> dict:
        if self._metadata is None:
            self._metadata = pdf_metadata(self.source) or {}
        return self._metadata

    def embedded_title(self) -> str:
        metadata = self.metadata()
        for key in ("title", "Title", "/Title", "dc:title"):
            value = metadata.get(key)
            if value:
                return str(value)
        return ""


_UNSET = object()


def _read_text(source, pages: Optional[int]) -> Optional[str]:
    """Text from the front `pages` (None = the whole document)."""
    min_chars = 1 if pages is None else MIN_FRONT_CHARS
    if isinstance(source, (bytes, bytearray)):
        return pdf_text_from_bytes(bytes(source), max_pages=pages, min_chars=min_chars)
    return pdf_text(source, max_pages=pages, min_chars=min_chars)


def _is_generic_title(embedded: str) -> bool:
    stripped = (embedded or "").strip()
    return bool(_GENERIC_TITLE_RE.match(stripped) or _FILENAME_TITLE_RE.match(stripped))


def _clip(text: str, limit: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
