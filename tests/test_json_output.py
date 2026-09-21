"""Offline tests for `--json`: what lands on stdout, and what must not.

The contract these defend is narrow and worth naming: under the flag, stdout
holds one JSON object per record and nothing else, so a caller can `json.loads`
every line it reads without looking at any of them first. Every test below
therefore parses the WHOLE of stdout, not the line it cares about.

No network: the chain is replaced with a fake that writes the bytes a real
route would have written and records what a real route would have recorded.
"""

import json
import os

import pytest

from fetchpdf import fetchpdf as fp


def _fake_chain(doi, save_path, *args, **kwargs):
    """Stand-in for `fetch_pdf`: writes a PDF and names the source.

    The skip-if-exists branch is reproduced rather than skipped over, because
    the label it sets is what the record under test reads: the real chain
    returns the path it found and calls `_record_source(_source_out,
    "existing")` without fetching or verifying anything.
    """
    if os.path.exists(save_path):
        fp._record_source(kwargs.get("_source_out"), "existing")
        return save_path
    with open(save_path, "wb") as f:
        f.write(b"%PDF-1.4 stand-in")
    fp._note_identity(save_path, "verified", "")
    fp._record_source(kwargs.get("_source_out"), "unpaywall")
    return save_path


def _refusing_chain(doi, save_path, *args, **kwargs):
    """Stand-in for a PDF fetched, judged the wrong article and deleted."""
    fp._note_identity(save_path, "wrong_article",
                      "PDF does not contain the requested DOI or the title.")
    return None


@pytest.fixture
def cli(monkeypatch):
    """`main(argv)` with identifier resolution and the chain stubbed out."""
    monkeypatch.setattr(fp, "resolve_identifier_to_doi",
                        lambda identifier, **kw: str(identifier).strip())

    def run(argv, chain=_fake_chain):
        monkeypatch.setattr(fp, "fetch_pdf", chain)
        return fp.main(argv)

    return run


def _stdout_records(capsys):
    """Every line of stdout, parsed. Fails loudly on the first that is not JSON."""
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line]


#: The documented keys, in the documented order.
FIELDS = ["identifier", "doi", "success", "status", "path", "format",
          "source", "identity", "reasons", "paths"]


def test_success_object_names_the_path_the_source_and_the_verdict(cli, capsys, tmp_path):
    code = cli(["10.1234/one", "-o", str(tmp_path), "--json"])
    (record,) = _stdout_records(capsys)

    assert code == 0
    assert list(record) == FIELDS
    assert record["identifier"] == "10.1234/one"
    assert record["doi"] == "10.1234/one"
    assert record["success"] is True
    assert record["status"] == "downloaded"
    assert record["path"] == str(tmp_path / "10.1234--one.pdf")
    assert record["format"] == "pdf"
    assert record["source"] == "unpaywall"
    assert record["identity"] == "verified"
    assert record["reasons"] == []


def test_a_file_already_on_disk_is_not_reported_as_a_download(cli, capsys, tmp_path):
    """The distinction the flag exists for.

    Nothing is fetched and nothing re-reads the file on this branch, so the
    honest answers are `already_on_disk` and an identity of null -- where the
    prose line says "Successfully downloaded to ..." either way.
    """
    (tmp_path / "10.1234--one.pdf").write_bytes(b"%PDF-1.4 from an earlier run")

    code = cli(["10.1234/one", "-o", str(tmp_path), "--json"])
    (record,) = _stdout_records(capsys)

    assert code == 0
    assert record["success"] is True
    assert record["status"] == "already_on_disk"
    assert record["identity"] is None
    assert record["path"] == str(tmp_path / "10.1234--one.pdf")


def test_a_refused_pdf_reports_the_refusal_not_a_bare_failure(cli, capsys, tmp_path):
    code = cli(["10.1234/one", "-o", str(tmp_path), "--json"],
               chain=_refusing_chain)
    (record,) = _stdout_records(capsys)

    assert code == 1
    assert record["success"] is False
    assert record["status"] == "failed"
    assert record["path"] is None
    assert record["identity"] == "wrong_article"
    assert record["reasons"] == [
        "PDF does not contain the requested DOI or the title."]


def test_structured_full_text_after_a_refusal_is_not_reported_as_refused(
        cli, capsys, tmp_path):
    """The chain's own behaviour when a PDF is refused: it keeps going.

    `fetch_pdf` falls through to structured full text, so the record succeeds
    as XML. The refusal belongs to a file that no longer exists and must not be
    hung on the artifact that did arrive.
    """
    def _refuse_then_fall_back(doi, save_path, *args, **kwargs):
        fp._note_identity(save_path, "wrong_article",
                          "PDF does not contain the requested DOI or the title.")
        xml_path = os.path.splitext(save_path)[0] + ".xml"
        with open(xml_path, "wb") as f:
            f.write(b"<article/>")
        fp._record_source(kwargs.get("_source_out"), "structured_fallback")
        return xml_path

    code = cli(["10.1234/one", "-o", str(tmp_path), "--json"],
               chain=_refuse_then_fall_back)
    (record,) = _stdout_records(capsys)

    assert code == 0
    assert record["format"] == "xml"
    assert record["source"] == "structured_fallback"
    assert record["identity"] is None
    assert record["reasons"] == []


def test_a_batch_writes_one_parseable_line_per_row_and_nothing_else(
        cli, capsys, tmp_path):
    """Including the progress lines, tallies and summary -- all on stderr."""
    csv = tmp_path / "records.csv"
    csv.write_text("DOI\n10.1234/one\n10.1234/two\n", encoding="utf-8")
    out_dir = tmp_path / "pdfs"

    code = cli([str(csv), "-o", str(out_dir), "--json", "--no-missing-report"])
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line]

    assert code == 0
    assert [r["identifier"] for r in records] == ["10.1234/one", "10.1234/two"]
    assert all(r["status"] == "downloaded" for r in records)
    # The prose went somewhere, and that somewhere is not stdout.
    assert "Processing 2 identifiers" in captured.err
    assert "📚" not in captured.out


def test_without_the_flag_stdout_is_what_it_has_always_been(cli, capsys, tmp_path):
    code = cli(["10.1234/one", "-o", str(tmp_path)])
    out = capsys.readouterr().out
    pdf = tmp_path / "10.1234--one.pdf"

    assert code == 0
    assert out == (f"💾 No output path provided; using: {pdf}\n"
                   f"\n✅ Successfully downloaded to {pdf}\n")
