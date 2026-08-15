"""Europe PMC: the highest-value T1 route, and the one trap in it.

Measured on a 1023-DOI clinical corpus: 48.7% of records resolve to a PMCID, and
82.5% of those yield a JATS document that passes the T1 gate. That is ~40% of a
batch served by one free, unauthenticated endpoint.

The trap is preprints. Europe PMC indexes them as PPR records with availability
flags identical to an OA journal article:

    10.1101/2020.01.30.927871 -> id=PPR110986 source=PPR inEPMC=Y
                                 isOpenAccess=Y hasPDF=Y
                                 fullTextIdList={'fullTextId': ['PPR110986']}

...and /PPR110986/fullTextXML returns 404. So do /supplementaryFiles and
/textMinedTerms. Reproduced on PPR118691. The flags do not predict full text for
preprints, so PPR records are handed to the bioRxiv/medRxiv source instead --
and the PPR id is stashed on the IdentifierSet so that source knows to run.

The flags are still worth fetching: they gate the fullTextXML call for journal
articles and carry the license. But the fetch also tolerates a 404 on a record
the flags called available, because ~12% of PMCID records are indexed in the ID
Converter without being in the Europe PMC OA subset.
"""

from typing import Optional

from ..artifact import Artifact
from ..tiers import Tier

REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def search_flags(ids, ctx) -> dict:
    """Fetch and memoize the Europe PMC record for this DOI/PMCID.

    Cheap, and it answers three questions at once: is the full text here, is it
    open access, and is this a preprint pretending otherwise.
    """
    if ids.epmc_flags.get("_searched"):
        return ids.epmc_flags

    query = None
    if ids.doi:
        query = 'DOI:"{}"'.format(ids.doi)
    elif ids.pmcid:
        query = "PMCID:{}".format(ids.pmcid)
    if not query:
        return ids.epmc_flags

    response = ctx.http.get(
        REST + "/search",
        params={"query": query, "format": "json", "resultType": "core"},
        timeout=25,
    )
    ids.epmc_flags["_searched"] = True
    if not response.ok:
        ids.chain.record("europepmc.search", url=REST + "/search", http_status=response.status)
        return ids.epmc_flags

    try:
        results = (response.json() or {}).get("resultList", {}).get("result", [])
    except ValueError:
        results = []
    if not results:
        ids.chain.record(
            "europepmc.search", url=REST + "/search", http_status=response.status,
            note="no Europe PMC record",
        )
        return ids.epmc_flags

    record = results[0]
    for key in ("inEPMC", "inPMC", "isOpenAccess", "hasPDF", "hasSuppl",
                "hasTextMinedTerms", "source", "id"):
        if record.get(key) is not None:
            ids.epmc_flags[key] = record[key]
    ids.epmc_flags["fullTextIdList"] = record.get("fullTextIdList") or {}

    values = {}
    if record.get("pmcid"):
        values["pmcid"] = record["pmcid"]
    if record.get("pmid"):
        values["pmid"] = str(record["pmid"])
    if record.get("license"):
        values["license"] = record["license"]
    # A PPR id is the signal that this is a preprint; the preprint source uses it.
    if str(record.get("source") or "").upper() == "PPR" and record.get("id"):
        values["ppr_id"] = record["id"]
    ids.learn(
        "europepmc.search",
        url=REST + "/search",
        http_status=response.status,
        note="availability flags",
        **values
    )
    return ids.epmc_flags


def fetch_fulltext_xml(ids, ctx) -> Optional[Artifact]:
    """JATS full text for a PMC record."""
    flags = search_flags(ids, ctx)

    if str(flags.get("source") or "").upper() == "PPR":
        ctx.log("    Europe PMC: preprint (PPR) record -- flags do not imply full text here")
        return None

    if not ids.pmcid:
        return None

    # Gate on the flags rather than blind-fetching, but do not treat them as
    # authoritative: fetch anyway when they are simply absent.
    if flags and flags.get("inEPMC") == "N" and flags.get("inPMC") == "N":
        ctx.log(f"    Europe PMC: {ids.pmcid} flagged as not in EPMC/PMC")
        return None

    url = "{}/{}/fullTextXML".format(REST, ids.pmcid)
    response = ctx.http.get(url, timeout=45)
    if not response.ok or not response.content:
        # Expected for ~12% of PMCID records: indexed, but not in the OA subset.
        ctx.log(f"    Europe PMC: {ids.pmcid} fullTextXML HTTP {response.status}")
        return None

    ctx.pmc_xml_already_taken = True
    return Artifact(
        content=response.content,
        tier=Tier.T1_XML,
        source="europepmc_xml",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.pmcid,
        license=flags.get("license") or ids.license,
        extra={"epmc_flags": {k: v for k, v in flags.items() if not k.startswith("_")}},
    )
