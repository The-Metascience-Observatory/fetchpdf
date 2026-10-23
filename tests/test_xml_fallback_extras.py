"""An XML-only result gets its own folder, and a re-run finds it there."""
import os

from fetchpdf import fetchpdf as fpd
from fetchpdf.retrieval.to_markdown import jats_to_markdown


def test_an_xml_fallback_moves_into_its_own_folder_with_its_siblings(tmp_path):
    stem = "10.1126--x.1"
    for suffix in (".xml", ".provenance.json"):
        (tmp_path / (stem + suffix)).write_text("x")
    other = tmp_path / "10.1126--x.10.xml"      # another record sharing the prefix
    other.write_text("y")
    save_path = str(tmp_path / f"{stem}.pdf")

    result, new_save = fpd._move_xml_fallback_to_own_folder(
        str(tmp_path / f"{stem}.xml"), save_path)

    folder = tmp_path / stem
    assert new_save == str(folder / f"{stem}.pdf")
    assert result == str(folder / f"{stem}.xml") and os.path.exists(result)
    assert (folder / f"{stem}.provenance.json").exists()
    assert not (tmp_path / f"{stem}.xml").exists()
    assert other.exists()


def test_a_record_already_in_its_folder_is_left_alone(tmp_path):
    folder = tmp_path / "10.1126--x.1"
    folder.mkdir()
    (folder / "10.1126--x.1.xml").write_text("x")
    save_path = str(folder / "10.1126--x.1.pdf")
    xml = str(folder / "10.1126--x.1.xml")
    assert fpd._move_xml_fallback_to_own_folder(xml, save_path) == (xml, save_path)


def test_a_flat_rerun_finds_the_filed_xml(tmp_path):
    flat = str(tmp_path / "10.1126--x.1.pdf")
    assert fpd._xml_fallback_save_path(flat) == flat
    (tmp_path / "10.1126--x.1").mkdir()
    (tmp_path / "10.1126--x.1" / "10.1126--x.1.xml").write_text("x")
    assert fpd._xml_fallback_save_path(flat) == str(tmp_path / "10.1126--x.1" / "10.1126--x.1.pdf")


def test_only_an_untiered_xml_result_is_a_fallback():
    assert fpd._is_xml_fallback("a/b.xml", tiered=False)
    assert not fpd._is_xml_fallback("a/b.xml", tiered=True)
    assert not fpd._is_xml_fallback("a/b.pdf", tiered=False)
    assert not fpd._is_xml_fallback(None, tiered=False)


def test_figures_in_a_jats_floats_group_are_rendered():
    xml = b"""<?xml version="1.0"?><article xmlns:xlink="http://www.w3.org/1999/xlink">
    <front><article-meta><title-group><article-title>T</article-title></title-group>
    </article-meta></front><body><sec><title>Results</title>
    <p>See <xref ref-type="fig" rid="F1">Fig. 1</xref>. """ + b"word " * 60 + b"""</p>
    </sec></body><back></back><floats-group><fig id="F1"><label>Fig. 1.</label>
    <caption><p>Neutrophils.</p></caption><graphic xlink:href="f0001.jpg"/></fig>
    </floats-group></article>"""
    conversion = jats_to_markdown(xml, "a.xml")
    assert conversion.n_figures == 1
    assert "Neutrophils" in conversion.markdown
