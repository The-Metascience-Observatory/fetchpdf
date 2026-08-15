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
