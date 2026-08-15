"""Regression tests for validate_t2's landing-page/decoy detection.

Every signature/marker here was pulled from a REAL page that fooled the
original validator (only a paywall-phrase list, a >=1-populated-<td> check,
and a byte-count floor) during a live corpus run against ~30 cerebrolysin
papers fetched with --get-xml-or-html. Each fixture below is a minimal,
synthetic reproduction of the specific structural signal that fooled the
original checks -- not a copy of the real page (which would be large,
publisher-copyrighted, and an external-path dependency this suite shouldn't
have) -- but reproduces the SAME failure mode byte-for-byte on the signal that
matters (the exact phrase, the exact meta-tag mismatch, the exact URL shape).

Real DOIs/URLs are used in the synthetic fixtures purely as realistic-looking
identifiers; the HTML bodies are hand-built minimal reproductions.
"""

from fetchpdf.retrieval.validate import validate_t2

_REAL_ARTICLE_TABLE = (
    "<table><tr><th>Outcome</th><th>Drug</th></tr>"
    "<tr><td>Mortality</td><td>12 (4%)</td></tr></table>"
)


def _page(body_extra: str, table: str = _REAL_ARTICLE_TABLE, filler_paragraphs: int = 150) -> bytes:
    """A page long enough to clear the byte-count floors on its own merits,
    so a test fails for the reason it claims to, not because it is short."""
    filler = "<p>Filler paragraph text to clear the length floor.</p>" * filler_paragraphs
    return (
        f"<!DOCTYPE html><html><head><title>Article</title></head>"
        f"<body>{body_extra}<article>{filler}{table}</article></body></html>"
    ).encode("utf-8")


def test_accepts_a_real_article():
    content = _page("<p>" + "Real body text of the trial report. " * 200 + "</p>")
    result = validate_t2(content)
    assert result.ok, result.reason
    assert result.n_tables == 1


def test_rejects_journal_toc_cookie_banner():
    """Journal of Neurosurgery TOC page: 'Dismiss this warning' cookie banner,
    with a real-looking metrics <table> (Abstract Views / PDF Downloads
    counters) that trivially clears the old populated-<td> check."""
    content = _page(
        "<p>Jump to Content. Tags and tracking settings help give you the "
        "very best browsing experience. Dismiss this warning</p>",
        table="<table><tr><td>Abstract Views</td><td>2330</td></tr>"
              "<tr><td>PDF Downloads</td><td>37</td></tr></table>",
        filler_paragraphs=150,
    )
    result = validate_t2(content)
    assert not result.ok
    assert "dismiss this warning" in result.reason.lower()


def test_rejects_no_js_fallback_shell():
    """Dove Medical Press: a plain GET only fetches the no-JS shell."""
    content = _page(
        "<p>Javascript is currently disabled in your browser. Several "
        "features of this site will not function whilst javascript is "
        "disabled.</p>"
    )
    result = validate_t2(content)
    assert not result.ok
    assert "javascript is currently disabled" in result.reason.lower()


def test_rejects_russian_publisher_age_gate():
    content = _page(
        "<p>Сайт издательства содержит материалы, предназначенные "
        "исключительно для работников здравоохранения. "
        "Закрывая это сообщение, Вы подтверждаете...</p>"
    )
    result = validate_t2(content)
    assert not result.ok
    assert "закрывая это сообщение" in result.reason.lower()


def test_rejects_generic_cookie_notice():
    content = _page("<p>This site uses cookies to improve your experience.</p>")
    result = validate_t2(content)
    assert not result.ok
    assert "this site uses cookies" in result.reason.lower()


def test_rejects_institutional_repository_by_url():
    """IRIS/DSpace catalog pages show the real title and abstract -- a
    text-content check alone cannot tell this from the publisher's own page.
    The URL is where the page's actual identity lives."""
    content = _page(
        "<p>" + "This is the genuine abstract text of the deposited item. " * 50 + "</p>"
    )
    result = validate_t2(
        content, url="https://iris.unimore.it/handle/11380/1073431", doi="10.1007/s12035-015-9235-x"
    )
    assert not result.ok
    assert "repository" in result.reason.lower()


def test_accepts_same_content_without_repository_url():
    """Sanity check on the test above: it is the URL doing the rejecting,
    not something incidental about the body text."""
    content = _page(
        "<p>" + "This is the genuine abstract text of the deposited item. " * 50 + "</p>"
    )
    result = validate_t2(content, url="https://link.springer.com/article/xyz", doi="10.1007/s12035-015-9235-x")
    assert result.ok, result.reason


def test_rejects_page_declaring_a_different_doi():
    content = _page(
        "<p>" + "Body text of some article. " * 100 + "</p>"
        '<meta name="citation_doi" content="10.9999/not-the-requested-doi">'
    )
    result = validate_t2(content, doi="10.1007/s12035-015-9235-x")
    assert not result.ok
    assert "different article" in result.reason.lower()


def test_missing_citation_doi_is_not_a_mismatch():
    """Absence proves nothing -- most publisher HTML omits these tags. A
    missing meta tag must never be treated as a mismatch."""
    content = _page("<p>" + "Body text of some article. " * 100 + "</p>")
    result = validate_t2(content, doi="10.1007/s12035-015-9235-x")
    assert result.ok, result.reason


def test_no_doi_supplied_skips_the_citation_check():
    content = _page(
        "<p>" + "Body text of some article. " * 100 + "</p>"
        '<meta name="citation_doi" content="10.9999/anything">'
    )
    result = validate_t2(content, doi=None)
    assert result.ok, result.reason


import pytest


@pytest.mark.xfail(
    reason=(
        "Known gap, not fixed by this change: a JS-rendered app shell (Next.js/"
        "React) that correctly declares the right citation_doi/og:title in "
        "<head>, and has non-trivial populated <table> content elsewhere on the "
        "page (nav/citation-export/related-articles widgets), but never "
        "server-rendered the article's own body text. Two real examples "
        "(an LWW/Ovid Spine article and a Wiley JNR article) were measured "
        "with correct metadata, >20KB of <article>-tag text, and >100 "
        "populated table cells -- none of which distinguish them from a "
        "genuine article by any cheap structural signal tried so far. "
        "Fixing this needs either a stronger content-provenance check "
        "(e.g. is the populated-table content inside vs. outside <article>) "
        "or broadening has_empty_table_containers()'s Playwright-retry trigger "
        "beyond 'zero populated cells'. Left as a documented, verified-open gap."
    ),
    strict=True,
)
def test_known_gap_js_shell_with_correct_metadata_is_not_yet_caught():
    # Minimal reproduction: correct metadata, populated widget table, no real
    # article prose -- the actual shape of both real examples measured.
    content = _page(
        "",
        table="<table><tr><td>Related article 1</td><td>Cited by 4</td></tr>"
              "<tr><td>Related article 2</td><td>Cited by 9</td></tr></table>",
        # Bulk boilerplate/nav text, standing in for the real pages' SPA-shell
        # markup -- length alone must not be what makes this test pass or fail.
        filler_paragraphs=150,
    ).decode("utf-8")
    content = content.replace(
        "<head>",
        '<head><meta name="citation_doi" content="10.1002/jnr.24072">'
        '<meta property="og:title" content="The real article title">',
    ).encode("utf-8")
    result = validate_t2(content, doi="10.1002/jnr.24072")
    assert not result.ok, "if this now passes, the gap above may be fixed -- update the docstring"
