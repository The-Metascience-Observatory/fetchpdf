import functools
from typing import Optional
import json
import os
import re
import sys
import time
import difflib
import html
import shutil
import tempfile
import subprocess
import collections
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
import urllib3
from requests.exceptions import SSLError

# .env.local is loaded here, at import time -- see _env.py.
from ._env import (
    EMAIL as _DEFAULT_EMAIL,
    ELSEVIER_TDM_API_KEY as _ELSEVIER_TDM_API_KEY,
    ENTREZ_API_KEY as _ENTREZ_API_KEY,
    S2_API_KEY as _S2_API_KEY,
)
from ._http import (USER_AGENT, POLITE_USER_AGENT, headers_for,
                    elsevier_first_page_only)

# The ID Converter moved: www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/ now 301s
# here. requests follows the redirect, so the old URL still worked -- it just
# paid an extra round-trip on every call, on every record, forever.
IDCONV_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"

# Session-level state for providers that should be disabled after a fatal auth error.
# These are shared across all ThreadPoolExecutor workers in a single batch run, so
# the check-then-act that sets the flag is guarded by a lock (mirrors _PUBMED_WEB_LOCK).
_CORE_SESSION_DISABLED = False
_CORE_SESSION_LOCK = threading.Lock()
_SERPAPI_SESSION_DISABLED = False
_SERPAPI_SESSION_LOCK = threading.Lock()

# Consecutive CORE *transport* failures (timeouts, resets) before we stop asking
# for the rest of the run. Distinct from the 401/403 kill switch: a slow API is
# not a bad key, but paying 15-30s per remaining record of a 6361-paper batch
# is how CORE becomes the most expensive miss in the chain.
_CORE_TIMEOUT_COUNT = 0
_CORE_TIMEOUT_LIMIT = 3
_CORE_TIMEOUT_DISABLED = False

# CORE's budget is a DAILY quota (x-ratelimit-limit: 500), not a per-second
# rate, so throttling does not help and retrying is actively harmful: every 429
# still costs a request. Once the quota is out, the only correct move is to stop
# asking until x-ratelimit-retry-after. These track that across all workers.
_CORE_QUOTA_LOCK = threading.Lock()
_CORE_QUOTA_REMAINING = None      # int, from x-ratelimit-remaining
_CORE_QUOTA_RESET_AT = None       # epoch seconds, from x-ratelimit-retry-after
_CORE_QUOTA_NOTIFIED = False


def _core_quota_note_headers(headers) -> None:
    """Remember what CORE just told us about the remaining daily budget."""
    global _CORE_QUOTA_REMAINING, _CORE_QUOTA_RESET_AT
    remaining = headers.get("x-ratelimit-remaining")
    reset_at = headers.get("x-ratelimit-retry-after")
    with _CORE_QUOTA_LOCK:
        if remaining is not None:
            try:
                _CORE_QUOTA_REMAINING = int(remaining)
            except (TypeError, ValueError):
                pass
        if reset_at:
            try:
                from datetime import datetime
                parsed = datetime.strptime(reset_at, "%Y-%m-%dT%H:%M:%S%z")
                _CORE_QUOTA_RESET_AT = parsed.timestamp()
            except (TypeError, ValueError):
                pass


def _core_quota_exhausted(verbose=False) -> bool:
    """True when the daily quota is spent and the reset has not yet passed."""
    global _CORE_QUOTA_REMAINING, _CORE_QUOTA_NOTIFIED
    with _CORE_QUOTA_LOCK:
        if _CORE_QUOTA_REMAINING is None or _CORE_QUOTA_REMAINING > 0:
            return False
        if _CORE_QUOTA_RESET_AT and time.time() >= _CORE_QUOTA_RESET_AT:
            # Window rolled over; let the next call re-learn the budget.
            _CORE_QUOTA_REMAINING = None
            _CORE_QUOTA_NOTIFIED = False
            return False
        should_notify = not _CORE_QUOTA_NOTIFIED
        _CORE_QUOTA_NOTIFIED = True
        reset_at = _CORE_QUOTA_RESET_AT
    if should_notify:
        when = ""
        if reset_at:
            mins = max(0, int((reset_at - time.time()) // 60))
            when = f"; resets in ~{mins} min"
        _print_yellow_warning(
            f"⚠️  CORE daily quota exhausted{when}. Skipping CORE for now "
            f"(this is a per-day budget, so retrying would only waste calls)."
        )
    return True


def _core_timeout_skip(verbose=False) -> bool:
    """True when CORE has timed out enough times this run to be skipped."""
    return _CORE_TIMEOUT_DISABLED


def _core_note_timeout(verbose=False) -> None:
    """Record a CORE transport failure; disable CORE after `_CORE_TIMEOUT_LIMIT`."""
    global _CORE_TIMEOUT_COUNT, _CORE_TIMEOUT_DISABLED
    with _CORE_SESSION_LOCK:
        _CORE_TIMEOUT_COUNT += 1
        if _CORE_TIMEOUT_COUNT < _CORE_TIMEOUT_LIMIT or _CORE_TIMEOUT_DISABLED:
            return
        _CORE_TIMEOUT_DISABLED = True
    _print_yellow_warning(
        "⚠️  CORE timed out repeatedly; skipping it for the rest of this run "
        "(the API is slow, not unauthorized -- unset nothing)."
    )


def _core_note_success() -> None:
    """A finished CORE search resets the consecutive-timeout counter."""
    global _CORE_TIMEOUT_COUNT
    with _CORE_SESSION_LOCK:
        _CORE_TIMEOUT_COUNT = 0


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """A ThreadPoolExecutor whose workers cannot keep the process alive.

    Needed because BOTH of the interpreter's exit-time joins have to be
    defeated, and each needs a different remedy:

      * threading._shutdown() joins every non-daemon thread. Only daemon
        threads escape it, and .daemon cannot be assigned once a thread is
        running ("cannot set daemon status of active thread"), so the flag has
        to be set at construction -- hence this subclass rather than a fix-up
        applied afterwards.
      * concurrent.futures._python_exit() joins every thread in its module
        registry regardless of daemon status; _unregister_pool_from_atexit()
        below removes them from it.

    Verified: with only one of the two, a wedged worker still hangs the process
    after the summary prints. With both, the interpreter exits cleanly.
    """

    def _adjust_thread_count(self):
        _real_thread = threading.Thread

        class _DaemonThread(_real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.daemon = True

        threading.Thread = _DaemonThread
        try:
            super()._adjust_thread_count()
        finally:
            threading.Thread = _real_thread


#: Pretty names for the per-record success line. Keys are the labels passed to
#: _record_source; anything missing falls back to the raw label, so a new source
#: shows up as itself rather than disappearing.
SOURCE_DISPLAY_NAMES = {
    "apa_supplemental": "APA",
    "core": "CORE",
    "crossref": "Crossref",
    "crossref_preprint": "Crossref-preprint",
    "datacite": "DataCite",
    "datacite_related": "DataCite-related",
    "direct_doi": "direct-DOI",
    "doaj": "DOAJ",
    "elsevier": "Elsevier",
    "elife": "eLife",
    "escholarship": "eScholarship",
    "europepmc": "EuropePMC",
    "existing": "on-disk",
    "figshare": "figshare",
    "openalex": "OpenAlex",
    "osf": "OSF",
    "pmc": "PMC",
    "pmid_direct": "PubMed",
    "psycharchives": "PsychArchives",
    "semantic_scholar": "SemanticScholar",
    "serpapi_scholar": "GoogleScholar (SerpApi)",
    "ssrn": "SSRN",
    "unpaywall": "Unpaywall",
    "wiley": "Wiley",
    "zenodo": "Zenodo",
}


#: schema.org types that mean "this DOI is not a research article with a PDF".
#: Nature's d41586-* magazine DOIs are the common case in a metascience corpus.
_NO_FULLTEXT_SCHEMA_TYPES = (
    '"@type":"NewsArticle"',
    '"@type": "NewsArticle"',
    '"@type":"Report"',
    '"@type": "Report"',
)


#: DOIs already reported as news items. try_landing_page_pdf_fallback runs from
#: several places per record, so without this the warning prints two or three
#: times for one DOI.
_NEWS_ITEM_SEEN = set()
_NEWS_ITEM_LOCK = threading.Lock()


def _note_news_item(doi: str) -> bool:
    """True the first time this DOI is identified as a news item."""
    with _NEWS_ITEM_LOCK:
        if doi in _NEWS_ITEM_SEEN:
            return False
        _NEWS_ITEM_SEEN.add(doi)
        return True


#: The requested article's title and page range, per DOI, for the identity check
#: that decides whether a downloaded PDF is actually this paper. Filled in bulk
#: at the start of a batch, then from the payloads the chain already parses, and
#: only as a last resort by asking for one DOI on its own.
_TITLE_MEMO = {}
_PAGES_MEMO = {}
#: Crossref files a subtitle in its own field, so `title` for many SAGE and APA
#: records is only the first half of what the paper is called. Kept apart from
#: the title because the identity check must not start demanding it. "" means
#: Crossref answered and there is none; absent means nobody asked.
_SUBTITLE_MEMO = {}
#: The year a record was issued, per DOI, from the same Crossref answers. Read
#: by the grey last resorts to skip Sci-Hub for papers it cannot have: measured
#: 2026-09-02 over ~550 attempts, papers issued 2023 or later hit 2 times in 93.
_YEAR_MEMO = {}
_TITLE_LOCK = threading.Lock()

#: DOIs whose metadata we could not ASK for -- a 429, a timeout, a DNS blip --
#: as opposed to DOIs Crossref answered for and had no title for. The two must
#: never be confused: "this record has no title" is a fact about the record,
#: while "we could not reach Crossref" is a fact about the afternoon, and under
#: reject-always the second one was deleting correct files. Verified: with the
#: single-DOI lookup 429ing, an accepted manuscript that prints its title and
#: not its DOI was refused and unlinked, and the empty title was then cached for
#: the rest of the run so every retry refused it again.
_METADATA_UNAVAILABLE = set()

#: Crossref publishes its own allowance in every response: `x-rate-limit-limit:
#: 10`, `x-rate-limit-interval: 1s`, measured 2026-08-23. The tiered engine has
#: always respected it; the legacy chain called requests.get directly from five
#: places, so with ten workers there was no ceiling at all.
#:
#: The bucket is fetched from `shared_host_limiter`, NOT built here. A private
#: one was the whole bug in the first version of this: two buckets, each
#: politely allowing 10/s, is 20/s.
_CROSSREF_HOST = "api.crossref.org"

#: How many DOIs to ask about in one request. Crossref takes a comma-joined
#: `filter=doi:...` list; measured 2026-08-23, batches of 20/50/100/150 all
#: return every item, at URL lengths of 1.7-5.3 KB. Fifty is the conservative
#: pick and was also the fastest. A 10,000-DOI run needs 200 requests for every
#: title and page range instead of 10,000.
_CROSSREF_BATCH = 50


def _crossref_bucket():
    """The one process-wide token bucket for Crossref."""
    from .retrieval.ratelimit import shared_host_limiter
    from .retrieval.tiers import load_ladder

    try:
        limits = load_ladder().rate_limits
    except Exception:
        limits = None
    return shared_host_limiter(limits).bucket(_CROSSREF_HOST)


def _crossref_get(url: str, params=None, timeout=20, retries=3, verbose=False):
    """A rate-limited, 429-aware Crossref GET. Returns the parsed message or None.

    Returns None for "could not ask" and {} for "asked, nothing there" -- the
    caller has to tell those apart, because one of them is allowed to become a
    permanent answer and the other is not.
    """
    params = dict(params or {})
    if _DEFAULT_EMAIL:
        params.setdefault("mailto", _DEFAULT_EMAIL)
    for attempt in range(retries):
        _crossref_bucket().acquire()
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            if verbose:
                print(f"  Crossref request failed: {str(e)[:80]}")
            time.sleep(min(2 ** attempt, 8))
            continue
        if r.status_code == 200:
            try:
                return r.json().get("message") or {}
            except ValueError:
                return None
        if r.status_code == 404:
            return {}
        if r.status_code in (429, 500, 502, 503, 504):
            wait = _retry_after_seconds(r) or min(2 ** attempt, 8)
            if verbose:
                print(f"  Crossref HTTP {r.status_code}; waiting {wait:.1f}s")
            # Drain the SHARED bucket rather than sleeping this thread. Sleeping
            # here is what makes a rate-limited run keep hammering: one worker
            # backs off while the other nine carry on spending the very budget
            # that just ran out. Penalising instead makes the NEXT acquire --
            # this thread's and everyone else's -- do the waiting, once.
            _crossref_bucket().penalize(wait)
            continue
        return None
    return None


def _crossref_raw_get(url: str, **kwargs):
    """requests.get against Crossref, throttled by the shared bucket.

    A one-line seam for the chain's own Crossref steps, which need the raw
    response (status codes, `link[]`, the /agency endpoint) rather than the
    parsed message `_crossref_get` returns. What matters is that they take a
    token from the SAME bucket as everything else: ten workers each politely
    keeping to 10/s is 100/s, which is how a --workers 10 run got 429ed.
    """
    _crossref_bucket().acquire()
    return requests.get(url, **kwargs)


def _retry_after_seconds(response):
    raw = (response.headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), 60.0))
    except (TypeError, ValueError):
        return None


def _year_for(doi: str, verbose=False) -> Optional[int]:
    """The year this DOI was issued: from the memo, else one Crossref call, else None.

    Same discipline as `_title_for`: a lookup that fails is not cached, so a
    429 cannot become a permanent "year unknown". Only Crossref is asked --
    DataCite records (OSF, Zenodo) are refused by the grey sources on their
    prefix before the year is ever a question.
    """
    if not doi:
        return None
    key = doi.strip().lower()
    with _TITLE_LOCK:
        if key in _YEAR_MEMO:
            return _YEAR_MEMO[key]
    message = _crossref_get(f"https://api.crossref.org/works/{key}", verbose=verbose)
    if not message:
        return None
    titles = message.get("title") or []
    year = _issued_year(message)
    _remember_title(key, titles[0] if titles else "", message.get("page"), year)
    return year


def _datacite_title(doi: str, verbose=False):
    """(title, page_range) from DataCite, or None when it could not be asked.

    Crossref does not register Zenodo, OSF, figshare, Dryad or most
    institutional repositories, and "Crossref has no record" was being read as
    "this record has no title" -- which, under a verifier that deletes what it
    cannot check, destroyed the correct PDF for every one of those DOIs on
    sight. Replication corpora are full of them: 8 of the 9 unverifiable records
    in the inbox have a DataCite title.

    Returns `("", "")` for a DOI DataCite genuinely does not have, which IS an
    answer, versus None for a request that failed, which is not.
    """
    from .retrieval.ratelimit import shared_host_limiter

    url = "https://api.datacite.org/dois/" + quote_plus((doi or "").strip().lower())
    try:
        shared_host_limiter().bucket("api.datacite.org").acquire()
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=20)
    except Exception as e:
        if verbose:
            print(f"  DataCite request failed for {doi}: {str(e)[:80]}")
        return None
    if r.status_code == 404:
        return "", ""
    if r.status_code != 200:
        return None
    try:
        attributes = ((r.json() or {}).get("data") or {}).get("attributes") or {}
    except ValueError:
        return None
    titles = attributes.get("titles") or []
    title = str((titles[0] or {}).get("title") or "") if titles else ""
    container = attributes.get("container") or {}
    first, last = container.get("firstPage") or "", container.get("lastPage") or ""
    page_range = f"{first}-{last}" if first and last else str(first or "")
    return title, page_range


def _issued_year(message) -> Optional[int]:
    """The year from a Crossref work's `issued` (else `published`) date-parts."""
    for field in ("issued", "published", "published-print", "published-online"):
        parts = ((message or {}).get(field) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _remember_title(doi: str, title, page_range=None, year=None, subtitle=None) -> None:
    """Memoise a record's title (and page range, year, subtitle when given).

    `subtitle=None` means the caller did not ask Crossref for it; a list, even
    an empty one, is Crossref's answer and is remembered as such.
    """
    if not doi:
        return
    key = doi.strip().lower()
    if isinstance(title, (list, tuple)):
        title = title[0] if title else ""
    title = str(title or "").strip()
    with _TITLE_LOCK:
        if subtitle is not None:
            if isinstance(subtitle, (list, tuple)):
                subtitle = subtitle[0] if subtitle else ""
            _SUBTITLE_MEMO.setdefault(key, str(subtitle or "").strip())
        if title:
            _TITLE_MEMO.setdefault(key, title)
        if page_range:
            _PAGES_MEMO.setdefault(key, str(page_range).strip())
        if year:
            _YEAR_MEMO.setdefault(key, int(year))
    if title:
        _METADATA_UNAVAILABLE.discard(key)


def prime_record_metadata(dois, verbose=False) -> int:
    """Fetch titles and page ranges for a whole batch, ~50 DOIs per request.

    Called once before a run so that the per-record path almost never has to
    ask. This is what keeps --workers 10 under Crossref's 10/s: the identity
    check needs a title for every record, and asking per record made the
    verification the most rate-limit-hungry step in the tool.
    """
    wanted = []
    seen = set()
    for raw in dois:
        doi = str(raw or "").strip().lower()
        if not doi.startswith("10.") or doi in seen:
            continue
        seen.add(doi)
        with _TITLE_LOCK:
            if doi in _TITLE_MEMO:
                continue
        wanted.append(doi)
    if not wanted:
        return 0

    filled = 0
    for start in range(0, len(wanted), _CROSSREF_BATCH):
        chunk = wanted[start:start + _CROSSREF_BATCH]
        message = _crossref_get(
            "https://api.crossref.org/works",
            params={"filter": ",".join("doi:" + d for d in chunk),
                    "select": "DOI,title,subtitle,page,issued", "rows": len(chunk)},
            timeout=45, verbose=verbose,
        )
        if message is None:
            _METADATA_UNAVAILABLE.update(chunk)
            continue
        answered = set()
        for item in message.get("items") or []:
            doi = str(item.get("DOI") or "").strip().lower()
            if not doi:
                continue
            answered.add(doi)
            titles = item.get("title") or []
            if titles:
                _remember_title(doi, titles[0], item.get("page"), _issued_year(item),
                                subtitle=item.get("subtitle") or [])
                filled += 1
        # Crossref answered, but "not in Crossref" is not "has no title" -- it
        # is most often a DataCite DOI. Left unmemoised so `_title_for` asks
        # DataCite when the record is actually reached; memoising "" here is
        # what used to delete every Zenodo and OSF PDF.
        for doi in chunk:
            if doi not in answered and verbose:
                print(f"    not in Crossref, will try DataCite: {doi}")
    if verbose:
        print(f"  Crossref metadata primed for {filled}/{len(wanted)} record(s)"
              f" in {(len(wanted) + _CROSSREF_BATCH - 1) // _CROSSREF_BATCH} request(s)")
    return filled


def _title_for(doi: str, verbose=False) -> str:
    """This DOI's title: from the memo, else one Crossref call, else "".

    A failed lookup is NOT cached. Caching it would turn one 429 into a
    permanent "this record has no title", and a record with no title cannot be
    verified, and an unverifiable PDF is deleted.
    """
    if not doi:
        return ""
    key = doi.strip().lower()
    with _TITLE_LOCK:
        if key in _TITLE_MEMO:
            return _TITLE_MEMO[key]
    message = _crossref_get(f"https://api.crossref.org/works/{key}", verbose=verbose)
    if message is None:
        _METADATA_UNAVAILABLE.add(key)
        if verbose:
            print(f"  Could not reach Crossref for {doi}; identity check will be weaker")
        return ""
    titles = message.get("title") or []
    if titles:
        _remember_title(key, titles[0], message.get("page"), _issued_year(message),
                        subtitle=message.get("subtitle") or [])
        return str(titles[0])

    # Crossref answered and has no such record. That is not the end of the
    # question -- it does not register Zenodo, OSF, figshare or Dryad DOIs.
    found = _datacite_title(key, verbose=verbose)
    if found is None:
        _METADATA_UNAVAILABLE.add(key)
        return ""
    title, page_range = found
    if title:
        _remember_title(key, title, page_range)
        return title
    with _TITLE_LOCK:
        _TITLE_MEMO.setdefault(key, "")
    return ""



def _subtitle_for(doi: str, verbose=False) -> str:
    """This DOI's Crossref subtitle: from the memo, else one Crossref call, else "".

    Usually free: the batch primer and `_title_for` both memoise it from the
    answer they already fetched. A failed lookup is not cached.
    """
    if not doi:
        return ""
    key = doi.strip().lower()
    with _TITLE_LOCK:
        if key in _SUBTITLE_MEMO:
            return _SUBTITLE_MEMO[key]
    message = _crossref_get(f"https://api.crossref.org/works/{key}", verbose=verbose)
    if message is None:
        return ""
    subtitles = message.get("subtitle") or []
    with _TITLE_LOCK:
        _SUBTITLE_MEMO.setdefault(key, str(subtitles[0]).strip() if subtitles else "")
        return _SUBTITLE_MEMO[key]

#: URLs already downloaded and refused for a given DOI, so the same file is not
#: fetched and judged again. try_landing_page_pdf_fallback runs from nine call
#: sites per record and several of them converge on the same publisher URL: one
#: Brill preview was downloaded, parsed and discarded four times in a single
#: run before this existed.
_REJECTED_URLS = collections.defaultdict(set)
_REJECTED_LOCK = threading.Lock()

#: Endpoints that exist to serve a preview rather than the article. Checked
#: before the download, because the cheapest rejection is the one that never
#: transfers a file. Both were observed serving two pages of a seventeen- and a
#: thirty-two-page article.
_PREVIEW_URL_MARKERS = ("/previewpdf/", "/preview-pdf/", "previewpdf?")


def _looks_like_preview_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _PREVIEW_URL_MARKERS)


def _already_rejected(doi: str, url: str) -> bool:
    if not doi or not url:
        return False
    with _REJECTED_LOCK:
        return url in _REJECTED_URLS[doi.strip().lower()]


def _note_rejected(doi: str, url: str) -> None:
    if not doi or not url:
        return
    with _REJECTED_LOCK:
        _REJECTED_URLS[doi.strip().lower()].add(url)


def _metadata_was_unavailable(doi: str) -> bool:
    return (doi or "").strip().lower() in _METADATA_UNAVAILABLE


#: Records whose PDF was kept without being verified, because the metadata to
#: verify it against could not be fetched. Reported at the end of a run: an
#: unverified artifact is a smaller problem than a deleted one, but it is not
#: nothing, and a run that produced a hundred of them was a run against a
#: rate-limited Crossref rather than a clean corpus.
_UNVERIFIED_KEPT = {}
_UNVERIFIED_LOCK = threading.Lock()


def _note_unverified(doi: str, url: str = "") -> None:
    with _UNVERIFIED_LOCK:
        _UNVERIFIED_KEPT.setdefault((doi or "").strip().lower(), url)


def unverified_records() -> dict:
    """DOI -> source URL for every PDF kept without verification this run."""
    with _UNVERIFIED_LOCK:
        return dict(_UNVERIFIED_KEPT)


def _report_unverified_keeps() -> None:
    """Name the records whose PDF nobody was able to check.

    Printed unconditionally. A run that produced a hundred of these was a run
    against a rate-limited Crossref, not a clean corpus, and the difference is
    invisible in the success count -- every one of them counted as a success.
    """
    kept = unverified_records()
    if not kept:
        return
    _print_yellow_warning(
        f"⚠️  {len(kept)} PDF(s) were KEPT WITHOUT VERIFICATION because Crossref "
        f"could not be reached for their metadata. Re-run these once it is "
        f"reachable; they are the only artifacts in this run nobody checked:"
    )
    for doi in sorted(kept)[:20]:
        print(f"     - {doi}")
    if len(kept) > 20:
        print(f"     ... and {len(kept) - 20} more")


def _accept_downloaded_pdf(save_path: str, doi: str, url: str = "", verbose=False) -> bool:
    """True if the bytes just written are this DOI's paper. Deletes them if not.

    ONLY this paper's own material. A PDF the paper CITES belongs to somebody
    else and must not be collected. The bibliography of a research article is a
    list of other people's files, and a `try_download` that accepts on
    Content-Type alone will take one of them the moment the paper's own PDF is
    unavailable -- which is exactly what happened to 10.1111/all.14949.

    Returning False rather than aborting is the point: the caller's loop moves
    on to the next candidate, so a wrong hit costs one request instead of
    ending the search on somebody else's document.
    """
    if not save_path or not os.path.exists(save_path):
        return False
    # Only PDFs are judged here. The chain's XML fallbacks write .xml through
    # the same paths and have already passed _looks_like_jats_fulltext.
    try:
        with open(save_path, "rb") as f:
            if f.read(4) != b"%PDF":
                return True
    except OSError:
        return False

    from .retrieval.pdf_identity import TRUNCATED, WRONG, verify_pdf_identity
    from .retrieval.resolve import arxiv_id_from_doi

    title = _title_for(doi, verbose=verbose)
    with _TITLE_LOCK:
        page_range = _PAGES_MEMO.get((doi or "").strip().lower(), "")
    verdict = verify_pdf_identity(save_path, doi, title, pages=page_range,
                                  arxiv_id=arxiv_id_from_doi(doi))
    _note_identity(save_path, verdict.state, verdict.reason)
    if verdict.ok:
        return True

    # Deleting requires POSITIVE evidence that this is the wrong document, and
    # evidence we can actually trust. Two rules, and the asymmetry between them
    # is the whole point: a wrong file kept can be found again by re-running the
    # audit, a right file deleted cannot.
    #
    #   1. Only WRONG and TRUNCATED are positive findings. NO_REFERENCE,
    #      UNREADABLE and NO_ENGINE all mean "nobody checked" -- an absent title,
    #      a scanned page, an install with no PDF reader. None of them is a
    #      statement about the file, and every one of them used to delete it.
    #   2. Not even those, when the metadata the verdict rests on could not be
    #      fetched. A 429 is a fact about the afternoon, not about the paper.
    #
    # Measured before this existed: re-judging 706 known-good corpus PDFs with
    # metadata unavailable deleted eight of them, and every DataCite-only DOI
    # (Zenodo, OSF, figshare) was destroyed on sight because Crossref has no
    # record of it and "no record" was being read as "no title".
    if verdict.state not in (WRONG, TRUNCATED) or _metadata_was_unavailable(doi):
        _note_unverified(doi, url)
        _print_yellow_warning(
            f"⚠️  Could not verify the PDF for {doi}: {verdict.reason} "
            f"KEEPING the file unverified rather than deleting something nobody "
            f"checked -- re-check this record once its metadata is reachable."
        )
        return True

    _note_rejected(doi, url)
    _print_yellow_warning(
        f"⚠️  Discarded a PDF fetched for {doi}: {verdict.reason}"
        + (f"\n     source: {url[:110]}" if url else "")
    )
    _unlink_quietly(save_path)
    return False


#: Destination path -> the URL its bytes came from. Recorded in `try_download`,
#: which every route that writes a PDF passes through, so the provenance
#: sidecar can finally say WHERE a file came from. Until now
#: `sources/legacy_pdf.py` constructed its Artifact with `url=""` and
#: `provenance.py` dropped `Artifact.extra`, so a wrong file on disk carried no
#: record of its own origin -- which is why diagnosing the Dietary Guidelines
#: mis-fetch meant re-running the code rather than reading the sidecar.
_DOWNLOAD_URLS = {}
_DOWNLOAD_URL_LOCK = threading.Lock()


def _note_download_url(save_path: str, url: str) -> None:
    if not save_path or not url:
        return
    with _DOWNLOAD_URL_LOCK:
        _DOWNLOAD_URLS[os.path.abspath(save_path)] = url


def download_url_for(save_path: str) -> str:
    """The URL a downloaded artifact came from, or "" if unrecorded."""
    if not save_path:
        return ""
    with _DOWNLOAD_URL_LOCK:
        return _DOWNLOAD_URLS.get(os.path.abspath(save_path), "")


#: Destination path -> (verdict state, reason) from the last identity check run
#: against it in this process. Same shape and same reason as _DOWNLOAD_URLS: the
#: verdict is decided deep inside `_accept_downloaded_pdf`, which reports it to
#: its caller as a bare True/False, so by the time the CLI has a result the only
#: surviving trace of "this PDF was the wrong article" is a printed warning.
#: --json reads it back here rather than parsing that line.
_IDENTITY_VERDICTS = {}
_IDENTITY_LOCK = threading.Lock()


def _note_identity(save_path: str, state: str, reason: str = "") -> None:
    if not save_path:
        return
    with _IDENTITY_LOCK:
        _IDENTITY_VERDICTS[os.path.abspath(save_path)] = (state, reason or "")


def identity_for(save_path: str):
    """(state, reason) of the last identity check on a path, or None.

    None means nobody judged it: an .xml artifact, a file already on disk, or a
    record whose chain never wrote a PDF at all. That is a different answer from
    any of the pdf_identity states and is kept distinct.
    """
    if not save_path:
        return None
    with _IDENTITY_LOCK:
        return _IDENTITY_VERDICTS.get(os.path.abspath(save_path))


def _stat_key(path: str):
    """(size, mtime) for a path, or None. Enough to tell "untouched" from "rewritten"."""
    try:
        info = os.stat(path)
        return (info.st_size, info.st_mtime_ns)
    except OSError:
        return None


def _artifact_snapshot(save_path: str) -> dict:
    """Every artifact already on disk for this record, by absolute path."""
    from .retrieval.tiers import ARTIFACT_EXTENSIONS

    snapshot = {}
    if not save_path:
        return snapshot
    stem = os.path.splitext(save_path)[0]
    for candidate in [save_path] + [stem + ext for ext in ARTIFACT_EXTENSIONS]:
        key = _stat_key(candidate)
        if key is not None:
            snapshot[os.path.abspath(candidate)] = key
    return snapshot


def _unlink_quietly(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _looks_like_no_fulltext_item(html_text: str) -> bool:
    """True when a landing page is a news/editorial item with no PDF to get.

    Deliberately narrow: it requires an explicit schema.org NewsArticle (or
    Report) marker AND the absence of a citation_pdf_url. Guessing from URL
    shape or title would misfile real articles as unfetchable, which is a worse
    error than the one this fixes -- a paper wrongly marked "no full text"
    would never be retried.
    """
    if not html_text:
        return False
    if "citation_pdf_url" in html_text:
        return False
    compact = html_text.replace("\n", " ")
    return any(marker in compact for marker in _NO_FULLTEXT_SCHEMA_TYPES)


def _quiet_close(browser) -> None:
    """Close a Playwright browser without letting teardown fail the record.

    browser.close() can raise ("Connection closed while reading from the
    driver"), and at several sites below the close sits between the file write
    and `return save_path` -- so the raise skips the return and a complete,
    validated PDF already on disk is reported as a failure.
    """
    try:
        browser.close()
    except Exception:
        pass


def _unregister_pool_from_atexit(executor) -> bool:
    """Let the interpreter exit without joining this pool's worker threads.

    concurrent.futures installs a `_python_exit` hook that join()s every live
    pool thread at shutdown. A worker blocked in a socket read therefore keeps
    the process alive indefinitely *after* the batch summary has printed --
    shutdown(wait=False, cancel_futures=True) does not help, because the join
    happens later and unconditionally. That is the end-of-run stall.

    Dropping the threads from that registry is what does the work: _python_exit
    only joins what it finds there. (Setting .daemon is NOT an option -- CPython
    raises "cannot set daemon status of active thread" once a thread is running,
    and these already are.) Threading's own _shutdown still joins non-daemon
    threads, but a thread absent from the futures registry never receives the
    None sentinel, so it stays parked in its socket read and the interpreter's
    normal teardown is not gated on it.

    Writes are atomic (os.replace), so a worker abandoned mid-download costs a
    temp file, not a corrupt artifact.

    Returns False if CPython's internals have moved, in which case the caller
    still works -- it just keeps the old blocking-at-exit behaviour.
    """
    try:
        import concurrent.futures.thread as _cft
    except Exception:
        return False
    try:
        registry = getattr(_cft, "_threads_queues", None)
        if registry is None:
            return False
        threads = list(getattr(executor, "_threads", ()) or ())
        if not threads:
            return False
        for thread in threads:
            registry.pop(thread, None)
        return True
    except Exception:
        return False

# Suppress InsecureRequestWarning when we fall back to verify=False for sites with SSL issues
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import urljoin, quote_plus, urlparse

# PDF-preferring, used by try_download. text/html is present (q=0.9) so a
# publisher that 406s an Accept with no HTML type still answers; the PDF
# types stay first so a content-negotiating OA host serves the file.
headers = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "application/pdf,application/octet-stream,"
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Landing-page GETs. PDF-first Accept plus Sec-Fetch-Site: same-origin on a
# first request to a new host is a bot tell and a documented 406 cause
# (eLife's Fastly/Varnish among others). No empty Referer: that header is
# either a real URL (set per request) or absent.
HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# 406 retry: drop Sec-Fetch / Accept-Encoding entirely. Some CDNs 406 a
# Chrome UA that claims br without a matching Client Hints set.
_HTML_HEADERS_MINIMAL = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DOI_URL_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
FILENAME_SAFE_DOI_RE = re.compile(r"^(10\.\d{4,9})--(.+)$", re.IGNORECASE)
PMID_URL_RE = re.compile(r"^https?://(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?", re.IGNORECASE)
PMID_INPUT_RE = re.compile(r"^(?:pmid[\s:._-]*)?(\d{4,10})$", re.IGNORECASE)
DOI_CANDIDATE_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)


def _ncbi_url(base_url):
    """Append NCBI Entrez api_key param if available."""
    if _ENTREZ_API_KEY:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}api_key={_ENTREZ_API_KEY}"
    return base_url


def _get_with_retries(url, timeout=15, retries=3, backoff=0.5, **kwargs):
    """
    requests.get with exponential backoff on transient network errors.

    Retries on SSL EOF, connection reset, read/connect timeouts, chunked-encoding
    truncation. Non-transient errors (HTTP 4xx/5xx, JSON parse, etc.) are the
    caller's problem — we only retry low-level transport failures that tend to
    resolve themselves on the next attempt. Raises the last caught exception
    if every attempt fails.
    """
    from requests.exceptions import ConnectionError as _ReqConnErr, Timeout, ChunkedEncodingError
    last_exc = None
    for attempt in range(retries):
        try:
            return requests.get(url, timeout=timeout, **kwargs)
        except (SSLError, _ReqConnErr, Timeout, ChunkedEncodingError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc


# Session-level circuit breaker for the PubMed *website* (pubmed.ncbi.nlm.nih.gov).
# The NCBI website — unlike the E-utilities API (eutils.ncbi.nlm.nih.gov) — throttles
# aggressively under parallel load and returns slow 15s read-timeouts. Several deep
# fallbacks scrape it per paper, so a throttled NCBI can burn 30-45s of dead time on
# every remaining DOI. Once it has timed out a few times we stop scraping it for the
# rest of the run. DOI/metadata resolution via E-utilities and Europe PMC is unaffected.
_PUBMED_WEB_DISABLED = False
_PUBMED_WEB_TIMEOUT_COUNT = 0
_PUBMED_WEB_LOCK = threading.Lock()


def _get_pubmed_web_html(pmid, verbose=False, timeout=10):
    """Fetch a PubMed abstract page (the website) with retries + a session circuit breaker.

    Returns the page HTML on success, else None. After repeated read-timeouts it disables
    further PubMed-website scraping for the rest of the session so doomed scrapes fail fast.
    """
    global _PUBMED_WEB_DISABLED, _PUBMED_WEB_TIMEOUT_COUNT
    from requests.exceptions import Timeout, ConnectionError as _ReqConnErr
    if _PUBMED_WEB_DISABLED or not pmid:
        return None
    try:
        r = _get_with_retries(
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            headers=headers,
            timeout=timeout,
            retries=2,
        )
        if r.status_code == 200 and r.text:
            return r.text
        return None
    except (Timeout, _ReqConnErr) as e:
        with _PUBMED_WEB_LOCK:
            _PUBMED_WEB_TIMEOUT_COUNT += 1
            if _PUBMED_WEB_TIMEOUT_COUNT >= 3 and not _PUBMED_WEB_DISABLED:
                _PUBMED_WEB_DISABLED = True
                _print_yellow_warning(
                    "⚠️  PubMed website (pubmed.ncbi.nlm.nih.gov) repeatedly timed out; "
                    "disabling PubMed-web scraping for this session. "
                    "DOI/metadata lookups via E-utilities & Europe PMC are unaffected."
                )
        if verbose:
            print(f"  PubMed web fetch failed for {pmid}: {str(e)[:100]}")
        return None
    except Exception as e:
        if verbose:
            print(f"  PubMed web fetch error for {pmid}: {str(e)[:100]}")
        return None


def defers_existing_to_engine(tiered: bool, upgrade_existing: bool,
                              get_xml_or_html: bool) -> bool:
    """Should the batch worker let the engine decide about files already on disk?

    The worker's cheap "does any artifact exist?" check is right for the default
    path and wrong for both goal-aware flags, which reason per goal rather than per
    record:

      --upgrade-existing   is looking for a BETTER tier than what is here
      --get-xml-or-html    is looking for the MISSING HALF of a pair

    Skipping in the worker defeats both. It cost a silent no-op once already: a
    directory of PDFs reported every record "already exists" and gained no XML at
    all. Named rather than inlined because that failure is invisible -- the run
    looks like a success.
    """
    return bool(tiered and (upgrade_existing or get_xml_or_html))


def skip_check_extensions(tiered: bool) -> list:
    """Which suffixes count as "this record is already downloaded".

    Two gates, deliberately. How many suffixes count is a property of the TIERED
    path only: widening it for --pull-supplementary would start skipping
    default-path records that happen to have a .fulltext.html or .landing.html on
    disk, which the chain would otherwise have re-attempted.

    Shared rather than inlined because two places have to agree exactly -- the
    batch worker's per-record skip and the startup scan that tells the operator
    how many records it is about to skip. A scan that counted a wider set would
    promise a skip that never happens.
    """
    if tiered:
        from .retrieval.tiers import ARTIFACT_EXTENSIONS

        return list(ARTIFACT_EXTENSIONS)
    return [".pdf", ".xml"]


def _record_source(_source_out, source):
    """If _source_out is provided (mutable list), set _source_out[0] = source."""
    if _source_out is not None:
        _source_out[0] = source


# ---------------------------------------------------------------------------
# Per-source cost accounting
#
# --tracksource has always recorded which source WON, and never what any source
# COST. That made "is this step worth its time?" unanswerable from a run -- the
# only way to answer it was to aggregate source_tracking.csv across every output
# directory on disk and then read the code to guess at timings. The seconds-per-hit
# column below is the whole point: a source with real cost and no hits sorts
# straight to the top.
#
# Counters are always collected (they are three numbers per source) but only
# written out under --tracksource, so no run gains a file it did not ask for.
# ---------------------------------------------------------------------------

_SOURCE_TIMING = {}
_SOURCE_TIMING_LOCK = threading.Lock()


def _timed(source_name):
    """Count calls, hits and wall-clock seconds for one source function.

    Applied to the chain steps that already exist as functions -- one line each,
    rather than instrumenting the ~23 inline blocks. A source that raises still
    has its call and its time recorded, because a source that reliably blows up
    after 20 seconds is exactly what this is meant to expose.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            hit = False
            try:
                result = fn(*args, **kwargs)
                hit = bool(result)
                return result
            finally:
                elapsed = time.monotonic() - started
                # "3 hits" does not say whether they were PDFs or XML stubs, and
                # those are very different outcomes -- an XML-only source looks
                # like a win in the hits column while leaving every record
                # without a PDF. Break the hits down by what was actually saved.
                fmt = None
                if hit:
                    try:
                        fmt = format_label(result if isinstance(result, str) else None)
                    except Exception:
                        fmt = None
                with _SOURCE_TIMING_LOCK:
                    entry = _SOURCE_TIMING.setdefault(
                        source_name,
                        {"calls": 0, "hits": 0, "seconds": 0.0, "formats": {}},
                    )
                    entry.setdefault("formats", {})
                    entry["calls"] += 1
                    entry["seconds"] += elapsed
                    if hit:
                        entry["hits"] += 1
                        # A source can return True rather than a path (the
                        # legacy chain's try_* helpers do); count those as
                        # "hit, format unknown" instead of guessing.
                        key = fmt or "?"
                        entry["formats"][key] = entry["formats"].get(key, 0) + 1
        return wrapper
    return decorator


def source_timing_rows():
    """Timing as sorted rows, most expensive-per-hit first.

    A source with zero hits has no finite cost per hit, so it sorts above
    everything -- which is the ranking a reader wants.
    """
    with _SOURCE_TIMING_LOCK:
        snapshot = {k: dict(v) for k, v in _SOURCE_TIMING.items()}
    rows = []
    for source, e in snapshot.items():
        per_hit = (e["seconds"] / e["hits"]) if e["hits"] else None
        formats = e.get("formats") or {}
        rows.append({
            "source": source,
            "calls": e["calls"],
            "hits": e["hits"],
            # Per-format hit counts, so "3 hits" is legible as 3 XML or 3 PDFs.
            "xml": formats.get("xml", 0),
            "html": formats.get("html", 0),
            "pdf": formats.get("pdf", 0),
            "other": sum(v for k, v in formats.items()
                         if k not in _HEADLINE_FORMATS),
            "total_seconds": round(e["seconds"], 2),
            "mean_seconds": round(e["seconds"] / e["calls"], 3) if e["calls"] else 0.0,
            "seconds_per_hit": round(per_hit, 2) if per_hit is not None else "",
        })
    # No hits first (cost with nothing to show for it), then dearest per hit.
    rows.sort(key=lambda r: (r["seconds_per_hit"] != "", -(r["seconds_per_hit"] or 0)))
    return rows


def reset_source_timing():
    """Clear the counters. Exists for tests; batches are one process each."""
    with _SOURCE_TIMING_LOCK:
        _SOURCE_TIMING.clear()


#: Defaults for a build without the optional source pack. The private block
#: below overrides all three when the pack is present; without it these stand,
#: every last-resort site in this module is already a no-op, and
#: `last_resorts_enabled()` can still answer for the build it is running in.
LAST_RESORT_SOURCES = ()
_LAST_RESORTS_ENABLED = False
_DISABLED_SOURCES = set()


def last_resorts_enabled():
    """Whether any optional last-resort source can still be reached this process.

    Public, and outside the private block, because a caller with a licence
    posture to defend has to be able to *prove* the answer rather than assume
    it. A pipeline that stores retrieved bytes and gates egress on how they were
    acquired needs an assertion it can put in a test; a comment saying the pack
    is absent is not one, and neither is an import that raises in one build and
    not the other.

    A build without the pack answers False because there is nothing to reach. A
    build that has called `disable_last_resorts` answers False because it was
    turned off. The two are deliberately indistinguishable from outside: what a
    caller can act on is reachability, not which mechanism delivered it.
    """
    return _LAST_RESORTS_ENABLED and any(
        src.name not in _DISABLED_SOURCES for src in LAST_RESORT_SOURCES
    )




#: Progress-display label per artifact suffix. Longest suffix first, so
#: ".fulltext.html" is not read as a landing page and ".source.tar.gz" is not
#: read as a gzip of nothing in particular.
_FORMAT_LABELS = (
    (".source.tar.gz", "latex"),
    (".fulltext.html", "html"),
    (".landing.html", "landing"),
    (".suppl.zip", "suppl"),
    ("_abstract.md", "abstract"),
    (".xml", "xml"),
    (".pdf", "pdf"),
    (".txt", "text"),
)


def format_label(path):
    """Which artifact format a saved path represents, for the running tally.

    Keyed on the suffix rather than the tier because it has to work on the
    default path too, where nothing computes a tier but .xml files still show up
    via the Elsevier fallback.
    """
    if not path:
        return None
    lowered = str(path).lower()
    for suffix, label in _FORMAT_LABELS:
        if lowered.endswith(suffix):
            return label
    return "other"


#: Always shown in the running tally, even at zero. A batch that has produced no
#: XML yet and a batch that has produced no XML at all look identical if the
#: label only appears once it is non-zero -- and "is this run getting any XML?"
#: is the whole question the tally exists to answer.
_HEADLINE_FORMATS = ("xml", "html", "pdf")


def _tally_cell(label, count):
    """`xml  4`: counts right-aligned in two columns, so the tallies line up
    down a log; a column widens on its own once a count needs three digits."""
    return f"{label} {count:>{max(2, len(str(count)))}}"


def running_tally(counter, missing=None):
    """'xml  4 | html  0 | pdf  5 | missing  2 | landing  1' -- headline formats
    always present, `missing` (records that produced nothing) when given, the
    rarer formats trailing once seen."""
    parts = [_tally_cell(label, counter.get(label, 0)) for label in _HEADLINE_FORMATS]
    if missing is not None:
        parts.append(_tally_cell("missing", missing))
    parts += [
        _tally_cell(label, count)
        for label, count in sorted(counter.items())
        if label not in _HEADLINE_FORMATS and count
    ]
    return " | ".join(parts)


def _format_tally(counter, total=None):
    """'xml 4 (40%), pdf 5 (50%)', commonest first. Empty string if nothing yet."""
    if not counter:
        return ""
    total = total or sum(counter.values())
    if not total:
        return ""
    return ", ".join(
        f"{label} {count} ({count / total * 100:.0f}%)"
        for label, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def canonicalize_doi(raw_doi):
    """Normalize DOI input and restore filename-safe form."""
    if not isinstance(raw_doi, str):
        return raw_doi

    doi = DOI_URL_PREFIX_RE.sub("", raw_doi.strip()).lower()
    match = FILENAME_SAFE_DOI_RE.match(doi)
    if match:
        # Restore filesystem-escaped chars in the suffix (inverse of doi_to_safe_filename).
        # Every '--' becomes '/', not just the one the regex split on: DOIs with two
        # or more path segments are common (10.1093/abm/kaad072 encodes to
        # 10.1093--abm--kaad072), and decoding only the first left the rest corrupted.
        suffix = _decode_fs_tokens(match.group(2)).replace("--", "/").replace("\x00", "-")
        doi = f"{match.group(1)}/{suffix}"
    return doi


def sanitize_doi(raw_doi):
    """Strip common URL artifacts from DOI strings.

    Handles: query params (?...), /full/html, /html, trailing slashes,
    .pdf extensions, bioRxiv version suffixes, OUP trailing article IDs.
    """
    if not isinstance(raw_doi, str):
        return raw_doi

    original = raw_doi.strip()
    doi = original

    # Strip query parameters (?redirectedfrom=fulltext, ?journalcode=prxa, etc.)
    if '?' in doi:
        doi = doi.split('?')[0]

    # Strip common URL path suffixes
    for suffix in ['/full/html', '/full/pdf', '/html', '/pdf', '/abstract', '/summary', '/full']:
        if doi.lower().endswith(suffix):
            doi = doi[:len(doi) - len(suffix)]
            break

    # Strip trailing slashes
    doi = doi.rstrip('/')

    # Strip .pdf extension (e.g. 10.1101/2024.02.13.580153v1.full.pdf)
    if doi.lower().endswith('.pdf'):
        doi = doi[:-4]

    # Strip bioRxiv/medRxiv version suffixes (v1.full, v2.full, etc.)
    doi = re.sub(r'v\d+\.full$', '', doi, flags=re.IGNORECASE)

    # Strip OUP-style trailing numeric article IDs (e.g. 10.1093/abm/kaad072/7512904)
    # OUP DOIs are 10.1093/{journal}/{article} — trailing /\d+ is a URL artifact
    if doi.startswith('10.1093/'):
        doi = re.sub(r'/\d+$', '', doi)

    # Convert filename-format DOIs back to real DOIs (inverse of
    # doi_to_safe_filename): '~XX~'/'~' -> forbidden chars, '--' -> '/'.
    # e.g. "10.1093/eurheartj--ehaf339" → "10.1093/eurheartj/ehaf339"
    #      "10.1023/a~1018769825030"    → "10.1023/a:1018769825030"
    # A string that already contains '/' is a real DOI, whose '--' may be literal
    # (ASEE: 10.18260/1-2--47556) — leave it alone. The one observed mixed
    # artifact class (OUP paths with an encoded tail) keeps the old behavior.
    if '~' in doi:
        doi = _decode_fs_tokens(doi)
    if '--' in doi and ('/' not in doi or doi.startswith('10.1093/')):
        doi = doi.replace('--', '/')
    doi = doi.replace('\x00', '-')

    return doi


def extract_pmid(raw_identifier):
    """Extract PMID digits from plain or URL input."""
    if not isinstance(raw_identifier, str):
        return None
    text = raw_identifier.strip()
    if not text:
        return None

    m = PMID_URL_RE.match(text)
    if m:
        return m.group(1)

    m = PMID_INPUT_RE.match(text)
    if m:
        return m.group(1)
    return None


def doi_to_pmid(doi: str, verbose=False):
    """Resolve DOI to PMID when the article is indexed in PubMed."""
    if not isinstance(doi, str) or not doi.strip():
        return None
    doi = doi.strip()
    if not doi.startswith("10."):
        doi = canonicalize_doi(doi)
    if not doi or not doi.startswith("10."):
        return None

    # 1) NCBI idconv (accepts DOI, returns pmid when in PubMed)
    try:
        r = _get_with_retries(
            _ncbi_url(f"{IDCONV_URL}?ids={quote_plus(doi)}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            records = (r.json() or {}).get("records") or []
            for rec in records:
                pmid = rec.get("pmid")
                if pmid is not None:
                    return str(pmid).strip()
    except Exception as e:
        if verbose:
            print(f"  DOI->PMID idconv lookup failed for {doi}: {str(e)[:100]}")

    # 2) Europe PMC search by DOI
    try:
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{quote_plus(doi)}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            hits = (r.json().get("resultList") or {}).get("result") or []
            for h in hits:
                if h.get("pmid"):
                    return str(h["pmid"]).strip()
    except Exception as e:
        if verbose:
            print(f"  DOI->PMID Europe PMC lookup failed: {str(e)[:100]}")
    return None


def pmid_to_doi(pmid: str, verbose=False):
    """Resolve PMID to DOI using multiple metadata sources."""

    def _clean(doi_value):
        if not isinstance(doi_value, str):
            return None
        doi = doi_value.strip()
        if doi.lower().startswith("doi:"):
            doi = doi[4:].strip()
        doi = doi.strip(" \t\r\n.;,)]}>\"'")
        doi = canonicalize_doi(doi)
        return doi if doi.startswith("10.") else None

    def _first_doi_from_text(text):
        if not isinstance(text, str):
            return None
        m = DOI_CANDIDATE_RE.search(text)
        if not m:
            return None
        return _clean(m.group(0))

    def _extract_pubmed_core_fields(xml_text: str):
        title = ""
        journal = ""
        year = None
        if not isinstance(xml_text, str) or not xml_text:
            return title, journal, year

        mt = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", xml_text, re.IGNORECASE | re.DOTALL)
        if mt:
            title = re.sub(r"<[^>]+>", " ", mt.group(1))
            title = html.unescape(title)
            title = re.sub(r"\s+", " ", title).strip()

        mj = re.search(r"<JournalTitle>(.*?)</JournalTitle>", xml_text, re.IGNORECASE | re.DOTALL)
        if mj:
            journal = re.sub(r"<[^>]+>", " ", mj.group(1))
            journal = html.unescape(journal)
            journal = re.sub(r"\s+", " ", journal).strip()

        # Prefer article publication year from PubDate/ArticleDate; first <Year> can be DateCompleted etc.
        my = re.search(
            r"<(?:PubDate|ArticleDate)[^>]*>.*?<Year>(\d{4})</Year>",
            xml_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not my:
            my = re.search(r"<Year>(\d{4})</Year>", xml_text, re.IGNORECASE)
        if my:
            try:
                year = int(my.group(1))
            except Exception:
                year = None
        return title, journal, year

    def _crossref_item_year(item: dict):
        for key in ("issued", "published-print", "published-online", "created"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and isinstance(parts[0], list) and parts[0]:
                try:
                    return int(parts[0][0])
                except Exception:
                    pass
        return None

    def _lookup_doi_via_crossref_title(title: str, journal: str, year, verbose=False):
        title = (title or "").strip()
        if len(title) < 8:
            return None

        candidates = {}
        queries = [
            {"query.title": title, "rows": 25},
            {"query.bibliographic": " ".join(x for x in [title, journal or "", str(year or "")] if x), "rows": 25},
        ]
        try:
            for params in queries:
                # Add mailto for polite pool (10 req/s vs 5 req/s)
                if _DEFAULT_EMAIL:
                    params["mailto"] = _DEFAULT_EMAIL
                r = _crossref_raw_get("https://api.crossref.org/works", params=params, timeout=15)
                if r.status_code != 200:
                    continue
                items = ((r.json() or {}).get("message") or {}).get("items") or []
                for item in items:
                    doi = _clean(item.get("DOI"))
                    if not doi:
                        continue
                    if doi in candidates:
                        continue
                    ctitle = ((item.get("title") or [""])[0] or "").strip()
                    cjournal = ((item.get("container-title") or [""])[0] or "").strip()
                    cyear = _crossref_item_year(item)
                    t_sim = _text_similarity(title, ctitle)
                    j_sim = _text_similarity(journal, cjournal) if journal else 0.0
                    y_bonus = 0.15 if (year and cyear and year == cyear) else 0.0
                    score = (0.75 * t_sim) + (0.20 * j_sim) + y_bonus
                    candidates[doi] = {
                        "score": score,
                        "title_sim": t_sim,
                        "journal_sim": j_sim,
                        "year": cyear,
                        "crossref_title": ctitle,
                    }

            if not candidates:
                return None

            # When PubMed has a year, only consider Crossref candidates with that year
            # (avoids wrong match for common titles e.g. "Chronic fatigue syndrome" 2008 vs 2016)
            if year is not None:
                same_year = {k: v for k, v in candidates.items() if v["year"] == year}
                if not same_year:
                    if verbose:
                        print(
                            f"  Crossref title lookup: no candidate with year={year} for PMID {pmid}, skipping"
                        )
                    return None
                candidates = same_year

            best_doi, best = max(candidates.items(), key=lambda kv: kv[1]["score"])
            strong_title = best["title_sim"] >= 0.72
            year_mismatch = (
                year is not None
                and best["year"] is not None
                and year != best["year"]
            )
            plausible = strong_title and not year_mismatch and (
                (year and best["year"] and year == best["year"])
                or best["journal_sim"] >= 0.40
                or best["score"] >= 0.68
            )
            if not plausible:
                if verbose:
                    print(
                        f"  Crossref title lookup best match too weak for PMID {pmid}: "
                        f"sim={best['title_sim']:.2f}, journal={best['journal_sim']:.2f}, score={best['score']:.2f}"
                    )
                return None
            # When PubMed has a year, verify with Crossref works API (search response can be wrong)
            if year is not None:
                try:
                    crossref_params = {}
                    if _DEFAULT_EMAIL:
                        crossref_params["mailto"] = _DEFAULT_EMAIL
                    verify_r = _crossref_raw_get(
                        f"https://api.crossref.org/works/{quote_plus(best_doi)}",
                        params=crossref_params,
                        timeout=10,
                    )
                    if verify_r.status_code == 200:
                        v_item = (verify_r.json() or {}).get("message") or {}
                        v_year = _crossref_item_year(v_item)
                        if v_year is not None and v_year != year:
                            if verbose:
                                print(
                                    f"  Crossref title lookup rejecting year mismatch (works API): "
                                    f"PMID {pmid} PubMed year={year}, {best_doi} year={v_year}"
                                )
                            return None
                except Exception:
                    pass
            if verbose:
                print(
                    f"  Crossref title lookup matched PMID {pmid} -> DOI {best_doi} "
                    f"(sim={best['title_sim']:.2f}, score={best['score']:.2f})"
                )
            return canonicalize_doi(best_doi)
        except Exception as e:
            if verbose:
                print(f"  Crossref title lookup failed for PMID {pmid}: {str(e)[:100]}")
            return None

    # 1) NCBI PMC idconv
    try:
        r = _get_with_retries(
            _ncbi_url(f"{IDCONV_URL}?ids={pmid}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            records = (r.json() or {}).get("records") or []
            for rec in records:
                doi = _clean(rec.get("doi"))
                if doi:
                    return doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI idconv lookup failed for {pmid}: {str(e)[:100]}")

    # 2) PubMed EFetch XML (ArticleId IdType=doi / ELocationID)
    try:
        xml_text = ""
        r = _get_with_retries(
            _ncbi_url(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"),
            timeout=12,
        )
        if r.status_code == 200 and r.text:
            xml_text = r.text
            article_ids = re.findall(
                r'<ArticleId[^>]*IdType=["\']doi["\'][^>]*>([^<]+)</ArticleId>',
                xml_text,
                re.IGNORECASE,
            )
            for candidate in article_ids:
                doi = _clean(candidate)
                if doi:
                    return doi

            e_location_ids = re.findall(
                r'<ELocationID[^>]*EIdType=["\']doi["\'][^>]*>([^<]+)</ELocationID>',
                xml_text,
                re.IGNORECASE,
            )
            for candidate in e_location_ids:
                doi = _clean(candidate)
                if doi:
                    return doi

            fallback_doi = _first_doi_from_text(xml_text)
            if fallback_doi:
                return fallback_doi

            # 2b) No DOI field present -> Crossref title-based recovery.
            title, journal, year = _extract_pubmed_core_fields(xml_text)
            crossref_doi = _lookup_doi_via_crossref_title(title, journal, year, verbose=verbose)
            if crossref_doi:
                return crossref_doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI efetch lookup failed for {pmid}: {str(e)[:100]}")

    # 3) Europe PMC
    try:
        q = f"EXT_ID:{pmid}%20AND%20SRC:MED"
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = ((r.json() or {}).get("resultList") or {}).get("result") or []
            for result in results:
                doi = _clean(result.get("doi"))
                if doi:
                    return doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI Europe PMC lookup failed for {pmid}: {str(e)[:100]}")

    # 4) PubMed HTML meta tag fallback
    try:
        page_text = _get_pubmed_web_html(pmid, verbose=verbose)
        if page_text:
            m = re.search(
                r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']',
                page_text,
                re.IGNORECASE,
            )
            if m:
                doi = _clean(m.group(1))
                if doi:
                    return doi

            fallback_doi = _first_doi_from_text(page_text)
            if fallback_doi:
                return fallback_doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI PubMed HTML lookup failed for {pmid}: {str(e)[:100]}")

    if verbose:
        print(f"  PMID->DOI resolution exhausted all sources for {pmid}")
    return None


def resolve_identifier_to_doi(identifier, verbose=False):
    """
    Normalize DOI-like inputs and resolve PMID inputs to DOI.
    Returns canonical DOI string on success, else None.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        return None

    raw = sanitize_doi(identifier.strip())
    doi_candidate = canonicalize_doi(raw)
    if doi_candidate.startswith("10."):
        return doi_candidate

    pmid = extract_pmid(raw)
    if pmid:
        resolved = pmid_to_doi(pmid, verbose=verbose)
        if verbose:
            if resolved:
                print(f"  Resolved PMID {pmid} -> DOI {resolved}")
            else:
                print(f"  Could not resolve PMID {pmid} to a DOI")
        return resolved

    return None


# NTFS/exFAT forbidden filename chars other than '/' (-> '--') and ':' (-> '~').
# Rare in DOIs (mainly ancient Wiley SICI DOIs with '<' '>'); hex-escaped '~XX~'.
_FS_FORBIDDEN = '<>"\\|?*'


def doi_to_safe_filename(doi: str) -> str:
    """Convert DOI to a filesystem-safe filename stem (reversibly).

    Encoding (must match mo_pipeline.corpus.models.doi_to_folder):
      '/' -> '--'          (DOI path separator)
      ':' -> '~'           (colon; forbidden on NTFS/exFAT, ~never a literal in DOIs)
      < > " \\ | ? * -> '~XX~'  (hex-escaped; e.g. Wiley SICI DOIs)
      '-' -> '~2d~'        (only in a '--' run or adjacent to '/', where it would be
                            ambiguous with the slash encoding; ASEE DOIs like
                            10.18260/1-2--47556 contain a literal '--')
    Distinct tokens keep the mapping reversible — the old behavior collapsed both
    '/' and ':' to '--', making 10.1023/a:123 indistinguishable from 10.1023/a/123."""
    s = re.sub(r"-+(?=/)|(?<=/)-+|-{2,}", lambda m: "~2d~" * len(m.group()), doi)
    s = s.replace("/", "--")
    for ch in _FS_FORBIDDEN:
        s = s.replace(ch, f"~{ord(ch):02x}~")
    s = s.replace(":", "~")
    return _windows_safe_stem(s)


#: Windows reserves these as device names -- WITH any extension, so "con.pdf" is
#: still the console. Opening one does not raise: it writes to the device and no
#: file appears on disk, which is the worst shape a bug can take here (a
#: "successful" download that left nothing behind).
_DOS_DEVICES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

#: Characters _windows_safe_stem may hex-escape, beyond _FS_FORBIDDEN. The dot
#: and space are the trailing-character rule; the letters are the first
#: character of every DOS device name, escaped to defeat the device test. Listed
#: explicitly so _decode_fs_tokens can invert exactly this set -- a decoder that
#: guessed would be the thing that breaks reversibility.
_FS_NAME_ESCAPED = ". capnlCAPNL"


def _windows_safe_stem(stem: str) -> str:
    """Neutralise the two Windows NAME-level rules the char escapes miss.

    The escapes above handle forbidden characters. Two rules are about the name
    as a whole and survive them:

    1. Trailing dots and spaces are silently STRIPPED by Windows, so a stem
       ending in one names a different file than the string we hold. With
       --make-subfolder that desynchronises the directory from the path the
       code carries, and the exists-check then misses every time.
    2. A basename whose pre-extension part is a DOS device is the device.

    Both are fixed by hex-escaping into the SAME '~XX~' vocabulary the rest of
    the encoding uses, so the result stays reversible by _decode_fs_tokens and
    stays byte-identical to mo_pipeline.corpus.models.doi_to_folder, which must
    mirror this function. A real DOI always starts "10." so it cannot hit rule
    2 -- but this is also called on bare identifiers, where "con" can arrive.
    """
    if not stem:
        return stem
    if stem[-1] in ". ":
        # Escaping the final character alone is enough: the name no longer ends
        # in a dot or space, so Windows leaves it intact.
        stem = stem[:-1] + f"~{ord(stem[-1]):02x}~"
    if stem.split(".")[0].upper() in _DOS_DEVICES:
        # Escape the FIRST character rather than appending anything. Appending
        # would decode back into the identifier and corrupt it; escaping a
        # character that is already part of the name round-trips exactly, and
        # the escaped form is no longer a device name.
        stem = f"~{ord(stem[0]):02x}~" + stem[1:]
    return stem


def _decode_fs_tokens(s: str) -> str:
    """Restore the '~XX~' hex tokens + bare '~' -> ':' (inverse of the escapes in
    doi_to_safe_filename, excluding the '--' -> '/' step which callers handle).
    Escaped hyphens ('~2d~') become a '\\x00' sentinel so the caller's later
    '--' -> '/' pass cannot touch them; callers restore them with
    .replace('\\x00', '-') AFTER that pass."""
    s = s.replace("~2d~", "\x00")
    for ch in _FS_FORBIDDEN:
        s = s.replace(f"~{ord(ch):02x}~", ch)
    # Also emitted by _windows_safe_stem, which escapes a trailing dot/space and
    # a leading device-name letter into the same vocabulary. Decoded here so the
    # mapping stays bijective; without this the generic '~' -> ':' below would
    # turn "~2e~" into ":2e:".
    for ch in _FS_NAME_ESCAPED:
        s = s.replace(f"~{ord(ch):02x}~", ch)
    return s.replace("~", ":")


# ---------------------------------------------------------------------------
# Safe-filename -> DOI inverse (with legacy-encoding fallback)
# ---------------------------------------------------------------------------

def safe_filename_to_doi_variants(safe_name: str) -> list[str]:
    """Decode a safe-DOI filename into candidate DOIs, most likely first.

    Modern encoding (see ``doi_to_safe_filename``) writes ':' as '~' and any
    '-{2,}' run as '~2d~' escapes, so the only '--' that appears in a
    modern-encoded name is the one separating '10.NNNN' from the suffix.

    An OLDER encoding (used by some corpora on disk) wrote BOTH '/' and ':'
    as '--'. That makes the decoding ambiguous: '10.1023--a--1016394031947'
    could mean either '10.1023/a/1016394031947' (modern reading, two path
    segments) or '10.1023/A:1016394031947' (legacy reading, colon-DOI in
    Kluwer/Springer style). We cannot tell without asking a registry.

    Rather than commit to one reading, this function returns BOTH candidates
    when the raw suffix contains '--' after the prefix separator; the caller
    resolves the ambiguity (e.g., by trying each and keeping the one that
    Crossref knows). Modern reading first because that's what any recently
    written folder will be.

    Returns
    -------
    list[str]
        Ordered list of candidate DOIs. Empty for empty input. For inputs
        that don't match the safe-DOI shape, returns [safe_name] verbatim so
        the caller can pass through anything that already looks like a DOI.
    """
    if not safe_name:
        return []
    match = FILENAME_SAFE_DOI_RE.match(safe_name)
    if not match:
        return [safe_name]

    prefix = match.group(1)
    raw_suffix = match.group(2)

    # Modern reading: '--' -> '/', '~XX~' + bare '~' -> forbidden chars + ':'.
    # _decode_fs_tokens returns the '\x00' sentinel for '~2d~' so the '--'
    # -> '/' pass below can't touch escaped hyphens; we restore them after.
    modern_suffix = _decode_fs_tokens(raw_suffix).replace("--", "/").replace("\x00", "-")
    modern = f"{prefix}/{modern_suffix}"
    variants: list[str] = [modern]

    # Legacy reading: any '--' in the suffix after the prefix separator is a
    # colon (the first '--' -- the prefix separator -- was already consumed
    # by FILENAME_SAFE_DOI_RE and is always '/'). Only produce this when the
    # raw suffix actually contains '--'; a modern name never does.
    if "--" in raw_suffix:
        legacy_suffix = _decode_fs_tokens(raw_suffix).replace("--", ":").replace("\x00", "-")
        legacy = f"{prefix}/{legacy_suffix}"
        if legacy != modern:
            variants.append(legacy)

    return variants


def _crossref_doi_exists(doi: str, email: str | None = None,
                         timeout: float = 6.0) -> bool | None:
    """Ask Crossref whether a DOI is registered. None on network error.

    Cheapest endpoint that answers yes/no: ``/works/{doi}/agency`` returns
    the registration agency for known DOIs and 404 for unknown ones. About
    150 bytes on the wire either way.
    """
    if not doi or not doi.startswith("10."):
        return None
    try:
        params = {}
        h = dict(headers)
        if email:
            h["User-Agent"] = f"{h.get('User-Agent','fetchpdf/1.0')} (mailto:{email})"
        r = _crossref_raw_get(
            f"https://api.crossref.org/works/{doi}/agency",
            headers=h, timeout=timeout,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        return None
    except Exception:
        return None


def _pick_variant_via_crossref(variants: list[str],
                               email: str | None = None) -> str:
    """Return the first candidate Crossref confirms; else the first candidate.

    Callers use this when a safe-DOI decoded to multiple candidates and only
    one of them is a real DOI. If Crossref is unreachable we keep the modern
    reading -- the caller's batch will still record the failure honestly.
    """
    if len(variants) <= 1:
        return variants[0] if variants else ""
    for v in variants:
        ok = _crossref_doi_exists(v, email=email)
        if ok is True:
            return v
    return variants[0]


def dois_from_dir(root_dir, email: str | None = None,
                  verbose: bool = False) -> list[str]:
    """Read subdirectory names of ``root_dir`` and decode each to a DOI.

    A subdirectory whose name is not shaped like a safe DOI (does not match
    ``FILENAME_SAFE_DOI_RE``) is silently skipped -- these are usually side
    directories like ``done``, ``figmap``, ``panels``, etc. Legacy '--'-encoded
    folder names are disambiguated via Crossref; when Crossref is
    unreachable the modern reading is kept.

    The returned list preserves discovery order and is deduplicated.
    """
    root = Path(root_dir)
    if not root.is_dir():
        return []
    seen: set[str] = set()
    out: list[str] = []
    n_ambig = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        variants = safe_filename_to_doi_variants(entry.name)
        if not variants:
            continue
        head = variants[0]
        # Non-safe-DOI-shaped names come back as [name] verbatim. Skip anything
        # that doesn't start with a DOI prefix rather than injecting nonsense
        # into the batch.
        if not head.startswith("10."):
            continue
        if len(variants) > 1:
            n_ambig += 1
            chosen = _pick_variant_via_crossref(variants, email=email)
        else:
            chosen = head
        if chosen in seen:
            continue
        seen.add(chosen)
        out.append(chosen)
    if verbose and n_ambig:
        print(f"   • {n_ambig} ambiguous legacy-encoded folder name(s) "
              f"disambiguated via Crossref")
    return out


def migrate_legacy_folders(root_dir, email: str | None = None,
                           dry_run: bool = True,
                           verbose: bool = False) -> list[dict]:
    """Rename legacy '--'-encoded folders under ``root_dir`` to modern encoding.

    A folder is considered legacy when its name matches ``FILENAME_SAFE_DOI_RE``
    AND its suffix contains further '--' AND Crossref confirms the legacy
    reading (':' where the extra '--' sits) resolves to a real DOI while the
    modern reading does not. That last check keeps this safe on directories
    whose names happen to encode multi-segment DOIs (10.1093/abm/kaad072 →
    10.1093--abm--kaad072 -- legitimate modern encoding, no migration).

    Files inside a migrated folder whose stem starts with the OLD folder name
    are also renamed so their stems match the new folder name -- fetchpdf's
    sidecars (``{safe_doi}_supplementary_info.json`` etc.) rely on that
    invariant.

    Returns a list of action records: one dict per folder examined, with
    keys ``dir``, ``action`` (renamed | merged | kept | error),
    ``modern_doi``, ``legacy_doi``, ``new_name`` and, when relevant,
    ``reason``. Nothing on disk changes when ``dry_run`` is True.
    """
    root = Path(root_dir)
    actions: list[dict] = []
    if not root.is_dir():
        return actions
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        m = FILENAME_SAFE_DOI_RE.match(name)
        if not m or "--" not in m.group(2):
            continue

        variants = safe_filename_to_doi_variants(name)
        if len(variants) < 2:
            continue

        modern_doi, legacy_doi = variants[0], variants[1]
        modern_ok = _crossref_doi_exists(modern_doi, email=email)
        legacy_ok = _crossref_doi_exists(legacy_doi, email=email)

        # Only migrate when the legacy reading resolves AND the modern one
        # does not. If both resolve (extremely rare -- would be two DOIs
        # coincidentally colliding under the two encodings), we refuse to
        # act rather than risk destroying data.
        if legacy_ok is not True or modern_ok is True:
            actions.append({
                "dir": str(entry),
                "action": "kept",
                "modern_doi": modern_doi,
                "legacy_doi": legacy_doi,
                "reason": (
                    f"modern_resolves={modern_ok!r}, legacy_resolves={legacy_ok!r}"
                    " -- either modern reading is valid or Crossref could not "
                    "confirm the legacy reading; not renaming"
                ),
            })
            continue

        new_name = doi_to_safe_filename(legacy_doi)
        new_path = root / new_name
        record: dict = {
            "dir": str(entry),
            "modern_doi": modern_doi,
            "legacy_doi": legacy_doi,
            "new_name": new_name,
        }

        if new_path.exists() and new_path != entry:
            # Merge legacy into the already-present modern folder (union of
            # files, never clobber). The legacy folder is then removed once
            # every file it held has a home.
            record["action"] = "merged" if not dry_run else "would_merge"
            record["merged_into"] = str(new_path)
            if not dry_run:
                for src in entry.rglob("*"):
                    rel = src.relative_to(entry)
                    dst = new_path / rel
                    if src.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                        continue
                    if dst.exists():
                        # Collision -- the modern-target's copy is the winner
                        # by no-clobber policy, so the legacy version is
                        # redundant. Drop it so the legacy tree fully empties
                        # and the caller sees exactly one folder per DOI.
                        src.unlink()
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
                # Remove the emptied legacy tree
                for p in sorted(entry.rglob("*"), reverse=True):
                    if p.is_dir() and not any(p.iterdir()):
                        p.rmdir()
                if not any(entry.iterdir()):
                    entry.rmdir()
        else:
            record["action"] = "renamed" if not dry_run else "would_rename"
            if not dry_run:
                entry.rename(new_path)
                # Rename inner files whose stem starts with the old folder
                # name so sidecar filenames match the new folder name.
                for f in new_path.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.startswith(name):
                        new_f = new_path / (new_name + f.name[len(name):])
                        if not new_f.exists():
                            f.rename(new_f)

        actions.append(record)
        if verbose:
            print(f"   [{record['action']}] {name} -> {new_name}")
    return actions


#: What prompt_on_existing may return, and the values --on-existing accepts for
#: the first two. Named so the parser choices and the callers cannot drift.
ON_EXISTING_ASK = "ask"
ON_EXISTING_SKIP = "skip"
ON_EXISTING_SUPPLEMENT = "supplement"
ON_EXISTING_ABORT = "abort"


def count_existing_records(identifiers, output_dir, make_subfolder=False,
                           extensions=None) -> tuple:
    """How many of ``identifiers`` the batch worker would skip as already here.

    Mirrors the worker's own path arithmetic exactly -- the record dir of
    _download_one_inner, then its ARTIFACT_EXTENSIONS loop -- because the number
    is shown to the operator as "N of M will be skipped". A scan that counted
    differently from the run would be worse than no scan at all.

    Two deliberate limitations, both to keep this free:

    * No identifier resolution. resolve_identifier_to_doi returns immediately for
      anything already "10."-shaped, but a PMID costs a round trip, and a scan
      that hit the network once per row would be slower than the skip it is
      describing. PMID-only rows are therefore counted as missing even when their
      folder is on disk -- the run itself still skips them, so the prompt
      undercounts rather than overpromising.
    * Case is settled by canonicalize_doi, which lowercases -- the same call the
      worker makes before encoding a filename, so both sides agree. Do not add
      a case-insensitive directory scan on top: folders written by something
      other than this worker (tools/fetch_into_subfolders.py documents a corpus
      built from pre-lowercased DOIs) would then be counted as skippable when
      the run would not in fact skip them.

    Returns (n_existing, n_total).
    """
    if extensions is None:
        extensions = skip_check_extensions(False)

    total = 0
    existing = 0
    for identifier in identifiers:
        raw = str(identifier).strip()
        if not raw:
            continue
        total += 1
        display_id = canonicalize_doi(sanitize_doi(raw))
        if not display_id.startswith("10."):
            continue  # a PMID or junk -- see the docstring
        safe_doi = doi_to_safe_filename(display_id)
        record_dir = (os.path.join(output_dir, safe_doi)
                      if make_subfolder else output_dir)
        stem = os.path.join(record_dir, safe_doi)
        if any(os.path.exists(stem + ext) for ext in extensions):
            existing += 1
    return existing, total


def _prompt_choice(n_existing, n_total, output_dir):
    """Draw the menu and read one keystroke-ish line. Returns a choice constant.

    Split from prompt_on_existing so the guards can be tested without a tty and
    the menu without re-deciding whether to show it.
    """
    print()
    print(f"\U0001F4C1 {n_existing:,} of {n_total:,} records already have "
          f"artifacts in {output_dir}.")
    print()
    print(f"   [s] skip them             download only the "
          f"{n_total - n_existing:,} still missing   (default)")
    print("   [m] skip, but pull SI/SM  keep the papers, fetch supplementary "
          "material for all of them")
    print("   [a] abort")
    print()
    while True:
        try:
            answer = input("Choice [s]: ").strip().lower()
        except EOFError:
            # stdin closed underneath us mid-prompt. Take the default rather
            # than looping forever on a stream that will never yield a line.
            print()
            return ON_EXISTING_SKIP
        except KeyboardInterrupt:
            # Ctrl-C at a menu means "I did not mean to start this", not
            # "crash". Nothing has been downloaded yet, so abort is clean.
            print()
            return ON_EXISTING_ABORT
        if answer in ("", "s", "skip"):
            return ON_EXISTING_SKIP
        if answer in ("m", "supplement", "si", "sm"):
            return ON_EXISTING_SUPPLEMENT
        if answer in ("a", "abort", "q", "quit"):
            return ON_EXISTING_ABORT
        print("   Please answer s, m or a.")


def prompt_on_existing(identifiers, args):
    """Offer the operator a choice when a run is about to skip existing records.

    The skip itself is silent and per-record, so "I already have these papers,
    this time I want their supplementary material" currently requires knowing
    that --pull-supplementary fires on the skip branch. This surfaces it.

    Returns one of the ON_EXISTING_* constants. Only the supplement answer has a
    side effect: it sets args.pull_supplementary for the rest of the run, which
    keeps every PDF already on disk exactly where it is and adds the SI pass.

    Silent no-op (returns skip) unless every guard passes. The tty guard is the
    load-bearing one: nothing else in this package has ever read stdin, so cron
    jobs, pipes and the -n auto test suite must reach this and walk straight
    through it.
    """
    if args.abstract_only:
        # --abstract-only already warns that --pull-supplementary is ignored;
        # offering it here would contradict that.
        return ON_EXISTING_SKIP
    if getattr(args, "pull_supplementary", False):
        return ON_EXISTING_SKIP  # the question is already answered
    if args.on_existing == ON_EXISTING_SUPPLEMENT:
        args.pull_supplementary = True
        return ON_EXISTING_SUPPLEMENT
    if args.on_existing == ON_EXISTING_SKIP:
        return ON_EXISTING_SKIP

    tiered = bool(args.prioritize_xml or args.xml_only or args.xml_html_only
                  or args.get_xml_or_html)
    if defers_existing_to_engine(tiered, args.upgrade_existing,
                                 args.get_xml_or_html):
        # The worker skips nothing under these flags -- the engine reasons per
        # goal instead, backfilling the missing half or hunting a better tier.
        # There is no skip to offer an alternative to, and claiming otherwise
        # would misreport what the run is about to do.
        return ON_EXISTING_SKIP

    n_existing, n_total = count_existing_records(
        identifiers, args.output_dir,
        make_subfolder=args.make_subfolder,
        extensions=skip_check_extensions(tiered),
    )
    if not n_existing:
        return ON_EXISTING_SKIP

    if not _stdio_is_interactive():
        _print_yellow_warning(
            f"\u26A0\uFE0F  {n_existing:,} of {n_total:,} records already have "
            f"artifacts in {args.output_dir} and will be skipped. "
            f"Use --on-existing supplement to fetch their supplementary material."
        )
        return ON_EXISTING_SKIP

    choice = _prompt_choice(n_existing, n_total, args.output_dir)
    if choice == ON_EXISTING_SUPPLEMENT:
        args.pull_supplementary = True
    return choice


def _stdio_is_interactive() -> bool:
    """Both ends attached to a terminal, and neither one lying about it.

    isatty is missing entirely on the stdout wrappers this package installs
    (PrefixedOutput implements write and flush and nothing else), and stdin can
    be None under pythonw-style launchers -- so a bare .isatty() call is an
    AttributeError waiting for the one caller who wires it up wrong. Anything
    unexpected here means "not a terminal", which is the safe answer: it keeps
    the run non-interactive.
    """
    import sys

    if _JSON_MODE:
        # stdout is the results channel there and stderr may well be a
        # terminal, so the test below would answer yes and put a question on
        # screen that the calling script cannot see, let alone answer.
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty()
                    and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


#-----------------------------------------------------------------------------------------
def try_download(url, save_path, verbose=False):
    """Try downloading PDF from URL and save to save_path."""

    if not url:
        return False

    # Per-host UA: Springer/OUP answer a browser-like UA on their direct
    # /content/pdf routes with an HTML interstitial, and the real file with
    # anything else. headers_for() swaps only for those hosts.
    request_headers = {**headers_for(url, headers), "Referer": url}

    from requests.exceptions import ConnectionError as _ReqConnErr, Timeout as _ReqTimeout
    try:

        # Retry once on transient connection errors (reset, read timeout).
        # SSL errors keep their existing verify=False fallback path.
        r = None
        for _attempt in range(2):
            try:
                r = requests.get(
                    url,
                    headers=request_headers,
                    timeout=20,
                    allow_redirects=True,
                    stream=True,
                )
                break
            except SSLError as e:
                if verbose:
                    print("SSL verification failed, retrying with verify=False (INSECURE):", e)
                r = requests.get(
                    url,
                    headers=request_headers,
                    timeout=25,
                    allow_redirects=True,
                    stream=True,
                    verify=False,        # last-resort bypass
                )
                break
            except (_ReqConnErr, _ReqTimeout) as e:
                if _attempt == 0:
                    if verbose:
                        print(f"Transient connection error, retrying once: {str(e)[:100]}")
                    time.sleep(1)
                    continue
                raise

        content_type = r.headers.get("content-type", "").lower()
        if r.status_code == 200:
            # Accept application/pdf or application/octet-stream with PDF magic bytes
            # (PsychArchives, DSpace, etc. often use octet-stream)
            if "pdf" in content_type:
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                _note_download_url(save_path, url)
                if verbose:
                    print("PDF downloaded OK from direct URL.")
                return True
            if "octet-stream" in content_type:
                chunks = list(r.iter_content(8192))
                if chunks and chunks[0][:4] == b"%PDF":
                    with open(save_path, "wb") as f:
                        for chunk in chunks:
                            if chunk:
                                f.write(chunk)
                    _note_download_url(save_path, url)
                    if verbose:
                        print("PDF downloaded OK (octet-stream).")
                    return True
            # 200 + HTML on a URL we asked for a PDF is the inverted bot check:
            # the host served an interstitial because the client looked like a
            # browser. Retry once with the polite UA. This generalizes the
            # POLITE_UA_HOSTS allow-list to publishers not yet catalogued, and
            # costs a request only in the case that was already a failure.
            if ("html" in content_type
                    and request_headers.get("User-Agent") != POLITE_USER_AGENT):
                try:
                    r.close()
                except Exception:
                    pass
                retry_headers = {**request_headers,
                                 "User-Agent": POLITE_USER_AGENT}
                try:
                    r2 = requests.get(url, headers=retry_headers, timeout=20,
                                      allow_redirects=True, stream=True)
                    ctype2 = r2.headers.get("content-type", "").lower()
                    if r2.status_code == 200 and ("pdf" in ctype2
                                                  or "octet-stream" in ctype2):
                        chunks = list(r2.iter_content(8192))
                        if chunks and chunks[0][:4] == b"%PDF":
                            with open(save_path, "wb") as f:
                                for chunk in chunks:
                                    if chunk:
                                        f.write(chunk)
                            _note_download_url(save_path, url)
                            if verbose:
                                print("PDF downloaded OK (polite-UA retry after "
                                      "HTML interstitial).")
                            return True
                except Exception as e:
                    if verbose:
                        print(f"Polite-UA retry failed for {url[:60]}: "
                              f"{str(e)[:80]}")
        # Silently fail for non-200 or non-PDF responses (normal during fallback attempts)
    except Exception as e:
        if verbose:
            print(f"Failed downloading from {url[:80]}: {str(e)[:100]}")
    return False


def try_download_with_session(url: str, save_path: str, referer: str = None, verbose=False) -> bool:
    """Download PDF from URL using a session (visits referer first if given). Needed for sites like eScholarship."""
    if not url:
        return False
    try:
        session = requests.Session()
        session.headers.update(headers)
        if referer:
            session.headers["Referer"] = referer
            session.get(referer, headers=HTML_HEADERS, timeout=15)
        r = session.get(url, timeout=20, allow_redirects=True, stream=True)
        content_type = r.headers.get("content-type", "").lower()
        if r.status_code == 200:
            if "pdf" in content_type:
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                _note_download_url(save_path, url)
                if verbose:
                    print("PDF downloaded OK (session).")
                return True
            if "octet-stream" in content_type:
                chunks = list(r.iter_content(8192))
                if chunks and chunks[0][:4] == b"%PDF":
                    with open(save_path, "wb") as f:
                        for chunk in chunks:
                            if chunk:
                                f.write(chunk)
                    _note_download_url(save_path, url)
                    if verbose:
                        print("PDF downloaded OK (session, octet-stream).")
                    return True
    except Exception as e:
        if verbose:
            print(f"Session download failed: {e}")
    return False


def _is_plausible_http_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if any(ch in u for ch in [" ", "{", "}", "<", ">", "\\n", "\\r", "\\t"]):
        return False
    return True


#: Where a candidate URL came from, best first. This is the ranking; `score`
#: only breaks ties inside a band.
#:
#: Provenance, not URL shape, because the shape cannot tell them apart. The
#: page's own `citation_pdf_url` and a PDF cited in its bibliography are both
#: "a URL ending in .pdf", and the old scorer gave both +4.0 -- the article's
#: own copy stayed in front only by stable-sort luck, and lost the moment it
#: was bot-walled. (The old scorer's +3.0 for `citation_pdf_url` never fired at
#: all: it searched for that string *inside the URL*, where it never appears.
#: The signal it was reaching for is ORIGIN_DECLARED, assigned at harvest time
#: while we still know which regex produced the link.)
ORIGIN_DECLARED = 0    # <meta citation_pdf_url|og:pdf> -- the page says this IS its PDF
ORIGIN_DOI_PATH = 1    # the requested DOI's suffix appears in the URL path
ORIGIN_SAME_HOST = 2   # served by the same site as the landing page
ORIGIN_REPOSITORY = 3  # off-host, but an OA repository or a file endpoint
ORIGIN_FOREIGN = 4     # off-host with no reason to think it is ours -- DROPPED

_Candidate = namedtuple("_Candidate", "url origin score")

#: Off-host hosts that legitimately serve a copy of somebody else's article.
#: Not new knowledge -- these were already spelled out twice in this function,
#: in the score bonuses and in the downloadable-keyword list.
_REPOSITORY_HOSTS = (
    "arxiv.org", "escholarship.org", "psycharchives.org", "psycharchives.de",
    "econstor.eu", "zenodo.org", "osf.io", "ndownloader.figshare.com",
    "figshare.com", "europepmc.org", "pmc.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov", "biorxiv.org", "medrxiv.org", "core.ac.uk",
    "ssrn.com", "papers.ssrn.com", "dspace.mit.edu", "hal.science",
)

#: Path shapes that identify a file endpoint rather than a document someone
#: happens to link. These are safe OFF-host because they name a repository's
#: delivery route, not merely a file extension.
_REPOSITORY_PATHS = (
    "/bitstream/", "/bitstreams/", "viewcontent.cgi", "/api/access/datafile",
    "/doi/pdf/", "/doi/pdfdirect/", "pdfdirect", "/content/qt",
    "/article/file/", "/ndownloader/",
)

#: Markers that mean "downloadable" only when the LANDING PAGE ITSELF serves
#: them. This is the split that fixes the reported bug: `/files/` matches
#: dietaryguidelines.gov/sites/default/files/..., and a bare `.pdf` matches
#: eaaci.org/globalatlas/GlobalAtlasAllergy.pdf -- both cited works, both
#: previously kept on the strength of the substring alone.
_SAME_HOST_MARKERS = (
    ".pdf", "download", "full.pdf", "/doi/pdf", "pdfdirect", "/content/",
    "/document/", "/files/", "/article/file/",
)

#: The minimum "this is a file, not a page" signal, required in every band.
_FILE_MARKERS = (
    ".pdf", "/pdf", "download", "pdfdirect", "viewcontent.cgi", "/bitstream",
    "/api/access/datafile", "/content/qt", "/ndownloader/", "/article/file/",
)


def _doi_suffix_in_path(url: str, doi: str) -> bool:
    """Whether the requested DOI's own suffix appears in this URL's path."""
    if not doi or "/" not in doi:
        return False
    suffix = doi.split("/", 1)[1].strip().lower()
    if len(suffix) < 4:
        return False
    return suffix in (urlparse(url).path or "").lower()


def _host_matches(host: str, landing_host: str) -> bool:
    if not host or not landing_host:
        return False
    return (host == landing_host
            or host.endswith("." + landing_host)
            or landing_host.endswith("." + host))


def _candidate_origin(url: str, landing_host: str, doi: str) -> int:
    """Which band this URL belongs to, from where it points rather than how it reads.

    Every band still requires the URL to look like a FILE. Being on a friendly
    host is not enough on its own: a PMC article page carries a few hundred
    same-host and NCBI-wide navigation links, and admitting those would spend
    the eight candidate slots on account settings and bibliography pages before
    reaching anything downloadable.
    """
    host = (urlparse(url).netloc or "").lower()
    lowered = url.lower()
    looks_like_file = any(k in lowered for k in _FILE_MARKERS)

    if looks_like_file and _doi_suffix_in_path(url, doi):
        return ORIGIN_DOI_PATH
    if _host_matches(host, landing_host):
        return ORIGIN_SAME_HOST if any(k in lowered for k in _SAME_HOST_MARKERS) else ORIGIN_FOREIGN
    on_repository_host = any(host == h or host.endswith("." + h) for h in _REPOSITORY_HOSTS)
    if any(k in lowered for k in _REPOSITORY_PATHS):
        return ORIGIN_REPOSITORY
    if on_repository_host and looks_like_file:
        return ORIGIN_REPOSITORY
    return ORIGIN_FOREIGN


def _collect_landing_page_candidates(landing_url: str, html_text: str, doi: str = "",
                                     verbose: bool = False):
    """Likely PDF/download URLs on a landing page, ranked by where they came from.

    ONLY this paper's own material. A PDF the paper CITES belongs to somebody
    else and must not be collected. The bibliography of a research article is a
    list of other people's files, and a regex that keeps every `.pdf` on the
    page keeps all of them.

    Verified on PMC9292464 (10.1111/all.14949): 667 raw links, 108 survivors
    under the old filter, and the third-ranked one was the USDA's *Dietary
    Guidelines for Americans* -- a work the paper cites. It was downloaded and
    saved as the paper, because it was a genuine `application/pdf` and the
    article's own copy, ranked first, was behind a bot wall.

    Returns `_Candidate(url, origin, score)`, best first.
    """
    harvested = []

    # High-signal metadata: the page naming its own PDF. Kept separate from the
    # generic link sweep because after urljoin you can no longer tell a meta tag
    # from an href, and that difference is the entire ranking.
    for m in re.findall(
        r'<meta[^>]+(?:name|property)=["\'](?:citation_pdf_url|og:pdf)["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    ):
        harvested.append((urljoin(landing_url, html.unescape(m.strip())), ORIGIN_DECLARED))

    for m in re.findall(
        r'<meta[^>]+(?:name|property)=["\']dc\.identifier["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    ):
        harvested.append((urljoin(landing_url, html.unescape(m.strip())), None))

    # Common link-bearing attributes.
    for m in re.findall(
        r'(?:href|src|data-href|data-url)=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    ):
        harvested.append((urljoin(landing_url, html.unescape(m.strip())), None))

    # Bare URLs in inline scripts/JSON-LD.
    for m in re.findall(r'https?://[^\s"\'<>]+', html_text, re.IGNORECASE):
        harvested.append((html.unescape(m.strip()), None))

    blocked_domains = (
        "googletagmanager.com",
        "google-analytics.com",
        "doubleclick.net",
        "facebook.net",
        "twitter.com/i/",
        "youtube.com/",
        "vimeo.com/",
    )

    landing_host = (urlparse(landing_url).netloc or "").lower()

    candidates = []
    seen = set()
    dropped_foreign = 0
    for url, forced_origin in harvested:
        if not _is_plausible_http_url(url):
            continue
        lowered = url.lower()
        if any(d in lowered for d in blocked_domains):
            continue
        # Exclude obvious non-document assets.
        if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?|ttf)(\?|$)", lowered):
            continue
        if url in seen:
            continue
        seen.add(url)

        origin = forced_origin
        if origin is None:
            origin = _candidate_origin(url, landing_host, doi)
        if origin == ORIGIN_FOREIGN:
            dropped_foreign += 1
            continue
        candidates.append(_Candidate(url, origin, _score(url)))

    if verbose and dropped_foreign:
        print(f"  Landing-page links dropped as somebody else's "
              f"(off-host, no repository signature): {dropped_foreign}")

    candidates.sort(key=lambda c: (c.origin, -c.score))
    return candidates


def _score(url: str) -> float:
    """Tie-break within an origin band. Never decides between bands."""
    u = url.lower()
    s = 0.0
    if ".pdf" in u:
        s += 4.0
    if any(k in u for k in ["/doi/pdf", "pdfdirect", "/download", "download=",
                            "/bitstream/", "/content/", "viewcontent.cgi"]):
        s += 2.0
    if any(k in u for k in ["tandfonline.com", "sagepub.com", "wiley.com",
                            "jneurosci.org", "direct.mit.edu", "econstor.eu",
                            "canterbury.ac.nz"]):
        s += 0.8
    return s


#: How many landing-page candidates to actually try, and how many of those get a
#: second attempt through a cookie-bearing session.
#:
#: Was 40 candidates x 2 attempts = up to 80 requests at 20s timeouts, per call,
#: from 9 call sites -- one of them inside a loop over Crossref chooser links. That
#: is the single most expensive helper in the chain, and across 18,487 attributed
#: artifacts the whole landing-page route produced 30 hits (0.16%). Candidates are
#: ordered high-signal first, so the deep tail was paying full timeout price for
#: URLs that were never going to work.
_LANDING_MAX_CANDIDATES = 8
_LANDING_SESSION_RETRIES = 2


@_timed("landing_page")
def try_landing_page_pdf_fallback(doi: str, landing_url: str, save_path: str, verbose=False) -> bool:
    """Try publisher/repository-specific and scraped links from a landing page."""
    if not landing_url or not _is_plausible_http_url(landing_url):
        return False

    try:
        landing_resp = requests.get(
            landing_url, headers=HTML_HEADERS, timeout=25, allow_redirects=True
        )
    except Exception as e:
        if verbose:
            print(f"  Landing page request failed: {str(e)[:120]}")
        return False

    # 406 is content-negotiation, not a block. Retry once without Sec-Fetch /
    # Accept-Encoding; some CDNs 406 a Chrome UA that claims br.
    if landing_resp.status_code == 406:
        try:
            landing_resp = requests.get(
                landing_url, headers=_HTML_HEADERS_MINIMAL, timeout=25,
                allow_redirects=True,
            )
        except Exception as e:
            if verbose:
                print(f"  Landing page 406-retry failed: {str(e)[:120]}")
            return False

    # Landing already resolved to a PDF.
    if landing_resp.status_code == 200 and "pdf" in (landing_resp.headers.get("content-type", "").lower()):
        with open(save_path, "wb") as f:
            f.write(landing_resp.content)
        if verbose:
            print(f"✅ Landing page resolved directly to PDF for {doi}")
        return True

    if landing_resp.status_code != 200:
        # Was a bare `return False`. A Cloudflare 403 and a genuinely empty page
        # then produced the same "candidate URLs: 0" line, which is why every
        # Elsevier record in a 46-DOI run looked identical to a page with no
        # links on it. Name the status, and name a block as a block.
        blocked = landing_resp.status_code in (401, 403, 429) or (
            landing_resp.status_code == 503
        )
        if verbose:
            why = "blocked by publisher/CDN" if blocked else "unavailable"
            print(f"  Landing page {why} (HTTP {landing_resp.status_code}): "
                  f"{(landing_resp.url or landing_url)[:100]}")
        return False

    final_landing = landing_resp.url or landing_url
    html_text = landing_resp.text or ""

    # Deterministic publisher URL patterns.
    host = (urlparse(final_landing).netloc or "").lower()
    deterministic = []
    if "tandfonline.com" in host:
        deterministic.extend([
            f"https://www.tandfonline.com/doi/pdf/{doi}",
            f"https://www.tandfonline.com/doi/pdf/{doi}?download=true",
        ])
    if "sagepub.com" in host:
        deterministic.extend([
            f"https://journals.sagepub.com/doi/pdf/{doi}",
            f"https://journals.sagepub.com/doi/pdf/{doi}?download=true",
        ])
    if "wiley.com" in host:
        deterministic.extend([
            f"https://onlinelibrary.wiley.com/doi/pdf/{doi}",
            f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
        ])
    if "emerald.com" in host or doi.lower().startswith("10.1108/"):
        doi_upper = doi.upper()
        deterministic.extend([
            f"https://www.emerald.com/insight/content/doi/{doi_upper}/full/pdf",
            f"https://www.emerald.com/insight/content/doi/{doi}/full/pdf",
        ])
    if "jneurosci.org" in host:
        suffix = doi.split("/", 1)[1].upper() if "/" in doi else doi.upper()
        deterministic.append(f"https://www.jneurosci.org/content/{suffix}.full.pdf")
    if "direct.mit.edu" in host or doi.lower().startswith("10.1162/"):
        mit_slug = doi.lower().replace("/imag.a.", "/imag_a_").replace("/imag.", "/imag_")
        deterministic.append(f"https://direct.mit.edu/doi/pdf/{mit_slug}")
    # Acta Biochimica Polonica moved to Frontiers Partnerships; doi.org can 404 but PDF lives at journal path
    if "frontierspartnerships.org" in host or doi.lower().startswith("10.18388/abp"):
        deterministic.append(
            f"https://www.frontierspartnerships.org/journals/acta-biochimica-polonica/articles/{doi}/pdf"
        )

    # Scraped landing-page candidates.
    scraped = _collect_landing_page_candidates(final_landing, html_text, doi=doi,
                                               verbose=verbose)
    scraped_urls = [c.url for c in scraped]
    candidate_urls = deterministic + [u for u in scraped_urls if u not in deterministic]

    # Not every DOI has a paper behind it. A magazine news item is a complete,
    # correct result with no full text to fetch, and reporting it as "Nothing
    # worked" sends a reader hunting for a bug that isn't there. Warned
    # unconditionally rather than under --verbose: this changes what the
    # failure MEANS, so it should not be something a run can miss.
    # Verified on 10.1038/d41586-022-01305-x -- page fetches 200/260 KB and
    # contains no citation_pdf_url at all.
    # Note the check is on the PAGE, not on candidate count: a news item can
    # still yield 100+ same-host nav links (Nature's d41586 pages yield 106),
    # none of which is a PDF. Zero candidates was the wrong signal.
    if _looks_like_no_fulltext_item(html_text) and _note_news_item(doi):
        _print_yellow_warning(
            f"⚠️  {doi} is a news/editorial item, not a research paper -- "
            f"no full text is published for it. Nothing to download; this is "
            f"not a retrieval failure."
        )
    if verbose:
        print(
            f"  Landing-page candidate URLs: {len(candidate_urls)}"
            f" (trying {min(len(candidate_urls), _LANDING_MAX_CANDIDATES)})"
        )

    for index, candidate in enumerate(candidate_urls[:_LANDING_MAX_CANDIDATES]):
        if _looks_like_preview_url(candidate):
            if verbose:
                print(f"  Skipping preview endpoint: {candidate[:100]}")
            continue
        if _already_rejected(doi, candidate):
            if verbose:
                print(f"  Skipping URL already refused for this record: {candidate[:90]}")
            continue
        if try_download(candidate, save_path, verbose):
            # A valid PDF is not necessarily THIS paper's PDF. Before this
            # check, the first candidate that returned application/pdf ended
            # the search -- and on a page whose own PDF is bot-walled, that is
            # a document from the reference list.
            if _accept_downloaded_pdf(save_path, doi, candidate, verbose):
                if verbose:
                    print(f"✅ Landing-page extracted PDF success for {doi}")
                return True
        # The session retry costs a second full timeout on the same URL, so it is
        # capped far tighter than the candidate list. _collect_landing_page_candidates
        # puts the high-signal sources first (citation_pdf_url, og:pdf), and those
        # are the only ones where visiting a referer first has ever mattered.
        if index < _LANDING_SESSION_RETRIES and try_download_with_session(
            candidate, save_path, referer=final_landing, verbose=verbose
        ):
            if _accept_downloaded_pdf(save_path, doi, candidate, verbose):
                if verbose:
                    print(f"✅ Landing-page session PDF success for {doi}")
                return True

    return False


#-----------------------------------------------------------------------------------------
@_timed("escholarship")
def try_escholarship_via_pubmed(doi: str, save_path: str, verbose=False) -> bool:
    """
    Find eScholarship PDF via PubMed/Europe PMC LinkOut.
    UC and other universities deposit in eScholarship; PubMed abstract pages list these.
    eScholarship requires visiting the item page first (session/referer) to get the PDF.
    """
    # This fallback depends entirely on scraping the PubMed website; if that's been
    # disabled (repeated timeouts) there's nothing to do — skip the Europe PMC call too.
    if _PUBMED_WEB_DISABLED:
        return False
    try:
        # Get PMID from Europe PMC search by DOI
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json",
            timeout=20,
        )
        if r.status_code != 200:
            return False
        hits = (r.json().get("resultList") or {}).get("result") or []
        pmid = None
        for h in hits:
            if h.get("pmid"):
                pmid = h["pmid"]
                break
        if not pmid:
            return False

        # Fetch PubMed abstract page for LinkOut full-text links (eScholarship etc.)
        # Europe PMC abstract often lacks LinkOut; PubMed has them
        page_text = _get_pubmed_web_html(pmid, verbose=verbose)
        if not page_text:
            return False

        # Find eScholarship item links
        escholarship_items = re.findall(
            r'https?://(?:www\.)?escholarship\.org/uc/item/([a-zA-Z0-9]+)',
            page_text,
            re.IGNORECASE,
        )
        if not escholarship_items:
            return False

        for item_id in escholarship_items[:3]:  # try up to 3
            item_url = f"https://escholarship.org/uc/item/{item_id}"
            # eScholarship PDF URL pattern: /content/qt{item_id}/{item_id}.pdf
            pdf_url = f"https://escholarship.org/content/qt{item_id}/qt{item_id}.pdf"
            if try_download_with_session(pdf_url, save_path, referer=item_url, verbose=verbose):
                if verbose:
                    print(f"✅ eScholarship (PubMed LinkOut) success for {doi}")
                return True
    except Exception as e:
        if verbose:
            print(f"Error with eScholarship/PubMed: {e}")
    return False


@_timed("pmid_direct")
def try_pmid_direct_pdf_fallback(pmid: str, save_path: str, verbose=False, allow_xml=True) -> bool:
    """
    Try PMID-native PDF retrieval when DOI is unavailable.
    Uses PMCID routes + Europe PMC full-text links.
    """
    if not pmid:
        return False

    pmcid = None
    try:
        r = _get_with_retries(
            _ncbi_url(f"{IDCONV_URL}?ids={pmid}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            for rec in (r.json() or {}).get("records", []) or []:
                c = rec.get("pmcid")
                if c:
                    pmcid = c.upper()
                    if not pmcid.startswith("PMC"):
                        pmcid = f"PMC{pmcid}"
                    break
    except Exception as e:
        if verbose:
            print(f"  PMID native idconv lookup failed for {pmid}: {str(e)[:100]}")

    # PMCID-derived direct PDF endpoints.
    if pmcid:
        pmc_candidates = [
            f"https://europepmc.org/articles/{pmcid}?pdf=render",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/?pdf=1",
        ]
        for url in pmc_candidates:
            if try_download(url, save_path, verbose):
                if verbose:
                    print(f"✅ PMID native PMCID route success for PMID {pmid}")
                return True
        if allow_xml:
            xml_got = try_pmc_xml_fallback(pmcid, save_path, verbose=verbose, doi=f"pmid:{pmid}")
            if xml_got:
                return xml_got

    # Europe PMC record full-text URLs.
    try:
        query = f"EXT_ID:{pmid}%20AND%20SRC:MED"
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={query}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = (r.json().get("resultList", {}) or {}).get("result", []) or []
            for result in results:
                for u in (result.get("fullTextUrlList", {}) or {}).get("fullTextUrl", []) or []:
                    url = (u.get("url") or "").strip()
                    if not url:
                        continue
                    if try_download(url, save_path, verbose):
                        if verbose:
                            print(f"✅ PMID native Europe PMC URL success for PMID {pmid}")
                        return True
                    if try_landing_page_pdf_fallback(f"pmid:{pmid}", url, save_path, verbose):
                        if verbose:
                            print(f"✅ PMID native Europe PMC landing success for PMID {pmid}")
                        return True
    except Exception as e:
        if verbose:
            print(f"  PMID native Europe PMC lookup failed for {pmid}: {str(e)[:100]}")

    # PubMed provider links / citation_pdf_url fallback.
    try:
        html_text = _get_pubmed_web_html(pmid, verbose=verbose)
        if html_text:
            candidates = []

            # Direct PDF meta tag when available.
            for m in re.findall(
                r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
                re.IGNORECASE,
            ):
                candidates.append(html.unescape(m.strip()))

            # Outbound provider links shown on PubMed page.
            for m in re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text, re.IGNORECASE):
                u = html.unescape(m.strip())
                ul = u.lower()
                if any(k in ul for k in [".pdf", "pdf", "/article/", "fulltext", "/doi/", "springer", "wiley", "tandfonline", "sagepub", "sciencedirect", "nature.com"]):
                    candidates.append(u)

            # Deduplicate while preserving order.
            deduped = []
            seen = set()
            for c in candidates:
                if c and c not in seen:
                    seen.add(c)
                    deduped.append(c)

            if verbose:
                print(f"  PMID native PubMed provider candidates: {len(deduped)}")

            for url in deduped[:20]:
                if try_download(url, save_path, verbose):
                    if verbose:
                        print(f"✅ PMID native PubMed direct URL success for PMID {pmid}")
                    return True
                if try_landing_page_pdf_fallback(f"pmid:{pmid}", url, save_path, verbose):
                    if verbose:
                        print(f"✅ PMID native PubMed landing success for PMID {pmid}")
                    return True
    except Exception as e:
        if verbose:
            print(f"  PMID native PubMed provider lookup failed for {pmid}: {str(e)[:100]}")

    return False


def _extract_elsevier_pii_from_crossref(crossref_message: dict):
    """Extract Elsevier PII from Crossref links."""
    for link in (crossref_message or {}).get("link", []) or []:
        url = link.get("URL") or ""
        m = re.search(r"PII:([A-Z0-9]+)", url, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _print_yellow_warning(message: str):
    """Print warning in yellow text for terminal users.

    Plain text under --json when stderr is redirected to a file or a pipe:
    those warnings are the caller's log, and escape codes in a log are noise a
    reader has to strip. The check is only made in that mode, because the
    stdout wrapper the parallel batch installs implements write and flush and
    nothing else -- asking it whether it is a terminal is an AttributeError.
    """
    if _JSON_MODE and not (hasattr(sys.stderr, "isatty") and sys.stderr.isatty()):
        print(message)
        return
    print(f"\033[93m{message}\033[0m")


def _xml_path_for_pdf_path(save_path: str) -> str:
    base, _ = os.path.splitext(save_path)
    return f"{base}.xml"


_ELIFE_DOI_RE = re.compile(r"^10\.7554/elife\.(\d+)$", re.IGNORECASE)
_XML_TDM_TYPES = {"application/xml", "text/xml"}
EPMC_FULLTEXT_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
EFETCH_PMC_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _elife_article_id(doi: str):
    """Numeric eLife article id, or None if this is not an eLife DOI."""
    if not doi:
        return None
    m = _ELIFE_DOI_RE.match(str(doi).strip())
    return m.group(1) if m else None


def _elife_xml_urls(doi: str):
    """CDN XML URLs for an eLife DOI. v1 first; later versions are fallbacks.

    elifesciences.org HTML 406s a PDF-first Accept (and, from some IPs, any
    Chrome UA). The CDN does not: Crossref already publishes
    cdn.elifesciences.org/articles/{id}/elife-{id}-v1.xml as a TDM link.
    """
    article_id = _elife_article_id(doi)
    if not article_id:
        return []
    return [
        f"https://cdn.elifesciences.org/articles/{article_id}/elife-{article_id}-v{n}.xml"
        for n in range(1, 5)
    ]


def _looks_like_jats_fulltext(content) -> bool:
    """True when bytes look like JATS with a body, not an EPMC error bean or stub."""
    if not content:
        return False
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if len(content) < 200:
        return False
    head = content.lstrip()[:200].lower()
    if not (head.startswith(b"<?xml") or head.startswith(b"<")):
        return False
    lowered = content.lower()
    if b"<errorbean" in lowered[:4000] or b"<error>" in lowered[:2000]:
        if b"<article" not in lowered[:8000]:
            return False
    return b"<body" in lowered


def _try_save_jats_xml(url, save_path, verbose=False, doi=None):
    """Fetch URL and write {stem}.xml if it is JATS full text. Returns that path or None."""
    if not url:
        return None
    try:
        r = _get_with_retries(url, timeout=30, retries=2)
    except Exception as e:
        if verbose:
            print(f"  JATS XML fetch failed: {str(e)[:120]}")
        return None
    if getattr(r, "status_code", 0) != 200:
        return None
    content = getattr(r, "content", None) or b""
    if not _looks_like_jats_fulltext(content):
        if verbose:
            print(f"  JATS XML at {str(url)[:80]} is not full text")
        return None
    xml_path = _xml_path_for_pdf_path(save_path)
    try:
        with open(xml_path, "wb") as f:
            f.write(content)
    except OSError as e:
        if verbose:
            print(f"  could not write JATS XML: {e}")
        return None
    label = doi or url
    _print_yellow_warning(
        f"WARNING: Saved full-text XML (not PDF) for {label} -> {xml_path}"
    )
    if verbose:
        print(f"✅ JATS XML fallback success for {label}")
    return xml_path


def _crossref_tdm_xml_urls(message):
    """Crossref text-mining XML links. similarity-checking URLs are landing pages."""
    urls = []
    for link in (message or {}).get("link") or []:
        if not isinstance(link, dict):
            continue
        if (link.get("intended-application") or "").lower() != "text-mining":
            continue
        ctype = (link.get("content-type") or "").lower().split(";")[0].strip()
        if ctype not in _XML_TDM_TYPES:
            continue
        url = link.get("URL")
        if url:
            urls.append(url)
    return urls


#: How many OA copies to actually GET. best_oa_location is often a publisher
#: landing with no PDF while oa_locations[] still has a repository file.
_OA_DIRECT_MAX = 5
_OA_LANDING_MAX = 2

_UNPAYWALL_VERSION_RANK = {
    "publishedversion": 0, "acceptedversion": 1, "submittedversion": 2,
}

_CROSSREF_PREPRINT_RELS = (
    "has-preprint", "is-preprint-of", "has-version", "is-version-of",
)


def _dedupe_keep_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _dedupe_url_pairs(pairs):
    """Keep the first (highest-ranked) occurrence of each URL."""
    seen = set()
    out = []
    for url, is_direct in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((url, is_direct))
    return out


def _collect_unpaywall_pdf_urls(data):
    """(url, is_direct) from every Unpaywall OA copy, repository PDF first.

    is_direct True means url_for_pdf or a URL that already looks like a file.
    Publisher landings are last and the caller caps how many of those it GETs.
    """
    locs = [loc for loc in (data.get("oa_locations") or []) if isinstance(loc, dict)]
    best = data.get("best_oa_location")
    if isinstance(best, dict) and best not in locs:
        locs = [best] + locs
    scored = []
    for loc in locs:
        pdf = loc.get("url_for_pdf")
        url = loc.get("url")
        host_rank = 0 if (loc.get("host_type") or "").lower() == "repository" else 1
        ver_rank = _UNPAYWALL_VERSION_RANK.get((loc.get("version") or "").lower(), 3)
        if pdf:
            scored.append((0, host_rank, ver_rank, pdf, True))
        if url and url != pdf:
            looks_file = ".pdf" in url.lower() or "/pdf" in url.lower()
            scored.append((0 if looks_file else 2, host_rank, ver_rank, url, looks_file))
    scored.sort()
    return _dedupe_url_pairs([(url, is_direct) for *_, url, is_direct in scored])


def _collect_openalex_pdf_urls(data):
    """(url, is_direct) from OpenAlex locations[], not only best_oa_location."""
    locs = [loc for loc in (data.get("locations") or []) if isinstance(loc, dict)]
    for extra_key in ("best_oa_location", "primary_location"):
        extra = data.get(extra_key)
        if isinstance(extra, dict) and extra not in locs:
            locs.insert(0, extra)
    scored = []
    for loc in locs:
        # Closed locations have no file we are entitled to; skip them.
        if loc.get("is_oa") is False:
            continue
        pdf = loc.get("pdf_url")
        landing = loc.get("landing_page_url")
        src = loc.get("source") or {}
        host_rank = 0 if (src.get("type") or "").lower() == "repository" else 1
        if pdf:
            scored.append((0, host_rank, pdf, True))
        if landing and landing != pdf:
            looks_file = ".pdf" in landing.lower() or "/pdf" in landing.lower()
            scored.append((0 if looks_file else 2, host_rank, landing, looks_file))
    scored.sort()
    return _dedupe_url_pairs([(url, is_direct) for *_, url, is_direct in scored])


_OSF_ITEM_RE = re.compile(r"osf\.io/([a-z0-9]{5})(?:[/?#]|$)", re.IGNORECASE)


def _try_oa_location_urls(doi, candidates, save_path, verbose=False) -> bool:
    """Download from ranked OA copies. Direct PDFs first; a few landings after."""
    direct_left = _OA_DIRECT_MAX
    landing_left = _OA_LANDING_MAX
    for url, is_direct in candidates:
        if not url:
            continue
        # An OSF item is a project, not a file: scraping its page yields nothing
        # because the file list is rendered client-side. Unpaywall lists these
        # as the only OA copy for green-OA psychology records, so without this
        # the whole location is wasted.
        osf_match = _OSF_ITEM_RE.search(url)
        if osf_match and _download_pdf_from_osf_api(
            osf_match.group(1), save_path, verbose, doi=doi
        ):
            return True
        if is_direct and direct_left > 0 and not _already_rejected(doi, url):
            direct_left -= 1
            if try_download(url, save_path, verbose) and _accept_downloaded_pdf(
                save_path, doi, url, verbose
            ):
                return True
        if landing_left > 0:
            landing_left -= 1
            if try_landing_page_pdf_fallback(doi, url, save_path, verbose):
                return True
    return False


def _crossref_related_dois(message, self_doi):
    """Preprint/version DOIs from Crossref relation, excluding this record."""
    rels = (message or {}).get("relation") or {}
    found = []
    self_doi = (self_doi or "").lower().strip()
    for key in _CROSSREF_PREPRINT_RELS:
        for item in rels.get(key) or []:
            if not isinstance(item, dict):
                continue
            ident = (item.get("id") or "").strip()
            id_type = (item.get("id-type") or "").lower()
            if ident.lower().startswith("https://doi.org/"):
                ident = ident.split("doi.org/", 1)[-1]
            if not ident.lower().startswith("10."):
                continue
            if id_type and id_type != "doi":
                continue
            if ident.lower() == self_doi:
                continue
            found.append(ident)
    return _dedupe_keep_order(found)[:2]


def try_pmc_xml_fallback(pmcid, save_path, verbose=False, doi=None):
    """Europe PMC fullTextXML for a PMCID. PDF-render 404s on XML-only OA."""
    if not pmcid:
        return None
    pmcid = str(pmcid).strip().upper()
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    return _try_save_jats_xml(
        EPMC_FULLTEXT_XML.format(pmcid=pmcid),
        save_path, verbose=verbose, doi=doi,
    )


def try_pmc_efetch_xml_fallback(pmcid, save_path, verbose=False, doi=None):
    """NCBI efetch JATS for a PMCID. Serves records Europe PMC's OA route will not."""
    if not pmcid:
        return None
    numeric = str(pmcid).strip().upper().replace("PMC", "")
    if not numeric.isdigit():
        return None
    return _try_save_jats_xml(
        _ncbi_url(f"{EFETCH_PMC_URL}?db=pmc&id={numeric}&retmode=xml"),
        save_path, verbose=verbose, doi=doi,
    )


def try_elife_xml_fallback(doi: str, save_path: str, verbose=False):
    """eLife CDN / API XML. The HTML site 406s; this does not need it."""
    article_id = _elife_article_id(doi)
    if not article_id:
        return None
    urls = []
    try:
        r = _get_with_retries(
            f"https://api.elifesciences.org/articles/{article_id}",
            timeout=20, retries=2,
        )
        if r.status_code == 200:
            xml_url = (r.json() or {}).get("xml")
            if xml_url:
                urls.append(xml_url)
    except Exception as e:
        if verbose:
            print(f"  eLife API: {str(e)[:100]}")
    for url in _elife_xml_urls(doi):
        if url not in urls:
            urls.append(url)
    for url in urls:
        got = _try_save_jats_xml(url, save_path, verbose=verbose, doi=doi)
        if got:
            return got
    return None


def _safe_page_content(page, timeout_ms: int = 5000) -> str:
    """Get page.content() while tolerating in-flight Playwright navigations.

    Avoids the intermittent error: "Page.content: Unable to retrieve content
    because the page is navigating and changing the content."
    Waits for DOM to be loaded, then retries once after a short sleep if the
    first attempt fails.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        return page.content()
    except Exception:
        try:
            page.wait_for_timeout(1000)
            return page.content()
        except Exception:
            return ""


def _try_elsevier_pdf_by_pii(pii: str, save_path: str, api_key: str, verbose=False):
    """Elsevier full-text PDF by PII. Returns the saved path, or None.

    Same endpoint as the XML fallback below, asked with a different Accept.
    A non-entitled request answers 200 with an XML error payload, so the %PDF
    magic bytes -- not the status code -- decide whether this worked.
    """
    url = f"https://api.elsevier.com/content/article/PII:{pii}"
    try:
        r = requests.get(
            url,
            headers={"X-ELS-APIKey": api_key,
                     "Accept": "application/pdf",
                     "User-Agent": headers["User-Agent"]},
            timeout=90,
        )
    except Exception as e:
        if verbose:
            print(f"  Elsevier PDF request failed for {pii}: {str(e)[:100]}")
        return None

    if r.status_code != 200:
        if verbose:
            print(f"  Elsevier PDF status {r.status_code} for {pii}")
        return None
    content = getattr(r, "content", None)
    if not content or not content.startswith(b"%PDF"):
        if verbose:
            print(f"  Elsevier PDF: 200 but not a PDF for {pii} (not entitled)")
        return None
    # Entitlement can also be PARTIAL: a key without PDF rights gets a genuine
    # typeset first page -- 200, %PDF magic, plausible size -- and only the
    # X-ELS-Status warning header says so. Printed unconditionally because the
    # visible outcome changes: no PDF appears where "success" used to.
    if elsevier_first_page_only(getattr(r, "headers", None)):
        print(f"  Elsevier PDF: first-page preview only for {pii} "
              f"(key not entitled to this record's PDF); discarding")
        return None

    try:
        with open(save_path, "wb") as f:
            f.write(content)
    except OSError as e:
        print(f"  could not write Elsevier PDF: {e}")
        return None
    print(f"✅ Elsevier PDF success for PII {pii} ({len(content)} bytes)")
    return save_path


@_timed("elsevier")
def try_elsevier_fulltext_api_fallback(doi: str, save_path: str, crossref_message=None,
                                       verbose=False, allow_xml=True):
    """Elsevier Full-Text Retrieval API: PDF if entitled, else raw XML.

    Helpful when ScienceDirect PDF endpoints are bot-blocked. `allow_xml=False`
    (from --no-xml-fallback) suppresses only the XML half -- the PDF attempt
    still runs, because a caller that wants PDFs has no reason to skip a route
    that returns one.
    """
    api_key = _ELSEVIER_TDM_API_KEY
    if not api_key:
        return None

    message = crossref_message or {}
    if not message:
        crossref_params = {}
        if _DEFAULT_EMAIL:
            crossref_params["mailto"] = _DEFAULT_EMAIL
        r = _crossref_raw_get(
            f"https://api.crossref.org/works/{doi}",
            params=crossref_params,
            timeout=12
        )
        if r.status_code != 200:
            return None
        message = (r.json() or {}).get("message", {})

    pii = _extract_elsevier_pii_from_crossref(message)
    if not pii:
        return None

    # Entitlement at Elsevier is negotiated per REPRESENTATION, so the two views
    # of one record can disagree: the XML is often coredata-only metadata (no
    # <body>) or a 400-char corrigendum notice while `Accept: application/pdf`
    # on the SAME PII returns the full typeset article. Verified on four
    # records the XML route rejected -- all four served real PDFs, 237-446 KB.
    #
    # Order: XML first, because it carries table structure that a PDF does not,
    # and that is what most downstream ingestion wants. But take BOTH when both
    # exist -- XML for tables, PDF for figures and the rendered page. Only when
    # the XML turns out to be a stub does the PDF become the primary artifact.
    #
    # Mirrors the elsevier_pdf source in the tiered ladder; duplicated rather
    # than shared because the two paths have no common plumbing (bare requests
    # here, HttpClient there).
    if not allow_xml:
        # --no-xml-fallback: caller ingests PDFs and cannot read .xml, so the
        # PDF is the only acceptable answer from this route.
        if verbose:
            print("  Elsevier: XML suppressed (--no-xml-fallback), trying PDF")
        return _try_elsevier_pdf_by_pii(pii, save_path, api_key, verbose=verbose)

    api_url = f"https://api.elsevier.com/content/article/PII:{pii}?httpAccept=text/xml&apiKey={api_key}"
    try:
        r = requests.get(api_url, timeout=45, headers={"User-Agent": headers["User-Agent"]})
        if r.status_code != 200:
            if verbose:
                print(f"  Elsevier API status {r.status_code} for {pii}")
            return None

        xml_text = r.text
        if len(xml_text.strip()) < 100:
            return None

        # Reject anything that is not actually structured full text. For non-OA
        # articles the Elsevier TDM API returns coredata with no article body, which
        # is useless to save as a "success".
        #
        # Two markers used to be on this list and should never have been:
        #
        #   <ce:abstract>  -- an abstract is not full text, so an abstract-only
        #                     response was written out as a full-text .xml success.
        #   <xocs:rawtext> -- the article as ONE FLAT STRING. No sections, no
        #                     <table>. Confirmed on 10.1016/s0924-977x(00)80463-0:
        #                     accepted here, zero tables in the file. That is plain
        #                     text wearing an .xml extension, and for a pipeline
        #                     whose whole purpose is reading tables it is the single
        #                     worst thing to file as a win -- every number present,
        #                     none attached to a row or column.
        #
        # Only real body markup counts now. The tiered path reaches the same verdict
        # by classifying rawtext as T6 and refusing it for extraction.
        lowered = xml_text.lower()
        has_body = any(marker in lowered for marker in ("<ja:body", "<ce:para", "<ce:sections"))
        if not has_body:
            if verbose:
                reason = ("flat rawtext only, no structure"
                          if "<xocs:rawtext" in lowered
                          else "abstract only" if "<ce:abstract" in lowered
                          else "metadata only")
                print(f"  Elsevier API returned {reason} (no full text) for {pii}; "
                      f"trying PDF representation")
            # The XML is a stub, but entitlement is per-representation -- the
            # PDF of this same PII is very often the complete article. This is
            # the case that used to end the record as "Nothing worked".
            return _try_elsevier_pdf_by_pii(pii, save_path, api_key, verbose=verbose)

        xml_path = _xml_path_for_pdf_path(save_path)
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml_text)

        # Both slots, not one. XML carries the table structure; the PDF carries
        # the figures and the rendered page, and the supplementary/figure
        # tooling downstream reads the PDF. Having taken the XML, spend the one
        # extra call to fill the PDF slot too rather than leaving the record
        # half-collected.
        # Guarded separately: the XML is already written and is a real result,
        # so nothing that happens while fetching the companion may take it
        # away. (Without this, any error here fell into the outer handler and
        # the whole call returned None, discarding the saved XML.)
        try:
            companion = _try_elsevier_pdf_by_pii(
                pii, save_path, api_key, verbose=verbose
            )
        except Exception as e:
            companion = None
            if verbose:
                print(f"  Elsevier PDF companion failed: {str(e)[:100]}")
        if companion:
            if verbose:
                print(f"✅ Elsevier XML+PDF for {doi}")
            # Return the XML: it is the better artifact for ingestion, and the
            # PDF is already on disk at save_path for whatever wants it.
            return xml_path

        _print_yellow_warning(
            f"WARNING: Saved Elsevier full-text XML (not PDF) for {doi} -> {xml_path}"
        )
        if verbose:
            print(f"✅ Elsevier XML fallback success for {doi}")
        return xml_path
    except Exception as e:
        if verbose:
            print(f"Error with Elsevier API fallback: {e}")
    return None


#-----------------------------------------------------------------------------------------
#: How far into an OSF project's folder tree to look, and how many PDFs to try.
#: A real project is organised by submission round -- "PCIRR Stage 1",
#: "PCIRR Stage 2 Endorsed manuscript", "POC print" -- so every PDF that matters
#: is at least one folder down, and a top-level-only scan finds nothing at all.
_OSF_MAX_DEPTH = 4
_OSF_MAX_FILES = 25


def _osf_pdf_files(files_url: str, verbose=False):
    """Every PDF in an OSF file tree, breadth-first, as (path, download_url)."""
    found = []
    queue = [(files_url, "")]
    seen = set()
    depth = 0
    while queue and depth <= _OSF_MAX_DEPTH and len(found) < _OSF_MAX_FILES:
        next_level = []
        for url, prefix in queue:
            if url in seen:
                continue
            seen.add(url)
            try:
                r = requests.get(url, timeout=25)
                if r.status_code != 200:
                    continue
                items = (r.json() or {}).get("data") or []
            except Exception as e:
                if verbose:
                    print(f"    OSF listing failed: {str(e)[:80]}")
                continue
            for item in items:
                attrs = item.get("attributes") or {}
                name = attrs.get("name") or ""
                rel = (item.get("relationships", {}).get("files", {})
                       .get("links", {}).get("related", {}).get("href"))
                if attrs.get("kind") == "folder":
                    if rel:
                        next_level.append((rel, prefix + "/" + name))
                elif name.lower().endswith(".pdf"):
                    dl = (item.get("links") or {}).get("download")
                    if dl:
                        found.append((prefix + "/" + name, dl))
                        if len(found) >= _OSF_MAX_FILES:
                            break
        queue = next_level
        depth += 1
    return found


def _download_pdf_from_osf_api(osf_id: str, save_path: str, verbose=False,
                               doi: str = "") -> bool:
    """The article's own PDF from an OSF project, registration or preprint.

    A project is not a file, so there is rarely one obvious candidate: the node
    behind 10.1037/cns0000401 holds fourteen PDFs -- six preprint revisions,
    three "Draftable Comparison Export" diffs, two replies to decision letters,
    a cover letter, an endorsed manuscript and the published print. Picking the
    first one found is a coin flip, and the first one found there is a draft.

    So every candidate is offered to the identity check and the first that
    passes wins. That check is what makes looking this hard safe: without it,
    walking a stranger's folder tree for "a PDF" is precisely how the wrong file
    ends up filed as the paper.
    """
    try:
        g = requests.get(f"https://api.osf.io/v2/guids/{osf_id}/", timeout=20)
        if g.status_code != 200:
            return False
        gtype = ((g.json() or {}).get("data") or {}).get("type")
        if gtype not in ("registrations", "nodes", "preprints"):
            return False
        files_url = f"https://api.osf.io/v2/{gtype}/{osf_id}/files/"

        candidates = _osf_pdf_files(files_url, verbose=verbose)
        if verbose:
            print(f"    OSF {osf_id}: {len(candidates)} PDF(s) in the file tree")
        for path, dl_url in candidates:
            if _already_rejected(doi, dl_url):
                continue
            if not try_download(dl_url, save_path, verbose):
                continue
            if doi and not _accept_downloaded_pdf(save_path, doi, dl_url, verbose):
                continue
            if verbose:
                print(f"✅ OSF API file download success for {osf_id}: {path[-70:]}")
            return True
    except Exception as e:
        if verbose:
            print(f"  OSF API file fallback error: {str(e)[:120]}")
    return False


def _render_osf_page_to_pdf(osf_id: str, save_path: str, verbose=False, page=None) -> bool:
    """
    Render OSF registration/project page to PDF.
    Useful when registration has metadata but no uploaded PDF files.
    """
    try:
        # Reuse existing page when called from OSF Playwright branch.
        if page is None:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                context = browser.new_context(user_agent=headers["User-Agent"])
                local_page = context.new_page()
                local_page.set_default_timeout(60000)
                resp = local_page.goto(f"https://osf.io/{osf_id}/", wait_until="domcontentloaded")
                local_page.wait_for_timeout(5000)
                if not resp or resp.status >= 400:
                    _quiet_close(browser)
                    return False
                body_text = (local_page.inner_text("body") or "").lower()
                if "page not found" in body_text or "unable to resolve your request" in body_text:
                    _quiet_close(browser)
                    return False
                local_page.pdf(path=save_path, format="A4", print_background=True)
                _quiet_close(browser)
        else:
            body_text = (page.inner_text("body") or "").lower()
            if "page not found" in body_text or "unable to resolve your request" in body_text:
                return False
            page.pdf(path=save_path, format="A4", print_background=True)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
            with open(save_path, "rb") as f:
                if f.read(4) == b"%PDF":
                    if verbose:
                        print(f"✅ OSF rendered page fallback success for {osf_id}")
                    return True
    except Exception as e:
        if verbose:
            print(f"  OSF rendered fallback error: {str(e)[:120]}")
    return False


#-----------------------------------------------------------------------------------------
def _text_similarity(a: str, b: str) -> float:
    """Return normalized similarity score for two strings."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _extract_pdf_links_from_doaj_record(record: dict):
    """Extract candidate full-text links from a DOAJ API record."""
    candidates = []
    bibjson = record.get("bibjson", {}) if isinstance(record, dict) else {}
    for link in bibjson.get("link", []) or []:
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if not url:
            continue
        link_type = (link.get("type") or "").lower()
        if "fulltext" in link_type or ".pdf" in url.lower():
            candidates.append(url)
    return candidates


@_timed("core")
def try_core_fallback(doi: str, save_path: str, verbose=False):
    """Try CORE (core.ac.uk) API for open-access PDFs."""
    global _CORE_SESSION_DISABLED

    # Skip silently if CORE was disabled earlier in this session due to auth failure
    if _CORE_SESSION_DISABLED:
        return False
    if _core_timeout_skip(verbose=verbose):
        return False

    core_api_key = os.getenv("COREAPIKEY")
    if not core_api_key:
        if verbose:
            print("  CORE: no COREAPIKEY in .env.local, skipping")
        return False

    # Don't spend a request to be told we have none left.
    if _core_quota_exhausted(verbose=verbose):
        return False

    try:
        core_headers = {
            "Authorization": f"Bearer {core_api_key}",
            "Accept": "application/json",
            "User-Agent": "MetascienceObservatory/1.0",
        }
        r = _get_with_retries(
            "https://api.core.ac.uk/v3/search/works/",
            params={"q": f'doi:"{doi}"', "limit": 3},
            headers=core_headers,
            timeout=30,
            retries=3,
        )
        _core_note_success()
        _core_quota_note_headers(r.headers)
        if r.status_code == 429:
            # A 429 means the daily budget is gone, not that we asked too fast.
            # Record it as exhausted so the remaining records skip CORE outright
            # instead of each spending a request to rediscover the same 429.
            global _CORE_QUOTA_REMAINING
            with _CORE_QUOTA_LOCK:
                if not _CORE_QUOTA_REMAINING:
                    _CORE_QUOTA_REMAINING = 0
            _core_quota_exhausted(verbose=verbose)
            if verbose:
                print("  CORE: rate limited (429)")
            return False
        if r.status_code in (401, 403):
            # Auth failure — disable CORE for the rest of the session (all workers).
            # Lock the check-then-act so only the first worker to see the failure
            # prints the warning; the rest observe the flag already set.
            with _CORE_SESSION_LOCK:
                if not _CORE_SESSION_DISABLED:
                    _CORE_SESSION_DISABLED = True
                    _print_yellow_warning(
                        f"⚠️  CORE disabled for session: HTTP {r.status_code}. "
                        f"Check COREAPIKEY in .env.local"
                    )
            return False
        if r.status_code != 200:
            if verbose:
                print(f"  CORE: HTTP {r.status_code}")
            return False
        data = r.json()
        results = data.get("results", [])
        if not results:
            if verbose:
                print(f"  CORE: no results for {doi}")
            return False

        # A hit found by DOI is not necessarily the article. CORE files a
        # grant report or a thesis under the DOI of the paper it produced: for
        # 10.1111/psyp.14329 the only hit with a downloadUrl was a six-page
        # Spanish "Informe final del proyecto" by the same authors, while the
        # work whose title matched had no file at all (2026-09-02). The identity
        # check refuses such bytes afterwards; refusing the hit by its own
        # title first spares the download and the misleading "success" line.
        # A hit with no title, or an article whose title is unknown, is left
        # for the identity check to judge.
        wanted_title = _title_for(doi, verbose=verbose)
        if wanted_title:
            from .retrieval._util import titles_match

            matching, untitled = [], []
            for result in results:
                hit_title = " ".join((result.get("title") or "").split())
                if not hit_title:
                    untitled.append(result)
                elif titles_match(wanted_title, hit_title):
                    matching.append(result)
                elif verbose:
                    print(f'  CORE: skipping "{hit_title[:60]}" -- title does not match')
            results = matching + untitled

        for result in results:
            download_url = result.get("downloadUrl")
            if not download_url:
                continue
            if verbose:
                print(f"  CORE: trying downloadUrl: {download_url[:120]}")
            if try_download(download_url, save_path, verbose):
                if verbose:
                    print(f"✅ CORE success for {doi}")
                return True
            # downloadUrl might be a landing page
            if try_landing_page_pdf_fallback(doi, download_url, save_path, verbose):
                if verbose:
                    print(f"✅ CORE (landing-page) success for {doi}")
                return True
        return False
    except Exception as e:
        from requests.exceptions import ConnectionError as _ReqConnErr, Timeout as _ReqTimeout
        if isinstance(e, (_ReqConnErr, _ReqTimeout, SSLError)):
            _core_note_timeout(verbose=verbose)
        if verbose:
            print(f"  CORE error: {e}")
        return False


#: A title shorter than this is a phrase, not an identifier, and Scholar ranks
#: somebody else's paper first. Measured by the PR #2 author, 2026-09-20: the
#: title Crossref holds for 10.1177/1948550616659120, "Social class and
#: prosocial behavior", returned four other papers' PDFs in the top five; with
#: the subtitle added, the article's own PDF came first.
_SCHOLAR_QUERY_MIN = 60

#: Substrings of SerpApi's `error` text that mean no later search can succeed
#: either: a bad key, or a plan with no searches left.
_SERPAPI_FATAL_ERRORS = ("api key", "run out", "out of searches", "exhausted")


def _serpapi_error_is_fatal(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _SERPAPI_FATAL_ERRORS)


def _scholar_title_query(doi: str, title: str, verbose=False) -> str:
    """The quoted title, with the Crossref subtitle added when the title is short.

    The title stays a quoted phrase; the subtitle follows unquoted, so Scholar
    ranks by it without requiring its exact punctuation.
    """
    base = '"' + html.unescape(title).replace('"', ' ').strip() + '"'
    if len(title) >= _SCHOLAR_QUERY_MIN:
        return base
    subtitle = html.unescape(_subtitle_for(doi, verbose=verbose)).replace('"', ' ').strip()
    return f"{base} {subtitle}" if subtitle else base


@_timed("serpapi_scholar")
def try_serpapi_scholar_fallback(doi: str, save_path: str, verbose=False):
    """Search Scholar only after other sources fail; at most two API calls.

    DOI searches can return papers citing the target. Filter by known title
    and use the normal OA downloader, including its PDF identity checks.
    """
    global _SERPAPI_SESSION_DISABLED
    api_key = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not api_key or _SERPAPI_SESSION_DISABLED:
        return False

    from .retrieval._util import titles_match
    from urllib.parse import urlsplit

    title = _title_for(doi, verbose=verbose)
    queries = [f'"{doi}"']
    if title:
        queries.append(_scholar_title_query(doi, title, verbose=verbose))
    seen = set()
    for query in queries:
        if _SERPAPI_SESSION_DISABLED:
            return False
        try:
            # No automatic retries: each search may consume account credits.
            response = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google_scholar", "q": query,
                        "api_key": api_key, "num": 5, "hl": "en"},
                timeout=30,
            )
            if response.status_code in (401, 403, 429):
                with _SERPAPI_SESSION_LOCK:
                    if not _SERPAPI_SESSION_DISABLED:
                        _SERPAPI_SESSION_DISABLED = True
                        _print_yellow_warning(
                            f"SerpApi disabled for session: HTTP {response.status_code}. "
                            "Check SERPAPI_API_KEY and your account quota."
                        )
                return False
            if response.status_code != 200:
                if verbose:
                    print(f"  SerpApi: HTTP {response.status_code}")
                return False
            data = response.json()
            if not isinstance(data, dict):
                if verbose:
                    print("  SerpApi: search failed")
                return False
            error = str(data.get("error") or "")
            if error:
                if _serpapi_error_is_fatal(error):
                    with _SERPAPI_SESSION_LOCK:
                        if not _SERPAPI_SESSION_DISABLED:
                            _SERPAPI_SESSION_DISABLED = True
                            _print_yellow_warning(
                                "SerpApi disabled for session: the API reported a key "
                                "or quota problem. Check SERPAPI_API_KEY and your account."
                            )
                    return False
                # SerpApi reports "Google returned nothing for this query" as an
                # error on a 200. For the DOI query that is the common case, and
                # the title query is the one likely to find the paper -- so move
                # on rather than end the attempt. The text is never printed: it
                # is the API's own body, and the policy here is not to echo it.
                if verbose:
                    print("  SerpApi: no usable results for this query")
                continue
            results = data.get("organic_results") or []
            if not isinstance(results, list):
                return False
            candidates = []
            for result in results[:5]:
                if not isinstance(result, dict):
                    continue
                hit_title = result.get("title")
                if title and isinstance(hit_title, str) and hit_title and not titles_match(title, hit_title):
                    continue
                resources = result.get("resources") or []
                if isinstance(resources, list):
                    for resource in resources:
                        if isinstance(resource, dict):
                            candidates.append((resource.get("link"),
                                               str(resource.get("file_format", "")).upper() == "PDF"))
                link = result.get("link")
                if isinstance(link, str):
                    candidates.append((link, ".pdf" in link.lower() or "/pdf" in link.lower()))
            # PDF resources outrank article landings, even across results.
            candidates.sort(key=lambda item: not item[1])
            fresh = []
            for url, direct in candidates:
                if not isinstance(url, str) or url in seen:
                    continue
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    continue
                seen.add(url)
                fresh.append((url, direct))
            if _try_oa_location_urls(doi, fresh, save_path, verbose):
                return True
        except Exception as exc:
            # Requests exceptions can contain the URL with the private key.
            # Never print the exception text or the API response body.
            if verbose:
                print(f"  SerpApi: request failed ({type(exc).__name__})")
            return False
    return False


@_timed("doaj")
def try_doaj_fallback(doi: str, save_path: str, verbose=False):
    """Try DOAJ API for OA full-text links."""
    try:
        query = quote_plus(f"doi:{doi}")
        url = f"https://doaj.org/api/search/articles/{query}"
        r = requests.get(url, timeout=12)
        if r.status_code != 200:
            return False

        data = r.json() or {}
        results = data.get("results", []) or []
        for rec in results[:10]:
            for candidate in _extract_pdf_links_from_doaj_record(rec):
                if try_download(candidate, save_path, verbose):
                    if verbose:
                        print(f"✅ DOAJ success for {doi}")
                    return True
    except Exception as e:
        if verbose:
            print(f"Error with DOAJ: {e}")
    return False


def _collect_datacite_candidate_urls(datacite_attributes: dict):
    """Collect likely downloadable URLs from DataCite metadata."""
    urls = []
    if not isinstance(datacite_attributes, dict):
        return urls

    direct_url = datacite_attributes.get("url")
    if isinstance(direct_url, str) and direct_url:
        urls.append(direct_url)

    content_url = datacite_attributes.get("contentUrl")
    if isinstance(content_url, list):
        urls.extend([u for u in content_url if isinstance(u, str) and u])
    elif isinstance(content_url, str) and content_url:
        urls.append(content_url)

    for rid in datacite_attributes.get("relatedIdentifiers", []) or []:
        if not isinstance(rid, dict):
            continue
        identifier = rid.get("relatedIdentifier")
        relation = (rid.get("relationType") or "").lower()
        if isinstance(identifier, str) and identifier.startswith("http"):
            if relation in {"issupplementto", "isversionof", "isidenticalto", "iscitedby", "references"}:
                urls.append(identifier)

    deduped = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _is_datacite_prefix(doi: str) -> bool:
    """True when a DataCite lookup for this DOI could plausibly return anything.

    Measured across four completed --tracksource runs (18,487 attributed
    artifacts): the DataCite routes produced ZERO. They were nonetheless calling
    api.datacite.org for every record, twice -- once here and once for related
    identifiers -- including for the ~95% of DOIs registered with Crossref, where
    a miss is guaranteed before the request is sent.

    Honest about the trade: DataCite has thousands of prefixes and this set holds
    only the repository ones, so a DataCite DOI outside it is no longer tried at
    all. That is a recall-for-cost trade, not a free win. It is justified by the
    zero hit rate plus the fact that step 0 of the chain already has a dedicated
    handler for every prefix in the set. Widen the set in retrieval/resolve.py if
    a real miss ever turns up.
    """
    if not isinstance(doi, str) or not doi.startswith("10."):
        return False
    # Imported at call time on purpose: a module-level import would execute
    # fetchpdf/retrieval/__init__.py just to read one dict.
    from .retrieval.resolve import DATACITE_REPOSITORY_PREFIXES

    return doi.split("/", 1)[0].lower() in DATACITE_REPOSITORY_PREFIXES


@_timed("datacite")
def try_datacite_fallback(doi: str, save_path: str, verbose=False):
    """Try DataCite for direct content URLs and related resource links."""
    if not _is_datacite_prefix(doi):
        return False
    try:
        r = requests.get(f"https://api.datacite.org/dois/{doi}", timeout=12)
        if r.status_code != 200:
            return False

        attributes = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        candidates = _collect_datacite_candidate_urls(attributes)
        for candidate in candidates[:12]:
            if try_download(candidate, save_path, verbose):
                if verbose:
                    print(f"✅ DataCite success for {doi}")
                return True
    except Exception as e:
        if verbose:
            print(f"Error with DataCite fallback: {e}")
    return False


@_timed("apa_supplemental")
def try_apa_supplemental_fallback(doi: str, save_path: str, verbose=False):
    """Try APA supplemental files and convert doc/docx to PDF when available."""
    if not doi.startswith("10.1037/"):
        return False

    article_code = doi.split("/", 1)[1].strip().lower()
    if not re.fullmatch(r"[a-z0-9]+", article_code or ""):
        return False

    supp_page = f"https://supp.apa.org/psycarticles/supplemental/{article_code}/{article_code}_supp.html"
    try:
        r = requests.get(supp_page, timeout=12)
        if r.status_code != 200:
            return False

        links = re.findall(r'href=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
        candidates = []
        for href in links:
            href_lower = href.lower()
            if href_lower.endswith(".pdf") or href_lower.endswith(".docx") or href_lower.endswith(".doc"):
                if href.startswith("http"):
                    candidates.append(href)
                else:
                    candidates.append(f"https://supp.apa.org/psycarticles/supplemental/{article_code}/{href.lstrip('/')}")

        if verbose:
            print(f"  APA supplemental candidates: {len(candidates)}")

        for candidate in candidates:
            # Direct PDF supplemental
            if candidate.lower().endswith(".pdf"):
                if try_download(candidate, save_path, verbose):
                    if verbose:
                        print(f"✅ APA supplemental PDF success for {doi}")
                    return True
                continue

            # DOC/DOCX supplemental -> convert to PDF
            if candidate.lower().endswith(".doc") or candidate.lower().endswith(".docx"):
                tmp = requests.get(candidate, timeout=20)
                if tmp.status_code != 200 or len(tmp.content) < 100:
                    continue

                with tempfile.TemporaryDirectory() as tmpdir:
                    ext = ".docx" if candidate.lower().endswith(".docx") else ".doc"
                    input_path = os.path.join(tmpdir, f"{article_code}{ext}")
                    with open(input_path, "wb") as f:
                        f.write(tmp.content)

                    if shutil.which("soffice") is None:
                        if verbose:
                            print("  LibreOffice not found; cannot convert APA supplemental DOCX.")
                        continue

                    proc = subprocess.run(
                        [
                            "soffice",
                            "--headless",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            tmpdir,
                            input_path,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        if verbose:
                            print(f"  DOCX->PDF conversion failed: {proc.stderr[:120]}")
                        continue

                    converted_pdf = os.path.join(tmpdir, f"{article_code}.pdf")
                    if not os.path.exists(converted_pdf):
                        pdf_candidates = [p for p in os.listdir(tmpdir) if p.lower().endswith(".pdf")]
                        if not pdf_candidates:
                            continue
                        converted_pdf = os.path.join(tmpdir, pdf_candidates[0])

                    if os.path.getsize(converted_pdf) < 500:
                        continue
                    with open(converted_pdf, "rb") as rf:
                        if rf.read(4) != b"%PDF":
                            continue

                    shutil.copyfile(converted_pdf, save_path)
                    if verbose:
                        print(f"✅ APA supplemental DOCX converted to PDF for {doi}")
                    return True

    except Exception as e:
        if verbose:
            print(f"Error with APA supplemental fallback: {e}")
    return False


@_timed("wiley")
def try_wiley_rendered_pdf_fallback(doi: str, save_path: str, verbose=False):
    """
    Fallback for Wiley pages where /doi/pdf* is bot-blocked but /doi/full is readable.
    Renders the full page in Playwright and prints to PDF.
    """
    if not doi.startswith("10.1111/"):
        return False

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False

    article_url = f"https://onlinelibrary.wiley.com/doi/full/{doi}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.set_default_timeout(45000)
            resp = page.goto(article_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            if not resp or resp.status >= 400:
                _quiet_close(browser)
                return False

            body_text = (page.inner_text("body") or "").lower()
            # Avoid saving block pages/challenges as PDFs.
            blocked_signals = ["performing security verification", "just a moment", "ray id"]
            if any(signal in body_text for signal in blocked_signals):
                _quiet_close(browser)
                return False

            # Only do rendered fallback when page indicates free/open access.
            access_signals = ["free access", "open access"]
            if not any(signal in body_text for signal in access_signals):
                _quiet_close(browser)
                return False

            page.pdf(path=save_path, format="A4", print_background=True)
            _quiet_close(browser)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
            with open(save_path, "rb") as f:
                if f.read(4) == b"%PDF":
                    if verbose:
                        print(f"✅ Wiley rendered PDF fallback success for {doi}")
                    return True
    except Exception as e:
        if verbose:
            print(f"Error with Wiley rendered fallback: {e}")
    return False


#-----------------------------------------------------------------------------------------
def fetch_pdf(doi,
              save_path,
              email=None,
              verbose=False,
              delay=0.1,
              allow_xml_fallback=True,
              use_playwright=False,
              prioritize_xml=False,
              xml_only=False,
              xml_html_only=False,
              get_xml_or_html=False,
              to_markdown=False,
              target_task="extraction",
              upgrade_existing=False,
              want_provenance=False,
              _source_out=None,
              _paths_out=None,
              _visited=None,
              _resolver=None,
              ):
    """The chain, plus the question the chain never asked: is this the paper?

    A thin wrapper rather than an edit at each of the chain's forty
    `return save_path` sites. Every route that can write a PDF funnels through
    its return value, so one check here covers the direct downloads, the
    Crossref links, the repository routes and the Playwright page renders alike
    -- including any route added later, which is the part that matters.

    Not redundant with the checks inside the chain's own candidate loops: those
    let a search CONTINUE past a wrong file, where this one can only refuse the
    finished result. Catching it here and nowhere else would turn "wrong PDF"
    into "no PDF" for records whose right copy was two candidates further down.

    Verification is skipped in three cases, each for its own reason:
      * the tiered path already ran it, at `engine._validate_for_tier`;
      * the result is not a PDF -- an .xml result has passed `validate_t1`;
      * the file was already on disk. Re-verifying a corpus on every re-run
        would delete files this change never fetched; that is a different
        decision from "do not fetch the wrong file", and not this one's to make.

    The signature is spelled out rather than collapsed into **kwargs because
    the chain re-enters itself positionally -- `fetch_pdf(related, save_path,
    email, verbose, delay=0, ...)` at the Crossref-relation and DataCite steps.
    """
    # Snapshot what is already on disk for this record. The chain's
    # skip-if-exists check returns a path it did not fetch, and re-judging a
    # corpus on every re-run would delete files this change never wrote --
    # a different decision from "do not fetch the wrong file", and not this
    # one's to make. _source_out is not enough on its own: callers may pass
    # none, and the skip-if-exists path then reports nothing at all.
    pre_existing = _artifact_snapshot(save_path)

    written = _fetch_pdf_chain(
        doi, save_path, email=email, verbose=verbose, delay=delay,
        allow_xml_fallback=allow_xml_fallback, use_playwright=use_playwright,
        prioritize_xml=prioritize_xml, xml_only=xml_only,
        xml_html_only=xml_html_only, get_xml_or_html=get_xml_or_html,
        to_markdown=to_markdown, target_task=target_task,
        upgrade_existing=upgrade_existing, want_provenance=want_provenance,
        _source_out=_source_out, _paths_out=_paths_out, _visited=_visited,
        _resolver=_resolver,
    )

    tiered = bool(prioritize_xml or xml_only or xml_html_only or get_xml_or_html)
    if tiered:
        return written
    if written and _source_out is not None and _source_out[0] == "existing":
        return written
    if written and pre_existing.get(os.path.abspath(written)) == _stat_key(written):
        return written

    resolved = resolve_identifier_to_doi(doi, verbose=verbose) or doi
    if written and _accept_downloaded_pdf(written, resolved, verbose=verbose):
        return written
    if written:
        _record_source(_source_out, None)

    # No acceptable PDF. Structured full text is a BETTER artifact than a PDF,
    # not a consolation prize -- it is tier 1 on the extraction ladder and the
    # PDF is tier 5 -- so a record that cannot yield a PDF should still yield
    # the paper if the paper exists in markup anywhere. Not gated behind a flag,
    # because "no PDF" and "no full text" are different answers and only one of
    # them is worth a human's time.
    #
    # Restricted to T1/T2, which contain no T5 rung, so this cannot re-enter
    # the chain it was called from.
    if not allow_xml_fallback:
        return None
    try:
        from .retrieval.engine import retrieve_tiered

        if verbose:
            print(f"  No usable PDF for {resolved}; trying structured full text")
        result = retrieve_tiered(
            raw_identifier=doi,
            doi=resolved,
            pmid=extract_pmid(doi),
            save_path=save_path,
            target_task=target_task,
            xml_html_only=True,
            want_provenance=want_provenance,
            email=email,
            verbose=verbose,
            delay=delay,
            use_playwright=use_playwright,
            resolver=_resolver,
        )
    except Exception as e:
        if verbose:
            print(f"  Structured-full-text fallback failed: {str(e)[:120]}")
        return None

    path = getattr(result, "path", None)
    if path and os.path.exists(path):
        _record_source(_source_out, "structured_fallback")
        _print_yellow_warning(
            f"WARNING: No PDF available for {resolved}; saved structured full "
            f"text instead -> {path}"
        )
        return path
    return None


def _fetch_pdf_chain(doi,
                       save_path,
                       email=None,
                       verbose=False,
                       delay=0.1,
                       allow_xml_fallback=True,
                       use_playwright=False,
                       prioritize_xml=False,
                       xml_only=False,
                       xml_html_only=False,
                       get_xml_or_html=False,
                       to_markdown=False,
                       target_task="extraction",
                       upgrade_existing=False,
                       want_provenance=False,
                       _source_out=None,
                       _paths_out=None,
                       _visited=None,
                       _resolver=None,
                      ):
    """
    Try to download a PDF for a DOI (or PMID resolved to DOI) using multiple fallbacks:
      0. OSF, SSRN, Figshare, PsychArchives, Zenodo (if DOI matches pattern)
      1. PubMed Central (PMC)
      2. Unpaywall
      3. Crossref (direct PDF links or landing page)
      4. Europe PMC
      5. Semantic Scholar
      6. OpenAlex (strict rate limits: 1,000/day)
      7. CORE (core.ac.uk)
      8. Direct DOI resolver (html scraping, Crossref chooser)
      9. DataCite related identifiers
     10. ResearchGate
     11. DOI→PMID fallback
     12. Google Scholar via SerpApi (optional API key)
     13. Elsevier XML API (last resort)

    Saves PDF to save_dir as: doi.replace('/', '--') + '.pdf'
    Returns the path if successful, else None.

    prioritize_xml / xml_only / target_task / upgrade_existing select the
    format-prioritized path in fetchpdf.retrieval instead of the chain above.
    With all of them off -- the default -- not a line of this function changes
    behaviour, and the retrieval package is never even imported.

    Note what is deliberately *not* a parameter here: --pull-supplementary. It
    runs as a second pass above this function, in download_one and in main(),
    never inside it. The reason is retrieval/sources/legacy_pdf.py, which re-enters
    this function at T5 with a .fetchpdf-t5-* temporary save_path and without
    _visited -- so that re-entry is indistinguishable from a top-level call. A
    supplementary flag threaded through here would fire on it and scatter
    supplementary siblings next to a file that is unlinked seconds later. Keeping
    the pass above this function makes that impossible rather than merely
    forbidden. See fetchpdf.retrieval.supplementary.
    """
    # Use email from .env.local if not provided
    if email is None:
        email = _DEFAULT_EMAIL

    if _visited is None:
        _visited = set()

    if not isinstance(doi, str) or not doi.strip():
        print(f"ERROR with identifier: {doi}")
        return None

    # ---------------- Format-prioritized path (opt-in) ----------------
    # Placed before everything else so the default path below is reached in
    # exactly the state it always was. The tiered engine calls back into this
    # function for its T5 rung with these flags off, which is what stops it
    # recursing and what lets it reuse the whole chain rather than fork it.
    if prioritize_xml or xml_only or xml_html_only or get_xml_or_html:
        from .retrieval.engine import retrieve_tiered

        result = retrieve_tiered(
            raw_identifier=doi,
            doi=resolve_identifier_to_doi(doi, verbose=verbose),
            pmid=extract_pmid(doi),
            save_path=save_path,
            target_task=target_task,
            xml_only=xml_only,
            xml_html_only=xml_html_only,
            get_xml_or_html=get_xml_or_html,
            upgrade_existing=upgrade_existing,
            want_provenance=want_provenance,
            email=email,
            verbose=verbose,
            delay=delay,
            use_playwright=use_playwright,
            resolver=_resolver,
        )
        if result.artifact is not None:
            _record_source(_source_out, result.artifact.source)
        elif result.path:
            _record_source(_source_out, "existing")
        # The return stays a single path, because every caller treats it as one.
        # A --get-xml-or-html walk produces two, so the rest go out through this
        # list -- the same mutable-out-param idiom as _source_out above.
        if to_markdown:
            # Convert here rather than in the batch worker so single-DOI runs get
            # it too. Only the structured artifacts have a route; a PDF does not.
            from .retrieval.to_markdown import CONVERTIBLE, write_markdown

            for artifact_path in result.paths:
                if artifact_path.endswith(CONVERTIBLE):
                    write_markdown(artifact_path, verbose=verbose)
        if _paths_out is not None:
            _paths_out.extend(result.paths)
            _paths_out.append(result.summary)
        return result.path

    original_identifier = doi
    pmid_input = extract_pmid(original_identifier)
    doi = resolve_identifier_to_doi(doi, verbose=verbose)
    if doi:
        if doi in _visited:
            if verbose:
                print(f"  Skipping {doi} — already attempted (cycle detected)")
            return None
        _visited.add(doi)
    if not doi:
        if pmid_input:
            # Fallback path when PMID has no DOI in metadata indexes.
            xml_path = os.path.splitext(save_path)[0] + ".xml"
            if os.path.exists(save_path) or os.path.exists(xml_path):
                existing = save_path if os.path.exists(save_path) else xml_path
                if verbose:
                    print(f"✅ Skipping PMID {pmid_input} - file already exists: {existing}")
                _record_source(_source_out, "existing")
                return existing
            time.sleep(delay)
            if verbose:
                print(f"  DOI unavailable for PMID {pmid_input}; trying PMID-native PDF fallbacks...")
            pmid_got = try_pmid_direct_pdf_fallback(
                pmid_input, save_path, verbose, allow_xml=allow_xml_fallback
            )
            if pmid_got:
                _record_source(_source_out, "pmid_direct")
                return pmid_got if isinstance(pmid_got, str) else save_path
        if verbose:
            print(f"  Could not normalize/resolve identifier: {original_identifier}")
        return None
    if verbose and doi != canonicalize_doi(str(original_identifier)):
        print(f"  Using DOI: {doi}")

    # Defer Elsevier XML fallback until all PDF-oriented fallbacks have failed.
    elsevier_crossref_message = None
    _pmid_cache = [None]  # Cached from PMC idconv when available; used for DOI->PMID fallback

    # Skip if PDF or XML already exists
    xml_path = os.path.splitext(save_path)[0] + ".xml"
    if os.path.exists(save_path) or os.path.exists(xml_path):
        existing = save_path if os.path.exists(save_path) else xml_path
        if verbose:
            print(f"✅ Skipping {doi} - file already exists: {existing}")
        _record_source(_source_out, "existing")
        return existing

    time.sleep(delay)

    # ---------------- 0  OSF DOI handling ----------------
    # Handles both OSF Projects (10.17605/osf.io/xxxxx) and OSF Preprints (10.31234/osf.io/xxxxx)
    if doi.lower().startswith("10.17605/osf.io") or doi.lower().startswith("10.31234/osf.io") or "osf.io" in doi.lower():
        if verbose:
            print(f"🔍 Trying OSF download for {doi}...")
        try:
            # Normalize DOI → OSF identifier
            osf_id = doi.split("/")[-1].replace("%2F", "").replace("OSF.IO", "").strip().lower()
            if not osf_id:
                osf_id = re.findall(r"osf\.io/([a-z0-9]+)", doi.lower())
                osf_id = osf_id[0] if osf_id else None

            if verbose:
                print(f"  OSF ID extracted: {osf_id}")

            if osf_id:
                # Method 1: Try simple direct download URLs first (fast, no browser needed)
                if verbose:
                    print("  Trying OSF direct URLs...")
                candidate_urls = [
                    f"https://osf.io/{osf_id}/download",
                    f"https://osf.io/download/{osf_id}/",
                ]

                for url in candidate_urls:
                    if try_download(url, save_path, verbose):
                        print(f"✅ OSF direct download success for {doi}")
                        _record_source(_source_out, "osf")
                        return save_path

                # Method 2: Use Playwright to scrape OSF page for download links
                if use_playwright:
                  if verbose:
                    print(f"  Trying OSF Playwright scraping for {osf_id}...")
                try:
                    if not use_playwright:
                        raise ImportError("Playwright disabled")
                    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

                    with sync_playwright() as p:
                        browser = p.chromium.launch(
                            headless=True,
                            args=[
                                "--no-sandbox",
                                "--disable-setuid-sandbox",
                                "--disable-blink-features=AutomationControlled",  # Hide automation
                            ],
                        )
                        context = browser.new_context(
                            user_agent=headers["User-Agent"],
                            accept_downloads=True,
                            # Add extra headers to appear more like a real browser
                            extra_http_headers={
                                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                "Accept-Language": "en-US,en;q=0.9",
                                "Accept-Encoding": "gzip, deflate, br",
                            },
                        )
                        page = context.new_page()
                        page.set_default_timeout(30000)

                        # Hide webdriver property to avoid detection
                        page.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', {
                                get: () => undefined
                            });
                        """)

                        osf_url = f"https://osf.io/{osf_id}/"
                        if verbose:
                            print(f"    Loading OSF page: {osf_url}")

                        try:
                            # Wait for network to be idle (Angular app needs time to load)
                            resp = page.goto(osf_url, wait_until="networkidle", timeout=60000)

                            if verbose:
                                status = resp.status if resp else "None"
                                print(f"    Page response status: {status}")

                            if resp and resp.status == 429:
                                if verbose:
                                    print("    ⚠️ OSF rate limit hit (429 Too Many Requests)")
                                _quiet_close(browser)
                                # Don't continue with other methods after rate limit
                                return None

                            if resp and resp.status < 400:
                                # Give Angular additional time to render (OSF is a SPA)
                                if verbose:
                                    print("    Waiting for Angular app to render...")

                                # Wait for either a download button or main content to appear
                                try:
                                    page.wait_for_selector("button[aria-label='Download'], p-button, .p-button", timeout=10000)
                                    if verbose:
                                        print("    Page content loaded")
                                except PlaywrightTimeout:
                                    if verbose:
                                        print("    Timeout waiting for page content, continuing anyway...")
                                    page.wait_for_timeout(2000)

                                # Debug: Save page screenshot and HTML
                                if verbose:
                                    debug_dir = "./test_downloads/debug"
                                    os.makedirs(debug_dir, exist_ok=True)
                                    screenshot_path = os.path.join(debug_dir, f"osf_{osf_id}.png")
                                    html_path = os.path.join(debug_dir, f"osf_{osf_id}.html")
                                    try:
                                        page.screenshot(path=screenshot_path)
                                        with open(html_path, "w", encoding="utf-8") as f:
                                            f.write(page.content())
                                        print(f"    Debug: Saved screenshot to {screenshot_path}")
                                        print(f"    Debug: Saved HTML to {html_path}")
                                    except Exception as e:
                                        print(f"    Debug save error: {str(e)[:50]}")

                                # Look for download button/link
                                # OSF uses Angular p-button components: <p-button><button aria-label="Download"><span class="fas fa-download"></span></button></p-button>
                                download_selectors = [
                                    "button[aria-label='Download']",  # Direct button with aria-label
                                    "p-button button[aria-label='Download']",  # Angular p-button wrapper
                                    "button.p-button[aria-label='Download']",  # Button with p-button class
                                    "button:has(span.fa-download)",  # Button containing fa-download icon
                                    "a[href*='/download']",  # Direct download links
                                    ".file-download",  # File download class
                                ]

                                if verbose:
                                    print("    Searching for download buttons...")

                                for selector in download_selectors:
                                    try:
                                        if verbose:
                                            print(f"    Trying selector: {selector}")

                                        download_btn = page.query_selector(selector)
                                        if download_btn:
                                            if verbose:
                                                print(f"    ✓ Found element with selector: {selector}")

                                            # If we found a span icon, get the parent button
                                            try:
                                                tag_name = download_btn.evaluate("el => el.tagName").lower()
                                                if tag_name == "span":
                                                    if verbose:
                                                        print("    Found icon span, getting parent button...")
                                                    parent = page.evaluate_handle("el => el.closest('button')", download_btn)
                                                    if parent:
                                                        download_btn = parent.as_element()
                                                        if verbose:
                                                            print("    Using parent button element")
                                            except Exception as e:
                                                if verbose:
                                                    print(f"    Error getting parent: {str(e)[:100]}")

                                            # First try to get href (for links)
                                            href = download_btn.get_attribute("href")
                                            if verbose:
                                                print(f"    Element href: {href}")

                                            # If it's a button (no href), try clicking and intercepting download
                                            if not href:
                                                if verbose:
                                                    print("    No href - trying to click button and intercept download...")

                                                try:
                                                    with page.expect_download(timeout=30000) as download_info:
                                                        download_btn.click()
                                                    download = download_info.value
                                                    download.save_as(save_path)

                                                    if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                                                        with open(save_path, "rb") as f:
                                                            if f.read(4) == b"%PDF":
                                                                if verbose:
                                                                    print("✅ OSF PDF downloaded via button click")
                                                                _record_source(_source_out, "osf")
                                                                _quiet_close(browser)
                                                                return save_path
                                                    if verbose:
                                                        print("    Download file check failed")
                                                except PlaywrightTimeout:
                                                    if verbose:
                                                        print("    Button click didn't trigger download")
                                                except Exception as e:
                                                    if verbose:
                                                        print(f"    Button click error: {str(e)[:100]}")

                                            # If we have an href, navigate to it
                                            if href:
                                                if not href.startswith("http"):
                                                    href = f"https://osf.io{href}"

                                                if verbose:
                                                    print(f"    Navigating to: {href[:80]}...")

                                                # Try to download via Playwright
                                                try:
                                                    pdf_resp = page.goto(href, wait_until="load", timeout=30000)
                                                    if pdf_resp:
                                                        if verbose:
                                                            print(f"    Response status: {pdf_resp.status}")
                                                            print(f"    Content-Type: {pdf_resp.headers.get('content-type', 'unknown')}")

                                                        if pdf_resp.status == 200:
                                                            content_type = pdf_resp.headers.get("content-type", "")
                                                            body = pdf_resp.body()

                                                            if verbose:
                                                                print(f"    Body size: {len(body) if body else 0} bytes")
                                                                if body and len(body) > 4:
                                                                    print(f"    First 4 bytes: {body[:4]}")

                                                            if body and len(body) > 10000:
                                                                # Check for PDF signature
                                                                if body[:4] == b"%PDF" or "pdf" in content_type.lower():
                                                                    with open(save_path, "wb") as f:
                                                                        f.write(body)
                                                                    if verbose:
                                                                        print("✅ OSF PDF downloaded via href navigation")
                                                                    _record_source(_source_out, "osf")
                                                                    _quiet_close(browser)
                                                                    return save_path
                                                                else:
                                                                    if verbose:
                                                                        print("    Body doesn't appear to be PDF")
                                                except Exception as e:
                                                    if verbose:
                                                        print(f"    Navigation error: {str(e)[:100]}")
                                                    continue
                                        else:
                                            if verbose:
                                                print("    ✗ No element found")

                                    except Exception as e:
                                        if verbose:
                                            print(f"    Selector error: {str(e)[:100]}")
                                        continue

                            else:
                                if verbose:
                                    print(f"    OSF page returned error status: {resp.status if resp else 'No response'}")
                                _quiet_close(browser)
                                return None

                            if resp and resp.status < 400:
                                # If buttons didn't work, look for PDF links in page content
                                if verbose:
                                    print("    Looking for PDF URLs in page source...")

                                content = _safe_page_content(page)
                                pdf_patterns = [
                                    r'href="(https://osf\.io/[^"]*download[^"]*)"',
                                    r'href="(/download/[^"]+)"',
                                ]

                                pdf_urls = set()
                                for pattern in pdf_patterns:
                                    matches = re.findall(pattern, content, re.IGNORECASE)
                                    if verbose:
                                        print(f"    Pattern '{pattern}' found {len(matches)} matches")
                                    for match in matches:
                                        if match.startswith("http"):
                                            pdf_urls.add(match)
                                        else:
                                            pdf_urls.add(f"https://osf.io{match}")

                                if verbose:
                                    print(f"    Found {len(pdf_urls)} unique PDF URLs")
                                    if len(pdf_urls) > 0:
                                        for url in list(pdf_urls)[:5]:
                                            print(f"      - {url[:80]}...")

                                # Try each unique PDF URL (limit to 3)
                                for i, pdf_url in enumerate(list(pdf_urls)[:3]):
                                    if verbose:
                                        print(f"    Trying OSF URL {i+1}: {pdf_url[:80]}...")

                                    try:
                                        pdf_resp = page.goto(pdf_url, wait_until="load", timeout=30000)
                                        if pdf_resp:
                                            if verbose:
                                                print(f"      Response status: {pdf_resp.status}")
                                                print(f"      Content-Type: {pdf_resp.headers.get('content-type', 'unknown')}")

                                            if pdf_resp.status == 200:
                                                content_type = pdf_resp.headers.get("content-type", "")
                                                body = pdf_resp.body()

                                                if verbose:
                                                    print(f"      Body size: {len(body) if body else 0} bytes")
                                                    if body and len(body) > 4:
                                                        print(f"      First 4 bytes: {body[:4]}")

                                                if body and len(body) > 10000:
                                                    # Check for PDF signature
                                                    if body[:4] == b"%PDF" or "pdf" in content_type.lower():
                                                        with open(save_path, "wb") as f:
                                                            f.write(body)
                                                        if verbose:
                                                            print("✅ OSF PDF downloaded via scraped link")
                                                        _record_source(_source_out, "osf")
                                                        _quiet_close(browser)
                                                        return save_path
                                                    else:
                                                        if verbose:
                                                            print("      Body doesn't appear to be PDF")
                                    except Exception as e:
                                        if verbose:
                                            print(f"      Error: {str(e)[:100]}")
                                        continue

                                # Method 3: OSF API file-provider fallback
                                if _download_pdf_from_osf_api(osf_id, save_path, verbose, doi=doi):
                                    _record_source(_source_out, "osf")
                                    _quiet_close(browser)
                                    return save_path

                                # Method 4: Render metadata page as PDF when no files exist
                                if _render_osf_page_to_pdf(osf_id, save_path, verbose, page=page):
                                    _record_source(_source_out, "osf")
                                    _quiet_close(browser)
                                    return save_path

                        except PlaywrightTimeout:
                            if verbose:
                                print("    OSF page load timeout")
                        except Exception as e:
                            if verbose:
                                print(f"    OSF Playwright error: {e}")

                        _quiet_close(browser)

                except ImportError:
                    if verbose:
                        print("    Playwright not available for OSF download")
                except Exception as e:
                    if verbose:
                        print(f"    OSF Playwright failed: {e}")

        except Exception as e:
            print(f"⚠️ OSF download failed for {doi}: {e}")
            pass

    # ---------------- SSRN DOI handling ----------------
    if "ssrn" in doi.lower() or doi.startswith("10.2139/"):
        if verbose:
            print(f"🔍 Trying SSRN download for {doi}...")
        try:
            # Extract SSRN ID from DOI: 10.2139/ssrn.XXXXXXX → XXXXXXX
            ssrn_id = None
            m = re.search(r"ssrn[.\-/]?(\d+)", doi, re.IGNORECASE)
            if m:
                ssrn_id = m.group(1)

            if verbose:
                print(f"  SSRN ID extracted: {ssrn_id}")

            if ssrn_id:
                # Use Playwright to handle Cloudflare protection and download PDF
                try:
                    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

                    with sync_playwright() as p:
                        browser = p.chromium.launch(
                            headless=True,
                            args=["--no-sandbox", "--disable-setuid-sandbox"],
                        )
                        context = browser.new_context(
                            user_agent=headers["User-Agent"],
                            accept_downloads=True,
                        )
                        page = context.new_page()
                        page.set_default_timeout(30000)

                        abstract_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}"
                        if verbose:
                            print(f"  Loading SSRN page: {abstract_url}")

                        try:
                            resp = page.goto(abstract_url, wait_until="domcontentloaded")
                            if resp and resp.status < 400:
                                # Wait for Cloudflare challenge to complete
                                page.wait_for_timeout(5000)

                                # Look for download button/link
                                download_selectors = [
                                    "a:has-text('Download')",
                                    "a.download-button",
                                    "a[href*='Delivery.cfm']",
                                    "button:has-text('Download')",
                                ]

                                for selector in download_selectors:
                                    try:
                                        download_btn = page.query_selector(selector)
                                        if download_btn:
                                            if verbose:
                                                print(f"  Found download button: {selector}")

                                            # Get the href if it's a link
                                            href = download_btn.get_attribute("href")
                                            if href and verbose:
                                                print(f"    Button href: {href[:80]}...")

                                            # Try to click and download
                                            try:
                                                with page.expect_download(timeout=30000) as download_info:
                                                    download_btn.click()
                                                download = download_info.value
                                                download.save_as(save_path)

                                                if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                                                    with open(save_path, "rb") as f:
                                                        if f.read(4) == b"%PDF":
                                                            if verbose:
                                                                print("✅ SSRN PDF downloaded via Playwright download")
                                                            _record_source(_source_out, "ssrn")
                                                            _quiet_close(browser)
                                                            return save_path
                                                if verbose:
                                                    print("    Download file check failed")
                                            except PlaywrightTimeout:
                                                if verbose:
                                                    print("    expect_download timeout, trying href navigation...")
                                                # Download didn't trigger - try navigating to href
                                                if href:
                                                    if not href.startswith("http"):
                                                        if href.startswith("/"):
                                                            href = f"https://papers.ssrn.com{href}"
                                                        else:
                                                            href = f"https://papers.ssrn.com/sol3/{href}"

                                                    if verbose:
                                                        print(f"    Navigating to: {href[:80]}...")

                                                    pdf_resp = page.goto(href, wait_until="load", timeout=30000)
                                                    if pdf_resp:
                                                        if verbose:
                                                            print(f"    Response status: {pdf_resp.status}")
                                                            print(f"    Content-Type: {pdf_resp.headers.get('content-type', 'unknown')}")

                                                        if pdf_resp.status == 200:
                                                            content_type = pdf_resp.headers.get("content-type", "")
                                                            body = pdf_resp.body()

                                                            if verbose:
                                                                print(f"    Body size: {len(body) if body else 0} bytes")
                                                                if body and len(body) > 4:
                                                                    print(f"    First 4 bytes: {body[:4]}")

                                                            if body and len(body) > 10000:
                                                                # Check for PDF signature
                                                                if body[:4] == b"%PDF" or "pdf" in content_type.lower():
                                                                    with open(save_path, "wb") as f:
                                                                        f.write(body)
                                                                    if verbose:
                                                                        print("✅ SSRN PDF downloaded from href")
                                                                    _record_source(_source_out, "ssrn")
                                                                    _quiet_close(browser)
                                                                    return save_path
                                                                else:
                                                                    if verbose:
                                                                        print("    Body doesn't appear to be PDF")
                                            except Exception as e:
                                                if verbose:
                                                    print(f"    Button click error: {str(e)[:100]}")
                                                continue
                                    except Exception as e:
                                        if verbose:
                                            print(f"    Selector error: {str(e)[:100]}")
                                        continue

                                # If button click didn't work, try to find PDF URLs in the page
                                if verbose:
                                    print("  Looking for PDF URLs in page source...")

                                content = _safe_page_content(page)
                                pdf_patterns = [
                                    r'href="([^"]*Delivery\.cfm[^"]*)"',
                                ]

                                # Collect unique PDF URLs
                                pdf_urls = set()
                                for pattern in pdf_patterns:
                                    matches = re.findall(pattern, content, re.IGNORECASE)
                                    for match in matches:
                                        if "delivery" in match.lower():
                                            # Fix URL formatting
                                            if match.startswith("http"):
                                                pdf_urls.add(match)
                                            elif match.startswith("/sol3/"):
                                                pdf_urls.add(f"https://papers.ssrn.com{match}")
                                            elif match.startswith("/"):
                                                pdf_urls.add(f"https://papers.ssrn.com{match}")

                                if verbose:
                                    print(f"  Found {len(pdf_urls)} unique PDF URLs")

                                # Try each unique PDF URL (limit to 3 attempts)
                                for i, pdf_url in enumerate(list(pdf_urls)[:3]):
                                    if verbose:
                                        print(f"  Trying PDF URL {i+1}: {pdf_url[:80]}...")

                                    try:
                                        pdf_resp = page.goto(pdf_url, wait_until="load", timeout=30000)
                                        if pdf_resp:
                                            if verbose:
                                                print(f"    Response status: {pdf_resp.status}")
                                                print(f"    Content-Type: {pdf_resp.headers.get('content-type', 'unknown')}")

                                            if pdf_resp.status == 200:
                                                content_type = pdf_resp.headers.get("content-type", "")
                                                body = pdf_resp.body()

                                                if verbose:
                                                    print(f"    Body size: {len(body) if body else 0} bytes")
                                                    if body and len(body) > 4:
                                                        print(f"    First 4 bytes: {body[:4]}")

                                                if body and len(body) > 10000:
                                                    # Check for PDF signature
                                                    if body[:4] == b"%PDF" or "pdf" in content_type.lower():
                                                        with open(save_path, "wb") as f:
                                                            f.write(body)
                                                        if verbose:
                                                            print("✅ SSRN PDF downloaded from URL")
                                                        _record_source(_source_out, "ssrn")
                                                        _quiet_close(browser)
                                                        return save_path
                                                    else:
                                                        if verbose:
                                                            print("    Body doesn't appear to be PDF")
                                    except Exception as e:
                                        if verbose:
                                            print(f"    Failed: {str(e)[:100]}")
                                        continue

                        except PlaywrightTimeout:
                            if verbose:
                                print("  SSRN page load timeout")
                        except Exception as e:
                            if verbose:
                                print(f"  SSRN Playwright error: {e}")

                        _quiet_close(browser)

                except ImportError:
                    if verbose:
                        print("  Playwright not available for SSRN download")
                except Exception as e:
                    if verbose:
                        print(f"  SSRN Playwright failed: {e}")

        except Exception as e:
            if verbose:
                print(f"⚠️ SSRN download failed for {doi}: {e}")

    # ---------------- Figshare DOI handling ----------------
    if "figshare" in doi or doi.startswith("10.6084/"):
        try:
            # Extract article ID from DOI: 10.6084/m9.figshare.XXXXXXX.vN → XXXXXXX
            m = re.search(r"figshare\.(\d+)", doi)
            if m:
                article_id = m.group(1)
                r = requests.get(
                    f"https://api.figshare.com/v2/articles/{article_id}/files",
                    timeout=15,
                )
                if r.status_code == 200:
                    for fobj in r.json():
                        if (fobj.get("mimetype", "") == "application/pdf"
                                or fobj.get("name", "").lower().endswith(".pdf")):
                            dl_url = fobj.get("download_url")
                            if dl_url and try_download(dl_url, save_path, verbose):
                                print(f"✅ Figshare API success for {doi}")
                                _record_source(_source_out, "figshare")
                                return save_path
        except Exception as e:
            print(f"⚠️ Figshare download failed for {doi}: {e}")

    # ---------------- PsychArchives DOI handling ----------------
    # 10.23668/psycharchives.* - Leibniz psychology repository, PDFs via bitstream
    if "psycharchives" in doi.lower() or doi.startswith("10.23668/"):
        try:
            resolved = requests.get(
                f"https://doi.org/{doi}",
                headers=headers,
                timeout=12,
                allow_redirects=True,
            )
            if resolved.status_code == 200 and "psycharchives" in resolved.url.lower():
                item_html = resolved.text
                # Extract bitstream URLs (pada.psycharchives.org/bitstream/UUID)
                bitstream_urls = list(dict.fromkeys(
                    re.findall(
                        r'https?://[^"\'<>\s]*psycharchives[^"\'<>\s]*/bitstream/[a-f0-9\-]+',
                        item_html,
                        re.IGNORECASE,
                    )
                ))
                for bitstream_url in bitstream_urls[:5]:
                    if try_download(bitstream_url, save_path, verbose):
                        if verbose:
                            print(f"✅ PsychArchives bitstream success for {doi}")
                        _record_source(_source_out, "psycharchives")
                        return save_path
        except Exception as e:
            if verbose:
                print(f"Error with PsychArchives: {e}")

    # ---------------- Zenodo DOI handling ----------------
    # 10.5281/zenodo.* - CERN repository, files listed by the InvenioRDM API.
    # Handled here rather than left to the generic chain: the record page is the
    # only other route to the file link, and the API also disambiguates versions
    # (a concept DOI silently resolves to the latest version).
    if "zenodo" in doi.lower() or doi.startswith("10.5281/"):
        try:
            m = re.search(r"zenodo\.(\d+)", doi.lower())
            if m:
                r = requests.get(
                    f"https://zenodo.org/api/records/{m.group(1)}",
                    headers=headers,
                    timeout=15,
                )
                if r.status_code == 200:
                    # Largest PDF first: multi-file records usually carry the paper
                    # alongside smaller supplements. Restricted records list files
                    # with no links.self, so those simply yield no candidates.
                    pdfs = sorted(
                        (f for f in (r.json().get("files") or [])
                         if f.get("key", "").lower().endswith(".pdf")
                         and (f.get("links") or {}).get("self")),
                        key=lambda f: f.get("size") or 0,
                        reverse=True,
                    )
                    for fobj in pdfs:
                        if try_download(fobj["links"]["self"], save_path, verbose):
                            if verbose:
                                print(f"✅ Zenodo API success for {doi}")
                            _record_source(_source_out, "zenodo")
                            return save_path
        except Exception as e:
            if verbose:
                print(f"Error with Zenodo: {e}")

    # ---------------- PubMed Central (PMC) via Europe PMC ----------------
    # Many OA papers are freely available via PMC. Europe PMC's pdf=render
    # endpoint reliably serves PDFs (NCBI PMC uses JS redirects).
    try:
        r = _get_with_retries(
            _ncbi_url(f"{IDCONV_URL}?ids={doi}&format=json&tool=replication_search&email={email}"),
            timeout=15,
        )
        if r.status_code == 200:
            records = r.json().get("records", [])
            if records:
                rec0 = records[0]
                pmcid = rec0.get("pmcid")
                if rec0.get("pmid") is not None:
                    _pmid_cache[0] = str(rec0["pmid"]).strip()
                if pmcid:
                    epmc_pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
                    if try_download(epmc_pdf_url, save_path, verbose):
                        if verbose: print(f"✅ PMC/EuropePMC success for {doi} ({pmcid})")
                        _record_source(_source_out, "pmc")
                        return save_path
                    # pdf=render 404s for OA records Europe PMC holds only as
                    # JATS (eLife 34573, PLOS Currents). The XML is the paper.
                    if allow_xml_fallback:
                        xml_got = try_pmc_xml_fallback(
                            pmcid, save_path, verbose=verbose, doi=doi
                        )
                        if xml_got:
                            _record_source(_source_out, "pmc")
                            return xml_got
                        # Europe PMC serves fullTextXML only for records inside
                        # its OA subset. NIH author manuscripts are not in it --
                        # free to read on the PMC website, not redistributable --
                        # so that endpoint 404s while NCBI efetch serves the same
                        # JATS. Verified on PMC9292464 (10.1111/all.14949):
                        # Europe PMC 404, ?pdf=render 500, the web PDF behind a
                        # reCAPTCHA, and efetch 200 with a 27,571-character
                        # <body>. Without this the record has no full text at
                        # all, which is how its chain reached a landing-page
                        # scrape and came back with a cited document.
                        #
                        # A non-OA record's efetch reply is a well-formed
                        # <article> with no <body> and the refusal in an XML
                        # comment; _looks_like_jats_fulltext already rejects it.
                        xml_got = try_pmc_efetch_xml_fallback(
                            pmcid, save_path, verbose=verbose, doi=doi
                        )
                        if xml_got:
                            _record_source(_source_out, "pmc_efetch")
                            return xml_got
    except Exception as e:
        if verbose: print(f"Error with PMC: {e}")
        pass

    # ---------------- eLife CDN / API XML (HTML site 406s) ----------------
    if allow_xml_fallback and _elife_article_id(doi):
        try:
            xml_got = try_elife_xml_fallback(doi, save_path, verbose=verbose)
            if xml_got:
                _record_source(_source_out, "elife")
                return xml_got
        except Exception as e:
            if verbose:
                print(f"Error with eLife XML: {e}")

    # ---------------- eScholarship via PubMed LinkOut ----------------
    # UC and other universities deposit in eScholarship; PubMed abstract lists these
    try:
        if try_escholarship_via_pubmed(doi, save_path, verbose):
            _record_source(_source_out, "escholarship")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with eScholarship: {e}")

    # ---------------- Unpaywall ----------------
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            _remember_title(doi, data.get("title"))
            # best_oa_location is often a publisher landing with no PDF while
            # oa_locations[] still has a repository or preprint file. Walk them
            # all (capped); the ranked helper puts url_for_pdf first.
            if _try_oa_location_urls(
                doi, _collect_unpaywall_pdf_urls(data), save_path, verbose
            ):
                if (verbose): print(f"✅ Unpaywall success for {doi}")
                _record_source(_source_out, "unpaywall")
                return save_path
    except Exception as e: 
        if (verbose): print(f"Error with Unpaywall: {e}")
        pass

    # ----------------  Crossref ----------------
    try:
        # Use polite pool for higher rate limits (10 req/s vs 5 req/s)
        crossref_params = {}
        if _DEFAULT_EMAIL:
            crossref_params["mailto"] = _DEFAULT_EMAIL
        r = _crossref_raw_get(
            f"https://api.crossref.org/works/{doi}",
            params=crossref_params,
            timeout=10
        )
        if r.status_code == 200:
            m = r.json().get("message", {})
            _remember_title(doi, m.get("title"), m.get("page"))
            # Direct PDF links in Crossref metadata
            for link in m.get("link", []):
                if link.get("content-type") == "application/pdf":
                    if try_download(link.get("URL"), save_path, verbose):
                        if (verbose): print(f"✅ Crossref direct link success for {doi}")
                        _record_source(_source_out, "crossref")
                        return save_path
            # Text-mining XML. similarity-checking / unspecified links are
            # landing pages (PLOS dx.plos.org, eLife HTML). eLife's TDM link
            # is the CDN JATS file the HTML site 406s would have pointed at.
            if allow_xml_fallback:
                for xml_url in _crossref_tdm_xml_urls(m):
                    xml_got = _try_save_jats_xml(
                        xml_url, save_path, verbose=verbose, doi=doi
                    )
                    if xml_got:
                        _record_source(_source_out, "crossref")
                        return xml_got
            # Landing page fallback
            landing = m.get("URL")
            if try_download(landing, save_path, verbose):
                if (verbose): print(f"✅ Crossref landing page worked for {doi}")
                _record_source(_source_out, "crossref")
                return save_path
            if landing and try_landing_page_pdf_fallback(doi, landing, save_path, verbose):
                if verbose:
                    print(f"✅ Crossref landing-page extraction success for {doi}")
                _record_source(_source_out, "crossref")
                return save_path
            # Taylor & Francis: try direct /doi/pdf/ URL (works for some OA)
            if landing and "tandfonline.com" in landing:
                tf_pdf = f"https://www.tandfonline.com/doi/pdf/{doi}"
                if try_download(tf_pdf, save_path, verbose):
                    if verbose:
                        print(f"✅ Taylor & Francis /doi/pdf/ success for {doi}")
                    _record_source(_source_out, "crossref")
                    return save_path
            # Elsevier API fallback is intentionally deferred until the end.
            # Prefer a real PDF from other fallbacks first.
            if (
                doi.lower().startswith("10.1016/")
                or (landing and ("sciencedirect.com" in landing.lower() or "elsevier.com" in landing.lower()))
            ):
                elsevier_crossref_message = m
            # A published paper's PsyArXiv/bioRxiv copy is a Crossref relation,
            # not Unpaywall's "best" location. Re-enter the chain on that DOI;
            # _visited stops a cycle.
            for related in _crossref_related_dois(m, doi):
                if verbose:
                    print(f"  Trying Crossref related DOI: {related}")
                related_path = fetch_pdf(
                    related, save_path, email, verbose, delay=0,
                    allow_xml_fallback=allow_xml_fallback,
                    use_playwright=use_playwright,
                    _source_out=_source_out, _visited=_visited,
                )
                if related_path:
                    if verbose:
                        print(f"✅ Crossref related DOI success for {doi} via {related}")
                    _record_source(_source_out, "crossref_preprint")
                    return related_path
    except Exception as e:
        if (verbose): print(f"Error with Crossref: {e}")
        pass

    # ----------------  Europe PMC ----------------
    try:
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = r.json().get("resultList", {}).get("result", [])
            if results:
                full_urls = results[0].get("fullTextUrlList", {}).get("fullTextUrl", [])
                for u in full_urls:
                    if "pdf" in (u.get("url", "").lower()):
                        if try_download(u["url"], save_path, verbose):
                            if (verbose): print(f"✅ EuropePMC success for {doi}")
                            _record_source(_source_out, "europepmc")
                            return save_path
    except Exception as e:
        if (verbose): print(f"Error with Europe PMC: {e}")
        pass

    # ----------------  Semantic Scholar ----------------
    try:
        s2_headers = {}
        if _S2_API_KEY:
            s2_headers["x-api-key"] = _S2_API_KEY
        # Verified, unlike before. This used to pass verify=False permanently on the
        # strength of "S2 cert has expired" -- a standing MITM exposure kept alive by
        # a comment. Probed 2026-07-30: TLS verification succeeds. If the cert ever
        # lapses again, the SSLError branch below degrades for that one call and says
        # so out loud, rather than never verifying again.
        s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf"
        try:
            r = requests.get(s2_url, timeout=10, headers=s2_headers)
        except SSLError as e:
            _print_yellow_warning(
                f"⚠️  Semantic Scholar TLS verification failed ({str(e)[:80]}); "
                f"retrying this one call unverified."
            )
            r = requests.get(s2_url, timeout=10, headers=s2_headers, verify=False)
        if r.status_code == 200:
            pdf_url = r.json().get("openAccessPdf", {}).get("url")
            if pdf_url:
                if try_download(pdf_url, save_path, verbose):
                    if (verbose): print(f"✅ Semantic Scholar success for {doi}")
                    _record_source(_source_out, "semantic_scholar")
                    return save_path
                # URL may return HTML (e.g. OJS redirect, landing page); try landing-page extraction
                if try_landing_page_pdf_fallback(doi, pdf_url, save_path, verbose):
                    if (verbose): print(f"✅ Semantic Scholar (landing-page) success for {doi}")
                    _record_source(_source_out, "semantic_scholar")
                    return save_path
                # PsyArXiv preprints are on OSF: psyarxiv.com serves HTML, osf.io serves PDF
                if "psyarxiv.com" in pdf_url.lower():
                    m = re.search(r"psyarxiv\.com/([a-zA-Z0-9]+)", pdf_url, re.IGNORECASE)
                    if m:
                        osf_url = f"https://osf.io/{m.group(1).lower()}/download"
                        if try_download(osf_url, save_path, verbose):
                            if verbose:
                                print(f"✅ Semantic Scholar (PsyArXiv→OSF) success for {doi}")
                            _record_source(_source_out, "semantic_scholar")
                            return save_path
    except Exception as e:
        if (verbose): print(f"Error with Semantic Scholar: {e}")
        pass

    # ----------------OpenAlex ----------------
    # Note: OpenAlex has strict rate limits (1,000 downloads/day even with API key)
    # Placed after Semantic Scholar to preserve quota for harder-to-find papers
    try:
        openalex_headers = {}
        openalex_api_key = os.getenv("OPENALEXAPIKEY")
        if openalex_api_key:
            openalex_headers["Authorization"] = f"Bearer {openalex_api_key}"
        r = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            headers=openalex_headers,
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if _try_oa_location_urls(
                doi, _collect_openalex_pdf_urls(data), save_path, verbose
            ):
                if (verbose): print(f"✅ OpenAlex success for {doi}")
                _record_source(_source_out, "openalex")
                return save_path
        # 404 and other non-200 are normal (work not in OpenAlex); no need to print
    except Exception as e:
        if (verbose): print(f"Error with OpenAlex: {e}")
        pass

    # ---------------- CORE (core.ac.uk) ----------------
    try:
        if try_core_fallback(doi, save_path, verbose):
            _record_source(_source_out, "core")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with CORE fallback: {e}")

    # ---------------- DOAJ ----------------
    try:
        if try_doaj_fallback(doi, save_path, verbose):
            _record_source(_source_out, "doaj")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with DOAJ fallback: {e}")

    # ---------------- DataCite relation/content URLs ----------------
    try:
        if try_datacite_fallback(doi, save_path, verbose):
            _record_source(_source_out, "datacite")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with DataCite fallback: {e}")

    # ---------------- Wiley rendered fallback ----------------
    if use_playwright:
        try:
            if try_wiley_rendered_pdf_fallback(doi, save_path, verbose):
                _record_source(_source_out, "wiley")
                return save_path
        except Exception as e:
            if verbose:
                print(f"Error with Wiley rendered fallback: {e}")

    # ---------------- APA supplemental fallback ----------------
    try:
        if try_apa_supplemental_fallback(doi, save_path, verbose):
            _record_source(_source_out, "apa_supplemental")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with APA supplemental fallback: {e}")

    # ---------------- Direct DOI resolver ----------------
    try:
        resolved_url = f"https://doi.org/{doi}"
        request_headers = {**HTML_HEADERS}
        r = requests.get(resolved_url, headers=request_headers, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            # Direct PDF response
            if "application/pdf" in r.headers.get("content-type", "").lower():
                with open(save_path, "wb") as f:
                    f.write(r.content)
                if (verbose):  print(f"✅ Direct DOI PDF success for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path

            # Handle Crossref chooser page (multiple resolution)
            if "chooser.crossref.org" in r.url:
                # Extract primary-resource from debug JSON
                primary_match = re.search(r"'primary-resource':\s*'([^']+)'", r.text)
                if primary_match:
                    primary_resource = primary_match.group(1)
                    if verbose:
                        print(f"  Crossref chooser detected, following primary resource: {primary_resource}")
                    if try_landing_page_pdf_fallback(doi, primary_resource, save_path, verbose):
                        if (verbose): print(f"✅ Found PDF via Crossref chooser primary resource for {doi}")
                        _record_source(_source_out, "direct_doi")
                        return save_path

                # Fallback: try all resource-line links
                resource_links = re.findall(r'<div class="resource-line">.*?<a href="([^"]+)"', r.text, re.DOTALL)
                for resource_link in resource_links:
                    if verbose:
                        print(f"  Trying Crossref chooser resource: {resource_link}")
                    if try_landing_page_pdf_fallback(doi, resource_link, save_path, verbose):
                        if (verbose): print(f"✅ Found PDF via Crossref chooser resource for {doi}")
                        _record_source(_source_out, "direct_doi")
                        return save_path

            # Parse and follow landing-page PDF/download links.
            if try_landing_page_pdf_fallback(doi, r.url, save_path, verbose):
                if (verbose): print(f"✅ Found PDF via DOI landing-page parsing for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path
        # doi.org may redirect to a 404 (e.g. Acta Biochimica Polonica migrated to Frontiers Partnerships)
        if r.status_code == 404 and doi.lower().startswith("10.18388/abp"):
            fp_url = f"https://www.frontierspartnerships.org/journals/acta-biochimica-polonica/articles/{doi}/pdf"
            if try_download(fp_url, save_path, verbose):
                if verbose:
                    print(f"✅ Frontiers Partnerships (migrated ABP) success for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path
    except Exception as e:
        if (verbose): print(f"Error with Direct DOI: {e}")
        pass

    # ---------------- DataCite related identifiers fallback ----------------
    # IsSupplementTo: supplemental material → try to get supplement first, then main paper as fallback
    # IsIdenticalTo: same content, different ID (e.g. versioned 10.25384/sage.11807913.v1 → 10.25384/sage.11807913)
    # IsVersionOf: newer version of this DOI (e.g. preprint → published)
    #
    # Same prefix gate as try_datacite_fallback, and the same reason: this was the
    # SECOND unconditional api.datacite.org call per record, and the relation types
    # it looks for only exist on repository deposits anyway. Gating the request
    # rather than wrapping the block keeps the diff to two lines.
    try:
        r = (
            requests.get(f"https://api.datacite.org/dois/{quote_plus(doi)}", timeout=10)
            if _is_datacite_prefix(doi) else None
        )
        if r is not None and r.status_code == 200:
            rels = (r.json().get("data") or {}).get("attributes") or {}
            fallback_types = [
                ("issupplementto", "main paper (IsSupplementTo)"),
                ("isidenticalto", "identical (IsIdenticalTo)"),
                ("isversionof", "newer version (IsVersionOf)"),
            ]
            for rel_type, label in fallback_types:
                for rid in (rels.get("relatedIdentifiers") or []):
                    if (rid.get("relationType") or "").lower() == rel_type:
                        alt_doi = rid.get("relatedIdentifier")
                        if alt_doi and alt_doi.startswith("10.") and alt_doi != doi:
                            if rel_type == "issupplementto":
                                # Try to get the actual supplementary material first (what user requested)
                                supp_url = rels.get("url")
                                if supp_url and verbose:
                                    print(f"  Attempting supplementary material from: {supp_url[:80]}...")
                                got_supplement = False
                                if supp_url:
                                    supp_candidates = _collect_datacite_candidate_urls(rels)
                                    if not supp_candidates:
                                        supp_candidates = [supp_url]
                                    for cand in supp_candidates[:5]:
                                        if try_download(cand, save_path, verbose):
                                            got_supplement = True
                                            break
                                        if try_download_with_session(cand, save_path, referer=supp_url, verbose=verbose):
                                            got_supplement = True
                                            break
                                    if not got_supplement:
                                        m = re.search(r"figshare\.com/articles/[^/]+/(\d+)", supp_url, re.I)
                                        if m:
                                            art_id = m.group(1)
                                            base = supp_url.split("/articles")[0]
                                            ndl_url = f"{base}/ndownloader/articles/{art_id}"
                                            if try_download(ndl_url, save_path, verbose):
                                                got_supplement = True
                                if got_supplement:
                                    if verbose:
                                        print(f"✅ Supplementary material success for {doi}")
                                    _record_source(_source_out, "datacite_related")
                                    return save_path
                                # Could not get supplementary material—warn and try main paper
                                _print_yellow_warning(
                                    f"⚠️  Could not access supplementary material ({doi}). "
                                    f"It may be behind access controls or require browser interaction. "
                                    f"Fetching main paper {alt_doi} instead."
                                )
                            if verbose:
                                print(f"  Trying {label}: {alt_doi}")
                            alt_result = fetch_pdf(
                                alt_doi, save_path, email, verbose, delay=0, use_playwright=use_playwright, _source_out=_source_out, _visited=_visited
                            )
                            if alt_result:
                                if verbose:
                                    print(f"✅ {label} success for {doi}")
                                _record_source(_source_out, "datacite_related")
                                return save_path
                            break  # only try first match per type
    except Exception as e:
        if verbose:
            print(f"Error with DataCite related identifiers fallback: {e}")


    # ---------------- DOI→PMID fallback (before last resorts) ----------------
    # When DOI flow fails, try PMID-native sources (PubMed page citation_pdf_url, etc.)
    if not os.path.exists(save_path):
        pmid = _pmid_cache[0]
        if pmid is None:
            pmid = doi_to_pmid(doi, verbose=verbose)
        if pmid:
            pmid_str = str(pmid).strip()
            pmid_got = try_pmid_direct_pdf_fallback(
                pmid_str, save_path, verbose, allow_xml=allow_xml_fallback
            )
            if pmid_got:
                _record_source(_source_out, "pmid_direct")
                return pmid_got if isinstance(pmid_got, str) else save_path


    # ---------------- Google Scholar via SerpApi ---------------------------
    # Preserve search credits until the ordinary PDF sources are exhausted.
    if try_serpapi_scholar_fallback(doi, save_path, verbose):
        _record_source(_source_out, "serpapi_scholar")
        return save_path

    # ---------------- Deferred Elsevier fallback (PDF, then XML) ------------
    # Last resort for Elsevier DOIs when every other route has failed. Tries
    # the PDF representation first and only settles for XML after that.
    #
    # allow_xml_fallback gates the XML half ONLY. --no-xml-fallback means "this
    # pipeline ingests PDFs and cannot read a .xml file" -- it is not a reason
    # to skip a route that returns an actual PDF, so the PDF attempt runs
    # either way.
    if elsevier_crossref_message is not None:
        try:
            elsevier_path = try_elsevier_fulltext_api_fallback(
                doi, save_path, crossref_message=elsevier_crossref_message,
                verbose=verbose, allow_xml=allow_xml_fallback,
            )
            if elsevier_path:
                _record_source(_source_out, "elsevier")
                return elsevier_path
        except Exception as e:
            if verbose:
                print(f"Error with deferred Elsevier fallback: {e}")


def _append_missing_si_to_report(output_dir, si_statuses, run_timestamp, lock,
                                 drafts_by_doi=None, figure_statuses=None):
    """Add a "missing materials" section to missing_pdfs.html.

    The report was PDF-only, so a record whose paper arrived but whose data did
    not never appeared -- the one artifact a user opens after a run was silent
    about half the gaps.

    Written ONCE at end of run rather than appended per record like the PDF
    path: supplementary status is not known until _pull_si has finished for
    every record, so there is nothing to append while the run is in flight.

    Click-through cases lead the table. A record the publisher will serve to a
    human but refused our client is worth one click in a browser, which beats
    an email and a wait -- so those sort first and carry a badge. Where the
    refusal was recorded with the URL that produced it, that URL is printed:
    "fetch by hand" is only actionable if it says WHAT to fetch, and sending
    somebody to the DOI to hunt for a supplement they cannot name is most of
    the work still undone.

    Figures share the table rather than getting a second report. A figure PMC
    refused and a supplement Atypon refused are the same problem with the same
    answer, and splitting them would mean a person had two lists to read.
    """
    rows = []
    for display_id, summary in (si_statuses or {}).items():
        status = getattr(summary, "status", "")
        blocked_urls = list(getattr(summary, "blocked_urls", None) or [])
        if status not in ("partial", "incomplete"):
            continue
        missing = ", ".join(getattr(summary, "missing_declared", None) or []) or "—"
        # A withheld-but-existing supplement is the click-through candidate:
        # the publisher has it and serves humans. A recorded 403 says so
        # outright rather than by inference.
        manual = bool(blocked_urls) or (
            status == "partial" and not getattr(summary, "missing_declared", None))
        rows.append((manual, display_id, status, missing, blocked_urls))

    for display_id, summary in (figure_statuses or {}).items():
        blocked_urls = list(getattr(summary, "blocked_urls", None) or [])
        if not blocked_urls:
            # A figure that simply is not in PMC is not a click-through case,
            # and listing it here would send a person after something no
            # browser will produce either.
            continue
        rows.append((True, display_id, "figures blocked", "—", blocked_urls))
    if not rows:
        return None

    rows.sort(key=lambda r: (not r[0], r[1]))
    manual_count = sum(1 for r in rows if r[0])

    body = []
    for manual, display_id, status, missing, blocked_urls in rows:
        badge = ('<span style="background:#b45309;color:#fff;padding:2px 6px;'
                 'border-radius:3px;font-size:11px">CLICK-THROUGH</span>'
                 if manual else "")
        link = html.escape(f"https://doi.org/{display_id}")
        draft = (drafts_by_doi or {}).get(display_id)
        draft_cell = (f'<a href="{html.escape(draft)}">draft email</a>'
                      if draft else "—")
        by_hand = "<br>".join(
            f'<a href="{html.escape(url)}" target="_blank" '
            f'rel="noopener noreferrer">fetch by hand</a>'
            for url in blocked_urls[:5]) or "—"
        body.append(
            f"            <tr>\n"
            f"                <td>{badge}</td>\n"
            f'                <td><a href="{link}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(str(display_id))}</a></td>\n'
            f"                <td>{html.escape(status)}</td>\n"
            f"                <td>{html.escape(missing)}</td>\n"
            f"                <td>{by_hand}</td>\n"
            f"                <td>{draft_cell}</td>\n"
            f"            </tr>\n")

    lead = (f"<p><strong>{manual_count} of these may need only one click from "
            f"you</strong> — the publisher serves humans but refused this "
            f"client. Open the link in your own browser.</p>"
            if manual_count else "")
    section = f"""
    <section class="run">
        <h2>Missing supplementary material and figures — {run_timestamp}</h2>
        {lead}
        <table>
            <thead>
                <tr><th></th><th>Identifier</th><th>Status</th>
                    <th>Declared but not obtained</th><th>Refused to us</th>
                    <th>Request</th></tr>
            </thead>
            <tbody>
{''.join(body)}            </tbody>
        </table>
    </section>
"""
    html_file = os.path.join(output_dir, "missing_pdfs.html")
    marker = "<!-- MISSING_PDFS_RUNS -->"
    with lock:
        try:
            if os.path.exists(html_file):
                with open(html_file, encoding="utf-8") as handle:
                    document = handle.read()
                document = (document.replace(marker, marker + section)
                            if marker in document else document + section)
            else:
                # A run with perfect PDF recall but missing SI would otherwise
                # report nothing at all.
                document = (f"<!DOCTYPE html>\n<html lang=\"en\"><head>"
                            f"<meta charset=\"UTF-8\"><title>Missing materials</title>"
                            f"</head><body><h1>Missing materials</h1>{marker}"
                            f"{section}</body></html>\n")
            with open(html_file, "w", encoding="utf-8") as handle:
                handle.write(document)
        except OSError:
            return None
    return html_file


def _append_missing_to_report(output_dir, identifier, run_timestamp, lock):
    """
    Append a single failed identifier to missing_pdfs.html on the fly.
    Thread-safe: uses lock for file read/modify/write.
    """

    html_file = os.path.join(output_dir, "missing_pdfs.html")
    marker = "<!-- MISSING_PDFS_RUNS -->"

    safe_id = doi_to_safe_filename(identifier)
    expected_filename = f"{safe_id}.pdf"
    escaped_id = html.escape(identifier)
    escaped_filename = html.escape(expected_filename)
    pmid = extract_pmid(identifier)
    if pmid:
        link_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    else:
        link_url = f"https://doi.org/{identifier}"
    escaped_link = html.escape(link_url)
    row_html = f"""            <tr>
                <td><a href="{escaped_link}" target="_blank" rel="noopener noreferrer">{escaped_id}</a></td>
                <td><code>{escaped_filename}</code></td>
            </tr>
"""

    run_section = f"""
    <section class="run">
        <h2>Run: {run_timestamp}</h2>
        <p><strong>Missing in this run:</strong> 1</p>
        <table>
            <thead>
                <tr>
                    <th>Identifier (DOI/PMID)</th>
                    <th>Filename</th>
                </tr>
            </thead>
            <tbody>
{row_html}            </tbody>
        </table>
    </section>
"""

    with lock:
        escaped_output_dir = html.escape(output_dir)
        if not os.path.exists(html_file):
            full_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Missing PDFs Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f7f7f7;
            color: #1f2937;
        }}
        h1 {{ margin-bottom: 8px; }}
        .summary {{ margin-bottom: 20px; color: #4b5563; }}
        .run {{
            background: #fff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 14px;
            margin-bottom: 16px;
        }}
        .run h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
        .run p {{ margin: 0 0 10px 0; color: #4b5563; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
        th {{ font-weight: 600; color: #374151; background: #f9fafb; }}
        td code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>Missing PDFs Report</h1>
    <p class="summary">Output directory: <code>{escaped_output_dir}</code></p>
{run_section}
{marker}
</body>
</html>
"""
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(full_doc)
            return

        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        run_header = f"Run: {run_timestamp}"
        run_start = content.find(run_header)
        if run_start != -1:
            tbody_end = content.find("</tbody>", run_start)
            if tbody_end != -1:
                new_content = content[:tbody_end] + row_html + content[tbody_end:]
                section_end = content.find("</section>", run_start)
                if section_end != -1:
                    section = new_content[run_start:section_end]
                    section = re.sub(
                        r"(Missing in this run:</strong> )(\d+)",
                        lambda m: m.group(1) + str(int(m.group(2)) + 1),
                        section,
                        count=1,
                    )
                    new_content = new_content[:run_start] + section + new_content[section_end:]
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(new_content)
                return

        updated = content.replace(
            marker,
            run_section.rstrip() + "\n" + marker,
            1,
        )
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(updated)


def _append_failed_to_csv(output_dir, doi, category, detail, lock):
    """
    Append a single failed DOI to failed_dois.csv for machine-readable retry.
    Thread-safe. The resulting CSV can be passed directly to batch_fetch_pdfs()
    on a retry run, since it contains a "doi" column.

    Columns: timestamp, doi, category, detail
    """
    from datetime import datetime
    import csv as _csv

    csv_path = os.path.join(output_dir, "failed_dois.csv")
    with lock:
        is_new = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            if is_new:
                writer.writerow(["timestamp", "doi", "category", "detail"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                doi,
                category or "all_sources_failed",
                (detail or "")[:500],
            ])

def _prune_empty_record_dir(record_dir):
    """Drop a --make-subfolder directory the record never wrote anything into.

    The per-record directory has to be created *before* the download starts --
    nothing down the writing chain calls makedirs -- so a record that fails
    every source would otherwise leave an empty folder behind, and a run over a
    few thousand missing DOIs would bury the hits in empty directories.
    os.rmdir rather than rmtree, and unconditional rather than only-on-failure:
    a directory with anything at all in it (a PDF, an abstract, a partial the
    engine left) is a directory this must not touch, and the emptiness test is
    the one check that gets that right without trusting a success flag.
    """
    if not record_dir:
        return
    try:
        os.rmdir(record_dir)
    except OSError:
        pass  # non-empty (the normal success case), gone already, or in use


def _read_csv_column(path, column, case_insensitive=True):
    """Read one column from a CSV as a list of stripped, non-empty strings.

    Returns None if the column is missing so callers can raise or print
    their own error. utf-8-sig so Excel-exported CSVs with a BOM still
    match on the first header name.
    """
    import csv
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if case_insensitive:
            col_map = {c.lower(): c for c in fieldnames}
            actual = col_map.get(column.lower())
        else:
            actual = column if column in fieldnames else None
        if actual is None:
            return None
        return [v.strip() for row in reader
                if (v := row.get(actual)) is not None and v.strip()]


def batch_fetch_pdfs(dois, output_dir, email=None, verbose=False, delay=0.1, workers=1, create_missing_report=True, track_source=False, start_offset=0, abstract_if_no_pdf=False, abstract_only=False, allow_xml_fallback=True, use_playwright=False, prioritize_xml=False, xml_only=False, xml_html_only=False, get_xml_or_html=False, to_markdown=False, extract_images=False, pull_figures=False, target_task="extraction", upgrade_existing=False, want_provenance=False, pull_supplementary=False, refresh_supplementary=False, max_supplementary_bytes=None, shared_resolver=None, record_timeout=1200, batch_timeout=None, make_subfolder=False, download_data_artifacts=False, max_data_artifact_bytes=None, llm_adjudicate_artifacts=False, llm_agent_retrieval=False, llm_backend=None, max_llm_records=0, llm_model=None, unpack_data_artifacts=False, download_related_unverified=False, draft_requests=False, json_out=None):
    """
    Download PDFs for multiple DOIs with optional parallel processing.

    Args:
        dois: List of DOIs to download or path to CSV file
        output_dir: Directory to save PDFs
        email: Email for API calls (defaults to EMAIL from .env.local)
        verbose: Print detailed progress
        delay: Delay between API calls per worker
        workers: Number of parallel workers (1 = sequential)
        create_missing_report: Create HTML report for missing PDFs (default: True)
        track_source: If True, write CSV (pdf, source) and JSON (source counts) to output_dir
        pull_supplementary: Also fetch every supplementary file for each record, as
            {stem}_supplementary_info_N.ext siblings plus a manifest. Runs after
            retrieval on both the default chain and the tiered path, including for
            records already on disk. Never affects whether a record succeeded.
        refresh_supplementary: Re-run the supplementary pass over records that already
            have a manifest, picking up files deposited since.
        max_supplementary_bytes: Per-file cap for the above (default 300 MB).

    Returns:
        List of tuples: (doi, success, save_path) or (doi, success, save_path, source) when track_source
    """
    # Use email from .env.local if not provided
    if email is None:
        email = _DEFAULT_EMAIL

    # Gated, not unconditional: with the flag off the retrieval package must not
    # even be imported. See the note on the opt-in flags in fetch_pdf.
    if pull_supplementary and max_supplementary_bytes is None:
        from .retrieval.supplementary import DEFAULT_MAX_FILE_BYTES

        max_supplementary_bytes = DEFAULT_MAX_FILE_BYTES

    import io
    import sys

    # Force UTF-8 output so a Greek letter or an em dash in a title cannot raise
    # 'latin-1' codec errors mid-batch.
    #
    # Only when it is actually needed, and always put back. Re-wrapping an already
    # UTF-8 stream (the norm on Linux) is pure downside: the wrapper we discard
    # CLOSES the buffer it was built on when it is garbage-collected, so a second
    # batch_fetch_pdfs call in the same process printed to a dead stream, and any
    # test that called this function killed the rest of the session with
    # "ValueError: I/O operation on closed file". _restore_stdio in the finally
    # below is what makes this function safe to call twice.
    _saved_stdout, _saved_stderr = sys.stdout, sys.stderr

    def _needs_utf8_wrapper(stream):
        return (
            hasattr(stream, "buffer")
            and (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            not in ("utf8",)
        )

    if _needs_utf8_wrapper(sys.stdout):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    if _needs_utf8_wrapper(sys.stderr):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    def _restore_stdio():
        """Detach any wrapper we installed before dropping it.

        detach() hands the buffer back instead of closing it on collection, which
        is the whole bug. Restoring the saved references alone is not enough.
        """
        for installed, saved in ((sys.stdout, _saved_stdout), (sys.stderr, _saved_stderr)):
            if installed is not saved and isinstance(installed, io.TextIOWrapper):
                try:
                    installed.flush()
                    installed.detach()
                except (ValueError, AttributeError):
                    pass
        sys.stdout, sys.stderr = _saved_stdout, _saved_stderr

    try:
        return _batch_fetch_pdfs_inner(
            dois=dois, output_dir=output_dir, email=email, verbose=verbose,
            delay=delay, workers=workers, create_missing_report=create_missing_report,
            track_source=track_source, start_offset=start_offset,
            abstract_if_no_pdf=abstract_if_no_pdf, abstract_only=abstract_only,
            allow_xml_fallback=allow_xml_fallback, use_playwright=use_playwright,
            prioritize_xml=prioritize_xml, xml_only=xml_only,
            xml_html_only=xml_html_only, get_xml_or_html=get_xml_or_html,
            to_markdown=to_markdown,
            extract_images=extract_images,
            pull_figures=pull_figures,
            target_task=target_task, upgrade_existing=upgrade_existing,
            want_provenance=want_provenance, pull_supplementary=pull_supplementary,
            refresh_supplementary=refresh_supplementary,
            max_supplementary_bytes=max_supplementary_bytes,
            shared_resolver=shared_resolver,
            record_timeout=record_timeout, batch_timeout=batch_timeout,
            download_data_artifacts=download_data_artifacts,
            max_data_artifact_bytes=max_data_artifact_bytes,
            llm_adjudicate_artifacts=llm_adjudicate_artifacts,
            llm_agent_retrieval=llm_agent_retrieval,
            llm_backend=llm_backend,
            max_llm_records=max_llm_records,
            llm_model=llm_model,
            unpack_data_artifacts=unpack_data_artifacts,
            download_related_unverified=download_related_unverified,
            draft_requests=draft_requests,
            make_subfolder=make_subfolder,
            json_out=json_out,
        )
    finally:
        _restore_stdio()


def _batch_fetch_pdfs_inner(dois, output_dir, email=None, verbose=False, delay=0.1, workers=1, create_missing_report=True, track_source=False, start_offset=0, abstract_if_no_pdf=False, abstract_only=False, allow_xml_fallback=True, use_playwright=False, prioritize_xml=False, xml_only=False, xml_html_only=False, get_xml_or_html=False, to_markdown=False, extract_images=False, pull_figures=False, target_task="extraction", upgrade_existing=False, want_provenance=False, pull_supplementary=False, refresh_supplementary=False, max_supplementary_bytes=None, shared_resolver=None, record_timeout=1200, batch_timeout=None, make_subfolder=False, download_data_artifacts=False, max_data_artifact_bytes=None, llm_adjudicate_artifacts=False, llm_agent_retrieval=False, llm_backend=None, max_llm_records=0, llm_model=None, unpack_data_artifacts=False, download_related_unverified=False, draft_requests=False, json_out=None):
    """The body of batch_fetch_pdfs, split out so stdio restoration is guaranteed.

    Everything below is unchanged; the only reason for the split is that the
    wrapper above needs a try/finally around the whole thing.
    """
    import os
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    os.makedirs(output_dir, exist_ok=True)

    # If dois is a string, assume it's a CSV file path
    if isinstance(dois, str):
        dois = _read_csv_column(dois, 'DOI')
        if dois is None:
            raise ValueError("CSV must have a 'DOI' column (case-insensitive)")

    results = []
    started_at = time.monotonic()
    progress_interval = 20

    # ---------------- Batched resolution (format-prioritized mode only) -------
    # The ID Converter takes up to 200 ids per call, so resolving a batch up
    # front costs 6 HTTP calls per 1000 records instead of 1000. Doing it
    # per-record inside the worker would throw that away, which is why the
    # resolver is built here and threaded down rather than created on demand.
    _resolver = None
    _ladder = None
    _tiered = prioritize_xml or xml_only or xml_html_only or get_xml_or_html

    # Two gates, deliberately: which suffixes count as "already downloaded" is a
    # property of the tiered path only (see skip_check_extensions). The shared
    # resolver is a separate question.
    ARTIFACT_EXTENSIONS = skip_check_extensions(_tiered)

    if _tiered or pull_supplementary:
        from .retrieval.cache import ResolutionCache
        from .retrieval.http import HttpClient
        from .retrieval.ratelimit import HostRateLimiter, shared_host_limiter
        from .retrieval.resolve import BatchResolver
        from .retrieval.tiers import load_ladder

        _ladder = load_ladder()
        if shared_resolver is not None:
            # A caller running many small batches in parallel -- one per output
            # directory -- must pass its own resolver, or every batch builds a
            # private HostRateLimiter and the configured per-host limits are
            # multiplied by the number of concurrent batches. Fifteen parallel
            # callers would turn NCBI's 3/s into 45/s. Sharing one resolver also
            # shares one resolution cache and one batched ID Converter pass.
            _resolver = shared_resolver
        else:
            _resolver = BatchResolver(
                HttpClient(shared_host_limiter(_ladder.rate_limits), email=email, verbose=verbose),
                ResolutionCache.for_output_dir(output_dir, verbose),
                _ladder,
                verbose,
            )
        print(f"🔎 Resolving identifiers for {len(dois)} record(s)...")
        _resolver.prime([str(d).strip() for d in dois])

    # Titles and page ranges for the identity check, in bulk, before any worker
    # starts. Every record needs one, so asking per record made verification the
    # most rate-limit-hungry step in the tool -- and Crossref allows 10/s, which
    # ten workers reach instantly. Fifty DOIs per request turns a 10,000-record
    # run's 10,000 metadata calls into 200. Unconditional: the legacy chain needs
    # this every bit as much as the tiered walk, and it is the legacy chain that
    # had no rate limiting at all.
    if len(dois) > 1:
        print(f"🔎 Fetching titles for {len(dois)} record(s) (identity check)...")
        prime_record_metadata([str(d).strip() for d in dois], verbose=verbose)

    # Thread-local storage for prefix
    _thread_prefix = threading.local()
    _print_lock = threading.Lock()
    _missing_report_lock = threading.Lock()
    # Running counter of failures by category, for stats display (Fix #7).
    # Uses the missing_report_lock above for thread-safe updates.
    from collections import Counter as _Counter
    _failure_counter = _Counter()
    # Running composition of what we are actually getting: xml vs html vs pdf.
    # Shares the missing-report lock rather than taking a third one -- both are
    # updated once per record, so contention is not the concern.
    _format_counter = _Counter()

    def _tally(path=None):
        """Count one artifact (none for a failed record) and return the running
        tally as a string, missing column included.

        Snapshotted inside the lock so a parallel worker cannot print a tally
        that never existed (two increments landing between read and format).
        """
        label = format_label(path) if path else None
        with _missing_report_lock:
            if label:
                _format_counter[label] += 1
            return running_tally(_format_counter, missing=sum(_failure_counter.values()))

    # One HttpClient per worker thread, all sharing the batch's single
    # HostRateLimiter. The limiter is lock-guarded but not a singleton, so a
    # client built per record would give every worker its own token buckets and
    # multiply the configured per-host rate by the worker count -- on what is by
    # far the most request-heavy path in the tool. The session is per-thread
    # because requests.Session is not documented thread-safe and supplementary
    # transfers are long-lived streams through it.
    _si_local = threading.local()

    #: display_id -> SupplementarySummary, for the end-of-run accounting. A
    #: plain dict is safe here: workers only ever assign distinct keys, and
    #: assignment is atomic under the GIL.
    _si_statuses = {}

    #: display_id -> FiguresSummary, for the same report. Kept separate rather
    #: than merged into _si_statuses: the two carry different fields, and a
    #: reader of either dict should not have to ask which pass wrote a row.
    _figure_statuses = {}

    def _si_http():
        client = getattr(_si_local, "client", None)
        if client is None:
            from .retrieval.http import HttpClient

            client = HttpClient(_resolver.http.limiter, email=email, verbose=verbose)
            _si_local.client = client
        return client

    def _pull_images(path):
        """Embedded-image dump for one PDF. Never changes the verdict.

        Same reason as _pull_si: a missing PyMuPDF or a corrupt PDF must not
        turn a successful fetch into a failed record. Fires on the already-
        exists skip branch -- "I have 5,000 PDFs, now dump their images" is
        the primary use of --extract-images, and fetch_pdf is never called
        there.
        """
        if not extract_images or not path:
            return
        pdf = path if str(path).lower().endswith(".pdf") else (
            os.path.splitext(path)[0] + ".pdf"
        )
        if not str(pdf).lower().endswith(".pdf") or not os.path.exists(pdf):
            return
        try:
            from .retrieval.extract_images import extract_images as _xi
            _xi(pdf, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  images: {os.path.basename(pdf)} failed ({e})")

    def _pull_figures(display_id, raw_id, canonical, save_path):
        """Published figure images for one record. Never changes the verdict.

        Wrapped whole for the same reason as _pull_si, and run on the
        already-exists branch for the same reason as _pull_images: a corpus
        fetched before this flag existed is the main thing anyone points it at,
        and fetch_pdf is never called there.
        """
        if not pull_figures or not save_path:
            return
        try:
            from .retrieval.figures import pull_for_record as pull_figures_for_record

            summary = pull_figures_for_record(
                raw_identifier=raw_id,
                doi=canonical,
                save_path=save_path,
                resolver=_resolver,
                http=_si_http(),
                ladder=_ladder,
                verbose=verbose,
                email=email,
                delay=delay,
            )
            _figure_statuses[display_id] = summary
            if summary.written and summary.status != "skipped":
                print(f"   🖼️  {display_id}: {summary.written} figure(s)"
                      + (f", {summary.failed} not obtained" if summary.failed else ""))
            elif verbose:
                print(f"   🖼️  {display_id}: {summary.status} "
                      f"({summary.refusal or summary.detail or 'no detail'})")
        except Exception as e:
            print(f"   🖼️  {display_id}: figure pass failed ({str(e)[:120]})")

    def _pull_si(display_id, raw_id, canonical, save_path):
        """Supplementary pass for one record. Never changes the verdict.

        Wrapped whole, because an exception escaping here would escape
        download_one, escape future.result(), and take down the entire batch --
        thousands of records lost because one repository served malformed JSON.
        """
        if not pull_supplementary or not save_path:
            return
        try:
            from .retrieval.supplementary import pull_for_record

            summary = pull_for_record(
                raw_identifier=raw_id,
                doi=canonical,
                save_path=save_path,
                resolver=_resolver,
                http=_si_http(),
                ladder=_ladder,
                max_file_bytes=max_supplementary_bytes,
                refresh=refresh_supplementary,
                verbose=verbose,
                email=email,
                delay=delay,
                use_playwright=use_playwright,
                download_data_artifacts=download_data_artifacts,
                max_data_artifact_bytes=max_data_artifact_bytes,
                llm_adjudicate_artifacts=llm_adjudicate_artifacts,
                llm_agent_retrieval=llm_agent_retrieval,
                llm_backend=llm_backend,
                max_llm_records=max_llm_records,
                llm_model=llm_model,
                unpack_data_artifacts=unpack_data_artifacts,
                download_related_unverified=download_related_unverified,
            )
            _si_statuses[display_id] = summary
            if summary.status == "incomplete":
                # Named data loss -- a file the paper's own XML declares was
                # not obtained. Printed regardless of verbosity: the whole
                # point of the gate is that this cannot pass silently.
                names = ", ".join(summary.missing_declared) or "unknown"
                print(f"   ⚠️  {display_id}: SI INCOMPLETE — missing: {names}")
            elif summary.written and summary.status != "skipped":
                extra = f", {summary.skipped} skipped" if summary.skipped else ""
                print(f"   📎 {display_id}: {summary.written} supplementary file(s){extra}")
            elif verbose:
                print(f"   📎 {display_id}: {summary.status} ({summary.detail or 'no detail'})")
        except Exception as e:
            print(f"   📎 {display_id}: supplementary pass failed ({str(e)[:120]})")

    _run_timestamp = None
    if create_missing_report:
        from datetime import datetime
        _run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _original_stdout = sys.stdout

    class PrefixedOutput:
        """Wrapper that adds prefix to all output."""
        def __init__(self, original):
            self.original = original

        def write(self, text):
            if text.strip():
                prefix = getattr(_thread_prefix, 'value', '')
                if prefix:
                    with _print_lock:
                        self.original.write(f"{prefix} {text}")
                else:
                    self.original.write(text)
            else:
                self.original.write(text)

        def flush(self):
            self.original.flush()

    def download_one(doi, idx, total):
        """Download one record, registered in-flight so a stall is nameable."""
        _inflight_start(idx, str(doi).strip())
        # Filled in by the inner call once it knows the record's directory, so
        # the cleanup below runs even when that call raises.
        made_dir = []
        try:
            return _download_one_inner(doi, idx, total, made_dir)
        finally:
            _inflight_end(idx)
            if made_dir:
                _prune_empty_record_dir(made_dir[0])

    def _download_one_inner(doi, idx, total, made_dir=None):
        """Download a single DOI/PMID with progress tracking."""
        # idx is 0-based within the processed list, add start_offset for actual CSV row
        actual_row = start_offset + idx + 1
        total_rows = start_offset + total
        prefix = f"[{actual_row}/{total_rows}]"
        _thread_prefix.value = prefix

        raw_identifier = str(doi).strip()
        canonical_doi = resolve_identifier_to_doi(raw_identifier, verbose=verbose)
        pmid = extract_pmid(raw_identifier)

        # Use PMID digits for filename when DOI resolution fails
        if canonical_doi:
            display_id = canonical_doi
        elif pmid:
            display_id = pmid  # Just the digits, not "pmid:..." or "PMID ..."
        else:
            display_id = raw_identifier

        safe_doi = doi_to_safe_filename(display_id)
        # --make-subfolder gives each record its own directory named by the same
        # encoder as the files, so folder and stem match:
        #   {output_dir}/10.1073--pnas.123/10.1073--pnas.123.pdf
        # Every artifact derived from save_path (XML, provenance, linked
        # artifacts, supplementary, markdown) follows for free. The abstract
        # writers do NOT -- they take a directory, so they get record_dir below.
        # Created here rather than at batch start: workers reach this line
        # before any per-record directory exists, and the default-chain PDF
        # writes do not makedirs on their own.
        record_dir = os.path.join(output_dir, safe_doi) if make_subfolder else output_dir
        if make_subfolder:
            os.makedirs(record_dir, exist_ok=True)
            if made_dir is not None:
                made_dir.append(record_dir)
        save_path = os.path.join(record_dir, f"{safe_doi}.pdf")

        # Always allocated, not just under --tracksource: the success line names
        # the acquiring source, and that should not depend on an unrelated flag.
        source_out = [None]
        paths_out = [] if _tiered else None

        ab_path = os.path.splitext(save_path)[0] + "_abstract.md"

        if abstract_only:
            if os.path.exists(ab_path):
                print(f"⏭️  Skipping {display_id} (abstract already exists)")
                _tally(ab_path)  # counted, but the line above stays terse
                _record_json(idx, raw_identifier, canonical_doi, ab_path,
                             JSON_STATUS_ALREADY_ON_DISK, source="existing")
                if track_source:
                    return (display_id, True, ab_path, "existing")
                return (display_id, True, ab_path)
            reason = "no abstract found"
            try:
                from .fetch_abstract_from_doi import save_abstract_markdown
                saved = save_abstract_markdown(display_id, record_dir, email=email, verbose=verbose)
                if saved:
                    print(f"📄 {display_id} - abstract saved")
                    _tally(saved)
                    _record_json(idx, raw_identifier, canonical_doi, saved,
                                 JSON_STATUS_DOWNLOADED, source="abstract")
                    if track_source:
                        return (display_id, True, saved, "abstract")
                    return (display_id, True, saved)
                else:
                    print(f"❌ {display_id} - no abstract found")
            except Exception as e:
                print(f"❌ {display_id} - abstract error: {e}")
                reason = f"abstract error: {str(e)[:150]}"
            _record_json(idx, raw_identifier, canonical_doi, None,
                         JSON_STATUS_FAILED, reasons=[reason])
            if track_source:
                return (display_id, False, None, None)
            return (display_id, False, None)

        # In tiered mode the record may already be on disk as any of the
        # artifact formats, not just .pdf/.xml. --upgrade-existing defers the
        # decision to the engine, which will only write on a better tier.
        stem = os.path.splitext(save_path)[0]
        existing_file = None
        # Both --upgrade-existing and --get-xml-or-html defer the
        # already-have-it decision to the engine, which reasons per goal: one
        # looks for a better tier, the other for the *missing half* of a pair.
        # Skipping here on any single artifact would defeat both -- a directory of
        # PDFs would report every record "already exists" and never gain an XML.
        if not defers_existing_to_engine(_tiered, upgrade_existing, get_xml_or_html):
            for _ext in ARTIFACT_EXTENSIONS:
                candidate = save_path if _ext == ".pdf" else stem + _ext
                if os.path.exists(candidate):
                    existing_file = candidate
                    break
        if existing_file:
            print(f"[{_tally(existing_file)}] ⏭️  Skipping {display_id} (already exists: "
                  f"{os.path.basename(existing_file)})")
            # "I already have 5,000 PDFs, now go and get the supplements" is the
            # main reason this flag exists, and this return is the one that would
            # otherwise swallow it: fetch_pdf is never called on this
            # branch, so a hook inside it would do nothing here.
            _pull_si(display_id, raw_identifier, canonical_doi, save_path)
            _pull_figures(display_id, raw_identifier, canonical_doi, save_path)
            _pull_images(existing_file)
            # Nothing was fetched and nothing re-read the file, so identity
            # stays null: this branch cannot tell a stale artifact from a good
            # one, and the status is what says so.
            _record_json(idx, raw_identifier, canonical_doi, existing_file,
                         JSON_STATUS_ALREADY_ON_DISK, source="existing")
            if track_source:
                return (display_id, True, existing_file, "existing")
            return (display_id, True, existing_file)

        print(f"📥 Downloading {display_id}...")
        result = fetch_pdf(
            canonical_doi or raw_identifier, save_path, email, verbose, delay,
            allow_xml_fallback=allow_xml_fallback,
            use_playwright=use_playwright, _source_out=source_out,
            _paths_out=paths_out,
            prioritize_xml=prioritize_xml, xml_only=xml_only,
            xml_html_only=xml_html_only, get_xml_or_html=get_xml_or_html,
            to_markdown=to_markdown,
            target_task=target_task, upgrade_existing=upgrade_existing,
            want_provenance=want_provenance, _resolver=_resolver,
        )

        # Defensive guard: a downstream save site may have written the file
        # to a path without the .pdf extension (e.g. some Playwright code
        # path that strips the suffix). If save_path is missing but the
        # same path without .pdf exists and starts with %PDF magic bytes,
        # rename it to add the extension.
        if save_path.lower().endswith(".pdf") and not os.path.exists(save_path):
            stripped = save_path[:-4]
            if os.path.exists(stripped) and os.path.isfile(stripped):
                try:
                    with open(stripped, "rb") as _f:
                        if _f.read(4) == b"%PDF":
                            os.rename(stripped, save_path)
                            if verbose:
                                print(f"  Renamed extension-less PDF → {save_path}")
                            if result is None:
                                result = save_path
                except Exception as _e:
                    if verbose:
                        print(f"  Extension-fix rename error: {_e}")

        success = result is not None

        # Deliberately after `success` is decided and never folded into it. Run
        # unconditionally, including when the paper could not be had: a paywalled
        # article with three retrievable .xlsx files is a better outcome than
        # nothing, and it is still reported as a PDF failure. Note that `result`
        # is not passed and not read -- _tally must never see a supplementary
        # path, or a record with four supplementary PDFs would report "pdf 5" and
        # corrupt the format composition tally.
        _pull_si(display_id, raw_identifier, canonical_doi, save_path)
        _pull_figures(display_id, raw_identifier, canonical_doi, save_path)
        _pull_images(result or save_path)

        if success:
            # A --get-xml-or-html record has two artifacts. Both are counted, and
            # the line names the pair ("xml+pdf") instead of only the best one.
            # paths_out's last element is the goal summary; the rest are paths.
            paths_out = paths_out or []
            label = paths_out[-1] if paths_out else ""
            for other in [p for p in paths_out[:-1] if p != result]:
                _tally(other)
            # Which source actually produced this is the first thing you want
            # when reading a run back -- otherwise the only way to tell whether
            # a record came from CORE or Unpaywall is to cross-reference
            # source_tracking.csv afterwards.
            src = source_out[0] if source_out and source_out[0] else None
            src_str = f" [{SOURCE_DISPLAY_NAMES.get(src, src)}]" if src else ""
            # The tally leads the line, right after the [i/n] prefix, so the
            # columns line up down a log and the state of the run is readable
            # at a glance without scanning to the end of each line.
            print(
                f"[{_tally(result)}] ✅ {display_id}{src_str} "
                f"[{label or format_label(result)}]"
            )
        else:
            # Counted before the line prints, so the record is already in the
            # missing column of its own line.
            with _missing_report_lock:
                _failure_counter["all_sources_failed"] += 1
            if abstract_if_no_pdf:
                try:
                    from .fetch_abstract_from_doi import save_abstract_markdown
                    ab_path = save_abstract_markdown(display_id, record_dir, email=email, verbose=verbose)
                    if ab_path:
                        print(f"[{_tally()}] 📄 {display_id} - no PDF, abstract saved: {os.path.basename(ab_path)}")
                    else:
                        print(f"[{_tally()}] ❌ {display_id} - Nothing worked 🙃🙃🙃")
                except Exception as e:
                    if verbose:
                        print(f"  Abstract fallback error: {e}")
                    print(f"[{_tally()}] ❌ {display_id} - Nothing worked 🙃🙃🙃")
            else:
                print(f"[{_tally()}] ❌ {display_id} - Nothing worked 🙃🙃🙃")
            if create_missing_report and _run_timestamp:
                _append_missing_to_report(output_dir, display_id, _run_timestamp, _missing_report_lock)
            # Always write machine-readable retry CSV (even without HTML report)
            try:
                _append_failed_to_csv(
                    output_dir, display_id,
                    category="all_sources_failed",
                    detail="",
                    lock=_missing_report_lock,
                )
            except Exception as _e:
                if verbose:
                    print(f"  failed_dois.csv append error: {_e}")

        identity, reasons = _identity_and_reasons(result, save_path)
        _record_json(
            idx, raw_identifier, canonical_doi, result if success else None,
            JSON_STATUS_DOWNLOADED if success else JSON_STATUS_FAILED,
            source=source_out[0] if success else None,
            identity=identity,
            reasons=reasons if (reasons or success)
            else ["no source produced a usable artifact"],
            # paths_out's last element is the tiered goal summary, not a path.
            extra_paths=[p for p in (paths_out or [])[:-1] if p != result],
        )
        if track_source:
            return (display_id, success, result if success else None, source_out[0] if success else None)
        return (display_id, success, result if success else None)

    total = len(dois)

    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hrs, rem = divmod(seconds, 3600)
        mins, secs = divmod(rem, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"

    def _print_rate_update(done_count: int, total_count: int, success_count: int):
        if done_count == 0:
            return
        elapsed = max(time.monotonic() - started_at, 1.0)
        rate_per_hour = (done_count / elapsed) * 3600.0
        pct = (success_count / done_count * 100) if done_count else 0
        remaining = total_count - done_count
        eta_seconds = (remaining / done_count) * elapsed if done_count else 0
        # Formats and failures are on every record line already (the running
        # tally); repeating them here said nothing the previous line had not.
        print(
            f"⏱️  processing {rate_per_hour:.1f} / hour  "
            f"({done_count}/{total_count}, elapsed {_format_elapsed(elapsed)}, "
            f"ETA {_format_elapsed(eta_seconds)}, success {success_count}/{done_count} = {pct:.0f}%)"
        )

    # ---------------- Slow-record visibility -----------------------------
    # A batch cannot finish before its slowest record, and a record that is
    # merely slow looks exactly like one that is wedged. The only difference a
    # user can observe is whether anyone says so, so an in-flight record that
    # passes SLOW_RECORD_SECONDS announces itself and keeps announcing.
    SLOW_RECORD_SECONDS = 300      # 5 minutes -- first "still working" notice
    SLOW_REPEAT_SECONDS = 120      # and every 2 minutes after that
    HEARTBEAT_POLL_SECONDS = 15

    _inflight = {}                 # idx -> (doi, started_monotonic, last_warned)
    _inflight_lock = threading.Lock()

    def _inflight_start(idx, doi):
        with _inflight_lock:
            _inflight[idx] = [doi, time.monotonic(), 0.0]

    def _inflight_end(idx):
        with _inflight_lock:
            _inflight.pop(idx, None)

    def _heartbeat(stop_event):
        """Name any record that has been running too long, until it finishes."""
        while not stop_event.wait(HEARTBEAT_POLL_SECONDS):
            now = time.monotonic()
            slow = []
            with _inflight_lock:
                for idx, entry in _inflight.items():
                    doi, started, last_warned = entry
                    age = now - started
                    if age < SLOW_RECORD_SECONDS:
                        continue
                    since = now - last_warned if last_warned else age
                    if last_warned and since < SLOW_REPEAT_SECONDS:
                        continue
                    entry[2] = now
                    slow.append((idx, doi, age))
            for idx, doi, age in sorted(slow, key=lambda s: -s[2]):
                print(
                    f"⏳ [{idx + 1}/{total}] still working on {doi} "
                    f"({_format_elapsed(age)} elapsed) -- "
                    f"timeout at {_format_elapsed(record_timeout)}"
                    if record_timeout else
                    f"⏳ [{idx + 1}/{total}] still working on {doi} "
                    f"({_format_elapsed(age)} elapsed, no record timeout set)"
                )

    def _record_failure(identifier, category, detail=""):
        """Log a record the worker loop failed on, the same way download_one does.

        A timed-out or crashed record never reaches download_one's own reporting,
        so without this it would vanish from missing_pdfs.html and
        failed_dois.csv -- present in the totals, absent from the retry list.
        """
        if create_missing_report and _run_timestamp:
            try:
                _append_missing_to_report(
                    output_dir, identifier, _run_timestamp, _missing_report_lock
                )
            except Exception as _e:
                if verbose:
                    print(f"  missing-report append error: {_e}")
        try:
            _append_failed_to_csv(
                output_dir, identifier, category=category, detail=detail,
                lock=_missing_report_lock,
            )
            with _missing_report_lock:
                _failure_counter[category] += 1
        except Exception as _e:
            if verbose:
                print(f"  failed_dois.csv append error: {_e}")

    SOURCE_TRACK_INTERVAL = 10  # Write CSV/JSON every N completions

    def _write_source_tracking(entries_to_add):
        """Append to source_tracking.csv and merge into source_counts.json."""
        if not entries_to_add:
            return
        import csv
        import json
        csv_path = os.path.join(output_dir, "source_tracking.csv")
        json_path = os.path.join(output_dir, "source_counts.json")
        # Append to CSV (write header only if file is new)
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["pdf", "source"])
            if write_header:
                w.writeheader()
            w.writerows([{"pdf": p, "source": s} for p, s in entries_to_add])
        # Merge into JSON counts
        counts = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    counts = dict((data.get("source_counts") or {}))
            except (json.JSONDecodeError, OSError):
                pass
        for _path, _src in entries_to_add:
            counts[_src] = counts.get(_src, 0) + 1
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"source_counts": counts}, f, indent=2)

    source_entries = []  # (path, source) for successful downloads
    source_entries_written = 0
    completed_count = 0

    # --json, one line per input row. The worker fills its own slot and the
    # collection loops below are the only writers: a record abandoned at
    # --record-timeout is still running in a daemon thread, and letting that
    # thread write would mean a half-written line arriving on stdout during
    # interpreter shutdown. Each slot is written by exactly one worker and read
    # only after its future has been collected, so it needs no lock of its own.
    json_records = [None] * len(dois)

    def _record_json(idx, identifier, doi, path, status, **fields):
        """Called by the worker that owns row `idx`; writes nothing itself."""
        if json_out is None:
            return
        json_records[idx] = _json_record(identifier, doi=doi, path=path,
                                         status=status, **fields)

    def _emit_record(idx, identifier, **fields):
        """Write the worker's record for a row, or a stand-in when it has none."""
        if json_out is None:
            return
        record = json_records[idx]
        if record is None:
            record = _json_record(identifier, status=JSON_STATUS_FAILED, **fields)
        json_out.write(record)

    if workers <= 1:
        # Sequential processing - no need for prefix wrapper. The heartbeat still
        # runs: a single-threaded run can stall just as easily, and with no other
        # output arriving it is even harder to tell from a finished one.
        _hb_stop = threading.Event()
        _hb = threading.Thread(target=_heartbeat, args=(_hb_stop,),
                               name="fetchpdf-heartbeat", daemon=True)
        _hb.start()
        try:
            for idx, doi in enumerate(dois):
                r = download_one(doi, idx, total)
                _emit_record(idx, str(doi).strip())
                results.append(r)
                if track_source and len(r) >= 4 and r[1] and r[2] and r[3] and r[3] != "existing":
                    source_entries.append((r[2], r[3]))
                completed_count += 1
                if completed_count % SOURCE_TRACK_INTERVAL == 0 and track_source:
                    to_add = source_entries[source_entries_written:]
                    _write_source_tracking(to_add)
                    source_entries_written = len(source_entries)
                done = idx + 1
                if done % progress_interval == 0 or done == total:
                    success_so_far = sum(1 for r in results if r[1])
                    _print_rate_update(done, total, success_so_far)
        finally:
            _hb_stop.set()
    else:
        # Parallel processing - install prefix wrapper
        sys.stdout = PrefixedOutput(_original_stdout)
        # A plain `with ThreadPoolExecutor(...)` calls shutdown(wait=True) on the
        # way out, which blocks on EVERY submitted task. One wedged record then
        # holds the whole process open after the other workers have drained --
        # indistinguishable, from outside, from a hang at the end of the run.
        # So the executor is driven explicitly and torn down with
        # cancel_futures, and each result is collected under a deadline.
        # Daemon workers, deliberately. concurrent.futures registers a
        # _python_exit hook that unconditionally join()s every pool thread at
        # interpreter shutdown -- so shutdown(wait=False) alone does NOT let the
        # process exit while a worker is wedged in a socket read. Verified: the
        # naive version prints its summary and then hangs forever at exit, which
        # is precisely the "stalled at the end" symptom. Daemon threads are not
        # joined, so the interpreter can leave them behind. Safe here because
        # every artifact is written atomically via os.replace: a worker killed
        # mid-flight leaves a temp file, never a half-written PDF.
        executor = _DaemonThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="fetchpdf"
        )
        _hb_stop = threading.Event()
        _hb = threading.Thread(target=_heartbeat, args=(_hb_stop,),
                               name="fetchpdf-heartbeat", daemon=True)
        _hb.start()
        try:
            futures = {
                executor.submit(download_one, doi, idx, total): idx
                for idx, doi in enumerate(dois)
            }
            # After submit(), not before: pool threads are spawned lazily as work
            # arrives, so there is nothing to detach until the queue is filled.
            _unregister_pool_from_atexit(executor)
            ordered_results = [None] * total
            completed = 0
            batch_deadline = (
                time.monotonic() + batch_timeout if batch_timeout else None
            )
            pending = set(futures)
            for future in as_completed(futures):
                pending.discard(future)
                idx = futures[future]
                doi_label = str(dois[idx]).strip()
                try:
                    # as_completed already handed us a finished future, so this
                    # timeout only guards the pathological case; the real
                    # per-record bound is enforced below on what is left over.
                    r = future.result(timeout=0)
                except Exception as e:
                    print(f"❌ {doi_label} - worker error: {type(e).__name__}: {e}")
                    _record_failure(doi_label, "worker_error", str(e)[:200])
                    r = (doi_label, False, None, None) if track_source else (doi_label, False, None)
                    # The worker raised before it could fill its slot, so the
                    # stand-in record carries the exception instead.
                    json_records[idx] = None
                    _emit_record(idx, doi_label,
                                 reasons=[f"worker error: {type(e).__name__}: {str(e)[:150]}"])
                else:
                    _emit_record(idx, doi_label)
                ordered_results[idx] = r
                if track_source and len(r) >= 4 and r[1] and r[2] and r[3] and r[3] != "existing":
                    source_entries.append((r[2], r[3]))
                completed += 1
                if completed % SOURCE_TRACK_INTERVAL == 0 and track_source:
                    to_add = source_entries[source_entries_written:]
                    _write_source_tracking(to_add)
                    source_entries_written = len(source_entries)
                if completed % progress_interval == 0 or completed == total:
                    success_so_far = sum(1 for r in ordered_results if r is not None and r[1])
                    _print_rate_update(completed, total, success_so_far)
                if batch_deadline and time.monotonic() > batch_deadline:
                    print(f"\n⏰ batch timeout ({_format_elapsed(batch_timeout)}) reached "
                          f"with {len(pending)} record(s) unfinished -- abandoning them")
                    break

            # Anything still running is over budget by construction: give it the
            # per-record grace it has not already used, then abandon it. Threads
            # cannot be killed, so the daemon executor is simply left behind.
            if pending:
                for future in list(pending):
                    idx = futures[future]
                    doi_label = str(dois[idx]).strip()
                    with _inflight_lock:
                        entry = _inflight.get(idx)
                    started = entry[1] if entry else time.monotonic()
                    grace = (
                        max(0.0, record_timeout - (time.monotonic() - started))
                        if record_timeout else None
                    )
                    try:
                        r = future.result(timeout=grace)
                        ordered_results[idx] = r
                        if track_source and len(r) >= 4 and r[1] and r[2] and r[3] and r[3] != "existing":
                            source_entries.append((r[2], r[3]))
                        _emit_record(idx, doi_label)
                        continue
                    except FuturesTimeout:
                        waited = time.monotonic() - started
                        print(f"⏰ {doi_label} - abandoned after "
                              f"{_format_elapsed(waited)} (record timeout)")
                        _record_failure(doi_label, "timeout",
                                        f"exceeded {int(record_timeout)}s")
                        json_reason = f"record timeout: exceeded {int(record_timeout)}s"
                    except Exception as e:
                        print(f"❌ {doi_label} - worker error: {type(e).__name__}: {e}")
                        _record_failure(doi_label, "worker_error", str(e)[:200])
                        json_reason = f"worker error: {type(e).__name__}: {str(e)[:150]}"
                    ordered_results[idx] = (
                        (doi_label, False, None, None) if track_source
                        else (doi_label, False, None)
                    )
                    # The abandoned worker is still running and may yet fill its
                    # slot. Emitted from here regardless, so the stream holds one
                    # line per input row and holds it before the process exits.
                    json_records[idx] = None
                    _emit_record(idx, doi_label, reasons=[json_reason])

            results = [
                r if r is not None else (
                    (str(d).strip(), False, None, None) if track_source
                    else (str(d).strip(), False, None)
                )
                for r, d in zip(ordered_results, dois)
            ]
        finally:
            _hb_stop.set()
            # wait=False so a wedged worker cannot hold the process open; the
            # pool's threads are daemons, so the interpreter can still exit.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:      # cancel_futures is 3.9+
                executor.shutdown(wait=False)
            sys.stdout = _original_stdout

    # Resolution results outlive this run: persisting them is what makes a
    # resumed batch cheap.
    if _resolver is not None:
        _resolver.cache.flush()

    # Print summary
    total = len(results)
    succeeded = sum(1 for r in results if r[1])
    failed = total - succeeded
    print(f"\n{'='*60}")
    print("  BATCH DOWNLOAD COMPLETE")
    print(f"  Total: {total}  |  Success: {succeeded}  |  Failed: {failed}  |  Rate: {succeeded/total*100:.0f}%" if total > 0 else "  No DOIs processed")
    elapsed_total = time.monotonic() - started_at
    print(f"  Total time: {_format_elapsed(elapsed_total)}")
    if _format_counter:
        total_artifacts = sum(_format_counter.values())
        print(f"  Formats ({total_artifacts} artifacts):")
        for label, count in sorted(_format_counter.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {label:9s} {count:5d}  {count / total_artifacts * 100:5.1f}%")
    if _si_statuses:
        from collections import Counter as _Counter
        by_status = _Counter(s.status for s in _si_statuses.values())
        rendered = "  ".join(f"{k}:{v}" for k, v in sorted(by_status.items()))
        print(f"  Supplementary: {rendered}")
        incomplete = {d: s for d, s in _si_statuses.items()
                      if s.status == "incomplete"}
        if incomplete:
            # The one condition a run must never end quietly with: files the
            # papers themselves declare, not obtained by any provider.
            print(f"  ⚠️  SI INCOMPLETE — {len(incomplete)} record(s) missing "
                  f"declared files:")
            for display_id, s in sorted(incomplete.items()):
                names = ", ".join(s.missing_declared) or "unknown"
                print(f"      {display_id}: {names}")
        # Supplements Europe PMC holds but is not permitted to serve. Warned
        # per-record as they happen; listed again here because in a long run
        # those lines scroll away, and this is the set a user might still
        # fetch by hand.
        try:
            from .retrieval.supplementary import not_open_access_records
            withheld = not_open_access_records()
        except Exception:
            withheld = []
        if withheld:
            _print_yellow_warning(
                f"  ⚠️  SI WITHHELD — Europe PMC has supplementary material for "
                f"{len(withheld)} record(s) but may not serve it (not open "
                f"access). This is a Europe PMC limitation, not a verdict on other routes:"
            )
            for display_id in sorted(set(withheld)):
                print(f"      {display_id}")

        # What LAYER 2 did, which is not the same question as what it found.
        # "Never called" and "called and found nothing" are different facts
        # about different things -- one is about this run's configuration, the
        # other about the papers -- and on these corpora the second is the
        # common case, so the first would hide behind it forever.
        try:
            from .retrieval.llm_agent_retrieval import run_report
            agent_outcomes = run_report()
        except Exception:
            agent_outcomes = {}
        if agent_outcomes:
            rendered = "  ".join(f"{k}:{v}"
                                 for k, v in sorted(agent_outcomes.items()))
            print(f"  Retrieval agent: {rendered}")
            unreached = sum(v for k, v in agent_outcomes.items()
                            if k in ("backend_unavailable", "unknown_backend",
                                     "skipped_ceiling", "no_full_text",
                                     "call_failed"))
            if unreached:
                _print_yellow_warning(
                    f"  ⚠️  {unreached} record(s) were NOT examined by the "
                    f"retrieval agent. Those are not records where it found "
                    f"nothing."
                )

    # The SI half of the missing-materials report, written once now that every
    # record's supplementary pass has finished.
    if create_missing_report and (_si_statuses or _figure_statuses) and _run_timestamp:
        try:
            _append_missing_si_to_report(output_dir, _si_statuses,
                                         _run_timestamp, _missing_report_lock,
                                         figure_statuses=_figure_statuses)
        except Exception as e:
            if verbose:
                print(f"  missing-SI report failed: {str(e)[:120]}")

    # Draft emails for material that demonstrably exists and could not be had.
    # Last, because it reads the manifests every other pass has finished
    # writing. Failures here must never affect the run's verdict.
    if draft_requests:
        try:
            from .retrieval.request_drafts import (
                collect_asks, group_by_author, write_drafts,
            )
            failed_dois = [r[0] for r in results if not r[1]]
            news_dois = list(_NEWS_ITEM_SEEN)
            pairs, unresolved = collect_asks(
                output_dir, failed_dois=failed_dois, news_dois=news_dois)
            written = write_drafts(group_by_author(pairs), unresolved)
        except Exception as e:
            written = []
            if verbose:
                print(f"  draft-requests failed: {str(e)[:160]}")
        if written:
            _print_yellow_warning(
                f"  ✉️  EMAIL REQUESTS — {len(written)} draft(s) written for material "
                f"that could not be fetched. Review and send them yourself; "
                f"nothing was emailed."
            )
            for path, papers, pdfs, supps in written:
                parts = []
                if pdfs:
                    parts.append(f"{pdfs} PDF{'s' if pdfs > 1 else ''}")
                if supps:
                    parts.append(f"{supps} supplementary")
                detail = ", ".join(parts) or "no address found"
                print(f"      ./{os.path.basename(path):<38} "
                      f"({papers} paper{'s' if papers > 1 else ''}: {detail})")
    print(f"{'='*60}")

    # Missing PDFs report is appended on the fly; just show path if there were failures
    if create_missing_report and failed > 0:
        report_path = os.path.join(output_dir, "missing_pdfs.html")
        print(f"\n📄 Missing PDFs report: {report_path}")

    # Final source tracking write (catches remainder when total % interval != 0)
    if track_source and source_entries:
        to_add = source_entries[source_entries_written:]
        _write_source_tracking(to_add)
        csv_path = os.path.join(output_dir, "source_tracking.csv")
        json_path = os.path.join(output_dir, "source_counts.json")
        print(f"\n📊 Source tracking: {csv_path}")
        print(f"📊 Source counts:  {json_path}")

    # What each source COST, not just which one won. A separate writer rather than
    # a change to _write_source_tracking: that one appends per-artifact rows as the
    # run proceeds, this is one whole-run summary written once.
    if track_source:
        _print_source_timing()

    return results


def _print_source_timing():
    """Print the seconds-per-hit ranking. Terminal only -- no file written."""
    rows = source_timing_rows()
    if not rows:
        return

    print("\n⏳ Source cost (dearest per hit first):")
    print(f"    {'source':22s} {'calls':>6s} {'hits':>5s} "
          f"{'xml':>5s} {'html':>5s} {'pdf':>5s} {'total_s':>9s} {'s/hit':>8s}")
    for r in rows:
        per_hit = r["seconds_per_hit"]
        # No hits means no finite cost per hit -- the thing worth seeing at a glance.
        shown = f"{per_hit:>8}" if per_hit != "" else "       —"
        # A dot, not a 0: "this source produced no XML" and "this source has
        # never been asked for XML" should not look the same at a glance.
        def _cell(n):
            return f"{n:5d}" if n else "    ·"
        print(
            f"    {r['source']:22s} {r['calls']:6d} {r['hits']:5d} "
            f"{_cell(r.get('xml', 0))} {_cell(r.get('html', 0))} "
            f"{_cell(r.get('pdf', 0))} "
            f"{r['total_seconds']:9.2f} {shown}"
        )


#: True while --json is in force. Read by _stdio_is_interactive, which is the
#: one gate between a piped run and a blocking prompt: under --json stdout is
#: the results channel and stderr may still be a terminal, so the usual "are
#: both ends a tty" test would answer yes and stop the run on a question its
#: caller cannot see.
_JSON_MODE = False

#: Every value `status` can take. Three states, because a caller has three
#: different jobs to do: use the file, use the file knowing nothing re-checked
#: it, or handle the failure.
JSON_STATUS_DOWNLOADED = "downloaded"        # fetched and written by this run
JSON_STATUS_ALREADY_ON_DISK = "already_on_disk"  # found on disk, not re-verified
JSON_STATUS_FAILED = "failed"                # nothing usable was written


class _JsonWriter:
    """One JSON object per record on the real stdout, flushed as it is written.

    Holds the stdout the process started with, captured before the redirect
    that sends every existing print() to stderr. Locked because batch workers
    finish in whatever order they finish in; the lock is what keeps two records
    from interleaving halfway through a line.
    """

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def _json_record(identifier, doi=None, path=None,
                 status=JSON_STATUS_FAILED, source=None,
                 identity=None, reasons=None, extra_paths=None) -> dict:
    """The --json object for one record. Key order is the documented order."""
    return {
        "identifier": str(identifier),
        "doi": doi or None,
        "success": status != JSON_STATUS_FAILED,
        "status": status,
        "path": os.path.abspath(path) if path else None,
        "format": format_label(path) if path else None,
        "source": source or None,
        "identity": identity,
        "reasons": list(reasons or []),
        "paths": [os.path.abspath(p) for p in (extra_paths or [])],
    }


def _emit_json(json_out, identifier, doi, path, status, **fields) -> None:
    """Write one record, or nothing at all when --json is off."""
    if json_out is None:
        return
    json_out.write(_json_record(identifier, doi=doi, path=path,
                                status=status, **fields))


def _identity_and_reasons(result_path, save_path):
    """(identity, reasons) for a finished record, from the recorded verdict.

    Looked up on the artifact when there is one and on the intended path when
    there is not -- a discarded PDF is deleted, so the path it occupied is the
    only place its refusal can be found. Never on both: a record whose PDF was
    refused and which then succeeded as structured full text would inherit the
    deleted PDF's refusal and report an .xml artifact as the wrong article.

    A verified PDF carries no reasons; any other state carries the verdict's
    own sentence, which covers both the refusals and the kept-unverified case
    where the file exists and nobody could check it.
    """
    from .retrieval.pdf_identity import VERIFIED

    entry = identity_for(result_path) if result_path else identity_for(save_path)
    if entry is None:
        return None, []
    state, reason = entry
    if state == VERIFIED:
        return state, []
    return state, [reason] if reason else []


def main(argv=None):
    """Entry point. Splits off --json before argparse gets to print anything.

    The flag turns stdout into the results channel, so the redirect has to be
    installed before the first write to it -- and argparse writes there itself,
    for --help and for a usage error. Hence a two-option pre-parse rather than
    a check after parse_args(): parse_known_args ignores everything else and
    accepts the same abbreviations the real parser does.
    """
    global _JSON_MODE
    import argparse
    import contextlib

    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--json", action="store_true")
    if not pre.parse_known_args(argv)[0].json:
        return _main(argv)

    json_out = _JsonWriter(sys.stdout)
    _JSON_MODE = True
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return _main(argv, json_out)
    finally:
        _JSON_MODE = False


def _main(argv=None, json_out=None):
    """Command-line interface for fetchpdf."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Download PDFs from DOIs using multiple fallback sources.",
        epilog=(
            "Examples:\n"
            "  fetchpdf papers.csv -o ./pdfs                      # batch from CSV\n"
            "  fetchpdf papers.csv -o ./pdfs -w 4                 # batch, 4 workers\n"
            "  fetchpdf papers.csv -o ./pdfs --start-from-row 100 # resume from row 100\n"
            "  fetchpdf \"10.1038/nature12373\"                      # single DOI\n"
            "  fetchpdf \"10.1038/nature12373\" -o ./papers          # single DOI, custom dir\n"
            "  fetchpdf \"39804400\"                                 # PMID (auto-resolved)\n"
            "  fetchpdf --csv papers.csv --output-dir ./pdfs --workers 4\n"
            "  fetchpdf --pmid-csv pmids.csv -o ./pdfs\n"
            "  fetchpdf papers.csv -o ./pdfs --pull-supplementary  # + supplementary files"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="DOI, PMID, or CSV file path (auto-detected)")
    parser.add_argument("output_pos", nargs="?", help="Output file path for single DOI download")
    parser.add_argument(
        "--csv",
        help="CSV file with DOIs to process (for batch download)"
    )
    parser.add_argument(
        "--doi-column",
        default="DOI",
        help="Column name for DOI/PMID identifiers in CSV (default: DOI)"
    )
    parser.add_argument(
        "--pmid-csv",
        dest="pmid_csv",
        help="CSV file with PMIDs to process (for batch download)"
    )
    parser.add_argument(
        "--input-from-dirs",
        dest="input_from_dirs",
        default=None,
        help=(
            "Batch from an EXISTING corpus directory: read each subdirectory "
            "name, decode it back to a DOI (safe-filename inverse), and add "
            "it to the batch. Supports both modern ('~' for ':') and legacy "
            "('--' for both '/' and ':') folder-name encodings; ambiguous "
            "legacy names are resolved via a Crossref existence check. Non-"
            "safe-DOI-shaped subdirectory names are skipped."
        ),
    )
    parser.add_argument(
        "--migrate-folders",
        dest="migrate_folders",
        default=None,
        help=(
            "Rename legacy '--'-encoded folders under this directory to the "
            "current modern encoding ('~' for ':') and exit. A folder is "
            "migrated only when Crossref confirms the legacy reading "
            "resolves to a real DOI AND the modern reading does not, so "
            "legitimate multi-segment DOI folders (10.1093/abm/kaad072) are "
            "left alone. Inner files whose stem starts with the old folder "
            "name are also renamed so sidecar filenames stay in sync. Pair "
            "with --migrate-dry-run to preview."
        ),
    )
    parser.add_argument(
        "--migrate-dry-run",
        dest="migrate_dry_run",
        action="store_true",
        help="Report what --migrate-folders would do without touching disk.",
    )
    parser.add_argument(
        "--pmid-column",
        default="pmid",
        help="Column name for PMIDs in CSV when using --pmid-csv (default: pmid)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default="./pdfs",
        help="Output directory for downloads (default: ./pdfs)"
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Email for API calls (default: EMAIL from .env.local)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable results on stdout: one JSON object for a single "
             "identifier, one per record as JSON Lines for a batch, written as "
             "each record finishes. Everything the CLI normally prints -- "
             "progress, warnings, tallies -- goes to stderr instead, so stdout "
             "holds nothing but JSON. Never prompts. Exit codes are unchanged. "
             "For scripts and agents that want a path or a failure, not prose."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between API calls in seconds (default: 0.1)"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel workers for batch processing (default: 1)"
    )
    parser.add_argument(
        "--record-timeout",
        type=int,
        default=1200,
        help="Give up on a single record after N seconds and move on "
             "(default: 1200; 0 disables). Prevents one wedged record from "
             "holding the whole batch open."
    )
    parser.add_argument(
        "--batch-timeout",
        type=int,
        default=0,
        help="Stop collecting results after N seconds and report what "
             "finished (default: 0, meaning no batch limit)."
    )
    parser.add_argument(
        "--no-missing-report",
        action="store_true",
        help="Do not create HTML report for missing PDFs"
    )
    parser.add_argument(
        "--tracksource",
        action="store_true",
        help="Create source_tracking.csv (pdf, source) and source_counts.json "
             "in output dir, and print a per-source timing table at the end"
    )
    parser.add_argument(
        "--add-playwright",
        action="store_true",
        help="Enable Playwright-based scrapers (publisher browser sessions, OSF browser). Disabled by default."
    )
    parser.add_argument(
        "--start-from-row",
        type=int,
        default=0,
        help="Skip to row X in the CSV (0-indexed, default: 0). Useful for resuming interrupted batches."
    )
    parser.add_argument(
        "--abstract-if-no-pdf",
        action="store_true",
        help="When a PDF cannot be downloaded, fetch title+abstract and save as {DOI}_abstract.md"
    )
    parser.add_argument(
        "--abstract-only",
        action="store_true",
        help="Only fetch title+abstract (no PDF download). Save as {DOI}_abstract.md, skip if already exists."
    )
    parser.add_argument(
        "--no-xml-fallback",
        action="store_true",
        help="Disable the Elsevier full-text XML fallback (last resort for Elsevier DOIs). "
             "XML fallback is ON by default; use this for PDF-only pipelines that can't ingest XML."
    )
    parser.add_argument(
        "--prioritize-xml",
        action="store_true",
        help="Walk format tiers (XML > HTML > LaTeX > supplements > PDF) instead of the "
             "source-ordered chain, so structured full text from a worse-ranked source "
             "beats a PDF from a better-ranked one. Default behaviour is unchanged without it."
    )
    parser.add_argument(
        "--xml-only",
        action="store_true",
        help="Fail the record rather than descending below structured XML. Implies --prioritize-xml."
    )
    parser.add_argument(
        "--xml-html-only",
        action="store_true",
        help="Fail the record rather than descending below structured full text: XML, then "
             "publisher HTML, then stop. Never writes a PDF. Requires lxml for the HTML rung "
             "(pip install 'fetchpdf[html]'). Implies --prioritize-xml."
    )
    parser.add_argument(
        "--get-xml-or-html",
        action="store_true",
        help="Keep BOTH a structured copy (XML, else publisher HTML) AND the PDF, "
             "instead of stopping at the best single format. Skips tiers that fill "
             "neither slot (LaTeX source, supplements, landing pages). Doubles as a "
             "backfill: pointed at a directory of existing PDFs it fetches only the "
             "missing structured half and re-downloads nothing. Implies --prioritize-xml."
    )
    parser.add_argument(
        "--to-markdown",
        action="store_true",
        help="Also write {stem}.md for every XML/HTML artifact retrieved. Prose becomes "
             "Markdown; tables stay canonical HTML so colspan/rowspan survive, which "
             "Markdown cannot express. Run fetchpdf-md on a directory to convert "
             "artifacts you already have."
    )
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="Dump every embedded image bitstream from each retrieved PDF into "
             "{stem}_images/ with a {stem}_images.json manifest. Original streams, "
             "not page renders. Also runs for PDFs already on disk (the backfill "
             "case). Requires: pip install 'fetchpdf[images]'. Or run "
             "fetchpdf-images on a directory."
    )
    parser.add_argument(
        "--pull-figures",
        action="store_true",
        help="Also fetch the article's published figure images into {stem}_figures/ "
             "with a {stem}_figures.json manifest recording each figure's label, "
             "caption, source URL and sha256. PMC only: the Open Data mirror when "
             "the package holds the files, else the article page's blob CDN. These "
             "are the publisher's image files, not the bitstreams embedded in a PDF "
             "-- see --extract-images for those. Also runs for records already on "
             "disk, and a figure failure never fails the record."
    )
    parser.add_argument(
        "--target-task",
        choices=["extraction", "screening"],
        default="extraction",
        help="Which tier ladder to use (default: extraction). Screening ranks plain text 4th; "
             "extraction refuses it outright, since flattened text destroys row/column "
             "association silently."
    )
    parser.add_argument(
        "--upgrade-existing",
        action="store_true",
        help="Re-run over already-downloaded records, writing only when a strictly better "
             "format tier is obtained. Non-destructive: the superseded file is left on disk "
             "and the sidecar records which artifact is authoritative. Implies --prioritize-xml."
    )
    parser.add_argument(
        "--provenance",
        action="store_true",
        help="Write a {stem}.provenance.json audit sidecar per record: source, tier, why that "
             "tier, resolution chain, request URL, status, hash, license, table count."
    )
    parser.add_argument(
        "--pull-supplementary",
        action="store_true",
        help="Also download every supplementary file the SI endpoints offer for each record -- "
             "PDFs, spreadsheets, documents, images, archives, raw data -- saved as flat "
             "siblings {stem}_supplementary_info_1.xlsx, _2.pdf, ... with a "
             "{stem}_supplementary_info.json manifest recording each file's original name, "
             "provider, URL and sha256. Works on the default chain and the tiered path alike. "
             "A record with no supplementary material is not a failure, and a supplementary "
             "failure never fails the record."
    )
    parser.add_argument(
        "--max-supplementary-mb",
        type=float,
        default=300,
        help="Per-file size cap for --pull-supplementary in megabytes (default: 300). A file "
             "whose Content-Length exceeds it is skipped without downloading; a server that "
             "under-reports is aborted mid-stream and its partial removed. Nothing is ever "
             "truncated -- a truncated spreadsheet is a corrupt spreadsheet."
    )
    parser.add_argument(
        "--refresh-supplementary",
        action="store_true",
        help="Re-run the supplementary pass over records that already have a manifest, picking "
             "up files deposited since. Existing files are left on disk. Implies "
             "--pull-supplementary."
    )
    parser.add_argument(
        "--download-data-artifacts",
        action="store_true",
        help="Also download the datasets and code the paper links to -- Zenodo, Dryad, OSF, "
             "figshare, Dataverse deposits and GitHub repositories -- not just record them in "
             "the linked-artifacts sidecar. Without this only the paper's own material is "
             "fetched, which in practice means supplementary files you already have: on a real "
             "corpus every 'owned' link was a mirror of those, while the actual replication "
             "packages were classified 'related' and skipped. Also scans the paper's own full "
             "text for deposits the link indexes miss. Implies --pull-supplementary."
    )
    parser.add_argument(
        "--max-data-artifact-mb",
        type=float,
        default=500,
        help="Per-record cap for --download-data-artifacts in megabytes (default: 500). Counted "
             "separately from --max-supplementary-mb so one large dataset cannot starve the "
             "paper's own supplementary files. Refusals are recorded in the manifest."
    )
    parser.add_argument(
        "--draft-requests",
        action="store_true",
        help="Write a draft email per corresponding author asking for material that could not "
             "be fetched -- papers that failed entirely, and supplements the publisher holds "
             "but will not serve. Drafts land in the CURRENT directory as "
             "email_request_<author>.md. Nothing is ever sent: extraction is ~97%% precise, "
             "addresses go stale, and who to contact is your call, not the tool's."
    )
    parser.add_argument(
        "--download-related-unverified",
        action="store_true",
        help="Take every index assertion at face value when downloading data artifacts. By "
             "default a 'related' deposit is skipped if its own DataCite record says it "
             "belongs to a DIFFERENT article -- which is how a 2006 commitment-savings "
             "dataset was once filed under a 2025 savings-reminder megastudy. Use this only "
             "when you want breadth over precision."
    )
    parser.add_argument(
        "--unpack-data-artifacts",
        action="store_true",
        help="Expand zip archives found in linked deposits. Off by default: a replication "
             "package is a coherent thing with its own internal layout, and unpacking one "
             "turned a single record into 204 files, 199 of them a vendored Stata library. "
             "The archive is always kept either way."
    )
    parser.add_argument(
        "--llm-adjudicate-artifacts",
        action="store_true",
        help="When the deterministic rules cannot tell whether a linked repository is this "
             "paper's own deposit or somebody else's, ask a locally installed Claude Code CLI. "
             "Only ambiguous and rejected candidates are sent (roughly a cent per paper); "
             "accepted ones are never re-litigated. Falls back to rules alone, with one "
             "warning, if the CLI is absent or errors. Requires --download-data-artifacts."
    )
    parser.add_argument(
        "--pull-everything",
        action="store_true",
        help="Both retrieval layers at once: every API route (--pull-supplementary "
             "--download-data-artifacts) plus an AI agent that reads the paper itself "
             "and goes after the supplementary files and datasets those routes did "
             "not get. SI lands beside the PDF as numbered siblings; linked deposits "
             "land in {stem}_data_artifacts/. NOT literally everything -- Wiley, ACS "
             "and MDPI serve HTTP 403 to plain requests, arXiv ancillary files are "
             "uncovered, and GEO/SRA/Mendeley/ICPSR have no file enumerator; those "
             "are recorded in the linked-artifacts sidecar, not fetched. Costs a "
             "model call per record: see --llm-backend and --max-llm-records."
    )
    parser.add_argument(
        "--llm-agent-retrieval",
        action="store_true",
        help="The second layer on its own, without implying the first. Rarely what "
             "you want -- the agent is told what layer 1 already obtained so it does "
             "not fetch a second copy, and without layer 1 that list is empty. "
             "Implies --download-data-artifacts."
    )
    parser.add_argument(
        "--llm-backend",
        choices=["claude-cli", "openrouter"],
        default="claude-cli",
        help="Which model runs the retrieval agent (default: claude-cli). "
             "'claude-cli' shells out to a locally installed Claude Code CLI and "
             "reuses whatever auth you already have; it is locked down to WebFetch "
             "with no file, shell or MCP tools, so it navigates and reports URLs "
             "and fetchpdf does the transfer. 'openrouter' needs OPENROUTER_API_KEY "
             "in .env.local and downloads directly through two tools fetchpdf "
             "implements. Both end with the same files in the same places."
    )
    parser.add_argument(
        "--max-llm-records",
        type=int,
        default=0,
        help="Stop calling the retrieval agent after this many records (0 = no "
             "limit). A corpus run makes one model call per record, so this is the "
             "ceiling that stops an overnight batch spending without bound. The "
             "records past the limit still get every API route; only the agent is "
             "skipped, and the manifest says so."
    )
    parser.add_argument(
        "--llm-model",
        default="haiku",
        help="Model for --llm-adjudicate-artifacts (default: haiku)."
    )
    parser.add_argument(
        "--make-subfolder",
        action="store_true",
        help="Give each record its own directory, {output_dir}/{safe_doi}/, named by the same "
             "DOI encoding used for the filenames, so folder and file stems match. Every "
             "artifact for a record (PDF, XML, abstract, provenance, linked artifacts, "
             "supplementary files) lands inside it; run-level files (failed_dois.csv, "
             "missing_pdfs.html, source_tracking.csv, ...) stay at the output-dir root. "
             "Note a subfolder run cannot see PDFs from an earlier flat run into the same "
             "directory, so those records are fetched again. A record that comes back with "
             "nothing leaves no folder behind. Ignored when an explicit output "
             "file path is given for a single download."
    )
    parser.add_argument(
        "--on-existing",
        choices=[ON_EXISTING_ASK, ON_EXISTING_SKIP, ON_EXISTING_SUPPLEMENT],
        default=ON_EXISTING_ASK,
        help="What to do about records already on disk when a batch re-runs over a "
             "directory it has filled before. 'skip' is the historical behaviour: "
             "download only what is missing. 'supplement' also skips the download but "
             "turns on --pull-supplementary, so records already here keep their PDFs "
             "and gain their supplementary material. 'ask' (the default) puts the same "
             "choice on screen, and falls back to 'skip' whenever stdin or stdout is "
             "not a terminal -- pipes, cron and CI never block."
    )

    args = parser.parse_args(argv)


    # Answered once, here, rather than per record. Every retrieved PDF is
    # checked against the record it was fetched for, so with no text engine
    # every PDF would be rejected -- and a 5,000-DOI run would end with zero
    # PDFs and 5,000 individually reasonable-looking warnings. That failure
    # mode is indistinguishable from "nothing was available", which is exactly
    # the confusion this whole change exists to remove.
    from .retrieval.pdf_text import engine_status as _engine_status
    if _engine_status()[0] is None:
        parser.error(
            "no PDF text engine is installed, so no retrieved PDF can be "
            "verified as the article it was fetched for -- every PDF would be "
            "rejected. Install one:  pip install 'fetchpdf[text]'  (or pypdf)"
        )

    # --xml-only and --upgrade-existing are meaningless outside the tier walk.
    if args.xml_only or args.xml_html_only or args.upgrade_existing or args.get_xml_or_html:
        args.prioritize_xml = True

    # --refresh-supplementary is a modifier on the pass, not a mode of its own.
    if args.refresh_supplementary:
        args.pull_supplementary = True
    # Likewise: the artifact machinery lives inside the supplementary pass, so
    # asking for artifacts without it would silently do nothing.
    if args.download_data_artifacts:
        args.pull_supplementary = True
    if args.max_supplementary_mb <= 0:
        parser.error("--max-supplementary-mb must be a positive number of megabytes")
    if args.max_data_artifact_mb <= 0:
        parser.error("--max-data-artifact-mb must be a positive number of megabytes")
    if args.pull_everything:
        # One flag, both layers. Set before the checks below so it cannot trip
        # the error its own implications satisfy.
        args.pull_supplementary = True
        args.download_data_artifacts = True
        args.llm_adjudicate_artifacts = True
        args.llm_agent_retrieval = True
    if args.llm_agent_retrieval:
        args.pull_supplementary = True
        args.download_data_artifacts = True
    if args.max_llm_records < 0:
        parser.error("--max-llm-records cannot be negative")

    if args.llm_adjudicate_artifacts and not args.download_data_artifacts:
        # Adjudication decides which candidates to DOWNLOAD; with nothing being
        # downloaded it would spend money to change nothing.
        parser.error(
            "--llm-adjudicate-artifacts has no effect without --download-data-artifacts")
    if args.pull_supplementary and args.abstract_only:
        # --abstract-only downloads no files at all, so there is no artifact for
        # supplements to sit beside. Silently pulling them anyway would be a
        # surprise; silently not pulling them would look like a bug.
        _print_yellow_warning(
            "⚠️  --abstract-only downloads no files, so --pull-supplementary is ignored."
        )
        args.pull_supplementary = False

    if args.get_xml_or_html and (args.xml_only or args.xml_html_only):
        other = "--xml-only" if args.xml_only else "--xml-html-only"
        _print_yellow_warning(
            f"❌ --get-xml-or-html keeps a PDF alongside the structured copy, and "
            f"{other} forbids writing a PDF at all. These cannot both hold -- drop one."
        )
        return 2

    if args.xml_only and args.xml_html_only:
        _print_yellow_warning(
            "⚠️  --xml-only and --xml-html-only both given; --xml-only is stricter and wins "
            "(no HTML will be accepted)."
        )
    elif args.xml_html_only:
        # Without lxml the HTML rung demotes on every record, so --xml-html-only
        # silently becomes --xml-only. Better to say so than to let someone
        # conclude their corpus has no HTML full text.
        try:
            import lxml.html  # noqa: F401
        except ImportError:
            _print_yellow_warning(
                "⚠️  --xml-html-only needs lxml for the HTML rung, and it is not installed.\n"
                "   Every HTML candidate will be demoted, making this equivalent to --xml-only.\n"
                "   Install it with:  pip install 'fetchpdf[html]'"
            )

    # Short-circuit: migrate legacy folder names and exit. No downloads run.
    if args.migrate_folders:
        target = args.migrate_folders
        if not os.path.isdir(target):
            print(f"❌ --migrate-folders target is not a directory: {target}")
            return 1
        actions = migrate_legacy_folders(
            target, email=args.email,
            dry_run=args.migrate_dry_run,
            verbose=args.verbose,
        )
        by_action: dict[str, int] = {}
        for a in actions:
            by_action[a["action"]] = by_action.get(a["action"], 0) + 1
        prefix = "[dry-run] " if args.migrate_dry_run else ""
        print(f"{prefix}Scanned {len(actions)} candidate legacy folder(s):")
        for k, v in sorted(by_action.items()):
            print(f"  {k}: {v}")
        return 0

    # Batch from an existing corpus dir (decode folder names → DOIs).
    if args.input_from_dirs:
        if not os.path.isdir(args.input_from_dirs):
            print(f"❌ --input-from-dirs target is not a directory: {args.input_from_dirs}")
            return 1
        dois_from_folders = dois_from_dir(
            args.input_from_dirs, email=args.email, verbose=args.verbose,
        )
        if not dois_from_folders:
            print(f"❌ No safe-DOI-shaped subdirectories found under {args.input_from_dirs}")
            return 1
        # Feed the discovered list through the same batch path used by --csv,
        # by writing a temporary in-memory CSV. Keeps a single code path for
        # start_from_row / workers / everything else the batch already handles.
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8",
        )
        tmp.write("DOI\n")
        for d in dois_from_folders:
            tmp.write(d + "\n")
        tmp.close()
        args.csv = tmp.name
        print(f"📁 --input-from-dirs: discovered {len(dois_from_folders)} DOI(s) "
              f"from subdirectories of {args.input_from_dirs}")

    # Auto-detect CSV from positional input arg
    if args.input and not args.csv and not args.pmid_csv:
        if args.input.lower().endswith('.csv') and os.path.isfile(args.input):
            args.csv = args.input
            args.input = None
        elif args.input.lower().endswith('.csv'):
            # File doesn't exist but looks like a CSV path
            print(f"❌ CSV file not found: {args.input}")
            return 1

    # Map positional args to legacy names for compatibility
    args.doi = args.input
    args.output = args.output_pos

    # Batch mode from CSV
    if args.csv:
        dois = _read_csv_column(args.csv, args.doi_column)
        if dois is None:
            print(f"❌ CSV must have a '{args.doi_column}' column (case-insensitive)")
            return 1

        # Apply start_from_row skip
        if args.start_from_row > 0:
            if args.start_from_row >= len(dois):
                print(f"❌ --start-from-row {args.start_from_row} is beyond CSV length ({len(dois)} rows)")
                return 1
            print(f"⏭️  Skipping first {args.start_from_row} rows")
            dois = dois[args.start_from_row:]

        print(f"📚 Processing {len(dois)} identifiers from {args.csv}")
        print(f"💾 Output directory: {args.output_dir}")
        print(f"⚙️  Workers: {args.workers}")
        print("-" * 60)

        if prompt_on_existing(dois, args) == ON_EXISTING_ABORT:
            print("Aborted; nothing downloaded.")
            return 0

        results = batch_fetch_pdfs(
            dois=dois,
            output_dir=args.output_dir,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
            workers=args.workers,
            create_missing_report=not args.no_missing_report,
            track_source=args.tracksource,
            start_offset=args.start_from_row,
            abstract_if_no_pdf=args.abstract_if_no_pdf,
            abstract_only=args.abstract_only,
            allow_xml_fallback=not args.no_xml_fallback,
            use_playwright=args.add_playwright,
            prioritize_xml=args.prioritize_xml,
            xml_only=args.xml_only,
            xml_html_only=args.xml_html_only,
            get_xml_or_html=args.get_xml_or_html,
            to_markdown=args.to_markdown,
            extract_images=args.extract_images,
            pull_figures=args.pull_figures,
            target_task=args.target_task,
            upgrade_existing=args.upgrade_existing,
            want_provenance=args.provenance,
            pull_supplementary=args.pull_supplementary,
            refresh_supplementary=args.refresh_supplementary,
            max_supplementary_bytes=int(args.max_supplementary_mb * 1024 * 1024),
            record_timeout=args.record_timeout or None,
            batch_timeout=args.batch_timeout or None,
            download_data_artifacts=args.download_data_artifacts,
            max_data_artifact_bytes=int(args.max_data_artifact_mb * 1024 * 1024),
            llm_adjudicate_artifacts=args.llm_adjudicate_artifacts,
            llm_agent_retrieval=args.llm_agent_retrieval,
            llm_backend=args.llm_backend,
            max_llm_records=args.max_llm_records,
            llm_model=args.llm_model,
            unpack_data_artifacts=args.unpack_data_artifacts,
            download_related_unverified=args.download_related_unverified,
            draft_requests=args.draft_requests,
            make_subfolder=args.make_subfolder,
            json_out=json_out,
        )

        success_count = sum(1 for r in results if r[1])
        failed = [r[0] for r in results if not r[1]]

        print("\n" + "=" * 60)
        print(f"✅ Success: {success_count}/{len(dois)} ({success_count/len(dois)*100:.1f}%)")
        _report_unverified_keeps()

        if failed:
            print(f"\n❌ Failed DOIs ({len(failed)}):")
            for doi in failed[:10]:  # Show first 10 failures
                print(f"   - {doi}")
            if len(failed) > 10:
                print(f"   ... and {len(failed)-10} more")

        return 0 if success_count == len(dois) else 1

    # Batch mode from PMID CSV
    elif args.pmid_csv:
        identifiers = _read_csv_column(args.pmid_csv, args.pmid_column,
                                       case_insensitive=False)
        if identifiers is None:
            print(f"❌ CSV must have a '{args.pmid_column}' column")
            return 1

        # Apply start_from_row skip and end_at_row limit
        if args.start_from_row > 0:
            if args.start_from_row >= len(identifiers):
                print(f"❌ --start-from-row {args.start_from_row} is beyond CSV length ({len(identifiers)} rows)")
                return 1
            print(f"⏭️  Skipping first {args.start_from_row} rows")
            identifiers = identifiers[args.start_from_row:]

        print(f"📚 Processing {len(identifiers)} PMIDs from {args.pmid_csv}")
        print(f"💾 Output directory: {args.output_dir}")
        print(f"⚙️  Workers: {args.workers}")
        print("-" * 60)

        # PMIDs resolve to DOIs inside the worker, so the scan behind this
        # cannot name their folders and will report nothing to skip. Called
        # anyway: a --pmid-csv row may already be a DOI, and the guards make
        # a zero count a silent no-op.
        if prompt_on_existing(identifiers, args) == ON_EXISTING_ABORT:
            print("Aborted; nothing downloaded.")
            return 0

        results = batch_fetch_pdfs(
            dois=identifiers,
            output_dir=args.output_dir,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
            workers=args.workers,
            create_missing_report=not args.no_missing_report,
            track_source=args.tracksource,
            start_offset=args.start_from_row,
            abstract_if_no_pdf=args.abstract_if_no_pdf,
            abstract_only=args.abstract_only,
            allow_xml_fallback=not args.no_xml_fallback,
            use_playwright=args.add_playwright,
            prioritize_xml=args.prioritize_xml,
            xml_only=args.xml_only,
            xml_html_only=args.xml_html_only,
            get_xml_or_html=args.get_xml_or_html,
            to_markdown=args.to_markdown,
            extract_images=args.extract_images,
            pull_figures=args.pull_figures,
            target_task=args.target_task,
            upgrade_existing=args.upgrade_existing,
            want_provenance=args.provenance,
            pull_supplementary=args.pull_supplementary,
            refresh_supplementary=args.refresh_supplementary,
            max_supplementary_bytes=int(args.max_supplementary_mb * 1024 * 1024),
            record_timeout=args.record_timeout or None,
            batch_timeout=args.batch_timeout or None,
            download_data_artifacts=args.download_data_artifacts,
            max_data_artifact_bytes=int(args.max_data_artifact_mb * 1024 * 1024),
            llm_adjudicate_artifacts=args.llm_adjudicate_artifacts,
            llm_agent_retrieval=args.llm_agent_retrieval,
            llm_backend=args.llm_backend,
            max_llm_records=args.max_llm_records,
            llm_model=args.llm_model,
            unpack_data_artifacts=args.unpack_data_artifacts,
            download_related_unverified=args.download_related_unverified,
            draft_requests=args.draft_requests,
            make_subfolder=args.make_subfolder,
            json_out=json_out,
        )

        success_count = sum(1 for r in results if r[1])
        failed = [r[0] for r in results if not r[1]]

        print("\n" + "=" * 60)
        print(f"✅ Success: {success_count}/{len(identifiers)} ({success_count/len(identifiers)*100:.1f}%)")
        _report_unverified_keeps()

        if failed:
            print(f"\n❌ Failed PMIDs ({len(failed)}):")
            for pid in failed[:10]:
                print(f"   - {pid}")
            if len(failed) > 10:
                print(f"   ... and {len(failed)-10} more")

        return 0 if success_count == len(identifiers) else 1

    # Single DOI mode
    elif args.doi:
        resolved_identifier = resolve_identifier_to_doi(args.doi, verbose=args.verbose)
        display_id = resolved_identifier or args.doi

        # Abstract-only mode for single DOI
        if args.abstract_only:
            from .fetch_abstract_from_doi import save_abstract_markdown
            os.makedirs(args.output_dir, exist_ok=True)
            safe_doi = doi_to_safe_filename(display_id)
            record_dir = (os.path.join(args.output_dir, safe_doi)
                          if args.make_subfolder else args.output_dir)
            if args.make_subfolder:
                os.makedirs(record_dir, exist_ok=True)
            ab_path = os.path.join(record_dir, f"{safe_doi}_abstract.md")
            if os.path.exists(ab_path):
                print(f"⏭️  Abstract already exists: {ab_path}")
                _emit_json(json_out, args.doi, resolved_identifier, ab_path,
                           JSON_STATUS_ALREADY_ON_DISK, source="existing")
                return 0
            result = save_abstract_markdown(display_id, record_dir, email=args.email, verbose=args.verbose)
            if result:
                print(f"\n📄 Abstract saved to {result}")
                _emit_json(json_out, args.doi, resolved_identifier, result,
                           JSON_STATUS_DOWNLOADED, source="abstract")
                return 0
            else:
                if args.make_subfolder:
                    _prune_empty_record_dir(record_dir)
                print(f"\n❌ No abstract found for {display_id}")
                _emit_json(json_out, args.doi, resolved_identifier, None,
                           JSON_STATUS_FAILED, reasons=["no abstract found"])
                return 1

        # Set only when --make-subfolder actually created a directory, so a run
        # that comes back empty-handed does not leave one behind.
        made_record_dir = None
        if args.output:
            save_path = args.output
        else:
            # Default single DOI output path if not provided
            # Use PMID digits for filename when DOI resolution fails
            if resolved_identifier:
                filename_id = resolved_identifier
            else:
                pmid = extract_pmid(args.doi)
                filename_id = pmid if pmid else canonicalize_doi(args.doi)
            safe_doi = doi_to_safe_filename(filename_id)
            os.makedirs(args.output_dir, exist_ok=True)
            # --make-subfolder is deliberately ignored above when args.output is
            # set: the user named an exact file, so honour it.
            record_dir = (os.path.join(args.output_dir, safe_doi)
                          if args.make_subfolder else args.output_dir)
            if args.make_subfolder:
                os.makedirs(record_dir, exist_ok=True)
                made_record_dir = record_dir
            save_path = os.path.join(record_dir, f"{safe_doi}.pdf")
            print(f"💾 No output path provided; using: {save_path}")

        # Allocated the way the batch worker allocates them, and for the same
        # reason: which source produced the file, and which artifacts a tiered
        # run wrote, are facts the chain already has and nothing else can
        # recover afterwards. Passing them changes no output -- fetch_pdf reads
        # _source_out only to skip re-verifying a file it did not fetch, which
        # the on-disk snapshot beside it already skips.
        _tiered = bool(args.prioritize_xml or args.xml_only
                       or args.xml_html_only or args.get_xml_or_html)
        source_out = [None]
        paths_out = [] if _tiered else None

        result = fetch_pdf(
            doi=resolved_identifier or args.doi,
            save_path=save_path,
            _source_out=source_out,
            _paths_out=paths_out,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
            allow_xml_fallback=not args.no_xml_fallback,
            use_playwright=args.add_playwright,
            prioritize_xml=args.prioritize_xml,
            xml_only=args.xml_only,
            xml_html_only=args.xml_html_only,
            get_xml_or_html=args.get_xml_or_html,
            to_markdown=args.to_markdown,
            target_task=args.target_task,
            upgrade_existing=args.upgrade_existing,
            want_provenance=args.provenance,
        )

        # A separate call rather than a parameter on fetch_pdf, and that
        # is the recursion guard: the tiered engine re-enters the chain at T5 via
        # retrieval/sources/legacy_pdf.py, which calls fetch_pdf with a
        # temporary save_path that is unlinked seconds later. A flag threaded
        # through that function would fire on the re-entry and scatter
        # supplementary siblings next to a temp file. Because the pass lives
        # above fetch_pdf instead, the T5 re-entry structurally cannot
        # reach it -- there is nothing to remember not to pass.
        if args.pull_supplementary:
            try:
                from .retrieval.supplementary import pull_for_record

                summary = pull_for_record(
                    raw_identifier=args.doi,
                    doi=resolved_identifier,
                    save_path=save_path,
                    max_file_bytes=int(args.max_supplementary_mb * 1024 * 1024),
                    refresh=args.refresh_supplementary,
                    verbose=args.verbose,
                    email=args.email,
                    delay=args.delay,
                    use_playwright=args.add_playwright,
                    download_data_artifacts=args.download_data_artifacts,
                    max_data_artifact_bytes=int(args.max_data_artifact_mb * 1024 * 1024),
                    llm_adjudicate_artifacts=args.llm_adjudicate_artifacts,
                    llm_agent_retrieval=args.llm_agent_retrieval,
                    llm_backend=args.llm_backend,
                    max_llm_records=args.max_llm_records,
                    llm_model=args.llm_model,
                    unpack_data_artifacts=args.unpack_data_artifacts,
                    download_related_unverified=args.download_related_unverified,
                )
                print(f"\n📎 {summary.written} supplementary file(s), "
                      f"{summary.skipped} skipped ({summary.status})")
                if summary.manifest_path:
                    print(f"   manifest: {summary.manifest_path}")
            except Exception as e:
                print(f"\n📎 supplementary pass failed: {str(e)[:200]}")

        if args.pull_figures:
            # A separate call above fetch_pdf, for the same reason the
            # supplementary pass is one: the tiered engine re-enters the chain
            # at T5 with a temporary save_path, and a flag threaded through
            # fetch_pdf would fire on that re-entry and scatter a figure
            # directory next to a file that is unlinked seconds later.
            try:
                from .retrieval.figures import pull_for_record as pull_figures_for_record

                summary = pull_figures_for_record(
                    raw_identifier=args.doi,
                    doi=resolved_identifier,
                    save_path=save_path,
                    verbose=args.verbose,
                    email=args.email,
                    delay=args.delay,
                )
                print(f"\n🖼️  {summary.written} figure(s), {summary.failed} not "
                      f"obtained ({summary.status}"
                      + (f": {summary.refusal}" if summary.refusal else "") + ")")
                if summary.manifest_path:
                    print(f"   manifest: {summary.manifest_path}")
            except Exception as e:
                print(f"\n🖼️  figure pass failed: {str(e)[:200]}")

        if args.extract_images:
            pdf = None
            if result and str(result).lower().endswith(".pdf"):
                pdf = result
            elif os.path.exists(save_path) and save_path.lower().endswith(".pdf"):
                pdf = save_path
            if pdf:
                try:
                    from .retrieval.extract_images import extract_images as _xi
                    img = _xi(pdf, verbose=args.verbose)
                    if img.status in ("ok", "skipped"):
                        print(f"\n🖼️  images: {img.status} "
                              f"({img.written} stream(s)) -> {img.manifest_path}")
                    elif img.status == "unavailable":
                        print(f"\n🖼️  {img.detail}")
                    else:
                        print(f"\n🖼️  images: {img.status}"
                              + (f" ({img.detail})" if img.detail else ""))
                except Exception as e:
                    print(f"\n🖼️  image extraction failed: {str(e)[:200]}")

        identity, reasons = _identity_and_reasons(result, save_path)
        if result:
            print(f"\n✅ Successfully downloaded to {result}")
            # "existing" is the chain's own word for "this was already here and
            # nothing re-read it", and it is the whole reason status is not a
            # bare boolean: the prose line above says "Successfully downloaded"
            # either way. Such a record carries identity null, because the
            # skip branch runs no identity check to report.
            on_disk = source_out[0] == "existing"
            # paths_out's last element is the tiered goal summary, not a path.
            others = [p for p in (paths_out or [])[:-1] if p != result]
            _emit_json(
                json_out, args.doi, resolved_identifier, result,
                JSON_STATUS_ALREADY_ON_DISK if on_disk else JSON_STATUS_DOWNLOADED,
                source=source_out[0], identity=identity, reasons=reasons,
                extra_paths=others,
            )
            return 0
        else:
            _prune_empty_record_dir(made_record_dir)
            print(f"\n❌ Failed to download PDF for {args.doi}")
            _emit_json(json_out, args.doi, resolved_identifier, None,
                       JSON_STATUS_FAILED, identity=identity,
                       reasons=reasons or ["no source produced a usable artifact"])
            return 1

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
