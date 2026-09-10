"""T2: the PMC article page, for records the PMC XML routes could not serve.

MEASURED 2026-09-06 over a 105-paper working corpus. The format ladder settled
on publisher XML for 99 records and on a PDF for 6 -- and all six of those
resolve to a PMCID and serve a complete PMC article page, body text and real
``<table>`` markup included. Not one was genuinely PDF-only; nobody had asked
for the web version. Those same six records also produced every conversion
defect the corpus had (a font map reporting a mu as an m, so a dose read
1000x wrong; dashes read as digits), so what looked like a conversion problem
was a fetching gap one HTTP request wide.

WHY THIS IS NOT A SECOND COPY OF THE PMC JATS. A tier is exhausted before the
walk descends, so this rung is reached only when ``europepmc_xml`` and
``pmc_efetch`` have both failed or been rejected -- which, for a record that
has a PMCID, means the OA subset does not carry the XML. The page is a
different document from the one those endpoints declined to serve, which is
also why ``pmc_xml_already_taken`` is not consulted here: that flag exists to
stop two copies of one XML being stored as if they corroborated each other,
and this is not XML.

THE RETRY IS NOT DEFENSIVE PROGRAMMING. PMC intermittently answers 200 with a
page whose article body is absent -- verified twice on 2026-09-06, on the same
article, minutes apart, with the complete page arriving 30 s later. A single
request records that transient as "PMC serves no full text for this record",
which is a claim about the paper written out of somebody else's flaky cache.
So the page is asked for again on a widening pause, and the attempt count goes
into the sidecar: a page that needed three tries has to be distinguishable
from one that answered first time.

What this module does NOT do is decide whether the page is usable. After the
last attempt the bytes are handed over whatever they contain, and the T2 gate
in validate.py judges them against the same character threshold, populated-
``<td>`` requirement and bot-challenge signatures every other HTML source is
held to. Refusing here on the body marker alone would mean that the day PMC
renames a CSS class this source silently stops existing, rather than costing
three requests and then deferring to a check that reads the actual content.
"""

import re
import time
from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

PMC_ARTICLE_PAGE = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"

#: Pauses before each re-request of the article page. Wide, and deliberately
#: not a tight loop: the failure being waited out is a cache miss on PMC's
#: side, and hammering it is both rude and useless.
PAGE_RETRY_WAITS = (5, 15, 30)

#: PMC's own container for the article body. Verified live 2026-09-07 on
#: PMC1817752: the complete page carries ``class="body main-article-body"``
#: exactly once, and the truncated page carries no article body at all.
#: Matched as a substring of the raw bytes rather than through a parser
#: because this decides whether to ASK AGAIN, not whether to accept -- and an
#: lxml-less install must still be able to make that call.
_BODY_MARKER = b"main-article-body"

#: ``<article-id pub-id-type="pmc">`` in a JATS file on disk. PMC writes the
#: type as "pmc" and Europe PMC as "pmcid"; both are accepted.
_ARTICLE_ID_RE = re.compile(
    rb'<article-id[^>]*pub-id-type="(?:pmcid|pmc)"[^>]*>\s*(?:PMC)?(\d+)\s*<',
    re.IGNORECASE,
)

#: How much of an XML file to read looking for that id. The article ids sit in
#: the front matter; reading a 40 MB supplement-bearing JATS to the end to find
#: a number in its first kilobyte would be a waste per record.
_JATS_HEAD_BYTES = 20000


def fetch_pmc_article_page(ids, ctx, waits=None) -> Optional[Artifact]:
    """The PMC article page for this record, or None if PMC has no page to give.

    `waits` is read at call time rather than baked in, so a test can shrink the
    pauses without monkeypatching the clock.
    """
    pmcid = pmcid_for(ids, ctx)
    if not pmcid:
        return None

    url = PMC_ARTICLE_PAGE.format(pmcid=pmcid)
    waits = PAGE_RETRY_WAITS if waits is None else waits

    response = None
    attempts = 0
    for pause in (0,) + tuple(waits):
        if pause:
            ctx.log(f"    PMC page: no article body yet; asking again in {pause}s")
            time.sleep(pause)
        attempts += 1
        response = ctx.http.get(url, timeout=45)
        if not response.ok or not response.content:
            ctx.log(f"    PMC page: HTTP {response.status} for {pmcid}")
            return None
        if page_has_article_body(response.content):
            break

    complete = page_has_article_body(response.content)
    if attempts > 1 and ctx.provenance is not None:
        ctx.provenance.note(
            f"pmc_html: the article body {'appeared on attempt' if complete else 'was still absent after'} "
            f"{attempts} of {len(waits) + 1} requests for {pmcid}"
        )

    return Artifact(
        content=response.content,
        tier=Tier.T2_HTML,
        source="pmc_html",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=pmcid,
        license=ids.license,
        extra={
            "page_attempts": attempts,
            "article_body_present": complete,
        },
    )


def page_has_article_body(content: bytes) -> bool:
    """Whether this response is the whole article page or PMC's truncated one."""
    return _BODY_MARKER in (content or b"")


def pmcid_for(ids, ctx) -> Optional[str]:
    """The PMCID for this record, from resolution or from a JATS file on disk.

    The disk half is why this source declares no `requires` in ladder.json. A
    record can carry a PMC JATS from an earlier run while this run's resolution
    never touches Europe PMC -- the resolution chain reaches a PMCID only when
    it happens to pass through a service that returns one -- and a `requires`
    gate would skip the source before it could look. A JATS from PMC always
    names its own id.
    """
    if ids.pmcid:
        return normalise_pmcid(ids.pmcid)
    return _pmcid_from_disk(ctx)


def normalise_pmcid(value) -> Optional[str]:
    digits = re.search(r"(\d+)", str(value or ""))
    return f"PMC{digits.group(1)}" if digits else None


def _pmcid_from_disk(ctx) -> Optional[str]:
    from ..engine import _stem_for

    try:
        with open(_stem_for(ctx.save_path) + ".xml", "rb") as f:
            head = f.read(_JATS_HEAD_BYTES)
    except OSError:
        return None
    match = _ARTICLE_ID_RE.search(head)
    if not match:
        return None
    pmcid = normalise_pmcid(match.group(1))
    if pmcid:
        ctx.log(f"    PMC page: {pmcid} read off the JATS already on disk")
    return pmcid
