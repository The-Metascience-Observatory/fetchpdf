#!/usr/bin/env python3
"""Fetch into a corpus laid out as one subfolder per record.

fetchpdf writes flat into a single -o directory. A corpus like
agent_for_forensic_metascience/high_value_pdfs_to_run_on is laid out the other
way: one directory per paper, named with the DOI-safe stem, already holding the
PDF and whatever a previous pipeline derived from it. This bridges the two by
running one fetchpdf batch per subfolder with -o set to that subfolder, so
artifacts land where they belong and nothing needs moving afterwards.

The cost is one ID Converter call per record instead of one per 200. On a corpus
of ~100 that is around 30 seconds, and it buys a corpus where every folder is
self-contained and independently resumable -- kill this halfway and re-running
picks up exactly where it stopped, because each folder carries its own
resolution cache and its own skip-if-exists state.

The PDFs already being present is not a problem, it is the point:
--get-xml-or-html pre-fills the `pdf` goal from disk, so each record fetches only
the structured half it is missing and re-downloads nothing.
"""

import argparse
import csv
import io
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# The files a previous grobid/docling pass left behind. Deliberately an explicit
# list rather than a glob: `forensic_review*/` in these folders holds analysis
# output (reports, tool-call logs, sandbox audits) that is expensive to
# regenerate, and a glob would eventually eat it.
PDF4LLM_DERIVATIVES = (
    "body.md",
    "abstract.md",
    "references.md",
    "references.json",
    "provenance.json",
)


class _ThreadRoutedStdout:
    """Give each worker thread its own stdout buffer.

    contextlib.redirect_stdout swaps a PROCESS-global, so with workers > 1 the
    threads clobber each other's redirects and their logs interleave into
    whichever buffer happened to be installed last. Routing per thread keeps each
    record's output intact, which matters because that log is the only place a
    failure's reason appears.
    """

    def __init__(self, fallback):
        self.fallback = fallback
        self._local = threading.local()

    def claim(self):
        self._local.buffer = io.StringIO()
        return self._local.buffer

    def release(self) -> str:
        buffer = getattr(self._local, "buffer", None)
        self._local.buffer = None
        return buffer.getvalue() if buffer is not None else ""

    def write(self, text):
        buffer = getattr(self._local, "buffer", None)
        (buffer or self.fallback).write(text)

    def flush(self):
        self.fallback.flush()



def safe_stem(doi: str) -> str:
    """The folder name for a DOI, using the package's own encoder.

    Not a local reimplementation: the encoding has escapes for ':' and multi-
    segment DOIs, and a second copy of those rules would drift.
    """
    from fetchpdf.fetchpdf import doi_to_safe_filename

    return doi_to_safe_filename(doi.strip())


def read_dois(manifest: str, column: str):
    with open(manifest, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    lookup = {c.lower(): c for c in rows[0]}
    actual = lookup.get(column.lower())
    if not actual:
        raise SystemExit(
            f"❌ {manifest} has no '{column}' column. Found: {list(rows[0])}"
        )
    return [r[actual].strip() for r in rows if (r.get(actual) or "").strip()]


def plan(base: str, dois):
    """Pair each DOI with its existing subfolder. Reports what has no folder.

    Resolution is case-insensitive against the real directory names, and the
    directory's OWN name wins over the encoder's output. That is not tidiness:
    doi_to_safe_filename preserves case ("10.1097--PR9...") while these folders
    were created from a lowercased DOI ("10.1097--pr9..."), so an exact match
    silently skipped 8 of 95 records -- and a skip looks identical to "this paper
    has no folder yet".
    """
    actual = {}
    for name in os.listdir(base):
        if os.path.isdir(os.path.join(base, name)):
            actual[name.lower()] = name

    matched, missing = [], []
    for doi in dois:
        stem = safe_stem(doi)
        on_disk = actual.get(stem.lower())
        if on_disk:
            matched.append((doi, os.path.join(base, on_disk), on_disk))
        else:
            missing.append((doi, os.path.join(base, stem)))
    return matched, missing


def clean(matched, apply_changes: bool) -> int:
    """Remove the previous pipeline's derivatives. Dry run unless apply_changes."""
    removed = 0
    for _doi, folder, _stem in matched:
        for name in PDF4LLM_DERIVATIVES:
            path = os.path.join(folder, name)
            if not os.path.exists(path):
                continue
            removed += 1
            if apply_changes:
                try:
                    os.unlink(path)
                except OSError as e:
                    print(f"   could not remove {path}: {e}")
            else:
                print(f"   would remove {os.path.relpath(path, os.path.dirname(folder))}")
    return removed


def fetch_one(doi: str, folder: str, args, resolver, router) -> dict:
    """One fetchpdf batch of a single record, writing into its own folder.

    batch_fetch_pdfs rather than fetch_pdf because --pull-supplementary
    is only plumbed through the batch entry point. The resolver is passed in and
    shared: without that, every concurrent record would build its own per-host
    rate limiter and the configured limits would be multiplied by --workers.
    """
    from fetchpdf.fetchpdf import batch_fetch_pdfs

    router.claim()
    try:
        results = batch_fetch_pdfs(
            dois=[doi],
            output_dir=folder,
            verbose=args.verbose,
            workers=1,
            create_missing_report=False,
            track_source=False,
            get_xml_or_html=True,
            # Off deliberately. The XML is what the extraction agent reads; a
            # Markdown rendering is a second lossy transform on top of it, and
            # the converter was measured dropping 31% of tables (145 of 473)
            # while reporting success -- silent loss is the one failure mode
            # this pipeline exists to avoid. Native XML costs ~41k tokens for a
            # median record, which is affordable at one paper per agent run.
            to_markdown=False,
            want_provenance=True,
            pull_supplementary=True,
            max_supplementary_bytes=args.max_supplementary_mb * 1024 * 1024,
            shared_resolver=resolver,
        )
        ok = bool(results and results[0][1])
        return {"doi": doi, "ok": ok, "note": "", "log": router.release()}
    except Exception as e:
        return {"doi": doi, "ok": False,
                "note": f"{type(e).__name__}: {str(e)[:100]}", "log": router.release()}


def si_incomplete(matched):
    """(stem, missing names) for every record whose manifest says incomplete.

    Read off the disk rather than threaded through the workers, because the
    manifest is the durable record and the summary object is not: a crashed
    worker leaves a manifest but returns nothing.
    """
    import json

    flagged = []
    for _doi, folder, stem in matched:
        path = os.path.join(folder, f"{stem}_supplementary_info.json")
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        if manifest.get("status") == "incomplete":
            missing = (manifest.get("declared") or {}).get("missing") or []
            flagged.append((stem, missing))
    return flagged


def describe(folder: str, stem: str) -> str:
    """What this record ended up with, read off the disk.

    The markdown is written as "xml->md" rather than "xml+md" because it is not a
    third thing we obtained -- it is a conversion of the artifact to its left. A
    "+" would read as another source having produced it, and would make a record
    look like it has more independent evidence than it does.
    """
    def here(suffix):
        return os.path.exists(os.path.join(folder, stem + suffix))

    got = []
    structured = "xml" if here(".xml") else "html" if here(".fulltext.html") else ""
    converted = here("_from_xml.md") or here("_from_html.md")
    if structured:
        # Both, if a record somehow has each; only the first feeds the markdown.
        if here(".xml") and here(".fulltext.html"):
            structured = "xml+html"
        got.append(f"{structured}->md" if converted else structured)
    elif converted:
        # Markdown with no structured sibling: the source was removed after
        # conversion. Worth showing as odd rather than as a normal outcome.
        got.append("md(orphan)")
    if here(".pdf"):
        got.append("pdf")
    supp = len([
        n for n in os.listdir(folder) if "_supplementary_info_" in n
    ])
    if supp:
        got.append(f"si×{supp}")
    return "+".join(got) if got else "nothing"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", required=True,
                        help="Corpus root: one subfolder per record, named with the DOI-safe stem")
    parser.add_argument("--manifest", required=True, help="CSV containing the DOIs")
    parser.add_argument("--doi-column", default="DOI")
    parser.add_argument("--clean", action="store_true",
                        help="Remove the previous pipeline's derivatives "
                             f"({', '.join(PDF4LLM_DERIVATIVES)}) before fetching")
    parser.add_argument("--yes", action="store_true",
                        help="Actually delete and actually fetch. Without it this is a dry run.")
    parser.add_argument("--limit", type=int, help="Only the first N records (trial run)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Records fetched in parallel (default: 1). All workers share "
                             "one per-host rate limiter, so raising this does NOT multiply "
                             "the API limits -- but see the note printed at startup about "
                             "the legacy PDF chain, which is not rate limited at all.")
    parser.add_argument("--max-supplementary-mb", type=int, default=300)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)


    if not os.path.isdir(args.base):
        raise SystemExit(f"❌ Not a directory: {args.base}")

    dois = read_dois(args.manifest, args.doi_column)
    matched, missing = plan(args.base, dois)
    if args.limit:
        matched = matched[: args.limit]

    print(f"📁 {args.base}")
    print(f"   {len(dois)} DOIs in {os.path.basename(args.manifest)}")
    print(f"   {len(matched)} with an existing subfolder"
          f"{f' (limited to {args.limit})' if args.limit else ''}")
    if missing:
        print(f"   {len(missing)} with no subfolder — skipped, not created:")
        for doi, _ in missing[:5]:
            print(f"      {doi}")
        if len(missing) > 5:
            print(f"      ... and {len(missing) - 5} more")

    if args.clean:
        print(f"\n🧹 Clearing previous-pipeline derivatives"
              f"{'' if args.yes else ' (DRY RUN)'}")
        count = clean(matched, apply_changes=args.yes)
        print(f"   {count} files {'removed' if args.yes else 'would be removed'}")
        if args.yes:
            # Said plainly because it is the one consequence that is not obvious:
            # fetchpdf does not produce body.md. Records with no XML/HTML have no
            # body text at all until the grobid/docling pass runs again.
            print("   NOTE: fetchpdf does not regenerate body.md — records with no")
            print("         XML/HTML will have no body text until pdf4llm re-runs.")

    if not args.yes:
        print("\n🔎 DRY RUN — nothing fetched. Re-run with --yes to execute.")
        print(f"   Would fetch {len(matched)} records into their own subfolders.")
        return 0

    # One resolver for the whole run: one HttpClient, one HostRateLimiter, one
    # resolution cache. Built here rather than inside each batch because a
    # per-batch limiter would multiply every configured per-host limit by
    # --workers -- 15 concurrent records would turn NCBI's 3/s into 45/s.
    from fetchpdf.retrieval.cache import ResolutionCache
    from fetchpdf.retrieval.http import HttpClient
    from fetchpdf.retrieval.ratelimit import HostRateLimiter
    from fetchpdf.retrieval.resolve import BatchResolver
    from fetchpdf.retrieval.tiers import load_ladder

    ladder = load_ladder()
    resolver = BatchResolver(
        HttpClient(HostRateLimiter(ladder.rate_limits), email=None, verbose=args.verbose),
        ResolutionCache(os.path.join(args.base, ".fetchpdf_resolution.json"), args.verbose),
        ladder,
        args.verbose,
    )
    # Batched up front: 95 records resolve in one ID Converter call instead of 95.
    calls = resolver.prime([doi for doi, _folder, _stem in matched])
    print(f"🔎 Resolved {len(matched)} identifiers in {calls} ID Converter call(s)")

    if args.workers > 1:
        print(f"⚠️  {args.workers} workers share one rate limiter for the API tiers, but the")
        print("   legacy PDF chain issues unthrottled requests. If publishers start")
        print("   returning 429s, lower --workers.")

    print(f"\n⬇️  Fetching {len(matched)} records with {args.workers} worker(s)\n")
    stats = {"ok": 0, "failed": 0, "xml": 0, "html": 0, "md": 0, "si": 0}
    router = _ThreadRoutedStdout(sys.stdout)
    printed = threading.Lock()
    done = [0]

    def run(entry):
        doi, folder, stem = entry
        outcome = fetch_one(doi, folder, args, resolver, router)
        got = describe(folder, stem)
        with printed:
            done[0] += 1
            flag = "✅" if outcome["ok"] else "❌"
            print(f"[{done[0]}/{len(matched)}] {flag} {doi}  →  {got}")
            if outcome["note"]:
                print(f"        {outcome['note']}")
            if not outcome["ok"] and args.verbose:
                print(outcome["log"])
            stats["ok" if outcome["ok"] else "failed"] += 1
            for key in ("xml", "html", "md"):
                if key in got:
                    stats[key] += 1
            if "si×" in got:
                stats["si"] += 1

    real_stdout = sys.stdout
    sys.stdout = router
    try:
        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for future in as_completed([pool.submit(run, e) for e in matched]):
                    future.result()
        else:
            for entry in matched:
                run(entry)
    finally:
        sys.stdout = real_stdout
        resolver.cache.flush()

    print(f"\n{'=' * 58}")
    print(f"  records processed   {len(matched)}")
    print(f"  succeeded           {stats['ok']}")
    print(f"  failed              {stats['failed']}")
    print(f"  with XML            {stats['xml']}")
    print(f"  with HTML           {stats['html']}")
    print(f"  with markdown       {stats['md']}")
    print(f"  with supplementary  {stats['si']}")
    incomplete = si_incomplete(matched)
    if incomplete:
        # Files the papers themselves declare that no provider obtained. The
        # red-flag position at the bottom is deliberate: this line is the one
        # a run must never end without, and the last thing printed is the
        # first thing read.
        print(f"  ⚠️  SI INCOMPLETE     {len(incomplete)} record(s):")
        for stem, names in incomplete:
            print(f"      {stem}: {', '.join(names) or 'unknown'}")
    print(f"{'=' * 58}")
    # Keyed on the artifacts, not the markdown: conversion is off by default
    # now (native XML is the deliverable), so "no md" no longer implies
    # "no structured text".
    no_structured = len(matched) - stats["xml"] - stats["html"]
    if no_structured > 0:
        print(f"\n  {no_structured} records have no XML/HTML.")
        print("  Those still need the PDF pipeline for body text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
