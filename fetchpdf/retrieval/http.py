"""The one way this package talks to the network.

Everything goes through HttpClient so that four things are true by construction
rather than by remembering: every request passes a per-host token bucket, every
response carries the metadata provenance needs, transient failures back off, and
no URL containing a secret is ever logged or written to disk.

Politeness parameters (mailto, tool/email, api keys) are injected here from the
environment rather than being spelled out at each call site, so a new source
cannot accidentally opt out of the polite pool.
"""

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError as _ReqConnErr,
    SSLError,
    Timeout,
)

from . import blocked as _blocked
from .._env import EMAIL as _DEFAULT_EMAIL
from .._http import USER_AGENT
from .ratelimit import HostRateLimiter

#: Query parameters whose values must never appear in a log line or a sidecar.
_SECRET_PARAMS = {"apikey", "api_key", "key", "token", "access_token", "auth"}
_SECRET_HEADERS = {"authorization", "x-api-key", "apikey"}

#: Headers that carry a credential, i.e. the ones worth REMOVING and retrying
#: when a host rejects them. Same membership as `_SECRET_HEADERS` today but a
#: separate name on purpose: that set answers "must this be redacted before it
#: is logged", this one answers "is this what the host just refused". A future
#: header could easily need one and not the other.
_CREDENTIAL_HEADERS = {"authorization", "x-api-key", "apikey"}

#: Which environment variable supplies the credential for a host, so a rejection
#: names the variable the user has to fix rather than "an API key somewhere".
_CREDENTIAL_ENV = {
    "api.semanticscholar.org": "SEMANTIC_SCHOLAR_API_KEY",
    "api.openalex.org": "OPENALEXAPIKEY",
    "api.core.ac.uk": "COREAPIKEY",
}


def _credential_env_for(host: str) -> Optional[str]:
    return _CREDENTIAL_ENV.get(host)


#: Hosts already reported as rejecting their credential, so the warning is
#: emitted once per process rather than once per request.
_WARNED_CREDENTIALS: set = set()

_SECRET_IN_PATH_RE = re.compile(
    r"(?i)\b(apikey|api_key|access_token|token)[=/]([^&/?#\s]+)"
)


def redact(url: Optional[str]) -> Optional[str]:
    """Replace secret query-parameter values with REDACTED.

    Applied to every URL before it is logged or persisted. Elsevier takes its
    key as a query parameter, so an un-redacted provenance sidecar would be a
    credential file that looks like an audit record.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if parts.query:
            cleaned = [
                (k, "REDACTED" if k.lower() in _SECRET_PARAMS else v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            ]
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(cleaned), parts.fragment)
            )
    except ValueError:
        pass
    return _SECRET_IN_PATH_RE.sub(r"\1=REDACTED", url)


@dataclass
class Response:
    """A completed request, with everything provenance needs to explain it."""

    url: str                 # final URL after redirects, redacted
    request_url: str         # as requested, redacted
    status: int
    content: bytes
    content_type: str
    headers: Dict[str, str]
    elapsed: float
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        """Parse as JSON. Raises ValueError on anything that is not.

        Deliberately not silently returning {}: a source that asked for JSON and
        got an HTML error page served as 200 needs to know, because that is
        exactly the failure this package exists to catch.
        """
        import json as _json

        return _json.loads(self.text)

    def json_or(self, default):
        try:
            return self.json()
        except ValueError:
            return default


@dataclass
class Download:
    """The outcome of a size-capped streaming GET written straight to disk.

    `path` is set only on a complete, under-cap write, so `ok` is the single
    question a caller has to ask. Everything else exists so the supplementary
    manifest can say *why* a file is not there -- "refused, 4.2 GB" and "nothing
    was offered" are different facts and must not collapse into one.
    """

    url: str                              # final URL after redirects, redacted
    request_url: str                      # as requested, redacted
    status: int
    content_type: str = ""
    bytes_written: int = 0
    sha256: str = ""                      # of the bytes actually written
    path: Optional[str] = None
    outcome: str = "ok"                   # ok | too-large | blocked | http-error | empty | unreachable
    declared_length: Optional[int] = None  # Content-Length as served, when sent
    elapsed: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


class HttpClient:
    """Rate-limited, retrying, redacting HTTP for the retrieval path."""

    #: Transport-level failures worth another attempt. HTTP 4xx/5xx are not
    #: retried here -- a 403 from a publisher TDM link is an answer, not a blip.
    _TRANSIENT = (SSLError, _ReqConnErr, Timeout, ChunkedEncodingError)

    def __init__(self, limiter: HostRateLimiter, email: Optional[str] = None,
                 verbose: bool = False, timeout: int = 30):
        self.limiter = limiter
        self.email = email or _DEFAULT_EMAIL
        self.verbose = verbose
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.call_count = 0

    # -- politeness ---------------------------------------------------------

    def polite_params(self, host: str, params: Optional[dict] = None) -> dict:
        """Add each API's required or rate-limit-lifting identification."""
        params = dict(params or {})
        if not self.email:
            return params
        if host == "api.crossref.org":
            params.setdefault("mailto", self.email)
        elif host.endswith("ncbi.nlm.nih.gov"):
            params.setdefault("tool", "fetchpdf")
            params.setdefault("email", self.email)
            api_key = os.getenv("ENTREZ_EUTILS_API_KEY")
            if api_key:
                params.setdefault("api_key", api_key)
        elif host == "api.unpaywall.org":
            params.setdefault("email", self.email)
        elif host == "www.ebi.ac.uk":
            params.setdefault("email", self.email)
        return params

    def polite_headers(self, host: str, headers: Optional[dict] = None) -> dict:
        headers = dict(headers or {})
        if host == "api.semanticscholar.org":
            key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
            if key:
                headers.setdefault("x-api-key", key)
        elif host == "api.openalex.org":
            key = os.getenv("OPENALEXAPIKEY")
            if key:
                headers.setdefault("Authorization", f"Bearer {key}")
        elif host == "api.core.ac.uk":
            key = os.getenv("COREAPIKEY")
            if key:
                headers.setdefault("Authorization", f"Bearer {key}")
        return headers

    # -- the request --------------------------------------------------------

    def get(self, url, params=None, headers=None, timeout=None, retries=3,
            allow_redirects=True, polite=True, stream_limit=None) -> Response:
        """GET with rate limiting, backoff and redaction.

        Never raises for an HTTP status: sources decide what a status means.
        Transport failures after `retries` attempts surface as a synthetic
        status 0 response, so a dead host demotes the source instead of killing
        the record.
        """
        host = urlparse(url).netloc
        if polite:
            params = self.polite_params(host, params)
            headers = self.polite_headers(host, headers)

        backoff = 0.5
        last_error = ""
        for attempt in range(max(1, retries)):
            self.limiter.acquire(host)
            started = time.monotonic()
            try:
                r = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout or self.timeout,
                    allow_redirects=allow_redirects,
                )
                self.call_count += 1
                elapsed = time.monotonic() - started

                # A key we sent and the host rejected is WORSE than no key:
                # unkeyed, these APIs answer at a lower rate limit; keyed with a
                # dead credential they answer 401/403 and the source drops out
                # of the chain entirely. Measured 2026-08-17: an expired
                # SEMANTIC_SCHOLAR_API_KEY turned every Semantic Scholar call --
                # step 5 of the default resolution chain -- into a 403, while
                # the same request with no key at all returned 200. Nothing
                # said so, because a dead source just looks like a source that
                # had nothing. Retry once without the credential and say which
                # key is bad, so the failure is a warning rather than a silent
                # capability loss.
                if r.status_code in (401, 403) and polite:
                    stripped = {k: v for k, v in (headers or {}).items()
                                if k.lower() not in _CREDENTIAL_HEADERS}
                    if stripped != (headers or {}):
                        bad = _credential_env_for(host) or "its API key"
                        # Once per host per process. The keyed hosts sit in the
                        # per-record resolution chain, so a per-call warning
                        # prints once per paper and buries the run log under
                        # thousands of copies of one fact.
                        if host not in _WARNED_CREDENTIALS:
                            _WARNED_CREDENTIALS.add(host)
                            print(f"    ⚠ {host} rejected {bad} ({r.status_code}); "
                                  f"retrying unkeyed at the lower rate limit for "
                                  f"the rest of this run. Refresh or unset it to "
                                  f"silence this.")
                        headers = stripped
                        polite = False       # do not re-add it on the next pass
                        continue

                # 429/503 are worth waiting out; they mean "later", not "no".
                if r.status_code in (429, 503) and attempt < retries - 1:
                    wait = _retry_after(r) or backoff * (2 ** attempt)
                    self.limiter.penalize(host, wait)
                    if self.verbose:
                        print(f"    {host} {r.status_code}; backing off {wait:.1f}s")
                    time.sleep(wait)
                    continue

                content = r.content
                if stream_limit and len(content) > stream_limit:
                    content = content[:stream_limit]
                return Response(
                    url=redact(r.url),
                    request_url=redact(url),
                    status=r.status_code,
                    content=content,
                    content_type=r.headers.get("content-type", "").split(";")[0].strip().lower(),
                    headers={
                        k: ("REDACTED" if k.lower() in _SECRET_HEADERS else v)
                        for k, v in r.headers.items()
                    },
                    elapsed=elapsed,
                )
            except self._TRANSIENT as e:
                last_error = str(e)[:200]
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
                    continue
            except Exception as e:  # malformed URL, bad encoding, ...
                last_error = str(e)[:200]
                break

        if self.verbose:
            print(f"    {host} unreachable: {last_error}")
        return Response(
            url=redact(url),
            request_url=redact(url),
            status=0,
            content=b"",
            content_type="",
            headers={},
            elapsed=0.0,
        )

    # -- the JSON request ---------------------------------------------------

    def post_json(self, url, payload, headers=None, timeout=None, retries=2,
                  polite=False) -> Response:
        """POST a JSON body, with the same rate limiting, backoff and redaction.

        Added for the retrieval agent's OpenRouter backend, and routed through
        here rather than through `requests` directly for one reason: `redact`
        and `_SECRET_HEADERS`. A bearer token that reaches a log line reaches
        the manifest and the sidecar too, and those are files a corpus gets
        shared as.

        Fewer retries than `get` by default. A model call is expensive and
        slow, so a repeat is not the cheap insurance it is for a metadata
        lookup, and an inference endpoint that 429s wants a real wait rather
        than three quick attempts.

        Never raises for an HTTP status, exactly as `get` does not -- the
        caller decides what a status means, and a dead endpoint must degrade
        the agent rather than kill the record.
        """
        host = urlparse(url).netloc
        if polite:
            headers = self.polite_headers(host, headers)

        backoff = 1.0
        last_error = ""
        for attempt in range(max(1, retries)):
            self.limiter.acquire(host)
            started = time.monotonic()
            try:
                r = self.session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout or self.timeout,
                )
                self.call_count += 1
                elapsed = time.monotonic() - started

                if r.status_code in (429, 503) and attempt < retries - 1:
                    wait = _retry_after(r) or backoff * (2 ** attempt)
                    self.limiter.penalize(host, wait)
                    if self.verbose:
                        print(f"    {host} {r.status_code}; backing off {wait:.1f}s")
                    time.sleep(wait)
                    continue

                return Response(
                    url=redact(r.url),
                    request_url=redact(url),
                    status=r.status_code,
                    content=r.content,
                    content_type=r.headers.get("content-type", "").split(";")[0].strip().lower(),
                    headers={
                        k: ("REDACTED" if k.lower() in _SECRET_HEADERS else v)
                        for k, v in r.headers.items()
                    },
                    elapsed=elapsed,
                )
            except self._TRANSIENT as e:
                last_error = str(e)[:200]
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
                    continue
            except Exception as e:
                last_error = str(e)[:200]
                break

        if self.verbose:
            print(f"    {host} unreachable: {last_error}")
        return Response(url=redact(url), request_url=redact(url), status=0,
                        content=b"", content_type="", headers={}, elapsed=0.0)

    # -- the size-capped transfer -------------------------------------------

    def download(self, url, dest, max_bytes, params=None, headers=None, timeout=None,
                 retries=2, allow_redirects=True, polite=True,
                 chunk_size=65536) -> Download:
        """Stream a URL to `dest`, aborting cleanly above `max_bytes`.

        Unlike get(), nothing is buffered in memory and nothing is ever
        truncated. get()'s stream_limit keeps the first N bytes of an oversized
        body, which is right for sniffing a landing page and catastrophic for a
        file: a 300 MB prefix of a 400 MB .xlsx is a zip with a corrupt central
        directory that some readers open far enough to yield wrong numbers.
        Here an over-cap body is abandoned and its partial removed, so `dest` is
        complete or absent -- never partial.

        Never raises for an HTTP status, and never raises for a transport
        failure: like get(), a dead host comes back as a synthetic status 0 so
        one bad URL demotes a file rather than killing the record.
        """
        if str(url).startswith("file://"):
            # A provider that had to fetch the bytes itself hands them back as a
            # local path. supplement_atypon does this because its URLs only work
            # from inside the browser session that clicked them, so there is
            # nothing here to re-request -- but the file still has to go through
            # the same cap, hashing, sniffing and manifesting as everything else.
            return _adopt_local_file_impl(url, dest, max_bytes)

        host = urlparse(url).netloc
        if polite:
            params = self.polite_params(host, params)
            headers = self.polite_headers(host, headers)

        directory = os.path.dirname(os.path.abspath(dest)) or "."
        backoff = 0.5
        last_error = ""
        for attempt in range(max(1, retries)):
            self.limiter.acquire(host)
            started = time.monotonic()
            temp_path = None
            try:
                r = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout or self.timeout,
                    allow_redirects=allow_redirects,
                    stream=True,
                )
                self.call_count += 1
                with r:
                    declared = _content_length(r)

                    if r.status_code in (429, 503) and attempt < retries - 1:
                        wait = _retry_after(r) or backoff * (2 ** attempt)
                        self.limiter.penalize(host, wait)
                        if self.verbose:
                            print(f"    {host} {r.status_code}; backing off {wait:.1f}s")
                        time.sleep(wait)
                        continue

                    outcome = _Outcome(r, declared, url)
                    if not (200 <= r.status_code < 300):
                        # 403 from a publisher and 404 from a repository are
                        # different facts. The first says nothing about whether
                        # the file exists -- it says a person could have it and
                        # this client could not -- and folding it into
                        # "http-error" is how a bot protection ends up recorded
                        # as a claim about what the authors published.
                        return outcome.failure(
                            _blocked.classify(r.status_code) or "http-error",
                            f"HTTP {r.status_code}")

                    # The cheap refusal: a declared length over the cap costs no
                    # bandwidth at all. Deliberately read off the streaming
                    # response rather than from a separate HEAD -- plenty of
                    # repository file hosts 405 HEAD, and Europe PMC answers it
                    # with content-length: 0 while GET returns real bytes.
                    if declared is not None and declared > max_bytes:
                        return outcome.failure(
                            "too-large", f"declared {declared} > cap {max_bytes}"
                        )

                    os.makedirs(directory, exist_ok=True)
                    handle, temp_path = tempfile.mkstemp(
                        prefix=".fetchpdf-dl-", dir=directory
                    )
                    hasher = hashlib.sha256()
                    written = 0
                    over_cap = False
                    with os.fdopen(handle, "wb") as f:
                        for chunk in r.iter_content(chunk_size):
                            if not chunk:
                                continue
                            written += len(chunk)
                            # Servers lie, and chunked responses declare nothing.
                            # This is the case the Content-Length check above
                            # cannot catch, and the one that makes truncating
                            # tempting.
                            if written > max_bytes:
                                over_cap = True
                                break
                            hasher.update(chunk)
                            f.write(chunk)

                    if over_cap:
                        _unlink(temp_path)
                        return outcome.failure(
                            "too-large", f"exceeded cap {max_bytes} mid-stream"
                        )
                    if written == 0:
                        _unlink(temp_path)
                        return outcome.failure("empty", "no bytes served")

                    apply_default_mode(temp_path)
                    os.replace(temp_path, dest)
                    return Download(
                        url=redact(r.url),
                        request_url=redact(url),
                        status=r.status_code,
                        content_type=_content_type(r),
                        bytes_written=written,
                        sha256=hasher.hexdigest(),
                        path=dest,
                        outcome="ok",
                        declared_length=declared,
                        elapsed=time.monotonic() - started,
                    )
            except self._TRANSIENT as e:
                # Retry from scratch, never resume: a resumed partial with a
                # mismatched offset is silent corruption of exactly the kind the
                # no-truncation rule above exists to prevent.
                _unlink(temp_path)
                last_error = str(e)[:200]
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
                    continue
            except BaseException as e:
                _unlink(temp_path)
                if isinstance(e, Exception):
                    last_error = str(e)[:200]
                    break
                raise

        if self.verbose:
            print(f"    {host} download failed: {last_error}")
        return Download(
            url=redact(url),
            request_url=redact(url),
            status=0,
            outcome="unreachable",
            detail=last_error,
        )


def _adopt_local_file_impl(url, dest, max_bytes) -> Download:
    """Bring a provider-fetched local file into the pipeline under the same rules.

    Same cap, same hash, same "complete or absent" guarantee as a network
    download -- the only difference is where the bytes came from. Copied rather
    than moved so the provider's scratch dir stays consistent and a retry is
    still possible.
    """
    source = url[len("file://"):]
    try:
        size = os.path.getsize(source)
    except OSError as error:
        return Download(url=redact(url), request_url=redact(url), status=0,
                        outcome="unreachable", detail=str(error)[:120])
    if size == 0:
        return Download(url=redact(url), request_url=redact(url), status=200,
                        outcome="empty", declared_length=0)
    if size > max_bytes:
        return Download(url=redact(url), request_url=redact(url), status=200,
                        outcome="too-large", declared_length=size,
                        detail=f"{size} bytes over the {max_bytes} cap")

    hasher = hashlib.sha256()
    written = 0
    try:
        with open(source, "rb") as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
                written += len(chunk)
    except OSError as error:
        try:
            os.unlink(dest)
        except OSError:
            pass
        return Download(url=redact(url), request_url=redact(url), status=0,
                        outcome="unreachable", detail=str(error)[:120])

    return Download(url=redact(url), request_url=redact(url), status=200,
                    content_type="", bytes_written=written,
                    sha256=hasher.hexdigest(), path=dest, outcome="ok",
                    declared_length=size)


class _Outcome:
    """Builds a failed Download that still carries the response metadata."""

    def __init__(self, response, declared, request_url):
        self.url = redact(response.url)
        self.request_url = redact(request_url)
        self.status = response.status_code
        self.content_type = _content_type(response)
        self.declared = declared

    def failure(self, outcome: str, detail: str) -> Download:
        return Download(
            url=self.url,
            request_url=self.request_url,
            status=self.status,
            content_type=self.content_type,
            outcome=outcome,
            declared_length=self.declared,
            detail=detail,
        )


def _content_type(response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


def _content_length(response) -> Optional[int]:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _default_file_mode() -> int:
    """The mode an ordinary open(..., "wb") would produce here.

    Read once, at import, because reading a umask means temporarily setting it:
    doing that later would race every other thread in batch_fetch_pdfs's pool,
    any of which could create a file during the window when the umask is 0.
    """
    try:
        current = os.umask(0)
        os.umask(current)
        return 0o666 & ~current
    except OSError:
        return 0o644


DELIVERED_FILE_MODE = _default_file_mode()


def apply_default_mode(path: str) -> None:
    """Give a delivered file the permissions an ordinary write would have had.

    tempfile.mkstemp creates 0600, which is right for a staging file and wrong for
    a delivered one: supplementary files would end up less readable than the PDF
    sitting beside them, which anyone sharing an output directory hits at once.
    """
    try:
        os.chmod(path, DELIVERED_FILE_MODE)
    except OSError:
        pass


def _unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _retry_after(response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 60.0)
    except (TypeError, ValueError):
        return None
