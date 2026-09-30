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
