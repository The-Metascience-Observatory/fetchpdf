"""The default chain settles for XML only after every PDF source has failed."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import fetchpdf.fetchpdf as fpd

DOI = "10.1371/journal.pone.0000308"
PMCID = "PMC1817752"


def _miss(*a, **kw):
    return SimpleNamespace(status_code=404, headers={}, text="", content=b"",
                           url="https://publisher.example/article", json=lambda: {})


@pytest.fixture
def chain(monkeypatch, tmp_path):
    """A chain where PMC knows the paper but serves no PDF, and nothing else hits."""
    def get(url, *a, **kw):
        if "idconv" in url and DOI in url:
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"records": [{"pmcid": PMCID}]})
        return _miss()

    monkeypatch.setattr(fpd.requests, "get", Mock(side_effect=_miss))
    monkeypatch.setattr(fpd, "_get_with_retries", Mock(side_effect=get))
    monkeypatch.setattr(fpd, "_crossref_raw_get", Mock(side_effect=_miss))
    monkeypatch.setattr(fpd, "doi_to_pmid", lambda *a, **kw: None)
    for name in ("try_core_fallback", "try_doaj_fallback", "try_datacite_fallback",
                 "try_apa_supplemental_fallback", "try_landing_page_pdf_fallback",
                 "try_download", "try_serpapi_scholar_fallback",
                 "try_pmc_efetch_xml_fallback"):
        monkeypatch.setattr(fpd, name, Mock(return_value=False))
    xml_path = str(tmp_path / "paper.xml")
    pmc_xml = Mock(return_value=xml_path)
    monkeypatch.setattr(fpd, "try_pmc_xml_fallback", pmc_xml)
    return SimpleNamespace(pdf=str(tmp_path / "paper.pdf"), xml=xml_path, pmc_xml=pmc_xml)


def test_a_later_pdf_beats_xml_from_pmc(monkeypatch, chain):
    monkeypatch.setattr(fpd, "try_core_fallback", Mock(return_value=True))
    source = [None]
    assert fpd._fetch_pdf_chain(DOI, chain.pdf, email="test@example.org", delay=0,
                                _source_out=source) == chain.pdf
    chain.pmc_xml.assert_not_called()
    assert source == ["core"]


def test_pmc_xml_is_the_last_resort(chain):
    source = [None]
    assert fpd._fetch_pdf_chain(DOI, chain.pdf, email="test@example.org", delay=0,
                                _source_out=source) == chain.xml
    chain.pmc_xml.assert_called_once()
    fpd.try_serpapi_scholar_fallback.assert_called_once()
    assert source == ["pmc"]


def test_pmid_route_tries_pdf_before_its_xml(monkeypatch, chain):
    # No PMCID from the DOI, so only the PMID route can reach PMC XML.
    monkeypatch.setattr(fpd, "_get_with_retries", Mock(side_effect=_miss))
    monkeypatch.setattr(fpd, "doi_to_pmid", lambda *a, **kw: "17327915")
    calls = []

    def pmid_route(pmid, save_path, verbose=False, allow_xml=True):
        calls.append(allow_xml)
        return chain.xml if allow_xml else False

    monkeypatch.setattr(fpd, "try_pmid_direct_pdf_fallback", pmid_route)
    source = [None]
    assert fpd._fetch_pdf_chain(DOI, chain.pdf, email="test@example.org", delay=0,
                                _source_out=source) == chain.xml
    assert calls == [False, True]
    fpd.try_serpapi_scholar_fallback.assert_called_once()
    assert source == ["pmid_direct"]
