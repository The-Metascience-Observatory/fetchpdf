"""The identifier set built by phase 1, and the audit trail of how it was built.

Resolution is separated from retrieval because a PMCID discovered via the ID
Converter is what unlocks the entire T1 tier -- and on a real clinical corpus it
does so for roughly 40% of records without touching a single quota-limited API.
Interleaving resolution with retrieval per source throws that away: you cannot
short-circuit on an identifier you have not looked up yet.

IdentifierSet is mutable rather than frozen. Resolution is incremental by nature
(stage 1 yields a PMCID, stage 3 may yield an arXiv id that feeds a second
lookup), and threading dataclasses.replace() through five resolvers obscures
what is actually happening. Mutation goes through learn(), which records it.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ResolutionStep:
    """One resolution attempt, recorded whether or not it yielded anything."""

    step: str                      # "crossref", "idconv", "openalex", ...
    url: Optional[str]             # redacted before it is ever persisted
    http_status: Optional[int]
    yielded: Dict[str, str]        # {"pmcid": "PMC123"} -- empty dict on a miss
    note: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "url": self.url,
            "http_status": self.http_status,
            "yielded": dict(self.yielded),
            "note": self.note,
            "timestamp": self.timestamp,
        }


class ResolutionChain:
    """Ordered record of every resolution attempt for one record.

    Built as resolution happens rather than reconstructed afterwards: the
    provenance requirement is to explain why a given number came from a given
    file, and "which lookup produced the identifier we retrieved by" is half of
    that answer.
    """

    def __init__(self):
        self.steps: List[ResolutionStep] = []

    def record(self, step, url=None, http_status=None, yielded=None, note=""):
        self.steps.append(
            ResolutionStep(
                step=step,
                url=url,
                http_status=http_status,
                yielded=dict(yielded or {}),
                note=note,
            )
        )

    def to_list(self) -> List[dict]:
        return [s.to_dict() for s in self.steps]

    def __len__(self):
        return len(self.steps)

    def __repr__(self):
        return f"<ResolutionChain {len(self.steps)} steps>"


#: Fields a source may declare in ladder.json under "requires".
IDENTIFIER_FIELDS = (
    "doi",
    "pmid",
    "pmcid",
    "openalex_id",
    "arxiv_id",
    "ppr_id",
    "elsevier_pii",
    "repository_id",
)


@dataclass
class IdentifierSet:
    """Everything phase 1 managed to learn about one record."""

    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    openalex_id: Optional[str] = None
    arxiv_id: Optional[str] = None
    ppr_id: Optional[str] = None          # Europe PMC preprint id (PPR…)
    elsevier_pii: Optional[str] = None
    repository_id: Optional[str] = None   # figshare/zenodo/OSF record id

    # Context that is not an identifier but gates later stages.
    publisher_prefix: Optional[str] = None   # DOI registrant prefix, e.g. "10.1016"
    preprint_server: Optional[str] = None    # "biorxiv" | "medrxiv"
    license: Optional[str] = None
    #: Europe PMC availability flags (inEPMC, isOpenAccess, hasPDF, ...), exactly
    #: what the name says. It used to double as a general scratch bag holding
    #: memoized Crossref and Unpaywall payloads, which made the name a lie about
    #: the contents; those live in `memo` now.
    epmc_flags: Dict[str, Any] = field(default_factory=dict)

    #: Memoized API payloads, so two consumers of the same record share one fetch.
    #: Keyed by source name ("crossref", "unpaywall").
    memo: Dict[str, Any] = field(default_factory=dict)
    crossref_links: List[dict] = field(default_factory=list)

    chain: ResolutionChain = field(default_factory=ResolutionChain)

    def learn(self, step: str, url=None, http_status=None, note="", **values) -> Dict[str, str]:
        """Set any identifier fields not already known, and record the attempt.

        Never overwrites a value that is already set: the first resolver to find
        an identifier wins, so a later, less trustworthy source cannot quietly
        redirect retrieval to a different paper.
        """
        gained = {}
        for key, value in values.items():
            if value in (None, "", []):
                continue
            if not hasattr(self, key):
                raise AttributeError(f"IdentifierSet has no field {key!r}")
            if getattr(self, key):
                continue
            setattr(self, key, value)
            if isinstance(value, (str, int)):
                gained[key] = str(value)
        self.chain.record(step, url=url, http_status=http_status, yielded=gained, note=note)
        return gained

    @property
    def best_id(self) -> Optional[str]:
        """The identifier a human would use to name this record."""
        return self.doi or self.pmcid or self.pmid or self.arxiv_id

    def to_dict(self) -> dict:
        return {
            f: getattr(self, f)
            for f in IDENTIFIER_FIELDS
            if getattr(self, f)
        }
