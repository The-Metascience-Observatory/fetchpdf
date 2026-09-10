"""Draft emails asking authors for what could not be fetched.

Nothing here sends anything. It writes markdown files you read, edit and send
yourself. That is a deliberate limit, not an unfinished feature:

  * Extraction is ~97% precise, not 100%. One wrong address emails a stranger
    in your name, and there is no undo.
  * Addresses go stale invisibly -- one author appears under four addresses in
    a single corpus, two of them dead.
  * Forensic corpora contain people under scrutiny. Whether and how to contact
    them is a judgement the tool has no business making.

So there is no --send flag, no SMTP, no mail library, and no mailto: auto-open.
Please do not add one.

WHAT EARNS AN EMAIL
-------------------
Only material we can show exists. Three qualifying cases:

    epmc_not_open_access   EPMC's hasSuppl=Y: the files exist, withheld
    declared.missing       the article's own JATS names a file nobody got
    no artifact on disk    the paper itself was never fetched

A record whose supplements simply do not exist gets nothing. Asking an author
for a file that was never published wastes their time and yours, and it is the
same false-alarm failure that once made this tool warn about five records when
only one had anything to withhold.

News and editorial items are excluded outright: there is no paper to request,
and they are also where address extraction is least reliable -- a Nature news
page reprints other articles' author blocks.
"""

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import blocked
from .corresponding import Contact

#: Written to the directory fetchpdf was RUN FROM, not the output dir: these
#: are working documents for a person, so they belong where the person is. The
#: prefix does a folder's job -- groups them in a listing, globbable for
#: cleanup (rm email_request_*.md) -- without claiming a directory we do not
#: own.
FILENAME_PREFIX = "email_request_"
UNRESOLVED_NAME = FILENAME_PREFIX + "UNRESOLVED.md"

#: Why a record is being asked about. Kept distinct because the ask differs:
#: a missing paper is a reprint request, a missing supplement is not.
WANT_PDF = "pdf"
WANT_SUPPLEMENT = "supplement"

#: Whether a human could plausibly get it themselves. See classify_block().
MANUAL_LIKELY = "manual_likely"
BLOCKED_HARD = "blocked_hard"
NOT_PUBLISHED = "not_published"

_DEFAULT_PURPOSE = ("I am compiling a corpus of published work and its "
                    "underlying materials for a research project on "
                    "reproducibility.")


@dataclass
class Ask:
    """One paper, and what is missing from it."""

    doi: str
    title: str = ""
    year: Optional[int] = None
    wants: List[str] = field(default_factory=list)      # WANT_PDF / WANT_SUPPLEMENT
    detail: str = ""                                    # e.g. "Table S1, Appendix A"
    block: str = ""                                     # MANUAL_LIKELY / BLOCKED_HARD
    url: str = ""

    @property
    def summary(self) -> str:
        if WANT_PDF in self.wants and WANT_SUPPLEMENT in self.wants:
            return "the full text and its supplementary materials"
        if WANT_PDF in self.wants:
            return "the full text"
        return self.detail or "the supplementary materials"


def classify_block(status: Optional[int], title: str, links_found: int) -> str:
    """Could a person get this by clicking, or is it a hard wall?

    The obvious test -- "is Cloudflare involved" -- is useless: a
    challenge-platform script is present on pages that work fine. What
    separates them is whether the interstitial CLEARED.

    Measured on three real publisher pages with a headed browser on the same
    VPN: PNAS 200 with an SI link, Wiley 200 with an SI link, SAGE 403 titled
    "Just a moment...". Only the last is a wall.
    """
    blocked_title = bool(blocked.CHALLENGE_TITLE_RE.match(title or ""))
    if status == 403 or blocked_title:
        return MANUAL_LIKELY if not blocked_title else BLOCKED_HARD
    if links_found:
        return ""                    # retrieved fine; nothing to report
    return NOT_PUBLISHED


def _slug(email: str, name: Optional[str] = None) -> str:
    base = (name or email.split("@")[0]).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "author"


@dataclass
class _Group:
    contact: Contact
    asks: List[Ask] = field(default_factory=list)
    alternates: List[Contact] = field(default_factory=list)


def group_by_author(pairs) -> Dict[str, _Group]:
    """Collapse (Contact, Ask) pairs into one group per address.

    One author accounts for many records in a single-author corpus, and 38
    separate drafts would be unusable where one listing eight papers is a
    reasonable thing to send.

    Addresses are grouped case-insensitively, and the group keeps the contact
    from the NEWEST paper -- a 2005 address is probably dead while a 2024 one
    probably is not. The older ones are kept as alternates rather than
    discarded, because the tool cannot actually tell which is live.
    """
    groups: Dict[str, _Group] = {}
    for contact, ask in pairs:
        key = contact.email.lower()
        group = groups.get(key)
        if group is None:
            groups[key] = _Group(contact=contact, asks=[ask])
            continue
        group.asks.append(ask)
        if (ask.year or 0) > (group.contact.paper_year or 0):
            group.alternates.append(group.contact)
            group.contact = contact
        else:
            group.alternates.append(contact)
    for group in groups.values():
        group.asks.sort(key=lambda a: -(a.year or 0))
    return groups


def render(group: _Group, purpose: str = _DEFAULT_PURPOSE) -> str:
    """One draft, as markdown. Neutral, specific, and editable."""
    asks = group.asks
    plural = "s" if len(asks) > 1 else ""
    pdfs = sum(1 for a in asks if WANT_PDF in a.wants)
    supps = sum(1 for a in asks if WANT_SUPPLEMENT in a.wants)

    kind = ("materials" if pdfs and supps
            else "full text" if pdfs else "supplementary materials")
    subject = f"Request for {kind} — {len(asks)} paper{plural}"

    lines = [f"# Request: {kind} ({len(asks)} paper{plural})", "",
             f"**To:** {group.contact.email}", f"**Subject:** {subject}", "",
             "---", "",
             (f"Dear Dr. {group.contact.name}," if group.contact.name
              else "Dear Author,"), "",
             purpose, "",
             f"I was unable to obtain the following from the published record, "
             f"and would be grateful for a copy:", ""]

    for ask in asks:
        year = f" ({ask.year})" if ask.year else ""
        title = f" — {ask.title}" if ask.title else ""
        lines.append(f"- **{ask.doi}**{year}{title}")
        lines.append(f"  - Missing: {ask.summary}")
    lines += ["", "Thank you for your time.", "", "---", ""]

    lines.append(f"*Address source: {group.contact.provenance} "
                 f"(confidence: {group.contact.confidence})*")
    if group.alternates:
        others = ", ".join(
            f"{c.email}" + (f" ({c.paper_year})" if c.paper_year else "")
            for c in dict.fromkeys(
                (c for c in group.alternates if c.email != group.contact.email)))
        if others:
            lines.append(f"*Other addresses seen for this author: {others}*")
    manual = [a for a in asks if a.block == MANUAL_LIKELY and a.url]
    if manual:
        lines += ["", "*Before sending: these may just need one click in your "
                  "own browser —*"]
        lines += [f"*  - {a.url}*" for a in manual]
    lines.append("")
    lines.append("*Draft only. Nothing has been emailed.*")
    return "\n".join(lines) + "\n"


#: Manifest statuses that mean the paper's own supplements were not obtained.
_SUPPLEMENT_GAP_REASONS = frozenset({"epmc_not_open_access"})


def collect_asks(output_dir: str, failed_dois=None, news_dois=None):
    """Walk an output directory and decide what is worth asking for.

    Reads the manifests already written by the supplementary pass, so this can
    run over a finished corpus as easily as at the end of a live run.

    Returns (pairs, unresolved) where pairs is [(Contact, Ask)] and unresolved
    is [Ask] for records with a real gap but no address.
    """
    import glob
    import json

    from .corresponding import find_corresponding_author
    from .supplementary import MANIFEST_SUFFIX

    failed = {str(d).strip().lower() for d in (failed_dois or ())}
    news = {str(d).strip().lower() for d in (news_dois or ())}
    pairs, unresolved = [], []

    pattern = os.path.join(output_dir, "**", "*" + MANIFEST_SUFFIX)
    for manifest_path in sorted(glob.glob(pattern, recursive=True)):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue

        identifiers = manifest.get("identifiers_resolved") or {}
        doi = identifiers.get("doi") or manifest.get("identifier") or ""
        if not doi or doi.strip().lower() in news:
            # No paper to request, and the least reliable place to read an
            # address from -- news pages carry other articles' author blocks.
            continue

        wants, details = [], []
        declared_missing = ((manifest.get("declared") or {}).get("missing")) or []
        if declared_missing:
            wants.append(WANT_SUPPLEMENT)
            details.append(", ".join(str(n) for n in declared_missing[:6]))
        elif any((s or {}).get("reason") in _SUPPLEMENT_GAP_REASONS
                 for s in (manifest.get("skipped") or [])):
            wants.append(WANT_SUPPLEMENT)

        stem = manifest_path[:-len(MANIFEST_SUFFIX)]
        if doi.strip().lower() in failed and not _has_artifact(stem):
            wants.append(WANT_PDF)

        if not wants:
            continue                 # nothing demonstrably missing -- no email

        ask = Ask(doi=doi, wants=wants, detail="; ".join(d for d in details if d),
                  url=f"https://doi.org/{doi}")
        contact = find_corresponding_author(stem, doi=doi)
        if contact:
            ask.year = contact.paper_year
            pairs.append((contact, ask))
        else:
            unresolved.append(ask)
    return pairs, unresolved


def _has_artifact(stem: str) -> bool:
    """Whether any full-text artifact for this record landed on disk."""
    return any(os.path.exists(stem + ext)
               for ext in (".pdf", ".xml", ".nxml", ".fulltext.html", ".html"))


def write_drafts(groups: Dict[str, _Group], unresolved: List[Ask],
                 directory: Optional[str] = None,
                 purpose: str = _DEFAULT_PURPOSE) -> List[tuple]:
    """Write one file per author into `directory` (default: the CWD).

    Returns [(path, n_papers, n_pdfs, n_supplements)] for the run summary.
    """
    directory = directory or os.getcwd()
    written = []
    for group in sorted(groups.values(), key=lambda g: -len(g.asks)):
        path = os.path.join(
            directory, FILENAME_PREFIX + _slug(group.contact.email,
                                               group.contact.name) + ".md")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(render(group, purpose))
        except OSError:
            continue
        written.append((path, len(group.asks),
                        sum(1 for a in group.asks if WANT_PDF in a.wants),
                        sum(1 for a in group.asks if WANT_SUPPLEMENT in a.wants)))

    if unresolved:
        path = os.path.join(directory, UNRESOLVED_NAME)
        lines = ["# Unresolved: no corresponding-author address found", "",
                 "These records are missing material, but no address could be "
                 "extracted from the artifacts on disk. Look them up by hand.", ""]
        for ask in sorted(unresolved, key=lambda a: -(a.year or 0)):
            year = f" ({ask.year})" if ask.year else ""
            lines.append(f"- **{ask.doi}**{year} — missing {ask.summary}")
            if ask.url:
                lines.append(f"  - {ask.url}")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            written.append((path, len(unresolved), 0, 0))
        except OSError:
            pass
    return written
