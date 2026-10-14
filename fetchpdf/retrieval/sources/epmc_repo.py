"""Europe PMC's render repository: full text for in-EPMC records that are not OA.

The last rung of T1 and of T2. It backs up the documented routes above it and
is never preferred to one of them; reached on the tiered path and by
`fetch_pdf`'s structured fallback when no PDF could be had.

A record can be inEPMC=Y and isOpenAccess=N at once. That pair is common enough
to matter (JAMA, and most subscription titles depositing under a public-access
mandate) and it defeats every documented route simultaneously: /{pmcid}/
fullTextXML 404s, oa.fcgi answers idIsNotOpenAccess, efetch returns front matter
plus the denial comment pmc.py documents, and doi.org lands on a 403. Meanwhile
europepmc.org renders the whole article, free, to anyone.

The site is a JS application, so fetching the article URL statically returns a
28KB shell with zero <table>. What it calls underneath needs no browser:

    /api/fulltextRepo?pmcId=..&type=METADATA   file manifest for the render
    /api/fulltextRepo?type=FILE&fileName=..    the JATS behind it
    /api/fulltextXmlTransform?ft_id=..         that JATS rendered to HTML

The XML route is the one worth having: canonical JATS with a real <body>, not
the denial stub, because the publisher's no-XML flag is enforced on NCBI's APIs
rather than on the repository EPMC's own reader reads from. Verified on
PMC12712832 (JAMA; 3 tables, 263 populated cells, 38.7k body chars) and on four
further inEPMC/non-OA records, all returning JATS with a populated <body>.

CAVEAT: these are internal endpoints of the Europe PMC frontend, not the
documented RESTful API. Nothing promises they keep their shape, so both sources
validate like any other and demote quietly. Separately, the content is free to
read but the publisher has signalled no bulk XML; a deployment that must honour
that sets `"enabled": false` on both sources in its ladder config.
"""

from typing import List, Optional

from ..artifact import Artifact
from ..tiers import Tier

REPO = "https://europepmc.org/api/fulltextRepo"
TRANSFORM = "https://europepmc.org/api/fulltextXmlTransform"

# Both are the same article. `ssnex` is the plain deposit; `ssnexlite` is the
# variant EPMC's reader prefers. Ordered rather than picked so a title that
# ships only one still resolves.
_XML_PREFERENCE = (".ssnex.xml", ".ssnexlite.xml")


def _metadata(ids, ctx) -> Optional[dict]:
    """The render manifest, memoized: both sources here want it."""
    key = "epmc_repo_metadata:{}".format(ids.pmcid)
    if key in ctx.scratch:
        return ctx.scratch[key]

    response = ctx.http.get(
        REPO, params={"pmcId": ids.pmcid, "type": "METADATA"}, timeout=25,
    )
    data = response.json_or(None) if response.ok else None
    if not isinstance(data, dict):
        data = None
        ctx.log(f"    EPMC repo: no manifest for {ids.pmcid} (HTTP {response.status})")
    ctx.scratch[key] = data
    return data


def _xml_filenames(manifest: dict) -> List[str]:
    names = [
        f.get("filename")
        for f in manifest.get("files") or []
        if f.get("type") == "xml" and f.get("filename")
    ]

    def rank(name):
        for i, suffix in enumerate(_XML_PREFERENCE):
            if name.endswith(suffix):
                return i
        return len(_XML_PREFERENCE)

    return sorted(names, key=rank)


def fetch_repo_xml(ids, ctx) -> Optional[Artifact]:
    """JATS from the render repository, for records the OA routes cannot serve."""
    if not ids.pmcid:
        return None
    if ctx.pmc_xml_already_taken:
        ctx.log("    EPMC repo: skipped, PMC JATS already served for this record")
        return None

    manifest = _metadata(ids, ctx)
    if not manifest:
        return None

    filenames = _xml_filenames(manifest)
    if not filenames:
        ctx.log(f"    EPMC repo: manifest for {ids.pmcid} lists no XML file")
        return None

    # The manifest states the version; the FILE endpoint requires it. Assuming 1
    # works today and would silently serve a stale revision the day it stops.
    version = manifest.get("version") or 1
    try:
        version = int(float(version))
    except (TypeError, ValueError):
        version = 1

    for filename in filenames:
        response = ctx.http.get(
            REPO,
            params={
                "pmcId": ids.pmcid, "type": "FILE", "fileName": filename,
                "mimeType": "application/xml", "version": version,
            },
            timeout=45,
        )
        if not response.ok or not response.content:
            ctx.log(f"    EPMC repo: {filename} HTTP {response.status}")
            continue

        ctx.pmc_xml_already_taken = True
        return Artifact(
            content=response.content,
            tier=Tier.T1_XML,
            source="epmc_repo_xml",
            url=response.url,
            http_status=response.status,
            served_content_type=response.content_type,
            identifier_used=ids.pmcid,
            license=ids.license,
            extra={"repo_file": filename, "repo_version": version,
                   "repo_domain": manifest.get("domain")},
        )
    return None


def fetch_transform_html(ids, ctx) -> Optional[Artifact]:
    """The same JATS, server-rendered to HTML. Fallback when the XML file is gone.

    Kept because it is a different failure surface, not a different document: the
    manifest can list no XML while the transform still renders. `pmc_domain` is
    branding only -- the response is byte-identical without it -- so it is not
    sent.
    """
    if not ids.pmcid:
        return None

    response = ctx.http.get(
        TRANSFORM, params={"ft_id": ids.pmcid, "source": "PMC"}, timeout=45,
    )
    if not response.ok or not response.content:
        ctx.log(f"    EPMC transform: {ids.pmcid} HTTP {response.status}")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T2_HTML,
        source="epmc_transform_html",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.pmcid,
        license=ids.license,
    )
