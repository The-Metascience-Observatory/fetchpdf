"""--pull-supplementary: every supplementary file, alongside the paper.

This is not a tier. The tier walk in engine.py is winner-take-all -- it returns
the moment one artifact is accepted, because its question is "what is the best
single representation of this paper's full text". This pass answers a different
question, "what else did the authors deposit", and the answer is a set, not a
winner. So it runs after retrieval rather than inside it, and it never touches
the record's verdict: a paper with no supplementary material is not a failure,
and a repository serving malformed JSON must not turn a successful download into
a failed one.

Files land as flat siblings of the main artifact:

    10.1371--journal.pone.0000308.pdf
    10.1371--journal.pone.0000308_supplementary_info_1.doc
    10.1371--journal.pone.0000308_supplementary_info_2.xls
    10.1371--journal.pone.0000308_supplementary_info.json

The numbering makes the manifest mandatory rather than nice to have: it is the
only record of what _2 originally was, what it was called, where it came from,
and what was refused for being too large. "Nothing was offered" and "one 4 GB
HDF5 was refused" are different facts about a record and must not collapse into
the same empty directory listing.

Numbering is deterministic, because it has no other anchor. Same remote content
gives the same files in the same order across re-runs and across worker counts --
see _sort_key.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import blocked
from .http import apply_default_mode, redact
from .linked_artifacts import write_linked_sidecar
from .supplement_index import (
    ROLE_ARTICLE,
    ROLE_FIGURE,
    SupplementFile,
    _epmc_withholds_supplements,
    _matches_href,
    enumerate_all,
    provider_rank,
)
from .tiers import ARTIFACT_EXTENSIONS

_SCHEMA_VERSION = 1

#: The infix that makes a supplementary sibling recognisable. Anything
#: downstream globbing *.pdf over an output directory will pick these up too, so
#: it needs to be distinctive enough to filter on.
INFIX = "_supplementary_info"
MANIFEST_SUFFIX = INFIX + ".json"

#: Linked deposits (Zenodo, OSF, Dryad, ...) land under
#: <stem>_data_artifacts/<deposit>/ with their ORIGINAL filenames, rather than
#: in the flat _supplementary_info_N_ namespace the paper's own SI uses. They
#: are a different kind of thing -- often hundreds of MB of third-party data --
#: and mixing them in buried a paper's four real files under 199 members of a
#: vendored Stata package.
DATA_ARTIFACT_INFIX = "_data_artifacts"

#: Every supplementary file, downloaded or unpacked, is named
#:
#:     {stem}_supplementary_info_{index}_{descriptor}{ext}
#:
#: One rule, because two would be worse than either: a reader seeing a name with
#: no descriptor could not tell whether the provider gave no usable name or
#: whether a different code path produced the file.
#:
#: The index stays the anchor -- deterministic, unique, and the thing the
#: manifest keys on. The descriptor is added because a name is the only thing an
#: agent sees when deciding what to open, and "TablesS1-S5" earns its place.
#: Measured across this corpus, though, only ~20% of provider names carry a
#: content word; the rest are codes like "jamanetwopen-e2337679-s001" or
#: Springer's generic "MOESM1_ESM". So the descriptor is a bonus, never the
#: identifier, and it is omitted entirely when it would say nothing.
#:
#: A kept archive's members are unpacked beside it and take indices of their own.
#: The archive itself stays: it is the only proof of what the members came from.
DESCRIPTOR_MAX_CHARS = 60

#: Per-file cap. The CLI default; --max-supplementary-mb overrides it.
DEFAULT_MAX_FILE_BYTES = 300 * 1024 * 1024

#: An archive's members are unknowable until it has landed, so the per-file cap
#: cannot gate the transfer. These bound the damage instead.
BUNDLE_CAP_MULTIPLE = 4
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_FILES = 200

#: zipfile will happily inflate a bomb. Refuse an archive whose members claim to
#: expand to more than this.
_RATIO_GUARD_MULTIPLE = 20

#: Bodies that are a challenge page rather than the file that was asked for.
#: Imported rather than restated: this list used to live here, a second one in
#: repository_waf.py and a title regex in request_drafts.py, so a page one of
#: them recognised was invisible to the other two.
_CHALLENGE_MARKERS = blocked.CHALLENGE_BODY_MARKERS

_TEXTUAL_EXTENSIONS = (".html", ".htm", ".txt", ".csv", ".tsv", ".xml", ".json", ".md")

#: Formats whose bytes cannot legitimately begin with `<`. Used to catch an API
#: that answers a file request with an XML wrapper describing the file -- see
#: `_looks_like_a_document`.
_BINARY_DOC_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".eps",
    ".mov", ".mp4", ".avi", ".wmv", ".sav", ".dta", ".mat",
)

_MAGIC = (
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"\x1f\x8b", ".gz"),
    (b"\x89PNG", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
    (b"Rar!", ".rar"),
    (b"7z\xbc\xaf", ".7z"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
    (b"BZh", ".bz2"),
)

_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/msword": ".doc",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/plain": ".txt",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-tar": ".tar",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "video/mp4": ".mp4",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
}

#: Skip reasons that mean a file the record has was not obtained, as opposed to
#: one that was obtained through a different provider. Only these make a run
#: "partial"; a duplicate is a complete result, not a shortfall. "no-bundle" is
#: deliberately absent: a speculative listing (EPMC constructs its bundle URL
#: for every PMCID without checking) answered by 404 or an <errorBean> stub is
#: a clean negative, not a loss -- misfiling it here once turned 40 negatives
#: into phantom "partial" statuses across a 99-record corpus.
_LOST_REASONS = frozenset({
    "too-large", "http-error", "unreachable", "empty", "not-a-document",
    "not-an-archive", "decompression-ratio", "unreadable-member", "unwritable",
    # A refusal of this client is a shortfall like any other -- more so, since
    # the file demonstrably exists at the other end of a URL a person can open.
    blocked.BLOCKED,
    # The files exist and were withheld -- that is a shortfall, unlike
    # "no-bundle", which means there was nothing to fetch in the first place.
    "epmc_not_open_access",
})

#: EPMC's phrasing when it holds supplements it is not permitted to serve.
#: Matched on the message, not the status code: the refusal arrives as HTTP 200
#: wrapping a 165-byte <errorBean>, so the status says "fine" while the body
#: says "no".
_NOT_OPEN_ACCESS_RE = re.compile(r"not\s+open\s+access", re.I)

#: Records whose supplements Europe PMC withheld this run, for the end-of-run
#: summary. A 500-record batch should give a number and a list, not 500 lines
#: the user has to scroll back through.
_NOT_OPEN_ACCESS_RECORDS: List[str] = []
_NOA_LOCK = threading.Lock()


def _print_yellow_warning(message: str) -> None:
    """Yellow, like the CORE and news-item notices in fetchpdf.py.

    Defined here rather than imported: fetchpdf.py imports this module, so
    reaching back into it would be a cycle.
    """
    print(f"\033[93m{message}\033[0m")


def not_open_access_records() -> List[str]:
    """DOIs whose supplements EPMC held but refused, for the run summary."""
    with _NOA_LOCK:
        return list(_NOT_OPEN_ACCESS_RECORDS)


def reset_not_open_access_records() -> None:
    """Exists for tests; a batch is one process."""
    with _NOA_LOCK:
        _NOT_OPEN_ACCESS_RECORDS.clear()

#: Refusals worth a slow second look. HTTP layer already retries 429/503 once
#: with Retry-After; this is the record-level list for the escalating rounds in
#: pull_for_record. 403/404/410 are answers, not blips, and stay terminal.
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Waits between record-level retry rounds, chosen to slow down dramatically:
#: a transient failure gets a second chance quickly, then the run backs far off
#: rather than hammering a struggling host.
_RETRY_WAITS = (20, 90, 300)

#: Suffixes trusted as extensions when they appear on a listed filename. Anything
#: outside this set loses to the served Content-Type and to the magic bytes -- see
#: _extension_for for why that ordering matters.
_KNOWN_EXTENSIONS = frozenset(
    e.lstrip(".") for e in _CONTENT_TYPE_EXTENSIONS.values()
) | frozenset("""
    pdf doc docx xls xlsx xlsm ods csv tsv txt rtf odt ppt pptx odp
    zip gz tgz bz2 xz 7z rar tar
    png jpg jpeg gif tif tiff bmp svg eps ps webp heic
    mp4 mov avi mkv webm wmv mp3 wav flac m4a
    xml json yaml yml html htm md tex bib
    h5 hdf5 nc mat sav dta rds rda rdata sas7bdat npy npz parquet feather
    fasta fastq fa vcf bam sam bed gff gtf sra cel idat nii mgf mzml raw
    py r m do sh ipynb nb sav pkl db sqlite
""".split())

#: One lock per output stem, so two workers handed the same DOI twice in one CSV
#: cannot interleave each other's numbering. os.replace already prevents torn
#: files; this prevents run A's _1 sitting next to run B's _2.
_STEM_LOCKS: Dict[str, threading.Lock] = {}
_STEM_LOCKS_GUARD = threading.Lock()


@dataclass
class SupplementarySummary:
    """What the pass did. Never an input to whether the record succeeded."""

    #: incomplete > partial > error > ok > none_found. "incomplete" means a file
    #: the article's own JATS declares was not obtained -- named data loss, and
    #: the one status callers must surface loudly. "partial" means an undeclared
    #: listed file was lost; "skipped" is the repair path's no-op.
    status: str = "none_found"     # ok | none_found | partial | incomplete | error | skipped
    written: int = 0
    skipped: int = 0
    bytes_written: int = 0
    manifest_path: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    detail: str = ""
    #: Declared-but-not-obtained original filenames, when status == "incomplete".
    missing_declared: List[str] = field(default_factory=list)
    #: URLs a publisher or CDN refused to this client but serves to a person.
    #: Carried on the summary rather than left in the manifest because the
    #: missing-materials report is where somebody will actually see them.
    blocked_urls: List[str] = field(default_factory=list)

    def __bool__(self):
        return self.written > 0


def pull_for_record(raw_identifier, doi=None, pmid=None, save_path=None,
                    resolver=None, ladder=None, http=None,
                    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
                    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
                    max_files=DEFAULT_MAX_FILES,
                    refresh=False, providers=None, email=None,
                    verbose=False, delay=0.1, use_playwright=False,
                    download_data_artifacts=False,
                    max_data_artifact_bytes=None,
                    llm_adjudicate_artifacts=False,
                    llm_agent_retrieval=False,
                    llm_backend=None,
                    max_llm_records=0,
                    llm_model=None,
                    unpack_data_artifacts=False,
                    download_related_unverified=False,
                    verify_hashes=True) -> SupplementarySummary:
    """Fetch every supplementary file for one record into flat siblings of save_path.

    Never raises for a network or provider failure; the outcome is the returned
    summary and the manifest beside the artifact. Callers must not let the result
    influence whether the record succeeded -- see the module docstring.
    """
    if not save_path:
        return SupplementarySummary(status="error", detail="no save_path")

    stem = stem_for(save_path)
    manifest_path = stem + MANIFEST_SUFFIX

    with _stem_lock(stem):
        # The manifest, not the files, is the skip signal. On a real corpus most
        # records have no supplementary material at all, and that is a fact worth
        # recording: without it every re-run would re-enumerate every empty record
        # at 3-6 requests each, which on run 2 onward is the entire cost of the
        # flag.
        if os.path.exists(manifest_path) and not refresh:
            return _repair(manifest_path, stem, resolver, http, ladder, email,
                           verbose, max_file_bytes, verify_hashes)

        ladder, http, resolver, owns_resolver = _wire(
            resolver, http, ladder, save_path, email, verbose)
        ctx = _context(http, resolver, ladder, save_path, verbose, email, delay,
                       use_playwright,
                       download_data_artifacts=download_data_artifacts,
                       max_data_artifact_bytes=max_data_artifact_bytes,
                       llm_adjudicate_artifacts=llm_adjudicate_artifacts,
                       llm_agent_retrieval=llm_agent_retrieval,
                       llm_backend=llm_backend,
                       max_llm_records=max_llm_records,
                       llm_model=llm_model,
                       download_related_unverified=download_related_unverified)

        ids = _resolve(resolver, raw_identifier, doi, pmid)
        listed, reports = enumerate_all(ids, ctx, providers)

        run = _Run(stem=stem, ctx=ctx, http=http, ids=ids,
                   max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
                   max_files=max_files,
                   max_artifact_bytes=max_data_artifact_bytes,
                   unpack_data_artifacts=unpack_data_artifacts)
        run.absorb_main_artifact()
        for entry in sorted(_wanted(listed), key=_sort_key):
            if not run.has_budget():
                break
            # Linked deposits answer to their own ceiling, so one large
            # replication package cannot consume the allowance the paper's own
            # supplementary files need.
            if run._is_data_artifact(entry) and not run.artifact_has_budget(entry):
                continue
            run.take(entry)
        _retry_transients(run, stem)

        # Providers that had to download into a temp dir (atypon_suppl, and
        # the retrieval agent's downloading backend) are done with it once
        # every file has been committed.
        for module in ("supplement_atypon", "llm_agent_retrieval"):
            try:
                cleanup = __import__(f"fetchpdf.retrieval.{module}",
                                     fromlist=["cleanup_scratch"])
                cleanup.cleanup_scratch(ctx)
            except Exception:
                pass
        summary = run.finish(manifest_path, ids, reports, max_file_bytes)
        # The link services' answers, including the empty ones. Written after
        # the manifest so a crash between the two loses the links, never the
        # account of the files. No-op when no link provider ran.
        write_linked_sidecar(stem, ids, ctx, verbose=verbose)
        if owns_resolver:
            # Batch mode flushes once at the end instead; doing it per record
            # there would rewrite a file that grows with every row.
            try:
                resolver.cache.flush()
            except Exception:
                pass
        return summary


# -- selection --------------------------------------------------------------


def _wanted(listed: List[SupplementFile]) -> List[SupplementFile]:
    """Everything except the paper itself and its figures.

    The one filter. --pull-supplementary was asked for as much as possible, so
    ROLE_UNKNOWN is kept: a spurious file costs disk, a dropped one loses data.
    """
    return [f for f in listed if f.role not in (ROLE_ARTICLE, ROLE_FIGURE) and f.url]


def _sort_key(entry: SupplementFile) -> tuple:
    """Provider order, then natural filename order, then URL.

    Determinism is not a nicety here. The output names carry no information --
    _2.xlsx means nothing without the manifest -- so if the order shifted between
    runs, the same file would land under different names and a re-run would look
    like a different set of supplements. Provider rank comes first so appending a
    provider cannot renumber the files an earlier one already produced; API
    listing order is deliberately not the key, because neither Zenodo nor figshare
    guarantees it.
    """
    return (provider_rank(entry.provider), _natural(entry.basename), entry.url or "")


def _natural(name: str) -> tuple:
    """Digit runs compared as ints, so Table_S2 sorts before Table_S10."""
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part.lower())
        for part in re.split(r"(\d+)", name or "")
        if part != ""
    )


# -- the run ----------------------------------------------------------------


class _Run:
    """One record's pass: download, dedupe, name, and account for everything."""

    def __init__(self, stem, ctx, http, ids, max_file_bytes, max_total_bytes,
                 max_files, max_artifact_bytes=None,
                 unpack_data_artifacts=False):
        self.stem = stem
        self.ctx = ctx
        self.http = http
        self.ids = ids
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self.max_artifact_bytes = max_artifact_bytes
        self.unpack_data_artifacts = unpack_data_artifacts
        self.directory = os.path.dirname(os.path.abspath(stem)) or "."

        self.kept: List[dict] = []
        self.skipped: List[dict] = []
        #: (entry, its skip record) for refusals worth a slow second look --
        #: consumed by _retry_transients, which pops the stale record and
        #: re-takes the entry.
        self.transient: List[Tuple[SupplementFile, dict]] = []
        self.bytes_written = 0
        #: Bytes spent on LINKED DATA ARTIFACTS, counted separately from the
        #: paper's own supplementary files. A replication package can be
        #: hundreds of MB, and sharing one budget would let a single dataset
        #: starve the SI the record actually needs (or the reverse).
        self.artifact_bytes_written = 0
        #: Computed sha256 -> index. Authoritative, and the only cross-origin key.
        self.by_hash: Dict[str, int] = {}
        #: Provider-declared digest -> index. Kept apart from by_hash because a
        #: declared md5 and a computed sha256 are not comparable; conflating them
        #: would make every declared-checksum lookup a guaranteed miss.
        self.by_declared: Dict[str, int] = {}
        self.by_name_size: Dict[Tuple[str, Optional[int]], int] = {}
        self.by_name_within_origin: Dict[Tuple[Optional[str], str], int] = {}
        #: Size -> path of an artifact already on disk for this record, with its
        #: hash filled in only if some candidate turns out to match that size.
        self.main_by_size: Dict[int, str] = {}
        self.main_hash_by_size: Dict[int, Optional[str]] = {}

    # -- budgets ----------------------------------------------------------

    def _warn_not_open_access(self, stub: str) -> None:
        """Say out loud that supplements exist and cannot be fetched.

        Loud, and per-record rather than once per run: unlike a dead API key,
        each occurrence names a DIFFERENT paper whose SI the user may want by
        hand, so collapsing them would hide the list. This silence is what let
        the gap go unnoticed until a colleague reported it.
        """
        identifier = (getattr(self.ids, "doi", None)
                      or getattr(self.ids, "pmcid", None) or "record")
        pmcid = getattr(self.ids, "pmcid", None) or "the PMC record"
        _print_yellow_warning(
            f"⚠️  {identifier}: Europe PMC reports supplementary material exists "
            f"but may not serve it -- {pmcid} is not open access. Other "
            f"retrieval routes may still succeed; the final supplement manifest "
            f"records what was obtained and what remains missing."
        )
        with _NOA_LOCK:
            _NOT_OPEN_ACCESS_RECORDS.append(str(identifier))

    def _artifact_dir(self, entry) -> Optional[str]:
        """The directory a linked deposit's files go in, created on demand.

        `<stem>_data_artifacts/<repo>_<id>/`, so a 14 MB third-party CSV is
        visibly not the paper's own supplementary material and each deposit
        stays grouped. Returns None for the paper's own files, which keep the
        flat numbered scheme they have always had -- changing that would
        renumber every existing corpus.
        """
        if not self._is_data_artifact(entry):
            return None
        provider = (getattr(entry, "provider", "") or "").replace(":", "_")
        origin = (getattr(entry, "origin_doi", "") or "").strip().lower()
        slug = re.sub(r"[^a-z0-9._-]+", "_", origin).strip("._-")[:60] or provider
        path = os.path.join(self.directory,
                            os.path.basename(self.stem) + DATA_ARTIFACT_INFIX,
                            slug)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return None            # fall back to flat rather than lose the file
        return path

    @staticmethod
    def _is_data_artifact(entry) -> bool:
        """True for a file routed from a linked deposit rather than the paper.

        Keyed on the provider tag: _files_in_repository stamps routed files as
        "<via>:<provider>", so the prefix names the discovery route.
        """
        # The retrieval agent's DATASET files are tagged
        # "fulltext_scan:llm_agent" precisely so they match the
        # "fulltext_scan" prefix below and land in _data_artifacts/ on the
        # data budget. Its SUPPLEMENT files are tagged "llm_agent", which
        # deliberately does not match: those are the paper's own SI and belong
        # in the flat numbered namespace beside the PDF.
        provider = getattr(entry, "provider", "") or ""
        return provider.startswith(("scholix_related:", "epmc_datalinks:",
                                    "datacite_related:", "fulltext_scan"))

    def artifact_has_budget(self, entry) -> bool:
        """The separate ceiling for linked data artifacts."""
        if self.max_artifact_bytes is None:
            return True
        if self.artifact_bytes_written >= self.max_artifact_bytes:
            self._note_budget(
                f"per-record data-artifact limit {self.max_artifact_bytes} bytes reached")
            return False
        return True

    def has_budget(self) -> bool:
        if len(self.kept) >= self.max_files:
            self._note_budget(f"per-record file limit {self.max_files} reached")
            return False
        if self.bytes_written >= self.max_total_bytes:
            self._note_budget(f"per-record byte limit {self.max_total_bytes} reached")
            return False
        return True

    def _note_budget(self, reason: str) -> None:
        if not any(s.get("reason") == "record-budget" for s in self.skipped):
            self.skipped.append({"reason": "record-budget", "detail": reason})
            self.ctx.log(f"    supplements: {reason}")

    # -- the main artifact ------------------------------------------------

    def absorb_main_artifact(self) -> None:
        """Note the sizes of what is already on disk for this record.

        Repositories routinely list the paper alongside its supplements, and the
        role heuristics only guess at that from a filename. Byte identity settles
        it. Sizes are collected now and hashed lazily, on an exact size match
        only, so a record with a 30 MB PDF is not sha256'd for nothing.
        """
        for extension in ARTIFACT_EXTENSIONS:
            path = self.stem + extension
            try:
                if os.path.isfile(path):
                    self.main_by_size.setdefault(os.path.getsize(path), path)
            except OSError:
                continue

    def _is_the_main_artifact(self, size: int, digest: str) -> Optional[str]:
        path = self.main_by_size.get(size)
        if not path:
            return None
        if size not in self.main_hash_by_size:
            self.main_hash_by_size[size] = _hash_file(path)
        return path if self.main_hash_by_size[size] == digest else None

    # -- taking one entry -------------------------------------------------

    def take(self, entry: SupplementFile) -> None:
        if entry.is_archive:
            self._take_archive(entry)
        else:
            self._take_file(entry)

    def _take_file(self, entry: SupplementFile) -> None:
        # The free refusal: a listing that declares an oversized file costs us
        # nothing to decline.
        if entry.size_bytes is not None and entry.size_bytes > self.max_file_bytes:
            self._refuse(entry, "too-large", declared_bytes=entry.size_bytes)
            return
        if self._already_seen(entry):
            return

        staged = self._stage()
        if staged is None:
            return
        result = self.http.download(
            entry.url, staged, self.max_file_bytes, polite=entry.polite
        )
        if not result.ok:
            _unlink(staged)
            self._refuse(entry, result.outcome,
                         declared_bytes=result.declared_length,
                         detail=result.detail, url=result.request_url,
                         status=result.status)
            return

        self._commit(entry, staged, result.sha256, result.bytes_written,
                     result.content_type, url=result.url)

    def _take_archive(self, entry: SupplementFile) -> None:
        """Fetch a bundle under its own cap, then expand it member by member.

        A zip's central directory sits at the end of the file, so nothing about
        its members is knowable until the whole archive has landed. The per-file
        cap therefore cannot gate this transfer; a larger bundle cap bounds it,
        and the per-file cap is applied to each member on the way out.
        """
        bundle_cap = min(self.max_file_bytes * BUNDLE_CAP_MULTIPLE, self.max_total_bytes)
        staged = self._stage()
        if staged is None:
            return
        result = self.http.download(entry.url, staged, bundle_cap, polite=entry.polite)
        if not result.ok:
            _unlink(staged)
            # A speculative listing was never evidence the bundle exists, so a
            # 404 (or 410) is the endpoint saying "nothing here" -- verified
            # reproducible at concurrency 1 -- not a download that failed.
            if entry.extra.get("speculative") and result.status in (404, 410):
                self._refuse(entry, "no-bundle", url=result.request_url,
                             status=result.status)
            else:
                self._refuse(entry, result.outcome,
                             declared_bytes=result.declared_length,
                             detail=result.detail, url=result.request_url,
                             status=result.status)
            return

        try:
            self._expand_zip(entry, staged, result)
        finally:
            _unlink(staged)

    def _expand_zip(self, entry: SupplementFile, staged: str, result) -> None:
        # EPMC's other way of saying "no bundle": HTTP 200 wrapping a tiny
        # <errorBean> XML stub ("Article with id PMC... is not open access
        # one", 165 bytes live). Catching it before ZipFile keeps the honest
        # not-an-archive reason for payloads that were supposed to be zips.
        stub = _error_bean(staged)
        if stub is not None:
            # Two very different facts arrive through the same stub, and only
            # one is worth a human's attention: "this article has no
            # supplements" versus "it HAS supplements and Europe PMC is not
            # allowed to give them to you".
            #
            # The stub CANNOT tell them apart. Europe PMC returns the identical
            # body either way -- verified: PMC11215513 (hasSuppl=N) and
            # PMC3458378 (hasSuppl=Y) both answer 200 with "...is not open
            # access one". The message means "this article is closed", not "we
            # are withholding files". An earlier version of this branch keyed on
            # that text and warned about five records when only one of them had
            # any supplements to withhold.
            #
            # hasSuppl is the authority, so ask the search index.
            if _NOT_OPEN_ACCESS_RE.search(stub or "") \
                    and _epmc_withholds_supplements(
                        self.ids, self.ctx, default_without_pmcid=False):
                self._refuse(entry, "epmc_not_open_access", detail=stub,
                             url=result.url)
                # Breadcrumb for E19: the publisher fallback costs a headed
                # browser, so it only runs for the records EPMC has actually
                # said it is withholding.
                self.ctx.scratch["epmc_withheld_supplements"] = True
                self._warn_not_open_access(stub)
            else:
                # Includes the closed-access-but-no-supplements case, which is
                # a clean negative: nothing exists, nothing was lost, say
                # nothing.
                self._refuse(entry, "no-bundle", detail=stub, url=result.url)
            return
        try:
            archive = zipfile.ZipFile(staged)
        except (zipfile.BadZipFile, OSError) as e:
            self._refuse(entry, "not-an-archive", detail=str(e)[:120],
                         url=result.url)
            return

        with archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            declared = sum(m.file_size for m in members)
            allowance = max(result.bytes_written * _RATIO_GUARD_MULTIPLE, self.max_file_bytes)
            if declared > allowance:
                self._refuse(entry, "decompression-ratio",
                             declared_bytes=declared, url=result.url,
                             detail=f"{declared} bytes from a {result.bytes_written}-byte archive")
                return

            # Sorted by name, not left in the archive's own order. A zip's
            # central directory preserves whatever order the producer wrote, and
            # Europe PMC's is not the obvious one -- its s001-s004 bundle stores
            # s002 first -- so honouring it would number the members in a way that
            # matches neither _sort_key nor anybody's expectation.
            ordered = sorted(
                members,
                key=lambda m: _natural(os.path.basename(m.filename.replace("\\", "/"))),
            )
            for index, member in enumerate(ordered):
                if not self.has_budget():
                    return
                # Member names are untrusted input. They are never joined to a
                # path here -- output names are generated -- but take the
                # basename anyway so a traversing name cannot survive into the
                # manifest either.
                name = os.path.basename(member.filename.replace("\\", "/"))
                member_entry = SupplementFile(
                    name=name or "supplement",
                    url=entry.url,
                    provider=entry.provider,
                    listing_index=index,
                    size_bytes=member.file_size,
                    role=entry.role,
                    origin_doi=entry.origin_doi,
                    extra=dict(entry.extra),
                )
                if member.file_size > self.max_file_bytes:
                    self._refuse(member_entry, "too-large",
                                 declared_bytes=member.file_size,
                                 container=entry.basename)
                    continue
                if self._already_seen(member_entry, container=entry.basename):
                    continue
                try:
                    payload = archive.read(member)
                except Exception as e:
                    self._refuse(member_entry, "unreadable-member",
                                 detail=str(e)[:120], container=entry.basename)
                    continue

                staged_member = self._stage()
                if staged_member is None:
                    return
                with open(staged_member, "wb") as f:
                    f.write(payload)
                apply_default_mode(staged_member)
                self._commit(member_entry, staged_member,
                             hashlib.sha256(payload).hexdigest(), len(payload),
                             "", url=result.url, container=entry.basename)

    # -- dedupe -----------------------------------------------------------

    def _already_seen(self, entry: SupplementFile, container=None) -> bool:
        """Cheap dedupe, before spending a transfer on it.

        A duplicate deliberately does not consume an index: a gap would make _3
        mean different things on different runs.
        """
        if entry.checksum:
            index = self.by_declared.get(_digest_of(entry.checksum))
            if index:
                self._refuse(entry, "duplicate-of", duplicate_of_index=index,
                             container=container)
                return True
        key = (entry.basename.lower(), entry.size_bytes)
        if entry.basename and entry.size_bytes is not None and key in self.by_name_size:
            self._refuse(entry, "duplicate-of",
                         duplicate_of_index=self.by_name_size[key], container=container)
            return True
        # Name alone is only evidence within one deposit. Across origins,
        # "Supplementary_Table_1.xlsx" in a Dryad deposit and in a figshare
        # deposit are routinely different files, so those go to the hash check
        # after download instead.
        origin_key = (entry.origin_doi, entry.basename.lower())
        if entry.basename and origin_key in self.by_name_within_origin:
            self._refuse(entry, "duplicate-of",
                         duplicate_of_index=self.by_name_within_origin[origin_key],
                         container=container)
            return True
        return False

    # -- commit -----------------------------------------------------------

    def _commit(self, entry, staged, digest, size, content_type, url=None,
                container=None) -> None:
        head = _head_of(staged)
        plausible, why = _looks_like_a_document(entry.basename, head, content_type, size)
        if not plausible:
            _unlink(staged)
            # "not-a-document" and "blocked" are both refusals of these bytes,
            # and only the second says a person could get the file. Asked of
            # the same marker list _looks_like_a_document uses, so the two
            # cannot disagree about what a challenge page is.
            reason = (blocked.BLOCKED if blocked.looks_like_challenge_body(head)
                      else "not-a-document")
            self._refuse(entry, reason, detail=why, url=url,
                         container=container)
            return

        duplicate = self.by_hash.get(digest)
        if duplicate:
            _unlink(staged)
            self._refuse(entry, "duplicate-of", duplicate_of_index=duplicate,
                         url=url, container=container, sha256=digest)
            return

        main = self._is_the_main_artifact(size, digest)
        if main:
            _unlink(staged)
            self._refuse(entry, "duplicate-of-main-artifact", url=url,
                         container=container, sha256=digest,
                         main_artifact=os.path.basename(main))
            return

        index = len(self.kept) + 1
        extension = _extension_for(entry.basename, content_type, head)
        artifact_dir = self._artifact_dir(entry)
        if artifact_dir:
            # A linked deposit keeps its own filenames inside its own directory.
            # The flat _supplementary_info_N_ scheme exists because the paper's
            # own SI has no other namespace; a deposit does, and forcing it into
            # the flat one buried four real files under 199 members of a
            # vendored Stata package in a single record.
            filename = _safe_member_name(entry.basename, extension, index)
            final = os.path.join(artifact_dir, filename)
            filename = os.path.join(os.path.basename(artifact_dir), filename)
        else:
            filename = si_filename(os.path.basename(self.stem), index,
                                   entry.basename, extension)
            final = os.path.join(self.directory, filename)
        try:
            os.replace(staged, final)
        except OSError as e:
            _unlink(staged)
            self._refuse(entry, "unwritable", detail=str(e)[:120], url=url)
            return

        self.by_hash[digest] = index
        if entry.checksum:
            self.by_declared.setdefault(_digest_of(entry.checksum), index)
        if entry.basename:
            self.by_name_size[(entry.basename.lower(), size)] = index
            self.by_name_size.setdefault((entry.basename.lower(), entry.size_bytes), index)
            self.by_name_within_origin[(entry.origin_doi, entry.basename.lower())] = index
        self.bytes_written += size
        if self._is_data_artifact(entry):
            self.artifact_bytes_written += size
        self.kept.append({
            "index": index,
            "filename": filename,
            "original_name": entry.name,
            "label": entry.label,
            "provider": entry.provider,
            "listing_index": entry.listing_index,
            "container": container,
            "url": _provenance_url(entry, url),
            "content_type": content_type,
            "role": entry.role,
            "bytes": size,
            "sha256": digest,
            "retrieved_at": _iso(time.time()),
        })
        self.ctx.log(f"    📎 {filename} ← {entry.provider} ({size} bytes)")

        # A supplementary file that is itself a zip gets unpacked alongside.
        # Distinct from _take_archive, which expands the TRANSPORT bundle (the
        # Europe PMC container) and never keeps it. This one is a file the authors
        # deposited, so the archive stays and its members join it.
        # A data artifact's archive is KEPT WHOLE by default. A replication
        # package is a coherent thing with its own internal layout, and
        # unpacking one produced 199 files from a vendored Stata library in a
        # single record. --unpack-data-artifacts opts back in.
        if _is_zip(final) and not (self._artifact_dir(entry)
                                   and not self.unpack_data_artifacts):
            self._unpack_stored_zip(final, filename, entry, self.kept[-1])

    def _unpack_stored_zip(self, archive_path: str, archive_filename: str, entry,
                           archive_record: dict) -> None:
        """Unpack a kept supplementary archive beside itself.

        Reuses the same defences as the bundle expander: the decompression-ratio
        guard, the per-file cap, the running budget, and basenaming every member
        so a traversing name ("../../etc/passwd") cannot escape the directory or
        reach the manifest.
        """
        try:
            archive = zipfile.ZipFile(archive_path)
        except (zipfile.BadZipFile, OSError) as e:
            self._refuse(entry, "not-an-archive", detail=str(e)[:120],
                         container=archive_filename)
            return

        taken = 0
        with archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if not members:
                return
            declared = sum(m.file_size for m in members)
            on_disk = os.path.getsize(archive_path)
            allowance = max(on_disk * _RATIO_GUARD_MULTIPLE, self.max_file_bytes)
            if declared > allowance:
                self._refuse(entry, "decompression-ratio", declared_bytes=declared,
                             container=archive_filename,
                             detail=f"{declared} bytes from a {on_disk}-byte archive")
                return

            for member in sorted(members, key=lambda m: _natural(
                    os.path.basename(m.filename.replace("\\", "/")))):
                if not self.has_budget():
                    break
                name = os.path.basename(member.filename.replace("\\", "/"))
                if not name or name.startswith("."):
                    continue          # __MACOSX/, .DS_Store and friends
                if member.file_size > self.max_file_bytes:
                    self._refuse(entry, "too-large", declared_bytes=member.file_size,
                                 container=archive_filename, detail=name)
                    continue
                try:
                    payload = archive.read(member)
                except Exception as e:
                    self._refuse(entry, "unreadable-member", detail=f"{name}: {str(e)[:90]}",
                                 container=archive_filename)
                    continue

                index = len(self.kept) + 1
                extension = os.path.splitext(name)[1].lower() or ".bin"
                out_name = si_filename(os.path.basename(self.stem), index,
                                       name, extension)
                out_path = os.path.join(self.directory, out_name)
                try:
                    _write_bytes_atomic(out_path, payload)
                except OSError as e:
                    self._refuse(entry, "unwritable", detail=str(e)[:120],
                                 container=archive_filename)
                    continue

                self.bytes_written += len(payload)
                taken += 1
                # Recorded in the manifest like any other file. An extracted
                # member that existed only on disk would be untraceable: nothing
                # else says which archive it came from or what it was called.
                self.kept.append({
                    "index": index,
                    "filename": out_name,
                    "original_name": name,
                    "provider": entry.provider,
                    "extracted_from": archive_filename,
                    "url": redact(entry.url),
                    "role": entry.role,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "retrieved_at": _iso(time.time()),
                    # Nested archives are left packed rather than expanded
                    # recursively: one level is the useful case, and unbounded
                    # recursion is how a zip quine becomes a disk-filling loop.
                    "nested_archive": bool(name.lower().endswith(".zip")),
                })
            if taken:
                self.ctx.log(f"    📦 {archive_filename} → {taken} extracted file(s)")

        # Only after a successful extraction, and only outside the `with` so the
        # handle is closed first (Windows will not unlink an open file).
        if taken:
            _unlink(archive_path)
            self.bytes_written -= archive_record.get("bytes", 0)
            archive_record["extracted_members"] = taken
            # The record stays, marked. Dropping it would lose the only account
            # of where the members came from, and _repair would see a manifest
            # entry with no file and try to download the archive all over again.
            archive_record["removed_after_extraction"] = True
            archive_record["filename"] = None

    def _refuse(self, entry, reason, **fields) -> None:
        record = {
            "original_name": entry.name,
            "provider": entry.provider,
            "url": redact(fields.pop("url", None) or entry.url),
            "reason": reason,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        self.skipped.append(record)
        if _is_transient(reason, record.get("status")):
            self.transient.append((entry, record))
        self.ctx.log(f"    ✗ {entry.basename or entry.provider}: {reason}")

    def _stage(self) -> Optional[str]:
        """A staging path in the output dir.

        Downloads land here first because the keep-or-drop decision needs the
        sha256, and the sha256 needs the bytes. Two same-directory renames per
        file is free next to the transfer, and it keeps the property that a
        numbered sibling never exists in a partial state.
        """
        try:
            os.makedirs(self.directory, exist_ok=True)
            handle, path = tempfile.mkstemp(prefix=".fetchpdf-si-", dir=self.directory)
            os.close(handle)
            return path
        except OSError as e:
            self.ctx.log(f"    supplements: cannot stage in {self.directory}: {e}")
            return None

    # -- output -----------------------------------------------------------

    def finish(self, manifest_path, ids, reports, max_file_bytes) -> SupplementarySummary:
        # The completeness gate first: it outranks every other status. The
        # question that matters is not "did any request fail" -- providers list
        # speculatively and endpoints say "nothing here" in strange ways -- but
        # "did we get what the paper itself declares it has".
        declared = _declared_check(self.ctx, self.kept)

        # "partial" means something the record has was not obtained. A duplicate
        # is not that: the same file reachable three ways and stored once is a
        # complete result, and the overwhelming majority of skips are duplicates.
        lost = [s for s in self.skipped if s.get("reason") in _LOST_REASONS]
        if declared.get("missing"):
            # A file the article's own JATS names was not obtained by any
            # provider. Worse than "partial": this is known, named data loss.
            status = "incomplete"
        elif lost:
            # "partial" whether or not anything was kept. A record whose one
            # supplement was refused for size is not an error -- the refusal was
            # deliberate and is recorded with its byte count -- but it is not a
            # clean result either.
            status = "partial"
        elif self.kept:
            status = "ok"
        elif any(r.get("status") == "error" for r in reports):
            status = "error"
        else:
            status = "none_found"

        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "identifier": str(ids.doi or ids.best_id or ""),
            "identifiers_resolved": ids.to_dict() if hasattr(ids, "to_dict") else {},
            "stem": os.path.basename(self.stem),
            "completed_at": _iso(time.time()),
            "status": status,
            "limits": {
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
                "max_files": self.max_files,
            },
            "providers": reports,
            "declared": declared,
            "files": self.kept,
            "skipped": self.skipped,
            "counts": {
                "written": len(self.kept),
                "skipped": len(self.skipped),
                "bytes_written": self.bytes_written,
            },
        }
        written_path = write_manifest(manifest_path, manifest, self.ctx.verbose)
        return SupplementarySummary(
            status=status,
            written=len(self.kept),
            skipped=len(self.skipped),
            bytes_written=self.bytes_written,
            manifest_path=written_path,
            # filename is None for an archive removed after its members were
            # extracted (see _unpack_stored_zip): the record is kept as the only
            # account of where those members came from, but there is no file to
            # point at. Joining it raised TypeError and failed the whole
            # supplementary pass for the record.
            paths=[os.path.join(self.directory, f["filename"])
                   for f in self.kept if f.get("filename")],
            missing_declared=list(declared.get("missing") or []),
            blocked_urls=_blocked_urls(self.skipped),
        )


# -- the slow retry ----------------------------------------------------------


def _is_transient(reason: str, status) -> bool:
    """Whether a refusal deserves the record-level slow rounds.

    "unreachable" and "empty" are connection weather. A refusal earns a retry
    only for statuses that mean "later" -- 403/404/410 are answers, and
    retrying an answer is how a run spins forever against a correct "no".

    Both refusal reasons are read, not just one: 429 now records as `blocked`
    rather than `http-error`, and it is the status and not the label that says
    whether waiting can help.
    """
    if reason in ("unreachable", "empty"):
        return True
    return (reason in ("http-error", blocked.BLOCKED)
            and status in _TRANSIENT_STATUSES)


def _retry_transients(run: "_Run", stem: str, waits=None) -> None:
    """Re-take transiently failed entries, slowing down dramatically per round.

    The waits print unconditionally: a five-minute silence in a batch run reads
    as a hang, and the slowdown is deliberate behaviour worth narrating. Each
    round pops the stale skip record before re-taking, so a file that succeeds
    on round two leaves no trace of its round-one failure beyond the log.
    """
    if waits is None:
        waits = _RETRY_WAITS   # read at call time, so tests can shrink it
    for round_no, wait in enumerate(waits, start=1):
        pending, run.transient = run.transient, []
        if not pending or not run.has_budget():
            return
        names = ", ".join((e.basename or e.provider) for e, _ in pending[:3])
        print(f"    ⏳ SI retry round {round_no}/{len(waits)} for "
              f"{os.path.basename(stem)}: {len(pending)} file(s) ({names}), "
              f"waiting {wait}s")
        time.sleep(wait)
        for entry, record in pending:
            if not run.has_budget():
                run.transient.append((entry, record))
                continue
            try:
                run.skipped.remove(record)
            except ValueError:
                pass
            run.take(entry)


# -- the completeness gate ---------------------------------------------------


def _declared_block(declared: Dict[str, str], kept: List[dict]) -> dict:
    """The manifest's declared-vs-obtained accounting, computed purely.

    A declared basename counts as obtained when any kept file's original name
    matches it extension-insensitively (_matches_href): publishers list .jpg
    and serve .gif often enough that exact matching would cry wolf.
    """
    names = [f.get("original_name") or f.get("filename") or "" for f in kept]
    missing = sorted(base for base in declared
                     if not _matches_href(base, names))
    return {
        "source": "jats",
        "total": len(declared),
        "obtained": len(declared) - len(missing),
        "missing": missing,
    }


def _declared_check(ctx, kept: List[dict]) -> dict:
    """What the article declares vs what was obtained, or an honest shrug.

    "unavailable" when the JATS manifest never ran (no PMCID, no parseable XML,
    no <body>): a record that cannot be verified must not read as verified.
    """
    declared = ctx.scratch.get("jats_declared")
    if declared is None:
        supplements = ctx.scratch.get("jats_supplements")
        if supplements is None:
            return {"source": "unavailable"}
        declared = {base: "" for base in supplements}
    return _declared_block(declared, kept)


def _heal_manifest(manifest: dict, manifest_path: str, stem: str,
                   verbose: bool) -> Tuple[str, List[str]]:
    """Bring an old manifest's accounting up to current classification.

    Two upgrades, both idempotent and download-free:

    * EPMC bundle refusals recorded before "no-bundle" existed -- an http-error
      404/410, or a not-an-archive that was really the <errorBean> stub -- are
      reclassified, with the original reason kept under "was".
    * The declared-vs-obtained gate is recomputed from the record's own XML on
      disk, and the status re-derived under the same ladder finish() uses.

    Returns the status the repair-path summary should carry: the healed status
    when it demands attention ("incomplete"), else "skipped" -- the repair
    pass's contract that a no-op must never look like new work.
    """
    changed = False
    for skip in manifest.get("skipped") or []:
        reason = skip.get("reason")
        # Every provider, not just EPMC: the refusals worth reclassifying here
        # are Atypon's and PNAS's, and gating this on the provider below would
        # heal the one publisher that never sends a 403.
        if reason == "http-error" and skip.get("status") in blocked.BLOCKED_STATUSES:
            # Written before `blocked` existed. Healing it here is what puts an
            # old corpus's 403s into the missing-materials report, where the
            # answer is a person with a browser rather than another run.
            skip["reason"], skip["was"] = blocked.BLOCKED, reason
            changed = True
            continue
        if skip.get("provider") != "europepmc_supplements":
            continue
        if reason == "http-error" and skip.get("status") in (404, 410):
            skip["reason"], skip["was"] = "no-bundle", reason
            changed = True
        elif reason == "not-an-archive":
            # This endpoint's only known non-zip payload is the errorBean stub;
            # if it was something else, the gate below still catches any real
            # shortfall via the declared list.
            skip["reason"], skip["was"] = "no-bundle", reason
            changed = True

    declared = _declared_from_disk(stem)
    if declared is not None:
        block = _declared_block(declared, manifest.get("files") or [])
        if manifest.get("declared") != block:
            manifest["declared"] = block
            changed = True

    missing_declared = list((manifest.get("declared") or {}).get("missing") or [])
    lost = [s for s in (manifest.get("skipped") or [])
            if s.get("reason") in _LOST_REASONS]
    if missing_declared:
        status = "incomplete"
    elif lost:
        status = "partial"
    elif manifest.get("files"):
        status = "ok"
    elif any(r.get("status") == "error" for r in manifest.get("providers") or []):
        status = "error"
    else:
        status = "none_found"

    if manifest.get("status") != status:
        manifest["status"] = status
        changed = True
    if changed:
        write_manifest(manifest_path, manifest, verbose)
        if verbose:
            print(f"    📋 manifest healed: status {status}")
    return ("incomplete" if status == "incomplete" else "skipped",
            missing_declared)


def _blocked_urls(records) -> List[str]:
    """The URLs of everything a publisher or CDN refused this client.

    Deduplicated in order: one Atypon supplement page refusing four files is
    one thing for a person to open, not four.
    """
    urls = []
    for record in records or []:
        if (record or {}).get("reason") != blocked.BLOCKED:
            continue
        url = record.get("url")
        if url and "REDACTED" not in str(url) and url not in urls:
            urls.append(url)
    return urls


def _declared_from_disk(stem: str) -> Optional[Dict[str, str]]:
    """The declared-supplement map from {stem}.xml, or None if unverifiable."""
    from .supplement_pmc import declared_from_xml

    try:
        with open(stem + ".xml", "rb") as f:
            content = f.read()
    except OSError:
        return None
    return declared_from_xml(content)


# -- the repair pass --------------------------------------------------------


def _repair(manifest_path, stem, resolver, http, ladder, email, verbose,
            max_file_bytes, verify_hashes: bool = True) -> SupplementarySummary:
    """Honour an existing manifest, re-fetching what is missing OR corrupt.

    A file deleted from disk comes back at its recorded index, so removing _2
    never renumbers _3. When nothing is missing this costs N stat calls and zero
    HTTP, which is the whole point of the manifest being the skip signal.

    "Missing" means unusable, not absent. The manifest records a size and a
    sha256 for every file and this pass is the only place they are ever read
    back; without that, a truncated or git-lfs-stubbed supplement counts as
    present forever and the corpus reads as complete while holding 130-byte
    placeholders where its data should be.
    """
    manifest = read_manifest(manifest_path)
    if manifest is None:
        return SupplementarySummary(status="error", manifest_path=manifest_path,
                                    detail="manifest unreadable")

    # Heal before anything else: manifests written before the no-bundle
    # classification and the completeness gate carry phantom "partial"
    # statuses, and the repair pass runs on every re-run -- the natural place
    # to bring old accounting up to current truth without re-downloading.
    status, missing_declared = _heal_manifest(manifest, manifest_path, stem,
                                              verbose)
    # Read after healing, so an old manifest's 403s -- reclassified a moment
    # ago -- reach the report on the re-run that heals them rather than the one
    # after it.
    blocked_urls = _blocked_urls(manifest.get("skipped") or [])

    directory = os.path.dirname(os.path.abspath(stem)) or "."
    files = manifest.get("files") or []
    # A removed-after-extraction archive has no file BY DESIGN; refetching it
    # would restore the very zip whose members are already unpacked beside it.
    files = [f for f in files if not f.get("removed_after_extraction")]

    # Existence is not integrity -- see _integrity_of. A file that is present
    # but corrupt is re-fetched at its recorded index, exactly like an absent
    # one, so the no-renumber guarantee holds for both.
    verdicts = {}
    missing = []
    for f in files:
        verdict = _integrity_of(os.path.join(directory, f.get("filename") or ""),
                                f, verify_hashes)
        verdicts[id(f)] = verdict
        if verdict != INTACT and verdict != UNVERIFIABLE:
            missing.append(f)
    present = len(files) - len(missing)

    damaged = [(f, verdicts[id(f)]) for f in missing if verdicts[id(f)] != ABSENT]
    if damaged and verbose:
        for f, verdict in damaged:
            name = f.get("filename") or "?"
            if verdict == LFS_POINTER:
                # Naming the cause matters: the fix is a checkout/sync repair,
                # not a re-download, and the next sync will stub it again.
                print(f"    ⚠️  {name}: git-lfs pointer, content never smudged "
                      f"(re-fetching, but the storage needs fixing)")
            else:
                print(f"    ⚠️  {name}: {verdict.replace('_', ' ')} vs manifest "
                      f"(re-fetching)")

    if not missing:
        return SupplementarySummary(
            status=status, written=present, manifest_path=manifest_path,
            paths=[os.path.join(directory, f["filename"]) for f in files
                   if f.get("filename")],
            detail="manifest present; nothing missing",
            missing_declared=missing_declared,
            blocked_urls=blocked_urls,
        )

    if http is None:
        return SupplementarySummary(
            status=status, written=present, manifest_path=manifest_path,
            detail=f"{len(missing)} file(s) missing, no client to refetch with",
            missing_declared=missing_declared,
            blocked_urls=blocked_urls,
        )

    repaired = 0
    archives: Dict[str, List[dict]] = {}
    for record in missing:
        url = record.get("url") or ""
        filename = record.get("filename") or ""
        # A redacted URL cannot be re-fetched, and guessing at the secret would
        # be worse than saying so.
        if not url or not filename or "REDACTED" in url:
            continue
        # A file that arrived by archive expansion has the *archive's* URL, so
        # fetching it straight into the member's name would write the whole zip
        # where a single member belongs -- the right bytes for nothing.
        if record.get("container"):
            archives.setdefault(url, []).append(record)
            continue
        dest = os.path.join(directory, filename)
        if http.download(url, dest, max_file_bytes, polite=False).ok:
            repaired += 1
            if verbose:
                print(f"    📎 restored {filename}")

    for url, records in archives.items():
        repaired += _repair_from_archive(http, url, records, directory,
                                         max_file_bytes, verbose)

    # Say which were corrupt rather than absent: "restored 30 of 30" hides a
    # storage fault that will recur on the next sync.
    detail = f"restored {repaired} of {len(missing)} missing file(s)"
    if damaged:
        kinds = sorted({verdict for _, verdict in damaged})
        detail += f" ({len(damaged)} corrupt on disk: {', '.join(kinds)})"

    return SupplementarySummary(
        status=status, written=present + repaired, manifest_path=manifest_path,
        detail=detail,
        missing_declared=missing_declared,
        blocked_urls=blocked_urls,
    )


#: A git-lfs pointer stands where the content should be. This is NOT a download
#: failure -- the bytes were fetched and hashed correctly, then a checkout or a
#: file-sync replaced them with this 130-byte stub. Measured on a real corpus:
#: 30 of 38 supplementary payloads, every pointer oid matching the sha256 the
#: manifest recorded, which is the proof the real bytes were once on disk.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"

#: What _integrity_of found. "absent" and "lfs_pointer" are different facts and
#: call for different operator actions -- re-fetch versus fix your checkout --
#: so they never collapse into one "missing".
INTACT = "intact"
ABSENT = "absent"
LFS_POINTER = "lfs_pointer"
SIZE_MISMATCH = "size_mismatch"
HASH_MISMATCH = "hash_mismatch"
UNVERIFIABLE = "unverifiable"


def _integrity_of(path: str, record: dict, verify_hashes: bool = True) -> str:
    """Classify one manifest-listed file on disk.

    Existence is not integrity. A truncated, zeroed, half-synced or LFS-stubbed
    file passes os.path.isfile and fails every use it was downloaded for -- and
    a hollow supplement reads downstream as "this paper published no data",
    which is worse than a loud absence.

    Ordered by cost: stat before read, read before hash, so an intact corpus
    stays at N stats plus a 4 KB head read and the manifest keeps its
    zero-HTTP skip-signal property.

    A record with no recorded size or digest is UNVERIFIABLE, never corrupt:
    manifests written before this check exist, and re-downloading a healthy
    file on absent metadata would be a regression, not a repair.
    """
    if not os.path.isfile(path):
        return ABSENT

    declared_bytes = record.get("bytes")
    try:
        actual_bytes = os.path.getsize(path)
    except OSError:
        return ABSENT

    # Cheapest decisive check first, and on its own enough to catch every
    # stub: a 130-byte pointer where 9362 bytes were recorded.
    if _head_of(path, len(_LFS_POINTER_MAGIC)).startswith(_LFS_POINTER_MAGIC):
        return LFS_POINTER

    if isinstance(declared_bytes, int) and declared_bytes >= 0:
        if actual_bytes != declared_bytes:
            return SIZE_MISMATCH
    elif not record.get("sha256"):
        return UNVERIFIABLE

    declared_sha = str(record.get("sha256") or "").strip().lower()
    if verify_hashes and declared_sha:
        actual_sha = _hash_file(path)
        if actual_sha is None:
            return ABSENT
        if actual_sha.lower() != declared_sha:
            return HASH_MISMATCH

    if not isinstance(declared_bytes, int) and not declared_sha:
        return UNVERIFIABLE
    return INTACT


def _repair_from_archive(http, url, records, directory, max_file_bytes, verbose) -> int:
    """Re-extract missing members from their archive, at their recorded names."""
    staged = None
    restored = 0
    try:
        handle, staged = tempfile.mkstemp(prefix=".fetchpdf-si-", dir=directory)
        os.close(handle)
        bundle_cap = max_file_bytes * BUNDLE_CAP_MULTIPLE
        if not http.download(url, staged, bundle_cap, polite=False).ok:
            return 0
        with zipfile.ZipFile(staged) as archive:
            by_name = {
                os.path.basename(m.filename.replace("\\", "/")): m
                for m in archive.infolist()
                if not m.is_dir()
            }
            for record in records:
                member = by_name.get(record.get("original_name") or "")
                if member is None or member.file_size > max_file_bytes:
                    continue
                dest = os.path.join(directory, record["filename"])
                payload = archive.read(member)
                _write_bytes_atomic(dest, payload)
                restored += 1
                if verbose:
                    print(f"    📎 restored {record['filename']} from {url.rsplit('/', 1)[-1]}")
    except (zipfile.BadZipFile, OSError, KeyError):
        return restored
    finally:
        _unlink(staged)
    return restored


def si_filename(stem_basename: str, index: int, original_name: str, extension: str) -> str:
    """The one supplementary filename rule. See DESCRIPTOR_MAX_CHARS above."""
    descriptor = _descriptor(original_name, extension)
    return f"{stem_basename}{INFIX}_{index}{descriptor}{extension}"


def _safe_member_name(original: str, extension: str, index: int) -> str:
    """A deposit file's own name, made safe for every filesystem.

    Unlike the flat scheme this keeps the author's filename -- inside a
    per-deposit directory it is unambiguous, and "MainStudy_UK_raw.csv" is far
    more use to a reader than "_supplementary_info_7". Same whitelist as
    _descriptor, so Windows-forbidden characters and path traversal are both
    handled; the index only breaks ties.
    """
    base = os.path.basename(str(original or "").replace("\\", "/"))
    stem, dot_ext = os.path.splitext(base)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:DESCRIPTOR_MAX_CHARS]
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("._-")
    suffix = dot_ext if dot_ext and len(dot_ext) <= 8 else extension
    return f"{cleaned or f'file_{index}'}{suffix}"


def _descriptor(original_name: str, extension: str) -> str:
    """`_TablesS1-S5` from `TablesS1-S5.docx`, or "" when it would add nothing."""
    if not original_name:
        return ""
    base = os.path.basename(str(original_name).replace("\\", "/"))
    if extension and base.lower().endswith(extension.lower()):
        base = base[: -len(extension)]
    else:
        base = os.path.splitext(base)[0]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        return ""
    # Long enough to stay recognisable, short enough that stem + infix + index +
    # descriptor + extension cannot approach the 255-byte filename limit.
    return "_" + cleaned[:DESCRIPTOR_MAX_CHARS].strip("._-")


def _error_bean(path: str) -> Optional[str]:
    """The errMsg of an EPMC <errorBean> stub, or None for anything else.

    Size-gated hard: a real bundle is never this small, and nothing legitimate
    starts with an XML prolog and contains <errorBean>.
    """
    try:
        if os.path.getsize(path) > 1024:
            return None
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return None
    if not head.lstrip().startswith(b"<?xml") or b"<errorBean>" not in head:
        return None
    match = re.search(rb"<errMsg>(.*?)</errMsg>", head, re.DOTALL)
    if match:
        return match.group(1).decode("utf-8", "replace")[:160]
    return "errorBean with no errMsg"


def _provenance_url(entry, transferred_url=None) -> Optional[str]:
    """Where this file actually came from, for the manifest.

    Normally that is the URL we transferred. The exception is a file some
    provider had already fetched to a scratch directory and handed over as
    `file://...`: HttpClient.download adopts those under the same rules as a
    network transfer, which is the right thing for the BYTES and the wrong
    thing for the RECORD -- a temp path stops meaning anything the moment the
    directory is removed, and the manifest is what a reader consults to ask
    where a file came from.

    So a provider that knows the real origin puts it in `extra["source_url"]`
    and it wins. Losing that would be this toolkit's own signature defect: a
    local path presented as a provenance.
    """
    extra = getattr(entry, "extra", None) or {}
    source = extra.get("source_url")
    if source:
        return redact(str(source))
    candidate = transferred_url or entry.url
    if str(candidate or "").startswith("file://"):
        return None          # honest: we do not know, rather than a temp path
    return redact(candidate)


def _is_zip(path: str) -> bool:
    """By magic bytes, not extension: a .zip served as .dat is still a zip, and a
    .docx is a zip we must NOT unpack -- hence the extension check as well."""
    if not path.lower().endswith(".zip"):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _write_bytes_atomic(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-si-", dir=directory)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(payload)
        apply_default_mode(temp_path)
        os.replace(temp_path, path)
    except BaseException:
        _unlink(temp_path)
        raise


# -- naming -----------------------------------------------------------------


def stem_for(save_path: str) -> str:
    """The record's stem, sharing engine._stem_for's multi-part suffix handling."""
    from .engine import _stem_for

    return _stem_for(save_path)


def _extension_for(name: str, content_type: str, head: bytes) -> str:
    """The output extension, derived in a fixed order so it is reproducible.

    Falls through to no extension at all rather than inventing .bin: the manifest
    carries the original name and the content type, and a fabricated extension is
    a lie that a downstream tool will act on.

    The name is only believed when what follows its last dot is *recognisably* an
    extension. Plenty of supplementary identifiers end in a dotted segment that is
    not one -- a Crossref component DOI is named "pone.0000308.s001", and taking
    "s001" would produce a file called _1.s001 that nothing can open, while the
    served Content-Type said application/msword all along.
    """
    candidate = ""
    if name and "." in name:
        candidate = name.rsplit(".", 1)[-1].lower()
        if not (1 <= len(candidate) <= 8 and candidate.isalnum()):
            candidate = ""
    if candidate in _KNOWN_EXTENSIONS:
        return "." + candidate

    mapped = _CONTENT_TYPE_EXTENSIONS.get((content_type or "").strip().lower())
    if mapped:
        return mapped
    for magic, extension in _MAGIC:
        if head.startswith(magic):
            return extension
    # An unrecognised suffix is still better than nothing when the bytes and the
    # header offered no opinion either -- it is at least what the source called it.
    return "." + candidate if candidate else ""


def _looks_like_a_document(name, head: bytes, content_type: str, size: int) -> Tuple[bool, str]:
    """Reject the bodies that are a 200 without being the file that was asked for.

    Every marker here is served with a success status. This is the single guard
    that stops a Cloudflare interstitial being written as Supplementary Table 1.
    """
    if size < 64:
        return False, f"implausibly small ({size} bytes)"
    window = head[:2048]
    for marker in _CHALLENGE_MARKERS:
        if marker in window:
            return False, "bot challenge page"
    lowered = (name or "").lower()
    if not lowered.endswith(_TEXTUAL_EXTENSIONS):
        stripped = window.lstrip()[:9].lower()
        if (content_type or "").startswith("text/html") or stripped in (b"<!doctype", b"<html"):
            return False, f"served HTML for {name or 'an unnamed file'}"

    # A binary document that begins with markup is not that document, whatever
    # the extension claims. The HTML check above is too narrow to catch this:
    # Elsevier's attachment endpoint answers 200 with an
    # `<attachment-metadata-response>` wrapper -- ~726 bytes of XML whose only
    # payload is the URL of the file you actually asked for -- and it was
    # written straight to disk as `mmc1.pdf`. Measured on one corpus: 51 such
    # stubs across 33 papers, and 8 of them sat under a manifest reporting
    # `status: ok`.
    #
    # That is the worst shape a supplement failure can take. `fetchpdf-verify`
    # still passes them, because the sha256 recorded at download time matches
    # the sha256 on disk -- it answers "is what I have what I fetched", not "is
    # what I fetched a document". Downstream, a 726-byte XML stub named .pdf
    # reads as "this paper published this table", which is a claim about the
    # authors manufactured out of an API quirk.
    if lowered.endswith(_BINARY_DOC_EXTENSIONS) and window.lstrip()[:1] == b"<":
        root = window.lstrip()[:60].decode("ascii", "replace").split(">")[0]
        return False, f"served markup ({root}>) for {name or 'an unnamed file'}"
    return True, ""


# -- manifest io ------------------------------------------------------------


def write_manifest(path: str, manifest: dict, verbose: bool = False) -> Optional[str]:
    """Atomically, and never raising into the caller's record."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-sim-", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
        except BaseException:
            _unlink(temp_path)
            raise
        return path
    except Exception as e:
        if verbose:
            print(f"    ⚠️  could not write {os.path.basename(path)}: {e}")
        return None


def read_manifest(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        return manifest if isinstance(manifest, dict) else None
    except (OSError, ValueError):
        return None


# -- wiring -----------------------------------------------------------------


def _wire(resolver, http, ladder, save_path, email, verbose):
    """Borrow the batch's resolver and client, or build them.

    Borrowing matters more than it looks: HostRateLimiter is lock-guarded but not
    a singleton, so a client built per record gives every worker its own token
    buckets and multiplies the configured per-host rate by the worker count -- on
    the most request-heavy path in the tool.

    Building a resolver when there is none is not optional either. Half the
    providers here are keyed on a PMCID or an Elsevier PII rather than on the DOI,
    and without a resolver those identifiers are simply absent -- so the PMC
    routes, which are the highest-yield ones for biomedical records, would
    silently never run in single-record mode.
    """
    from .tiers import load_ladder

    ladder = ladder or (getattr(resolver, "ladder", None)) or load_ladder()
    if http is None and resolver is not None:
        http = getattr(resolver, "http", None)
    if http is None:
        from .http import HttpClient
        from .ratelimit import HostRateLimiter, shared_host_limiter

        http = HttpClient(shared_host_limiter(ladder.rate_limits), email=email, verbose=verbose)

    owns_resolver = resolver is None
    if owns_resolver:
        from .cache import ResolutionCache
        from .resolve import BatchResolver

        output_dir = os.path.dirname(os.path.abspath(save_path)) or "."
        resolver = BatchResolver(
            http, ResolutionCache.for_output_dir(output_dir, verbose), ladder, verbose
        )
    return ladder, http, resolver, owns_resolver


def _context(http, resolver, ladder, save_path, verbose, email, delay,
             use_playwright, download_data_artifacts=False,
             max_data_artifact_bytes=None, llm_adjudicate_artifacts=False,
             llm_agent_retrieval=False, llm_backend=None,
             max_llm_records=0,
             llm_model=None, download_related_unverified=False):
    from .context import RetrievalContext

    ctx = RetrievalContext(
        http=http, resolver=resolver, ladder=ladder, save_path=save_path,
        verbose=verbose, email=email, delay=delay,
        use_playwright=use_playwright,
        download_data_artifacts=download_data_artifacts,
        max_data_artifact_bytes=max_data_artifact_bytes,
        download_related_unverified=download_related_unverified,
    )
    # Adjudication settings ride on scratch rather than becoming typed fields:
    # they configure one optional provider, not the retrieval itself, and
    # RetrievalContext is shared with every source in the ladder.
    ctx.llm_adjudicate_artifacts = llm_adjudicate_artifacts
    ctx.llm_agent_retrieval = llm_agent_retrieval
    ctx.llm_backend = llm_backend
    ctx.max_llm_records = max_llm_records
    ctx.llm_model = llm_model
    return ctx


def _resolve(resolver, raw_identifier, doi, pmid):
    from .identifiers import IdentifierSet

    if resolver is None:
        return IdentifierSet(doi=doi, pmid=pmid)
    try:
        return resolver.resolve(raw_identifier, doi=doi, pmid=pmid)
    except Exception:
        return IdentifierSet(doi=doi, pmid=pmid)


def _stem_lock(stem: str) -> threading.Lock:
    key = os.path.abspath(stem)
    with _STEM_LOCKS_GUARD:
        lock = _STEM_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STEM_LOCKS[key] = lock
    return lock


# -- small helpers ----------------------------------------------------------


def _head_of(path: str, size: int = 4096) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def _hash_file(path: str) -> Optional[str]:
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


def _digest_of(checksum: str) -> str:
    """"md5:abc" -> "abc". Declared digests are compared to each other only."""
    return str(checksum).split(":", 1)[-1].strip().lower()


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
