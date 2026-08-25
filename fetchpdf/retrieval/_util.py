"""Small helpers that more than one module in this package needs.

Each of these existed in two to four copies before. That is mostly harmless
duplication -- except for `localname`, where it was not: both copies need the
non-string guard for lxml comment and processing-instruction nodes, whose `.tag`
is a *callable* rather than a name. A copy written without that guard raised
TypeError on every real publisher page, which is how T2 validation was broken
until an integration run caught it. One definition, one guard.
"""

import html as _html
import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

#: Two titles for the same work are the same title at 0.93 and are different
#: works well below it. The margin is not delicate: a Dataverse deposit and its
#: own article score 0.99, an unrelated one 0.12. Anything in between is not a
#: near-miss worth arguing over. `supplement_graph` still carries its own copy
#: of this number and its own `_squash`; they predate these and have not been
#: migrated, so the two are free to drift. Anyone touching either should move
#: that caller over rather than adding a third.
TITLE_MATCH_RATIO = 0.93


def localname(tag) -> str:
    """An element tag without its namespace.

    TEI is namespaced, JATS usually is not, and lxml hands back comments and
    processing instructions whose `.tag` is a function -- hence the isinstance
    check, which is the whole reason this lives in one place.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def as_int(value, default: int = 0) -> int:
    """int(value) for values that arrive from JSON as strings, or as None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def iso(timestamp: float) -> str:
    """A UTC ISO-8601 stamp. Used in every audit record this package writes."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unlink(path: Optional[str]) -> None:
    """Delete a path, tolerating its absence. Cleanup must never raise."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def squash(text: str) -> str:
    """Lowercased alphanumerics only, so punctuation and casing differences
    between a deposit title and a journal title cannot break the match.

    Dropping *everything* non-alphanumeric is also what makes this usable
    against text extracted from a PDF page: a title broken across a line, or
    hyphenated at the break, squashes to the same string as the title it came
    from. That is load-bearing for `pdf_identity`, not incidental.

    The NFKD pass is load-bearing for the same caller and for a reason that is
    invisible until it bites: PDF text extraction hands back the *ligature*
    glyphs a typesetter used, so a page reading "identification" arrives as
    "identiﬁcation" -- one character, U+FB01. Without decomposition that title
    silently fails to match itself, and under reject-always a silent non-match
    is a deleted file. Decomposing first turns the ligature back into "fi". It
    also strips accents to their base letters, so "café" and "cafe" agree.

    Kept as `str.isalnum()` rather than `[^a-z0-9]`, which is what this was:
    that character class erases every non-Latin script outright. A Cyrillic,
    Greek or CJK title squashed to the empty string, and an empty title matches
    nothing -- so under reject-always every paper with a non-Latin title and no
    DOI printed on the page would have been deleted as somebody else's.
    """
    # Unescaped first because titles arrive from Crossref carrying HTML
    # entities -- a real title reads "salience &amp; message framing" in the
    # metadata and "salience & message framing" on the page, and the stray
    # "amp" splits the match straight down the middle.
    unescaped = _html.unescape(text or "")
    decomposed = unicodedata.normalize("NFKD", unescaped).lower()
    return "".join(ch for ch in decomposed if ch.isalnum())


def matched_fraction(needle: str, haystack: str, min_block: int = 4) -> float:
    """How much of `needle` appears in `haystack`, across ALL matching runs.

    The right question to ask when matching a title against a page of extracted
    text, and `SequenceMatcher.ratio()` is the wrong one: ratio is symmetric and
    length-sensitive, so a 90-character title compared against 4,000 characters
    of page scores near zero however perfectly the title appears on it.

    Deliberately gap-tolerant. A single substituted character in the middle of a
    title halves the longest contiguous run while leaving the title obviously
    intact -- measured on a real record, "V717F β-Amyloid Precursor Protein"
    typeset as "V717F b-Amyloid Precursor Protein" scores 0.62 by longest run
    and 0.99 by matched total. Greek letters set in a Latin face are pervasive
    in the life sciences, so that is not an edge case, it is Tuesday.

    `min_block` is what keeps this honest: without it, a long page of prose
    supplies a matching character somewhere for nearly every letter of any
    title, and everything scores high. Only runs of at least `min_block`
    characters count, so the score still means "this page contains these
    phrases", not "this page contains these letters".

    Both sides are assumed already squashed.
    """
    import difflib

    if not needle or not haystack:
        return 0.0
    matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    total = sum(b.size for b in matcher.get_matching_blocks() if b.size >= min_block)
    return min(1.0, total / float(len(needle)))


def normalise_doi(doi: str) -> str:
    """A DOI stripped of the several prefixes it arrives wearing."""
    doi = (doi or "").strip().lower()
    for prefix in ("doi:", "https://doi.org/", "http://doi.org/", "doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "dx.doi.org/"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi


def titles_match(a: str, b: str, ratio: float = TITLE_MATCH_RATIO) -> bool:
    """Whether two titles name the same work.

    Containment first -- it is cheap and it is the common case, since one side
    is routinely a subtitle-trimmed or prefix-wrapped form of the other. The
    difflib ratio is only for the spelling-variant tail that containment drops.
    """
    import difflib

    left, right = squash(a), squash(b)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= ratio
