"""What a source returns, and what a validator says about it.

Kept in its own module so sources, validators, the engine and the provenance
writer can all share these types without importing each other.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tiers import TIER_EXTENSIONS, Tier


@dataclass
class ValidationResult:
    """The outcome of a tier gate, and enough detail to say WHY.

    `reason` is populated on failure and on success alike: provenance has to
    record which checks passed, not merely that some unnamed check did.
    """

    ok: bool
    reason: str = ""
    checks_passed: List[str] = field(default_factory=list)
    n_chars: int = 0
    n_tables: int = 0
    n_footnotes: int = 0
    n_populated_cells: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "checks_passed": list(self.checks_passed),
            "n_chars": self.n_chars,
            "n_tables": self.n_tables,
            "n_footnotes": self.n_footnotes,
            "n_populated_cells": self.n_populated_cells,
        }

    @classmethod
    def failure(cls, reason: str, **kwargs) -> "ValidationResult":
        return cls(ok=False, reason=reason, **kwargs)


@dataclass
class Artifact:
    """One retrieved candidate, before or after validation.

    `tier` is what the source expected to produce. The engine overwrites it with
    the tier the classifier assigns from the bytes themselves -- a source that
    advertises XML and returns an HTML error page is reclassified, not trusted.
    """

    content: bytes
    tier: Tier
    source: str
    url: str
    http_status: int
    served_content_type: str = ""
    identifier_used: str = ""
    license: Optional[str] = None
    extension: Optional[str] = None
    validation: Optional[ValidationResult] = None
    tables_unavailable: bool = False
    declared_tier: Optional[Tier] = None      # what the source claimed, if reclassified
    normalization_failures: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)



    @property
    def suffix(self) -> str:
        return self.extension or TIER_EXTENSIONS[self.tier]

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()

    def __len__(self):
        return len(self.content)

    def __repr__(self):
        return (
            f"<Artifact {self.source} {self.tier.name} "
            f"{len(self.content)}B status={self.http_status}>"
        )
