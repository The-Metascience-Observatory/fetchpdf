import pytest

from fetchpdf.retrieval.supplement_index import _matches_href, classify_role


@pytest.mark.parametrize("extension", ["pdf", "jpg", "png", "docx", "csv", "h5"])
def test_dotted_jats_asset_without_file_extension_matches(extension):
    assert _matches_href("pone.0312714.s001." + extension, {"pone.0312714.s001"})


@pytest.mark.parametrize("left,right", [
    ("pone.0312714.g001", "pone.0312714.g002"),
    ("pone.0312714.t001.jpg", "pone.0312714.t002"),
    ("pone.0312714.s001", "pone.0312714.s002"),
    ("pone.0312714.s001.unknown", "pone.0312714.s001"),
])
def test_distinct_publisher_assets_are_never_collapsed(left, right):
    assert not _matches_href(left, {right})


def test_same_asset_encodings_paths_queries_and_case():
    assert _matches_href("pone.0312714.g001.jpg", {"https://example.org/PONE.0312714.G001.PNG?download=1"})
    assert _matches_href("figure1.jpg", {"figure1"})
    assert not _matches_href("", {""})


def test_declared_figure_is_classified_and_declared_supplement_wins():
    assert classify_role("pone.0312714.g001.jpg", jats_figures={"pone.0312714.g001"}) == "figure"
    assert classify_role("pone.0312714.g001.jpg", jats_figures={"pone.0312714.g002"}) == "unknown"
    assert classify_role("pone.0312714.s001.jpg", jats_figures={"pone.0312714.s001"},
                         jats_supplements={"pone.0312714.s001"}) == "supplement"
