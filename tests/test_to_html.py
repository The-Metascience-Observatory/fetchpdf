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
    assert "Figures: 1 of 2 embedded" in rendering.html


def test_a_figure_is_found_by_its_file_name_without_a_manifest(tmp_path):
    """JATS names `pone.0000308.g001.jpg`; the served file may be `.png`."""
    xml = record(tmp_path, {"x": ("pone.0000308.g001.png", PNG)}, manifest=False)
    assert to_html.xml_to_html(xml).conversion.n_figures_embedded == 1


def test_tables_keep_their_spans(tmp_path):
    rendering = to_html.xml_to_html(record(tmp_path))
    assert rendering.html.count("<table") == rendering.conversion.n_tables > 0
    assert re.search(r'colspan="[2-9]"', rendering.html)


def test_the_page_loads_nothing_from_the_network(tmp_path):
    xml = record(tmp_path, {"pone.0000308.g001": ("pone.0000308.g001.jpg", PNG),
                            "pone.0000308.g002": ("pone.0000308.g002.jpg", PNG)})
    page = to_html.xml_to_html(xml).html
    sources = re.findall(r'\b(?:src|href)="([^"]*)"', page)
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
