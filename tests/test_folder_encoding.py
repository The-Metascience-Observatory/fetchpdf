"""Tests for the safe-DOI filename inverse + legacy-folder migration.

Covers the code paths introduced to handle corpora with a mix of modern
('~' for ':') and legacy ('--' for both '/' and ':') folder-name encodings:

  * ``safe_filename_to_doi_variants``  -- the inverse decoder that returns
    both candidate DOIs when a name is ambiguous under legacy encoding.
  * ``dois_from_dir``                   -- CLI helper for --input-from-dirs.
  * ``migrate_legacy_folders``          -- CLI helper for --migrate-folders.

Network calls to Crossref are monkeypatched so the tests are fast and hermetic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fetchpdf.fetchpdf import (
    doi_to_safe_filename,
    dois_from_dir,
    migrate_legacy_folders,
    safe_filename_to_doi_variants,
)


# ---------------------------------------------------------------------------
# safe_filename_to_doi_variants: modern reading, legacy reading, edge cases
# ---------------------------------------------------------------------------

def test_modern_single_segment_roundtrips():
    """The typical case: '10.1007--s11064-015-1745-4' encodes one segment."""
    variants = safe_filename_to_doi_variants("10.1007--s11064-015-1745-4")
    assert variants == ["10.1007/s11064-015-1745-4"]


def test_modern_multi_segment_stays_slash():
    """Legitimate multi-segment DOI (OUP style) must decode with slashes, not
    colons -- and must NOT be treated as ambiguous. Otherwise every OUP DOI
    would trigger a legacy-migration probe."""
    variants = safe_filename_to_doi_variants("10.1093--abm--kaad072")
    # Both readings are candidates -- disambiguation is Crossref's job -- but
    # the modern (slash) reading MUST come first so it wins when the network
    # is unreachable.
    assert variants[0] == "10.1093/abm/kaad072"
    assert "10.1093/abm:kaad072" in variants


def test_modern_colon_encoding_via_tilde():
    """A modern-encoded colon-DOI ('~' escape) decodes unambiguously."""
    variants = safe_filename_to_doi_variants("10.1023--a~1016394031947")
    # Case is preserved verbatim from the safe name (DOIs are case-insensitive
    # per the DOI spec, so 'a' vs 'A' resolves the same).
    assert variants == ["10.1023/a:1016394031947"]


def test_legacy_colon_encoding_via_double_dash():
    """The bug we hit in the field: '10.1023--a--1016394031947' is legacy
    encoding for '10.1023/A:1016394031947'. The decoder MUST offer both the
    modern (bogus) and legacy (correct) readings so the caller can pick."""
    variants = safe_filename_to_doi_variants("10.1023--a--1016394031947")
    # Modern reading first (would be tried and fail cleanly at Crossref)
    assert variants[0] == "10.1023/a/1016394031947"
    # Legacy reading is included so a Crossref-informed caller finds the real DOI
    assert "10.1023/a:1016394031947" in variants
    assert len(variants) == 2


def test_hyphen_escape_survives_roundtrip():
    """A DOI with a literal '--' (e.g. ASEE) is encoded with '~2d~' escapes,
    so the safe name does NOT contain a bare '--'. Decoding restores the
    hyphens without triggering the legacy branch."""
    doi = "10.18260/1-2--47556"
    safe = doi_to_safe_filename(doi)
    assert "--" not in safe.split("--", 1)[1]  # only the prefix separator
    variants = safe_filename_to_doi_variants(safe)
    assert variants == [doi]


def test_non_safe_input_passes_through():
    """Something that's already a DOI, or gibberish, is returned as-is so
    the caller can hand any string to this function without a special case."""
    assert safe_filename_to_doi_variants("10.1000/xyz") == ["10.1000/xyz"]
    assert safe_filename_to_doi_variants("") == []
    assert safe_filename_to_doi_variants("panels") == ["panels"]


# ---------------------------------------------------------------------------
# dois_from_dir: reads subdirs, skips non-safe-DOI names, dedupes
# ---------------------------------------------------------------------------

def test_dois_from_dir_skips_side_directories(tmp_path: Path):
    """Only safe-DOI-shaped subdirs contribute DOIs. Sibling metadata dirs
    like 'done', 'panels', 'figmap' must NOT show up in the batch as bogus
    DOIs."""
    (tmp_path / "10.1007--s11064-015-1745-4").mkdir()
    (tmp_path / "done").mkdir()
    (tmp_path / "panels").mkdir()
    (tmp_path / "notes.txt").write_text("not a dir")

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", return_value=True):
        dois = dois_from_dir(tmp_path, verbose=False)

    assert dois == ["10.1007/s11064-015-1745-4"]


def test_dois_from_dir_picks_legacy_when_crossref_confirms(tmp_path: Path):
    """Ambiguous legacy folder → dois_from_dir asks Crossref, picks the
    reading Crossref knows. This is the fix path for the field failure."""
    (tmp_path / "10.1023--a--1016394031947").mkdir()

    # Crossref says modern is unknown, legacy is real -- pick legacy.
    def fake_agency(doi, email=None, timeout=6.0):
        return {
            "10.1023/a/1016394031947": False,
            "10.1023/a:1016394031947": True,
        }.get(doi)

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", side_effect=fake_agency):
        dois = dois_from_dir(tmp_path, verbose=False)

    assert dois == ["10.1023/a:1016394031947"]


def test_dois_from_dir_falls_back_to_modern_when_offline(tmp_path: Path):
    """If Crossref is unreachable (returns None), we don't invent a DOI --
    we keep the modern reading and let the batch step record the failure."""
    (tmp_path / "10.1023--a--1016394031947").mkdir()

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", return_value=None):
        dois = dois_from_dir(tmp_path, verbose=False)

    assert dois == ["10.1023/a/1016394031947"]


# ---------------------------------------------------------------------------
# migrate_legacy_folders: renames only when Crossref confirms, safe by default
# ---------------------------------------------------------------------------

def test_migrate_renames_legacy_folder_and_inner_stems(tmp_path: Path):
    """A folder that Crossref confirms is legacy is renamed to the modern
    encoding, and inner files whose stem starts with the old name are also
    renamed so sidecar filenames stay in sync."""
    legacy = tmp_path / "10.1023--a--1016394031947"
    legacy.mkdir()
    (legacy / "10.1023--a--1016394031947.pdf").write_bytes(b"pdf")
    (legacy / "10.1023--a--1016394031947_supplementary_info.json").write_text("{}")
    (legacy / "abstract.md").write_text("# abstract")  # untouched (no matching stem)

    def fake_agency(doi, email=None, timeout=6.0):
        return {
            "10.1023/a/1016394031947": False,
            "10.1023/a:1016394031947": True,
        }.get(doi)

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", side_effect=fake_agency):
        actions = migrate_legacy_folders(tmp_path, dry_run=False, verbose=False)

    assert len(actions) == 1
    assert actions[0]["action"] == "renamed"
    new_folder = tmp_path / "10.1023--a~1016394031947"
    assert new_folder.is_dir()
    assert not legacy.exists()
    assert (new_folder / "10.1023--a~1016394031947.pdf").is_file()
    assert (new_folder / "10.1023--a~1016394031947_supplementary_info.json").is_file()
    # Files whose stem doesn't start with the old name are left alone.
    assert (new_folder / "abstract.md").is_file()


def test_migrate_dry_run_touches_nothing(tmp_path: Path):
    """--migrate-dry-run must not rename or move anything on disk."""
    legacy = tmp_path / "10.1023--a--1016394031947"
    legacy.mkdir()
    (legacy / "10.1023--a--1016394031947.pdf").write_bytes(b"pdf")

    def fake_agency(doi, email=None, timeout=6.0):
        return {
            "10.1023/a/1016394031947": False,
            "10.1023/a:1016394031947": True,
        }.get(doi)

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", side_effect=fake_agency):
        actions = migrate_legacy_folders(tmp_path, dry_run=True)

    assert actions[0]["action"] == "would_rename"
    assert legacy.exists()  # untouched
    assert (legacy / "10.1023--a--1016394031947.pdf").exists()


def test_migrate_refuses_when_modern_reading_resolves(tmp_path: Path):
    """A legitimate multi-segment DOI folder (10.1093/abm/kaad072) has BOTH
    readings syntactically, but only the modern one is a real DOI. The
    migrator MUST leave it alone -- a false rename here would corrupt the
    corpus."""
    legit = tmp_path / "10.1093--abm--kaad072"
    legit.mkdir()

    def fake_agency(doi, email=None, timeout=6.0):
        return {
            "10.1093/abm/kaad072": True,   # real DOI
            "10.1093/abm:kaad072": False,  # bogus
        }.get(doi)

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", side_effect=fake_agency):
        actions = migrate_legacy_folders(tmp_path, dry_run=False)

    assert actions[0]["action"] == "kept"
    assert legit.exists()  # untouched


def test_migrate_merges_when_modern_folder_already_present(tmp_path: Path):
    """Corpora built by successive fetchpdf versions can end up with BOTH
    the legacy and the modern folder for one paper. Merge the legacy into the
    modern (files-that-do-not-exist policy) and remove the empty legacy tree,
    so the corpus ends up with exactly one folder per DOI."""
    legacy = tmp_path / "10.1023--a--1016394031947"
    modern = tmp_path / "10.1023--a~1016394031947"
    legacy.mkdir()
    modern.mkdir()
    # Legacy has the fat content; modern is a thin sidecar-only stub.
    (legacy / "10.1023--a--1016394031947.pdf").write_bytes(b"pdf")
    (legacy / "abstract.md").write_text("# abstract from legacy")
    (modern / "10.1023--a~1016394031947_supplementary_info.json").write_text("{}")
    # A colliding file: modern already has an abstract.md, so the legacy
    # one must NOT clobber it (no-clobber merge policy).
    (modern / "abstract.md").write_text("# abstract from modern")

    def fake_agency(doi, email=None, timeout=6.0):
        return {
            "10.1023/a/1016394031947": False,
            "10.1023/a:1016394031947": True,
        }.get(doi)

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", side_effect=fake_agency):
        actions = migrate_legacy_folders(tmp_path, dry_run=False)

    assert actions[0]["action"] == "merged"
    assert not legacy.exists()  # emptied and removed
    # Legacy's PDF moved into modern (modern had none)
    assert (modern / "10.1023--a--1016394031947.pdf").is_file()
    # Sidecar the modern folder already had is preserved
    assert (modern / "10.1023--a~1016394031947_supplementary_info.json").is_file()
    # Colliding abstract.md kept the modern version (no-clobber)
    assert (modern / "abstract.md").read_text() == "# abstract from modern"


def test_migrate_ignores_non_safe_dirs(tmp_path: Path):
    """'done', 'panels', 'figmap' and other sibling metadata dirs must not
    show up in the migration report as errors or as candidates."""
    (tmp_path / "done").mkdir()
    (tmp_path / "panels").mkdir()
    (tmp_path / "10.1007--s11064-015-1745-4").mkdir()  # modern, no '--' in suffix

    with patch("fetchpdf.fetchpdf._crossref_doi_exists", return_value=True):
        actions = migrate_legacy_folders(tmp_path, dry_run=True)

    # No legacy folders at all -> no actions recorded.
    assert actions == []
