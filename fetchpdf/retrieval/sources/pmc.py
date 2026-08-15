"""NCBI E-utilities efetch against db=pmc. Fallback only.

Same underlying PMC JATS as Europe PMC serves, so it runs only when the Europe
PMC route did not produce an artifact. Storing both copies would not be
corroboration -- it would manufacture false agreement between two views of one
source, which is worse than having one.

This endpoint is also the cleanest example of why HTTP 200 cannot be trusted.
For a non-OA record it returns a complete, well-formed document -- journal
metadata, title, authors, the lot -- with no <body> element, and the reason
stated in an XML comment that every parser silently discards:

    <!--The publisher of this article does not allow downloading of the full
        text in XML form.-->

Verified on PMC3390974 and PMC2148499. The T1 validator catches it against the
raw bytes; the check lives there rather than here so every XML source gets it.

Scope note on the August 2026 NCBI change: it removes legacy *bulk dataset
files* from the FTP and Cloud services. The OA Web Service and E-utilities are
not in scope, and this package touches no FTP or S3 path, so nothing here needs
to migrate.
"""

from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def fetch_efetch_xml(ids, ctx) -> Optional[Artifact]:
    if not ids.pmcid:
        return None
    if ctx.pmc_xml_already_taken:
        ctx.log("    NCBI efetch: skipped, Europe PMC already served this PMC record")
        return None

    numeric_id = str(ids.pmcid).upper().replace("PMC", "")
    response = ctx.http.get(
        EFETCH,
        params={"db": "pmc", "id": numeric_id, "retmode": "xml"},
        timeout=45,
    )
    if not response.ok or not response.content:
        ctx.log(f"    NCBI efetch: HTTP {response.status} for {ids.pmcid}")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T1_XML,
        source="pmc_efetch",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.pmcid,
        license=ids.license,
    )
