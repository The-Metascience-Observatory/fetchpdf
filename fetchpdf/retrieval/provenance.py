"""The per-record audit sidecar.

The requirement this satisfies is not "nice logging". It is: given a number in
an extraction output, reconstruct why it came from that file -- which source
served it, which identifier got us there, which validation checks it passed, and
what we tried and rejected first.

That last part is why `attempts` records demotions as well as the accepted
artifact. "We used the PDF" is not an audit trail; "the Europe PMC XML was a
publisher denial stub and the Crossref TDM link 403'd, so we used the PDF" is.

Written only when --provenance is passed. Without it no sidecar appears, so an
existing output directory gains no new files.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .http import redact
from ._util import iso as _iso

SIDECAR_SUFFIX = ".provenance.json"
_SCHEMA_VERSION = 2


@dataclass
class Attempt:
    """One source tried, accepted or demoted."""

    source: str
    tier_attempted: str
    url: Optional[str] = None
    http_status: Optional[int] = None
    served_content_type: str = ""
    accepted: bool = False
    outcome: str = ""                       # why it was demoted, or "accepted"
    checks_passed: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "tier_attempted": self.tier_attempted,
            "url": redact(self.url),
            "http_status": self.http_status,
            "served_content_type": self.served_content_type,
            "accepted": self.accepted,
            "outcome": self.outcome,
            "checks_passed": list(self.checks_passed),
            "timestamp": _iso(self.timestamp),
        }


class ProvenanceRecord:
    """Accumulates everything about one record's retrieval."""

    def __init__(self, identifier: str, target_task: str, flags: Optional[dict] = None):
        self.identifier = identifier
        self.target_task = target_task
        self.flags = dict(flags or {})
        self.attempts: List[Attempt] = []
        self.started_at = time.time()
        self.artifact: Optional[dict] = None   # the best one, for single-artifact readers
        self.artifacts: List[dict] = []
        self.resolution_chain: List[dict] = []
        self.identifiers: Dict[str, str] = {}
        self.tables: List[dict] = []
        self.notes: List[str] = []

    def attempt(self, **kwargs) -> Attempt:
        record = Attempt(**kwargs)
        self.attempts.append(record)
        return record

    def note(self, message: str) -> None:
        self.notes.append(message)

    def accept(self, artifact, path: str, chunks=None, canonical_tokens: int = 0) -> None:
        """Record an accepted artifact.

        Appends. A --get-xml-or-html walk accepts two (a structured copy and the
        PDF), and an audit record for such a record has to describe both -- which
        one a number came from is the question this file exists to answer.
        `self.artifact` stays pointing at the first, best one for readers that
        expect a single entry.
        """
        validation = artifact.validation
        entry = {
            "path": os.path.basename(path),
            "source": artifact.source,
            "tier": artifact.tier.name,
            "tier_value": int(artifact.tier),
            "tier_declared_by_source": (
                artifact.declared_tier.name if artifact.declared_tier else artifact.tier.name
            ),
            "tier_assigned_by": "content inspection",
            "why_this_tier": {
                "checks_passed": list(validation.checks_passed) if validation else [],
                "reason": validation.reason if validation else "",
            },
            "served_content_type": artifact.served_content_type,
            "identifier_used": artifact.identifier_used,
            "request_url": redact(artifact.url),
            "http_status": artifact.http_status,
            "retrieved_at": _iso(time.time()),
            "content_hash": artifact.content_hash,
            "content_bytes": len(artifact.content),
            "license": artifact.license,
            "table_count": validation.n_tables if validation else 0,
            "footnote_count": validation.n_footnotes if validation else 0,
            "populated_cell_count": validation.n_populated_cells if validation else 0,
            "canonical_token_count": canonical_tokens,
            "tables_unavailable": artifact.tables_unavailable,
            "normalization_failures": list(artifact.normalization_failures),
        }
        self.artifacts.append(entry)
        if self.artifact is None:
            self.artifact = entry
        if chunks:
            self.tables.extend(c.to_dict() for c in chunks)
        # Zero tables in a trial paper is a finding, not a normal outcome -- but
        # only where we actually counted. A PDF's table count is 0 because
        # nothing here parses PDFs, which is not the same claim at all.
        countable = artifact.tier.name in ("T1_XML", "T2_HTML")
        if countable and entry["table_count"] == 0 and not artifact.tables_unavailable:
            self.note("zero tables in accepted artifact -- verify this paper has none")

    def to_dict(self) -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "identifier": self.identifier,
            "target_task": self.target_task,
            "flags": self.flags,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(time.time()),
            "identifiers_resolved": dict(self.identifiers),
            "resolution_chain": [
                {**step, "url": redact(step.get("url"))} for step in self.resolution_chain
            ],
            "attempts": [a.to_dict() for a in self.attempts],
            "artifact": self.artifact,
            "artifacts": list(self.artifacts),
            "tables": self.tables,
            "notes": list(self.notes),
        }


def sidecar_path_for(artifact_path: str) -> str:
    """Sidecar sits beside the artifact, keyed on the same stem."""
    stem = artifact_path
    for suffix in (".source.tar.gz", ".fulltext.html", ".landing.html", ".suppl.zip"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)] + SIDECAR_SUFFIX
    return os.path.splitext(stem)[0] + SIDECAR_SUFFIX


def write_sidecar(record: ProvenanceRecord, artifact_path: str, verbose: bool = False) -> Optional[str]:
    """Write the sidecar atomically. Never raises into the retrieval path."""
    path = sidecar_path_for(artifact_path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".fetchpdf-prov-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        if verbose:
            print(f"  could not write provenance sidecar: {e}")
        return None
    return path



