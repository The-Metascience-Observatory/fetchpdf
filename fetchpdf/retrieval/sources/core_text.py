"""T6: CORE plain full text. Screening only.

Flattening destroys row/column association silently. Every number is still
present, in reading order, anchored to nothing -- so an extraction model does
not fail, it produces confident, plausible, wrong arm-to-outcome assignments.
That is worse than a missing table, because a missing table is visible.

So this source refuses outright when the target task is extraction, rather than
ranking last and being degraded into. The artifact it does return for screening
is stamped tables_unavailable=True so nothing downstream can mistake it for a
source of tabular values.
"""

from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

CORE_SEARCH = "https://api.core.ac.uk/v3/search/works"


def fetch_core_fulltext(ids, ctx) -> Optional[Artifact]:
    if not ids.doi:
        return None

    if ctx.is_extraction():
        ctx.log("    CORE full text: refused (plain text cannot support extraction)")
        return None

    import os

    if not os.getenv("COREAPIKEY"):
        return None

    response = ctx.http.get(
        CORE_SEARCH,
        params={"q": 'doi:"{}"'.format(ids.doi), "limit": 1},
        timeout=30,
    )
    if not response.ok:
        return None
    try:
        results = (response.json() or {}).get("results") or []
    except ValueError:
        return None
    if not results:
        return None

    full_text = results[0].get("fullText")
    if not full_text or not full_text.strip():
        return None

    return Artifact(
        content=full_text.encode("utf-8"),
        tier=Tier.T6_PLAINTEXT,
        source="core_fulltext",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.doi,
        license=results[0].get("license") or ids.license,
        extension=".txt",
        tables_unavailable=True,
    )
