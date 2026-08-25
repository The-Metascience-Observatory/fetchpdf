"""Text out of a PDF, for the code that has to read the paper itself.

Two consumers, one seam: `fulltext_scan` looks for deposit URLs in the prose,
and the retrieval agent needs the same prose as its brief. Both were blind to
PDF-only records -- measured 2026-08-22 across four corpora, **606 papers have
a PDF and no XML or HTML at all** (cerebrolysin 212, masliah 369, nina_mazar
25). Those were never scanned, and a record that was never scanned was being
reported as one where nothing was found.

OPTIONAL BY CONTRACT, AND STILL IS. Neither engine raises here, and a missing
one degrades to "no text" with a named reason -- never an exception, never a
failed record. `engine_status()` says which of the three states holds so a
caller can tell "this machine has no PDF reader" from "this PDF has no text
layer", which are different facts about different things.

What changed is the POLICY LAYER above it, not this contract. A third consumer,
`pdf_identity`, uses this module to prove a retrieved PDF is the paper that was
asked for; with no engine it cannot prove anything, and under reject-always
that would silently discard every PDF in a run. So `pypdf` moved into the base
dependencies and the CLI refuses to start without an engine -- a RUN-level
error answered once at startup, not a per-record one answered 5,000 times. The
module keeps degrading; the caller owns the policy. Do not make this raise.

PyMuPDF first, pypdf second. PyMuPDF is faster and keeps reading order better;
pypdf is the fallback already used by `corresponding.from_pdf`, so a machine
set up for that keeps working here.
"""

import os
from typing import Optional, Tuple

#: Engines, in preference order. Names are what `engine_status` reports.
ENGINE_PYMUPDF = "pymupdf"
ENGINE_PYPDF = "pypdf"

#: Reported when neither engine imports. The message names the install, because
#: the alternative is a silent zero-yield scan that reads like a clean corpus.
MISSING_DEP_MSG = ("no PDF text engine, so no retrieved PDF can be checked "
                   "against the record it was fetched for; install pypdf, or "
                   "fetchpdf[text] for the faster PyMuPDF reader")

#: A page cap for callers that only need the front or back matter. None reads
#: the whole document, which is what the deposit scan wants -- a data
#: availability statement lives in the back matter, not on page one.
DEFAULT_MAX_PAGES = None

#: Below this many characters we treat the read as "no text layer" rather than
#: as text. A scanned page yields a handful of stray glyphs, not prose, and
#: calling that a successful read invites the caller to conclude the paper
#: named no deposit when nothing was ever legible.
MIN_USEFUL_CHARS = 500


def _pymupdf():
    """PyMuPDF if installed, else None. Optional extra: fetchpdf[text]."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz
            return fitz
        except ImportError:
            return None


def _pypdf():
    """pypdf if installed, else None."""
    try:
        from pypdf import PdfReader
        return PdfReader
    except ImportError:
        return None


def engine_status() -> Tuple[Optional[str], str]:
    """`(engine_name, detail)`. `engine_name` is None when none is installed."""
    if _pymupdf() is not None:
        return ENGINE_PYMUPDF, "PyMuPDF"
    if _pypdf() is not None:
        return ENGINE_PYPDF, "pypdf"
    return None, MISSING_DEP_MSG


def pdf_text(path: str, max_pages: Optional[int] = DEFAULT_MAX_PAGES,
             min_chars: int = MIN_USEFUL_CHARS) -> Optional[str]:
    """The document's text, or None when it could not be read as text.

    None covers every failure the same way on purpose -- no engine, an
    unreadable file, an encrypted document, a page tree of images. The caller
    that needs to tell those apart asks `engine_status()` first; the caller
    that just wants prose gets "there is none" without a traceback.

    `min_chars` exists because MIN_USEFUL_CHARS is calibrated for the whole-
    document deposit scan, where a few hundred stray glyphs mean "scanned, no
    text layer". A caller reading only the first two pages of a short paper can
    fall under that floor with perfectly good text, and reporting that as "no
    text layer" would, under reject-always, delete a correct PDF. Identity
    verification passes a lower floor; the default is unchanged, so
    `fulltext_scan` is untouched.
    """
    if not path or not os.path.exists(path):
        return None
    return _extract(path, max_pages, min_chars)


def pdf_text_from_bytes(content: bytes,
                        max_pages: Optional[int] = DEFAULT_MAX_PAGES,
                        min_chars: int = MIN_USEFUL_CHARS) -> Optional[str]:
    """`pdf_text` for a document that is still in memory.

    The tiered engine judges an artifact before anything has been written to
    disk -- deliberately, so a T5 artifact that fails validation leaves no file
    behind -- so it has bytes where the scan has a path.
    """
    if not content:
        return None
    return _extract(content, max_pages, min_chars)


def pdf_page_count(source) -> Optional[int]:
    """How many pages the document has, or None if it cannot be opened.

    Structural, not textual: this works on an image-only scan, which is the
    whole reason it exists. See `pdf_identity`, where a page count is the only
    identity evidence a scanned paper can offer.
    """
    module = _pymupdf()
    if module is not None:
        document = None
        try:
            document = _open_pymupdf(module, source)
            return int(document.page_count)
        except Exception:
            pass
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass

    reader_cls = _pypdf()
    if reader_cls is not None:
        try:
            return len(reader_cls(_as_pypdf_source(source)).pages)
        except Exception:
            return None
    return None


def pdf_metadata(source) -> dict:
    """The document's Info/XMP dictionary, or {} for anything unreadable.

    Accepts a path or bytes. Only the embedded `Title` is used today, as the
    last identity signal when a PDF's text layer is too thin to carry one.
    """
    module = _pymupdf()
    if module is not None:
        document = None
        try:
            document = _open_pymupdf(module, source)
            return dict(document.metadata or {})
        except Exception:
            pass
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass

    reader_cls = _pypdf()
    if reader_cls is not None:
        try:
            reader = reader_cls(_as_pypdf_source(source))
            return {str(k).lstrip("/").lower(): str(v)
                    for k, v in (reader.metadata or {}).items()}
        except Exception:
            return {}
    return {}


def _open_pymupdf(module, source):
    if isinstance(source, (bytes, bytearray)):
        return module.open(stream=bytes(source), filetype="pdf")
    return module.open(source)


def _as_pypdf_source(source):
    if isinstance(source, (bytes, bytearray)):
        import io
        return io.BytesIO(bytes(source))
    return source


def _extract(source, max_pages: Optional[int], min_chars: int) -> Optional[str]:
    """PyMuPDF first, pypdf second, over either a path or bytes."""
    module = _pymupdf()
    if module is not None:
        text = _read_pymupdf(module, source, max_pages, min_chars)
        if text is not None:
            return text

    reader = _pypdf()
    if reader is not None:
        return _read_pypdf(reader, source, max_pages, min_chars)

    return None


def _read_pymupdf(module, source, max_pages: Optional[int],
                  min_chars: int = MIN_USEFUL_CHARS) -> Optional[str]:
    document = None
    try:
        document = _open_pymupdf(module, source)
        pages = list(document)
        if max_pages is not None:
            pages = pages[:max_pages]
        text = "\n".join(page.get_text() or "" for page in pages)
    except Exception:
        # A corrupt or encrypted PDF is not this module's problem to raise on;
        # the pypdf fallback gets its turn, and then the caller gets None.
        return None
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
    return text if len(text.strip()) >= min_chars else None


def _read_pypdf(reader_cls, source, max_pages: Optional[int],
                min_chars: int = MIN_USEFUL_CHARS) -> Optional[str]:
    try:
        reader = reader_cls(_as_pypdf_source(source))
        pages = reader.pages if max_pages is None else reader.pages[:max_pages]
        text = "\n".join((page.extract_text() or "") for page in pages)
    except Exception:
        return None
    return text if len(text.strip()) >= min_chars else None
