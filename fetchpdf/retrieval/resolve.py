"""Phase 1: build the fullest identifier set we can, before retrieving anything.

Ordering principle: run free and batchable calls before quota-limited ones, and
gate every expensive call on a cheap signal that predicts it will succeed.

One deliberate deviation from the specified stage order, because measuring it
changed the answer. The spec puts a per-record Crossref call first in stage 1.
On a real clinical corpus (1023 DOIs) the batched ID Converter alone resolves a
PMCID for 48.7% of records, and 82.5% of those go on to yield a validated JATS
artifact -- so roughly 40% of the batch never needs Crossref at all. Crossref's
gating role (which publisher owns this DOI) is derivable from the DOI prefix
string with no network call whatsoever. So:

  prime()   -- batched ID Converter only. 6 HTTP calls for 1023 DOIs.
  resolve() -- returns as soon as a PMCID is in hand; retrieval short-circuits.
  crossref() and resolve_conditional() -- lazy, called only once the T1 routes
              that need no further resolution have failed.

This preserves the principle exactly while skipping ~40% of the Crossref calls
the literal step order would make.
"""

import re
from typing import Iterable, List, Optional, Sequence

from .identifiers import IdentifierSet

#: The ID Converter moved. www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/ now 301s
#: here, so the legacy URL costs an extra round-trip on every single call.
IDCONV_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"

#: Hard ceiling, verified empirically: 200 ids returns status "ok" with 200
#: records; 201 returns status "error" with zero records, no partial result and
#: no warning. Overshooting does not truncate -- it loses the whole chunk.
IDCONV_MAX_IDS = 200

#: DOI registrant prefixes that resolve through DataCite rather than Crossref.
#: Stage 3 only calls DataCite for these; on a Crossref DOI it is a guaranteed
#: miss, and a miss still costs a round-trip.
DATACITE_REPOSITORY_PREFIXES = {
    "10.5281": "zenodo",
    "10.6084": "figshare",
    "10.17605": "osf",
    "10.31234": "osf",       # PsyArXiv
    "10.31235": "osf",       # SocArXiv
    "10.23668": "psycharchives",
    "10.25384": "figshare",  # SAGE-hosted figshare
    "10.48550": "arxiv",
}

_ELSEVIER_PREFIX = "10.1016"
_PREPRINT_PREFIX = "10.1101"      # bioRxiv and medRxiv share it; the API disambiguates

_ARXIV_FROM_DOI_RE = re.compile(r"10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)", re.IGNORECASE)
_PII_RE = re.compile(r"PII:([A-Z0-9]+)", re.IGNORECASE)

#: An arXiv id sitting in a URL we already have.
#:
#: This exists because the arXiv routes -- T2 LaTeXML HTML and T3 LaTeX source --
#: were effectively dead. They require `arxiv_id`, and the only two places that ever
#: produced one were a 10.48550/arXiv.* DOI and Semantic Scholar's externalIds. An
#: arXiv paper published under a publisher DOI therefore never got an id, so the two
#: highest-fidelity routes available for physics/CS/stats silently never ran.
#:
#: The arXiv API cannot fix it: the legacy query API has no `doi:` field prefix
#: (verified -- such a search returns zero entries). But the responses we already
#: fetch do carry the id. Unpaywall, for instance:
#:     10.1103/PhysRevLett.116.061102 -> oa_locations[] has arxiv.org/pdf/1602.03837
#: so harvesting costs no additional request.
#:
#: Both id schemes: modern (2101.00001) and pre-2007 (hep-th/9901001).
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/"
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
    r"(?:v\d+)?",
    re.IGNORECASE,
)


def arxiv_id_in(*values) -> Optional[str]:
    """The first arXiv id found in any of these strings, version suffix stripped.

    Accepts anything stringable -- URLs, whole JSON blobs, None -- so callers can
    hand it whatever they happen to have without pre-filtering.
    """
    for value in values:
        if not value:
            continue
        match = _ARXIV_URL_RE.search(str(value))
        if match:
            return match.group(1)
    return None


def doi_prefix(doi: Optional[str]) -> Optional[str]:
    """The registrant prefix, e.g. '10.1016'. Free -- no network call."""
    if not doi or not doi.startswith("10."):
        return None
    return doi.split("/", 1)[0]


class BatchResolver:
    """Owns the resolution cache and the batched stage-1 lookups.

    One instance per batch. In single-DOI mode it degenerates to chunks of one,
    which is correct but wastes the 200x leverage -- hence batch_fetch_pdfs
    primes it with the whole DOI list up front.
    """

    def __init__(self, http, cache, ladder=None, verbose=False):
        self.http = http
        self.cache = cache
        self.ladder = ladder
        self.verbose = verbose
        self.chunk_size = (
            ladder.threshold("idconv_chunk_size", IDCONV_MAX_IDS) if ladder else IDCONV_MAX_IDS
        )
        if self.chunk_size > IDCONV_MAX_IDS:
            # Configuring this above the ceiling would silently drop whole chunks.
            self.chunk_size = IDCONV_MAX_IDS
        self._primed = set()

    # -- stage 1: batched, free, unauthenticated ----------------------------

    def prime(self, identifiers: Sequence[str]) -> int:
        """Resolve DOI/PMID -> PMCID for a whole batch in chunks of 200.

        Returns the number of HTTP calls made. Records already in the cache are
        skipped, so a resumed run costs nothing here.
        """
        dois, pmids = [], []
        for raw in identifiers:
            value = str(raw or "").strip()
            if not value or self.cache.get(value):
                continue
            if value.lower().startswith("10."):
                dois.append(value)
            elif value.isdigit():
                pmids.append(value)

        calls = 0
        for idtype, values in (("doi", dois), ("pmid", pmids)):
            for chunk in _chunks(_dedupe(values), self.chunk_size):
                calls += 1
                self._idconv_chunk(chunk, idtype)
        if self.verbose and calls:
            print(f"  resolution: {calls} ID Converter call(s) for {len(dois) + len(pmids)} identifier(s)")
        self.cache.flush()
        return calls

    def _idconv_chunk(self, chunk: List[str], idtype: str) -> None:
        response = self.http.get(
            IDCONV_URL,
            params={"ids": ",".join(chunk), "format": "json", "idtype": idtype},
        )
        if not response.ok:
            if self.verbose:
                print(f"  ID Converter chunk failed: HTTP {response.status}")
            return
        for record in _safe_records(response):
            requested = record.get("requested-id") or record.get(idtype)
            if not requested:
                continue
            values = {}
            if record.get("pmcid"):
                values["pmcid"] = record["pmcid"]
            if record.get("pmid"):
                values["pmid"] = str(record["pmid"])
            if record.get("doi"):
                values["doi"] = record["doi"]
            # Cache misses too: "not in PMC" is a fact worth not re-asking for.
            values["_idconv"] = record.get("errmsg") or "ok"
            self.cache.put(requested, values)

    def resolve(self, raw_identifier: str, doi: Optional[str] = None,
                pmid: Optional[str] = None) -> IdentifierSet:
        """Stage 0 + stage 1 for one record. Cheap: usually a dict lookup."""
        ids = IdentifierSet(doi=doi, pmid=pmid)
        ids.publisher_prefix = doi_prefix(ids.doi)

        cached = self.cache.get(raw_identifier) or (self.cache.get(doi) if doi else None)
        if cached:
            ids.learn(
                "cache",
                note="stage 0 hit",
                doi=cached.get("doi") or ids.doi,
                pmid=cached.get("pmid"),
                pmcid=cached.get("pmcid"),
            )
        else:
            self._idconv_chunk(
                [raw_identifier], "doi" if str(raw_identifier).startswith("10.") else "pmid"
            )
            fresh = self.cache.get(raw_identifier) or {}
            ids.learn(
                "idconv",
                url=IDCONV_URL,
                note="single-record lookup (not primed)",
                doi=fresh.get("doi") or ids.doi,
                pmid=fresh.get("pmid"),
                pmcid=fresh.get("pmcid"),
            )

        if not ids.publisher_prefix:
            ids.publisher_prefix = doi_prefix(ids.doi)
        self._learn_free_facts(ids)
        return ids

    def _learn_free_facts(self, ids: IdentifierSet) -> None:
        """Everything derivable from the DOI string itself. No network."""
        doi = (ids.doi or "").lower()
        if not doi:
            return
        m = _ARXIV_FROM_DOI_RE.search(doi)
        if m:
            ids.learn("doi-shape", note="arXiv DOI", arxiv_id=m.group(1))
        if doi.startswith(_PREPRINT_PREFIX + "/"):
            ids.preprint_server = "unknown"   # biorxiv vs medrxiv; the API decides

    # -- lazy: only once the free T1 routes have failed ---------------------

    def crossref(self, ids: IdentifierSet) -> dict:
        """Fetch and memoize Crossref metadata for this record.

        Cheapest informative single call, but deferred: a record that
        short-circuits on a PMCID never needs it.
        """
        if "crossref" in ids.memo:
            return ids.memo["crossref"]
        if not ids.doi:
            return {}
        url = "https://api.crossref.org/works/{}".format(ids.doi)
        response = self.http.get(url, timeout=15)
        message = {}
        if response.ok:
            try:
                message = (response.json() or {}).get("message") or {}
            except ValueError:
                message = {}
        links = message.get("link") or []
        pii = None
        for link in links:
            m = _PII_RE.search(link.get("URL") or "")
            if m:
                pii = m.group(1)
                break
        ids.crossref_links = links
        licenses = message.get("license") or []
        ids.learn(
            "crossref",
            url=url,
            http_status=response.status,
            note="publisher metadata, TDM links",
            elsevier_pii=pii,
            license=(licenses[0].get("URL") if licenses else None),
            # Crossref's link[] and relation[] sometimes point at the arXiv
            # preprint of the same work.
            arxiv_id=arxiv_id_in(response.text),
        )
        ids.memo["crossref"] = message
        return message

    def unpaywall(self, ids: IdentifierSet) -> dict:
        """Fetch and memoize the Unpaywall record.

        Memoized on the IdentifierSet because two callers want it: this stage, to
        harvest an arXiv id out of oa_locations[], and the T2 source, to walk those
        same locations looking for HTML full text. Without the memo the record would
        be fetched twice for no reason.
        """
        cached = ids.memo.get("unpaywall")
        if cached is not None:
            return cached
        if not ids.doi:
            return {}
        url = "https://api.unpaywall.org/v2/{}".format(ids.doi)
        response = self.http.get(url, timeout=25)
        payload = response.json_or({}) if response.ok else {}
        ids.memo["unpaywall"] = payload
        best = payload.get("best_oa_location") or {}
        ids.learn(
            "unpaywall",
            url=url,
            http_status=response.status,
            note="oa_locations scanned for an arXiv id",
            license=best.get("license"),
            # The whole payload, not just best_oa_location: the arXiv copy is
            # usually a repository location and rarely the "best" one.
            arxiv_id=arxiv_id_in(response.text),
        )
        return payload

    def resolve_conditional(self, ids: IdentifierSet) -> IdentifierSet:
        """Stage 3. Every call gated on a cheap signal; none unconditional."""
        prefix = ids.publisher_prefix or doi_prefix(ids.doi)

        # DataCite: only for prefixes that are actually registered there.
        if prefix in DATACITE_REPOSITORY_PREFIXES and not ids.repository_id:
            self._datacite(ids)

        # OpenAlex, then Semantic Scholar: only while the PMCID is still missing,
        # since a PMID from either feeds a second ID Converter pass.
        if not ids.pmcid:
            if not ids.pmid:
                self._openalex(ids)
            if not ids.pmid:
                self._semantic_scholar(ids)
            if ids.pmid:
                self._second_idconv_pass(ids)

        # Scopus exists here for exactly one purpose: yielding an Elsevier PII.
        # Crossref's link[] already carries it inline for every Elsevier DOI
        # tested, so this only runs in the rare case where link[] was absent.
        if prefix == _ELSEVIER_PREFIX and not ids.elsevier_pii:
            self.crossref(ids)
            if not ids.elsevier_pii:
                self._scopus(ids)

        # An arXiv id unlocks two of the highest-fidelity routes there are: LaTeXML
        # HTML, whose equations carry their original LaTeX in MathML alttext, and
        # the LaTeX source itself. Crossref may already have supplied one above;
        # Unpaywall is the reliable source and its record is memoized, so the T2
        # source reuses this response rather than fetching it again.
        if not ids.arxiv_id:
            self.crossref(ids)
        if not ids.arxiv_id:
            self.unpaywall(ids)

        return ids

    def _second_idconv_pass(self, ids: IdentifierSet) -> None:
        """A PMID found at stage 3 is a second chance at the PMCID."""
        self._idconv_chunk([ids.pmid], "pmid")
        fresh = self.cache.get(ids.pmid) or {}
        ids.learn(
            "idconv#2",
            url=IDCONV_URL,
            note="retry with PMID recovered at stage 3",
            pmcid=fresh.get("pmcid"),
        )

    def _openalex(self, ids: IdentifierSet) -> None:
        url = "https://api.openalex.org/works/https://doi.org/{}".format(ids.doi)
        response = self.http.get(url, timeout=15)
        if not response.ok:
            ids.chain.record("openalex", url=url, http_status=response.status)
            return
        try:
            data = response.json() or {}
        except ValueError:
            ids.chain.record("openalex", url=url, http_status=response.status, note="unparseable")
            return
        external = data.get("ids") or {}
        pmid = _tail(external.get("pmid"))
        pmcid = _tail(external.get("pmcid"))
        # OpenAlex lists every location it knows, and for an arXiv-hosted paper one
        # of them is an arxiv.org URL. Scanning the whole payload rather than
        # walking locations[] keeps this robust to their schema moving around.
        ids.learn(
            "openalex",
            url=url,
            http_status=response.status,
            openalex_id=_tail(external.get("openalex")),
            pmid=pmid,
            pmcid=("PMC" + pmcid.lstrip("PMC")) if pmcid else None,
            arxiv_id=arxiv_id_in(response.text),
        )

    def _semantic_scholar(self, ids: IdentifierSet) -> None:
        url = "https://api.semanticscholar.org/graph/v1/paper/DOI:{}".format(ids.doi)
        response = self.http.get(url, params={"fields": "externalIds"}, timeout=15)
        if not response.ok:
            ids.chain.record("semantic_scholar", url=url, http_status=response.status)
            return
        try:
            external = (response.json() or {}).get("externalIds") or {}
        except ValueError:
            external = {}
        pmcid = external.get("PubMedCentral")
        ids.learn(
            "semantic_scholar",
            url=url,
            http_status=response.status,
            pmid=str(external["PubMed"]) if external.get("PubMed") else None,
            pmcid=("PMC" + str(pmcid).lstrip("PMC")) if pmcid else None,
            arxiv_id=external.get("ArXiv"),
        )

    def _datacite(self, ids: IdentifierSet) -> None:
        url = "https://api.datacite.org/dois/{}".format(ids.doi)
        response = self.http.get(url, timeout=15)
        if not response.ok:
            ids.chain.record("datacite", url=url, http_status=response.status)
            return
        try:
            attributes = ((response.json() or {}).get("data") or {}).get("attributes") or {}
        except ValueError:
            attributes = {}
        repository_id = None
        for candidate in (attributes.get("url") or "", ids.doi or ""):
            m = re.search(r"(?:zenodo\.|figshare\.|records?/)(\d+)", candidate, re.IGNORECASE)
            if m:
                repository_id = m.group(1)
                break
        ids.learn(
            "datacite",
            url=url,
            http_status=response.status,
            note="bridge to repository record id",
            repository_id=repository_id,
            license=attributes.get("rightsList", [{}])[0].get("rightsUri")
            if attributes.get("rightsList") else None,
        )

    def _scopus(self, ids: IdentifierSet) -> None:
        import os

        api_key = os.getenv("SCOPUS_API_KEY")
        if not api_key:
            ids.chain.record("scopus", note="skipped: SCOPUS_API_KEY unset")
            return
        url = "https://api.elsevier.com/content/abstract/doi/{}".format(ids.doi)
        response = self.http.get(
            url,
            params={"apiKey": api_key, "httpAccept": "application/json"},
            timeout=20,
        )
        pii = None
        if response.ok:
            m = re.search(r'"pii"\s*:\s*"([A-Z0-9]+)"', response.text, re.IGNORECASE)
            if m:
                pii = m.group(1)
        ids.learn(
            "scopus",
            url=url,
            http_status=response.status,
            note="fallback only: Crossref link[] had no PII",
            elsevier_pii=pii,
        )


# -- helpers ----------------------------------------------------------------


def _chunks(values: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(values), size):
        yield list(values[i:i + size])


def _dedupe(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


def _tail(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return str(url).rstrip("/").rsplit("/", 1)[-1]


def _safe_records(response) -> List[dict]:
    try:
        payload = response.json()
    except Exception:
        return []
    records = payload.get("records")
    return records if isinstance(records, list) else []
