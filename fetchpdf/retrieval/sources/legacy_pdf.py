"""T5: the existing PDF chain, called as a single source.

Deliberately not reimplemented. The chain in fetchpdf is ~1000 lines
of accumulated publisher-specific knowledge -- Wiley rendered PDFs, the Crossref
chooser page, PsychArchives bitstreams, the eScholarship LinkOut route, session
cookies, SSL fallbacks and the Playwright paths. Forking it to slot into a tier would duplicate every one of
those behaviours and let the two copies drift.

So T5 is one call back into it with the tiered flags off. Its retries, backoff,
rate limiting and source tracking are preserved by construction rather than by
being carefully re-derived here.

It writes to a temporary path in the destination directory rather than to the
caller's save_path, because the engine has not decided to accept anything yet --
a T5 artifact that fails classification must leave no file behind.
"""

import os
import tempfile
from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier


def fetch_via_legacy_chain(ids, ctx) -> Optional[Artifact]:
    if not ids.doi:
        return None

    # Imported at call time: fetchpdf imports the engine, so a
    # module-scope import here would be a cycle.
    #
    # The CHAIN, not the `fetch_pdf` wrapper. The wrapper falls back to a
    # structured-full-text walk when it cannot produce a PDF -- correct for a
    # caller who asked for a paper, wrong here, where the tiered engine has
    # ALREADY walked and failed T1 and T2 before descending to this rung. Going
    # through the wrapper re-ran that walk verbatim for every hard record, with
    # `resolver=None`, so it also discarded the primed batch resolver and built
    # a fresh limiter, client and cache per record -- then announced it had
    # saved structured text to a temp path the caller never sees.
    from ...fetchpdf import _fetch_pdf_chain as fetch_pdf
    from ...fetchpdf import download_url_for

    directory = os.path.dirname(os.path.abspath(ctx.save_path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-t5-", suffix=".pdf", dir=directory)
    os.close(handle)
    # The chain's own skip-if-exists check would see the empty temp file and
    # return immediately, so it has to be gone before the call.
    _unlink(temp_path)

    source_out = [None]
    written = None
    try:
        written = fetch_pdf(
            ids.doi,
            temp_path,
            email=ctx.email,
            verbose=ctx.verbose,
            delay=ctx.delay,
            use_playwright=ctx.use_playwright,
            _source_out=source_out,
        )
        if not written or not os.path.exists(written):
            return None
        with open(written, "rb") as f:
            content = f.read()
        if not content:
            return None

        # The chain's own Elsevier XML fallback writes .xml, not .pdf. That is a
        # T1 artifact arriving through the T5 door; the engine's classifier will
        # re-tier it, so it is simply passed along as found.
        return Artifact(
            content=content,
            tier=Tier.T5_PDF,
            source="legacy_pdf_chain",
            # The URL the bytes actually came from, recorded by try_download.
            # This was "" until now, which is why a wrong artifact on disk could
            # not say where it came from.
            url=download_url_for(written) or download_url_for(temp_path),
            http_status=200,
            served_content_type="application/pdf",
            identifier_used=ids.doi,
            license=ids.license,
            extra={"legacy_source": source_out[0]},
        )
    finally:
        for path in {temp_path, written}:
            if path:
                _unlink(path)


def _unlink(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
