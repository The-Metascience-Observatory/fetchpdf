"""Short published abstracts are not abstract-only full-research stubs."""
import xml.etree.ElementTree as ET

import pytest

from fetchpdf.retrieval.validate import validate_t1

DOI = '10.1000/conference-report'


def conference_xml():
    root = ET.fromstring('<article article-type="abstract"><front><article-meta>'
        '<article-id pub-id-type="doi">10.1000/conference-report</article-id>'
        '<article-id pub-id-type="pmid">12345</article-id>'
        '<title-group><article-title>A separately published structured conference report</article-title></title-group>'
        '</article-meta></front><body /></article>')
    body = root.find('body')
    for label in ('Purpose:', 'Methods:', 'Results:', 'Conclusion(s):'):
        p = ET.SubElement(body, 'p')
        heading = ET.SubElement(p, 'bold'); heading.text = label
        heading.tail = (' Source report prose preserves the published description and its qualifications.' * 10)
    return root


def validate(root, **kwargs):
    return validate_t1(ET.tostring(root), doi=kwargs.pop('doi', DOI), **kwargs)


def test_complete_publisher_abstract_passes_without_claiming_a_full_research_paper():
    root = conference_xml(); result = validate(root)
    assert result.ok and 2500 <= result.n_chars < 5000
    assert 'publisher-declared-short-abstract' in result.checks_passed
    assert 'own-article-identity-verified' in result.checks_passed
    assert 'structured-abstract-body' in result.checks_passed
    assert 'min-chars>=5000' not in result.checks_passed
    assert root.get('article-type') == 'abstract'


def test_pmid_only_requires_matching_primary_identifier():
    assert validate(conference_xml(), doi=None, pmid='12345').ok
    assert not validate(conference_xml(), doi=None, pmid='99999').ok


@pytest.mark.parametrize('change', ['research_article', 'missing_type', 'no_requested_id', 'wrong_doi',
    'wrong_pmid', 'missing_own_doi', 'doi_in_reference', 'missing_title', 'short_title',
    'missing_body', 'empty_body', 'nested_abstract', 'missing_methods', 'missing_results',
    'missing_conclusions', 'empty_results', 'nested_article', 'sub_article', 'error',
    'collection', 'denial'])
def test_shortcut_never_bypasses_missing_evidence_or_identity(change):
    root = conference_xml(); kwargs = {}
    if change == 'research_article': root.set('article-type', 'research-article')
    elif change == 'missing_type': root.attrib.pop('article-type')
    elif change == 'no_requested_id': kwargs['doi'] = None
    elif change == 'wrong_doi': kwargs['doi'] = '10.1000/other'
    elif change == 'wrong_pmid': kwargs['pmid'] = '99999'
    elif change in {'missing_own_doi', 'doi_in_reference'}:
        meta = root.find('front/article-meta'); meta.remove(meta.find('article-id'))
        if change == 'doi_in_reference':
            ref = ET.SubElement(ET.SubElement(root, 'back'), 'ref')
            ET.SubElement(ref, 'pub-id', {'pub-id-type':'doi'}).text = DOI
    elif change == 'missing_title': root.find('front/article-meta').remove(root.find('front/article-meta/title-group'))
    elif change == 'short_title': root.find('.//article-title').text = 'Abstract'
    elif change == 'missing_body': root.remove(root.find('body'))
    elif change == 'empty_body': root.find('body').clear()
    elif change == 'nested_abstract':
        body = root.find('body'); paragraphs = list(body); body.clear()
        abstract = ET.SubElement(body, 'abstract'); abstract.extend(paragraphs)
    elif change in {'missing_methods','missing_results','missing_conclusions','empty_results'}:
        target = {'missing_methods':1,'missing_results':2,'missing_conclusions':3,'empty_results':2}[change]
        paragraph = root.find('body')[target]
        if change == 'empty_results':
            paragraph[0].tail = ''
            root.find('body')[0][0].tail += ' Extra unrelated background.' * 45
        else: paragraph[0].text = 'Background:'
    elif change == 'nested_article': ET.SubElement(root, 'article')
    elif change == 'sub_article': ET.SubElement(root, 'sub-article')
    elif change == 'error': ET.SubElement(root, 'error').text = 'Partial response'
    elif change == 'collection':
        collection = ET.Element('pmc-articleset'); collection.append(root); root = collection
    elif change == 'denial': root.insert(0, ET.Comment('The publisher does not allow downloading of the full text'))
    result = validate(root, **kwargs)
    assert not result.ok
    assert 'publisher-declared-short-abstract' not in result.checks_passed


def test_body_stub_floor_is_preserved_even_for_correct_abstract_identity():
    root = conference_xml()
    for p in root.find('body'): p[0].tail = p[0].tail[:90]
    result = validate(root)
    assert not result.ok and 'abstract-length stub' in result.reason


def test_front_matter_abstract_cannot_replace_missing_research_body():
    root = conference_xml(); root.set('article-type','research-article')
    body = root.find('body'); root.remove(body); body.tag = 'abstract'
    root.find('front/article-meta').append(body)
    assert not validate(root).ok


def test_engine_accepts_identity_bound_short_abstract_and_archives_reason(tmp_path):
    from fetchpdf.retrieval.artifact import Artifact
    from fetchpdf.retrieval.identifiers import IdentifierSet
    from fetchpdf.retrieval.tiers import Tier
    from test_retrieval import FakeResolver, make_ladder
    from fetchpdf.retrieval.engine import retrieve_tiered
    xml = ET.tostring(conference_xml())
    def short_xml(ids, ctx):
        return Artifact(content=xml,tier=Tier.T1_XML,source='conference',url='https://example.org/report.xml',http_status=200)
    ladder = make_ladder({Tier.T1_XML:[('conference',short_xml)]})
    ladder.thresholds.update(t1_min_chars=5000, abstract_stub_chars=2500)
    result = retrieve_tiered(DOI,doi=DOI,save_path=str(tmp_path/'record.pdf'),ladder=ladder,
        resolver=FakeResolver(IdentifierSet(doi=DOI,pmid='12345')),want_provenance=True)
    assert result.path.endswith('.xml')
    from pathlib import Path
    assert Path(result.path).read_bytes() == xml
    accepted = [row for row in result.provenance.attempts if row.accepted]
    assert len(accepted) == 1
    assert 'publisher-declared-short-abstract' in accepted[0].checks_passed
