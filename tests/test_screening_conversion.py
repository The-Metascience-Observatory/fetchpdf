from fetchpdf.retrieval.to_markdown import jats_to_markdown


def test_screening_metadata_and_back_matter():
    xml = b'''<article xmlns="urn:jats" xmlns:xlink="http://www.w3.org/1999/xlink">
    <front><article-meta><title-group><article-title>Trial</article-title></title-group>
    <abstract><title>Abstract</title><sec><title>Results</title><p>We analyzed <bold>120</bold> patients.</p></sec></abstract>
    <history><date date-type="received"><day>2</day><month>4</month><year>2020</year></date>
    <date date-type="rev-recd"><month>6</month><year>2021</year></date>
    <date date-type="accepted" iso-8601-date="2021-07-03"/></history>
    <pub-date pub-type="epub"><year>2021</year></pub-date>
    <funding-group><funding-statement>Supported by grant ABC.</funding-statement></funding-group>
    </article-meta></front><body><sec><title>Methods</title><p>Enrolled 120.</p></sec></body>
    <back><ack><title>Acknowledgments</title><p>Thanks to the study team.</p></ack>
    <fn-group><fn fn-type="ethics">Approval <italic>preceded</italic> recruitment.</fn></fn-group>
    <sec><title>Data availability</title><p>Available on request.</p>
    <ref-list><title>References</title><ref><mixed-citation>OMIT THIS CITATION</mixed-citation></ref></ref-list></sec>
    <app-group><app><title>Exclusions</title><p>Five samples were excluded.</p>
    <table-wrap><label>S1</label><table><tr><th colspan="2">Participants</th></tr><tr><td>n</td><td>115</td></tr></table>
    <table-wrap-foot><fn><p>Complete cases only.</p></fn></table-wrap-foot></table-wrap></app></app-group>
    <supplementary-material xlink:href="supp.pdf"><caption><p>Additional methods.</p></caption></supplementary-material>
    </back></article>'''
    c = jats_to_markdown(xml)
    md = c.markdown
    for text in ['We analyzed 120 patients.', 'received: day=2; month=4; year=2020',
                 'rev-recd: month=6; year=2021', 'accepted: iso-8601-date=2021-07-03',
                 'Publication: epub: year=2021', 'Supported by grant ABC.',
                 'Approval preceded recruitment.', 'Available on request.',
                 'Five samples were excluded.', 'Complete cases only.', 'supp.pdf',
                 'Additional methods.', 'colspan="2"']:
        assert text in md
    assert 'OMIT THIS CITATION' not in md
    assert 'References' not in md
    assert md.index('## Abstract') < md.index('## Methods') < md.index('## Back matter')
    assert c.n_tables == 1
    assert c.failures == []


def test_abstract_does_not_make_missing_body_successful():
    c = jats_to_markdown(b'<article><front><article-meta><abstract><p>Summary</p></abstract></article-meta></front></article>')
    assert not c.ok
    assert c.failures
