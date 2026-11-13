"""The DataCite reverse query: reading past the first page, one routing per
deposit, and Dryad's own statement of which paper a dataset belongs to.

Fixtures are trimmed copies of live responses captured 2026-11-13.
"""
import json
import os
from types import SimpleNamespace

import pytest

from fetchpdf.retrieval import supplement_graph as graph

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MANY = "10.1186/s12711-026-01107-z"


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class Response:
    def __init__(self, payload=None, ok=True):
        self.ok, self._payload = ok, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class Http:
    """Serves DataCite reverse pages from a record list, and Dryad datasets."""

    def __init__(self, records=(), dryad=None, total=None, fail_page=None,
                 ignore_page_number=False):
        self.records, self.dryad = list(records), dryad or {}
        self.total = len(self.records) if total is None else total
        self.fail_page, self.ignore_page_number = fail_page, ignore_page_number
        self.pages, self.dryad_calls = [], []

    def get(self, url, params=None, **kwargs):
        if "datadryad.org" in url:
            self.dryad_calls.append(url)
            for doi, payload in self.dryad.items():
                if doi.replace("/", "%2F") in url:
                    return Response(payload)
            return Response(ok=False)
        size, page = params["page[size]"], params["page[number]"]
        self.pages.append(page)
        if page == self.fail_page:
            return Response(ok=False)
        start = 0 if self.ignore_page_number else (page - 1) * size
        return Response({"data": self.records[start:start + size],
                         "meta": {"total": self.total,
                                  "totalPages": -(-self.total // size)}})


def context(http):
    logs = []
    return SimpleNamespace(http=http, scratch={}, log=logs.append, logs=logs)


@pytest.fixture
def routed(monkeypatch):
    seen = []
    monkeypatch.setattr(graph, "_files_in_repository",
                        lambda doi, ids, ctx, **kw: seen.append(doi) or [doi])
    return seen


def supplement(doi, article, created="2026-01-01", extra=()):
    return {"id": doi, "attributes": {
        "doi": doi, "created": created,
        "types": {"resourceTypeGeneral": "Dataset"},
        "titles": [{"title": "A deposit"}],
        "relatedIdentifiers": [{"relationType": "IsSupplementTo",
                                "relatedIdentifier": article}, *extra]}}


# -- the cap ------------------------------------------------------------------


def test_all_29_supplements_of_a_real_paper_are_routed_once_each(routed):
    """58 records -- every figshare item as `<doi>` and `<doi>.v1`. The old
    25-record page saw Additional files 17-29 and none of 1-16."""
    records = load("datacite_reverse_29_supplements.json")["data"]
    assert len(records) == 58
    files = graph.enumerate_datacite_related(
        SimpleNamespace(doi=MANY), context(Http(records)))
    assert len(routed) == len(files) == 29
    assert len({graph._VERSION_SUFFIX_RE.sub("", doi) for doi in routed}) == 29


def test_reads_every_page_until_the_index_is_exhausted(routed):
    records = [supplement(f"10.5281/zenodo.{n}", MANY) for n in range(230)]
    http = Http(records)
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(http))
    assert http.pages == [1, 2, 3]
    assert len(routed) == graph._MAX_DEPOSITS       # 230 deposits, capped


def test_a_single_page_costs_a_single_request(routed):
    http = Http([supplement("10.5281/zenodo.1", MANY)])
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(http))
    assert http.pages == [1] and routed == ["10.5281/zenodo.1"]


def test_stops_at_the_page_ceiling_and_says_what_was_left(routed):
    records = [supplement(f"10.5281/zenodo.{n}", "10.1/other") for n in range(600)]
    http, ctx = Http(records, total=6585), None
    ctx = context(http)
    assert graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), ctx) == []
    assert http.pages == list(range(1, graph._MAX_PAGES + 1))
    assert any("read 500 of 6585" in line for line in ctx.logs)


def test_a_failed_later_page_keeps_the_earlier_ones(routed):
    records = [supplement(f"10.5281/zenodo.{n}", MANY) for n in range(150)]
    graph.enumerate_datacite_related(
        SimpleNamespace(doi=MANY), context(Http(records, fail_page=2)))
    assert len(routed) == 100


def test_a_server_that_ignores_the_page_number_cannot_loop(routed):
    records = [supplement(f"10.5281/zenodo.{n}", MANY) for n in range(100)]
    http = Http(records, total=1000, ignore_page_number=True)
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(http))
    assert http.pages == [1, 2] and len(routed) == 100


# -- one routing per deposit --------------------------------------------------


def version_of(parent):
    return {"relationType": "IsVersionOf", "relatedIdentifier": parent}


def test_zenodo_versions_collapse_to_the_newest(routed):
    concept = "10.5281/zenodo.100"
    records = [
        supplement(concept, MANY, "2024-01-01"),
        supplement("10.5281/zenodo.101", MANY, "2024-01-01", [version_of(concept)]),
        supplement("10.5281/zenodo.205", MANY, "2025-06-01", [version_of(concept)]),
        supplement("10.5281/zenodo.150", MANY, "2024-09-01", [version_of(concept)]),
    ]
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(Http(records)))
    assert routed == ["10.5281/zenodo.205"]


def test_a_lone_versionless_doi_is_routed_as_it_is(routed):
    records = [supplement("10.5281/zenodo.100", MANY),
               supplement("10.6084/m9.figshare.7", MANY)]
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(Http(records)))
    assert routed == ["10.5281/zenodo.100", "10.6084/m9.figshare.7"]


def test_mendeley_numbered_versions_collapse(routed):
    base = "10.17632/tcpb72scrs"
    records = [supplement(base, MANY),
               supplement(base + ".1", MANY, "2026-01-01", [version_of(base)]),
               supplement(base + ".2", MANY, "2026-03-01", [version_of(base)])]
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(Http(records)))
    assert routed == [base + ".2"]


def test_version_of_something_in_another_registry_does_not_merge_deposits(routed):
    records = [supplement("10.5281/zenodo.1", MANY, extra=[version_of("10.1101/preprint")]),
               supplement("10.5281/zenodo.2", MANY, extra=[version_of("10.1101/preprint")])]
    graph.enumerate_datacite_related(SimpleNamespace(doi=MANY), context(Http(records)))
    assert sorted(routed) == ["10.5281/zenodo.1", "10.5281/zenodo.2"]


def test_refused_versions_do_not_stand_in_for_accepted_ones(routed):
    """Only the concept record claims the article; its version does not."""
    concept = "10.5281/zenodo.100"
    version = supplement("10.5281/zenodo.101", "10.1/other", extra=[version_of(concept)])
    graph.enumerate_datacite_related(
        SimpleNamespace(doi=MANY), context(Http([supplement(concept, MANY), version])))
    assert routed == [concept]


# -- Dryad --------------------------------------------------------------------

DRYAD = load("dryad_related_works.json")
LONE, MIXED = "10.5061/dryad.djh9w0whp", "10.5061/dryad.w6m905r2r"


def dryad_http():
    return Http(dryad={doi: entry["dryad"] for doi, entry in DRYAD.items()})


def accept(deposit, article, http=None):
    ctx = context(http or dryad_http())
    # No title evidence: the article's title is unknown to this context.
    ctx.scratch["_article_titles"] = {article: ""}
    return graph._accept_related(DRYAD[deposit]["datacite"], article, ctx), ctx


def test_dryad_primary_article_owns_the_dataset():
    assert accept(MIXED, "10.1139/cjfas-2025-0141")[0] == MIXED


def test_dryad_other_article_beside_a_primary_does_not():
    """Same deposit, same IsCitedBy in DataCite -- Dryad alone tells them apart."""
    assert accept(MIXED, "10.1139/cjfas-2023-0172")[0] is None


def test_dryad_lone_article_owns_the_dataset():
    """"Data and code from: ..." with a single related `article`."""
    assert accept(LONE, "10.1242/jeb.252685")[0] == LONE


def test_dryad_does_not_vouch_for_an_article_it_does_not_list():
    assert accept(LONE, "10.1000/unrelated")[0] is None


@pytest.mark.parametrize("works, owned", [
    ([("primary_article", "A")], True),
    ([("preprint", "A"), ("primary_article", "B")], True),
    ([("article", "A"), ("article", "B")], False),          # which one? unknown
    ([("article", "A"), ("preprint", "B")], True),
    ([("article", "A"), ("primary_article", "B")], False),
    ([("software", "A")], False),
    ([("dataset", "A"), ("supplemental_information", "A")], False),
    ([], False),
])
def test_dryad_relationship_rules(works, owned):
    article = "10.1000/a"
    dois = {"A": "https://doi.org/10.1000/A", "B": "10.1000/b"}
    payload = {"relatedWorks": [{"relationship": kind, "identifier": dois[which]}
                                for kind, which in works]}
    ctx = context(Http(dryad={"10.5061/dryad.x": payload}))
    assert graph._dryad_names_article("10.5061/dryad.x", article, ctx) is owned


def test_dryad_is_asked_once_per_deposit_and_only_about_dryad():
    http = dryad_http()
    ctx = context(http)
    for _ in range(3):
        graph._dryad_names_article(LONE, "10.1242/jeb.252685", ctx)
    assert len(http.dryad_calls) == 1
    assert graph._dryad_names_article("10.5281/zenodo.1", "10.1242/jeb.252685", ctx) is False
    assert len(http.dryad_calls) == 1


def test_dryad_lookup_failure_fails_closed():
    ctx = context(Http())
    assert graph._dryad_names_article(LONE, "10.1242/jeb.252685", ctx) is False

    def boom(*a, **kw):
        raise RuntimeError("connection reset")
    ctx = SimpleNamespace(http=SimpleNamespace(get=boom), scratch={}, log=lambda m: None)
    assert graph._dryad_names_article(LONE, "10.1242/jeb.252685", ctx) is False


def test_dryad_lookups_are_bounded_for_heavily_cited_papers():
    http = Http()
    ctx = context(http)
    for n in range(graph._MAX_DRYAD_LOOKUPS + 40):
        graph._dryad_names_article(f"10.5061/dryad.x{n}", "10.1000/a", ctx)
    assert len(http.dryad_calls) == graph._MAX_DRYAD_LOOKUPS


def test_dryad_statement_also_upgrades_related_links_to_owned(monkeypatch):
    monkeypatch.setattr(graph, "_datacite_attributes",
                        lambda doi, ctx: DRYAD[MIXED]["datacite"]["attributes"])
    ctx = context(dryad_http())
    ctx.scratch["_article_titles"] = {"10.1139/cjfas-2025-0141": ""}
    assert graph._deposit_names_article(MIXED, "10.1139/cjfas-2025-0141", ctx) is True


# -- deposit titles -----------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Data from: Colourful feathers do not resist degradation",
    "Data and code from: Colourful feathers do not resist degradation",
    "Code and data for: Colourful feathers do not resist degradation",
    "Data, code, and scripts from: Colourful feathers do not resist degradation",
    "Dataset for “Colourful feathers do not resist degradation”",
    "Supporting data for the article: Colourful feathers do not resist degradation",
    "Replication Data for: Colourful feathers do not resist degradation",
])
def test_deposit_title_variants_match_the_article(monkeypatch, title):
    monkeypatch.setattr(graph, "_article_title",
                        lambda doi, ctx: "Colourful feathers do not resist degradation")
    assert graph._replication_title_matches({"titles": [{"title": title}]}, "10.1/a", None)


@pytest.mark.parametrize("title", [
    "Colourful feathers do not resist degradation",        # no deposit prefix
    "Data from: A different study of parrots entirely",    # prefix, wrong paper
    "Database of colourful feathers",
])
def test_deposit_titles_that_are_not_a_claim(monkeypatch, title):
    monkeypatch.setattr(graph, "_article_title",
                        lambda doi, ctx: "Colourful feathers do not resist degradation")
    assert not graph._replication_title_matches({"titles": [{"title": title}]}, "10.1/a", None)
