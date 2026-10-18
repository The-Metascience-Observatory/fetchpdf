"""PMID retrieval must never turn a cited DOI into the requested report."""
import importlib
from types import SimpleNamespace
import pytest

fetch = importlib.import_module("fetchpdf.fetchpdf")


def article(own="", pmid="123"):
    return f'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Clinical report</ArticleTitle></Article></MedlineCitation><PubmedData><ArticleIdList>{own}</ArticleIdList><ReferenceList><Reference><Citation>doi:10.1000/cited</Citation><ArticleIdList><ArticleId IdType="doi">10.1000/cited</ArticleId></ArticleIdList></Reference></ReferenceList></PubmedData></PubmedArticle></PubmedArticleSet>'


def test_only_matching_report_identifiers_are_used():
    assert fetch._primary_pubmed_dois(article(), "123") == set()
    own = '<ArticleId IdType="doi">10.1000/actual</ArticleId>'
    assert fetch._primary_pubmed_dois(article(own), "123") == {"10.1000/actual"}
    assert fetch._primary_pubmed_dois(article(own, "999"), "123") == set()


def test_reference_doi_and_crossref_title_guess_cannot_replace_pmid(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, text=article() if "efetch" in url else "", json=lambda: {})
    monkeypatch.setattr(fetch, "_get_with_retries", get)
    monkeypatch.setattr(fetch, "_get_pubmed_web_html", lambda *a, **k: '<meta name="citation_pmid" content="123"><p>References doi:10.1000/cited</p>')
    assert fetch.pmid_to_doi("123") is None
    assert not any("crossref" in url for url in calls)


@pytest.mark.parametrize("other_pmid", ["999", ""])
def test_wrong_idconv_and_europe_pmc_record_is_rejected(monkeypatch, other_pmid):
    payload = {"records":[{"pmid":other_pmid,"doi":"10.1000/wrong"}], "resultList":{"result":[{"id":other_pmid,"source":"MED","doi":"10.1000/wrong"}]}}
    monkeypatch.setattr(fetch, "_get_with_retries", lambda *a, **k: SimpleNamespace(status_code=200,text=article(),json=lambda:payload))
    monkeypatch.setattr(fetch, "_get_pubmed_web_html", lambda *a, **k: "")
    assert fetch.pmid_to_doi("123") is None


def test_exact_primary_doi_still_resolves(monkeypatch):
    text = article('<ArticleId IdType="doi">10.1000/actual</ArticleId>')
    monkeypatch.setattr(fetch, "_get_with_retries", lambda *a, **k: SimpleNamespace(status_code=200,text=text,json=lambda:{}))
    assert fetch.pmid_to_doi("123") == "10.1000/actual"


def test_xml_cited_requested_doi_does_not_validate_a_different_article():
    from fetchpdf.retrieval.validate import validate_t1
    xml = b'<article><front><article-meta><article-id pub-id-type="doi">10.1000/actual</article-id><article-id pub-id-type="pmid">123</article-id></article-meta></front><body><p>Complete clinical study results.</p></body><back><ref><pub-id pub-id-type="doi">10.1000/cited</pub-id></ref></back></article>'
    assert validate_t1(xml, min_chars=5, abstract_stub_chars=5, doi="10.1000/actual", pmid="123").ok
    assert not validate_t1(xml, min_chars=5, abstract_stub_chars=5, doi="10.1000/cited").ok
    assert not validate_t1(xml, min_chars=5, abstract_stub_chars=5, pmid="999").ok
