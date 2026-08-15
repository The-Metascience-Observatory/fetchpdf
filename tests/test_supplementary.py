"""Offline tests for --pull-supplementary.

No network. The doubles here follow tests/test_retrieval.py: canned responses
replayed by URL substring, and a fake resolver handing back a fixed
IdentifierSet. Real captures of the endpoints these stand in for live in
tests/fixtures/, recorded by tests/fixtures/capture.py.

The load-bearing tests here:

  test_nothing_is_ever_truncated          -- the corruption this prevents
  test_oversize_without_content_length_aborts_and_leaves_no_partial
  test_legacy_t5_reentry_never_pulls_supplements  -- why the flag is not a param
  test_manifest_is_the_skip_signal        -- what makes a re-run affordable
"""

import contextlib
import hashlib
import io
import json
import os

import pytest

from fetchpdf.retrieval.context import RetrievalContext
from fetchpdf.retrieval.http import HttpClient
from fetchpdf.retrieval.identifiers import IdentifierSet
from fetchpdf.retrieval.ratelimit import HostRateLimiter
from fetchpdf.retrieval.tiers import load_ladder


# --------------------------------------------------------------------------
# Doubles above the transport
#
# Kept local rather than imported from test_retrieval.py: there is no
# tests/__init__.py, and the existing suite already keeps its doubles local.
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, content=b"", status=200, content_type="", url="https://example.org/x"):
        self.content = content
        self.status = status
        self.content_type = content_type
        self.url = url
        self.request_url = url
        self.headers = {}
        self.elapsed = 0.0

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Replays canned responses by URL substring, and records what was asked."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default or FakeResponse(status=404)
        self.requests = []
        self.downloads = []
        self.limiter = None

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return self.default


def json_response(payload):
    return FakeResponse(content=json.dumps(payload).encode())


def ctx_with(http, **kwargs):
    return RetrievalContext(http=http, resolver=None, ladder=load_ladder(),
                            save_path=kwargs.pop("save_path", "/tmp/rec.pdf"), **kwargs)


# --------------------------------------------------------------------------
# Transport doubles
#
# HttpClient.download() drives requests directly (stream=True, iter_content,
# the response as a context manager), so testing the cap means faking the
# session rather than faking HttpClient. Everything above the transport gets
# the FakeHttp in test_retrieval.py instead.
# --------------------------------------------------------------------------


class FakeStreamResponse:
    def __init__(self, body=b"", status=200, headers=None, url="https://example.org/f.xlsx",
                 chunk=8192, declare_length=True):
        self.body = body
        self.status_code = status
        self.url = url
        self._chunk = chunk
        self.headers = dict(headers or {})
        self.headers.setdefault("content-type", "application/octet-stream")
        if declare_length:
            self.headers.setdefault("Content-Length", str(len(body)))
        self.closed = False
        self.bytes_yielded = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def iter_content(self, chunk_size=None):
        size = chunk_size or self._chunk
        for start in range(0, len(self.body), size):
            piece = self.body[start:start + size]
            self.bytes_yielded += len(piece)
            yield piece


class FakeSession:
    """Replays one canned streaming response per URL substring."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.headers = {}
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        if self.default is not None:
            return self.default
        return FakeStreamResponse(status=404, body=b"missing", url=url)


class ExplodingSession(FakeSession):
    """Raises a transient transport error for the first `fail_times` calls."""

    def __init__(self, response, fail_times=1):
        super().__init__(default=response)
        self.fail_times = fail_times
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            from requests.exceptions import ConnectionError as ReqConnErr

            raise ReqConnErr("connection reset")
        return super().get(url, **kwargs)


def client(session, **kwargs):
    http = HttpClient(HostRateLimiter({"_default": {"rps": 1000, "burst": 1000}}), **kwargs)
    http.session = session
    return http


def temps_in(directory):
    return [n for n in os.listdir(directory) if n.startswith(".fetchpdf-")]


CAP = 1024


# --------------------------------------------------------------------------
# 1. The size cap
# --------------------------------------------------------------------------


def test_nothing_is_ever_truncated(tmp_path):
    """A body just under the cap is written whole and hashes to the source.

    Regression test for the trap in HttpClient.get(): its stream_limit keeps the
    first N bytes of an oversized body. That is right for sniffing a landing
    page and catastrophic for a file -- a truncated .xlsx is a zip with a
    corrupt central directory that some readers open far enough to produce wrong
    numbers. download() must never produce one.
    """
    body = os.urandom(CAP - 1)
    dest = str(tmp_path / "out.xlsx")
    result = client(FakeSession(default=FakeStreamResponse(body=body))).download(
        "https://example.org/f.xlsx", dest, CAP
    )

    assert result.ok and result.outcome == "ok"
    assert result.bytes_written == len(body)
    with open(dest, "rb") as f:
        written = f.read()
    assert written == body
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert not temps_in(str(tmp_path))


def test_content_length_over_cap_skips_without_downloading(tmp_path):
    """The cheap refusal: a declared length over the cap costs no bandwidth."""
    body = os.urandom(CAP * 4)
    response = FakeStreamResponse(body=body)
    dest = str(tmp_path / "big.xlsx")
    result = client(FakeSession(default=response)).download(
        "https://example.org/f.xlsx", dest, CAP
    )

    assert not result.ok
    assert result.outcome == "too-large"
    assert result.declared_length == len(body)
    assert response.bytes_yielded == 0, "the body must not be transferred at all"
    assert not os.path.exists(dest)
    assert not temps_in(str(tmp_path))


def test_oversize_without_content_length_aborts_and_leaves_no_partial(tmp_path):
    """A server that declares nothing is caught mid-stream, not trusted.

    Chunked responses send no Content-Length, and some hosts under-report it.
    This is the case the pre-check cannot catch and the one that makes
    truncating tempting.
    """
    body = os.urandom(CAP + 1)
    response = FakeStreamResponse(body=body, declare_length=False, chunk=64)
    dest = str(tmp_path / "chunked.bin")
    result = client(FakeSession(default=response)).download(
        "https://example.org/f.bin", dest, CAP
    )

    assert not result.ok
    assert result.outcome == "too-large"
    assert result.declared_length is None
    assert not os.path.exists(dest)
    assert not temps_in(str(tmp_path)), "a partial transfer must leave nothing behind"


def test_a_body_exactly_at_the_cap_is_kept(tmp_path):
    """The cap is inclusive: refusing a file for being exactly 300 MB is a bug."""
    body = os.urandom(CAP)
    dest = str(tmp_path / "exact.bin")
    result = client(FakeSession(default=FakeStreamResponse(body=body, chunk=64))).download(
        "https://example.org/f.bin", dest, CAP
    )

    assert result.ok
    assert result.bytes_written == CAP


def test_http_error_reports_the_status_and_writes_nothing(tmp_path):
    dest = str(tmp_path / "gone.pdf")
    result = client(FakeSession(default=FakeStreamResponse(status=403, body=b"denied"))).download(
        "https://example.org/f.pdf", dest, CAP
    )

    assert not result.ok
    assert result.outcome == "http-error"
    assert result.status == 403
    assert not os.path.exists(dest)


def test_empty_body_is_not_written_as_a_file(tmp_path):
    """A 200 with no bytes is a failure, not a zero-length supplement."""
    dest = str(tmp_path / "empty.xlsx")
    result = client(FakeSession(default=FakeStreamResponse(body=b""))).download(
        "https://example.org/f.xlsx", dest, CAP
    )

    assert not result.ok
    assert result.outcome == "empty"
    assert not os.path.exists(dest)
    assert not temps_in(str(tmp_path))


def test_transport_failure_retries_from_scratch_and_never_resumes(tmp_path):
    """A retry re-fetches whole. A resumed partial is silent corruption."""
    body = os.urandom(CAP - 1)
    session = ExplodingSession(FakeStreamResponse(body=body), fail_times=1)
    dest = str(tmp_path / "flaky.bin")
    result = client(session).download("https://example.org/f.bin", dest, CAP, retries=2)

    assert result.ok
    assert session.calls == 2
    with open(dest, "rb") as f:
        assert f.read() == body, "a resumed transfer would have doubled or offset the bytes"
    assert not temps_in(str(tmp_path))


def test_unreachable_host_is_a_synthetic_status_zero(tmp_path):
    """Same contract as get(): a dead host demotes a file, never raises."""
    session = ExplodingSession(FakeStreamResponse(body=b"x" * 100), fail_times=99)
    result = client(session).download(
        "https://example.org/f.bin", str(tmp_path / "x.bin"), CAP, retries=2
    )

    assert not result.ok
    assert result.status == 0
    assert result.outcome == "unreachable"
    assert not temps_in(str(tmp_path))


def test_download_is_atomic(tmp_path):
    """dest appears only once complete -- never as a growing partial."""
    dest = str(tmp_path / "sub" / "deep.xlsx")
    body = os.urandom(200)

    class WatchingResponse(FakeStreamResponse):
        def iter_content(self, chunk_size=None):
            for piece in super().iter_content(chunk_size):
                assert not os.path.exists(dest), "dest existed before the transfer finished"
                yield piece

    result = client(FakeSession(default=WatchingResponse(body=body, chunk=32))).download(
        "https://example.org/f.xlsx", dest, CAP
    )
    assert result.ok and os.path.exists(dest)


def test_secrets_in_the_url_are_redacted_in_the_result(tmp_path):
    """Download carries URLs into the manifest, so it redacts like Response."""
    result = client(FakeSession(default=FakeStreamResponse(body=b"x" * 100))).download(
        "https://api.elsevier.com/content/object/pii/S1?apiKey=SECRETVALUE",
        str(tmp_path / "e.pdf"),
        CAP,
        polite=False,
    )

    assert result.ok
    assert "SECRETVALUE" not in result.request_url
    assert "REDACTED" in result.request_url


@pytest.mark.parametrize("header", ["not-a-number", "-1"])
def test_unparseable_content_length_falls_through_to_streaming(tmp_path, header):
    """A garbage Content-Length must not be read as 'under the cap' or crash."""
    body = os.urandom(CAP + 1)
    response = FakeStreamResponse(
        body=body, declare_length=False, chunk=64, headers={"Content-Length": header}
    )
    result = client(FakeSession(default=response)).download(
        "https://example.org/f.bin", str(tmp_path / "x.bin"), CAP
    )

    assert not result.ok
    assert result.outcome == "too-large", "the stream counter must still catch it"
    assert not temps_in(str(tmp_path))


# --------------------------------------------------------------------------
# 2. The shared enumeration layer
#
# T4 and --pull-supplementary now read one listing and apply different
# predicates to it. These tests pin both halves of that: the tier still sees
# exactly what it saw before, and the pass sees everything.
# --------------------------------------------------------------------------


FIGSHARE_LISTING = [
    {"name": "Supplementary_Table_1.xlsx", "download_url": "https://nd.figshare.com/1",
     "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "size": 4096, "supplied_md5": "aaa"},
    {"name": "Supplementary_Figure_S1.png", "download_url": "https://nd.figshare.com/2",
     "mimetype": "image/png", "size": 2048},
    {"name": "Supporting_Information.pdf", "download_url": "https://nd.figshare.com/3",
     "mimetype": "application/pdf", "size": 8192},
    {"name": "external_resource", "download_url": "https://nd.figshare.com/4",
     "is_link_only": True},
]


def figshare_http(listing=None, item_type="dataset"):
    return FakeHttp(routes={
        "/files": json_response(listing if listing is not None else FIGSHARE_LISTING),
        "articles/999": json_response({"defined_type_name": item_type}),
    })


def figshare_ids():
    return IdentifierSet(doi="10.6084/m9.figshare.999")


def test_t4_still_sees_only_structured_files():
    """The tier's output must not change: one .xlsx, and nothing else."""
    from fetchpdf.retrieval.sources.supplements import _structured_only
    from fetchpdf.retrieval.supplement_index import enumerate_figshare

    http = figshare_http()
    listed = enumerate_figshare(figshare_ids(), ctx_with(http), with_item_type=False)
    assert [name for name, _ in _structured_only(listed)] == ["Supplementary_Table_1.xlsx"]


def test_the_pass_sees_the_pdf_and_the_image_that_t4_drops():
    """The whole point of the split: --pull-supplementary wants the lot."""
    from fetchpdf.retrieval.supplement_index import enumerate_figshare

    listed = enumerate_figshare(figshare_ids(), ctx_with(figshare_http()))
    assert [f.name for f in listed] == [
        "Supplementary_Table_1.xlsx",
        "Supplementary_Figure_S1.png",
        "Supporting_Information.pdf",
    ]


def test_link_only_entries_are_not_files():
    """download_url on an is_link_only entry serves HTML with a 200."""
    from fetchpdf.retrieval.supplement_index import enumerate_figshare

    listed = enumerate_figshare(figshare_ids(), ctx_with(figshare_http()))
    assert "external_resource" not in [f.name for f in listed]


def test_t4_does_not_pay_for_the_item_type_request():
    """It filters on extension and never reads a role, so it must not ask."""
    from fetchpdf.retrieval.supplement_index import enumerate_figshare

    http = figshare_http()
    enumerate_figshare(figshare_ids(), ctx_with(http), with_item_type=False)
    assert not any("articles/999" in url and "/files" not in url
                   for url, _ in http.requests)


def test_declared_size_and_checksum_survive_enumeration():
    """The cap refuses for free only if the listing's numbers reach it."""
    from fetchpdf.retrieval.supplement_index import enumerate_figshare

    listed = enumerate_figshare(figshare_ids(), ctx_with(figshare_http()))
    assert listed[0].size_bytes == 4096
    assert listed[0].checksum == "md5:aaa"


def test_epmc_archive_defaults_to_excluding_inline_images():
    """Verified on PMC1817752: 4 members / 34 KB with the flag, 14 / 132 KB without.

    The default endpoint returns the article's whole media blob set, figures
    included. Without this parameter the pass would write every figure in the
    paper as a supplementary file.
    """
    from fetchpdf.retrieval.supplement_index import enumerate_epmc_archive

    ids = IdentifierSet(doi="10.1371/journal.pone.0000308", pmcid="PMC1817752")
    entry = enumerate_epmc_archive(ids, ctx_with(figshare_http()))[0]
    assert entry.is_archive
    assert "includeInlineImage=no" in entry.url

    legacy = enumerate_epmc_archive(ids, ctx_with(figshare_http()),
                                    include_inline_images=True)[0]
    assert "includeInlineImage" not in legacy.url


@pytest.mark.parametrize("name,expected", [
    ("Supplementary_Table_S1.xlsx", "supplement"),
    ("41586_2020_2649_MOESM1_ESM.pdf", "supplement"),
    ("mmc1.pdf", "supplement"),
    ("pone.0000308.s001.doc", "supplement"),
    ("1-s2.0-S2589004225028421-main.pdf", "article"),
    ("PMC1817752.pdf", "article"),
    ("manuscript.pdf", "article"),
    ("some_random_data.h5", "unknown"),
])
def test_role_heuristics(name, expected):
    from fetchpdf.retrieval.supplement_index import classify_role

    assert classify_role(name) == expected


def test_jats_ground_truth_beats_the_filename_heuristic():
    """The article's own statement about its own files wins.

    JATS routinely omits the extension in xlink:href, so the match has to be
    extension-insensitive or it misses almost everything.
    """
    from fetchpdf.retrieval.supplement_index import classify_role

    assert classify_role("weird_name.xls", jats_supplements=["weird_name"]) == "supplement"
    assert classify_role("Table_S1.jpg", jats_figures=["Table_S1.jpg"]) == "figure"


def test_a_publication_records_non_pdf_files_are_not_the_article():
    """Zenodo "publication" records carry supplements next to the paper PDF."""
    from fetchpdf.retrieval.supplement_index import classify_role

    assert classify_role("paper_as_deposited.pdf", "publication") == "article"
    assert classify_role("raw_measurements.csv", "publication") == "unknown"


def test_osf_folders_are_followed():
    """T4 dropped kind=folder entries and everything inside them."""
    from fetchpdf.retrieval.supplement_index import enumerate_osf

    def node(entries):
        return json_response({"data": entries})

    def rel(href):
        return {"relationships": {"files": {"links": {"related": {"href": href}}}}}

    guid = json_response({"data": rel("https://api.osf.io/v2/n/files/")})
    providers = node([rel("https://api.osf.io/v2/n/files/osfstorage/")])
    top = node([
        dict(attributes={"name": "top.csv", "kind": "file", "size": 10},
             links={"download": "https://files.osf.io/top.csv"}),
        dict(attributes={"name": "data", "kind": "folder"}, links={"download": None},
             **rel("https://api.osf.io/v2/n/files/osfstorage/data/")),
    ])
    inner = node([
        dict(attributes={"name": "nested.xlsx", "kind": "file", "size": 20},
             links={"download": "https://files.osf.io/nested.xlsx"}),
    ])

    http = FakeHttp(routes={
        "guids/abcde": guid,
        "osfstorage/data/": inner,
        "osfstorage/": top,
        "/n/files/": providers,
    })
    listed = enumerate_osf(IdentifierSet(doi="10.17605/osf.io/abcde"), ctx_with(http))
    assert [f.name for f in listed] == ["top.csv", "nested.xlsx"]


def test_one_provider_raising_does_not_abort_the_others():
    """Mirrors engine._run_source: a bad repository costs us that repository."""
    from fetchpdf.retrieval.supplement_index import SupplementFile, enumerate_all

    def boom(ids, ctx):
        raise RuntimeError("malformed JSON")

    def fine(ids, ctx):
        return [SupplementFile(name="ok.csv", url="https://x/1", provider="fine")]

    files, reports = enumerate_all(
        figshare_ids(), ctx_with(figshare_http()),
        providers=(("boom", boom), ("fine", fine)),
    )
    assert [f.name for f in files] == ["ok.csv"]
    assert reports[0]["status"] == "error" and "malformed JSON" in reports[0]["reason"]
    assert reports[1]["status"] == "ok"


def test_provider_rank_is_stable_against_appending():
    """Appending a provider must not renumber files an earlier one produced."""
    from fetchpdf.retrieval import supplement_index

    ranks = [supplement_index.provider_rank(n) for n in supplement_index.PROVIDER_NAMES]
    assert ranks == sorted(ranks) == list(range(len(supplement_index.PROVIDER_NAMES)))
    assert supplement_index.provider_rank("jats_manifest") == 0
    # An unknown provider sorts last rather than colliding with a real one.
    assert supplement_index.provider_rank("something_new") == len(
        supplement_index.PROVIDER_NAMES)
    # A DataCite fan-out ranks with DataCite, since that is where it came from.
    assert (supplement_index.provider_rank("datacite_related:zenodo_files")
            == supplement_index.provider_rank("datacite_related"))


def test_provider_names_match_the_registry():
    """PROVIDER_NAMES exists so provider_rank does not import every provider
    module; it has to stay in step with the registry it summarises."""
    from fetchpdf.retrieval.supplement_index import PROVIDER_NAMES, _providers

    assert tuple(name for name, _ in _providers()) == PROVIDER_NAMES


# --------------------------------------------------------------------------
# 3. The pass: naming, dedupe, manifest, caps
# --------------------------------------------------------------------------


class FakeDownloader:
    """An HttpClient stand-in whose download() writes canned bodies.

    Honours declared lengths and the cap the same way the real one does, so the
    pass's accounting is tested against the same contract.
    """

    def __init__(self, bodies=None, declared=None, statuses=None, content_types=None):
        self.bodies = bodies or {}
        self.declared = declared or {}
        self.statuses = statuses or {}
        self.content_types = content_types or {}
        self.downloads = []
        self.requests = []
        self.limiter = object()

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        return FakeResponse(status=404)

    def _match(self, url, table, default=None):
        for fragment, value in table.items():
            if fragment in url:
                return value
        return default

    def download(self, url, dest, max_bytes, **kwargs):
        from fetchpdf.retrieval.http import Download

        self.downloads.append((url, dest, max_bytes))
        body = self._match(url, self.bodies)
        status = self._match(url, self.statuses, 200)
        content_type = self._match(url, self.content_types, "")
        if body is None:
            return Download(url=url, request_url=url, status=404, outcome="http-error")
        if status != 200:
            return Download(url=url, request_url=url, status=status,
                            outcome="http-error", content_type=content_type)
        declared = self._match(url, self.declared, len(body))
        if declared is not None and declared > max_bytes:
            return Download(url=url, request_url=url, status=200, outcome="too-large",
                            declared_length=declared, content_type=content_type)
        if len(body) > max_bytes:
            return Download(url=url, request_url=url, status=200, outcome="too-large",
                            content_type=content_type)
        with open(dest, "wb") as f:
            f.write(body)
        return Download(url=url, request_url=url, status=200, path=dest,
                        bytes_written=len(body), sha256=hashlib.sha256(body).hexdigest(),
                        content_type=content_type, declared_length=declared)


def provider_of(*files):
    def enumerate_them(ids, ctx):
        return list(files)
    return (("test_provider", enumerate_them),)


def si_name(index, original_name="", extension="", stem="10.1234--x"):
    """Expected supplementary filename, from the module's own rule.

    Computed rather than hardcoded: these tests are about the index, the
    extension and the SI tag, none of which change when the descriptor rule is
    tuned. Thirty-one literal filenames had to be touched the one time it was.
    """
    from fetchpdf.retrieval.supplementary import si_filename

    return si_filename(stem, index, original_name, extension)


def sf(name, url, **kwargs):
    from fetchpdf.retrieval.supplement_index import SupplementFile

    kwargs.setdefault("provider", "test_provider")
    kwargs.setdefault("role", "supplement")
    return SupplementFile(name=name, url=url, **kwargs)


def pull(tmp_path, http, providers, **kwargs):
    from fetchpdf.retrieval.supplementary import pull_for_record

    kwargs.setdefault("max_file_bytes", CAP)
    return pull_for_record(
        raw_identifier="10.1234/x", doi="10.1234/x",
        save_path=str(tmp_path / "10.1234--x.pdf"),
        http=http, providers=providers, **kwargs
    )


def manifest_of(tmp_path):
    from fetchpdf.retrieval.supplementary import read_manifest

    return read_manifest(str(tmp_path / "10.1234--x_supplementary_info.json"))


BODY_A = b"A" * 200
BODY_B = b"B" * 300


def test_files_land_as_numbered_flat_siblings(tmp_path):
    http = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
    summary = pull(tmp_path, http, provider_of(
        sf("Supplementary Table S1.xlsx", "https://x/a"),
        sf("Supporting Information.pdf", "https://x/b"),
    ))

    assert summary.status == "ok" and summary.written == 2
    names = sorted(os.listdir(tmp_path))
    assert names == [
        "10.1234--x_supplementary_info.json",
        si_name(1, "Supplementary Table S1.xlsx", ".xlsx"),
        si_name(2, "Supporting Information.pdf", ".pdf"),
    ]
    assert not temps_in(str(tmp_path))


def test_the_manifest_is_the_only_record_of_the_original_names(tmp_path):
    """Flat numbering discards the filenames, so this cannot be optional."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(
        sf("Supplementary Table S1.xlsx", "https://x/a", label="Table S1",
           size_bytes=200, checksum="md5:abc"),
    ))

    manifest = manifest_of(tmp_path)
    entry = manifest["files"][0]
    assert entry["index"] == 1
    assert entry["filename"] == si_name(1, "Supplementary Table S1.xlsx", ".xlsx")
    assert entry["original_name"] == "Supplementary Table S1.xlsx"
    assert entry["label"] == "Table S1"
    assert entry["provider"] == "test_provider"
    assert entry["bytes"] == 200
    assert entry["sha256"] == hashlib.sha256(BODY_A).hexdigest()
    assert manifest["counts"] == {"written": 1, "skipped": 0, "bytes_written": 200}


def test_numbering_is_deterministic_across_listing_order(tmp_path):
    """Same content, different listing order, same names on disk."""
    def run(directory, order):
        http = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
        from fetchpdf.retrieval.supplementary import pull_for_record
        pull_for_record(
            raw_identifier="10.1234/x", doi="10.1234/x",
            save_path=str(directory / "10.1234--x.pdf"),
            http=http, providers=provider_of(*order), max_file_bytes=CAP,
        )
        from fetchpdf.retrieval.supplementary import read_manifest
        manifest = read_manifest(str(directory / "10.1234--x_supplementary_info.json"))
        return {f["original_name"]: f["filename"] for f in manifest["files"]}

    first = sf("Table_S1.xlsx", "https://x/a")
    second = sf("Table_S2.xlsx", "https://x/b")
    forward = tmp_path / "forward"
    backward = tmp_path / "backward"
    forward.mkdir()
    backward.mkdir()

    assert run(forward, [first, second]) == run(backward, [second, first])


def test_natural_sort_puts_S2_before_S10(tmp_path):
    http = FakeDownloader(bodies={"/2": BODY_A, "/10": BODY_B})
    pull(tmp_path, http, provider_of(
        sf("Table_S10.xlsx", "https://x/10"),
        sf("Table_S2.xlsx", "https://x/2"),
    ))
    mapping = {f["original_name"]: f["index"] for f in manifest_of(tmp_path)["files"]}
    assert mapping["Table_S2.xlsx"] < mapping["Table_S10.xlsx"]


def test_duplicate_content_does_not_consume_an_index(tmp_path):
    """A gap would make _3 mean different things on different runs."""
    http = FakeDownloader(bodies={"/a": BODY_A, "/copy": BODY_A, "/b": BODY_B})
    summary = pull(tmp_path, http, provider_of(
        sf("first.xlsx", "https://x/a"),
        sf("second.xlsx", "https://x/copy"),
        sf("third.xlsx", "https://x/b"),
    ))

    assert summary.written == 2
    manifest = manifest_of(tmp_path)
    assert [f["index"] for f in manifest["files"]] == [1, 2]
    assert manifest["files"][1]["original_name"] == "third.xlsx"
    duplicate = [s for s in manifest["skipped"] if s["reason"] == "duplicate-of"][0]
    assert duplicate["duplicate_of_index"] == 1
    assert not any(n.startswith("10.1234--x_supplementary_info_3") for n in os.listdir(tmp_path))


def test_declared_checksums_dedupe_before_any_transfer(tmp_path):
    """A declared md5 match saves the bandwidth entirely."""
    http = FakeDownloader(bodies={"/a": BODY_A, "/copy": BODY_A})
    pull(tmp_path, http, provider_of(
        sf("first.xlsx", "https://x/a", checksum="md5:deadbeef"),
        sf("elsewhere.xlsx", "https://x/copy", checksum="md5:deadbeef"),
    ))
    assert len(http.downloads) == 1, "the second file must not be fetched"


def test_the_repo_copy_of_the_main_pdf_is_not_written_twice(tmp_path):
    """Zenodo and figshare list the paper alongside its supplements."""
    paper = b"%PDF-1.7" + b"p" * 500
    (tmp_path / "10.1234--x.pdf").write_bytes(paper)
    http = FakeDownloader(bodies={"/paper": paper, "/data": BODY_B})
    summary = pull(tmp_path, http, provider_of(
        sf("deposited_copy.pdf", "https://x/paper"),
        sf("measurements.csv", "https://x/data"),
    ))

    assert summary.written == 1
    skipped = [s for s in manifest_of(tmp_path)["skipped"]
               if s["reason"] == "duplicate-of-main-artifact"][0]
    assert skipped["main_artifact"] == "10.1234--x.pdf"
    written = [n for n in os.listdir(tmp_path) if "_supplementary_info_1" in n]
    assert written == [si_name(1, "measurements.csv", ".csv")], written


def test_main_artifact_dedupe_works_when_the_artifact_is_xml(tmp_path):
    """Under the tiered path the record on disk may be .xml, not .pdf."""
    full_text = b"<article><body>" + b"x" * 500 + b"</body></article>"
    (tmp_path / "10.1234--x.xml").write_bytes(full_text)
    http = FakeDownloader(bodies={"/xml": full_text})
    summary = pull(tmp_path, http, provider_of(sf("fulltext.xml", "https://x/xml")))

    assert summary.written == 0
    assert manifest_of(tmp_path)["skipped"][0]["reason"] == "duplicate-of-main-artifact"


def test_declared_oversize_is_refused_without_downloading(tmp_path):
    http = FakeDownloader(bodies={"/huge": BODY_A})
    summary = pull(tmp_path, http, provider_of(
        sf("raw_imaging.h5", "https://x/huge", size_bytes=CAP * 100),
    ))

    # "partial", not "none_found": something was offered and we did not get it.
    assert summary.written == 0 and summary.status == "partial"
    assert http.downloads == [], "a declared oversize costs no bandwidth"
    skipped = manifest_of(tmp_path)["skipped"][0]
    assert skipped["reason"] == "too-large"
    assert skipped["declared_bytes"] == CAP * 100


def test_a_refused_file_is_distinguishable_from_nothing_offered(tmp_path):
    """'no supplements' and 'one 4 GB file refused' are different facts."""
    quiet = tmp_path / "quiet"
    loud = tmp_path / "loud"
    quiet.mkdir()
    loud.mkdir()

    nothing = pull(quiet, FakeDownloader(), provider_of())
    refused = pull(loud, FakeDownloader(bodies={"/h": BODY_A}), provider_of(
        sf("huge.h5", "https://x/h", size_bytes=CAP * 100)))

    assert nothing.written == refused.written == 0
    assert nothing.skipped == 0 and refused.skipped == 1
    assert manifest_of(quiet)["status"] == "none_found"
    assert manifest_of(loud)["skipped"][0]["declared_bytes"] == CAP * 100


def test_html_error_page_is_not_saved_as_a_spreadsheet(tmp_path):
    http = FakeDownloader(
        bodies={"/a": b"<!DOCTYPE html><html><body>Access denied</body></html>" + b" " * 100},
        content_types={"/a": "text/html"},
    )
    summary = pull(tmp_path, http, provider_of(sf("Table_S1.xlsx", "https://x/a")))

    assert summary.written == 0
    assert manifest_of(tmp_path)["skipped"][0]["reason"] == "not-a-document"
    assert not any("_supplementary_info_1" in n for n in os.listdir(tmp_path))


def test_a_bot_challenge_page_is_rejected(tmp_path):
    """NCBI serves exactly this, with a 200, for /bin/ blob URLs."""
    challenge = (b'<html><head><base href="https://www.google.com/recaptcha/challengepage/">'
                 + b"x" * 200 + b"</html>")
    http = FakeDownloader(bodies={"/bin": challenge})
    summary = pull(tmp_path, http, provider_of(sf("pone.s001.doc", "https://x/bin")))

    assert summary.written == 0
    skipped = manifest_of(tmp_path)["skipped"][0]
    assert skipped["reason"] == "not-a-document" and "challenge" in skipped["detail"]


def test_filenames_never_escape_the_output_directory(tmp_path):
    """The output name is generated, so a hostile listing name cannot reach it."""
    http = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B, "/c": b"C" * 200})
    summary = pull(tmp_path, http, provider_of(
        sf("../../etc/passwd", "https://x/a"),
        sf("C:\\windows\\evil.xlsx", "https://x/b"),
        sf("nul\x00byte.csv", "https://x/c"),
    ))

    assert summary.written == 3
    for path in summary.paths:
        assert os.path.dirname(os.path.abspath(path)) == str(tmp_path)
    # Nothing but generated names exists in the directory, so no fragment of a
    # hostile listing name reached the filesystem at all.
    # Only the extension survives from a listing name, never a path fragment.
    assert sorted(os.listdir(tmp_path)) == [
        "10.1234--x_supplementary_info.json",
        si_name(1, "C:\\windows\\evil.xlsx", ".xlsx"),
        si_name(2, "nul\x00byte.csv", ".csv"),
        si_name(3, "../../etc/passwd", ""),
    ]


def test_no_extension_is_invented_when_nothing_declares_one(tmp_path):
    """A fabricated extension is a lie a downstream tool will act on."""
    http = FakeDownloader(bodies={"/a": b"\x07\x08opaque" + b"\x00" * 200})
    summary = pull(tmp_path, http, provider_of(sf("dataset", "https://x/a")))

    assert summary.paths == [str(tmp_path / si_name(1, "dataset", ""))]


@pytest.mark.parametrize("name,content_type,body,expected", [
    ("data.XLSX", "", b"x" * 200, ".xlsx"),
    ("noext", "application/pdf", b"x" * 200, ".pdf"),
    ("noext", "", b"%PDF-1.4" + b"x" * 200, ".pdf"),
    ("noext", "", b"PK\x03\x04" + b"x" * 200, ".zip"),
])
def test_extension_is_derived_in_a_fixed_order(tmp_path, name, content_type, body, expected):
    http = FakeDownloader(bodies={"/a": body}, content_types={"/a": content_type})
    summary = pull(tmp_path, http, provider_of(sf(name, "https://x/a")))
    assert summary.paths[0].endswith(expected)
    assert "_supplementary_info_1" in os.path.basename(summary.paths[0])


def test_secrets_are_redacted_in_the_manifest(tmp_path):
    """An audit file must not become a credential file."""
    http = FakeDownloader(bodies={"/object": BODY_A})
    pull(tmp_path, http, provider_of(
        sf("mmc1.pdf", "https://api.elsevier.com/content/object?apiKey=SUPERSECRET"),
    ))

    raw = (tmp_path / "10.1234--x_supplementary_info.json").read_text()
    assert "SUPERSECRET" not in raw
    assert "REDACTED" in raw


def test_figures_and_the_article_are_not_pulled(tmp_path):
    http = FakeDownloader(bodies={"/f": BODY_A, "/p": BODY_B, "/s": b"S" * 200})
    summary = pull(tmp_path, http, provider_of(
        sf("Figure_1.jpg", "https://x/f", role="figure"),
        sf("manuscript.pdf", "https://x/p", role="article"),
        sf("Table_S1.xlsx", "https://x/s", role="supplement"),
    ))
    assert summary.written == 1
    assert manifest_of(tmp_path)["files"][0]["original_name"] == "Table_S1.xlsx"


def test_provider_reports_reach_the_manifest(tmp_path):
    """'not applicable' and 'errored' must not collapse into an empty list."""
    def boom(ids, ctx):
        raise RuntimeError("bad JSON")

    summary = pull(tmp_path, FakeDownloader(), (("broken", boom),))
    manifest = manifest_of(tmp_path)
    assert summary.status == "error"
    assert manifest["providers"][0]["status"] == "error"
    assert "bad JSON" in manifest["providers"][0]["reason"]


def test_per_record_file_limit_is_recorded_not_silent(tmp_path):
    """A capped run must say so, or it reads as 'that was everything'."""
    bodies = {f"/{i}": bytes([65 + i]) * 200 for i in range(5)}
    http = FakeDownloader(bodies=bodies)
    summary = pull(tmp_path, http, provider_of(
        *[sf(f"t{i}.csv", f"https://x/{i}") for i in range(5)]
    ), max_files=3)

    assert summary.written == 3
    assert any(s["reason"] == "record-budget" for s in manifest_of(tmp_path)["skipped"])


# --------------------------------------------------------------------------
# 4. Archive expansion
# --------------------------------------------------------------------------


def zip_bytes(members, compress=True):
    import io
    import zipfile

    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buffer.getvalue()


def archive_of(*members, url="https://ebi/PMC1/supplementaryFiles"):
    return sf("PMC1_SupplementaryFiles.zip", url, is_archive=True,
              mimetype="application/zip")


def test_the_epmc_zip_is_expanded_into_siblings(tmp_path):
    """T4 stores the archive; the pass stores what is in it."""
    body = zip_bytes([("pone.s001.doc", b"D" * 200), ("pone.s004.xls", b"X" * 300)])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 2
    assert sorted(os.listdir(tmp_path)) == [
        "10.1234--x_supplementary_info.json",
        si_name(1, "pone.s001.doc", ".doc"),
        si_name(2, "pone.s004.xls", ".xls"),
    ]
    manifest = manifest_of(tmp_path)
    assert [f["original_name"] for f in manifest["files"]] == [
        "pone.s001.doc", "pone.s004.xls"]
    assert all(f["container"] == "PMC1_SupplementaryFiles.zip" for f in manifest["files"])
    assert manifest["files"][0]["sha256"] == hashlib.sha256(b"D" * 200).hexdigest()
    assert not temps_in(str(tmp_path)), "the archive itself must not be left behind"


def test_the_archive_is_fetched_under_a_larger_cap_than_its_members(tmp_path):
    """A bundle's members are unknowable until it lands, so it needs its own budget."""
    body = zip_bytes([("a.csv", b"A" * 200)])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    pull(tmp_path, http, provider_of(archive_of()), max_file_bytes=CAP)

    _, _, cap_used = http.downloads[0]
    assert cap_used > CAP


def test_a_zip_member_over_the_per_file_cap_is_skipped_not_truncated(tmp_path):
    body = zip_bytes([("small.csv", b"S" * 200), ("huge.h5", b"H" * (CAP + 50))])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 1
    skipped = [s for s in manifest_of(tmp_path)["skipped"] if s["reason"] == "too-large"]
    assert skipped[0]["original_name"] == "huge.h5"
    assert skipped[0]["declared_bytes"] == CAP + 50


def test_a_traversing_zip_member_name_is_basenamed(tmp_path):
    body = zip_bytes([("../../../etc/passwd", b"P" * 200)])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 1
    assert os.path.dirname(os.path.abspath(summary.paths[0])) == str(tmp_path)
    # Even the manifest records the basename, so nothing downstream can rejoin it.
    assert manifest_of(tmp_path)["files"][0]["original_name"] == "passwd"


def test_a_decompression_bomb_is_abandoned(tmp_path):
    """zipfile will happily inflate one; the ratio guard refuses first."""
    body = zip_bytes([("bomb.txt", b"\0" * (CAP * 200))])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 0
    skipped = manifest_of(tmp_path)["skipped"][0]
    assert skipped["reason"] == "decompression-ratio"
    assert not any(n.endswith(".txt") for n in os.listdir(tmp_path))


def test_a_body_that_is_not_an_archive_is_reported_as_such(tmp_path):
    http = FakeDownloader(bodies={"supplementaryFiles": b"not a zip at all" + b"x" * 200})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 0
    assert manifest_of(tmp_path)["skipped"][0]["reason"] == "not-an-archive"


def test_directory_entries_in_an_archive_are_not_written(tmp_path):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data/", b"")
        archive.writestr("data/values.csv", b"V" * 200)
    http = FakeDownloader(bodies={"supplementaryFiles": buffer.getvalue()})
    summary = pull(tmp_path, http, provider_of(archive_of()))

    assert summary.written == 1
    assert manifest_of(tmp_path)["files"][0]["original_name"] == "values.csv"


# --------------------------------------------------------------------------
# 5. Skip and repair
# --------------------------------------------------------------------------


def test_the_manifest_is_the_skip_signal(tmp_path):
    """On a real corpus most records have no supplements, and re-enumerating
    every one of them on every run is the entire cost of the flag from run 2 on."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    first = pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))
    assert first.status == "ok" and len(http.downloads) == 1

    again = FakeDownloader(bodies={"/a": BODY_A})
    second = pull(tmp_path, again, provider_of(sf("t.csv", "https://x/a")))

    assert second.status == "skipped"
    assert again.downloads == [] and again.requests == [], "a re-run must cost zero HTTP"
    assert second.written == 1


def test_a_record_with_no_supplements_is_also_skipped_on_re_run(tmp_path):
    """The majority case. Without a manifest this would re-enumerate forever."""
    first = pull(tmp_path, FakeDownloader(), provider_of())
    assert first.status == "none_found"

    probe = FakeDownloader()
    assert pull(tmp_path, probe, provider_of(sf("new.csv", "https://x/a"))).status == "skipped"
    assert probe.downloads == []


def test_refresh_re_enumerates_and_picks_up_new_deposits(tmp_path):
    pull(tmp_path, FakeDownloader(), provider_of())

    http = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, http, provider_of(sf("late_deposit.csv", "https://x/a")),
                   refresh=True)

    assert summary.status == "ok" and summary.written == 1
    assert manifest_of(tmp_path)["files"][0]["original_name"] == "late_deposit.csv"


def test_a_missing_file_is_refetched_at_its_recorded_index(tmp_path):
    """Deleting _1 must not renumber _2."""
    http = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
    pull(tmp_path, http, provider_of(
        sf("first.csv", "https://x/a"), sf("second.csv", "https://x/b")))
    os.unlink(tmp_path / si_name(1, "first.csv", ".csv"))

    again = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
    summary = pull(tmp_path, again, provider_of())

    assert summary.status == "skipped"
    assert (tmp_path / si_name(1, "first.csv", ".csv")).read_bytes() == BODY_A
    assert (tmp_path / si_name(2, "second.csv", ".csv")).read_bytes() == BODY_B
    assert len(again.downloads) == 1, "only the missing file is refetched"


def test_the_repair_pass_costs_nothing_when_nothing_is_missing(tmp_path):
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))

    probe = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, probe, provider_of())
    assert probe.downloads == []


def test_a_redacted_url_is_not_guessed_at_on_repair(tmp_path):
    """Refetching would need the secret, and inventing one is worse than saying so."""
    http = FakeDownloader(bodies={"/object": BODY_A})
    pull(tmp_path, http, provider_of(
        sf("mmc1.pdf", "https://api.elsevier.com/content/object?apiKey=SECRET")))
    os.unlink(tmp_path / si_name(1, "mmc1.pdf", ".pdf"))

    again = FakeDownloader(bodies={"/object": BODY_A})
    summary = pull(tmp_path, again, provider_of())
    assert again.downloads == []
    assert "restored 0 of 1" in summary.detail


# --------------------------------------------------------------------------
# 5b. Integrity: existence is not integrity
#
# A corpus once held 30 git-lfs pointer stubs where its supplementary data
# should have been -- every pointer's oid matching the sha256 the manifest
# recorded, i.e. the bytes were downloaded and hashed correctly and something
# later replaced them. The repair pass tested os.path.isfile and nothing else,
# so every re-run declared the corpus complete. A hollow supplement reads
# downstream as "this paper published no data".
# --------------------------------------------------------------------------


LFS_STUB = (b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
            b"size 9362\n")


def test_a_git_lfs_stub_is_not_a_present_file(tmp_path):
    """The exact corpus failure: content replaced by a 130-byte pointer."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("DataSheet1.xlsx", "https://x/a")))
    target = tmp_path / si_name(1, "DataSheet1.xlsx", ".xlsx")
    target.write_bytes(LFS_STUB)

    again = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, again, provider_of())
    assert len(again.downloads) == 1, "a stub must be re-fetched, not counted present"
    assert target.read_bytes() == BODY_A
    assert "lfs_pointer" in summary.detail


def test_a_truncated_file_is_refetched(tmp_path):
    """Half a spreadsheet is a corrupt zip that still opens far enough to lie."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))
    target = tmp_path / si_name(1, "t.csv", ".csv")
    target.write_bytes(BODY_A[:50])

    again = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, again, provider_of())
    assert len(again.downloads) == 1
    assert target.read_bytes() == BODY_A
    assert "size_mismatch" in summary.detail


def test_a_hash_mismatch_at_the_right_size_is_refetched(tmp_path):
    """Same length, different bytes -- only the digest can tell."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))
    target = tmp_path / si_name(1, "t.csv", ".csv")
    target.write_bytes(b"Z" * len(BODY_A))

    again = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, again, provider_of())
    assert len(again.downloads) == 1
    assert target.read_bytes() == BODY_A
    assert "hash_mismatch" in summary.detail


def test_an_intact_corpus_still_costs_zero_http(tmp_path):
    """The manifest's skip-signal property must survive the integrity check."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))

    probe = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, probe, provider_of())
    assert probe.downloads == []
    assert "nothing missing" in summary.detail


def test_a_manifest_without_integrity_metadata_never_refetches(tmp_path):
    """Older manifests predate the digest; absent metadata is not corruption."""
    from fetchpdf.retrieval.supplementary import read_manifest, write_manifest

    http = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("t.csv", "https://x/a")))

    manifest_path = str(tmp_path / "10.1234--x_supplementary_info.json")
    manifest = read_manifest(manifest_path)
    for entry in manifest["files"]:
        entry.pop("sha256", None)
        entry.pop("bytes", None)
    write_manifest(manifest_path, manifest, False)

    probe = FakeDownloader(bodies={"/a": BODY_A})
    pull(tmp_path, probe, provider_of())
    assert probe.downloads == []


def test_a_corrupt_file_returns_at_its_recorded_index(tmp_path):
    """Repairing _2 must not renumber _3, exactly as for a deleted file."""
    http = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
    pull(tmp_path, http, provider_of(sf("one.csv", "https://x/a"),
                                     sf("two.csv", "https://x/b")))
    second = tmp_path / si_name(2, "two.csv", ".csv")
    second.write_bytes(LFS_STUB)

    again = FakeDownloader(bodies={"/a": BODY_A, "/b": BODY_B})
    pull(tmp_path, again, provider_of())
    assert second.read_bytes() == BODY_B
    assert (tmp_path / si_name(1, "one.csv", ".csv")).read_bytes() == BODY_A


# --------------------------------------------------------------------------
# 6. Wiring: the invariants that would break silently
# --------------------------------------------------------------------------


def batch_module():
    """The fetchpdf *module*, not the function of the same name.

    fetchpdf/__init__.py re-exports the function, which shadows the submodule
    attribute on the package, so `import fetchpdf.fetchpdf as m` binds
    m to the function. sys.modules is the unambiguous way in.
    """
    import sys

    import fetchpdf.fetchpdf  # noqa: F401  -- populates sys.modules

    return sys.modules["fetchpdf.fetchpdf"]


class _KeptBytes(io.BytesIO):
    """A BytesIO whose contents survive being closed.

    batch_fetch_pdfs wraps our buffer in a TextIOWrapper, and that wrapper closes
    what it wrapped when it is collected -- which would take the output we want
    to assert on with it.
    """

    def __init__(self):
        super().__init__()
        self.kept = b""

    def close(self):
        self.kept = self.getvalue()
        super().close()

    def value(self) -> bytes:
        return self.kept if self.closed else self.getvalue()


class _Stdio:
    """Something batch_fetch_pdfs can safely wrap."""

    def __init__(self):
        self.buffer = _KeptBytes()

    def write(self, text):
        self.buffer.write(text.encode("utf-8", errors="replace"))

    def flush(self):
        pass


@contextlib.contextmanager
def isolated_stdio():
    """Let batch_fetch_pdfs rebind stdout without wrecking the rest of the run.

    It wraps sys.stdout.buffer in a fresh TextIOWrapper for UTF-8 safety
    (batch_fetch_pdfs, near the top). Under pytest that wrapper closes pytest's
    capture buffer when it is collected, and every later test in the process then
    dies with "I/O operation on closed file". Hand it a BytesIO to wrap instead,
    and put the originals back afterwards.
    """
    import sys

    saved_out, saved_err = sys.stdout, sys.stderr
    out = _Stdio()
    sys.stdout, sys.stderr = out, _Stdio()
    try:
        yield out
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        sys.stdout, sys.stderr = saved_out, saved_err


def printed(captured):
    return captured.buffer.value().decode("utf-8", errors="replace")


def run_batch(**kwargs):
    """batch_fetch_pdfs with stdout isolated. Returns (results, printed text)."""
    kwargs.setdefault("create_missing_report", False)
    with isolated_stdio() as out:
        results = batch_module().batch_fetch_pdfs(**kwargs)
    return results, printed(out)


def test_the_flag_is_off_by_default(tmp_path, monkeypatch):
    """Mirrors test_default_path_is_never_the_engine: the default must not change."""
    import fetchpdf.retrieval.supplementary as supplementary
    module = batch_module()

    def poisoned(*a, **k):
        raise AssertionError("the supplementary pass ran without the flag")

    monkeypatch.setattr(supplementary, "pull_for_record", poisoned)
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", lambda *a, **k: None)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path))


def test_the_flag_on_actually_runs_the_pass(tmp_path, monkeypatch):
    """The other half of the test above -- otherwise it proves nothing."""
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    seen = []

    def record(**kwargs):
        seen.append(kwargs)
        return supplementary.SupplementarySummary(status="none_found")

    monkeypatch.setattr(supplementary, "pull_for_record", record)
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", lambda *a, **k: None)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path), pull_supplementary=True)
    assert len(seen) == 1
    assert seen[0]["doi"] == "10.1234/x"


def test_fetch_pdf_from_doi_has_no_pull_supplementary_parameter():
    """The recursion guard is structural, and this is what enforces it.

    retrieval/sources/legacy_pdf.py re-enters fetch_pdf at T5 with a
    .fetchpdf-t5-* temp path that is unlinked seconds later, and it does not pass
    _visited -- so the re-entry is indistinguishable from a top-level call. Any
    flag threaded through that function would fire on it and scatter
    supplementary siblings next to a temp file. Keeping the pass above
    fetch_pdf_from_doi means there is nothing to remember not to pass.
    """
    import inspect

    from fetchpdf.fetchpdf import fetch_pdf

    parameters = inspect.signature(fetch_pdf).parameters
    assert "pull_supplementary" not in parameters
    assert "refresh_supplementary" not in parameters


def test_legacy_t5_reentry_never_pulls_supplements(tmp_path, monkeypatch):
    """The behavioural half of the test above."""
    import fetchpdf.retrieval.supplementary as supplementary
    from fetchpdf.retrieval.sources import legacy_pdf

    monkeypatch.setattr(supplementary, "pull_for_record", lambda **k: (_ for _ in ()).throw(
        AssertionError("the T5 re-entry reached the supplementary pass")))
    monkeypatch.setattr(batch_module(), "fetch_pdf", lambda *a, **k: None)

    ctx = ctx_with(FakeHttp(), save_path=str(tmp_path / "rec.pdf"))
    assert legacy_pdf.fetch_via_legacy_chain(IdentifierSet(doi="10.1234/x"), ctx) is None
    assert not [n for n in os.listdir(tmp_path) if "supplementary" in n]


def test_the_pass_shares_the_batch_rate_limiter(tmp_path, monkeypatch):
    """A per-record limiter would multiply the configured per-host rate by the
    worker count, on the most request-heavy path in the tool."""
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    captured = {}

    def record(**kwargs):
        captured["http"] = kwargs.get("http")
        captured["resolver"] = kwargs.get("resolver")
        return supplementary.SupplementarySummary(status="none_found")

    monkeypatch.setattr(supplementary, "pull_for_record", record)
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", lambda *a, **k: None)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path), pull_supplementary=True)

    assert captured["http"].limiter is captured["resolver"].http.limiter


def test_the_gate_split_does_not_widen_the_default_skip(tmp_path, monkeypatch):
    """ARTIFACT_EXTENSIONS breadth belongs to the tiered path, not to this flag.

    Before the split, the resolver and the skip-if-exists suffix list were bound
    in the same branch. Reusing that one gate for --pull-supplementary would have
    started skipping default-path records that happen to have a .fulltext.html on
    disk -- records the chain would otherwise have re-attempted.
    """
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    (tmp_path / "10.1234--x.fulltext.html").write_text("<html>partial</html>")
    attempted = []

    monkeypatch.setattr(supplementary, "pull_for_record",
                        lambda **k: supplementary.SupplementarySummary(status="none_found"))
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf",
                        lambda *a, **k: attempted.append(a[0]) or None)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path), pull_supplementary=True)
    assert attempted == ["10.1234/x"], "the record must still be attempted"


def test_a_supplementary_crash_never_fails_the_record(tmp_path, monkeypatch):
    """An exception here would escape download_one and future.result(), taking
    the whole batch down -- thousands of records lost to one bad repository."""
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    monkeypatch.setattr(supplementary, "pull_for_record",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("repository exploded")))
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)

    def fake_fetch(doi, save_path, *a, **k):
        with open(save_path, "wb") as f:
            f.write(b"%PDF-1.7" + b"x" * 2000)
        return save_path

    monkeypatch.setattr(module, "fetch_pdf", fake_fetch)

    results, _ = run_batch(dois=["10.1234/x"], output_dir=str(tmp_path),
                           pull_supplementary=True)
    assert results[0][1] is True, "the record succeeded and must be reported as such"
    assert not os.path.exists(tmp_path / "failed_dois.csv")


def test_no_supplements_is_not_a_failure(tmp_path, monkeypatch):
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    monkeypatch.setattr(supplementary, "pull_for_record",
                        lambda **k: supplementary.SupplementarySummary(status="none_found"))
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)

    def fake_fetch(doi, save_path, *a, **k):
        with open(save_path, "wb") as f:
            f.write(b"%PDF-1.7" + b"x" * 2000)
        return save_path

    monkeypatch.setattr(module, "fetch_pdf", fake_fetch)

    results, _ = run_batch(dois=["10.1234/x"], output_dir=str(tmp_path),
                           pull_supplementary=True)
    assert results[0][1] is True
    assert not os.path.exists(tmp_path / "failed_dois.csv")


def test_supplementary_files_are_not_counted_in_the_format_tally(tmp_path, monkeypatch):
    """format_label() on a _supplementary_info_1.pdf returns "pdf", so a record
    with four supplementary PDFs would report "pdf 5"."""
    module = batch_module()
    import fetchpdf.retrieval.supplementary as supplementary

    def write_supplements(**kwargs):
        stem = supplementary.stem_for(kwargs["save_path"])
        for n in (1, 2, 3):
            with open(f"{stem}_supplementary_info_{n}.pdf", "wb") as f:
                f.write(b"%PDF-1.7" + b"s" * 200)
        return supplementary.SupplementarySummary(status="ok", written=3)

    monkeypatch.setattr(supplementary, "pull_for_record", write_supplements)
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)

    def fake_fetch(doi, save_path, *a, **k):
        with open(save_path, "wb") as f:
            f.write(b"%PDF-1.7" + b"x" * 2000)
        return save_path

    monkeypatch.setattr(module, "fetch_pdf", fake_fetch)

    results, _ = run_batch(dois=["10.1234/x"], output_dir=str(tmp_path),
                           pull_supplementary=True, track_source=True)

    # One record, one artifact: the supplements are files, not retrieved formats.
    assert len(results) == 1
    assert results[0][2] == str(tmp_path / "10.1234--x.pdf")
    tracking = (tmp_path / "source_tracking.csv")
    if tracking.exists():
        rows = [r for r in tracking.read_text().splitlines() if "supplementary" in r]
        assert rows == [], "supplementary files must not appear in source_tracking.csv"


def test_abstract_only_disables_the_flag_with_a_warning(capsys):
    """--abstract-only downloads no files, so there is nothing to sit beside."""
    import sys

    from fetchpdf.fetchpdf import main

    argv = sys.argv
    sys.argv = ["fetchpdf", "--abstract-only", "--pull-supplementary"]
    try:
        main()
    finally:
        sys.argv = argv
    assert "--pull-supplementary is ignored" in capsys.readouterr().out


def test_a_non_positive_cap_is_rejected():
    import sys

    from fetchpdf.fetchpdf import main

    argv = sys.argv
    sys.argv = ["fetchpdf", "10.1234/x", "--pull-supplementary",
                "--max-supplementary-mb", "0"]
    try:
        with pytest.raises(SystemExit):
            main()
    finally:
        sys.argv = argv


def test_duplicates_do_not_make_a_run_partial(tmp_path):
    """The same file reachable three ways and stored once is a complete result.

    Real records hit this constantly -- on 10.1371/journal.pone.0000308 the four
    supplements are each reachable via Europe PMC, PMC S3, PLOS and Crossref
    components -- so counting dedupes as a shortfall would report almost every
    successful record as "partial".
    """
    http = FakeDownloader(bodies={"/a": BODY_A, "/same": BODY_A})
    summary = pull(tmp_path, http, provider_of(
        sf("t.csv", "https://x/a"),
        sf("t_from_elsewhere.csv", "https://x/same"),
    ))

    assert summary.written == 1 and summary.skipped == 1
    assert summary.status == "ok"


def test_a_delivered_file_is_not_left_at_mkstemp_permissions(tmp_path):
    """mkstemp creates 0600, which would make a supplement less readable than the
    PDF sitting beside it -- immediate trouble in a shared output directory."""
    import stat

    from fetchpdf.retrieval.http import DELIVERED_FILE_MODE

    dest = str(tmp_path / "out.xlsx")
    client(FakeSession(default=FakeStreamResponse(body=b"x" * 200))).download(
        "https://example.org/f.xlsx", dest, CAP)
    assert stat.S_IMODE(os.stat(dest).st_mode) == DELIVERED_FILE_MODE


# --------------------------------------------------------------------------
# 7. Real captures
#
# These assert against tests/fixtures/, recorded from the live APIs by
# tests/fixtures/capture.py. The reason they are captures and not mocks is that
# every failure they pin returns a success-shaped response, so a mock of what we
# assume the API sends would pass while the real thing broke.
# --------------------------------------------------------------------------


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name, mode="rb"):
    with open(os.path.join(FIXTURES, name), mode) as f:
        return f.read()


def test_the_epmc_default_bundle_is_mostly_figures():
    """The parameter that separates supplements from the whole media blob set.

    Verified against both captures: the default is 14 members and 132 KB, most of
    them g001/t001-style figure images; includeInlineImage=no is 4 members and
    34 KB, exactly s001-s004. Without the parameter --pull-supplementary would
    write every figure in the paper as a supplementary file.
    """
    import zipfile

    with zipfile.ZipFile(io.BytesIO(fixture("epmc_supplements_default.zip"))) as default:
        names = default.namelist()
    assert len(names) == 14
    assert any(".g001." in n for n in names), "figures are in the default bundle"

    with zipfile.ZipFile(io.BytesIO(fixture("epmc_supplements_no_images.zip"))) as filtered:
        filtered_names = sorted(filtered.namelist())
    assert filtered_names == [
        "pone.0000308.s001.doc", "pone.0000308.s002.doc",
        "pone.0000308.s003.txt", "pone.0000308.s004.xls",
    ]
    assert not any(".g0" in n for n in filtered_names)


def test_the_captured_epmc_bundle_expands_to_the_right_siblings(tmp_path):
    """End to end over a real archive, not a synthetic one."""
    body = fixture("epmc_supplements_no_images.zip")
    http = FakeDownloader(bodies={"supplementaryFiles": body},
                          content_types={"supplementaryFiles": "application/zip"})
    summary = pull(tmp_path, http, provider_of(
        sf("PMC1817752_SupplementaryFiles.zip",
           "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1817752/"
           "supplementaryFiles?includeInlineImage=no",
           is_archive=True)),
        max_file_bytes=1024 * 1024)

    assert summary.written == 4
    assert sorted(os.path.basename(p) for p in summary.paths) == [
        si_name(1, "pone.0000308.s001.doc", ".doc"),
        si_name(2, "pone.0000308.s002.doc", ".doc"),
        si_name(3, "pone.0000308.s003.txt", ".txt"),
        si_name(4, "pone.0000308.s004.xls", ".xls"),
    ]
    manifest = manifest_of(tmp_path)
    assert [f["original_name"] for f in manifest["files"]] == [
        "pone.0000308.s001.doc", "pone.0000308.s002.doc",
        "pone.0000308.s003.txt", "pone.0000308.s004.xls",
    ]


def test_the_bin_url_is_not_a_fetch_route():
    """Pins the finding that the JATS manifest cannot resolve its own hrefs.

    xlink:href is a bare filename, and the obvious base URL does not serve it.
    Captured with our own User-Agent: HTTP 404 with 48 KB of HTML. Others have
    seen a 200 serving reCAPTCHA from the same path. Either way it is not a file,
    and either way the guard rejects it -- which is the point.
    """
    from fetchpdf.retrieval.supplementary import _looks_like_a_document

    body = fixture("pmc_bin_not_a_file.html")
    assert len(body) > 40000, "a large HTML body, not a 404 with an empty one"
    plausible, why = _looks_like_a_document("pone.0000308.s001.doc", body[:4096],
                                            "text/html", len(body))
    assert not plausible and "HTML" in why


def test_the_jats_manifest_separates_supplements_from_figures():
    """The one thing filename heuristics cannot know for certain."""
    from fetchpdf.retrieval.supplement_pmc import enumerate_jats_manifest

    http = FakeHttp(routes={"fullTextXML": FakeResponse(
        content=fixture("epmc_fulltext_valid.xml"))})
    ctx = ctx_with(http, save_path="/tmp/does-not-exist/rec.pdf")
    assert enumerate_jats_manifest(IdentifierSet(pmcid="PMC1817752"), ctx) == []

    supplements = ctx.scratch["jats_supplements"]
    figures = ctx.scratch["jats_figures"]
    assert supplements == {
        "pone.0000308.s001.doc", "pone.0000308.s002.doc",
        "pone.0000308.s003.txt", "pone.0000308.s004.xls",
    }
    assert figures and not (figures & supplements), "the two sets must not overlap"


def test_an_abstract_stub_does_not_prove_there_are_no_supplements():
    """A denial stub parses cleanly and has no <supplementary-material> either.

    Trusting a zero count from a document with no <body> would record "this
    article has no supplements" for every paywalled record.
    """
    from fetchpdf.retrieval.supplement_pmc import enumerate_jats_manifest

    http = FakeHttp(routes={"fullTextXML": FakeResponse(
        content=fixture("efetch_denial_stub.xml"))})
    ctx = ctx_with(http, save_path="/tmp/does-not-exist/rec.pdf")
    enumerate_jats_manifest(IdentifierSet(pmcid="PMC3390974"), ctx)
    assert "jats_supplements" not in ctx.scratch


def test_pmc_s3_declares_sizes_and_md5s_before_transfer():
    """What makes the cap free on this route."""
    from fetchpdf.retrieval.supplement_pmc import enumerate_pmc_s3

    http = FakeHttp(routes={
        "prefix=PMC1817752.&": FakeResponse(
            content=b"<ListBucketResult><CommonPrefixes><Prefix>PMC1817752.1/"
                    b"</Prefix></CommonPrefixes></ListBucketResult>"),
        "metadata/PMC1817752.1.json": FakeResponse(content=fixture("pmc_s3_metadata.json")),
        "prefix=PMC1817752.1/": FakeResponse(content=fixture("pmc_s3_listing.xml")),
    })
    ctx = ctx_with(http, save_path="/tmp/does-not-exist/rec.pdf")
    files = enumerate_pmc_s3(IdentifierSet(doi="10.1371/journal.pone.0000308",
                                           pmcid="PMC1817752"), ctx)

    assert files, "the capture lists media_urls"
    assert all(f.url.startswith("https://pmc-oa-opendata.s3.amazonaws.com/") for f in files)
    assert all(f.checksum and f.checksum.startswith("md5:") for f in files)
    assert any(f.size_bytes for f in files), "sizes come from the ListBucket capture"
    # The article's own PDF/XML/text renditions are not supplementary files.
    assert not any(f.name.endswith((".pdf", ".xml", ".txt")) and "PMC1817752.1." in f.name
                   for f in files)


def test_datacite_related_is_gated_on_type_not_relation():
    """The capture is why. Those datasets were deposited as IsCitedBy/IsSourceOf,
    so a relationType whitelist -- the obvious design -- discards all of them."""
    import json as _json

    from fetchpdf.retrieval.supplement_graph import _accept_related

    payload = _json.loads(fixture("datacite_reverse_related.json", "r"))
    article = "10.1186/s40168-025-02261-0"
    accepted = [_accept_related(r, article, ctx_with(FakeHttp()))
                for r in payload.get("data") or []]
    kept = [doi for doi in accepted if doi]

    assert kept, "the datasets that name this article must survive the filter"
    relations = {
        str(rel.get("relationType", "")).lower()
        for record in payload["data"]
        for rel in (record.get("attributes") or {}).get("relatedIdentifiers") or []
        if article.lower() in str(rel.get("relatedIdentifier", "")).lower()
    }
    assert "issupplementto" not in relations, (
        "the premise of this test: none of them used the obvious relation")


def test_archive_members_are_numbered_by_name_not_archive_order():
    """A zip preserves whatever order the producer wrote, and Europe PMC's real
    s001-s004 bundle stores s002 first. Numbering by that would match neither
    _sort_key nor any reader's expectation."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(fixture("epmc_supplements_no_images.zip"))) as z:
        stored = [i.filename for i in z.infolist()]
    assert stored != sorted(stored), (
        "the premise: this real archive is not stored in name order")


def test_a_missing_archive_member_is_restored_from_the_archive(tmp_path):
    """A file that arrived by expansion carries the *archive's* URL in the
    manifest, so a naive refetch would write the whole zip where one member
    belongs -- the right bytes for entirely the wrong file."""
    body = zip_bytes([("a.csv", b"A" * 200), ("b.csv", b"B" * 300)])
    http = FakeDownloader(bodies={"supplementaryFiles": body})
    summary = pull(tmp_path, http, provider_of(archive_of()))
    assert summary.written == 2

    victim = tmp_path / si_name(2, "b.csv", ".csv")
    assert victim.read_bytes() == b"B" * 300
    victim.unlink()

    again = FakeDownloader(bodies={"supplementaryFiles": body})
    restored = pull(tmp_path, again, provider_of())

    assert "restored 1 of 1" in restored.detail
    assert victim.read_bytes() == b"B" * 300, "the member, not the archive"
    assert not temps_in(str(tmp_path))


# ---------------------------------------------------------------------------
# A supplementary file that is itself a zip gets unpacked beside it.
# ---------------------------------------------------------------------------


def _make_run(tmp_path, stem, max_file_bytes=10 * 1024 * 1024):
    """A bare _Run, enough for the unpacking methods. Uses the module's own
    SupplementFile and the existing `sf` helper rather than a parallel fake."""
    from types import SimpleNamespace

    from fetchpdf.retrieval.supplementary import _Run

    return _Run(
        stem=stem,
        ctx=SimpleNamespace(log=lambda *a, **k: None),
        http=None,
        ids=SimpleNamespace(doi="10.1/x"),
        max_file_bytes=max_file_bytes,
        max_total_bytes=2 * 1024 * 1024 * 1024,
        max_files=200,
    )


def _entry():
    return sf("bundle.zip", "https://example.org/bundle.zip")


def _zip_bytes(members):
    import io as _io, zipfile as _zf

    buffer = _io.BytesIO()
    # DEFLATED, not the default STORED: an uncompressed "bomb" is not a bomb, and
    # the ratio guard would correctly decline to fire on it.
    with _zf.ZipFile(buffer, "w", _zf.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_stored_zip_is_unpacked_with_supplementary_prefix(tmp_path):
    """Members keep the depositor's own filename, which is the point: 'Table_S1.xlsx'
    says what the file is where a positional '_7.xlsx' says nothing."""
    from fetchpdf.retrieval.supplementary import _is_zip

    archive = tmp_path / "rec_supplementary_info_1.zip"
    archive.write_bytes(_zip_bytes({"Table_S1.xlsx": b"x" * 200,
                                    "Appendix A.docx": b"y" * 200}))
    assert _is_zip(str(archive))

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run._unpack_stored_zip(str(archive), archive.name, _entry(), {"bytes": 0})

    names = sorted(p.name for p in tmp_path.iterdir())
    # Same rule as a downloaded file: index anchors it, descriptor rides along.
    assert any(n.startswith("rec_supplementary_info_") and n.endswith("Table_S1.xlsx")
               for n in names), names
    assert any(n.startswith("rec_supplementary_info_") and n.endswith("Appendix_A.docx")
               for n in names), names
    # The archive is removed once its members are safely out; the manifest keeps
    # the record of it, so provenance survives the file.
    assert archive.name not in names


def test_extracted_members_are_recorded_in_the_manifest(tmp_path):
    archive = tmp_path / "rec_supplementary_info_1.zip"
    archive.write_bytes(_zip_bytes({"data.csv": b"a,b\n1,2\n" * 40}))

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run._unpack_stored_zip(str(archive), archive.name, _entry(), {"bytes": 0})

    extracted = [k for k in run.kept if k.get("extracted_from")]
    assert len(extracted) == 1
    record = extracted[0]
    assert record["original_name"] == "data.csv"
    assert record["extracted_from"] == archive.name
    assert record["sha256"] and record["bytes"] > 0


def test_traversing_member_names_cannot_escape(tmp_path):
    """Member names are untrusted: '../../evil.sh' must not write outside."""
    archive = tmp_path / "rec_supplementary_info_1.zip"
    archive.write_bytes(_zip_bytes({"../../evil.sh": b"rm -rf /", "ok.txt": b"z" * 100}))

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run._unpack_stored_zip(str(archive), archive.name, _entry(), {"bytes": 0})

    assert not (tmp_path.parent.parent / "evil.sh").exists()
    written = sorted(p.name for p in tmp_path.iterdir())
    assert any("evil.sh" in n for n in written)          # basenamed, kept
    assert all(n.startswith("rec") for n in written)      # all inside, all prefixed
    assert all("_supplementary_info_" in n or n.endswith(".zip") for n in written)


def test_decompression_bomb_is_refused(tmp_path):
    """A zip claiming to expand far beyond its size never gets read."""
    archive = tmp_path / "rec_supplementary_info_1.zip"
    archive.write_bytes(_zip_bytes({"bomb.txt": b"0" * 5_000_000}))  # compresses tiny

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run.max_file_bytes = 1000
    run._unpack_stored_zip(str(archive), archive.name, _entry(), {"bytes": 0})

    assert not any("bomb" in p.name for p in tmp_path.iterdir() if p.suffix != ".zip")
    assert any(s.get("reason") == "decompression-ratio" for s in run.skipped)


def test_docx_is_not_unpacked(tmp_path):
    """A .docx IS a zip. Unpacking one would explode a document into XML parts."""
    from fetchpdf.retrieval.supplementary import _is_zip

    docx = tmp_path / "rec_supplementary_info_1.docx"
    docx.write_bytes(_zip_bytes({"word/document.xml": b"<w:document/>"}))
    assert _is_zip(str(docx)) is False


def test_colliding_member_names_do_not_overwrite(tmp_path):
    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    for n in (1, 2):
        archive = tmp_path / f"rec_supplementary_info_{n}.zip"
        archive.write_bytes(_zip_bytes({"same.csv": f"archive {n}".encode() * 40}))
        run._unpack_stored_zip(str(archive), archive.name, _entry(), {"bytes": 0})

    extracted = sorted(p.name for p in tmp_path.iterdir()
                       if "_supplementary_info_" in p.name and p.suffix == ".csv")
    assert len(extracted) == 2, extracted
    assert len(set(extracted)) == 2


def test_one_naming_rule_for_downloaded_and_extracted():
    """Two conventions would be worse than either: with a mixed corpus a reader
    cannot tell whether a missing descriptor means 'no name available' or 'a
    different code path made this file'."""
    from fetchpdf.retrieval.supplementary import INFIX, si_filename

    for original, ext in [("TablesS1-S5.docx", ".docx"), ("", ".pdf"),
                          ("jamanetwopen-e001.pdf", ".pdf")]:
        name = si_filename("10.1--x", 3, original, ext)
        assert name.startswith(f"10.1--x{INFIX}_3")
        assert name.endswith(ext)
        assert "suppl" in name.lower()      # every SI file is tagged as SI


def test_descriptor_is_omitted_when_it_would_say_nothing():
    from fetchpdf.retrieval.supplementary import si_filename

    assert si_filename("rec", 2, "", ".xlsx") == "rec_supplementary_info_2.xlsx"
    assert si_filename("rec", 2, "...", ".xlsx") == "rec_supplementary_info_2.xlsx"


def test_descriptor_is_bounded_so_filenames_stay_legal():
    from fetchpdf.retrieval.supplementary import DESCRIPTOR_MAX_CHARS, si_filename

    name = si_filename("10.1234--long.stem.here", 11, "z" * 500 + ".csv", ".csv")
    assert len(name.encode()) < 255
    assert name.count("z") <= DESCRIPTOR_MAX_CHARS


def test_extension_is_not_duplicated_in_the_descriptor():
    from fetchpdf.retrieval.supplementary import si_filename

    assert si_filename("rec", 1, "data.csv", ".csv") == "rec_supplementary_info_1_data.csv"


def test_the_archive_is_removed_but_its_record_survives(tmp_path):
    """Deleting the zip must not delete the account of where members came from."""
    archive = tmp_path / "rec_supplementary_info_1_bundle.zip"
    archive.write_bytes(_zip_bytes({"Table_S1.xlsx": b"x" * 200}))
    record = {"index": 1, "filename": archive.name, "bytes": archive.stat().st_size}

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run._unpack_stored_zip(str(archive), archive.name, _entry(), record)

    assert not archive.exists()
    assert record["removed_after_extraction"] is True
    assert record["extracted_members"] == 1
    assert record["filename"] is None


def test_a_zip_that_yields_nothing_is_kept(tmp_path):
    """If extraction produced no files, deleting the archive would lose data."""
    archive = tmp_path / "rec_supplementary_info_1_bundle.zip"
    archive.write_bytes(_zip_bytes({".DS_Store": b"junk"}))   # skipped as a dotfile
    record = {"index": 1, "filename": archive.name, "bytes": archive.stat().st_size}

    run = _make_run(tmp_path, stem=str(tmp_path / "rec"))
    run._unpack_stored_zip(str(archive), archive.name, _entry(), record)

    assert archive.exists(), "an archive that yielded nothing must survive"
    assert "removed_after_extraction" not in record


def test_repair_does_not_refetch_a_removed_archive(tmp_path):
    """Its file is absent BY DESIGN; refetching would restore the very zip whose
    members are already unpacked beside it."""
    from fetchpdf.retrieval.supplementary import _repair

    manifest = {
        "files": [
            {"index": 1, "filename": None, "url": "https://x/bundle.zip",
             "removed_after_extraction": True, "extracted_members": 1},
            {"index": 2, "filename": "rec_supplementary_info_2_Table.xlsx",
             "url": "https://x/bundle.zip"},
        ],
        "skipped": [],
    }
    (tmp_path / "rec_supplementary_info_2_Table.xlsx").write_bytes(b"x" * 50)
    path = tmp_path / "rec_supplementary_info.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = _repair(str(path), str(tmp_path / "rec"), None, None, None, None,
                      verbose=False, max_file_bytes=CAP)
    assert summary.status == "skipped"
    assert "nothing missing" in summary.detail


# --------------------------------------------------------------------------
# 8. Honest statuses: no-bundle, the completeness gate, the slow retry
# --------------------------------------------------------------------------


def speculative_bundle(url="https://ebi/PMC9/supplementaryFiles"):
    return sf("PMC9_SupplementaryFiles.zip", url, is_archive=True,
              mimetype="application/zip",
              extra={"pmcid": "PMC9", "speculative": True})


def declaring(declared, *files):
    """A provider that seeds the JATS declared-supplement scratch, like E1 does."""
    def enumerate_them(ids, ctx):
        ctx.scratch["jats_declared"] = dict(declared)
        ctx.scratch["jats_supplements"] = set(declared)
        return list(files)
    return (("test_provider", enumerate_them),)


def test_a_speculative_404_is_no_bundle_not_a_loss(tmp_path):
    """EPMC lists its bundle URL for every PMCID without checking; a 404 is the
    endpoint saying 'nothing here', verified reproducible at concurrency 1."""
    http = FakeDownloader()   # no body for the URL -> 404
    summary = pull(tmp_path, http, provider_of(speculative_bundle()))

    assert summary.status == "none_found"
    (skip,) = manifest_of(tmp_path)["skipped"]
    assert skip["reason"] == "no-bundle"
    assert skip["status"] == 404


def test_a_404_on_a_listed_file_is_still_a_loss(tmp_path):
    """Only speculative listings get the benefit of the doubt: a provider that
    listed a real file which then 404s has lost that file."""
    http = FakeDownloader()
    summary = pull(tmp_path, http, provider_of(sf("s1.pdf", "https://x/s1")))

    assert summary.status == "partial"
    (skip,) = manifest_of(tmp_path)["skipped"]
    assert skip["reason"] == "http-error"
    # ...and 404 is an answer, not a blip: exactly one attempt, no slow rounds.
    assert len(http.downloads) == 1


class _PmcidResolver:
    """Minimal resolver so the record carries a PMCID.

    The hasSuppl lookup needs one; without it the warning declines by design
    (see _epmc_withholds_supplements(default_without_pmcid=False)).
    """

    def __init__(self, pmcid):
        self._pmcid = pmcid
        self.cache = type("C", (), {"flush": lambda self: None})()

    def resolve(self, raw_identifier, doi=None, pmid=None):
        from fetchpdf.retrieval.identifiers import IdentifierSet
        return IdentifierSet(doi=doi, pmid=pmid, pmcid=self._pmcid)


class _SupplSearch(FakeDownloader):
    """FakeDownloader that also answers Europe PMC's hasSuppl lookup.

    The plain FakeDownloader 404s every get(), which makes the hasSuppl check
    fail closed -- so a test using it would pass for the wrong reason.
    """

    def __init__(self, has_suppl, **kw):
        super().__init__(**kw)
        self._has_suppl = has_suppl

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        if "/search" in url:
            body = json.dumps({"resultList": {"result": [
                {"hasSuppl": self._has_suppl, "isOpenAccess": "N"}]}})
            return FakeResponse(status=200, content=body.encode())
        return FakeResponse(status=404)


def test_withheld_supplements_are_named_when_they_exist(tmp_path, capsys):
    """hasSuppl=Y + closed access: the files exist and cannot be had.

    Worth saying out loud, because the user can still fetch them by hand.
    """
    from fetchpdf.retrieval import supplementary as sup
    sup.reset_not_open_access_records()

    stub = fixture("epmc_supplements_error_bean.xml")
    http = _SupplSearch("Y", bodies={"supplementaryFiles": stub})
    summary = pull(tmp_path, http, provider_of(speculative_bundle()),
                   resolver=_PmcidResolver("PMC3458378"))

    (skip,) = manifest_of(tmp_path)["skipped"]
    assert skip["reason"] == "epmc_not_open_access"
    assert "not open access" in skip["detail"]
    assert summary.status == "partial"          # a withheld file is a shortfall
    assert "will not serve it" in capsys.readouterr().out
    assert len(sup.not_open_access_records()) == 1


def test_closed_access_with_no_supplements_stays_quiet(tmp_path, capsys):
    """THE REGRESSION. Same stub body, opposite fact.

    Europe PMC returns the identical "...is not open access one" errorBean
    whether or not supplements exist -- verified against PMC11215513
    (hasSuppl=N) and PMC3458378 (hasSuppl=Y). Keying on that text warned about
    five records in a real run when only one had anything to withhold, sending
    the user hunting for files that do not exist.
    """
    from fetchpdf.retrieval import supplementary as sup
    sup.reset_not_open_access_records()

    stub = fixture("epmc_supplements_error_bean.xml")
    http = _SupplSearch("N", bodies={"supplementaryFiles": stub})
    summary = pull(tmp_path, http, provider_of(speculative_bundle()),
                   resolver=_PmcidResolver("PMC11215513"))

    (skip,) = manifest_of(tmp_path)["skipped"]
    assert skip["reason"] == "no-bundle"        # nothing existed, nothing lost
    assert summary.status == "none_found"
    assert "will not serve it" not in capsys.readouterr().out
    assert sup.not_open_access_records() == []


def test_a_plain_error_bean_is_still_no_bundle(tmp_path, capsys):
    """Any OTHER errorBean means "nothing here" -- and stays quiet."""
    from fetchpdf.retrieval import supplementary as sup
    sup.reset_not_open_access_records()

    stub = b'<?xml version="1.0"?><errorBean><errCode>0</errCode>' \
           b'<errMsg>No supplementary files</errMsg></errorBean>'
    http = _SupplSearch("Y", bodies={"supplementaryFiles": stub})
    summary = pull(tmp_path, http, provider_of(speculative_bundle()))

    (skip,) = manifest_of(tmp_path)["skipped"]
    assert skip["reason"] == "no-bundle"
    assert summary.status == "none_found"
    assert "will not serve it" not in capsys.readouterr().out


def test_a_genuine_non_zip_is_still_not_an_archive(tmp_path):
    http = FakeDownloader(bodies={"supplementaryFiles": b"<html>captcha</html>"})
    summary = pull(tmp_path, http, provider_of(speculative_bundle()))

    assert summary.status == "partial"
    assert manifest_of(tmp_path)["skipped"][0]["reason"] == "not-an-archive"


def test_a_declared_file_not_obtained_is_incomplete(tmp_path):
    """The gate's whole point: named data loss outranks every other status."""
    http = FakeDownloader(bodies={"/have": BODY_A})
    summary = pull(tmp_path, http, declaring(
        {"s1.docx": "s1.docx", "have.pdf": "have.pdf"},
        sf("have.pdf", "https://x/have"),
    ))

    assert summary.status == "incomplete"
    assert summary.missing_declared == ["s1.docx"]
    declared = manifest_of(tmp_path)["declared"]
    assert declared == {"source": "jats", "total": 2, "obtained": 1,
                        "missing": ["s1.docx"]}


def test_declared_files_obtained_as_zip_members_count(tmp_path):
    """Completeness is about bytes on disk, not about which route they took."""
    payload = zip_bytes([("Table_S1.xlsx", b"x" * 120)])
    http = FakeDownloader(bodies={"bundle": payload})
    summary = pull(tmp_path, http, declaring(
        {"table_s1.xlsx": "table_s1.xlsx"},
        sf("bundle.zip", "https://x/bundle", is_archive=True),
    ))

    assert summary.status == "ok"
    assert manifest_of(tmp_path)["declared"]["missing"] == []


def test_no_jats_means_unverifiable_not_incomplete(tmp_path):
    """A record that cannot be verified must not read as verified -- or as broken."""
    http = FakeDownloader(bodies={"/a": BODY_A})
    summary = pull(tmp_path, http, provider_of(sf("s1.pdf", "https://x/a")))

    assert summary.status == "ok"
    assert manifest_of(tmp_path)["declared"] == {"source": "unavailable"}


class FlakyDownloader(FakeDownloader):
    """Fails each URL with a given status a set number of times, then serves."""

    def __init__(self, fail_times, fail_status, **kwargs):
        super().__init__(**kwargs)
        self.fail_times = fail_times
        self.fail_status = fail_status
        self.attempts = {}

    def download(self, url, dest, max_bytes, **kwargs):
        from fetchpdf.retrieval.http import Download

        n = self.attempts[url] = self.attempts.get(url, 0) + 1
        if n <= self.fail_times:
            self.downloads.append((url, dest, max_bytes))
            return Download(url=url, request_url=url, status=self.fail_status,
                            outcome="http-error")
        return super().download(url, dest, max_bytes, **kwargs)


def test_a_transient_failure_is_retried_until_it_lands(tmp_path, monkeypatch):
    monkeypatch.setattr("fetchpdf.retrieval.supplementary._RETRY_WAITS", (0, 0, 0))
    http = FlakyDownloader(fail_times=2, fail_status=503, bodies={"/a": BODY_A})
    summary = pull(tmp_path, http, provider_of(sf("s1.pdf", "https://x/a")))

    assert summary.status == "ok"
    assert summary.written == 1
    assert http.attempts["https://x/a"] == 3
    assert manifest_of(tmp_path)["skipped"] == []   # the failure left no trace


def test_the_retry_waits_escalate_dramatically(tmp_path, monkeypatch):
    """20s, then 90s, then 300s: back off hard rather than hammer a sick host."""
    from fetchpdf.retrieval import supplementary as module

    slept = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    http = FlakyDownloader(fail_times=99, fail_status=503)
    summary = pull(tmp_path, http, provider_of(sf("s1.pdf", "https://x/a")))

    assert slept == [20, 90, 300]
    assert summary.status == "partial"
    assert http.attempts["https://x/a"] == 4   # first pass + three rounds


def test_a_terminal_403_gets_no_slow_rounds(tmp_path):
    http = FakeDownloader(statuses={"/a": 403}, bodies={"/a": BODY_A})
    pull(tmp_path, http, provider_of(sf("s1.pdf", "https://x/a")))

    assert len(http.downloads) == 1


def test_repair_heals_a_phantom_partial_manifest(tmp_path):
    """Manifests written before no-bundle existed carry phantom 'partial'
    statuses; the repair pass upgrades them without a single download."""
    from fetchpdf.retrieval.supplementary import _repair, read_manifest

    (tmp_path / "rec.xml").write_bytes(
        b'<article><body><p>text, no supplements</p></body></article>')
    (tmp_path / "rec_supplementary_info_1_Table.xlsx").write_bytes(b"x" * 50)
    manifest = {
        "status": "partial",
        "files": [{"index": 1,
                   "filename": "rec_supplementary_info_1_Table.xlsx",
                   "original_name": "Table.xlsx", "url": "https://x/t"}],
        "skipped": [{"provider": "europepmc_supplements",
                     "reason": "http-error", "status": 404,
                     "original_name": "PMC9_SupplementaryFiles.zip"}],
    }
    path = tmp_path / "rec_supplementary_info.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = _repair(str(path), str(tmp_path / "rec"), None, None, None, None,
                      verbose=False, max_file_bytes=CAP)

    assert summary.status == "skipped"
    healed = read_manifest(str(path))
    assert healed["status"] == "ok"
    assert healed["skipped"][0]["reason"] == "no-bundle"
    assert healed["skipped"][0]["was"] == "http-error"
    assert healed["declared"] == {"source": "jats", "total": 0, "obtained": 0,
                                  "missing": []}


def test_repair_flags_a_record_short_of_its_declared_set(tmp_path):
    from fetchpdf.retrieval.supplementary import _repair, read_manifest

    (tmp_path / "rec.xml").write_bytes(
        b'<article><body><supplementary-material>'
        b'<media href="s1.docx"/></supplementary-material></body></article>')
    manifest = {"status": "ok", "files": [], "skipped": []}
    path = tmp_path / "rec_supplementary_info.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = _repair(str(path), str(tmp_path / "rec"), None, None, None, None,
                      verbose=False, max_file_bytes=CAP)

    assert summary.status == "incomplete"
    assert summary.missing_declared == ["s1.docx"]
    assert read_manifest(str(path))["status"] == "incomplete"


def test_jats_declared_lists_only_absolute_hrefs():
    """A bare filename has no resolvable base (the /bin/ captcha); an absolute
    href -- LWW permalinks -- is fetchable exactly as written."""
    from fetchpdf.retrieval.supplement_pmc import enumerate_jats_declared
    from fetchpdf.retrieval.resolve import IdentifierSet

    ctx = ctx_with(FakeHttp({}))
    ctx.scratch["jats_declared"] = {
        "a213.docx": "http://links.lww.com/PR9/A213",
        "s1.docx": "s1.docx",
    }
    files = enumerate_jats_declared(IdentifierSet(doi="10.1097/x"), ctx)

    assert [(f.name, f.url) for f in files] == [
        ("a213.docx", "http://links.lww.com/PR9/A213")]


# ── --make-subfolder ─────────────────────────────────────────────────────────
# Two halves, per the pattern above: prove the default is unchanged, and prove
# the flag actually moves the artifacts. The interesting bug this guards is the
# abstract writers, which take a DIRECTORY rather than deriving from save_path
# and so do not follow into the subfolder for free.


def test_make_subfolder_is_off_by_default(tmp_path, monkeypatch):
    """Default stays flat: {output_dir}/{safe_doi}.pdf, no per-record folder."""
    module = batch_module()
    seen = {}

    def fake_fetch(doi, save_path, *a, **k):
        seen["save_path"] = save_path
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(b"%PDF-1.4\n")
        return save_path

    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", fake_fetch)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path))

    assert seen["save_path"] == str(tmp_path / "10.1234--x.pdf")
    assert not (tmp_path / "10.1234--x").is_dir()


def test_make_subfolder_puts_the_record_in_its_own_directory(tmp_path, monkeypatch):
    """Folder name uses the same encoder as the filename, so the stems match."""
    module = batch_module()
    seen = {}

    def fake_fetch(doi, save_path, *a, **k):
        seen["save_path"] = save_path
        with open(save_path, "wb") as f:
            f.write(b"%PDF-1.4\n")
        return save_path

    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", fake_fetch)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path), make_subfolder=True)

    record_dir = tmp_path / "10.1234--x"
    assert record_dir.is_dir(), "no per-record directory was created"
    assert seen["save_path"] == str(record_dir / "10.1234--x.pdf")
    # The directory must exist before the writer runs -- the default chain's raw
    # open(save_path, "wb") does not makedirs on its own.
    assert (record_dir / "10.1234--x.pdf").exists()


def test_make_subfolder_keeps_run_level_files_at_the_root(tmp_path, monkeypatch):
    """failed_dois.csv describes the run, not a record: it stays at output_dir."""
    module = batch_module()
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)
    monkeypatch.setattr(module, "fetch_pdf", lambda *a, **k: None)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path), make_subfolder=True)

    assert (tmp_path / "failed_dois.csv").exists(), "run-level CSV left the root"


def test_make_subfolder_routes_the_abstract_into_the_record_directory(tmp_path, monkeypatch):
    """save_abstract_markdown takes a DIRECTORY, so it needs the record dir passed
    explicitly -- otherwise the exists-check and the write disagree and the
    abstract lands flat while the check looks in the subfolder."""
    module = batch_module()
    seen = {}

    def fake_abstract(identifier, output_dir, **k):
        seen["dir"] = output_dir
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{module.doi_to_safe_filename(identifier)}_abstract.md")
        with open(path, "w") as f:
            f.write("# abstract\n")
        return path

    import fetchpdf.fetch_abstract_from_doi as abstract_mod
    monkeypatch.setattr(abstract_mod, "save_abstract_markdown", fake_abstract)
    monkeypatch.setattr(module, "resolve_identifier_to_doi", lambda d, **k: d)

    run_batch(dois=["10.1234/x"], output_dir=str(tmp_path),
              abstract_only=True, make_subfolder=True)

    assert seen["dir"] == str(tmp_path / "10.1234--x"), \
        "abstract was written flat instead of into the record directory"


class TestElsevierObjectStatus:
    """HTTP 300 is this endpoint's success response, not a failure.

    `Response.ok` is 200 <= status < 300, so `if not response.ok` rejected
    every Elsevier article ever passed to this provider. Six records in a
    46-paper corpus had supplements silently dropped that way.
    """

    class _Resp:
        def __init__(self, status, payload):
            self.status, self._payload = status, payload

        @property
        def ok(self):
            return 200 <= self.status < 300

        def json(self):
            return self._payload

    def _ctx(self, response):
        class _Http:
            def get(_s, url, **kw):
                return response

        class _Ctx:
            verbose = False

            def __init__(self):
                self.scratch = {}
                self.http = _Http()

            def log(self, message):
                pass

        return _Ctx()

    def _payload(self):
        return {"choices": {"choice": [
            {"@ref": "gr1", "@type": "IMAGE-DOWNSAMPLED",
             "$": "https://api.elsevier.com/content/object/eid/x-gr1.jpg"},
            {"@ref": "mmc1", "@type": "APPLICATION",
             "$": "https://api.elsevier.com/content/object/eid/x-mmc1.docx"},
        ]}}

    def test_http_300_is_accepted(self):
        from fetchpdf.retrieval.identifiers import IdentifierSet
        from fetchpdf.retrieval.supplement_publishers import enumerate_elsevier_objects
        ctx = self._ctx(self._Resp(300, self._payload()))
        files = enumerate_elsevier_objects(IdentifierSet(doi="10.1016/x"), ctx)
        assert [f.name for f in files] == ["mmc1.docx"]

    def test_figures_are_still_dropped(self):
        """Only @type APPLICATION is a supplement; IMAGE-* are figures."""
        from fetchpdf.retrieval.identifiers import IdentifierSet
        from fetchpdf.retrieval.supplement_publishers import enumerate_elsevier_objects
        ctx = self._ctx(self._Resp(300, self._payload()))
        files = enumerate_elsevier_objects(IdentifierSet(doi="10.1016/x"), ctx)
        assert not any("gr1" in f.name for f in files)

    def test_a_real_error_status_is_still_refused(self):
        from fetchpdf.retrieval.identifiers import IdentifierSet
        from fetchpdf.retrieval.supplement_publishers import enumerate_elsevier_objects
        ctx = self._ctx(self._Resp(404, {"service-error": {}}))
        assert enumerate_elsevier_objects(IdentifierSet(doi="10.1016/x"), ctx) == []

    def test_the_doi_route_is_tried_when_there_is_no_pii(self):
        """A PII from Crossref can be stale; the DOI form is the reliable one."""
        from fetchpdf.retrieval.supplement_publishers import _elsevier_object_urls
        from fetchpdf.retrieval.identifiers import IdentifierSet
        urls = _elsevier_object_urls(IdentifierSet(doi="10.1016/j.obhdp.2013.09.001"))
        assert len(urls) == 1 and "/object/doi/" in urls[0]
        both = _elsevier_object_urls(
            IdentifierSet(doi="10.1016/x", elsevier_pii="S123"))
        assert "/object/pii/" in both[0] and "/object/doi/" in both[1]
