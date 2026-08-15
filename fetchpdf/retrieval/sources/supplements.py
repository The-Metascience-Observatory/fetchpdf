"""T4: structured supplementary data.

Frequently the actual underlying data rather than a rendering of it -- a
spreadsheet of per-arm outcomes beats any reconstruction of the typeset table.
Coverage is partial and mapping supplements back to the paper's tables is
manual, which is why this sits below LaTeX rather than above it.

The repository routes here are nearly free: the existing Figshare and Zenodo
handlers already fetch the complete file listing and then discard everything
that is not a PDF. Filtering that same listing on mimetype and extension costs
one predicate.

Europe PMC's supplementaryFiles endpoint is the best of the four because it is
keyed on the PMCID phase 1 already resolved: GET /{PMCID}/supplementaryFiles
returns 200 application/zip (verified, 132 KB for PMC1817752).

The listings themselves now live in ../supplement_index.py, shared with
--pull-supplementary. The two callers want different subsets and the difference
is exactly one predicate, applied here and nowhere else: _is_structured. This
tier takes only what survives to an extraction pass with its rows and columns
intact, because here a supplement stands *in place of* the paper's full text --
a supplementary PDF is no better than the PDF it replaced. --pull-supplementary
takes everything, because there a supplement stands alongside the paper.
"""

import io
import zipfile
from typing import List, Optional, Tuple

from ..artifact import Artifact
from ..supplement_index import (
    EPMC_SUPPL,
    enumerate_figshare,
    enumerate_osf,
    enumerate_zenodo,
    match_id,
)
from ..tiers import Tier

#: Extensions whose row/column structure survives to the extraction pass.
STRUCTURED_EXTENSIONS = (".xlsx", ".xls", ".csv", ".tsv", ".docx", ".ods", ".txt")
STRUCTURED_MIMETYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "text/csv",
    "text/tab-separated-values",
)

_MAX_FILES = 25


def fetch_epmc_supplements(ids, ctx) -> Optional[Artifact]:
    """The EPMC bundle, stored verbatim as one .suppl.zip.

    Deliberately still its own request rather than going through
    supplement_index: this tier stores the archive exactly as served, so it wants
    the bytes, not a listing, and it wants them under the endpoint's default
    parameters. --pull-supplementary asks the same endpoint with
    includeInlineImage=no, which returns a different (better, for its purpose)
    archive -- 4 members instead of 14 on PMC1817752. Sharing one call between
    the two would mean silently changing what this tier writes.
    """
    if not ids.pmcid:
        return None
    url = EPMC_SUPPL.format(pmcid=ids.pmcid)
    response = ctx.http.get(url, timeout=60)
    if not response.ok or not response.content:
        ctx.log(f"    EPMC supplements: HTTP {response.status}")
        return None
    if not _looks_structured_zip(response.content):
        ctx.log("    EPMC supplements: bundle holds nothing structured")
        return None
    return Artifact(
        content=response.content,
        tier=Tier.T4_SUPPLEMENT,
        source="europepmc_supplements",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.pmcid,
        license=ids.license,
        extension=".suppl.zip",
    )


def fetch_figshare_files(ids, ctx) -> Optional[Artifact]:
    article_id = match_id(ids.doi, r"figshare\.(\d+)")
    if not article_id:
        return None
    files = enumerate_figshare(ids, ctx, with_item_type=False)
    return _bundle(ctx, _structured_only(files), ids, "figshare_files", article_id)


def fetch_zenodo_files(ids, ctx) -> Optional[Artifact]:
    record_id = match_id(ids.doi, r"zenodo\.(\d+)")
    if not record_id:
        return None
    return _bundle(ctx, _structured_only(enumerate_zenodo(ids, ctx)), ids,
                   "zenodo_files", record_id)


def fetch_osf_files(ids, ctx) -> Optional[Artifact]:
    osf_id = match_id(ids.doi, r"osf\.io/([a-z0-9]+)") or match_id(ids.doi, r"/([a-z0-9]{5})$")
    if not osf_id:
        return None
    return _bundle(ctx, _structured_only(enumerate_osf(ids, ctx)), ids,
                   "osf_files", osf_id)


# -- shared -----------------------------------------------------------------


def _structured_only(files) -> List[Tuple[str, str]]:
    """The one predicate that separates this tier from --pull-supplementary."""
    return [(f.name, f.url) for f in files if _is_structured(f.name, f.mimetype)]


def _is_structured(name: Optional[str], mimetype: Optional[str]) -> bool:
    """Filter on mimetype/extension rather than assuming PDF."""
    if mimetype and str(mimetype).lower() in STRUCTURED_MIMETYPES:
        return True
    lowered = (name or "").lower()
    return any(lowered.endswith(ext) for ext in STRUCTURED_EXTENSIONS)


def _bundle(ctx, candidates, ids, source, identifier) -> Optional[Artifact]:
    """Download the structured files and return them as one artifact.

    A single file keeps its own extension so the downstream reader does not have
    to unwrap a one-entry zip; several are bundled so a record's supplements
    stay together and the artifact stays a single addressable thing.
    """
    candidates = [(n, u) for n, u in candidates if u][:_MAX_FILES]
    if not candidates:
        return None

    downloaded = []
    for name, url in candidates:
        response = ctx.http.get(url, timeout=60, polite=False)
        if response.ok and response.content:
            downloaded.append((name or "supplement", response.content))
    if not downloaded:
        return None

    if len(downloaded) == 1:
        name, content = downloaded[0]
        extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".suppl"
        return Artifact(
            content=content,
            tier=Tier.T4_SUPPLEMENT,
            source=source,
            url=candidates[0][1],
            http_status=200,
            identifier_used=str(identifier),
            license=ids.license,
            extension=extension,
            extra={"files": [name]},
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in downloaded:
            archive.writestr(name, content)
    return Artifact(
        content=buffer.getvalue(),
        tier=Tier.T4_SUPPLEMENT,
        source=source,
        url=candidates[0][1],
        http_status=200,
        identifier_used=str(identifier),
        license=ids.license,
        extension=".suppl.zip",
        extra={"files": [n for n, _ in downloaded]},
    )


def _looks_structured_zip(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return any(_is_structured(name, None) for name in archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
