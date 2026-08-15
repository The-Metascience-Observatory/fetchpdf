"""The tiered retrieval loop: format tiers outer, sources inner.

This is the semantic core. The existing chain iterates sources and takes the
first artifact each one yields, which means the best-ranked source's PDF beats
the worst-ranked source's JATS. Here a tier is exhausted across every applicable
source before the walk descends, so structure wins over source ranking -- which
is the whole point, because a table that survives as markup is worth more than
one that has to be reconstructed no matter who served it.

Three rules hold throughout:

  * A source's declared tier is a hint. The tier that counts is the one the
    classifier reads off the bytes.
  * Validation failure demotes and continues. It never accepts silently and
    never hard-fails the record.
  * Nothing is written until an artifact has been classified and validated, so a
    rejected candidate leaves nothing on disk.
"""

import os
import tempfile
from typing import List, Optional, Tuple

from . import classify as classifier
from .artifact import Artifact, ValidationResult
from .cache import ResolutionCache
from .context import RetrievalContext
from . import goals
from .http import HttpClient
from .provenance import ProvenanceRecord, write_sidecar
from .ratelimit import HostRateLimiter
from .resolve import BatchResolver
from .tiers import ARTIFACT_EXTENSIONS, Tier, TIER_EXTENSIONS, load_ladder
from .validate import validate_t1, validate_t2


class RetrievalResult:
    """What the engine hands back to fetch_pdf.

    `path` and `artifact` stay singular and keep meaning "the best one": batch
    counting, the format tally and the missing-PDF report all read them, and
    changing what they mean would quietly alter every report. Multi-artifact walks
    expose the rest through `paths` / `artifacts` / `goals`.
    """

    def __init__(self, path=None, artifact=None, provenance=None, reason="",
                 goals=None):
        self.path = path
        self.artifact = artifact
        self.provenance = provenance
        self.reason = reason
        self.goals = goals

    def __bool__(self):
        return self.path is not None

    @property
    def paths(self) -> List[str]:
        """Every artifact written or already present, best tier first."""
        if self.goals is None:
            return [self.path] if self.path else []
        return self.goals.filled_paths()

    @property
    def artifacts(self) -> List[Artifact]:
        if self.goals is None:
            return [self.artifact] if self.artifact is not None else []
        return [g.artifact for g in self.goals if g.artifact is not None]

    def path_for(self, goal_name: str) -> Optional[str]:
        """The artifact filling a named goal, e.g. 'structured' or 'pdf'."""
        if self.goals is None:
            return None
        goal = self.goals.by_name(goal_name)
        return goal.path if goal is not None else None

    @property
    def structured_path(self) -> Optional[str]:
        return self.path_for("structured")

    @property
    def pdf_path(self) -> Optional[str]:
        return self.path_for("pdf")

    @property
    def summary(self) -> str:
        """'xml+pdf', 'pdf (structured: none)' -- for the per-record log line."""
        return self.goals.summary() if self.goals is not None else ""



def retrieve_tiered(
    raw_identifier,
    doi=None,
    pmid=None,
    save_path=None,
    target_task="extraction",
    xml_only=False,
    xml_html_only=False,
    get_xml_or_html=False,
    upgrade_existing=False,
    want_provenance=False,
    email=None,
    verbose=False,
    delay=0.1,
    use_playwright=False,
    resolver=None,
    ladder=None,
) -> RetrievalResult:
    """Walk the ladder for one record. Returns the written path, or a reason."""
    ladder = ladder or load_ladder()
    output_dir = os.path.dirname(os.path.abspath(save_path)) or "."

    owns_resolver = resolver is None
    if owns_resolver:
        # Single-record mode: a resolver of its own, so the CLI path works
        # without a batch priming it. Batch mode passes a primed one in.
        limiter = HostRateLimiter(ladder.rate_limits)
        http = HttpClient(limiter, email=email, verbose=verbose)
        resolver = BatchResolver(
            http, ResolutionCache.for_output_dir(output_dir, verbose), ladder, verbose
        )
    http = resolver.http

    ctx = RetrievalContext(
        http=http,
        resolver=resolver,
        ladder=ladder,
        save_path=save_path,
        target_task=target_task,
        verbose=verbose,
        use_playwright=use_playwright,
        email=email,
        delay=delay,
    )

    provenance = ProvenanceRecord(
        identifier=str(doi or raw_identifier),
        target_task=target_task,
        flags={
            "prioritize_xml": True,
            "xml_only": xml_only,
            "upgrade_existing": upgrade_existing,
        },
    ) if want_provenance else None
    ctx.provenance = provenance

    # -- phase 1: resolve ---------------------------------------------------
    ids = resolver.resolve(raw_identifier, doi=doi, pmid=pmid)
    ctx.scratch["ids"] = ids
    if provenance is not None:
        provenance.identifiers = ids.to_dict()

    # -- already on disk? ---------------------------------------------------
    stem = _stem_for(save_path)
    existing_path, existing_tier = _existing_artifact(stem)

    # Restricting the walk is subtraction from the configured ladder, not a
    # separate ladder: the source ordering within each rung, and which rungs
    # exist at all, stay whatever the config says.
    tiers = ladder.for_task(target_task)
    restriction = None
    keep = ()
    if xml_only:
        keep, restriction = (Tier.T1_XML,), "--xml-only"
    elif xml_html_only:
        keep, restriction = (Tier.T1_XML, Tier.T2_HTML), "--xml-html-only"
    if restriction:
        tiers = [t for t in tiers if t in keep]

    # What this walk is collecting. The default is a single "best" goal that any
    # tier fills, which reduces exactly to stop-at-first-hit -- one code path, not
    # a fork.
    goalset = goals.build(tiers, get_xml_or_html=get_xml_or_html)

    # Pre-filling from disk is how a goal gets skipped, which is exactly wrong
    # under --upgrade-existing: that flag means "look for something better than
    # what is here". Pre-filling would leave no unfilled goals, wanted_tiers()
    # would come back empty, and the walk would skip every tier and upgrade
    # nothing. The rank check inside the loop is what stops the walk descending
    # below what is already on disk.
    if not upgrade_existing:
        _fill_goals_from_disk(goalset, stem, ctx)

    if goalset.complete and not upgrade_existing:
        found = ", ".join(os.path.basename(p) for p in goalset.filled_paths())
        ctx.log(f"✅ Skipping {ids.best_id} - already exists: {found}")
        return RetrievalResult(
            path=goalset.filled_paths()[0], reason="already exists", goals=goalset,
        )

    # -- phase 2: retrieve --------------------------------------------------
    stage3_done = False
    for tier in tiers:
        if goalset.complete:
            break
        if tier not in goalset.wanted_tiers():
            # No unfilled goal accepts this tier. Under --get-xml-or-html that
            # skips T3/T4/T7 outright: a LaTeX tarball or a landing page fills
            # neither the structured nor the PDF slot, so trying them would only
            # cost time.
            continue
        if (
            upgrade_existing
            and existing_tier is not None
            and _rank_of(tier, tiers) >= _rank_of(existing_tier, tiers)
        ):
            # Everything from here down is no better than what is already on
            # disk, so there is nothing left worth fetching.
            ctx.log(
                f"    stopping at {tier.name}: not better than existing "
                f"{existing_tier.name} artifact"
            )
            break

        for spec in ladder.sources_for(tier):
            if not spec.applicable(ids):
                if not stage3_done:
                    # Conditional resolution, run at most once and only when a
                    # source actually wants an identifier we do not have. A
                    # record that short-circuits at T1 never reaches this.
                    ctx.log("    running conditional resolution (stage 3)")
                    resolver.resolve_conditional(ids)
                    stage3_done = True
                    if provenance is not None:
                        provenance.identifiers = ids.to_dict()
                if not spec.applicable(ids):
                    continue

            artifact = _run_source(spec, ids, ctx)
            if artifact is None:
                continue

            # Guarded for the same reason _run_source is. A validator is just
            # code, and a crash in one used to escape the record, escape
            # download_one, and take down the whole batch at future.result() --
            # thousands of records lost to one malformed page. "Demote and
            # continue" has to survive our own bugs, not just the network's.
            try:
                accepted, validation = _judge(artifact, tier, ctx, tiers, goalset, ids=ids)
            except Exception as e:
                accepted = False
                validation = ValidationResult.failure(
                    f"validation raised: {type(e).__name__}: {str(e)[:120]}"
                )
                ctx.log(f"    ✗ {spec.name}: {validation.reason}")
            if provenance is not None:
                provenance.attempt(
                    source=spec.name,
                    tier_attempted=tier.name,
                    url=artifact.url,
                    http_status=artifact.http_status,
                    served_content_type=artifact.served_content_type,
                    accepted=accepted,
                    outcome="accepted" if accepted else validation.reason,
                    checks_passed=list(validation.checks_passed),
                )
            if not accepted:
                ctx.log(f"    ✗ {spec.name}: {validation.reason}")
                continue

            goal = goalset.goal_for(artifact.tier)
            path = _accept(artifact, stem, ctx, provenance)
            goal.fill(path, artifact)
            ctx.log(
                f"    ✓ {spec.name} -> {artifact.tier.name} "
                f"{os.path.basename(path)} [{goal.name}]"
            )
            if existing_path and existing_path != path:
                # The superseded file is left in place: deleting a PDF someone
                # may already have annotated or indexed is not this tool's call.
                # The sidecar records which artifact is authoritative.
                ctx.log(f"      superseded {os.path.basename(existing_path)} (left on disk)")
                if provenance is not None:
                    provenance.note(
                        f"supersedes {os.path.basename(existing_path)} "
                        f"({existing_tier.name}); superseded file left on disk"
                    )
            # This goal is filled, so no other source at this tier can help it.
            # Move to the next tier, where a different goal may still be open.
            break

        if goalset.complete:
            break

    if goalset.any_filled:
        wrote_something = any(g.filled and not g.preexisting for g in goalset)
        if wrote_something:
            _write_sidecar_once(provenance, goalset, stem, ctx)
        _flush(resolver, owns_resolver)
        best = goalset.filled_paths()[0]
        best_artifact = next((g.artifact for g in goalset if g.artifact is not None), None)
        unfilled = [g.name for g in goalset.unfilled()]

        if not wrote_something:
            # Every filled goal was already on disk and nothing better turned up.
            # Under --upgrade-existing that is the expected outcome for most
            # records, and calling it a failure would fill failed_dois.csv with
            # records that are perfectly fine.
            reason = "kept existing {}".format(
                existing_tier.name if existing_tier else "artifact"
            )
            ctx.log("    no better tier than the existing artifact; keeping it")
        elif unfilled:
            reason = "unfilled: " + ", ".join(unfilled)
        else:
            reason = ""

        return RetrievalResult(
            path=best,
            artifact=best_artifact,
            provenance=provenance,
            goals=goalset,
            reason=reason,
        )

    _write_provenance_only(provenance, stem, ctx)
    _flush(resolver, owns_resolver)

    if restriction:
        reason = "{}: no validated {} artifact".format(
            restriction,
            "/".join(t.name for t in tiers),
        )
        ctx.log(f"    {reason}")
        if not existing_path:
            return RetrievalResult(reason=reason, provenance=provenance)

    if existing_path:
        # Under --upgrade-existing, "nothing better was available" is the
        # expected outcome for most records. Reporting it as a failure would
        # fill failed_dois.csv with records that are perfectly fine.
        ctx.log(f"    no better tier than the existing {existing_tier.name}; keeping it")
        return RetrievalResult(path=existing_path,
                               reason=f"kept existing {existing_tier.name}")

    return RetrievalResult(reason="no tier produced a validated artifact",
                           provenance=provenance)


# -- the three rules --------------------------------------------------------


def _run_source(spec, ids, ctx) -> Optional[Artifact]:
    """Call a source. A source that raises demotes; it never kills the record."""
    try:
        return spec.resolve()(ids, ctx)
    except Exception as e:
        ctx.log(f"    ✗ {spec.name} raised: {str(e)[:150]}")
        if ctx.provenance is not None:
            ctx.provenance.attempt(
                source=spec.name,
                tier_attempted="unknown",
                accepted=False,
                outcome=f"source raised: {str(e)[:150]}",
            )
        return None


def _judge(artifact: Artifact, expected: Tier, ctx, order: List[Tier],
           goalset=None, ids=None) -> Tuple[bool, ValidationResult]:
    """Classify by content, then validate against the tier the content is.

    "Better" means earlier in the active ladder, not a lower Tier number. The
    two differ: screening ranks plain text above PDF, extraction excludes it
    entirely. Comparing Tier ints instead would hardcode the extraction ordering
    into the accept rule and silently discard a perfectly good screening
    artifact.
    """
    actual = classifier.classify(artifact.content, declared=artifact.tier)
    if actual != artifact.tier:
        artifact.declared_tier = artifact.tier
        artifact.tier = actual
        ctx.log(
            f"      reclassified {artifact.source}: declared "
            f"{artifact.declared_tier.name}, content is {actual.name} "
            f"({classifier.describe(artifact.content)})"
        )

    # Not in this ladder at all: for extraction that is how plain text gets
    # refused outright rather than ranked last and degraded into.
    if artifact.tier not in order:
        return False, ValidationResult.failure(
            f"{artifact.tier.name} is not in the {ctx.target_task} ladder"
        )

    # A source that promised T1 and delivered T5 must not be accepted at this
    # rung -- otherwise the ladder's ordering means nothing. It is dropped here
    # and the rung that does match picks it up on its own pass.
    #
    # Better-than-expected is the opposite case and must be kept. The legacy
    # chain at T5 has its own Elsevier XML fallback, so it sometimes hands back
    # a T1 document; every rung above has already been tried and failed, so
    # finding structured full text late is a windfall, not a violation. Dropping
    # it is how one record with usable full text ended up saved as a landing page.
    if order.index(artifact.tier) > order.index(expected):
        return False, ValidationResult.failure(
            f"content is {artifact.tier.name}, not {expected.name}"
        )

    # Nothing left to put it in. Under --get-xml-or-html this is what stops a
    # second XML artifact overwriting the first one, and it is recorded as a
    # demotion reason rather than a silent drop.
    if goalset is not None and goalset.goal_for(artifact.tier) is None:
        return False, ValidationResult.failure(
            f"no unfilled goal accepts {artifact.tier.name}"
        )

    validation = _validate_for_tier(artifact, ctx, ids)
    artifact.validation = validation
    return validation.ok, validation


def _validate_for_tier(artifact: Artifact, ctx, ids=None) -> ValidationResult:
    tier = artifact.tier
    if tier == Tier.T1_XML:
        return validate_t1(
            artifact.content,
            min_chars=ctx.threshold("t1_min_chars", 5000),
            abstract_stub_chars=ctx.threshold("abstract_stub_chars", 2500),
        )
    if tier == Tier.T2_HTML:
        return validate_t2(
            artifact.content,
            min_chars=ctx.threshold("t2_min_chars", 5000),
            abstract_stub_chars=ctx.threshold("abstract_stub_chars", 2500),
            min_populated_td=ctx.threshold("min_populated_td", 1),
            url=artifact.url,
            doi=(ids.doi if ids is not None else None),
        )
    if tier == Tier.T3_SOURCE:
        if len(artifact.content) < 512:
            return ValidationResult.failure("source archive implausibly small")
        return ValidationResult(ok=True, checks_passed=["archive-magic", "min-size"])
    if tier == Tier.T4_SUPPLEMENT:
        if len(artifact.content) < 64:
            return ValidationResult.failure("supplement implausibly small")
        return ValidationResult(ok=True, checks_passed=["structured-file", "min-size"])
    if tier == Tier.T5_PDF:
        if not artifact.content.startswith(b"%PDF"):
            return ValidationResult.failure("not a PDF despite T5 classification")
        if len(artifact.content) < 1024:
            return ValidationResult.failure("PDF implausibly small")
        return ValidationResult(ok=True, checks_passed=["pdf-magic", "min-size"])
    if tier == Tier.T6_PLAINTEXT:
        text = artifact.content.decode("utf-8", errors="replace")
        minimum = ctx.threshold("abstract_stub_chars", 2500)
        if len(text.strip()) < minimum:
            return ValidationResult.failure(
                f"plain text below stub threshold ({len(text.strip())} < {minimum})",
                n_chars=len(text.strip()),
            )
        return ValidationResult(
            ok=True, checks_passed=["min-chars"], n_chars=len(text.strip())
        )
    # T7: a landing page is accepted as what it is, and marked as holding no tables.
    if not artifact.content:
        return ValidationResult.failure("empty landing page")
    return ValidationResult(ok=True, checks_passed=["non-empty"])


def _accept(artifact: Artifact, stem: str, ctx, provenance) -> str:
    """Normalize, chunk, write the artifact, then the sidecar."""
    chunks, canonical_tokens = _normalize(artifact, ctx)
    path = stem + artifact.suffix
    _write_atomic(path, artifact.content)

    if provenance is not None:
        provenance.resolution_chain = _chain_of(ctx)
        provenance.accept(artifact, path, chunks=chunks, canonical_tokens=canonical_tokens)
    return path


def _normalize(artifact: Artifact, ctx) -> Tuple[list, int]:
    """Canonical tables for the structured tiers. Deterministic, no model."""
    from .chunk import chunks_from_html, chunks_from_jats
    from .normalize import strip_boilerplate, token_count

    chunks: List = []
    try:
        if artifact.tier == Tier.T1_XML:
            chunks = chunks_from_jats(artifact.content, artifact.source)
        elif artifact.tier == Tier.T2_HTML:
            chunks = chunks_from_html(artifact.content, artifact.source)
            _, ratio, failures = strip_boilerplate(
                artifact.content.decode("utf-8", errors="replace")
            )
            artifact.extra["boilerplate_token_ratio"] = round(ratio, 3)
            artifact.normalization_failures.extend(failures)
    except Exception as e:
        artifact.normalization_failures.append(f"normalization failed: {str(e)[:120]}")
        return [], 0

    for chunk in chunks:
        artifact.normalization_failures.extend(chunk.normalization_failures)
    canonical_tokens = sum(token_count(c.as_block()) for c in chunks)
    return chunks, canonical_tokens


def _chain_of(ctx) -> list:
    ids = ctx.scratch.get("ids")
    return ids.chain.to_list() if ids is not None else []


# -- disk -------------------------------------------------------------------


def _stem_for(save_path: str) -> str:
    """The path with any artifact suffix removed.

    Handles the multi-part suffixes (.source.tar.gz, .fulltext.html) that
    os.path.splitext would only half-strip.
    """
    for suffix in sorted(ARTIFACT_EXTENSIONS, key=len, reverse=True):
        if save_path.endswith(suffix):
            return save_path[: -len(suffix)]
    return os.path.splitext(save_path)[0]


def _existing_artifact(stem: str) -> Tuple[Optional[str], Optional[Tier]]:
    """The best artifact already on disk for this record, if any.

    Iterating TIER_EXTENSIONS rather than Tier because not every tier writes a
    file, so `for tier in sorted(Tier)` would KeyError.
    """
    for tier in sorted(TIER_EXTENSIONS):
        path = stem + TIER_EXTENSIONS[tier]
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path, tier
    return None, None


def _rank_of(tier: Tier, tiers: List[Tier]) -> int:
    """Position on the ACTIVE ladder -- lower is better.

    Not the Tier int: the screening ladder ranks T6 above T5, so comparing ints
    would let a T5 PDF overwrite a better T6 screening artifact under
    --upgrade-existing. This is the same argument _judge's docstring already
    makes, applied to the one place that was still comparing ints.

    A preprint is ranked where the T8 rung sits even when that rung is not being
    walked, so --upgrade-existing without --alt-version cannot "upgrade" a
    preprint into a landing page.
    """
    if tier in tiers:
        return tiers.index(tier)
    return len(tiers)     # not on this ladder at all: anything beats it


def _write_atomic(path: str, content: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-w-", dir=directory)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(content)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _flush(resolver, owns_resolver: bool) -> None:
    """Persist the resolution cache when this call owns it.

    Batch mode flushes once at the end instead: flushing per record would
    rewrite a file that grows with every row, which is quadratic over a batch.
    """
    if owns_resolver:
        resolver.cache.flush()


def _fill_goals_from_disk(goalset, stem: str, ctx) -> None:
    """Pre-fill any goal whose artifact is already on disk.

    Per goal, not per record: that is what turns --get-xml-or-html into a backfill
    over an existing PDF inventory. A directory full of PDFs has its `pdf` goal
    satisfied and only the structured half fetched, and no PDF is touched.
    """
    for goal in goalset:
        for tier in goal.tiers:
            suffix = TIER_EXTENSIONS.get(tier)
            if not suffix:
                continue
            path = stem + suffix
            if os.path.exists(path) and os.path.getsize(path) > 0:
                goal.fill(path, artifact=None, preexisting=True)
                ctx.log(f"    {goal.name}: already on disk ({os.path.basename(path)})")
                break


def _write_sidecar_once(provenance, goalset, stem: str, ctx) -> None:
    """One sidecar per record, keyed on the best artifact's stem.

    Written after the walk rather than inside _accept: a two-artifact record has one
    audit trail describing both, not two files each telling half the story.
    """
    if provenance is None:
        return
    provenance.resolution_chain = _chain_of(ctx)
    paths = goalset.filled_paths()
    if paths:
        write_sidecar(provenance, paths[0], verbose=ctx.verbose)


def _write_provenance_only(provenance, stem: str, ctx) -> None:
    """Record a failed record too: 'tried and found nothing' is an audit fact."""
    if provenance is None:
        return
    provenance.resolution_chain = _chain_of(ctx)
    write_sidecar(provenance, stem + ".pdf", verbose=ctx.verbose)
