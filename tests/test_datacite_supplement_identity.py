from types import SimpleNamespace
from fetchpdf.retrieval.supplement_graph import _accept_related

ARTICLE = '10.1038/s41419-019-1376-9'
CTX = SimpleNamespace(log=lambda message: None)

def record(relation='References', identifier=ARTICLE):
    return {'attributes': {'doi': '10.6084/m9.figshare.27129665.v1',
        'types': {'resourceTypeGeneral': 'Dataset'},
        'titles': [{'title': 'Additional file 13 of a different ovarian study'}],
        'relatedIdentifiers': [{'relationType': relation, 'relatedIdentifier': identifier}]}}

def test_dataset_bibliography_does_not_establish_supplement_ownership():
    data = record()
    data['attributes']['relatedIdentifiers'].append({'relationType': 'IsSupplementTo', 'relatedIdentifier': '10.1186/s12917-024-04275-6'})
    assert _accept_related(data, ARTICLE, CTX) is None

def test_software_citation_is_also_not_ownership():
    data = record('IsCitedBy')
    data['attributes']['types']['resourceTypeGeneral'] = 'Software'
    assert _accept_related(data, ARTICLE, CTX) is None

def test_exact_non_citation_relation_accepts_doi_url():
    assert _accept_related(record('IsSupplementTo', 'https://doi.org/'+ARTICLE.upper()), ARTICLE, CTX)

def test_prefix_collision_and_missing_relation_do_not_pass():
    assert _accept_related(record('IsSupplementTo', ARTICLE+'0'), ARTICLE, CTX) is None
    assert _accept_related(record('', ARTICLE), ARTICLE, CTX) is None
    data=record();data['attributes']['relatedIdentifiers']=[]
    assert _accept_related(data, ARTICLE, CTX) is None


# -- Issue #8: only supplement-grade relations establish ownership ----------

import json
import os

import pytest

from fetchpdf.retrieval import supplement_graph as graph

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "datacite_derived_from_third_party.json")
ISSUE8_ARTICLE = "10.1038/s41388-021-02054-3"


def issue8_record():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["data"]


class _Response:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_issue8_third_party_derived_dataset_is_not_this_papers_supplement():
    """The real record: an unrelated author's 2024 Figshare compilation that
    IsDerivedFrom this article and two others."""
    assert _accept_related(issue8_record(), ISSUE8_ARTICLE, CTX) is None


def test_issue8_reverse_query_routes_nothing(monkeypatch):
    routed = []
    monkeypatch.setattr(graph, "_files_in_repository",
                        lambda doi, ids, ctx, **kw: routed.append(doi) or [])
    ctx = SimpleNamespace(
        log=lambda message: None, scratch={},
        http=SimpleNamespace(get=lambda *a, **kw: _Response({"data": [issue8_record()]})),
    )
    assert graph.enumerate_datacite_related(SimpleNamespace(doi=ISSUE8_ARTICLE), ctx) == []
    assert routed == []


def test_issue8_derived_dataset_is_not_upgraded_from_related_to_owned(monkeypatch):
    """The same test gates ScholeXplorer / Europe PMC links: a derived deposit
    they surface must stay `related`, not be promoted to `owned`."""
    monkeypatch.setattr(graph, "_datacite_attributes",
                        lambda doi, ctx: issue8_record()["attributes"])
    ctx = SimpleNamespace(log=lambda message: None, scratch={})
    assert graph._deposit_names_article(
        "10.6084/m9.figshare.25977025", ISSUE8_ARTICLE, ctx) is False


@pytest.mark.parametrize("relation", [
    "IsDerivedFrom", "IsVariantFormOf", "IsOriginalFormOf", "IsIdenticalTo",
    "IsVersionOf", "HasVersion", "IsNewVersionOf", "IsPreviousVersionOf",
    "Documents", "Describes", "Compiles", "IsCompiledBy", "Requires",
    "IsRequiredBy", "Reviews", "IsReviewedBy", "Continues", "IsContinuedBy",
    "Obsoletes", "IsObsoletedBy", "HasMetadata", "IsMetadataFor",
    "Collects", "IsCollectedBy", "IsPublishedIn", "IsTranslationOf",
    "HasTranslation", "SomethingNew",
])
def test_non_supplement_relations_do_not_establish_ownership(relation):
    assert _accept_related(record(relation), ARTICLE, CTX) is None


@pytest.mark.parametrize("relation", sorted(graph.SUPPLEMENT_RELATIONS))
def test_supplement_relations_establish_ownership_in_any_case(relation):
    assert _accept_related(record(relation.upper()), ARTICLE, CTX)


def test_one_supplement_relation_suffices_beside_a_derived_one():
    data = record("IsDerivedFrom")
    data["attributes"]["relatedIdentifiers"].append(
        {"relationType": "IsSupplementTo", "relatedIdentifier": ARTICLE})
    assert _accept_related(data, ARTICLE, CTX)


# -- Real supplements the type filter and the query used to lose -------------

TYPING_FIXTURE = os.path.join(os.path.dirname(FIXTURE), "datacite_supplement_typing.json")


def typing_record(doi):
    """A trimmed copy of the live DataCite record, captured 2026-11-13."""
    with open(TYPING_FIXTURE, encoding="utf-8") as f:
        for entry in json.load(f)["data"]:
            if entry["id"] == doi:
                return entry
    raise KeyError(doi)


def paper_typed(relations, specific="Journal contribution", general="JournalArticle"):
    return {"attributes": {
        "doi": "10.6084/m9.figshare.1",
        "types": {"resourceTypeGeneral": general, "resourceType": specific},
        "titles": [{"title": "Replication Data for: The article"}],
        "relatedIdentifiers": [{"relationType": kind, "relatedIdentifier": target}
                               for kind, target in relations]}}


def test_figshare_journal_contribution_supplement_is_kept():
    """Taylor & Francis's tpal_a_2753622_sm2942.docx: figshare item type
    "journal contribution", exported to DataCite as JournalArticle."""
    doi = "10.6084/m9.figshare.34735259"
    assert _accept_related(typing_record(doi), "10.1080/01916122.2026.2753622", CTX) == doi


def test_journal_contribution_is_kept_for_its_own_article_only():
    record_ = typing_record("10.6084/m9.figshare.34735259")
    assert _accept_related(record_, ARTICLE, CTX) is None


def test_a_paper_on_zenodo_is_still_not_a_supplement():
    """JournalArticle + IsSupplementTo without figshare's item type is a paper:
    17k such records, nearly all Zenodo."""
    record_ = typing_record("10.5281/zenodo.24197879")
    record_["attributes"]["relatedIdentifiers"].append(
        {"relationType": "IsSupplementTo", "relatedIdentifier": ARTICLE})
    assert _accept_related(record_, ARTICLE, CTX) is None


@pytest.mark.parametrize("relations", [
    [("IsPartOf", ARTICLE)],                 # looser ownership does not unlock a paper type
    [("IsSupplementedBy", ARTICLE)],
    [("IsCitedBy", ARTICLE)],
    [],                                      # nor does the replication-title match
    [("IsSupplementTo", ARTICLE), ("IsIdenticalTo", ARTICLE)],   # a copy of the article
    [("IsSupplementTo", ARTICLE), ("IsVersionOf", "https://doi.org/" + ARTICLE)],
    [("IsSupplementTo", ARTICLE + "0")],
])
def test_journal_contribution_needs_issupplementto_and_no_identity_claim(monkeypatch, relations):
    monkeypatch.setattr(graph, "_article_title", lambda doi, ctx: "The article")
    assert _accept_related(paper_typed(relations), ARTICLE, CTX) is None


def test_journal_contribution_with_its_own_version_chain_is_kept():
    """Every figshare item IsVersionOf / HasVersion its own DOI; that is not a
    claim to be the article."""
    data = paper_typed([("IsSupplementTo", ARTICLE),
                        ("IsVersionOf", "10.6084/m9.figshare.1"),
                        ("References", "10.1000/somebody.else")])
    data["attributes"]["doi"] = "10.6084/m9.figshare.1.v1"
    assert _accept_related(data, ARTICLE, CTX)


@pytest.mark.parametrize("general", ["Preprint", "Book", "BookChapter", "ConferencePaper",
                                     "PeerReview", "Report", "Dissertation", "DataPaper"])
def test_other_paper_types_are_still_dropped(general):
    data = record("IsSupplementTo")
    data["attributes"]["types"] = {"resourceTypeGeneral": general,
                                   "resourceType": "Journal contribution"}
    assert _accept_related(data, ARTICLE, CTX) is None


@pytest.mark.parametrize("doi, article", [
    ("10.5281/zenodo.24013608", "10.1101/2025.11.21.689558"),     # ComputationalNotebook
    ("10.5281/zenodo.8325626", "10.1016/j.jclepro.2023.136364"),  # InteractiveResource
])
def test_notebook_and_interactive_supplements_are_kept(doi, article):
    assert _accept_related(typing_record(doi), article, CTX) == doi


def test_sound_supplement_is_kept():
    data = record("IsSupplementTo")
    data["attributes"]["types"] = {"resourceTypeGeneral": "Sound"}
    assert _accept_related(data, ARTICLE, CTX)


def test_article_stored_as_a_doi_url_is_recognised():
    """Mendeley Data stores the article as https://doi.org/..., typed URL."""
    doi = "10.17632/tcpb72scrs.1"
    assert _accept_related(typing_record(doi), "10.2139/ssrn.7445040", CTX) == doi


def test_reverse_query_asks_for_both_spellings_of_the_doi():
    asked = []

    def get(url, params=None, **kwargs):
        asked.append(params["query"])
        return _Response({"data": []})

    ctx = SimpleNamespace(log=lambda message: None, scratch={},
                          http=SimpleNamespace(get=get))
    graph.enumerate_datacite_related(SimpleNamespace(doi="10.2139/SSRN.7445040"), ctx)
    assert asked == ['relatedIdentifiers.relatedIdentifier:'
                     '("10.2139/ssrn.7445040" OR "https://doi.org/10.2139/ssrn.7445040")']


@pytest.mark.parametrize("doi, expected", [
    ("https://doi.org/10.1/AbC", '("10.1/abc" OR "https://doi.org/10.1/abc")'),
    ('10.1/a"b', '("10.1/a\\"b" OR "https://doi.org/10.1/a\\"b")'),
    ("10.1/a\\b", '("10.1/a\\\\b" OR "https://doi.org/10.1/a\\\\b")'),
])
def test_reverse_query_normalises_and_escapes(doi, expected):
    assert graph._reverse_query(doi) == "relatedIdentifiers.relatedIdentifier:" + expected
