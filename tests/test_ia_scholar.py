"""Offline coverage for the Internet Archive Scholar fallback (issue #9).

The lookup answers 302 -> a Wayback capture of the PDF, or 404 with a JSON
`message`. Everything here is driven by fake responses of those two shapes.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import fetchpdf.fetchpdf as fpd

DOI = "10.1371/journal.pone.0000308"
PMID = "17375194"
WAYBACK = "https://web.archive.org/web/20171112043533/https://core.ac.uk/download/pdf/12211618.pdf"


class _Bucket:
    """Stands in for the shared token bucket: counts, never sleeps."""

    def __init__(self):
        self.acquired = 0
        self.penalties = []

    def acquire(self, tokens=1.0):
        self.acquired += 1
        return 0.0

    def penalize(self, seconds):
        self.penalties.append(seconds)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(fpd, "_IA_SCHOLAR_DISABLED", False)
    monkeypatch.setattr(fpd, "_IA_SCHOLAR_FAILURES", 0)
    buckets = {}
    monkeypatch.setattr(fpd, "_archive_bucket",
                        lambda host: buckets.setdefault(host, _Bucket()))
    monkeypatch.setattr(fpd.requests, "get",
                        Mock(side_effect=AssertionError("Unexpected HTTP")))
    monkeypatch.setattr(fpd, "_download_archived_pdf",
                        Mock(side_effect=AssertionError("Unexpected download")))
    monkeypatch.setattr(fpd, "_accept_downloaded_pdf",
                        Mock(side_effect=AssertionError("Unexpected identity check")))
    return buckets


def redirect(location=WAYBACK, status=302):
    return SimpleNamespace(status_code=status, headers={"Location": location},
                           json=lambda: {})


def miss(message, status=404, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: {"message": message})


NO_FULLTEXT = "no fulltext for doi:" + DOI
NO_RELEASE = "no release found with doi of " + DOI


def lookups(monkeypatch, *responses):
    get = Mock(side_effect=responses)
    monkeypatch.setattr(fpd.requests, "get", get)
    return get


def downloads(monkeypatch, downloaded=True, accepted=True):
    download = Mock(return_value=downloaded)
    accept = Mock(return_value=accepted)
    monkeypatch.setattr(fpd, "_download_archived_pdf", download)
    monkeypatch.setattr(fpd, "_accept_downloaded_pdf", accept)
    return download, accept


def test_a_redirect_is_downloaded_and_identity_checked(monkeypatch, isolated):
    get = lookups(monkeypatch, redirect())
    download, accept = downloads(monkeypatch)
    assert fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    assert get.call_args.args == (fpd._IA_SCHOLAR_LOOKUP,)
    assert get.call_args.kwargs["params"] == {"id_type": "doi", "id_value": DOI}
    # The Location is the answer; following it here would write unchecked bytes.
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"]["User-Agent"] == fpd.POLITE_USER_AGENT
    download.assert_called_once_with(WAYBACK, "out.pdf", False)
    accept.assert_called_once_with("out.pdf", DOI, WAYBACK, False)
    assert isolated["scholar.archive.org"].acquired == 1


def test_the_doi_is_lower_cased_for_the_lookup(monkeypatch):
    """The lookup is case-sensitive and fatcat stores DOIs in lower case."""
    get = lookups(monkeypatch, miss(NO_FULLTEXT))
    assert not fpd.try_ia_scholar_fallback("10.1371/JOURNAL.PONE.0000308", "out.pdf")
    assert get.call_args.kwargs["params"]["id_value"] == DOI


def test_no_fulltext_is_a_single_request_even_with_a_pmid(monkeypatch):
    """The Archive knows the record and holds no file: the PMID names the same
    release, so asking again cannot change the answer."""
    get = lookups(monkeypatch, miss(NO_FULLTEXT))
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf", pmid=PMID)
    assert get.call_count == 1


def test_an_unknown_doi_is_retried_by_pmid(monkeypatch):
    get = lookups(monkeypatch, miss(NO_RELEASE), redirect())
    download, accept = downloads(monkeypatch)
    assert fpd.try_ia_scholar_fallback(DOI, "out.pdf", pmid=PMID)
    assert get.call_count == 2
    assert get.call_args.kwargs["params"] == {"id_type": "pmid", "id_value": PMID}
    accept.assert_called_once_with("out.pdf", DOI, WAYBACK, False)


def test_an_unknown_doi_with_no_pmid_is_a_single_request(monkeypatch):
    get = lookups(monkeypatch, miss(NO_RELEASE))
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    assert get.call_count == 1


def test_pmid_alone_is_looked_up_and_left_to_the_callers_verification(monkeypatch):
    """No DOI means no title to verify against, exactly as for pmid_direct."""
    get = lookups(monkeypatch, redirect())
    download = Mock(return_value=True)
    monkeypatch.setattr(fpd, "_download_archived_pdf", download)
    assert fpd.try_ia_scholar_fallback(None, "out.pdf", pmid=PMID)
    assert get.call_args.kwargs["params"] == {"id_type": "pmid", "id_value": PMID}
    download.assert_called_once()
    fpd._accept_downloaded_pdf.assert_not_called()


def test_nothing_to_look_up_does_no_work():
    assert not fpd.try_ia_scholar_fallback(None, "out.pdf")
    assert not fpd.try_ia_scholar_fallback("", "out.pdf", pmid="not-a-pmid")
    fpd.requests.get.assert_not_called()


def test_a_capture_that_fails_the_identity_check_is_a_miss(monkeypatch):
    get = lookups(monkeypatch, redirect())
    download, accept = downloads(monkeypatch, accepted=False)
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf", pmid=PMID)
    accept.assert_called_once()
    # The PMID would be handed the same capture.
    assert get.call_count == 1


def test_a_capture_that_is_not_a_pdf_is_a_miss(monkeypatch):
    lookups(monkeypatch, redirect())
    download, accept = downloads(monkeypatch, downloaded=False)
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    accept.assert_not_called()


def test_a_capture_already_rejected_for_this_doi_is_not_fetched_again(monkeypatch):
    lookups(monkeypatch, redirect())
    monkeypatch.setattr(fpd, "_already_rejected", lambda doi, url: url == WAYBACK)
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    fpd._download_archived_pdf.assert_not_called()


@pytest.mark.parametrize("location", ["", "javascript:alert(1)", "ftp://host/x.pdf"])
def test_a_redirect_to_nowhere_usable_is_a_miss(monkeypatch, location):
    lookups(monkeypatch, redirect(location))
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    fpd._download_archived_pdf.assert_not_called()


def test_a_relative_redirect_is_resolved_against_the_lookup(monkeypatch):
    lookups(monkeypatch, redirect("/web/2020/https://example.org/a.pdf"))
    download, _ = downloads(monkeypatch)
    assert fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    assert download.call_args.args[0] == (
        "https://scholar.archive.org/web/2020/https://example.org/a.pdf")


@pytest.mark.parametrize("reply", [
    requests.Timeout("slow"), requests.ConnectionError("reset"),
    miss("", 503), miss("", 429),
])
def test_repeated_failures_stop_asking_for_the_rest_of_the_run(monkeypatch, reply, capsys):
    get = lookups(monkeypatch, *[reply] * fpd._IA_SCHOLAR_FAILURE_LIMIT)
    for _ in range(fpd._IA_SCHOLAR_FAILURE_LIMIT + 2):
        # A failed lookup is not "unknown DOI": it must not spend a PMID request.
        assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf", pmid=PMID)
    assert get.call_count == fpd._IA_SCHOLAR_FAILURE_LIMIT
    assert fpd._IA_SCHOLAR_DISABLED
    assert capsys.readouterr().out.count("Internet Archive Scholar is not answering") == 1


def test_an_answer_resets_the_failure_count(monkeypatch):
    lookups(monkeypatch, requests.Timeout("slow"), requests.Timeout("slow"),
            miss(NO_FULLTEXT), requests.Timeout("slow"), requests.Timeout("slow"))
    for _ in range(5):
        assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    assert not fpd._IA_SCHOLAR_DISABLED


def test_retry_after_drains_the_shared_bucket(monkeypatch, isolated):
    lookups(monkeypatch, miss("", 429, {"Retry-After": "7"}))
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf")
    assert isolated["scholar.archive.org"].penalties == [7.0]


def test_an_unreadable_404_body_does_not_spend_a_second_request(monkeypatch):
    """Only the Archive saying it has no such record earns the PMID retry."""
    def broken():
        raise ValueError("not json")

    get = lookups(monkeypatch,
                  SimpleNamespace(status_code=404, headers={}, json=broken))
    assert not fpd.try_ia_scholar_fallback(DOI, "out.pdf", pmid=PMID)
    assert get.call_count == 1


# ---------------------------------------------------------------------------
# Downloading the capture
# ---------------------------------------------------------------------------

RAW = WAYBACK.replace("20171112043533/", "20171112043533id_/")


@pytest.mark.parametrize("url, raw", [
    (WAYBACK, RAW),
    (RAW, RAW),
    (WAYBACK.replace("20171112043533/", "20171112043533if_/"), RAW),
    ("https://archive.org/download/item/paper.pdf", "https://archive.org/download/item/paper.pdf"),
    ("https://repository.example/web/2017/paper.pdf", "https://repository.example/web/2017/paper.pdf"),
])
def test_only_a_wayback_capture_is_rewritten_to_its_raw_form(url, raw):
    assert fpd._wayback_raw_url(url) == raw


class _Stream:
    """A streamed response: a status, a content type and a body."""

    def __init__(self, body=b"", status=200, ctype="application/pdf", headers=None):
        self.status_code = status
        self.headers = {"content-type": ctype, **(headers or {})}
        self._body = body

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


PDF_BYTES = b"%PDF-1.4 the capture"
VIEWER = _Stream(b"<!DOCTYPE html><iframe>", ctype="text/html")


@pytest.fixture
def real_downloader(monkeypatch):
    """Undo the autouse stub: these tests are about the downloader itself."""
    monkeypatch.setattr(fpd, "_download_archived_pdf", REAL_DOWNLOADER)


REAL_DOWNLOADER = fpd._download_archived_pdf


def test_the_raw_capture_is_asked_first_and_not_as_a_browser(
        monkeypatch, real_downloader, isolated, tmp_path):
    """Browser navigation headers get the Wayback viewer page, not the PDF."""
    get = lookups(monkeypatch, _Stream(PDF_BYTES))
    out = tmp_path / "out.pdf"
    assert fpd._download_archived_pdf(WAYBACK, str(out))
    assert out.read_bytes() == PDF_BYTES
    assert get.call_args.args == (RAW,)
    # A User-Agent and nothing else: an explicit Accept, even application/pdf,
    # is enough to be served the viewer on the plain form.
    assert get.call_args.kwargs["headers"] == {"User-Agent": fpd.POLITE_USER_AGENT}
    assert fpd.download_url_for(str(out)) == RAW
    assert isolated["web.archive.org"].acquired == 1


@pytest.mark.parametrize("first", [
    _Stream(b"<!DOCTYPE html>not found", status=404, ctype="text/html"),
    VIEWER,
    requests.Timeout("cold capture"),
])
def test_the_url_as_issued_is_the_second_attempt(monkeypatch, real_downloader, first, tmp_path):
    get = lookups(monkeypatch, first, _Stream(PDF_BYTES))
    out = tmp_path / "out.pdf"
    assert fpd._download_archived_pdf(WAYBACK, str(out))
    assert [c.args[0] for c in get.call_args_list] == [RAW, WAYBACK]
    assert fpd.download_url_for(str(out)) == WAYBACK


def test_a_capture_that_is_never_a_pdf_writes_nothing(monkeypatch, real_downloader, tmp_path):
    get = lookups(monkeypatch, VIEWER, VIEWER)
    out = tmp_path / "out.pdf"
    assert not fpd._download_archived_pdf(WAYBACK, str(out))
    assert get.call_count == 2
    assert not out.exists()


def test_a_non_wayback_target_is_asked_once(monkeypatch, real_downloader, isolated, tmp_path):
    get = lookups(monkeypatch, VIEWER)
    assert not fpd._download_archived_pdf(
        "https://archive.org/download/item/paper.pdf", str(tmp_path / "out.pdf"))
    assert get.call_count == 1
    assert isolated["archive.org"].acquired == 1


def test_a_429_from_wayback_drains_its_bucket(monkeypatch, real_downloader, isolated, tmp_path):
    lookups(monkeypatch, _Stream(status=429, ctype="text/html", headers={"Retry-After": "5"}),
            _Stream(PDF_BYTES))
    assert fpd._download_archived_pdf(WAYBACK, str(tmp_path / "out.pdf"))
    assert isolated["web.archive.org"].penalties == [5.0]


def test_both_archive_hosts_have_their_own_rate_limit():
    from fetchpdf.retrieval.tiers import load_ladder

    limits = load_ladder().rate_limits
    assert limits["scholar.archive.org"]["rps"] <= limits["_default"]["rps"]
    assert limits["web.archive.org"]["rps"] <= limits["_default"]["rps"]


def test_the_source_is_instrumented_and_named():
    assert hasattr(fpd.try_ia_scholar_fallback, "__wrapped__")
    assert "ia_scholar" in fpd.SOURCE_DISPLAY_NAMES


# ---------------------------------------------------------------------------
# Where it sits in the chain
# ---------------------------------------------------------------------------


def _http_miss(*a, **kw):
    return SimpleNamespace(status_code=404, headers={}, text="", content=b"",
                           url="https://publisher.example/article", json=lambda: {})


@pytest.fixture
def chain(monkeypatch, tmp_path):
    """A chain in which nothing ahead of the Archive finds the paper."""
    monkeypatch.setattr(fpd.requests, "get", Mock(side_effect=_http_miss))
    monkeypatch.setattr(fpd, "_get_with_retries", Mock(side_effect=_http_miss))
    monkeypatch.setattr(fpd, "_crossref_raw_get", Mock(side_effect=_http_miss))
    monkeypatch.setattr(fpd, "doi_to_pmid", lambda *a, **kw: None)
    for name in ("try_core_fallback", "try_doaj_fallback", "try_datacite_fallback",
                 "try_apa_supplemental_fallback", "try_landing_page_pdf_fallback",
                 "try_download", "try_serpapi_scholar_fallback",
                 "try_pmid_direct_pdf_fallback", "try_ia_scholar_fallback"):
        monkeypatch.setattr(fpd, name, Mock(return_value=False))
    return str(tmp_path / "paper.pdf")


def test_the_archive_is_asked_before_core_and_credited(monkeypatch, chain):
    monkeypatch.setattr(fpd, "try_ia_scholar_fallback", Mock(return_value=True))
    source = [None]
    assert fpd._fetch_pdf_chain(DOI, chain, email="test@example.org", delay=0,
                                _source_out=source) == chain
    assert source == ["ia_scholar"]
    assert fpd.try_ia_scholar_fallback.call_args.args[:2] == (DOI, chain)
    fpd.try_core_fallback.assert_not_called()


def test_a_miss_at_the_archive_falls_through_to_core(chain):
    assert fpd._fetch_pdf_chain(DOI, chain, email="test@example.org", delay=0) is None
    fpd.try_ia_scholar_fallback.assert_called_once()
    fpd.try_core_fallback.assert_called_once()


def test_a_pmid_with_no_doi_reaches_the_archive(monkeypatch, chain):
    monkeypatch.setattr(fpd, "resolve_identifier_to_doi", lambda *a, **kw: None)
    monkeypatch.setattr(fpd, "try_ia_scholar_fallback", Mock(return_value=True))
    source = [None]
    assert fpd._fetch_pdf_chain(PMID, chain, email="test@example.org", delay=0,
                                _source_out=source) == chain
    assert source == ["ia_scholar"]
    fpd.try_pmid_direct_pdf_fallback.assert_called_once()
    call = fpd.try_ia_scholar_fallback.call_args
    assert call.args[0] is None and call.kwargs == {"pmid": PMID}
