"""Golden tests for the network-free helpers.

Deliberately offline: no requests, no Playwright, no DOI resolution. These cover
the one bug class in this package that corrupts data silently instead of failing
loudly -- filename encoding. Everything else here fails noisily at runtime.

The load-bearing test is test_round_trip: encoding a DOI to a filename stem and
decoding it back must be the identity. That invariant is what keeps a PDF on disk
traceable to the paper it came from.
"""

import importlib

import pytest

from fetchpdf.fetchpdf import (
    _decode_fs_tokens,
    _is_plausible_http_url,
    _xml_path_for_pdf_path,
    canonicalize_doi,
    doi_to_safe_filename,
    extract_pmid,
    sanitize_doi,
)

# Real DOIs, chosen to cover each escape the encoding has to survive.
ROUND_TRIP_DOIS = [
    "10.1371/journal.pone.0000308",   # ordinary single slash
    "10.1038/nature12373",
    "10.1023/a:1018769825030",        # ':' -> '~'
    "10.1023/a/1018769825030",        # the slash twin of the line above: these two
                                      # must not collide (the whole point of ':' -> '~')
    "10.1093/abm/kaad072",            # two slashes (OUP shape)
    "10.5555/a/b/c/d",                # pathological: four slashes
    "10.1002/(sici)1097-0258(19960229)15:4<361::aid-sim168>3.0.co;2-4",  # Wiley SICI: < > and ':'
]


@pytest.mark.parametrize("doi", ROUND_TRIP_DOIS)
def test_round_trip(doi):
    """encode -> decode must be the identity, for every DOI shape."""
    assert canonicalize_doi(doi_to_safe_filename(doi)) == doi


def test_colon_and_slash_do_not_collide():
    """The encoding's reason for existing: ':' and '/' must stay distinguishable.

    Collapsing both to '--' (the old behavior) made these two DOIs -- different
    papers -- land on the same filename.
    """
    a = doi_to_safe_filename("10.1023/a:1018769825030")
    b = doi_to_safe_filename("10.1023/a/1018769825030")
    assert a != b
    assert canonicalize_doi(a) == "10.1023/a:1018769825030"
    assert canonicalize_doi(b) == "10.1023/a/1018769825030"


def test_multi_segment_doi_decodes_every_separator():
    """Regression: canonicalize_doi used to decode only the first '--'.

    10.1093/abm/kaad072 came back as 10.1093/abm--kaad072 -- a DOI that resolves
    to nothing. Two-segment DOIs are the standard OUP shape, not an edge case.
    """
    assert canonicalize_doi("10.1093--abm--kaad072") == "10.1093/abm/kaad072"


def test_doi_to_safe_filename_encoding():
    """The documented encoding. mo_pipeline.corpus.models.doi_to_folder must match this."""
    assert doi_to_safe_filename("10.1002/acp.4202") == "10.1002--acp.4202"
    assert doi_to_safe_filename("10.1023/a:123") == "10.1023--a~123"
    assert doi_to_safe_filename('10.1/a<b>c"d') == "10.1--a~3c~b~3e~c~22~d"


def test_safe_filename_has_no_forbidden_chars():
    """Output must be writable on NTFS/exFAT, which is why the escaping exists."""
    forbidden = set('/<>"\\|?*:')
    for doi in ROUND_TRIP_DOIS:
        assert not (set(doi_to_safe_filename(doi)) & forbidden)


def test_decode_fs_tokens():
    assert _decode_fs_tokens("a~123") == "a:123"
    assert _decode_fs_tokens("a~3c~b") == "a<b"
    assert _decode_fs_tokens("plain") == "plain"


def test_canonicalize_strips_url_prefix():
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
        assert canonicalize_doi(f"{prefix}10.1038/nature12373") == "10.1038/nature12373"


def test_canonicalize_is_case_insensitive_and_trims():
    assert canonicalize_doi("  10.1038/Nature12373  ") == "10.1038/nature12373"


def test_canonicalize_passes_through_non_strings():
    assert canonicalize_doi(None) is None
    assert canonicalize_doi(3.14) == 3.14


def test_sanitize_doi_strips_url_artifacts():
    assert sanitize_doi("10.1038/nature12373?utm_source=x") == "10.1038/nature12373"
    assert sanitize_doi("10.1038/nature12373/full/html") == "10.1038/nature12373"
    assert sanitize_doi("10.1038/nature12373/") == "10.1038/nature12373"
    assert sanitize_doi("10.1101/2024.02.13.580153v1.full") == "10.1101/2024.02.13.580153"
    assert sanitize_doi("10.1093/abm/kaad072/7512904") == "10.1093/abm/kaad072"


def test_extract_pmid():
    """A PMID must be recognised however it is written -- these all name one paper."""
    for raw in ("39804400", "pmid:39804400", "PMID 39804400",
                "https://pubmed.ncbi.nlm.nih.gov/39804400/"):
        assert extract_pmid(raw) == "39804400"


def test_extract_pmid_rejects_dois():
    assert extract_pmid("10.1038/nature12373") is None


def test_is_plausible_http_url():
    assert _is_plausible_http_url("https://example.org/a.pdf")
    assert _is_plausible_http_url("http://example.org/a.pdf")
    assert not _is_plausible_http_url("ftp://example.org/a.pdf")
    assert not _is_plausible_http_url("not a url")
    assert not _is_plausible_http_url("")
    assert not _is_plausible_http_url(None)


def test_xml_path_for_pdf_path():
    assert _xml_path_for_pdf_path("/tmp/a/10.1--b.pdf") == "/tmp/a/10.1--b.xml"


# ---------------------------------------------------------------------------
# Chain-cost changes: gating, fan-out caps, and the timing registry.
#
# Measured across four completed --tracksource runs (18,487 attributed
# artifacts), chain steps 10-15 produced 114 hits (0.62%) -- DataCite and the
# publisher-Playwright route produced zero. These tests pin the resulting cuts so
# a later well-meaning revert has to argue with a failing assertion.
# ---------------------------------------------------------------------------

# NOT "import fetchpdf.fetchpdf as fpd": fetchpdf/__init__.py does
# "from .fetchpdf import fetch_pdf_from_doi", which rebinds that name in
# the package namespace from the MODULE to the FUNCTION. The dotted-import form
# then resolves by attribute lookup and hands back the function, so every
# module-level lookup fails with a puzzling AttributeError on a 'function' object.
fpd = importlib.import_module("fetchpdf.fetchpdf")


class TestDataCiteGate:
    """DataCite was called for every DOI, twice, for zero hits ever."""

    @pytest.mark.parametrize("doi", [
        "10.1016/j.cell.2016.05.041",   # Elsevier, Crossref-registered
        "10.1038/nature12373",          # Springer Nature
        "10.1371/journal.pone.0000308",  # PLOS
        "10.1002/acp.4202",             # Wiley
    ])
    def test_crossref_prefixes_never_reach_the_network(self, doi, monkeypatch):
        def explode(*a, **kw):
            raise AssertionError(f"api.datacite.org was called for {doi}")

        monkeypatch.setattr(fpd.requests, "get", explode)
        assert fpd.try_datacite_fallback(doi, "/tmp/unused.pdf") is False

    @pytest.mark.parametrize("doi", [
        "10.5281/zenodo.1234567",
        "10.6084/m9.figshare.1234567",
        "10.17605/osf.io/abcde",
        "10.31234/osf.io/abcde",
    ])
    def test_repository_prefixes_are_still_tried(self, doi):
        assert fpd._is_datacite_prefix(doi) is True

    def test_non_doi_input_is_rejected_without_a_call(self):
        for value in ("", "not-a-doi", "39804400", None):
            assert fpd._is_datacite_prefix(value) is False


class _LandingResp:
    """A 200 HTML landing page, so the candidate loop is actually reached."""
    status_code = 200
    text = "<html></html>"
    url = "https://publisher.example/article"
    headers = {"content-type": "text/html"}
    content = b"<html></html>"


class TestLandingPageFanOut:
    """The single most expensive helper in the chain: 40 candidates x 2 attempts."""

    def test_candidate_list_is_capped(self, monkeypatch):
        tried = []
        monkeypatch.setattr(fpd, "try_download",
                            lambda url, path, verbose=False: tried.append(url) or False)
        monkeypatch.setattr(fpd, "try_download_with_session",
                            lambda *a, **kw: False)
        monkeypatch.setattr(fpd, "_collect_landing_page_candidates",
                            lambda landing, html: [f"https://x/{i}.pdf" for i in range(60)])

        # The function calls requests.get directly, NOT _get_with_retries.
        # Patching the wrong one makes the landing fetch fail, the function return
        # early, and the assertion below pass on zero calls -- green for the wrong
        # reason. This test only means something if try_download is reached.
        monkeypatch.setattr(fpd.requests, "get", lambda *a, **kw: _LandingResp())

        fpd.try_landing_page_pdf_fallback("10.1/x", "https://publisher.example/article",
                                          "/tmp/unused.pdf")
        assert tried, "candidate loop was never reached -- test proves nothing"
        assert len(tried) == fpd._LANDING_MAX_CANDIDATES == 8

    def test_session_retry_is_capped_tighter(self, monkeypatch):
        session_calls = []
        monkeypatch.setattr(fpd, "try_download", lambda *a, **kw: False)
        monkeypatch.setattr(fpd, "try_download_with_session",
                            lambda url, path, referer=None, verbose=False:
                            session_calls.append(url) or False)
        monkeypatch.setattr(fpd, "_collect_landing_page_candidates",
                            lambda landing, html: [f"https://x/{i}.pdf" for i in range(60)])

        # The function calls requests.get directly, NOT _get_with_retries.
        # Patching the wrong one makes the landing fetch fail, the function return
        # early, and the assertion below pass on zero calls -- green for the wrong
        # reason. This test only means something if try_download is reached.
        monkeypatch.setattr(fpd.requests, "get", lambda *a, **kw: _LandingResp())

        fpd.try_landing_page_pdf_fallback("10.1/x", "https://publisher.example/article",
                                          "/tmp/unused.pdf")
        assert session_calls, "session retry never reached -- test proves nothing"
        assert len(session_calls) == fpd._LANDING_SESSION_RETRIES == 2


def test_publisher_playwright_fallback_is_gone():
    """0 hits in 18,487 artifacts, and it launched a browser to get them."""
    assert not hasattr(fpd, "try_playwright_publisher_pdf_fallback")


class TestSourceTiming:
    """--tracksource recorded which source won and never what any source cost."""

    def setup_method(self):
        fpd.reset_source_timing()

    def teardown_method(self):
        fpd.reset_source_timing()

    def test_hit_records_call_hit_and_time(self):
        @fpd._timed("fake")
        def source():
            return "/tmp/got.pdf"

        assert source() == "/tmp/got.pdf"
        row = {r["source"]: r for r in fpd.source_timing_rows()}["fake"]
        assert row["calls"] == 1 and row["hits"] == 1
        assert row["seconds_per_hit"] != ""

    def test_miss_records_the_call_but_not_a_hit(self):
        @fpd._timed("fake")
        def source():
            return False

        source(); source()
        row = {r["source"]: r for r in fpd.source_timing_rows()}["fake"]
        assert row["calls"] == 2 and row["hits"] == 0
        # No hits means no finite cost per hit -- that blank is the whole signal.
        assert row["seconds_per_hit"] == ""

    def test_exception_still_records_the_call(self):
        @fpd._timed("boom")
        def source():
            raise RuntimeError("publisher had a bad day")

        with pytest.raises(RuntimeError):
            source()
        row = {r["source"]: r for r in fpd.source_timing_rows()}["boom"]
        assert row["calls"] == 1 and row["hits"] == 0

    def test_zero_hit_sources_sort_first(self):
        @fpd._timed("useless")
        def useless():
            return False

        @fpd._timed("useful")
        def useful():
            return "/tmp/x.pdf"

        useless(); useful()
        assert fpd.source_timing_rows()[0]["source"] == "useless"

    def test_the_real_chain_sources_are_instrumented(self):
        """The sources this exercise was about must all be measurable."""
        for name in ("datacite", "wiley", "apa_supplemental", "landing_page",
                     "core", "doaj", "escholarship", "pmid_direct",
                     "elsevier"):
            fn = {
                "datacite": fpd.try_datacite_fallback,
                "wiley": fpd.try_wiley_rendered_pdf_fallback,
                "apa_supplemental": fpd.try_apa_supplemental_fallback,
                "landing_page": fpd.try_landing_page_pdf_fallback,
                "core": fpd.try_core_fallback,
                "doaj": fpd.try_doaj_fallback,
                "escholarship": fpd.try_escholarship_via_pubmed,
                "pmid_direct": fpd.try_pmid_direct_pdf_fallback,
                "elsevier": fpd.try_elsevier_fulltext_api_fallback,
            }[name]
            assert hasattr(fn, "__wrapped__"), f"{name} is not instrumented"




# ---------------------------------------------------------------------------
# Review fixes: three bugs that each looked like success.
# ---------------------------------------------------------------------------


class TestStdioIsRestored:
    """batch_fetch_pdfs used to leave stdout permanently broken.

    It did `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` and never put the
    original back. The discarded wrapper CLOSES that buffer when collected, so a
    second call in the same process printed to a dead stream -- and any test that
    called the function killed the rest of the session with
    "ValueError: I/O operation on closed file". It took out 181 tests once.
    """

    def test_streams_are_the_same_objects_afterwards(self, tmp_path):
        import sys

        before_out, before_err = sys.stdout, sys.stderr
        fpd.batch_fetch_pdfs([], str(tmp_path), create_missing_report=False)
        assert sys.stdout is before_out
        assert sys.stderr is before_err

    def test_two_calls_in_one_process_both_work(self, tmp_path, capsys):
        fpd.batch_fetch_pdfs([], str(tmp_path), create_missing_report=False)
        fpd.batch_fetch_pdfs([], str(tmp_path), create_missing_report=False)
        # The real assertion: capture still functions after two calls.
        print("still alive")
        assert "still alive" in capsys.readouterr().out


class TestElsevierGateRequiresRealStructure:
    """The gate accepted two payloads that contain no usable full text."""

    def _envelope(self, inner):
        return (
            '<?xml version="1.0"?><full-text-retrieval-response>'
            "<coredata><dc:title>A paper</dc:title></coredata>"
            + inner + "</full-text-retrieval-response>"
        )

    def test_abstract_only_is_rejected(self, monkeypatch, tmp_path):
        """An abstract is not full text, yet <ce:abstract> alone used to pass."""
        body = self._envelope("<ce:abstract>" + "Short abstract. " * 20 + "</ce:abstract>")
        self._assert_rejected(monkeypatch, tmp_path, body)

    def test_flat_rawtext_is_rejected(self, monkeypatch, tmp_path):
        """<xocs:rawtext> is the article as one string: no sections, no tables.

        Observed on 10.1016/s0924-977x(00)80463-0 -- accepted by this gate, and the
        resulting .xml had zero <table> elements. For a pipeline whose purpose is
        reading tables that is the worst possible thing to record as a win.
        """
        body = self._envelope("<xocs:rawtext>" + "flat running text " * 500 + "</xocs:rawtext>")
        self._assert_rejected(monkeypatch, tmp_path, body)

    def test_real_body_is_still_accepted(self, monkeypatch, tmp_path):
        body = self._envelope(
            "<originalText><xocs:doc><ja:body><ce:sections><ce:para>"
            + "Real sectioned full text. " * 100
            + "</ce:para></ce:sections></ja:body></xocs:doc></originalText>"
        )
        out = self._run(monkeypatch, tmp_path, body)
        assert out is not None and out.endswith(".xml")

    def _assert_rejected(self, monkeypatch, tmp_path, body):
        assert self._run(monkeypatch, tmp_path, body) is None
        assert list(tmp_path.glob("*.xml")) == []

    def _run(self, monkeypatch, tmp_path, body):
        class R:
            status_code = 200
            text = body

        # Pinned, not inherited from the environment. Without a key the
        # function returns None before reaching the gate, so the two rejection
        # tests would pass for the wrong reason and this one would fail.
        monkeypatch.setattr(fpd, "_ELSEVIER_TDM_API_KEY", "test-key")
        monkeypatch.setattr(fpd.requests, "get", lambda *a, **kw: R())
        monkeypatch.setattr(fpd, "_extract_elsevier_pii_from_crossref", lambda m: "S0000000000")
        return fpd.try_elsevier_fulltext_api_fallback.__wrapped__(
            "10.1016/x", str(tmp_path / "rec.pdf"), crossref_message={"link": []},
        )


class TestElsevierFirstPagePreviewIsRejected:
    """Partial entitlement is a REAL one-page PDF, not an error payload.

    Observed 2026-08-16 on PII S0022510X13030578 (10.1016/j.jns.2013.11.028):
    HTTP 200, %PDF magic, 1.9 MB, genuine typeset text -- and 1 page of 8. The
    magic-bytes gate filed it as a success twice. The only in-band signal is
    the X-ELS-Status response header, which is what these tests pin.
    """

    WARNING = ("WARNING - Response limited to first page because "
               "requestor not entitled to resource")

    def _run(self, monkeypatch, tmp_path, response_headers):
        class R:
            status_code = 200
            content = b"%PDF-1.4 one lonely page"
            headers = response_headers

        monkeypatch.setattr(fpd.requests, "get", lambda *a, **kw: R())
        save = tmp_path / "rec.pdf"
        return fpd._try_elsevier_pdf_by_pii("S0000000000", str(save), "test-key"), save

    def test_preview_is_discarded_not_saved(self, monkeypatch, tmp_path):
        out, save = self._run(monkeypatch, tmp_path, {"X-ELS-Status": self.WARNING})
        assert out is None
        assert not save.exists()

    def test_lowercased_header_still_matches(self, monkeypatch, tmp_path):
        """The live API serves `x-els-status` over HTTP/2; requests' own dict
        is case-insensitive but the ladder's plain-dict Response is not."""
        out, save = self._run(monkeypatch, tmp_path, {"x-els-status": self.WARNING})
        assert out is None
        assert not save.exists()

    def test_full_pdf_without_warning_is_still_accepted(self, monkeypatch, tmp_path):
        out, save = self._run(monkeypatch, tmp_path, {"X-ELS-Status": "OK"})
        assert out == str(save)
        assert save.read_bytes().startswith(b"%PDF")

    def test_elsevier_stays_after_every_other_fallback_in_the_chain(self):
        """Deferral is what makes discarding the preview safe in the legacy
        chain: every other PDF route has already run by the time Elsevier is
        consulted, so a rejected preview leaves nothing untried. If someone
        moves the Elsevier block earlier, a preview would once again be able
        to preempt fallbacks that might hold the full PDF."""
        import inspect

        source = inspect.getsource(fpd.fetch_pdf)
        elsevier = source.index("Deferred Elsevier fallback")
        # The private last-resort block sits above Elsevier when present; a
        # scrubbed tree has no such block, and Elsevier must still be last.
        if "LAST RESORTS" in source:
            assert source.index("LAST RESORTS") < elsevier
        assert "Deferred Elsevier fallback" in source


def test_semantic_scholar_tls_is_verified():
    """A permanent verify=False bypass lived here behind a stale comment.

    Probed 2026-07-30: S2's certificate verifies. The bypass now only happens inside
    an SSLError handler, for one call, with a printed warning.
    """
    import inspect

    source = inspect.getsource(fpd.fetch_pdf)
    s2 = source[source.index("Semantic Scholar ---"):][:1600]
    # The unconditional keyword must be gone; the guarded retry may remain.
    assert "verify=False,  # S2 cert has expired" not in s2
    assert "except SSLError" in s2


def test_no_dead_alt_version_machinery():
    """T8 / preprint-marker scaffolding: unreachable, so deleted."""
    from fetchpdf.retrieval import artifact, tiers

    assert not hasattr(tiers, "PREPRINT_MARKER")
    assert not hasattr(tiers, "VOR_EXTENSIONS")
    assert not hasattr(tiers.Tier, "T8_ALT_VERSION")
    assert len(list(tiers.Tier)) == 7
    assert not hasattr(artifact.Artifact, "is_version_of_record")


def test_source_spec_has_no_inert_host_field():
    """It was assigned, never read, and its comment claimed it drove rate limiting.

    Real bucketing is on urlparse(url).netloc inside HttpClient, so editing a
    per-source host in ladder.json changed nothing at all.
    """
    from fetchpdf.retrieval.tiers import load_ladder

    ladder = load_ladder()
    spec = next(iter(ladder.sources.values()))
    assert not hasattr(spec, "host")
    assert all("host" not in s for s in ladder.raw["sources"].values())


class TestWindowsNameRules:
    """Character escaping is not enough: two Windows rules are about the NAME.

    Both survive a forbidden-character pass, and both were live defects:
      * a trailing dot/space is silently STRIPPED by Windows, so the directory
        --make-subfolder creates stops matching the path the code holds;
      * a basename whose pre-extension part is a DOS device IS that device --
        open() does not raise, it writes to the console and leaves no file.
    """

    RESERVED = (frozenset({"CON", "PRN", "AUX", "NUL"})
                | frozenset(f"COM{i}" for i in range(1, 10))
                | frozenset(f"LPT{i}" for i in range(1, 10)))

    def _roundtrip(self, doi):
        encoded = fpd.doi_to_safe_filename(doi)
        decoded = fpd._decode_fs_tokens(encoded).replace("--", "/").replace("\x00", "-")
        return encoded, decoded

    def test_trailing_dot_is_escaped(self):
        encoded, decoded = self._roundtrip("10.1234/x.")
        assert not encoded.endswith((".", " "))
        assert decoded == "10.1234/x."

    def test_trailing_space_is_escaped(self):
        encoded, decoded = self._roundtrip("10.1234/y ")
        assert not encoded.endswith((".", " "))
        assert decoded == "10.1234/y "

    def test_bare_device_names_are_defused(self):
        """Reachable from a bare identifier; a real DOI always starts "10."."""
        for name in ("con", "NUL", "com1", "aux", "LPT9", "prn"):
            encoded, decoded = self._roundtrip(name)
            assert encoded.split(".")[0].upper() not in self.RESERVED, encoded
            assert decoded == name

    def test_device_word_inside_a_real_doi_is_left_alone(self):
        """10.1234--con is not a device: Windows tests the whole basename."""
        assert fpd.doi_to_safe_filename("10.1234/con") == "10.1234--con"

    def test_real_dois_are_unchanged_by_the_name_pass(self):
        """The fix must not rename a single existing corpus artifact."""
        for doi in ("10.1073/pnas.2217551120", "10.1016/j.jebo.2026.107671",
                    "10.18260/1-2--47556", "10.1023/a:123", "10.1145/3.1"):
            encoded, decoded = self._roundtrip(doi)
            assert decoded == doi
            assert "~2e~" not in encoded and "~20~" not in encoded


# ---------------------------------------------------------------------------
# OA XML fallback, landing Accept, CORE timeout breaker.
# The 6361-paper run missed gold-OA JATS (eLife, PLOS Currents) because the
# default chain only saved Elsevier XML. These pin the recovery paths. Offline.
# ---------------------------------------------------------------------------

_JATS_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<article xmlns:xlink="http://www.w3.org/1999/xlink">'
    b"<front><article-meta><title-group>"
    b"<article-title>Correction: a real paper</article-title>"
    b"</title-group></article-meta></front>"
    b"<body>" + b"<p>Full text paragraph of the correction. </p>" * 12
    + b"</body></article>"
)


class _XmlResp:
    def __init__(self, content=_JATS_BODY, status=200):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": "application/xml"}
        self.text = content.decode("utf-8", errors="replace")
        self.url = "https://example.org/full.xml"


class TestJatsXmlFallback:
    def test_elife_cdn_url_from_doi(self):
        doi = "10.7554/eLife.34573"
        assert fpd._elife_article_id(doi) == "34573"
        urls = fpd._elife_xml_urls(doi)
        assert urls[0] == (
            "https://cdn.elifesciences.org/articles/34573/elife-34573-v1.xml"
        )
        assert fpd._elife_article_id("10.1002/ajmg.b.32068") is None
        assert fpd._elife_xml_urls("10.1002/ajmg.b.32068") == []

    def test_crossref_tdm_xml_keeps_text_mining_drops_similarity(self):
        message = {
            "link": [
                {"URL": "https://cdn.elifesciences.org/articles/34573/elife-34573-v1.xml",
                 "content-type": "application/xml",
                 "intended-application": "text-mining"},
                {"URL": "https://elifesciences.org/articles/34573",
                 "content-type": "unspecified",
                 "intended-application": "similarity-checking"},
                {"URL": "https://api.elsevier.com/content/article/PII:S1",
                 "content-type": "text/xml",
                 "intended-application": "text-mining"},
            ]
        }
        urls = fpd._crossref_tdm_xml_urls(message)
        assert urls == [
            "https://cdn.elifesciences.org/articles/34573/elife-34573-v1.xml",
            "https://api.elsevier.com/content/article/PII:S1",
        ]

    def test_error_bean_is_not_full_text(self):
        bean = (
            b'<?xml version="1.0"?><errorBean><errCode>0</errCode>'
            b"<errMsg>Article with id PMC1 is not open access one</errMsg>"
            b"</errorBean>"
        )
        assert fpd._looks_like_jats_fulltext(bean) is False
        assert fpd._looks_like_jats_fulltext(_JATS_BODY) is True

    def test_pmc_xml_is_saved_when_render_would_404(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd, "_get_with_retries", lambda *a, **k: _XmlResp())
        out = fpd.try_pmc_xml_fallback(
            "PMC5758112", str(tmp_path / "rec.pdf"), doi="10.7554/eLife.34573"
        )
        assert out == str(tmp_path / "rec.xml")
        assert (tmp_path / "rec.xml").read_bytes().startswith(b"<?xml")
        assert b"<body" in (tmp_path / "rec.xml").read_bytes()

    def test_pmc_xml_rejects_error_bean(self, monkeypatch, tmp_path):
        bean = (
            b'<?xml version="1.0"?><errorBean><errCode>0</errCode>'
            b"<errMsg>Article with id PMC1 is not open access one</errMsg>"
            b"</errorBean>" + b" " * 200
        )
        monkeypatch.setattr(fpd, "_get_with_retries",
                            lambda *a, **k: _XmlResp(content=bean))
        assert fpd.try_pmc_xml_fallback("PMC1", str(tmp_path / "rec.pdf")) is None
        assert list(tmp_path.glob("*.xml")) == []

    def test_elife_fallback_uses_api_xml_field(self, monkeypatch, tmp_path):
        class Api:
            status_code = 200
            def json(self):
                return {"xml": "https://cdn.elifesciences.org/articles/34573/elife-34573-v1.xml"}
        calls = []
        def fake_get(url, *a, **k):
            calls.append(url)
            if "api.elifesciences.org" in url:
                return Api()
            return _XmlResp()
        monkeypatch.setattr(fpd, "_get_with_retries", fake_get)
        out = fpd.try_elife_xml_fallback(
            "10.7554/eLife.34573", str(tmp_path / "rec.pdf")
        )
        assert out == str(tmp_path / "rec.xml")
        assert any("api.elifesciences.org/articles/34573" in u for u in calls)

    def test_fetch_pdf_gates_pmc_xml_on_allow_xml_fallback(self):
        import inspect
        source = inspect.getsource(fpd.fetch_pdf)
        pmc = source[source.index("PubMed Central"):source.index("eScholarship")]
        assert "allow_xml_fallback" in pmc
        assert "try_pmc_xml_fallback" in pmc
        assert "try_elife_xml_fallback" in source
        assert "_crossref_tdm_xml_urls" in source


class TestLandingPageHeaders:
    def test_download_accept_still_prefers_pdf(self):
        accept = fpd.headers["Accept"]
        assert accept.lower().startswith("application/pdf")
        assert "text/html" in accept

    def test_html_headers_lead_with_html_and_are_not_same_origin(self):
        assert fpd.HTML_HEADERS["Accept"].startswith("text/html")
        assert fpd.HTML_HEADERS.get("Sec-Fetch-Site") == "none"
        assert "Referer" not in fpd.HTML_HEADERS
        assert fpd.headers.get("Sec-Fetch-Site") == "none"
        assert fpd.headers.get("Referer") in (None, "")

    def test_landing_get_uses_html_accept(self, monkeypatch, tmp_path):
        seen = []
        class R:
            status_code = 200
            text = "<html></html>"
            url = "https://publisher.example/article"
            headers = {"content-type": "text/html"}
            content = b"<html></html>"
        def fake_get(url, *a, **kw):
            seen.append(kw.get("headers") or {})
            return R()
        monkeypatch.setattr(fpd.requests, "get", fake_get)
        monkeypatch.setattr(fpd, "try_download", lambda *a, **k: False)
        monkeypatch.setattr(fpd, "try_download_with_session", lambda *a, **k: False)
        monkeypatch.setattr(fpd, "_collect_landing_page_candidates",
                            lambda *a, **k: [])
        fpd.try_landing_page_pdf_fallback(
            "10.1/x", "https://publisher.example/article", str(tmp_path / "x.pdf")
        )
        assert seen, "landing GET never happened"
        assert seen[0]["Accept"].startswith("text/html")

    def test_406_retries_once_with_minimal_headers(self, monkeypatch, tmp_path):
        seen = []
        class R:
            def __init__(self, status):
                self.status_code = status
                self.text = "<html></html>"
                self.url = "https://publisher.example/article"
                self.headers = {"content-type": "text/html"}
                self.content = b"<html></html>"
        def fake_get(url, *a, **kw):
            seen.append(kw.get("headers") or {})
            return R(406 if len(seen) == 1 else 200)
        monkeypatch.setattr(fpd.requests, "get", fake_get)
        monkeypatch.setattr(fpd, "try_download", lambda *a, **k: False)
        monkeypatch.setattr(fpd, "try_download_with_session", lambda *a, **k: False)
        monkeypatch.setattr(fpd, "_collect_landing_page_candidates",
                            lambda *a, **k: [])
        fpd.try_landing_page_pdf_fallback(
            "10.1/x", "https://publisher.example/article", str(tmp_path / "x.pdf")
        )
        assert len(seen) == 2
        assert seen[0] is fpd.HTML_HEADERS or seen[0]["Accept"].startswith("text/html")
        assert seen[1] is fpd._HTML_HEADERS_MINIMAL or "Sec-Fetch-Site" not in seen[1]


class TestOaLocationWalk:
    """best_oa_location is often a publisher landing; the PDF is in oa_locations[]."""

    def test_unpaywall_repository_pdf_beats_publisher_landing(self):
        data = {
            "best_oa_location": {
                "url": "https://publisher.example/article",
                "url_for_pdf": None,
                "host_type": "publisher",
                "version": "publishedVersion",
            },
            "oa_locations": [
                {
                    "url": "https://publisher.example/article",
                    "url_for_pdf": None,
                    "host_type": "publisher",
                    "version": "publishedVersion",
                },
                {
                    "url": "https://repo.example/bitstreams/1",
                    "url_for_pdf": "https://repo.example/bitstreams/1.pdf",
                    "host_type": "repository",
                    "version": "acceptedVersion",
                },
            ],
        }
        urls = fpd._collect_unpaywall_pdf_urls(data)
        assert urls[0] == ("https://repo.example/bitstreams/1.pdf", True)
        assert any(u == "https://publisher.example/article" for u, _ in urls)

    def test_unpaywall_dedupes_the_best_copy(self):
        loc = {"url_for_pdf": "https://x.pdf", "url": "https://x.pdf",
               "host_type": "repository"}
        urls = fpd._collect_unpaywall_pdf_urls(
            {"best_oa_location": loc, "oa_locations": [loc]}
        )
        assert urls == [("https://x.pdf", True)]

    def test_openalex_uses_pdf_url_not_unpaywall_field_names(self):
        """The default chain used to read url_for_pdf on an OpenAlex payload."""
        data = {
            "best_oa_location": {
                "is_oa": True,
                "pdf_url": None,
                "landing_page_url": "https://publisher.example/a",
                "source": {"type": "journal"},
            },
            "locations": [
                {
                    "is_oa": True,
                    "pdf_url": "https://arxiv.org/pdf/2401.00001",
                    "landing_page_url": "https://arxiv.org/abs/2401.00001",
                    "source": {"type": "repository"},
                },
                {
                    "is_oa": False,
                    "pdf_url": "https://closed.example/secret.pdf",
                    "source": {"type": "journal"},
                },
            ],
        }
        urls = fpd._collect_openalex_pdf_urls(data)
        assert urls[0] == ("https://arxiv.org/pdf/2401.00001", True)
        assert all("closed.example" not in u for u, _ in urls)

    def test_oa_try_caps_direct_and_landing(self, monkeypatch):
        tried_direct, tried_landing = [], []
        monkeypatch.setattr(
            fpd, "try_download",
            lambda url, path, verbose=False: tried_direct.append(url) or False,
        )
        monkeypatch.setattr(
            fpd, "try_landing_page_pdf_fallback",
            lambda doi, url, path, verbose=False: tried_landing.append(url) or False,
        )
        cands = [(f"https://repo/{i}.pdf", True) for i in range(10)]
        cands += [(f"https://pub/{i}", False) for i in range(10)]
        assert fpd._try_oa_location_urls("10.1/x", cands, "/tmp/x.pdf") is False
        assert len(tried_direct) == fpd._OA_DIRECT_MAX == 5
        assert len(tried_landing) == fpd._OA_LANDING_MAX == 2


class TestCrossrefPreprintRelations:
    def test_has_preprint_yields_the_doi(self):
        message = {
            "relation": {
                "has-preprint": [
                    {"id-type": "doi", "id": "10.31234/osf.io/abcde"},
                    {"id-type": "doi", "id": "10.1002/same-as-self"},
                ]
            }
        }
        assert fpd._crossref_related_dois(message, "10.1002/same-as-self") == [
            "10.31234/osf.io/abcde"
        ]

    def test_doi_org_prefix_is_stripped_and_non_dois_dropped(self):
        message = {
            "relation": {
                "is-preprint-of": [
                    {"id-type": "doi", "id": "https://doi.org/10.1037/pst0000581"},
                    {"id-type": "pmid", "id": "12345678"},
                    {"id": "10.1101/2024.01.01.12345"},
                ]
            }
        }
        assert fpd._crossref_related_dois(message, "10.1/x") == [
            "10.1037/pst0000581",
            "10.1101/2024.01.01.12345",
        ]

    def test_caps_at_two(self):
        message = {
            "relation": {
                "has-preprint": [
                    {"id-type": "doi", "id": f"10.1/{i}"} for i in range(5)
                ]
            }
        }
        assert len(fpd._crossref_related_dois(message, "10.9/z")) == 2


class TestCoreTimeoutBreaker:
    def setup_method(self):
        fpd._CORE_TIMEOUT_COUNT = 0
        fpd._CORE_TIMEOUT_DISABLED = False
        fpd._CORE_SESSION_DISABLED = False

    def teardown_method(self):
        fpd._CORE_TIMEOUT_COUNT = 0
        fpd._CORE_TIMEOUT_DISABLED = False

    def test_search_goes_through_the_retry_helper(self, monkeypatch):
        monkeypatch.setenv("COREAPIKEY", "test-key")
        called = {}
        def fake_get(url, *a, **kw):
            called["url"] = url
            called["timeout"] = kw.get("timeout")
            called["retries"] = kw.get("retries")
            raise AssertionError("stop after observing the call")
        monkeypatch.setattr(fpd, "_get_with_retries", fake_get)
        monkeypatch.setattr(fpd, "_core_quota_exhausted", lambda verbose=False: False)
        try:
            fpd.try_core_fallback.__wrapped__("10.1/x", "/tmp/x.pdf")
        except AssertionError:
            pass
        assert "api.core.ac.uk" in called.get("url", "")
        assert called.get("timeout") == 30
        assert called.get("retries") == 3

    def test_repeated_timeouts_skip_core_for_the_rest_of_the_run(self, monkeypatch):
        from requests.exceptions import Timeout
        monkeypatch.setenv("COREAPIKEY", "test-key")
        monkeypatch.setattr(fpd, "_core_quota_exhausted", lambda verbose=False: False)
        monkeypatch.setattr(
            fpd, "_get_with_retries",
            lambda *a, **k: (_ for _ in ()).throw(Timeout("read timed out")),
        )
        for _ in range(fpd._CORE_TIMEOUT_LIMIT):
            assert fpd.try_core_fallback.__wrapped__("10.1/x", "/tmp/x.pdf") is False
        assert fpd._CORE_TIMEOUT_DISABLED is True
        # Next call must not hit the network.
        monkeypatch.setattr(
            fpd, "_get_with_retries",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("CORE was not skipped")),
        )
        assert fpd.try_core_fallback.__wrapped__("10.1/x", "/tmp/x.pdf") is False
