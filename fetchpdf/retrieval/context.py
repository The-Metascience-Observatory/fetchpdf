"""What a source is handed when the engine calls it.

Its own module so `sources.*` can depend on it without importing the engine,
which imports the sources back through the ladder config.
"""

from dataclasses import dataclass, field
from typing import Optional

from .tiers import Ladder, Tier


@dataclass
class RetrievalContext:
    """Per-record retrieval state shared with every source."""

    http: object                       # http.HttpClient
    resolver: object                   # resolve.BatchResolver
    ladder: Ladder
    save_path: str                     # the .pdf path the caller asked for
    target_task: str = "extraction"
    verbose: bool = False
    use_playwright: bool = False
    email: Optional[str] = None
    delay: float = 0.1
    provenance: Optional[object] = None   # provenance.ProvenanceRecord or None

    #: --download-data-artifacts. Off, the linked-artifact providers route only
    #: the paper's own material (CLASS_OWNED) and record everything else in the
    #: sidecar without fetching it. On, they also route `related` deposits --
    #: which is where the actual research data lives: in one 44-sidecar corpus
    #: every `owned` link was a BioStudies mirror of supplementary files already
    #: fetched, while the Zenodo and Dataverse replication packages were all
    #: classified `related`.
    download_data_artifacts: bool = False

    #: Per-record ceiling for those artifacts, kept separate from the
    #: supplementary budget so one large dataset cannot starve the paper's own
    #: SI (or the reverse). None means the default in supplementary.py.
    max_data_artifact_bytes: Optional[int] = None

    #: --download-related-unverified. Off, a `related` deposit is skipped when
    #: its own DataCite record declares it belongs to a DIFFERENT article. On,
    #: every index assertion is taken at face value -- which is how "Tying
    #: Odysseus to the Mast" ended up filed under a savings-reminder megastudy.
    download_related_unverified: bool = False



    #: Set once a T1 artifact has been accepted for this record. NCBI efetch and
    #: Europe PMC serve the same underlying PMC JATS, so storing both is not
    #: corroboration -- it manufactures false agreement downstream.
    pmc_xml_already_taken: bool = False

    #: Sources may leave breadcrumbs for later sources in the same walk.
    scratch: dict = field(default_factory=dict)

    def log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def threshold(self, key: str, default: int) -> int:
        return self.ladder.threshold(key, default)

    def is_extraction(self) -> bool:
        return self.target_task == "extraction"


__all__ = ["RetrievalContext", "Tier"]
