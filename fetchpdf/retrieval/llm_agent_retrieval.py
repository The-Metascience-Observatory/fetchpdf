"""Layer 2: a model reads the paper and goes after what the APIs did not get.

Layer 1 is eighteen enumerators and four link services, and it is very good at
the material publishers and repositories declare through an API. What it cannot
do is read. MEASURED across four corpora on 2026-08-22: those services returned
**3,005 `related` links that were never routed** and 280 `owned` ones of which
264 were BioStudies mirrors of supplements already in hand -- while
`fulltext_scan`, which does nothing but read the article's own prose, produced
83 OSF files from ten records, and the link indexes had surfaced *none* of the
fourteen deposits those articles named themselves.

So this provider runs LAST, after every free deterministic route, and it is
given three things layer 1 already knows and a model does not:

  1. the paper's text -- the availability statements, weighted toward them;
  2. what we already hold, so it does not fetch a second copy;
  3. what the link services found and declined, with their reasons. That list
     is the single richest input here: 3,005 candidates somebody already
     decided were probably not ours, among which the actual replication
     packages are hiding.

WHAT IT MAY NOT DO. It proposes; the existing gates dispose. A repository or
GitHub proposal meets `_ownership_refusal` first -- the full-text scan's own
verdict on how the paper mentions it, the tool-library backstop, and
`_deposit_claims_another_article` -- nothing it saves skips `_commit`'s size
cap, sha256, deduplication and challenge-page sniff, and nothing it does can
change whether the record succeeded. Every failure path --
no backend, no key, a timeout, a receipt that will not parse -- logs once and
returns an empty list, leaving layer 1's result exactly as it was.

The two backends differ in whether the model downloads or only navigates; see
`backends/__init__.py`, where the measurements that forced that split are
recorded.
"""

import os
import re
import shutil
import tempfile
import threading
from typing import List, Optional

from .backends import KIND_DATASET, get_backend
from .supplement_index import ROLE_SUPPLEMENT, SupplementFile

#: Records that have already had an agent call this process. A ceiling is the
#: only thing standing between an overnight corpus run and an unbounded bill,
#: and it has to be counted where the spending happens rather than where the
#: batch is planned -- workers run concurrently, so the count is shared and
#: guarded.
_SPENT = {"records": 0}
_SPENT_LOCK = threading.Lock()

#: What layer 2 actually did, per record, across this run. A run that says only
#: how many files it found lets "the agent was never called" and "the agent
#: looked and there was nothing" print identically -- and on these corpora the
#: second is the common case, so the first would hide behind it indefinitely.
_OUTCOMES: dict = {}


def _note(outcome: str) -> None:
    with _SPENT_LOCK:
        _OUTCOMES[outcome] = _OUTCOMES.get(outcome, 0) + 1


def run_report() -> dict:
    """`{outcome: count}` for the whole run, or empty if it never ran."""
    with _SPENT_LOCK:
        return dict(_OUTCOMES)


def reset_record_budget() -> None:
    """Forget how many records have been billed. For tests and long sessions."""
    with _SPENT_LOCK:
        _SPENT["records"] = 0
        _OUTCOMES.clear()


def _claim_record_budget(limit: int) -> bool:
    """Take one slot, or report that the ceiling is reached. Never blocks."""
    if not limit or limit <= 0:
        return True
    with _SPENT_LOCK:
        if _SPENT["records"] >= limit:
            return False
        _SPENT["records"] += 1
        return True


#: How much of the paper the model is shown. Bounded because cost scales with
#: it and because the answer lives in a few hundred characters of availability
#: prose, not in the methods section.
BRIEF_TEXT_CHARS = 14000

#: Characters kept around each availability statement.
DAS_WINDOW = 1800

#: Head of the document, for title and abstract: the model has to know which
#: paper it is judging ownership against.
HEAD_CHARS = 2500

#: Availability phrases, RANKED. The rank is the whole point: a real data
#: availability statement lives in the back matter, and a long paper mentions
#: "supplementary material" and "deposited" a dozen times before it gets
#: there. Filling the budget in document order therefore spends it on passing
#: mentions and drops the statement -- measured on
#: 10.1057/s41599-024-02881-1, where 11 phrases matched, the real statement sat
#: at character 49,385 of 60,736, and the brief the model received did not
#: contain the GitHub repository the authors named.
#:
#: So windows are chosen by score and only then put back into document order.
_AVAILABILITY_RANKED = (
    # A section heading. Nothing else in a paper says this.
    (3, re.compile(r"(data\s+and\s+code\s+availability|"
                   r"data\s+availability|code\s+availability|"
                   r"availability\s+of\s+(?:data|code)|"
                   r"data,\s*materials,?\s*and\s*(?:code|software)\s*availability)", re.I)),
    # A deposit being claimed, wherever it is said.
    (2, re.compile(r"(deposited|publicly\s+available|freely\s+available|"
                   r"available\s+(?:at|from|on|in)\s+(?:the\s+)?"
                   r"(?:https?|www|github|osf|zenodo|dryad|figshare|dataverse)|"
                   r"data\s+sharing|open\s+(?:data|science)\s+framework)", re.I)),
    # Weakest: the word appears. Worth reading if there is budget left.
    (1, re.compile(r"(supplementary\s+(?:material|information|data|file)|"
                   r"supporting\s+information|accession)", re.I)),
)

_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")

_INSTRUCTIONS = """\
You are finding supplementary files and datasets that belong to ONE paper, so
they can be archived alongside it.

Rules, in order of importance:

1. ONLY this paper's own material. A dataset the paper CITES belongs to
   somebody else and must not be collected. A software library the authors
   used is not their code. A preregistration is not the analysis data.
   If you are not sure it is theirs, leave it out and say so in "why".
2. Do not re-fetch anything in ALREADY OBTAINED below.
3. Prefer a direct file URL. If you only have a landing page, open it and find
   the file list before answering.
4. Ignore anything that is not a file: registry entries, accession numbers with
   no download, "available on request".

%(action)s

Answer with a JSON array and nothing else. One object per file:

  [{"url": "<direct file url>",
    "kind": "supplement" | "dataset",
    "deposit": "<landing page it came from, or empty>",
    "name": "<filename>",
    "why": "<one short line: why this is THIS paper's>"%(saved_as)s}]

An empty array is the right answer when the paper deposited nothing new. It is
the common case -- say it plainly rather than reaching for something.
"""

_ACTION_NAVIGATE = """\
You cannot save files. Use WebFetch to open pages and REPORT the URLs you
find; they will be downloaded for you.\
"""

_ACTION_DOWNLOAD = """\
Use `download` to save each file into your working directory, then report it.
Use `fetch_url` to open landing pages first.\
"""


def build_brief(text: str, identity: str, obtained, declined,
                downloads: bool) -> str:
    """The prompt: instructions, what we hold, what was declined, the paper."""
    action = _ACTION_DOWNLOAD if downloads else _ACTION_NAVIGATE
    saved_as = ',\n    "saved_as": "<the filename you saved>"' if downloads else ""
    parts = [_INSTRUCTIONS % {"action": action, "saved_as": saved_as}]

    parts.append(f"\nPAPER: {identity}")

    if obtained:
        parts.append("\nALREADY OBTAINED (do not fetch again):")
        parts.extend(f"  - {line}" for line in obtained[:60])
    else:
        parts.append("\nALREADY OBTAINED: nothing.")

    if declined:
        parts.append(
            "\nLINKS THE INDEXES FOUND AND DID NOT COLLECT. Each was judged "
            "not to be this paper's own material, on metadata alone. Some of "
            "them are wrong. The paper's text is better evidence than the "
            "index was:")
        parts.extend(f"  - {line}" for line in declined[:40])

    parts.append("\nPAPER TEXT:\n")
    parts.append(text)
    return "\n".join(parts)


def select_text(text: str, limit: int = BRIEF_TEXT_CHARS) -> str:
    """The head of the paper plus every availability statement in it.

    A whole article is mostly methods and results, and none of that says where
    the data went. Sending all of it would multiply the bill by the length of
    the paper for no gain, so the windows that matter are cut out and joined --
    with a marker, so the model is never told this is continuous prose.
    """
    if not text:
        return ""
    flat = _WHITESPACE.sub(" ", _TAGS.sub(" ", text))
    if len(flat) <= limit:
        return flat

    # The head is not optional and does not compete for budget: the model has
    # to know which paper it is judging ownership against.
    head = flat[:min(HEAD_CHARS, limit)]
    remaining = limit - len(head)
    if remaining <= 0:
        return head

    scored = []
    for score, pattern in _AVAILABILITY_RANKED:
        for match in pattern.finditer(flat, len(head)):
            scored.append((score, match.start()))
    if not scored:
        # Nothing said "availability" anywhere. The back matter is still where
        # a statement would be, so read the end rather than more of the front.
        return head + "\n[...]\n" + flat[-remaining:]

    # Strongest first, and later beats earlier at equal strength: a real
    # statement sits in the back matter, and the front-matter mention of the
    # same words is the abstract talking about somebody else's data.
    scored.sort(key=lambda pair: (-pair[0], -pair[1]))

    chosen: List[list] = []
    budget = remaining
    for _score, position in scored:
        if budget <= 0:
            break
        start = max(len(head), position - DAS_WINDOW // 3)
        end = min(len(flat), position + DAS_WINDOW)
        for span in chosen:                      # already covered?
            if start >= span[0] and end <= span[1]:
                start = end = 0
                break
        if end <= start:
            continue
        length = min(end - start, budget)
        chosen.append([start, start + length])
        budget -= length

    # Back into document order before it is shown to anyone. Overlaps merge,
    # so an availability section matched by three patterns is read once.
    merged: List[list] = []
    for start, end in sorted(chosen):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return "\n[...]\n".join([head] + [flat[a:b] for a, b in merged])


def _identity(ids) -> str:
    bits = [f"DOI {ids.doi}" if getattr(ids, "doi", None) else "",
            f"PMID {ids.pmid}" if getattr(ids, "pmid", None) else "",
            f"PMCID {ids.pmcid}" if getattr(ids, "pmcid", None) else ""]
    return ", ".join(b for b in bits if b) or "unidentified record"


def _obtained_lines(ctx) -> List[str]:
    lines = []
    for entry in ctx.scratch.get("enumerated_so_far") or []:
        name = getattr(entry, "name", "") or ""
        provider = getattr(entry, "provider", "") or ""
        lines.append(f"{name}  [{provider}]" if provider else name)
    return lines


def _declined_lines(ctx) -> List[str]:
    lines = []
    bucket = ctx.scratch.get("linked_artifacts") or {}
    for link in bucket.get("links") or []:
        if link.get("routed_to_download"):
            continue
        lines.append(
            f"{link.get('target_pid', '')}  "
            f"({link.get('classified', '?')}, {link.get('relation', '?')}) "
            f"{(link.get('title') or '')[:90]}")
    return lines


def enumerate_llm_agent(ids, ctx) -> List[SupplementFile]:
    """E20: the last word, and only when asked for.

    Runs after every deterministic provider, so everything it is told about
    what we already hold is true by the time it is told.
    """
    if not getattr(ctx, "llm_agent_retrieval", False):
        return []

    from .fulltext_scan import record_text
    from .linked_artifacts import record_query

    stem = os.path.splitext(ctx.save_path)[0] if ctx.save_path else ""
    if not stem:
        return []

    backend = get_backend(getattr(ctx, "llm_backend", None))
    if backend is None:
        ctx.log(f"    llm_agent: unknown backend "
                f"{getattr(ctx, 'llm_backend', None)!r}")
        _note("unknown_backend")
        return []
    ok, detail = backend.available()
    if not ok:
        record_query(ctx, service="llm_agent", url="(local model call)",
                     status="unavailable", detail=detail)
        ctx.log(f"    llm_agent: {detail}")
        _note("backend_unavailable")
        return []

    if not _claim_record_budget(getattr(ctx, "max_llm_records", 0) or 0):
        # Recorded, not just skipped. "We chose not to spend on this record"
        # and "we looked and there was nothing" are different facts, and a
        # sidecar that cannot tell them apart makes the second one up.
        record_query(ctx, service="llm_agent", url="(model call)",
                     status="skipped",
                     detail="--max-llm-records ceiling reached for this run")
        ctx.log("    llm_agent: --max-llm-records reached; not calling")
        _note("skipped_ceiling")
        return []

    source = record_text(stem, log=ctx.log)
    if source is None:
        # Not a failure of the agent: there is no text on disk to read. Said
        # out loud, because "no full text here" and "nothing was deposited"
        # are answers to different questions.
        record_query(ctx, service="llm_agent", url="(local full text)",
                     status="no_text", detail="no readable full text on disk")
        ctx.log("    llm_agent: no readable full text for this record")
        _note("no_full_text")
        return []

    _path, text = source
    # How the paper itself mentions each repository, judged by the same rules
    # the full-text scan applies. A proposal the paper names only inside a
    # reference entry, or as a preregistration, is refused below.
    from .fulltext_scan import scan_text
    paper = {_canonical_key(c): c for c in scan_text(text)}
    brief = build_brief(
        select_text(text), _identity(ids),
        _obtained_lines(ctx), _declined_lines(ctx), backend.downloads)

    # NOT a `with`: a downloading backend's files must stay readable until the
    # pipeline has committed them, and that happens long after this provider
    # returns. Deleting on the way out races the ingest and loses every file
    # the agent fetched -- silently, because a missing `file://` source reads
    # as an unreachable download. `cleanup_scratch` is called by
    # `pull_for_record` once every file is committed, exactly as the Atypon
    # provider's scratch dirs are.
    staging = tempfile.mkdtemp(prefix="fetchpdf-agent-")
    ctx.scratch.setdefault("_llm_agent_scratch_dirs", []).append(staging)

    receipt = _call(backend, brief, staging, ctx)
    if receipt is None:
        record_query(ctx, service="llm_agent", url="(model call)",
                     status="error", detail="backend returned nothing")
        _note("call_failed")
        return []
    record_query(ctx, service="llm_agent", url="(model call)", status="ok",
                 detail=f"{len(receipt)} file(s) proposed via {backend.name}")

    _note("proposed_something" if receipt else "ran_found_nothing")
    files: List[SupplementFile] = []
    for index, entry in enumerate(receipt):
        routed = _route(entry, ids, ctx, paper)
        if routed is None:
            routed = [_as_supplement_file(entry, ids, index)]
        files.extend(routed)
        _record(ctx, entry, routed)
    return files


#: Repository landing pages the existing enumerators already know how to walk.
#: A model that answers "the data are at https://osf.io/abcde/" is right, and
#: downloading that URL gets an HTML page -- measured: the agent found the
#: GitHub repository this paper's own regex could not (the PDF broke the URL
#: across a line), proposed the repo page, and the pipeline correctly refused
#: it as "served HTML". Right refusal, wrong question. The repo has an
#: enumerator; use it.
_REPOSITORY_HINTS = ("osf.io", "zenodo.org", "10.5281/zenodo", "datadryad.org",
                     "10.5061/dryad", "figshare.com", "10.6084",
                     "10.7910/dvn", "dataverse")
_GITHUB_HINT = "github.com/"


def _route(entry: dict, ids, ctx, paper=None):
    """The existing repository enumerators, when the URL names a repository.

    Returns None when this is an ordinary file URL and should just be
    downloaded, and [] when a repository proposal is refused. A repository or
    GitHub proposal must pass `_ownership_refusal` before it is enumerated:
    the enumerators themselves check nothing about whose deposit it is, and
    the index providers apply `_should_route` before they call them.
    """
    if entry.get("saved_as"):
        return None                       # already a file, on disk
    from .supplement_graph import _files_in_repository, _github_tarball

    target = (entry.get("deposit") or "") or entry.get("url") or ""
    lowered = target.lower()
    if _GITHUB_HINT in lowered or any(hint in lowered for hint in _REPOSITORY_HINTS):
        refusal = _ownership_refusal(target, ids, ctx, paper or {})
        if refusal:
            entry["refused"] = refusal
            ctx.log(f"    llm_agent: not routing {target}: {refusal}")
            return []
    if _GITHUB_HINT in lowered:
        repo = lowered.split(_GITHUB_HINT, 1)[1].strip("/")
        repo = "/".join(repo.split("/")[:2])
        if repo.count("/") == 1:
            return _github_tarball(repo, ctx) or []
        return None
    if any(hint in lowered for hint in _REPOSITORY_HINTS):
        return _files_in_repository(target, ids, ctx, via="llm_agent") or []
    return None


_EXPLICIT_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", re.I)


def _canonical_key(candidate) -> str:
    """A full-text-scan key that matches however the deposit was written.

    Zenodo appears as both zenodo.org/records/<n> and 10.5281/zenodo.<n>;
    the paper and the model need not pick the same form.
    """
    ident = candidate.ident.lower()
    if candidate.repo == "zenodo":
        ident = ident.rsplit("zenodo.", 1)[-1]
    return f"{candidate.repo}:{ident}"


def _deposit_doi(target: str, candidate) -> Optional[str]:
    """The deposit's DOI, when the proposal states it or it follows from the id."""
    match = _EXPLICIT_DOI.search(target or "")
    if match:
        return match.group(1).rstrip(".,;)/")
    if candidate is None:
        return None
    if candidate.repo == "osf":
        return f"10.17605/osf.io/{candidate.ident.lower()}"
    if candidate.repo == "zenodo":
        return f"10.5281/zenodo.{candidate.ident.lower().rsplit('zenodo.', 1)[-1]}"
    return None


def _ownership_refusal(target: str, ids, ctx, paper: dict) -> Optional[str]:
    """Why a repository proposal must not be downloaded, or None to allow it.

    Three checks, each reusing a rule the deterministic path already applies:

    1. The paper's own mention. If the full-text scan found this deposit in
       the paper and refused it -- inside a reference entry, attached to a
       preregistration, a preprint DOI -- the model's say-so does not override
       that. A deposit the paper never names (the agent found it by
       navigating) is not refused for that alone; finding those is the job.
    2. Known tool and library GitHub orgs, which are never the paper's own code.
    3. `_deposit_claims_another_article`: a deposit whose DataCite record says
       it belongs to a different article is that article's. Fails open on a
       lookup error, as it does for index links.
    """
    from .fulltext_scan import _PREPRINT_DOI, _TOOL_ORGS, REFUSE, scan_text
    from .supplement_graph import _deposit_claims_another_article

    if _PREPRINT_DOI.search(target or ""):
        return "a preprint DOI, not a data deposit"
    found = scan_text(target)
    candidate = found[0] if found else None
    if candidate is not None:
        if candidate.repo == "github" and \
                candidate.ident.split("/", 1)[0].lower() in _TOOL_ORGS:
            return f"{candidate.ident.split('/', 1)[0]} is a known tool/library org"
        mention = paper.get(_canonical_key(candidate))
        if mention is not None and mention.verdict == REFUSE:
            return f"the paper itself names it only as: {mention.reason}"
    doi = _deposit_doi(target, candidate)
    article = getattr(ids, "doi", None)
    if doi and article and _deposit_claims_another_article(doi, article, ctx):
        return "its DataCite record says it belongs to a different article"
    return None


def _record(ctx, entry: dict, routed) -> None:
    """Every proposal in the sidecar, downloaded or not.

    A model's suggestion that came to nothing is still the record of what was
    considered, and the sidecar is where a reader checks whether a paper's
    deposit was seen and rejected or never seen at all.
    """
    from .linked_artifacts import CLASS_OWNED, CLASS_RELATED, record_link

    target = entry.get("url") or entry.get("deposit") or ""
    refused = entry.get("refused")
    record_link(
        ctx, service="llm_agent", target_pid=target,
        target_type=("software" if _GITHUB_HINT in target.lower()
                     else "dataset" if entry.get("kind") == KIND_DATASET
                     else "supplement"),
        relation="NamedInFullText",
        classified=CLASS_RELATED if refused else CLASS_OWNED,
        title=((f"refused: {refused}; " if refused else "")
               + (entry.get("why") or ""))[:300],
        publisher=entry.get("deposit") or "",
        provider_name="llm_agent", routed=bool(routed),
    )


def cleanup_scratch(ctx) -> None:
    """Remove the staging dirs the retrieval agent downloaded into.

    Same contract as the Atypon provider's: called once the pipeline has copied
    the files to their real homes, never on the way out of enumeration.
    """
    for path in ctx.scratch.pop("_llm_agent_scratch_dirs", []) or []:
        shutil.rmtree(path, ignore_errors=True)


def _call(backend, brief: str, staging: str, ctx) -> Optional[List[dict]]:
    model = getattr(ctx, "llm_model", None) or None
    try:
        if getattr(backend, "downloads", False):
            return backend.run(brief, staging, model=model, log=ctx.log,
                               http=ctx.http)
        return backend.run(brief, staging, model=model, log=ctx.log)
    except Exception as error:                  # a helper may never fail a run
        ctx.log(f"    llm_agent: {type(error).__name__}: {str(error)[:150]}")
        return None


def _as_supplement_file(entry: dict, ids, index: int) -> SupplementFile:
    """One receipt entry as something `_Run.take` can act on.

    A staged file becomes a `file://` URL rather than a special case:
    `HttpClient.download` already adopts those under the same cap, the same
    sha256 and the same complete-or-absent guarantee as a network transfer, so
    a downloaded-by-the-agent file and a downloaded-by-us file travel one code
    path from here on.

    The real source URL rides in `extra`, because the `file://` path stops
    meaning anything the moment the staging directory is removed -- and the
    manifest is what a reader consults to ask where a file came from.
    """
    staged = entry.get("saved_as")
    url = f"file://{staged}" if staged else entry["url"]
    name = (entry.get("name")
            or (os.path.basename(staged) if staged else "")
            or os.path.basename(entry["url"].split("?", 1)[0])
            or f"llm_agent_{index + 1}")
    kind = entry.get("kind")
    return SupplementFile(
        name=name,
        url=url,
        provider=("fulltext_scan:llm_agent" if kind == KIND_DATASET
                  else "llm_agent"),
        listing_index=index,
        role=ROLE_SUPPLEMENT,
        origin_doi=getattr(ids, "doi", None),
        label=(entry.get("why") or "")[:200],
        extra={
            "source_url": entry.get("url") or "",
            "deposit": entry.get("deposit") or "",
            "llm_agent": True,
        },
    )
