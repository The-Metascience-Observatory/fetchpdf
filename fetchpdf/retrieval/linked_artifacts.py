"""The record of what datasets and code the citation graph links to this paper.

The supplement providers download files; this sidecar records *links* -- every
publication->dataset and publication->software relation the link services
returned, whether or not it resolved to files. The two facts are different and
both matter downstream: a Dryad DOI with no enumerator, a trial registration,
and an "xgboost software on GitHub" tool citation all belong here even though
none of them lands on disk.

Empty results are recorded explicitly. On the corpora this was measured against
(2026-08), most papers have zero links -- ~30% of a recent biomedical batch had
any ScholeXplorer link, and an older clinical-trial corpus had none at all --
so "queried, nothing found" is the common case, and it must stay
distinguishable from "never queried".

The classification vocabulary, coarsest that survives contact with real links:

  * owned          -- deposited as the paper's own material (IsSupplementTo
                      and friends), or reached through a data DOI.
  * related        -- a dataset/software record linked to the paper without a
                      supplement-grade relation. Most Scholix "cites" links.
  * tool_citation  -- a software record that is somebody's tool, not this
                      paper's code. OpenAIRE mints these for every R package a
                      paper cites; they dominate the software links.
  * registry       -- a registration, not an artifact: ClinicalTrials.gov and
                      the like. Never downloadable, always worth recording.

Providers append to ctx.scratch as they run; pull_for_record writes the
sidecar once, after the manifest. No scratch entry, no sidecar -- a run with a
restricted provider list must not write a file claiming services were asked
when they were not.
"""

import json
import os
import tempfile
import time
from typing import Optional

_SCHEMA_VERSION = 1

#: The sidecar lands beside the manifest, keyed on the same stem.
SIDECAR_SUFFIX = "_linked_artifacts.json"

#: Where providers accumulate their queries and links between enumeration and
#: the write. One key, holding {"queried": [...], "links": [...]}.
SCRATCH_KEY = "linked_artifacts"

CLASS_OWNED = "owned"
CLASS_RELATED = "related"
CLASS_TOOL_CITATION = "tool_citation"
CLASS_REGISTRY = "registry"


def _bucket(ctx) -> dict:
    return ctx.scratch.setdefault(SCRATCH_KEY, {"queried": [], "links": []})


def record_query(ctx, service: str, url: str, status: str, detail: str = "") -> None:
    """One service asked one question. `url` must already be redacted."""
    _bucket(ctx)["queried"].append({
        "service": service,
        "url": url,
        "status": status,
        "detail": detail,
        "retrieved_at": _iso(time.time()),
    })


def record_link(ctx, *, service: str, target_pid: Optional[str], target_type: str,
                relation: str, classified: str, title: str = "",
                publisher: str = "", provider_name: str = "",
                harvest_date: str = "", routed: bool = False) -> None:
    """One link the service asserted. `routed` means a repository enumerator
    was handed the target to resolve into files."""
    _bucket(ctx)["links"].append({
        "service": service,
        "target_pid": target_pid,
        "target_type": target_type,
        "relation": relation,
        "classified": classified,
        "title": (title or "")[:300],
        "publisher": (publisher or "")[:100],
        "provider_name": provider_name,
        "harvest_date": harvest_date,
        "routed_to_download": routed,
    })


def sidecar_path_for(stem: str) -> str:
    return stem + SIDECAR_SUFFIX


def write_linked_sidecar(stem: str, ids, ctx, verbose: bool = False) -> Optional[str]:
    """Write the sidecar atomically. Never raises into the supplementary pass.

    Returns the path written, or None -- both when writing failed and when
    there was nothing to write because no link service ran.
    """
    bucket = ctx.scratch.get(SCRATCH_KEY)
    if not bucket:
        return None

    links = bucket["links"]
    record = {
        "schema_version": _SCHEMA_VERSION,
        "identifier": str(getattr(ids, "doi", "") or getattr(ids, "best_id", "") or ""),
        "identifiers_resolved": ids.to_dict() if hasattr(ids, "to_dict") else {},
        "stem": os.path.basename(stem),
        "completed_at": _iso(time.time()),
        "queried": bucket["queried"],
        "links": links,
        "counts": {
            "links": len(links),
            "routed_to_download": sum(1 for l in links if l.get("routed_to_download")),
        },
    }

    path = sidecar_path_for(stem)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-la-", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        if verbose:
            print(f"    ⚠️  could not write {os.path.basename(path)}: {e}")
        return None
    return path


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
