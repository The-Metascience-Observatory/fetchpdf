"""Offline tests for rendering a retrieved XML to HTML and PDF (to_html.py).

The load-bearing tests here:

  test_the_markdown_route_is_unchanged        -- images are opt-in; --to-markdown
                                                 output must not move
  test_a_present_figure_is_embedded_and_a_missing_one_still_looks_missing
  test_the_page_loads_nothing_from_the_network
  test_the_rendering_is_never_written_as_the_publishers_pdf
"""

import io
import json
import os
import re
import shutil

import pytest

from fetchpdf.retrieval import to_html
from fetchpdf.retrieval.to_markdown import jats_to_markdown

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "epmc_fulltext_valid.xml")

#: A 1x1 PNG, so a data URI can be checked for its type without a real figure.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
       b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

pytest.importorskip("markdown_it")


def record(tmp_path, figures=None, manifest=True):
    """`rec.xml` from the fixture, plus `rec_figures/` holding `figures`."""
    xml = tmp_path / "rec.xml"
    shutil.copy(FIXTURE, xml)
    if figures:
        directory = tmp_path / "rec_figures"
        directory.mkdir()
        rows = []
        for href, (name, content) in figures.items():
            (directory / name).write_bytes(content)
            rows.append({"href": href, "file": name, "status": "ok"})
        if manifest:
            (tmp_path / "rec_figures.json").write_text(json.dumps({"figures": rows}))
    return str(xml)


def test_the_markdown_route_is_unchanged():
    with open(FIXTURE, "rb") as f:
        content = f.read()
    plain = jats_to_markdown(content)
    assert plain.n_figures_embedded == 0
    assert "<figure" not in plain.markdown
    assert "image not included" in plain.markdown
    assert jats_to_markdown(content, images=None).markdown == plain.markdown


def test_a_present_figure_is_embedded_and_a_missing_one_still_looks_missing(tmp_path):
    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.jpg", PNG)})

    rendering = to_html.xml_to_html(xml)

    assert rendering.conversion.n_figures_embedded == 1
    assert rendering.conversion.n_figures == 2
    assert rendering.html.count("data:image/png;base64,") == 1
    assert "<figcaption>" in rendering.html
    assert "image not included" in rendering.html          # Figure 2's placeholder
    assert "1 of 2 figures could not be embedded" in rendering.html


def test_a_figure_is_found_by_its_file_name_without_a_manifest(tmp_path):
    """JATS names `pone.0000308.g001.jpg`; the served file may be `.png`."""
    xml = record(tmp_path, {"x": ("pone.0000308.g001.png", PNG)}, manifest=False)
    assert to_html.xml_to_html(xml).conversion.n_figures_embedded == 1


def test_a_figure_inside_a_paragraph_is_a_figure_not_prose(tmp_path):
    """PMC's JATS for Elsevier articles nests each <fig> in the <p> citing it."""
    xml = tmp_path / "rec.xml"
    xml.write_text(
        '<article xmlns:xlink="http://www.w3.org/1999/xlink"><body><sec><title>Case</title>'
        '<p>Imaging showed hemorrhage (<xref rid="f1">Fig. 1</xref>).'
        '<fig id="f1"><label>Fig. 1</label><caption><p>CT scan.</p></caption>'
        '<alt-text>dummy alt text</alt-text><graphic xlink:href="gr1.webp"/></fig>'
        ' It resolved.</p></sec></body></article>')
    (tmp_path / "rec_figures").mkdir()
    (tmp_path / "rec_figures" / "gr1.png").write_bytes(PNG)

    rendering = to_html.xml_to_html(str(xml))

    assert rendering.conversion.n_figures == rendering.conversion.n_figures_embedded == 1
    assert "Imaging showed hemorrhage (Fig. 1). It resolved." in rendering.conversion.markdown
    assert "dummy alt text" not in rendering.html


def test_the_page_ends_with_the_reference_list_the_markdown_leaves_out(tmp_path):
    rendering = to_html.xml_to_html(record(tmp_path))

    assert len(rendering.references) == 37
    assert rendering.html.count('<li id="pone.0000308-') == 37
    assert "No reference list" not in rendering.html
    assert "doi.org/10.1001/jama.293.19.2362" in rendering.html
    assert rendering.html.index("<h2>References</h2>") > rendering.html.index("</table>")
    with open(FIXTURE, "rb") as f:
        assert "jama.293.19.2362" not in jats_to_markdown(f.read()).markdown


def test_references_from_both_vocabularies():
    from fetchpdf.retrieval.references import references_from_xml

    jats = references_from_xml(
        b'<article><back><ref-list><ref id="b1"><label>1</label>'
        b'<element-citation><person-group person-group-type="author">'
        b'<name><surname>Krings</surname><given-names>T.</given-names></name></person-group>'
        b'<article-title>Embryology</article-title><source>J Surg</source><year>2022</year>'
        b'<volume>14</volume><issue>9</issue><fpage>910</fpage><lpage>919</lpage>'
        b'<pub-id pub-id-type="doi">10.1136/x</pub-id></element-citation>'
        b'<mixed-citation><surname>Krings</surname><source>J Surg</source></mixed-citation></ref>'
        b'<ref id="b2"><mixed-citation>Smith J. <source>Typeset</source>. 2001.</mixed-citation>'
        b'</ref></ref-list></back></article>')
    assert [r.label for r in jats] == ["1", ""]
    assert jats[0].html.startswith("Krings T. Embryology. <i>J Surg.</i> 2022;14(9):910–919. doi:")
    assert jats[1].html == "Smith J. Typeset. 2001."

    ce, sb = "http://www.elsevier.com/xml/common/dtd", "http://www.elsevier.com/xml/common/struct-bib/dtd"
    elsevier = references_from_xml(f"""<doc xmlns:ce="{ce}" xmlns:sb="{sb}"><ce:bibliography>
        <ce:bibliography-sec>
        <ce:bib-reference id="br1"><ce:label>[1]</ce:label><sb:reference>
          <sb:contribution><sb:authors><sb:author><ce:given-name>D.</ce:given-name>
          <ce:surname>Alger</ce:surname></sb:author><sb:et-al/></sb:authors>
          <sb:title><sb:maintitle>Lab tests</sb:maintitle></sb:title></sb:contribution>
          <sb:host><sb:issue><sb:series><sb:title><sb:maintitle>Rev. Econ. Stud.</sb:maintitle>
          </sb:title><sb:volume-nr>54</sb:volume-nr></sb:series><sb:date>1987</sb:date></sb:issue>
          <sb:pages><sb:first-page>105</sb:first-page><sb:last-page>145</sb:last-page></sb:pages>
          </sb:host></sb:reference></ce:bib-reference>
        <ce:bib-reference id="br2"><ce:label>[2]</ce:label><ce:other-ref>
          <ce:textref>B. Greiner, ORSEE (2004).</ce:textref></ce:other-ref></ce:bib-reference>
        </ce:bibliography-sec></ce:bibliography></doc>""".encode())
    assert elsevier[0].html == "Alger D., et al. Lab tests. <i>Rev. Econ. Stud.</i> 1987;54:105–145."
    assert elsevier[1].html == "B. Greiner, ORSEE (2004)."


def test_a_figure_is_sized_by_its_shape_not_stretched_to_the_column():
    """A square figure at full column width filled a whole A4 page."""
    square, strip, tall = (to_html.figure_width_cm(1600, 1600),
                           to_html.figure_width_cm(3000, 1000),
                           to_html.figure_width_cm(1000, 3000))
    assert 10 < square < 11
    assert strip > 18                                   # left to max-width: 100%
    assert tall * 3 <= to_html.FIGURE_MAX_HEIGHT_CM + 0.01
    assert to_html.figure_width_cm(100, 100) < 2        # never blown up past 150 dpi


def test_the_page_gives_each_image_a_width(tmp_path):
    # Only the header is read for the size: an 800x600 IHDR on the 1x1 body.
    header = PNG[:16] + (800).to_bytes(4, "big") + (600).to_bytes(4, "big") + PNG[24:]
    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.png", header)})
    page = to_html.xml_to_html(xml).html
    width = to_html.figure_width_cm(800, 600)
    assert f'<img style="width: {width:.1f}cm" src="data:image/png' in page
    assert to_html.image_size(PNG) == (1, 1)


def test_the_banner_names_the_version_and_only_what_is_missing(tmp_path):
    from fetchpdf import __version__

    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.jpg", PNG),
                            "pone.0000308.g002": ("pone.0000308.g002.jpg", PNG)})
    banner = to_html.xml_to_html(xml).html.split("</aside>")[0]
    assert f"by fetchpdf {__version__} on" in banner
    assert "<li>" not in banner and "Figures" not in banner and "Tables" not in banner

    (tmp_path / "bare.xml").write_text("<article><body><p>Text.</p></body></article>")
    banner = to_html.xml_to_html(str(tmp_path / "bare.xml")).html.split("</aside>")[0]
    assert "No reference list could be read from the XML." in banner


def test_the_title_is_followed_by_authors_affiliations_and_journal(tmp_path):
    page = to_html.xml_to_html(record(tmp_path)).html
    head = page[page.index("</h1>"):page.index("</h1>") + 1500]
    assert "Heather A. Piwowar<sup>*</sup>, Roger S. Day, Douglas B. Fridsma" in head
    assert "University of Pittsburgh School of Medicine" in head
    assert "Ioannina" not in head                       # the academic editor's affiliation
    assert "<i>PLoS ONE.</i> 2007;2(3):e308." in head


def test_an_elsevier_byline_uses_the_affiliation_labels():
    from fetchpdf.retrieval.byline import byline_from_xml

    ce = "http://www.elsevier.com/xml/common/dtd"
    prism = "http://prismstandard.org/namespaces/basic/2.0/"
    byline = byline_from_xml(f"""<full-text-retrieval-response xmlns:ce="{ce}" xmlns:prism="{prism}">
        <coredata><prism:publicationName>Biol Psychiatry</prism:publicationName>
        <prism:volume>33</prism:volume><prism:issueIdentifier>4</prism:issueIdentifier>
        <prism:startingPage>295</prism:startingPage><prism:endingPage>297</prism:endingPage>
        <prism:coverDate>1993-02-15</prism:coverDate></coredata>
        <ce:author-group><ce:author><ce:given-name>Yue-Joe</ce:given-name><ce:surname>Lee</ce:surname>
        <ce:cross-ref refid="AFF1"><ce:sup>1</ce:sup></ce:cross-ref></ce:author>
        <ce:affiliation id="AFF1"><ce:label>a</ce:label><ce:textfn>Taipei</ce:textfn></ce:affiliation>
        </ce:author-group></full-text-retrieval-response>""".encode())
    assert byline.authors == [("Yue-Joe Lee", ["a"])]
    assert byline.affiliations == [("a", "Taipei")]
    assert byline.journal == "<i>Biol Psychiatry.</i> 1993;33(4):295–297."


def test_a_wide_table_is_marked_to_print_smaller_and_fit_the_page():
    narrow = "<table><tr><td>a</td><td>b</td></tr></table>"
    wide = "<table><tr><th colspan=\"6\">x</th><td>1</td><td>2</td><td>3</td></tr></table>"
    assert to_html.mark_wide_tables(narrow) == narrow
    assert to_html.mark_wide_tables(wide).startswith('<table class="wide">')
    assert "overflow-wrap: anywhere" in to_html._CSS


def test_tables_keep_their_spans(tmp_path):
    rendering = to_html.xml_to_html(record(tmp_path))
    assert rendering.html.count("<table") == rendering.conversion.n_tables > 0
    assert re.search(r'colspan="[2-9]"', rendering.html)


def test_the_page_loads_nothing_from_the_network(tmp_path):
    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.jpg", PNG),
                            "pone.0000308.g002": ("pone.0000308.g002.jpg", PNG)})
    page = to_html.xml_to_html(xml).html
    # Only `src` is fetched; an `href` (a reference's DOI) is a link a reader may follow.
    sources = re.findall(r'\bsrc="([^"]*)"', page)
    assert sources and all(s.startswith("data:") for s in sources)
    assert "<link" not in page and "<script" not in page


def test_the_page_says_what_it_is(tmp_path):
    page = to_html.xml_to_html(record(tmp_path)).html
    assert "not the publisher's typeset version" in page
    assert "DOI: 10.1371/journal.pone.0000308" in page
    assert page.startswith("<!doctype html>")


def test_the_rendering_is_never_written_as_the_publishers_pdf(tmp_path):
    xml = record(tmp_path)
    assert to_html.html_path_for(xml) == str(tmp_path / "rec_from_xml.html")
    assert to_html.pdf_path_for(xml) == str(tmp_path / "rec_from_xml.pdf")

    result = to_html.render_record(xml, html=True)

    assert result["html"] == str(tmp_path / "rec_from_xml.html")
    assert not (tmp_path / "rec.pdf").exists()


def test_an_existing_rendering_is_kept_unless_overwrite(tmp_path):
    xml = record(tmp_path)
    out = tmp_path / "rec_from_xml.html"
    out.write_text("mine")
    assert to_html.render_record(xml)["html"] == str(out)
    assert out.read_text() == "mine"
    to_html.render_record(xml, overwrite=True)
    assert out.read_text().startswith("<!doctype html>")


def test_an_unrenderable_xml_is_one_bad_record_not_an_exception(tmp_path):
    xml = tmp_path / "rec.xml"
    xml.write_bytes(b"<article><not-closed>")
    result = to_html.render_record(str(xml))
    assert result["html"] is None and result["error"]


def test_only_article_xml_is_picked_up_from_a_directory(tmp_path):
    record(tmp_path)
    (tmp_path / "rec_supplementary_info_1.xml").write_bytes(b"<dataset><row/></dataset>")
    assert to_html._xml_paths([str(tmp_path)]) == [str(tmp_path / "rec.xml")]


def test_an_image_is_typed_by_its_bytes_not_its_name(tmp_path):
    path = tmp_path / "figure.jpg"
    path.write_bytes(PNG)
    assert to_html.data_uri(str(path)).startswith("data:image/png;base64,")


def test_a_tiff_is_converted_because_no_browser_shows_one(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "white").save(buffer, "TIFF")
    path = tmp_path / "figure.tif"
    path.write_bytes(buffer.getvalue())
    assert to_html.data_uri(str(path)).startswith("data:image/png;base64,")


def test_an_elsevier_locator_finds_its_file_through_the_manifest(tmp_path):
    from test_figures import ELS_XML

    xml = tmp_path / "rec.xml"
    xml.write_bytes(ELS_XML)
    directory = tmp_path / "rec_figures"
    directory.mkdir()
    (directory / "1-s2.0-S0048969719320820-gr1.jpg").write_bytes(PNG)
    (tmp_path / "rec_figures.json").write_text(json.dumps({"figures": [
        {"href": "gr1", "file": "1-s2.0-S0048969719320820-gr1.jpg"}]}))

    rendering = to_html.xml_to_html(str(xml))

    assert rendering.conversion.n_figures_embedded == 1
    assert "Elsevier full-text XML" in rendering.html
    assert rendering.doi == "10.1016/j.scitotenv.2019.05.070"


def test_the_pdf_is_printed_from_the_page(tmp_path):
    pytest.importorskip("playwright.sync_api")
    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.jpg", PNG)})
    try:
        result = to_html.render_record(xml, html=False, pdf=True)
    except Exception as e:                      # pragma: no cover - environment
        pytest.skip(f"no browser: {e}")
    if result["error"] and "Executable" in result["error"]:
        pytest.skip(result["error"])
    assert result["pdf"] == str(tmp_path / "rec_from_xml.pdf")
    with open(result["pdf"], "rb") as f:
        assert f.read(4) == b"%PDF"
    assert not (tmp_path / "rec_from_xml.html").exists()


def test_the_batch_renders_a_record_already_on_disk_with_its_figures_pass(tmp_path, monkeypatch):
    """The flag implies --pull-figures, and that pass needs the batch's
    resolver: before it was built for --pull-figures, every record's figure
    pass failed on `None.http` and the rendering came out without images."""
    import fetchpdf.fetchpdf as fpd
    from fetchpdf.retrieval import figures, resolve

    shutil.copy(FIXTURE, tmp_path / "10.1234--x.xml")
    (tmp_path / "10.1234--x.pdf").write_bytes(b"%PDF-1.4 already here")

    class OfflineResolver:
        def __init__(self, http, cache, *args):
            self.http = http
            self.cache = cache

        def prime(self, identifiers):
            pass

    calls = []

    def pull(**kwargs):
        calls.append(kwargs)
        return figures.FiguresSummary(status="none_found")

    monkeypatch.setattr(resolve, "BatchResolver", OfflineResolver)
    monkeypatch.setattr(figures, "pull_for_record", pull)

    fpd.batch_fetch_pdfs(["10.1234/x"], str(tmp_path), generate_html_from_xml=True,
                         create_missing_report=False)

    assert calls and calls[0]["resolver"] is not None and calls[0]["http"] is not None
    assert (tmp_path / "10.1234--x_from_xml.html").exists()
    assert not (tmp_path / "10.1234--x_from_xml.pdf").exists()
