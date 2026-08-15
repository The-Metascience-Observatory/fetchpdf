"""fetchpdf-verify: does the corpus on disk still match its manifests?

A corpus can be complete by every count fetchpdf keeps -- every record fetched,
every manifest written, every declared file obtained -- and still be hollow. A
real one held 30 git-lfs pointer stubs where its supplementary spreadsheets
should have been, each 130 bytes standing in for tens of KB. Every pointer's
oid matched the sha256 the manifest recorded, which is the proof the bytes were
downloaded and hashed correctly and something afterward replaced them.

Nothing noticed, because the manifest's integrity metadata was only ever
written, never read back. This command reads it back. It makes no network
requests: it answers "is what I have what I fetched", not "is there more to
fetch".

The verdicts come from the same _integrity_of predicate the repair pass uses,
so an audit that says a file is damaged and a re-run that restores it can never
disagree about which files those are.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from typing import List, Tuple

from .supplementary import (
    ABSENT,
    DATA_ARTIFACT_INFIX,
    HASH_MISMATCH,
    INTACT,
    LFS_POINTER,
    MANIFEST_SUFFIX,
    SIZE_MISMATCH,
    UNVERIFIABLE,
    _integrity_of,
)

#: Ordered worst-first, because that is the order an operator needs to act in.
_REPORT_ORDER = (LFS_POINTER, HASH_MISMATCH, SIZE_MISMATCH, ABSENT, UNVERIFIABLE)

#: What to actually DO about each verdict. A stub and a truncated download both
#: read as "corrupt", but re-running fetchpdf fixes only one of them -- the
#: other returns on the next sync unless the storage is fixed.
_ADVICE = {
    LFS_POINTER: ("git-lfs pointer, content never smudged -- fix the checkout "
                  "(git lfs pull) or the sync; a re-fetch works but the next "
                  "sync will stub it again"),
    HASH_MISMATCH: "content differs from what was fetched -- re-run with --refresh-supplementary",
    SIZE_MISMATCH: "truncated or overwritten -- re-run with --refresh-supplementary",
    ABSENT: "file listed in the manifest is gone -- re-run with --refresh-supplementary",
    UNVERIFIABLE: ("manifest predates integrity metadata; nothing to check "
                   "against (not a fault)"),
}


def audit_manifest(manifest_path: str, verify_hashes: bool = True
                   ) -> List[Tuple[str, str]]:
    """Verdicts for one manifest, as (relative path, verdict) pairs."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return [(manifest_path, "manifest_unreadable")]

    directory = os.path.dirname(os.path.abspath(manifest_path)) or "."
    # Supplementary files are flat siblings of the manifest; linked-deposit
    # files keep their ORIGINAL names under <stem>_data_artifacts/ and record a
    # filename relative to THAT. Resolving both against the manifest's own
    # directory reports every deposit file as absent when all of them are there.
    stem = os.path.basename(manifest_path[:-len(MANIFEST_SUFFIX)]) \
        if manifest_path.endswith(MANIFEST_SUFFIX) else ""
    artifact_root = os.path.join(directory, stem + DATA_ARTIFACT_INFIX)

    results = []
    for record in manifest.get("files") or []:
        # No file by design -- its members are already unpacked beside it.
        if record.get("removed_after_extraction"):
            continue
        filename = record.get("filename") or ""
        if not filename:
            continue
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            candidate = os.path.join(artifact_root, filename)
            if os.path.isfile(candidate):
                path = candidate
        results.append((path, _integrity_of(path, record, verify_hashes)))
    return results


def audit_tree(root: str, verify_hashes: bool = True) -> List[Tuple[str, str]]:
    """Every manifest under root, recursively."""
    pattern = os.path.join(root, "**", "*" + MANIFEST_SUFFIX)
    results = []
    for manifest_path in sorted(glob.glob(pattern, recursive=True)):
        results.extend(audit_manifest(manifest_path, verify_hashes))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetchpdf-verify",
        description="Check supplementary files on disk against their manifests. "
                    "Makes no network requests.")
    parser.add_argument("directory", help="Corpus root (searched recursively)")
    parser.add_argument("--no-verify-hashes", action="store_true",
                        help="Compare sizes only. Faster on a large corpus, and "
                             "still catches every stub and truncation; misses "
                             "same-length corruption.")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Totals only, no per-file lines.")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.directory):
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2

    results = audit_tree(args.directory, not args.no_verify_hashes)
    if not results:
        print(f"No supplementary manifests found under {args.directory}")
        return 0

    tally = Counter(verdict for _, verdict in results)
    damaged = [(p, v) for p, v in results if v not in (INTACT, UNVERIFIABLE)]

    if damaged and not args.quiet:
        for verdict in _REPORT_ORDER:
            hits = [p for p, v in damaged if v == verdict]
            if not hits:
                continue
            print(f"\n{verdict.upper()}  ({len(hits)})")
            print(f"  → {_ADVICE.get(verdict, '')}")
            for path in hits:
                print(f"    {os.path.relpath(path, args.directory)}")

    print(f"\n{len(results)} file(s) checked under {args.directory}")
    for verdict in (INTACT,) + _REPORT_ORDER:
        if tally.get(verdict):
            print(f"  {verdict:<15} {tally[verdict]}")

    # Exit non-zero on real damage so this can gate an analysis run in CI or a
    # shell chain. UNVERIFIABLE is not damage: it is an older manifest.
    return 1 if damaged else 0


if __name__ == "__main__":
    raise SystemExit(main())
