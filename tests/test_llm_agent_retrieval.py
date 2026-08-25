"""Layer 2: what the retrieval agent may propose, and what it may never do.

The agent is the only part of this package that lets a model's free-form output
decide what gets downloaded and filed under a paper. Everything here is a
boundary test rather than a behaviour test: the interesting question is not
"does it find deposits" -- that depends on the model -- but "when it is wrong,
or hostile, or absent, what happens".

Offline. The backend is a double; no model is ever called.
"""

import hashlib
import json
import os

import pytest

from fetchpdf.retrieval import llm_agent_retrieval as agent
from fetchpdf.retrieval.backends import normalize_receipt
from fetchpdf.retrieval.http import Download
from fetchpdf.retrieval.identifiers import IdentifierSet

CAP = 10 * 1024 * 1024
#: Big enough to clear the pipeline's "implausibly small" refusal, which is a
#: real guard against a publisher serving a 40-byte error body as a file.
BODY = b"col_a,col_b\n" + b"".join(b"%d,%d\n" % (i, i * 2) for i in range(400))


class FakeHttp:
    """Writes a canned body for any download; refuses nothing."""

    def __init__(self):
        self.limiter = object()
        self.downloaded = []

    def get(self, url, **kwargs):        # pragma: no cover - not exercised
        raise AssertionError("the agent path should not GET")

    def download(self, url, dest, max_bytes, **kwargs):
        if url.startswith("file://"):
            from fetchpdf.retrieval.http import _adopt_local_file_impl
            return _adopt_local_file_impl(url, dest, max_bytes)
        self.downloaded.append(url)
        with open(dest, "wb") as handle:
            handle.write(BODY)
        return Download(url=url, request_url=url, status=200,
                        content_type="text/csv", bytes_written=len(BODY),
                        sha256=hashlib.sha256(BODY).hexdigest(), path=dest,
                        outcome="ok", declared_length=len(BODY))


class FakeBackend:
    """Returns a fixed receipt. `downloads` decides which contract it claims."""

    def __init__(self, receipt, downloads=False, available=True, name="fake"):
        self.name = name
        self.downloads = downloads
        self.receipt = receipt
        self._available = available
        self.briefs = []

    def available(self):
        return (True, "fake") if self._available else (False, "no fake backend here")

    def run(self, brief, staging, model=None, log=None, **kwargs):
        self.briefs.append(brief)
        receipt = self.receipt(staging) if callable(self.receipt) else self.receipt
        return normalize_receipt(receipt, staging)


def _record(tmp_path, text="Data availability\nDeposited at https://osf.io/abcde/.\n"):
    """A record on disk with readable full text, as the provider expects."""
    (tmp_path / "10.1234--x.xml").write_text(text * 20, encoding="utf-8")
    return str(tmp_path / "10.1234--x.pdf")


def _pull(tmp_path, http, backend=None, **kwargs):
    from fetchpdf.retrieval.supplementary import pull_for_record

    if backend is not None:
        kwargs.setdefault("llm_agent_retrieval", True)
    kwargs.setdefault("max_file_bytes", CAP)
    from fetchpdf.retrieval.supplement_index import _providers
    providers = tuple(p for p in _providers() if p[0] == "llm_agent")
    return pull_for_record(
        raw_identifier="10.1234/x", doi="10.1234/x",
        save_path=_record(tmp_path), http=http, providers=providers, **kwargs)


def _manifest(tmp_path):
    path = tmp_path / "10.1234--x_supplementary_info.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _reset():
    agent.reset_record_budget()
    yield
    agent.reset_record_budget()


# --- the flag is the whole gate -------------------------------------------

def test_the_agent_does_not_run_unless_asked(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(agent, "get_backend",
                        lambda *_a, **_k: called.append(1) or None)
    _pull(tmp_path, FakeHttp())
    assert called == []


def test_an_unavailable_backend_is_recorded_not_raised(tmp_path, monkeypatch):
    """A missing CLI or key must cost the agent, never the record."""
    monkeypatch.setattr(agent, "get_backend",
                        lambda *_a, **_k: FakeBackend([], available=False))
    summary = _pull(tmp_path, FakeHttp(), backend=True)
    assert summary.written == 0
    sidecar = json.loads(
        (tmp_path / "10.1234--x_linked_artifacts.json").read_text())
    statuses = {q["service"]: q["status"] for q in sidecar["queried"]}
    assert statuses["llm_agent"] == "unavailable"


def test_a_backend_that_raises_does_not_fail_the_record(tmp_path, monkeypatch):
    class Exploding(FakeBackend):
        def run(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: Exploding([]))
    summary = _pull(tmp_path, FakeHttp(), backend=True)
    assert summary.written == 0


# --- where the files land --------------------------------------------------

def test_a_supplement_lands_beside_the_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend(
        [{"url": "https://publisher.example/suppl/table_s1.csv",
          "kind": "supplement", "why": "Table S1 named in the paper"}]))
    summary = _pull(tmp_path, FakeHttp(), backend=True)
    assert summary.written == 1
    names = [p.name for p in tmp_path.iterdir() if "supplementary_info_1" in p.name]
    assert names and names[0].endswith("table_s1.csv")
    assert not list(tmp_path.glob("*_data_artifacts"))


def test_a_dataset_lands_in_the_data_artifacts_subfolder(tmp_path, monkeypatch):
    """The provider string is what routes it, and it is easy to get wrong."""
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend(
        [{"url": "https://lab.example/archive/raw.csv", "kind": "dataset",
          "name": "raw.csv", "why": "the authors' own deposit"}]))
    summary = _pull(tmp_path, FakeHttp(), backend=True)
    assert summary.written == 1
    staged = list(tmp_path.glob("*_data_artifacts/**/*"))
    assert [p.name for p in staged if p.is_file()] == ["raw.csv"]


def test_a_repository_landing_page_is_enumerated_not_downloaded(tmp_path, monkeypatch):
    """Downloading an OSF project page gets an HTML page, not the data.

    Measured live: the agent found a GitHub repository this paper's own regex
    could not -- the PDF broke the URL across a line -- proposed the repo page,
    and the pipeline correctly refused it as "served HTML". The right refusal
    to the wrong question. Routing it also puts the deposit through
    _should_route and _deposit_claims_another_article, so a model's proposal is
    judged by the same gate an index's is.
    """
    routed = []
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend(
        [{"url": "https://osf.io/abcde/", "kind": "dataset",
          "why": "named in the data availability statement"}]))
    import fetchpdf.retrieval.supplement_graph as graph
    monkeypatch.setattr(graph, "_files_in_repository",
                        lambda target, ids, ctx, via=None: routed.append((target, via)) or [])
    http = FakeHttp()
    _pull(tmp_path, http, backend=True)
    assert routed == [("https://osf.io/abcde/", "llm_agent")]
    assert http.downloaded == []


def test_a_github_repo_goes_to_the_tarball_enumerator(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend(
        [{"url": "https://github.com/hasanalpboz/safegraph-covid19-mobility",
          "kind": "dataset", "why": "the project repository"}]))
    import fetchpdf.retrieval.supplement_graph as graph
    monkeypatch.setattr(graph, "_github_tarball",
                        lambda repo, ctx: seen.append(repo) or [])
    _pull(tmp_path, FakeHttp(), backend=True)
    assert seen == ["hasanalpboz/safegraph-covid19-mobility"]


def test_every_proposal_reaches_the_sidecar_even_unrouted(tmp_path, monkeypatch):
    """A suggestion that came to nothing is still the record of what was seen."""
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend(
        [{"url": "https://lab.example/archive/raw.csv", "kind": "dataset",
          "why": "deposited by the authors"}]))
    _pull(tmp_path, FakeHttp(), backend=True)
    sidecar = json.loads(
        (tmp_path / "10.1234--x_linked_artifacts.json").read_text())
    mine = [l for l in sidecar["links"] if l["service"] == "llm_agent"]
    assert len(mine) == 1
    assert mine[0]["target_pid"] == "https://lab.example/archive/raw.csv"
    assert mine[0]["routed_to_download"] is True


def test_a_staged_file_is_ingested_and_keeps_its_real_provenance(tmp_path, monkeypatch):
    """A downloading backend hands over a local file; the manifest must still
    say where it came from. A temp path presented as a provenance is the
    substitution this toolkit exists to catch."""
    def receipt(staging):
        path = os.path.join(staging, "raw.csv")
        with open(path, "wb") as handle:
            handle.write(BODY)
        return [{"url": "https://dataverse.example/api/access/datafile/7",
                 "kind": "supplement", "name": "raw.csv",
                 "saved_as": "raw.csv", "why": "deposited by the authors"}]

    monkeypatch.setattr(agent, "get_backend",
                        lambda *_a, **_k: FakeBackend(receipt, downloads=True))
    http = FakeHttp()
    summary = _pull(tmp_path, http, backend=True)
    assert summary.written == 1
    assert http.downloaded == []          # adopted from disk, never re-fetched
    record = _manifest(tmp_path)["files"][0]
    assert record["url"] == "https://dataverse.example/api/access/datafile/7"
    assert record["sha256"] == hashlib.sha256(BODY).hexdigest()


# --- containment -----------------------------------------------------------

def test_a_saved_as_path_cannot_escape_staging(tmp_path):
    """The one field a model could use to reach outside its sandbox."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (tmp_path / "secret.txt").write_text("canary", encoding="utf-8")
    entries = normalize_receipt(
        [{"url": "https://x.example/a.csv", "saved_as": "../secret.txt"}],
        str(staging))
    assert entries == [{"url": "https://x.example/a.csv", "kind": "supplement"}]


def test_non_http_urls_are_dropped(tmp_path):
    assert normalize_receipt([{"url": "file:///etc/passwd"},
                              {"url": "ftp://x/y"},
                              {"url": "/etc/shadow"}], str(tmp_path)) == []


def test_a_receipt_that_is_not_a_list_is_not_a_crash(tmp_path):
    assert normalize_receipt(None, str(tmp_path)) == []
    assert normalize_receipt({"url": "https://x/y"}, str(tmp_path)) == []
    assert normalize_receipt(["nonsense", 3], str(tmp_path)) == []


# --- the spend ceiling -----------------------------------------------------

def test_the_record_ceiling_stops_the_call_and_says_so(tmp_path, monkeypatch):
    """Skipped-to-save-money and looked-and-found-nothing are different facts."""
    backend = FakeBackend([{"url": "https://x.example/a.csv", "kind": "supplement"}])
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: backend)
    agent._SPENT["records"] = 5
    summary = _pull(tmp_path, FakeHttp(), backend=True, max_llm_records=5)
    assert summary.written == 0
    assert backend.briefs == []
    sidecar = json.loads(
        (tmp_path / "10.1234--x_linked_artifacts.json").read_text())
    statuses = {q["service"]: q["status"] for q in sidecar["queried"]}
    assert statuses["llm_agent"] == "skipped"


def test_zero_means_no_ceiling(tmp_path, monkeypatch):
    backend = FakeBackend([])
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: backend)
    agent._SPENT["records"] = 9999
    _pull(tmp_path, FakeHttp(), backend=True, max_llm_records=0)
    assert len(backend.briefs) == 1


# --- what the model is told ------------------------------------------------

def test_the_brief_names_what_we_already_hold_and_what_was_declined(tmp_path):
    brief = agent.build_brief(
        "PAPER PROSE", "DOI 10.1234/x",
        ["mmc1.xlsx  [elsevier_objects]"],
        ["10.5281/zenodo.99 (related, References) Someone else's archive"],
        downloads=False)
    assert "mmc1.xlsx" in brief
    assert "zenodo.99" in brief
    assert "cannot save files" in brief
    assert "PAPER PROSE" in brief


def test_a_downloading_backend_is_told_to_download(tmp_path):
    brief = agent.build_brief("x", "DOI 10.1234/x", [], [], downloads=True)
    assert "`download`" in brief
    assert "saved_as" in brief


def test_the_brief_keeps_the_availability_statement_out_of_a_long_paper():
    """Sending a whole article multiplies the bill by its length for no gain."""
    text = ("methods " * 4000
            + "Data availability All data are at https://osf.io/abcde/. "
            + "results " * 4000)
    selected = agent.select_text(text)
    assert len(selected) <= agent.BRIEF_TEXT_CHARS
    assert "osf.io/abcde" in selected
    assert "[...]" in selected      # never presented as continuous prose


def test_no_full_text_is_reported_rather_than_reported_as_nothing_found(
        tmp_path, monkeypatch):
    backend = FakeBackend([])
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: backend)
    from fetchpdf.retrieval.supplementary import pull_for_record
    from fetchpdf.retrieval.supplement_index import _providers
    providers = tuple(p for p in _providers() if p[0] == "llm_agent")
    pull_for_record(raw_identifier="10.1234/x", doi="10.1234/x",
                    save_path=str(tmp_path / "10.1234--x.pdf"),
                    http=FakeHttp(), providers=providers,
                    llm_agent_retrieval=True, max_file_bytes=CAP)
    assert backend.briefs == []
    sidecar = json.loads(
        (tmp_path / "10.1234--x_linked_artifacts.json").read_text())
    statuses = {q["service"]: q["status"] for q in sidecar["queried"]}
    assert statuses["llm_agent"] == "no_text"



# --- what the run says it did ----------------------------------------------

def test_the_run_report_distinguishes_never_called_from_found_nothing(
        tmp_path, monkeypatch):
    """The whole reason this counter exists.

    A run that reports only files found lets a missing CLI and a paper with no
    deposit print the same way -- and on these corpora the second is the common
    case, so the first would hide behind it indefinitely.
    """
    monkeypatch.setattr(agent, "get_backend",
                        lambda *_a, **_k: FakeBackend([], available=False))
    _pull(tmp_path, FakeHttp(), backend=True)
    assert agent.run_report() == {"backend_unavailable": 1}


def test_a_call_that_found_nothing_is_recorded_as_such(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "get_backend", lambda *_a, **_k: FakeBackend([]))
    _pull(tmp_path, FakeHttp(), backend=True)
    assert agent.run_report() == {"ran_found_nothing": 1}


def test_the_report_is_empty_when_the_agent_never_ran(tmp_path):
    _pull(tmp_path, FakeHttp())
    assert agent.run_report() == {}
