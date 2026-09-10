"""Offline tests for the PMC article page as a T2 source.

No network. The doubles follow tests/test_retrieval.py: canned responses
replayed by URL substring, and a context carrying a fixed IdentifierSet.

The load-bearing tests here:

  test_a_page_without_its_body_is_asked_for_again  -- the transient this exists for
  test_the_last_attempt_is_handed_to_the_validator -- a renamed CSS class must not
                                                      delete the source
  test_pmcid_comes_off_a_jats_already_on_disk      -- why the source declares no
                                                      `requires`
  test_a_non_200_is_not_retried                    -- 404 is an answer, not a blip
"""

import json
import os

import pytest

from fetchpdf.retrieval.context import RetrievalContext
from fetchpdf.retrieval.identifiers import IdentifierSet
from fetchpdf.retrieval.provenance import ProvenanceRecord
from fetchpdf.retrieval.sources import pmc_html
from fetchpdf.retrieval.tiers import Tier, load_ladder


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, content=b"", status=200, content_type="text/html",
                 url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1817752/"):
        self.content = content
        self.status = status
        self.content_type = content_type
        self.url = url
        self.request_url = url
        self.headers = {}
        self.elapsed = 0.0

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class ScriptedHttp:
    """Hands back one canned response per call, in order, and counts them."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, **kwargs):
        self.requests.append(url)
        return self.responses[min(len(self.requests), len(self.responses)) - 1]


def ctx_for(http, tmp_path, provenance=None):
    ctx = RetrievalContext(http=http, resolver=None, ladder=load_ladder(),
                           save_path=str(tmp_path / "rec.pdf"))
    ctx.provenance = provenance
    return ctx


#: The shape PMC actually serves. Verified live 2026-09-07 on PMC1817752: the
#: complete page carries `class="body main-article-body"`, and the truncated
#: one carries no article body element at all.
COMPLETE_PAGE = (b'<html><body><section class="body main-article-body">'
                 b'<table><tr><td>1.2</td></tr></table></section></body></html>')
TRUNCATED_PAGE = b'<html><body><div class="usa-banner">PMC</div></body></html>'


def ids_with_pmcid(pmcid="PMC1817752"):
    return IdentifierSet(doi="10.1371/journal.pone.0000308", pmcid=pmcid)


# --------------------------------------------------------------------------
# The retry
# --------------------------------------------------------------------------


def test_a_page_without_its_body_is_asked_for_again(tmp_path):
    """The transient the retry exists for: 200, no body, then the real page.

    Verified twice on 2026-09-06 against the live endpoint, on the same
    article minutes apart. Accepting the first answer records PMC's cache miss
    as a fact about the paper.
    """
    http = ScriptedHttp([FakeResponse(TRUNCATED_PAGE), FakeResponse(COMPLETE_PAGE)])
    artifact = pmc_html.fetch_pmc_article_page(
        ids_with_pmcid(), ctx_for(http, tmp_path), waits=(0,))

    assert artifact is not None
    assert artifact.content == COMPLETE_PAGE
    assert artifact.extra["page_attempts"] == 2
    assert artifact.extra["article_body_present"] is True
    assert len(http.requests) == 2


def test_a_complete_page_costs_exactly_one_request(tmp_path):
    http = ScriptedHttp([FakeResponse(COMPLETE_PAGE)])
    artifact = pmc_html.fetch_pmc_article_page(
        ids_with_pmcid(), ctx_for(http, tmp_path), waits=(0, 0, 0))

    assert len(http.requests) == 1
    assert artifact.extra["page_attempts"] == 1
    assert artifact.tier == Tier.T2_HTML
    assert artifact.source == "pmc_html"
    assert artifact.identifier_used == "PMC1817752"


def test_the_last_attempt_is_handed_to_the_validator(tmp_path):
    """A body marker that never appears must cost three requests, not the source.

    The alternative -- refuse here -- means the day PMC renames a CSS class
    this rung silently stops existing. The T2 gate reads the actual content and
    is what refuses an empty page.
    """
    http = ScriptedHttp([FakeResponse(TRUNCATED_PAGE)])
    artifact = pmc_html.fetch_pmc_article_page(
        ids_with_pmcid(), ctx_for(http, tmp_path), waits=(0, 0, 0))

    assert artifact is not None
    assert artifact.content == TRUNCATED_PAGE
    assert artifact.extra["article_body_present"] is False
    assert len(http.requests) == 4


def test_a_non_200_is_not_retried(tmp_path):
    """404 is an answer. Retrying an answer is how a run spins against a "no"."""
    http = ScriptedHttp([FakeResponse(b"", status=404)])
    artifact = pmc_html.fetch_pmc_article_page(
        ids_with_pmcid(), ctx_for(http, tmp_path), waits=(0, 0, 0))

    assert artifact is None
    assert len(http.requests) == 1


def test_a_retry_is_recorded_in_provenance(tmp_path):
    """"Needed three tries" and "answered first time" must not read the same."""
    provenance = ProvenanceRecord(identifier="10.1/x", target_task="extraction")
    http = ScriptedHttp([FakeResponse(TRUNCATED_PAGE), FakeResponse(COMPLETE_PAGE)])
    pmc_html.fetch_pmc_article_page(
        ids_with_pmcid(), ctx_for(http, tmp_path, provenance), waits=(0,))

    assert any("pmc_html" in note and "2 of 2" in note for note in provenance.notes)


# --------------------------------------------------------------------------
# Where the PMCID comes from
# --------------------------------------------------------------------------


def test_no_pmcid_means_no_request(tmp_path):
    http = ScriptedHttp([FakeResponse(COMPLETE_PAGE)])
    artifact = pmc_html.fetch_pmc_article_page(
        IdentifierSet(doi="10.1/x"), ctx_for(http, tmp_path), waits=())

    assert artifact is None
    assert http.requests == []


def test_pmcid_comes_off_a_jats_already_on_disk(tmp_path):
    """Why this source declares no `requires` in ladder.json.

    The resolution chain reaches a PMCID only when it happens to pass through a
    service that returns one, so a record can carry PMC's own JATS from an
    earlier run while this run's IdentifierSet has no PMCID at all. A
    `requires: ["pmcid"]` gate would skip the source before it could look.
    """
    (tmp_path / "rec.xml").write_bytes(
        b'<article><front><article-meta>'
        b'<article-id pub-id-type="pmc">1817752</article-id>'
        b'</article-meta></front></article>')
    http = ScriptedHttp([FakeResponse(COMPLETE_PAGE)])
    artifact = pmc_html.fetch_pmc_article_page(
        IdentifierSet(doi="10.1/x"), ctx_for(http, tmp_path), waits=())

    assert artifact is not None
    assert artifact.identifier_used == "PMC1817752"
    assert http.requests == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC1817752/"]


def test_europe_pmc_spells_the_id_type_differently(tmp_path):
    (tmp_path / "rec.xml").write_bytes(
        b'<article-id pub-id-type="pmcid">PMC3390974</article-id>')
    assert pmc_html.pmcid_for(
        IdentifierSet(doi="10.1/x"),
        ctx_for(ScriptedHttp([]), tmp_path)) == "PMC3390974"


def test_an_xml_with_no_article_id_yields_nothing(tmp_path):
    (tmp_path / "rec.xml").write_bytes(b"<article><front/></article>")
    assert pmc_html.pmcid_for(
        IdentifierSet(doi="10.1/x"),
        ctx_for(ScriptedHttp([]), tmp_path)) is None


@pytest.mark.parametrize("raw,expected", [
    ("PMC1817752", "PMC1817752"),
    ("1817752", "PMC1817752"),
    ("pmc1817752", "PMC1817752"),
    ("", None),
    (None, None),
])
def test_pmcid_normalisation(raw, expected):
    assert pmc_html.normalise_pmcid(raw) == expected


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


def test_the_source_is_wired_between_unpaywall_and_the_doi_resolver():
    """Order is the whole point: after the routes that cost nothing to try,
    before plain DOI resolution, and inside T2 so the XML rungs run first."""
    ladder = load_ladder()
    t2 = [spec.name for spec in ladder.sources_for(Tier.T2_HTML)]

    assert "pmc_html" in t2
    assert t2.index("pmc_html") < t2.index("publisher_html")


def test_the_source_declares_no_identifier_requirement():
    """Guards the disk fallback above: a `requires` entry would disable it."""
    ladder = load_ladder()
    assert ladder.sources["pmc_html"].requires == ()
