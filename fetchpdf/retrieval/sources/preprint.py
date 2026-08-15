"""bioRxiv / medRxiv JATS.

Their details API exposes a `jatsxml` field pointing at the full JATS document:

    api.biorxiv.org/details/biorxiv/10.1101/2020.01.30.927871
      -> jatsxml: https://www.biorxiv.org/content/early/2020/01/31/....source.xml

The trap is the server segment. A medRxiv DOI queried against /details/biorxiv/
returns HTTP 200 with {"collection": [], "messages": [{"status": "no posts
found"}]} -- a miss that looks exactly like a successful empty answer. Querying
the wrong server for every medRxiv record would silently report "no full text"
for the entire preprint half of a corpus, so both servers are tried and the
first non-empty collection wins.

This is also where Europe PMC PPR records land: EPMC indexes preprints with
inEPMC=Y and a populated fullTextIdList but 404s on fullTextXML, so the flags
cannot be used and the publisher's own API is the real route.
"""

from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

DETAILS = "https://api.biorxiv.org/details/{server}/{doi}"
_SERVERS = ("biorxiv", "medrxiv")
_PREPRINT_PREFIX = "10.1101/"


def fetch_preprint_jats(ids, ctx) -> Optional[Artifact]:
    doi = (ids.doi or "").lower()
    is_preprint = doi.startswith(_PREPRINT_PREFIX) or bool(ids.ppr_id)
    if not is_preprint:
        return None

    record = None
    for server in _order_servers(ids.preprint_server):
        record = _latest_version(ctx, server, ids.doi)
        if record:
            ids.learn(
                "biorxiv.details",
                url=DETAILS.format(server=server, doi=ids.doi),
                note="preprint server identified",
                preprint_server=server,
                license=record.get("license"),
            )
            break

    if not record:
        ctx.log("    preprint: no record on either bioRxiv or medRxiv")
        return None

    jats_url = record.get("jatsxml")
    if not jats_url:
        ctx.log("    preprint: record has no jatsxml field")
        return None

    response = ctx.http.get(jats_url, timeout=45)
    if not response.ok or not response.content:
        ctx.log(f"    preprint: jatsxml HTTP {response.status}")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T1_XML,
        source="preprint_jats",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.doi,
        license=record.get("license"),
        extra={"version": record.get("version"), "server": record.get("server")},
    )


def _order_servers(known: Optional[str]):
    """Try the known server first; 'unknown' means the DOI prefix told us nothing."""
    if known in _SERVERS:
        return (known,) + tuple(s for s in _SERVERS if s != known)
    return _SERVERS


def _latest_version(ctx, server: str, doi: str) -> Optional[dict]:
    """The highest-version record, or None for the empty-collection miss."""
    response = ctx.http.get(DETAILS.format(server=server, doi=doi), timeout=25)
    if not response.ok:
        return None
    try:
        collection = (response.json() or {}).get("collection") or []
    except ValueError:
        return None
    if not collection:
        return None
    return max(collection, key=lambda r: _version_of(r))


def _version_of(record: dict) -> int:
    try:
        return int(record.get("version") or 0)
    except (TypeError, ValueError):
        return 0
