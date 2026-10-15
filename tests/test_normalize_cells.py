"""Cell text in the canonical table form: what separates, and what does not.

Offline. Each table is the smallest markup that reproduced a fault seen in a
real record.
"""

from lxml import etree

from fetchpdf.retrieval.normalize import html_table_to_canonical, jats_table_to_canonical

CE = "http://www.elsevier.com/xml/common/dtd"


def _jats(xml: str) -> str:
    return jats_table_to_canonical(etree.fromstring(xml))


def test_jats_break_separates_header_lines():
    """10.1007/s11064-023-03861-8 (Europe PMC JATS), Table 1: three header lines
    rendered as "Physiological VariablesPain TestingMorphological Analyses"."""
    html = _jats(
        "<table><thead><tr>"
        "<th>Physiological Variables<break/>Pain Testing<break/>Morphological Analyses</th>"
        "<th>MABP<break/>Torr</th>"
        "</tr></thead></table>"
    )
    assert "<th>Physiological Variables Pain Testing Morphological Analyses</th>" in html
    assert "<th>MABP Torr</th>" in html
    assert "<br" not in html  # canonical form is <table>/<tr>/<th>/<td> only


def test_inline_markup_adds_no_space():
    """Subscripts, italics and footnote markers are part of the token they sit in."""
    html = _jats("<table><tr><td>FEV<sub>1</sub>/FVC</td><td><italic>p</italic>&lt;0.05<sup>a</sup></td></tr></table>")
    assert "<td>FEV1/FVC</td>" in html
    assert "<td>p&lt;0.05[a]</td>" in html


def test_block_content_is_separated_and_kept_in_order():
    """Two <p> in one cell: text on either side ran together, and the old
    iterator emitted the first <p>'s trailing newline before its <sub>."""
    html = _jats(
        "<table><tr><td>\n<p>FVC &lt; -1.64 &amp;</p>\n<p>FEV<sub>1</sub>/FVC</p>\n</td>"
        "<td><list><list-item><label>-</label><p>Mild</p></list-item>"
        "<list-item><label>-</label><p>Severe</p></list-item></list></td></tr></table>"
    )
    assert "<td>FVC &lt; -1.64 &amp; FEV1/FVC</td>" in html
    assert "<td>- Mild - Severe</td>" in html


def test_superscript_holding_a_citation_stays_where_it_is():
    """<sup><xref/></sup> mid-cell: the marker used to land at the end of the cell."""
    html = _jats('<table><tr><td>FHS<sup><xref ref-type="bibr">*</xref></sup> (1948) N=7699</td></tr></table>')
    assert "<td>FHS* (1948) N=7699</td>" in html


def test_processing_instructions_do_not_leak_into_cells():
    html = _jats('<table><tr><td>R+L<?CDN img.jpg NEVER_LOAD?></td></tr></table>')
    assert "<td>R+L</td>" in html


def test_elsevier_ce_br_and_hsp_in_cals_entries():
    """Elsevier ce:entry cells (CALS path): ce:br is a line break, ce:hsp a space."""
    xml = (
        '<ce:table xmlns:ce="{0}"><tgroup cols="2">'
        '<colspec colname="col1"/><colspec colname="col2"/>'
        "<thead><row><ce:entry>Pre intervention<ce:br/>(n = 67)</ce:entry>"
        "<ce:entry>Dose<ce:br/>mg/kg</ce:entry></row></thead>"
        "<tbody><row><ce:entry>337.3<ce:hsp sp=\"0.25\"/>±<ce:hsp sp=\"0.25\"/>20.6</ce:entry>"
        "<ce:entry><ce:para>a</ce:para><ce:para>b</ce:para></ce:entry></row></tbody>"
        "</tgroup></ce:table>"
    ).format(CE)
    html = _jats(xml)
    assert "<th>Pre intervention (n = 67)</th>" in html
    assert "<th>Dose mg/kg</th>" in html
    assert "<td>337.3 ± 20.6</td>" in html
    assert "<td>a b</td>" in html


def test_publisher_html_br_separates():
    table = etree.fromstring(
        "<table><tr><th>Unvaccinated<br/>(n = 38)</th>"
        "<td><p>one</p><p>two</p></td><td>4.2<sup>a</sup></td></tr></table>"
    )
    html = html_table_to_canonical(table)
    assert "<th>Unvaccinated (n = 38)</th>" in html
    assert "<td>one two</td>" in html
    assert "<td>4.2a</td>" in html  # publisher HTML keeps its plain-text flattening
