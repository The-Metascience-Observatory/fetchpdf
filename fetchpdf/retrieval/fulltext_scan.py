"""Find the paper's own data deposits by reading the paper.

Link services (ScholeXplorer, Europe PMC datalinks) answer "what does an index
say is connected to this DOI". That misses a great deal: across 13 full-text
records in a real corpus they surfaced zero of the 14 OSF deposits the articles
themselves name in their own prose. The article is the better source, and it is
already on disk -- so this provider costs no network requests at all.

The catch is precision. A bare URL regex over full text is only about HALF
right, because the same page also contains:

  * preregistrations         "preregistration: https://osf.io/2efnw"
  * OTHER papers' deposits   a reference-list entry citing someone's archive
  * preprint DOIs            10.31234/osf.io/8r9p7 is PsyArXiv, not a deposit

Downloading those is the "dirty directories" failure: gigabytes of somebody
else's material filed under this paper. So three rules run over the prose around
each URL, and a candidate is accepted only when the sentence actually claims a
deposit. On the 14 hand-labelled OSF IDs this scores 7/7 accepted and 7/7
refused -- though those rules were written after reading those examples, so
treat the number as a regression baseline rather than a validated accuracy.

What this deliberately does NOT do is guess. A candidate the rules cannot settle
is returned as UNCERTAIN rather than silently dropped, so the caller can pay a
model to adjudicate it (see llm_adjudicate) or record it unresolved. Losing a
deposit silently is the one failure mode worth engineering against: an extra
download is visible and cheap, a missing one is invisible.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Verdicts. UNCERTAIN is a real answer, not a failure -- it is the queue that
#: optional LLM adjudication drains.
ACCEPT = "accept"
REFUSE = "refuse"
UNCERTAIN = "uncertain"

#: How much prose around a URL is read. 300 chars each way covers the sentence
#: and its neighbour, which is where "deposited"/"preregistration" live, without
#: dragging in the next paragraph's unrelated claims.
CONTEXT_CHARS = 300

#: Repositories worth resolving. Keyed by the name _files_in_repository expects.
#: GitHub is matched but handled separately -- see _GITHUB below.
_REPO_PATTERNS = (
    ("osf", re.compile(r"osf\.io/([a-z0-9]{5})\b", re.I)),
    ("zenodo", re.compile(r"zenodo\.org/record[s]?/(\d+)", re.I)),
    ("zenodo", re.compile(r"\b(10\.5281/zenodo\.\d+)", re.I)),
    ("dryad", re.compile(r"\b(10\.5061/dryad\.[a-z0-9./]+)", re.I)),
    ("dataverse", re.compile(r"\b(10\.7910/dvn/[a-z0-9]+)", re.I)),
    ("figshare", re.compile(r"figshare\.com/[^\s\"'<>)]*?(\d{6,})", re.I)),
    ("github", re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I)),
)

#: Rule 1 -- the sentence has to claim a deposit. Without one of these the URL
#: is just a URL: mentioned in passing, in a footnote, or somebody else's.
_DEPOSIT_LANGUAGE = re.compile(
    r"(deposited|data\s+availability|code\s+availability|are\s+available|"
    r"is\s+available|publicly\s+available|available\s+(at|on|from)|"
    r"analysis\s+(code|scripts)|materials\s+and\s+data|data\s+and\s+(code|materials)|"
    r"online\s+archive|can\s+be\s+(found|accessed)|have\s+been\s+shared|"
    r"osf\s+project)",
    re.I,
)

#: Rule 2 -- a preregistration is not data. Papers routinely link both, and the
#: prereg is the one that is NOT the analysis dataset.
#:
#: This must match the URL's PURPOSE, not merely the word appearing nearby. A
#: deposit's contents are often listed as "preregistrations, the analysis code,
#: anonymized data ... available as an OSF project at <url>" -- one link
#: holding everything, including a prereg. Matching a bare "prereg" there
#: refused a real deposit. So require the word to be attached to the link:
#: "preregistration: <url>", "preregistered at <url>", "(preregistration <url>)".
_PREREG_LANGUAGE = re.compile(
    r"(prereg\w*\s*(:|at|is|was|available|link|page|here)?\s*[\(\[]?\s*$|"
    r"pre-regist\w*\s*(:|at)?\s*[\(\[]?\s*$|"
    r"analysis\s+plan\s*[\(\[:]?\s*$|"
    r"registered\s+report\s*[\(\[:]?\s*$)",
    re.I,
)

#: A deposit that merely CONTAINS a preregistration among other things is still
#: a deposit. When both signals fire, this one wins.
_MULTI_ARTIFACT_DEPOSIT = re.compile(
    r"(all\s+materials|materials\s+includ\w+|includ\w+\s+.{0,80}"
    r"(analysis\s+code|anonymized\s+data|data\s+and\s+code)|"
    r"(analysis\s+code|anonymi[sz]ed\s+data)\s*,)",
    re.I,
)

#: Rule 3 -- inside a reference/citation the deposit belongs to the work being
#: CITED, not to this paper. JATS marks these structurally, which is why this
#: is reliable rather than a guess.
_CITATION_CONTEXT = re.compile(
    r"(<ref\b|</ref>|<mixed-citation|<element-citation|citation-string|"
    r"ref-list|<nlm-citation|\[Preprint\])",
    re.I,
)

#: A PsyArXiv DOI embeds "osf.io" but names a PREPRINT, not a deposit. Without
#: this, 10.31234/osf.io/8r9p7 is scraped as OSF id "8r9p7" and downloaded.
_PREPRINT_DOI = re.compile(r"10\.31234/", re.I)

#: Rule 3 boost -- a URL inside a data-availability section is as strong a
#: signal as prose gets. Not required (only 4 of 13 records have such a
#: section), but it upgrades an otherwise uncertain candidate.
_DAS_SECTION = re.compile(
    r"(data[\s-]*availability|code[\s-]*availability|"
    r"data,\s*materials,?\s*and\s*software\s*availability|<ack\b)",
    re.I,
)

#: Well-known tool orgs. A backstop under both the rules and any LLM verdict:
#: these are libraries the authors USED, never the paper's own deposit.
_TOOL_ORGS = frozenset({
    "stan-dev", "tidyverse", "numpy", "scipy", "pandas-dev", "rstudio",
    "matplotlib", "scikit-learn", "statsmodels", "pytorch", "tensorflow",
    "jupyter", "ipython", "r-lib", "rstan", "pymc-devs", "huggingface",
    "apache", "microsoft", "google", "facebook", "openai",
})


@dataclass
class Candidate:
    """One repository reference found in a paper, and what we decided about it."""

    repo: str                       # "osf", "zenodo", "github", ...
    ident: str                      # the id/DOI as matched
    url: str                        # normalized, resolvable
    verdict: str = UNCERTAIN
    reason: str = ""
    context: str = ""               # prose that drove the verdict; audit trail
    in_das: bool = False
    adjudicated_by: Optional[str] = None    # set when an LLM overrode the rules
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.repo}:{self.ident.lower()}"


_TAG = re.compile(r"<[^>]*>")
#: The window is a slice, so its final tag is usually CUT OFF mid-attribute
#: ('<ext-link xlink:href="') and has no closing bracket for _TAG to match.
#: That unterminated remnant is exactly what sits between the prereg word and
#: the URL, so it has to go too or the anchored test can never fire.
_OPEN_TAG_TAIL = re.compile(r"<[^<>]*$")
_URL_TAIL = re.compile(r"https?:(//)?\S*$", re.I)


def _strip_tags(fragment: str) -> str:
    """Prose only: closed tags, the trailing half-tag, and any dangling URL.

    All three matter for the anchored prereg test, which asks whether the word
    is attached to THIS link rather than merely present in the paragraph.
    """
    text = _TAG.sub(" ", fragment)
    text = _OPEN_TAG_TAIL.sub(" ", text)
    text = " ".join(text.split())
    return _URL_TAIL.sub("", text).rstrip()


def _normalize(repo: str, ident: str) -> str:
    """A URL the downstream enumerators can actually resolve."""
    if repo == "osf":
        return f"https://osf.io/{ident}/"
    if repo == "zenodo":
        if ident.lower().startswith("10.5281"):
            return f"https://doi.org/{ident}"
        # As the DOI, not the web URL: the downstream enumerator matches
        # r"zenodo\.(\d+)", which a /records/<id> path does not satisfy.
        return f"https://doi.org/10.5281/zenodo.{ident}"
    if repo == "dryad":
        return f"https://doi.org/{ident}"
    if repo == "dataverse":
        return f"https://doi.org/{ident}"
    if repo == "figshare":
        # Same reason as Zenodo: the enumerator matches r"figshare\.(\d+)",
        # which a /articles/<id> path does not satisfy.
        return f"https://doi.org/10.6084/m9.figshare.{ident}"
    if repo == "github":
        return f"https://github.com/{ident}"
    return ident


def _judge(repo: str, ident: str, context: str, in_das: bool,
           in_citation: bool = False, before: str = ""):
    """Apply the three rules to one candidate's surrounding prose.

    `in_citation` and `before` are both computed from what PRECEDES the URL,
    not from the whole window -- see _inside_citation for why that distinction
    is load-bearing. `before` is the ~120 chars immediately before the link.
    """
    # Rule 3a: a PsyArXiv DOI is a preprint that merely contains "osf.io".
    if _PREPRINT_DOI.search(context):
        return REFUSE, "PsyArXiv preprint DOI, not a data deposit"

    # Rule 3b: inside a reference, the deposit belongs to the cited work.
    if in_citation:
        return REFUSE, "appears inside a reference/citation entry"

    # Rule 2: a preregistration is not the analysis data -- but only when the
    # word is attached to THIS link (`before` is the text immediately preceding
    # the URL), and not when the sentence describes a multi-artifact deposit
    # that merely includes a prereg alongside the data.
    if _PREREG_LANGUAGE.search(before) and not _MULTI_ARTIFACT_DEPOSIT.search(context):
        return REFUSE, "preregistration, not a data deposit"

    if repo == "github":
        owner = ident.split("/", 1)[0].lower()
        if owner in _TOOL_ORGS:
            return REFUSE, f"{owner} is a known tool/library org"
        # GitHub is where the rules are weakest: "we used X" and "our code is
        # at X" look alike outside a DAS. Only a data-availability section is
        # strong enough on its own; everything else goes to adjudication.
        if in_das and _DEPOSIT_LANGUAGE.search(context):
            return ACCEPT, "code repository named in a data/code availability section"
        return UNCERTAIN, "GitHub link outside a data-availability section"

    # Rule 1: for deposit repositories, deposit language is the signal.
    if _DEPOSIT_LANGUAGE.search(context):
        where = "data-availability section" if in_das else "deposit language nearby"
        return ACCEPT, f"deposit repository with {where}"

    return UNCERTAIN, "repository URL with no deposit language nearby"


#: Markers that OPEN and CLOSE a reference entry. Direction matters, so these
#: are separate from the loose _CITATION_CONTEXT scan.
_CITATION_OPEN = re.compile(r"<(ref|mixed-citation|element-citation|nlm-citation)\b", re.I)
_CITATION_CLOSE = re.compile(r"</(ref|mixed-citation|element-citation|nlm-citation)>", re.I)


def _inside_citation(text: str, position: int) -> bool:
    """True when the URL at `position` sits inside a reference entry.

    Looking for "<ref" anywhere in a symmetric window is wrong, and wrongly
    refused a real deposit: PNAS puts the data-availability sentence
    immediately before <ref-list>, so a citation tag 200 characters AFTER the
    URL disqualified prose that plainly said "publicly available online as an
    OSF project at ...".

    What actually matters is whether the nearest citation delimiter BEFORE the
    URL is an opening tag. If it is, we are inside an entry; if it is a closing
    tag (or there is none), we are in body prose.
    """
    window = text[max(0, position - 4000):position]
    last_open = None
    for match in _CITATION_OPEN.finditer(window):
        last_open = match.start()
    last_close = None
    for match in _CITATION_CLOSE.finditer(window):
        last_close = match.start()
    if last_open is None:
        return False
    if last_close is None:
        return True
    return last_open > last_close


def _section_is_das(text: str, position: int) -> bool:
    """True when the URL sits under a data/code-availability heading.

    Looks back a bounded distance for the nearest heading-ish marker rather
    than parsing JATS: the scan has to work on HTML too, where the structure
    differs, and a bounded window is the same answer for far less machinery.
    """
    window = text[max(0, position - 1200):position]
    return bool(_DAS_SECTION.search(window))


def scan_text(text: str) -> List[Candidate]:
    """Every repository reference in one document, judged.

    Deduplicated by (repo, id): a deposit named five times is one candidate,
    and it keeps the STRONGEST verdict any of its mentions earned -- a URL
    given properly in the data-availability statement is not disqualified by
    also appearing in the reference list.
    """
    if not text:
        return []

    best: Dict[str, Candidate] = {}
    rank = {ACCEPT: 2, UNCERTAIN: 1, REFUSE: 0}

    for repo, pattern in _REPO_PATTERNS:
        for match in pattern.finditer(text):
            ident = match.group(1)
            if not ident:
                continue
            if repo == "github":
                # Strip a trailing ".git" and any path fragment beyond owner/repo.
                ident = ident.rsplit(".git", 1)[0]
            start = match.start()
            context = text[max(0, start - CONTEXT_CHARS):start + CONTEXT_CHARS]
            context = " ".join(context.split())
            in_das = _section_is_das(text, start)
            # Tags are stripped before the "is the prereg word attached to
            # THIS link" test: in JATS the URL lives inside
            # <ext-link xlink:href="..."> so 60+ characters of markup sit
            # between "preregistration:" and the link, and an anchored match
            # against the raw text never fires.
            before = _strip_tags(text[max(0, start - 200):start])
            verdict, reason = _judge(
                repo, ident, context, in_das,
                in_citation=_inside_citation(text, start), before=before)

            candidate = Candidate(
                repo=repo, ident=ident, url=_normalize(repo, ident),
                verdict=verdict, reason=reason, context=context[:600],
                in_das=in_das,
            )
            previous = best.get(candidate.key)
            if previous is None or rank[verdict] > rank[previous.verdict]:
                best[candidate.key] = candidate

    return sorted(best.values(), key=lambda c: (c.repo, c.ident))


#: Artifacts whose text is worth scanning. PDFs are excluded deliberately --
#: extracting their text is a separate cost and a separate failure mode, and
#: the markup formats are where the structural citation signal (rule 3) lives.
_SCANNABLE = (".xml", ".html", ".htm", ".nxml")


def scan_record(stem: str) -> List[Candidate]:
    """Scan whatever full text is already on disk for one record.

    `stem` is the output path stem (no extension), matching how the rest of the
    supplementary subsystem addresses a record.
    """
    candidates: Dict[str, Candidate] = {}
    rank = {ACCEPT: 2, UNCERTAIN: 1, REFUSE: 0}
    for extension in _SCANNABLE:
        path = stem + extension
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for candidate in scan_text(text):
            previous = candidates.get(candidate.key)
            if previous is None or rank[candidate.verdict] > rank[previous.verdict]:
                candidates[candidate.key] = candidate
    return sorted(candidates.values(), key=lambda c: (c.repo, c.ident))


def accepted(candidates: List[Candidate]) -> List[Candidate]:
    return [c for c in candidates if c.verdict == ACCEPT]


def uncertain(candidates: List[Candidate]) -> List[Candidate]:
    return [c for c in candidates if c.verdict == UNCERTAIN]
