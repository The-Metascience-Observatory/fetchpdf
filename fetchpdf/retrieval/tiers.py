"""The format tier ladder, as declarative config rather than scattered branches.

Tier ordering is the whole point of this package, so it is a first-class
IntEnum rather than a lookup table that can drift out of sync with the ladder.

Note that the enum's *numeric* order is the extraction preference, not a
universal one: the screening ladder ranks T6 above T5, so "better than what is
on disk" (which --upgrade-existing needs) is a position on the ACTIVE ladder,
not an int comparison. See engine._rank_of.

The ladder itself, the tier->sources map and the per-host rate limits live in
ladder.json so they can be reordered without editing logic. Set FETCHPDF_LADDER
to a path to override the shipped file wholesale.

JSON, not TOML: tomllib landed in 3.11 and pyproject declares
requires-python = ">=3.8". A tomli dependency to read our own config would be a
poor trade.
"""

import json
import os
from enum import IntEnum
from typing import Callable, Dict, List, Optional


class Tier(IntEnum):
    """Format tiers, ordered by how much table structure survives to the model.

    Lower is better. The numbering is load-bearing -- see module docstring.
    """

    T1_XML = 1          # JATS/TEI. Cells, spans, headers, footnotes are markup.
    T2_HTML = 2         # Publisher HTML. Real <table>; usually generated from the same JATS.
    T3_SOURCE = 3       # LaTeX/e-print. Lossless in principle, macro-hostile in practice.
    T4_SUPPLEMENT = 4   # .xlsx/.csv/.tsv/.docx. Often the underlying data; partial coverage.
    T5_PDF = 5          # Structure must be reconstructed. Undetectable numeric corruption.
    T6_PLAINTEXT = 6    # Flattened. Destroys row/column association SILENTLY.
    T7_LANDING = 7      # Landing page / abstract-only / search fallback.


#: Default on-disk suffix per tier. Sources may override via Artifact.extension
#: (a supplement may legitimately be .xlsx rather than the .suppl.zip default).
TIER_EXTENSIONS = {
    Tier.T1_XML: ".xml",
    Tier.T2_HTML: ".fulltext.html",
    Tier.T3_SOURCE: ".source.tar.gz",
    Tier.T4_SUPPLEMENT: ".suppl.zip",
    Tier.T5_PDF: ".pdf",
    Tier.T6_PLAINTEXT: ".txt",
    Tier.T7_LANDING: ".landing.html",
}

#: Every suffix the retrieval layer may write, best tier first: the
#: skip-if-exists check takes the first hit.
ARTIFACT_EXTENSIONS = [TIER_EXTENSIONS[t] for t in sorted(TIER_EXTENSIONS)]


_LADDER_FILENAME = "ladder.json"


class LadderConfigError(ValueError):
    """Raised at load time for a malformed ladder config.

    Deliberately fatal: a typo that silently drops a tier would not surface
    until some record thousands of rows into a batch quietly came back as a PDF.
    """


class SourceSpec:
    """One configured source: what it can serve, and what it needs to run."""

    def __init__(self, name, spec):
        self.name = name
        self.callable_ref = spec["callable"]          # "module:function"
        self.requires = tuple(spec.get("requires", ()))  # IdentifierSet fields
        self.enabled = bool(spec.get("enabled", True))
        self.notes = spec.get("notes", "")
        self._fn = None

    def applicable(self, ids) -> bool:
        """True when every identifier this source needs has been resolved."""
        if not self.enabled:
            return False
        return all(getattr(ids, field, None) for field in self.requires)

    def resolve(self):
        """Import and cache the source callable. Raises LadderConfigError if absent."""
        if self._fn is not None:
            return self._fn
        module_name, _, func_name = self.callable_ref.partition(":")
        if not module_name or not func_name:
            raise LadderConfigError(
                f"source {self.name!r}: callable must be 'module:function', "
                f"got {self.callable_ref!r}"
            )
        import importlib

        try:
            module = importlib.import_module(
                f"{__package__}.sources.{module_name}"
            )
        except ImportError as e:
            raise LadderConfigError(
                f"source {self.name!r}: cannot import sources.{module_name} ({e})"
            )
        fn = getattr(module, func_name, None)
        if fn is None or not callable(fn):
            raise LadderConfigError(
                f"source {self.name!r}: sources.{module_name} has no callable {func_name!r}"
            )
        self._fn = fn
        return fn

    def __repr__(self):
        return f"<SourceSpec {self.name} -> {self.callable_ref}>"


class Ladder:
    """A loaded, validated ladder config."""

    def __init__(self, raw: dict, path: Optional[str] = None):
        self.path = path
        self.raw = raw
        self.sources: Dict[str, SourceSpec] = {
            name: SourceSpec(name, spec) for name, spec in raw.get("sources", {}).items()
        }
        self.tier_sources: Dict[Tier, List[str]] = {}
        for tier_name, source_names in raw.get("tier_sources", {}).items():
            self.tier_sources[_tier_by_name(tier_name)] = list(source_names)
        self.ladders: Dict[str, List[Tier]] = {}
        for task, tier_names in raw.get("ladders", {}).items():
            self.ladders[task] = [_tier_by_name(n) for n in tier_names]
        self.rate_limits: Dict[str, dict] = raw.get("rate_limits", {})
        self.thresholds: Dict[str, int] = raw.get("thresholds", {})

    def for_task(self, target_task: str) -> List[Tier]:
        """The ordered tier walk for a target task."""
        try:
            return self.ladders[target_task]
        except KeyError:
            raise LadderConfigError(
                f"unknown --target-task {target_task!r}; "
                f"configured: {sorted(self.ladders)}"
            )

    def sources_for(self, tier: Tier) -> List[SourceSpec]:
        """Configured, enabled sources for a tier, in declared order."""
        return [
            self.sources[name]
            for name in self.tier_sources.get(tier, [])
            if self.sources[name].enabled
        ]

    def threshold(self, key: str, default: int) -> int:
        return int(self.thresholds.get(key, default))

    def validate(self, resolve_callables: bool = True) -> None:
        """Fail loudly on a config that would silently misbehave at runtime."""
        if not self.ladders:
            raise LadderConfigError("no ladders configured")
        for task, tiers in self.ladders.items():
            if not tiers:
                raise LadderConfigError(f"ladder {task!r} is empty")
            if len(set(tiers)) != len(tiers):
                raise LadderConfigError(f"ladder {task!r} lists a tier twice")
        for tier, names in self.tier_sources.items():
            for name in names:
                if name not in self.sources:
                    raise LadderConfigError(
                        f"tier {tier.name} references undefined source {name!r}"
                    )
        # A source defined but wired to no tier is dead config -- almost always a
        # rename that only got applied in one place.
        wired = {n for names in self.tier_sources.values() for n in names}
        orphans = sorted(set(self.sources) - wired)
        if orphans:
            raise LadderConfigError(f"sources defined but not wired to any tier: {orphans}")
        if resolve_callables:
            for spec in self.sources.values():
                if spec.enabled:
                    spec.resolve()


def _tier_by_name(name: str) -> Tier:
    try:
        return Tier[name]
    except KeyError:
        raise LadderConfigError(
            f"unknown tier {name!r}; valid: {[t.name for t in Tier]}"
        )


#: Hooks that may contribute sources to every ladder loaded after they register.
#: Empty in a plain install -- an optional pack fills it at its own import time,
#: which is what lets a pack add a rung without ladder.json naming it. JSON
#: carries no comment syntax, so a config file cannot hold an optional section
#: the way a module can.
LADDER_EXTENSIONS: List[Callable[["Ladder"], None]] = []


def register_ladder_extension(hook: Callable[["Ladder"], None]) -> None:
    """Register a hook run against each freshly loaded Ladder, before validation.

    Before rather than after so a malformed contribution fails at load like any
    other config error, instead of surfacing thousands of records into a batch.
    Idempotent: registering the same hook twice would wire its sources twice.
    """
    if hook not in LADDER_EXTENSIONS:
        LADDER_EXTENSIONS.append(hook)


def default_ladder_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _LADDER_FILENAME)


def load_ladder(path: Optional[str] = None, validate: bool = True) -> Ladder:
    """Load the ladder config.

    Precedence: explicit path > $FETCHPDF_LADDER > the shipped ladder.json.
    """
    path = path or os.getenv("FETCHPDF_LADDER") or default_ladder_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise LadderConfigError(f"cannot read ladder config {path}: {e}")
    except json.JSONDecodeError as e:
        raise LadderConfigError(f"ladder config {path} is not valid JSON: {e}")
    ladder = Ladder(raw, path=path)
    for extend in LADDER_EXTENSIONS:
        extend(ladder)
    if validate:
        ladder.validate()
    return ladder
