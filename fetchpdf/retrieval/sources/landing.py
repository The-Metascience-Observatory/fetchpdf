"""T7: the landing page. Last rung.

Kept as a real source rather than an implicit "nothing found" so that a record
which produced only a landing page is distinguishable from one that was never
attempted. That difference matters when re-running with --upgrade-existing:
the first is a known dead end, the second is work still to do.
"""

from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

DOI_RESOLVER = "https://doi.org/{doi}"


def fetch_landing_page(ids, ctx) -> Optional[Artifact]:
    if not ids.doi:
        return None
    response = ctx.http.get(DOI_RESOLVER.format(doi=ids.doi), timeout=30, polite=False)
    if not response.ok or not response.content:
        return None
    return Artifact(
        content=response.content,
        tier=Tier.T7_LANDING,
        source="landing_page",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.doi,
        license=ids.license,
        extension=".landing.html",
        tables_unavailable=True,
    )
