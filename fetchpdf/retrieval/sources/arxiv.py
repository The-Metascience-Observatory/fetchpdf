"""arXiv e-print source (T3) and LaTeXML HTML (T2).

The query API this codebase already uses returns metadata only. The LaTeX source
is at a separate endpoint entirely -- export.arxiv.org/e-print/{id} -- and it
was simply missing, which means arXiv PDFs were being taken while the source
that produced them sat one request away. Verified: both arxiv.org/e-print and
export.arxiv.org/e-print return 200 application/gzip.

HTML is available for effectively the whole corpus, via two endpoints:

  * arxiv.org/html/{id} -- official, but only from ~Dec 2023 onward.
    Verified: 2401.00001 -> 200, 2101.00001 -> 404.
  * ar5iv.labs.arxiv.org/html/{id} -- LaTeXML conversion of the back catalogue.
    Verified back to the first paper of the modern ID scheme: 0704.0001 -> 200,
    2.5 MB, 35 <table> elements. 2101.00001 -> 200, 129 KB, 2 tables.

Official first (it is the newer, better-maintained converter), ar5iv second.
There is no JATS route: arXiv does not produce JATS, so "arXiv as XML" is not a
thing -- T2 HTML and T3 LaTeX source are the structured options.

Both renderings are machine conversions of LaTeX, so a conversion artifact in a
table is possible in a way it is not for publisher JATS. That caveat is recorded
in provenance rather than assumed away.

arXiv tightened rate limits in February 2026 (429s reported even at 3-4 second
spacing), which is why both arXiv hosts get their own slow bucket.
"""

import re
from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

EPRINT = "https://export.arxiv.org/e-print/{arxiv_id}"

#: Official renderer first, then the back-catalogue converter. Order matters:
#: where both exist the official one is the better-maintained conversion.
LATEXML_HTML_URLS = (
    ("arxiv_html", "https://arxiv.org/html/{arxiv_id}"),
    ("ar5iv", "https://ar5iv.labs.arxiv.org/html/{arxiv_id}"),
)


def fetch_eprint_source(ids, ctx) -> Optional[Artifact]:
    """The LaTeX source tarball."""
    if not ids.arxiv_id:
        return None

    url = EPRINT.format(arxiv_id=ids.arxiv_id)
    response = ctx.http.get(url, timeout=90, polite=False)
    if not response.ok or not response.content:
        ctx.log(f"    arXiv e-print: HTTP {response.status} for {ids.arxiv_id}")
        return None

    # Some submissions are a single PDF with no source. Those are a T5 in T3's
    # clothing; the classifier will catch it, but saying so here is clearer.
    if response.content[:4] == b"%PDF":
        ctx.log("    arXiv e-print: submission is PDF-only, no LaTeX source")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T3_SOURCE,
        source="arxiv_eprint",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.arxiv_id,
        extension=".source.tar.gz",
    )


def fetch_latexml_html(ids, ctx) -> Optional[Artifact]:
    """arXiv HTML: the official renderer, falling back to ar5iv for older papers."""
    if not ids.arxiv_id:
        return None

    # Strip any version suffix: ar5iv 404s on some versioned ids that resolve
    # perfectly well unversioned.
    base_id = re.sub(r"v\d+$", "", str(ids.arxiv_id))

    for source_name, template in LATEXML_HTML_URLS:
        url = template.format(arxiv_id=base_id)
        response = ctx.http.get(url, timeout=60, polite=False)
        if not response.ok or not response.content:
            ctx.log(f"    {source_name}: HTTP {response.status} for {base_id}")
            continue
        return Artifact(
            content=response.content,
            tier=Tier.T2_HTML,
            source=source_name,
            url=response.url,
            http_status=response.status,
            served_content_type=response.content_type,
            identifier_used=base_id,
            normalization_failures=[
                f"{source_name}: LaTeX->HTML machine conversion; "
                f"tables may carry conversion artifacts"
            ],
        )
    return None
