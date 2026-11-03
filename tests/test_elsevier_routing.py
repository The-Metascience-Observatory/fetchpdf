"""Elsevier: the institutional token, and asking for the PDF early.

Two changes, one reason to pin them together. The institutional token
(`X-ELS-Insttoken`) only helps if it rides on EVERY api.elsevier.com request --
a token on the PDF request and not on the object listing grants entitlement to
some of a record's files and not others. And asking for the PDF early is only
safe because the early step takes nothing but a FULL PDF and writes nothing
else; both properties are what these tests hold in place.
"""
import inspect
import json

import pytest

import fetchpdf.fetchpdf as fpd
from fetchpdf import _env, _http
from fetchpdf.retrieval import tiers
from fetchpdf.retrieval.http import HttpClient
from fetchpdf.retrieval.ratelimit import HostRateLimiter

PREVIEW = ("WARNING - Response limited to first page because "
           "requestor not entitled to resource")


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------


def test_headers_carry_the_key_and_no_token_by_default(monkeypatch):
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", None)
    assert _http.elsevier_headers("k") == {"X-ELS-APIKey": "k"}


def test_headers_add_the_token_when_set_and_merge_extras(monkeypatch):
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "inst")
    out = _http.elsevier_headers("k", {"Accept": "application/pdf"})
    assert out == {"X-ELS-APIKey": "k", "X-ELS-Insttoken": "inst",
                   "Accept": "application/pdf"}


def test_an_empty_token_is_omitted_not_sent_blank(monkeypatch):
    """A blank header would be read by Elsevier as a malformed token."""
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "")
    assert "X-ELS-Insttoken" not in _http.elsevier_headers("k")


class _Resp:
    status_code = 200
    url = "https://api.elsevier.com/x"
    content = b"{}"
    headers = {"content-type": "application/json"}


class _Session:
    def __init__(self):
        self.headers = {}
        self.sent = []

    def get(self, url, **kwargs):
        self.sent.append((url, kwargs.get("headers") or {}))
        return _Resp()


def _client():
    http = HttpClient(HostRateLimiter({"_default": {"rps": 1000, "burst": 1000}}))
    http.session = _Session()
    return http


@pytest.mark.parametrize("polite", [True, False])
def test_http_client_sends_the_token_to_elsevier_either_way(monkeypatch, polite):
    """Object and supplement calls pass polite=False; the token must still go."""
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "inst")
    http = _client()
    http.get("https://api.elsevier.com/content/object/pii/S1", polite=polite)
    assert http.session.sent[0][1]["X-ELS-Insttoken"] == "inst"


def test_http_client_never_sends_the_token_to_another_host(monkeypatch):
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "inst")
    http = _client()
    for url in ("https://api.crossref.org/works/10.1/x",
                "https://api.elsevier.com.evil.example/x",
                "https://www.sciencedirect.com/x"):
        http.get(url)
    assert all("X-ELS-Insttoken" not in sent for _, sent in http.session.sent)


def test_http_client_does_not_override_a_token_the_caller_set(monkeypatch):
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "from-env")
    http = _client()
    http.get("https://api.elsevier.com/x", headers={"X-ELS-Insttoken": "explicit"})
    assert http.session.sent[0][1]["X-ELS-Insttoken"] == "explicit"


def test_http_client_is_unchanged_without_a_token(monkeypatch):
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", None)
    http = _client()
    http.get("https://api.elsevier.com/x", headers={"X-ELS-APIKey": "k"})
    assert http.session.sent[0][1] == {"X-ELS-APIKey": "k"}


def test_elsevier_credentials_are_redacted_from_recorded_headers():
    from fetchpdf.retrieval.http import _SECRET_HEADERS
    assert {"x-els-apikey", "x-els-insttoken"} <= _SECRET_HEADERS


def test_the_raw_requests_carry_the_token_too(monkeypatch, tmp_path):
    """fetchpdf.py talks to Elsevier with bare `requests`, not HttpClient."""
    monkeypatch.setattr(_env, "ELSEVIER_INSTTOKEN", "inst")
    seen = []

    class R:
        status_code = 200
        content = b"%PDF-1.4 full"
        headers = {"X-ELS-Status": "OK"}

    monkeypatch.setattr(fpd.requests, "get",
                        lambda url, **kw: seen.append(kw["headers"]) or R())
    assert fpd._try_elsevier_pdf_by_pii("S1", str(tmp_path / "r.pdf"), "k")
    assert seen[0]["X-ELS-Insttoken"] == "inst"
    assert seen[0]["X-ELS-APIKey"] == "k"


# --------------------------------------------------------------------------
# PDF first
# --------------------------------------------------------------------------

BODY_XML = ("<full-text-retrieval-response><originalText><xocs:doc><ja:body>"
            "<ce:sections><ce:para>" + "Real full text. " * 50 +
            "</ce:para></ce:sections></ja:body></xocs:doc></originalText>"
            "</full-text-retrieval-response>")
STUB_XML = ("<full-text-retrieval-response><coredata><dc:title>Only metadata "
            "here, padded out so it clears the length floor. " * 3 +
            "</dc:title></coredata></full-text-retrieval-response>")


class _Fake:
    """Routes bare `requests.get` and the Crossref seam to canned answers."""

    def __init__(self, monkeypatch, *, crossref=200, pdf=b"%PDF-1.4 full",
                 pdf_headers=None, xml=BODY_XML, pii="S0000000000"):
        self.calls = []
        self.pdf, self.pdf_headers, self.xml = pdf, pdf_headers or {"X-ELS-Status": "OK"}, xml
        self.crossref = crossref
        monkeypatch.setattr(fpd, "_ELSEVIER_TDM_API_KEY", "test-key")
        monkeypatch.setattr(fpd, "_extract_elsevier_pii_from_crossref", lambda m: pii)
        monkeypatch.setattr(fpd, "_crossref_raw_get", self._crossref_get)
        monkeypatch.setattr(fpd.requests, "get", self._get)

    def _crossref_get(self, url, **kw):
        self.calls.append("crossref")
        outer = self

        class R:
            status_code = outer.crossref

            @staticmethod
            def json():
                return {"message": {"link": []}}
        return R()

    def _get(self, url, **kw):
        outer = self
        accept = (kw.get("headers") or {}).get("Accept", "")
        if accept == "application/pdf":
            self.calls.append("pdf")

            class P:
                status_code = 200
                content = outer.pdf
                headers = outer.pdf_headers
            return P()
        self.calls.append("xml")

        class X:
            status_code = 200
            text = outer.xml
        return X()


def test_a_full_pdf_is_taken_with_its_xml_companion(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch)
    save = tmp_path / "rec.pdf"
    out = fpd.try_elsevier_pdf_first.__wrapped__("10.1016/x", str(save))
    assert save.read_bytes().startswith(b"%PDF")
    assert (tmp_path / "rec.xml").exists()
    assert out == str(tmp_path / "rec.xml")      # the deferred path's convention
    assert fake.calls == ["crossref", "pdf", "xml"]


def test_a_stub_xml_companion_leaves_the_pdf_alone(monkeypatch, tmp_path):
    _Fake(monkeypatch, xml=STUB_XML)
    save = tmp_path / "rec.pdf"
    out = fpd.try_elsevier_pdf_first.__wrapped__("10.1016/x", str(save))
    assert out == str(save)
    assert save.exists() and not (tmp_path / "rec.xml").exists()


def test_no_xml_fallback_asks_for_the_pdf_only(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch)
    out = fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), allow_xml=False)
    assert out == str(tmp_path / "rec.pdf")
    assert "xml" not in fake.calls


def test_a_first_page_preview_ends_nothing_and_writes_nothing(monkeypatch, tmp_path):
    """The property that makes asking early safe: a preview cannot pre-empt."""
    fake = _Fake(monkeypatch, pdf_headers={"x-els-status": PREVIEW})
    asked = []
    out = fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), asked_out=asked)
    assert out is None
    assert list(tmp_path.iterdir()) == []
    assert asked == [True]            # it WAS asked and refused
    assert "xml" not in fake.calls    # and no XML was fetched to sit on disk


def test_an_error_payload_in_place_of_a_pdf_writes_nothing(monkeypatch, tmp_path):
    _Fake(monkeypatch, pdf=b"<service-error>not entitled</service-error>")
    assert fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf")) is None
    assert list(tmp_path.iterdir()) == []


def test_a_crossref_failure_is_not_recorded_as_elsevier_refusing(monkeypatch, tmp_path):
    """The deferred call must still ask for the PDF in this case."""
    fake = _Fake(monkeypatch, crossref=503)
    asked = []
    assert fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), asked_out=asked) is None
    assert asked == []
    assert fake.calls == ["crossref"]


def test_other_publishers_cost_nothing(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch)
    assert fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1371/journal.pone.0000308", str(tmp_path / "rec.pdf")) is None
    assert fake.calls == []


def test_without_a_key_nothing_is_asked(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch)
    monkeypatch.setattr(fpd, "_ELSEVIER_TDM_API_KEY", None)
    assert fpd.try_elsevier_pdf_first.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf")) is None
    assert fake.calls == []


def test_the_deferred_call_does_not_ask_for_the_pdf_a_second_time(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, xml=BODY_XML)
    out = fpd.try_elsevier_fulltext_api_fallback.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), crossref_message={"link": []},
        try_pdf=False)
    assert out == str(tmp_path / "rec.xml")        # the XML is still taken
    assert "pdf" not in fake.calls


def test_the_deferred_call_with_a_stub_and_no_pdf_returns_nothing(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, xml=STUB_XML)
    assert fpd.try_elsevier_fulltext_api_fallback.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), crossref_message={"link": []},
        try_pdf=False) is None
    assert "pdf" not in fake.calls


def test_the_default_deferred_call_is_unchanged(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, xml=STUB_XML)
    out = fpd.try_elsevier_fulltext_api_fallback.__wrapped__(
        "10.1016/x", str(tmp_path / "rec.pdf"), crossref_message={"link": []})
    assert out == str(tmp_path / "rec.pdf")        # stub XML -> the PDF representation
    assert "pdf" in fake.calls


# --------------------------------------------------------------------------
# Where it sits
# --------------------------------------------------------------------------


def test_the_chain_asks_elsevier_before_pmc_unpaywall_and_the_long_tail():
    """A dead repository host in Unpaywall's list cost ~80s before Elsevier ran."""
    source = inspect.getsource(fpd._fetch_pdf_chain)
    first = source.index("---- Elsevier PDF first")
    assert first < source.index("---- PubMed Central (PMC) via Europe PMC")
    assert first < source.index("---- Unpaywall ----")
    assert first < source.index("---- Institutional access (opt-in)")
    assert first < source.index("----  Crossref ----")
    assert first < source.index("Deferred Elsevier fallback")


def test_the_deferred_call_is_still_there_for_the_xml_and_for_other_domains():
    source = inspect.getsource(fpd._fetch_pdf_chain)
    assert "Deferred Elsevier fallback" in source
    assert "try_pdf=not elsevier_asked" in source


def test_the_tiered_engine_asks_elsevier_before_the_chain_and_not_twice():
    ladder = tiers.load_ladder()
    t5 = [s.name for s in ladder.sources_for(tiers.Tier.T5_PDF)]
    assert t5.index("elsevier_pdf") < t5.index("legacy_pdf_chain")

    from fetchpdf.retrieval.sources import legacy_pdf
    assert "_elsevier_first=False" in inspect.getsource(legacy_pdf.fetch_via_legacy_chain)


def test_the_chain_default_asks_first():
    sig = inspect.signature(fpd._fetch_pdf_chain)
    assert sig.parameters["_elsevier_first"].default is True


def test_ladder_json_is_still_valid():
    json.loads((__import__("pathlib").Path(tiers.__file__).parent / "ladder.json").read_text())
