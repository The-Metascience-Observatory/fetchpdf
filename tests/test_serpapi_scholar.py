"""Offline coverage for the optional, credit-bounded Scholar fallback."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import fetchpdf.fetchpdf as fpd
from conftest import text_pdf

DOI = "10.1234/scholar-test"
TITLE = "A randomized trial of interventions for improving research reproducibility"
PDF = "https://repository.example/paper.pdf"
LANDING = "https://publisher.example/article"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "private-test-key")
    monkeypatch.setattr(fpd, "_SERPAPI_SESSION_DISABLED", False)
    monkeypatch.setattr(fpd, "_title_for", lambda *a, **kw: TITLE)
    monkeypatch.setattr(fpd.requests, "get", Mock(side_effect=AssertionError("Unexpected HTTP")))


def response(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data)


def setup_search(monkeypatch, *responses):
    get = Mock(side_effect=responses)
    monkeypatch.setattr(fpd.requests, "get", get)
    return get


def hit(resources=None, title=TITLE, link=LANDING):
    return {"title": title, "link": link, "resources": resources or []}


def test_missing_key_does_no_work(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY")
    metadata = Mock(side_effect=AssertionError("Unexpected metadata lookup"))
    monkeypatch.setattr(fpd, "_title_for", metadata)
    assert not fpd.try_serpapi_scholar_fallback(DOI, "unused.pdf")
    fpd.requests.get.assert_not_called()
    metadata.assert_not_called()


def test_pdf_resources_rank_first_and_duplicate_links_are_removed(monkeypatch):
    get = setup_search(monkeypatch, response({"organic_results": [
        hit(), hit([{"file_format": "PDF", "link": PDF}], link=PDF),
        hit([{"file_format": "PDF", "link": "https://wrong.example/paper.pdf"}],
            title="An unrelated study of sedimentary rock formation"),
    ]}))
    download = Mock(return_value=True)
    monkeypatch.setattr(fpd, "_try_oa_location_urls", download)
    assert fpd.try_serpapi_scholar_fallback(DOI, "out.pdf")
    assert download.call_args.args[1] == [(PDF, True), (LANDING, False)]
    assert get.call_count == 1
    assert get.call_args.kwargs["params"] == {
        "engine": "google_scholar", "q": f'"{DOI}"',
        "api_key": "private-test-key", "num": 5, "hl": "en",
    }


def test_title_search_runs_after_failed_download_and_deduplicates(monkeypatch):
    data = {"organic_results": [hit([{"file_format": "PDF", "link": PDF}])]}
    get = setup_search(monkeypatch, response(data), response(data))
    download = Mock(return_value=False)
    monkeypatch.setattr(fpd, "_try_oa_location_urls", download)
    assert not fpd.try_serpapi_scholar_fallback(DOI, "out.pdf")
    assert get.call_count == 2
    assert get.call_args.kwargs["params"]["q"] == f'"{TITLE}"'
    assert download.call_args.args[1] == []


def test_unknown_title_uses_only_one_search(monkeypatch):
    monkeypatch.setattr(fpd, "_title_for", lambda *a, **kw: "")
    get = setup_search(monkeypatch, response({"organic_results": []}))
    assert not fpd.try_serpapi_scholar_fallback(DOI, "out.pdf")
    assert get.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_or_quota_failure_stops_later_records(monkeypatch, status, capsys):
    get = setup_search(monkeypatch, response({}, status))
    assert not fpd.try_serpapi_scholar_fallback(DOI, "out.pdf", True)
    assert not fpd.try_serpapi_scholar_fallback(DOI, "out.pdf", True)
    assert get.call_count == 1
    assert "private-test-key" not in capsys.readouterr().out


@pytest.mark.parametrize("reply", [
    response({}, 500), response({"error": "private-test-key"}),
    response([]), response({"organic_results": {"unexpected": True}}),
    requests.Timeout("https://serpapi.com/search.json?api_key=private-test-key"),
    ValueError("private-test-key"),
])
def test_errors_fail_closed_without_leaking_key(monkeypatch, reply, capsys):
    get = setup_search(monkeypatch, reply)
    assert not fpd.try_serpapi_scholar_fallback(DOI, "out.pdf", True)
    assert get.call_count == 1
    assert "private-test-key" not in capsys.readouterr().out
    assert not fpd._SERPAPI_SESSION_DISABLED


def test_wrong_pdf_is_rejected_and_next_copy_is_tried(monkeypatch, tmp_path):
    good = "https://repository.example/correct.pdf"
    setup_search(monkeypatch, response({"organic_results": [hit([
        {"file_format": "PDF", "link": PDF},
        {"file_format": "PDF", "link": good},
    ])]}))
    path = tmp_path / "paper.pdf"
    attempted = []

    def download(url, save_path, verbose=False):
        attempted.append(url)
        if url == PDF:
            path.write_bytes(text_pdf(["An unrelated study of geological formations",
                                      "doi:10.9999/wrong"] + ["Sedimentary rocks form in layers."] * 20))
        else:
            assert not path.exists(), "Rejected PDF must be removed"
            path.write_bytes(text_pdf(doi=DOI))
        return True

    monkeypatch.setattr(fpd, "try_download", download)
    monkeypatch.setattr(fpd, "try_landing_page_pdf_fallback", lambda *a, **kw: False)
    assert fpd.try_serpapi_scholar_fallback(DOI, str(path))
    assert attempted == [PDF, good]
    assert path.read_bytes().startswith(b"%PDF")


def test_landing_page_is_used_when_no_pdf_resource_exists(monkeypatch):
    setup_search(monkeypatch, response({"organic_results": [hit()]}))
    landing = Mock(return_value=True)
    monkeypatch.setattr(fpd, "try_landing_page_pdf_fallback", landing)
    assert fpd.try_serpapi_scholar_fallback(DOI, "out.pdf")
    landing.assert_called_once_with(DOI, LANDING, "out.pdf", False)


@pytest.mark.parametrize("earlier_hit", [False, True])
def test_chain_reaches_scholar_only_after_ordinary_sources(monkeypatch, tmp_path, earlier_hit):
    miss = SimpleNamespace(status_code=404, headers={}, text="", content=b"",
                           url=LANDING, json=lambda: {})
    monkeypatch.setattr(fpd.requests, "get", Mock(return_value=miss))
    monkeypatch.setattr(fpd, "_get_with_retries", Mock(return_value=miss))
    monkeypatch.setattr(fpd, "_crossref_raw_get", Mock(return_value=miss))
    monkeypatch.setattr(fpd, "doi_to_pmid", lambda *a, **kw: None)
    for name in ("try_core_fallback", "try_doaj_fallback", "try_datacite_fallback",
                 "try_apa_supplemental_fallback", "try_landing_page_pdf_fallback",
                 "try_download"):
        monkeypatch.setattr(fpd, name, Mock(return_value=False))
    monkeypatch.setattr(fpd, "try_core_fallback", Mock(return_value=earlier_hit))
    scholar = Mock(return_value=True)
    monkeypatch.setattr(fpd, "try_serpapi_scholar_fallback", scholar)
    source = [None]
    path = str(tmp_path / "paper.pdf")
    assert fpd._fetch_pdf_chain(DOI, path, email="test@example.org", delay=0,
                               allow_xml_fallback=False, _source_out=source) == path
    if earlier_hit:
        scholar.assert_not_called()
        assert source == ["core"]
    else:
        scholar.assert_called_once_with(DOI, path, False)
        assert source == ["serpapi_scholar"]
