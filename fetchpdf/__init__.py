"""
fetchpdf: A comprehensive tool to download PDFs from DOIs using multiple sources.

This package provides functions to download academic papers (PDFs) from DOIs using
multiple fallback sources including OpenAlex, Unpaywall, PubMed Central, Crossref,
Europe PMC, Semantic Scholar
"""

from .fetchpdf import fetch_pdf, batch_fetch_pdfs
from .fetch_metadata_from_doi import fetch_metadata_from_doi

# Deprecated alias. `fetch_pdf_from_doi` was the old name for both this function
# and the module it lives in; both are retired in favour of `fetch_pdf` /
# `fetchpdf.fetchpdf`. Kept so existing callers keep working -- there is no
# behavioural difference, it is the same object.
fetch_pdf_from_doi = fetch_pdf

__version__ = "0.1.2"
__all__ = [
    "fetch_pdf",
    "fetch_pdf_from_doi",  # deprecated alias for fetch_pdf
    "batch_fetch_pdfs",
    "fetch_metadata_from_doi",
    "pull_supplementary_for",
]


def pull_supplementary_for(*args, **kwargs):
    """Fetch every supplementary file for one record, beside its main artifact.

    A separate call rather than a keyword on fetch_pdf_from_doi, deliberately: the
    tiered engine re-enters that function at T5 with a temporary save_path, so a
    flag threaded through it would fire on the re-entry and write supplementary
    siblings next to a file that is about to be deleted. The two-call shape makes
    that structurally impossible.

        from fetchpdf import fetch_pdf_from_doi, pull_supplementary_for

        path = fetch_pdf_from_doi("10.1371/journal.pone.0000308", "out/paper.pdf")
        summary = pull_supplementary_for("10.1371/journal.pone.0000308",
                                         doi="10.1371/journal.pone.0000308",
                                         save_path="out/paper.pdf")

    Imported lazily so the retrieval package is untouched unless it is used. See
    fetchpdf.retrieval.supplementary.pull_for_record for the full signature.
    """
    from .retrieval.supplementary import pull_for_record

    return pull_for_record(*args, **kwargs)
