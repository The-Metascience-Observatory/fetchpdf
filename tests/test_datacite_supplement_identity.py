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
