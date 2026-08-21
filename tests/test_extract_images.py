"""Embedded-image extraction: original bitstreams, xref identity, placements.

Fixtures build PDFs with PyMuPDF itself rather than shipping binary fixtures:
the properties under test (shared xrefs, raw-stream passthrough, smask
splitting) are properties of how fitz WRITES PDFs, so building with fitz keeps
the fixture honest and pins the library behavior the extractor relies on.

pytest.importorskip is called inside the tests that need fitz, never at module
level -- the missing-dependency test must run precisely when fitz is absent.
"""

import hashlib
import json
import os

import pytest

from fetchpdf.retrieval import extract_images as xi


def _fitz():
    return pytest.importorskip("fitz")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jpeg_bytes(fitz, width=40, height=30, gray=90):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height))
    pix.clear_with(gray)
    return pix.tobytes("jpeg")


def _two_page_pdf(fitz, path, jpeg):
    """One JPEG xref drawn twice on page 0 and once on page 1.

    keep_proportion=False so the drawn rects equal the insert rects exactly --
    with the default letterboxing the placement rect depends on the image's
    aspect ratio and the assertions would be testing fitz's layout math.
    """
    doc = fitz.open()
    page0 = doc.new_page(width=200, height=200)
    xref = page0.insert_image(fitz.Rect(10, 10, 60, 60), stream=jpeg,
                              keep_proportion=False)
    page0.insert_image(fitz.Rect(100, 100, 150, 150), stream=jpeg,
                       keep_proportion=False)
    page1 = doc.new_page(width=200, height=200)
    page1.insert_image(fitz.Rect(20, 20, 120, 70), xref=xref,
                       keep_proportion=False)
    doc.save(str(path))
    doc.close()
    return xref


INSERT_RECTS = [(10.0, 10.0, 60.0, 60.0), (100.0, 100.0, 150.0, 150.0),
                (20.0, 20.0, 120.0, 70.0)]


@pytest.fixture
def shared_xref_pdf(tmp_path):
    fitz = _fitz()
    jpeg = _jpeg_bytes(fitz)
    path = tmp_path / "10.1234--x.pdf"
    xref = _two_page_pdf(fitz, path, jpeg)
    return path, jpeg, xref


# -- enumeration core -------------------------------------------------------


def test_one_file_per_xref_three_placements(shared_xref_pdf):
    """One xref drawn three times is ONE bitstream with three placements.

    This is the identity property the whole module exists for: the file layer
    must mirror the PDF's object identity, not its page layout.
    """
    path, _jpeg, xref = shared_xref_pdf
    result = xi.extract_images(str(path))

    assert result.status == "ok"
    assert [img.xref for img in result.images] == [xref]
    image = result.images[0]
    assert image.file == "xref{:06d}.jpeg".format(xref)
    files = os.listdir(xi.images_dir_for(str(path)))
    assert files == [image.file]

    assert [p.page for p in image.placements] == [0, 0, 1]
    for placement, expected in zip(image.placements, INSERT_RECTS):
        assert placement.rect == pytest.approx(expected, abs=0.1)
        assert len(placement.matrix) == 6


def test_insert_same_stream_dedupes_to_one_xref(tmp_path):
    """Pins the fixture's own premise: fitz content-digest dedup means
    re-inserting identical bytes reuses the xref. If a PyMuPDF upgrade drops
    that, this fails loudly instead of test_one_file_per_xref failing
    mysteriously."""
    fitz = _fitz()
    jpeg = _jpeg_bytes(fitz)
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    first = page.insert_image(fitz.Rect(10, 10, 60, 60), stream=jpeg)
    second = page.insert_image(fitz.Rect(100, 100, 150, 150), stream=jpeg)
    doc.close()
    assert first == second


def test_raw_stream_hash_is_the_inserted_bytes(shared_xref_pdf):
    """The raw-stream sha256 must equal the sha256 of the JPEG that went in --
    that equality is the byte-identity anchor forensics downstream depends on."""
    path, jpeg, _xref = shared_xref_pdf
    result = xi.extract_images(str(path))

    image = result.images[0]
    assert image.raw_stream_sha256 == _sha256(jpeg)
    assert image.passthrough is True
    assert image.notes == []
    on_disk = open(os.path.join(xi.images_dir_for(str(path)), image.file), "rb").read()
    assert _sha256(on_disk) == image.sha256 == _sha256(jpeg)


def test_flate_image_is_marked_repacked_and_smask_dumped(tmp_path):
    """An RGBA image is stored as raw samples + SMask; extract_image repacks it
    to PNG. The manifest must say so (passthrough False, note set) or a
    downstream byte-identity check would silently compare re-encoded bytes.
    The smask bytes are dumped as their own file: without them the original
    alpha is unrecoverable, and compositing is forbidden (it re-encodes)."""
    fitz = _fitz()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), True)
    pix.clear_with(120)
    png = pix.tobytes("png")
    path = tmp_path / "rgba.pdf"
    doc = fitz.open()
    doc.new_page(width=100, height=100).insert_image(
        fitz.Rect(5, 5, 45, 45), stream=png)
    doc.save(str(path))
    doc.close()

    result = xi.extract_images(str(path))
    assert result.status == "ok"
    by_role = {img.role: img for img in result.images}
    assert set(by_role) == {"image", "smask"}

    image, smask = by_role["image"], by_role["smask"]
    assert image.passthrough is False
    assert "repacked-to-png" in image.notes
    assert image.smask_xref == smask.xref
    assert smask.smask_for == [image.xref]
    assert smask.placements == []
    files = set(os.listdir(xi.images_dir_for(str(path))))
    assert files == {image.file, smask.file}


def test_zero_image_pdf_records_ok_empty(tmp_path):
    fitz = _fitz()
    path = tmp_path / "empty.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()

    result = xi.extract_images(str(path))
    assert result.status == "ok" and result.images == []
    manifest = json.load(open(result.manifest_path))
    assert manifest["n_images"] == 0
    assert not os.path.exists(xi.images_dir_for(str(path)))
    # "nothing there" is a fact worth recording once: the re-run must skip.
    assert xi.extract_images(str(path)).status == "skipped"


def test_encrypted_pdf_is_recorded_not_raised(tmp_path):
    fitz = _fitz()
    doc = fitz.open()
    doc.new_page()
    payload = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                          owner_pw="o", user_pw="u")
    doc.close()
    path = tmp_path / "locked.pdf"
    path.write_bytes(payload)

    result = xi.extract_images(str(path))
    assert result.status == "encrypted"
    assert json.load(open(result.manifest_path))["status"] == "encrypted"
    assert xi.extract_images(str(path)).status == "skipped"


def test_garbage_pdf_is_recorded_not_raised(tmp_path):
    _fitz()
    path = tmp_path / "junk.pdf"
    path.write_bytes(b"this is not a pdf at all")
    result = xi.extract_images(str(path))
    assert result.status in ("corrupt", "error")


def test_missing_pymupdf_reports_and_writes_nothing(tmp_path, monkeypatch):
    """A missing dependency is run state, not a fact about the PDF: no
    manifest may be written, or it would satisfy skip-if-current forever
    after the user installs the extra."""
    monkeypatch.setattr(xi, "_pymupdf", lambda: None)
    path = tmp_path / "any.pdf"
    path.write_bytes(b"%PDF-1.4 irrelevant")

    result = xi.extract_images(str(path))
    assert result.status == "unavailable"
    assert "fetchpdf[images]" in result.detail
    assert os.listdir(tmp_path) == ["any.pdf"]


# -- writer layer -----------------------------------------------------------


def test_manifest_schema(shared_xref_pdf):
    path, jpeg, xref = shared_xref_pdf
    result = xi.extract_images(str(path))
    manifest = json.load(open(result.manifest_path))

    for key in ("schema_version", "generator", "fetchpdf_version",
                "pymupdf_version", "created_at", "source_pdf", "source_size",
                "source_sha256", "status", "page_count", "images_dir",
                "n_images", "n_placements", "images", "errors"):
        assert key in manifest, key
    assert manifest["schema_version"] == 1
    assert manifest["source_pdf"] == os.path.basename(str(path))       # basenames only
    assert manifest["images_dir"] == os.path.basename(xi.images_dir_for(str(path)))
    assert manifest["source_sha256"] == _sha256(open(str(path), "rb").read())
    assert manifest["n_images"] == 1 and manifest["n_placements"] == 3
    entry = manifest["images"][0]
    assert entry["xref"] == xref
    assert entry["raw_stream_sha256"] == _sha256(jpeg)
    assert entry["passthrough"] is True


def test_rerun_is_idempotent_skip(shared_xref_pdf):
    path, _jpeg, _xref = shared_xref_pdf
    first = xi.extract_images(str(path))
    manifest_before = open(first.manifest_path, "rb").read()

    second = xi.extract_images(str(path))
    assert second.status == "skipped"
    assert open(first.manifest_path, "rb").read() == manifest_before


def test_overwrite_is_deterministic(shared_xref_pdf):
    path, _jpeg, _xref = shared_xref_pdf
    first = xi.extract_images(str(path))
    a = json.load(open(first.manifest_path))
    image_file = os.path.join(xi.images_dir_for(str(path)), a["images"][0]["file"])
    bytes_a = open(image_file, "rb").read()

    second = xi.extract_images(str(path), overwrite=True)
    assert second.status == "ok"
    b = json.load(open(second.manifest_path))
    a.pop("created_at"), b.pop("created_at")
    assert a == b
    assert open(image_file, "rb").read() == bytes_a


def test_changed_pdf_invalidates_manifest_and_cleans_stale_files(shared_xref_pdf):
    """An upgraded PDF renumbers xrefs; orphaned bitstreams beside a fresh
    manifest would poison downstream byte-identity pooling."""
    path, _jpeg, _xref = shared_xref_pdf
    fitz = _fitz()
    first = xi.extract_images(str(path))
    stale = os.path.join(xi.images_dir_for(str(path)), first.images[0].file)
    assert os.path.exists(stale)

    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))            # same record path, now zero images
    doc.close()

    result = xi.extract_images(str(path))
    assert result.status == "ok" and result.images == []
    assert not os.path.exists(stale)
    assert json.load(open(result.manifest_path))["n_images"] == 0


# -- directory mode + CLI ---------------------------------------------------


def test_directory_mode_skips_si_and_finds_nested_records(tmp_path):
    fitz = _fitz()
    jpeg = _jpeg_bytes(fitz)
    _two_page_pdf(fitz, tmp_path / "10.1234--x.pdf", jpeg)
    _two_page_pdf(fitz, tmp_path / "10.1234--x_supplementary_info_1_extra.pdf", jpeg)
    nested = tmp_path / "10.5678--y"
    nested.mkdir()
    _two_page_pdf(fitz, nested / "10.5678--y.pdf", jpeg)

    summary = xi.extract_directory(str(tmp_path))
    assert summary["extracted"] == 2          # nested record found (recursion)
    assert summary["supplementary_skipped"] == 1
    assert summary["failed"] == 0

    # Second pass walks past the _images output dirs and skips both records.
    summary = xi.extract_directory(str(tmp_path))
    assert summary["extracted"] == 0 and summary["skipped"] == 2

    summary = xi.extract_directory(str(tmp_path), include_supplementary=True)
    assert summary["extracted"] == 1          # the SI PDF, this time


def test_batch_already_exists_branch_runs_the_pass(tmp_path):
    """--extract-images must fire on the already-have-this-PDF skip branch:
    "I already fetched 5,000 PDFs, now dump their images" is the primary use
    case, and the --to-markdown hook misses that branch entirely."""
    fitz = _fitz()
    _two_page_pdf(fitz, tmp_path / "10.1234--x.pdf", _jpeg_bytes(fitz))

    import importlib
    fpd = importlib.import_module("fetchpdf.fetchpdf")
    fpd.batch_fetch_pdfs(["10.1234/x"], str(tmp_path), extract_images=True,
                         create_missing_report=False)
    assert os.path.exists(str(tmp_path / "10.1234--x_images.json"))
    assert os.listdir(str(tmp_path / "10.1234--x_images"))


def test_main_console_script(tmp_path, monkeypatch, capsys):
    fitz = _fitz()
    _two_page_pdf(fitz, tmp_path / "10.1234--x.pdf", _jpeg_bytes(fitz))
    assert xi.main([str(tmp_path)]) == 0
    assert "extracted" in capsys.readouterr().out

    monkeypatch.setattr(xi, "_pymupdf", lambda: None)
    assert xi.main([str(tmp_path)]) == 1
    assert "fetchpdf[images]" in capsys.readouterr().out
