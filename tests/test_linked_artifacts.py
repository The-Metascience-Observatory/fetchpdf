"""The link-discovery providers (ScholeXplorer, EPMC datalinks) and their sidecar.

The stakes the assertions protect: a tool citation must never become a
downloaded "supplement", an empty answer must leave a written record, and the
same deposit reachable through three link services must be listed once.
"""

import json
import os

from fetchpdf.retrieval.context import RetrievalContext
from fetchpdf.retrieval.identifiers import IdentifierSet
from fetchpdf.retrieval.linked_artifacts import (
    SCRATCH_KEY,
    sidecar_path_for,
    write_linked_sidecar,
)
from fetchpdf.retrieval.supplement_graph import (
    enumerate_epmc_datalinks,
    enumerate_scholix_related,
)
from fetchpdf.retrieval.tiers import load_ladder

# --------------------------------------------------------------------------
# Doubles above the transport. Kept local rather than imported: there is no
# tests/__init__.py, and the existing suite keeps its doubles local.
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, content=b"", status=200, content_type="", url="https://example.org/x"):
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

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Replays canned responses by URL substring, and records what was asked."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default or FakeResponse(status=404)
        self.requests = []
        self.limiter = None

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        # Params matter here: the Scholix provider asks the same URL twice with
        # targetType=dataset then =software, so routes match against both.
        askable = url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        for fragment, response in self.routes.items():
            if fragment in askable:
                return response
        return self.default


def json_response(payload):
    return FakeResponse(content=json.dumps(payload).encode())


def ctx_with(http, **kwargs):
    return RetrievalContext(http=http, resolver=None, ladder=load_ladder(),
                            save_path=kwargs.pop("save_path", "/tmp/rec.pdf"), **kwargs)


# --------------------------------------------------------------------------
# Canned payloads, shaped as the live services shaped them (2026-08).
# --------------------------------------------------------------------------


def scholix_link(target_doi=None, title="", subtype="cites", target_type="dataset",
                 publisher="Zenodo"):
    identifier = []
    if target_doi:
        identifier.append({"ID": target_doi, "IDScheme": "doi", "IDURL": None})
    return {
        "HarvestDate": "2026-01-01",
        "LinkProvider": [{"identifier": [], "name": "OpenAIRE"}],
        "RelationshipType": {"Name": "IsRelatedTo", "SubType": subtype,
                             "SubTypeSchema": "datacite"},
        "source": {"Identifier": [], "Title": "the article"},
        "target": {
            "Identifier": identifier,
            "Title": title,
            "Type": target_type,
            "Publisher": [{"name": publisher}],
        },
    }


def scholix_page(links):
    return {"currentPage": 0, "totalLinks": len(links), "totalPages": 1,
            "result": links}


ZENODO_LISTING = {
    "metadata": {"resource_type": {"type": "dataset"}},
    "files": [
        {"key": "analysis_data.csv", "links": {"self": "https://zenodo.org/api/files/d1/analysis_data.csv"},
         "size": 1234, "checksum": "md5:abc"},
    ],
}

# Shaped like the live Dryad v2 API (verified 2026-08-09): dataset -> version
# href -> files with declared size, sha-256 digest and a stash:download href.
DRYAD_DATASET_PAYLOAD = {
    "identifier": "doi:10.5061/dryad.h44j0zpwg",
    "_links": {"stash:version": {"href": "/api/v2/versions/126667"}},
}
DRYAD_FILES_PAYLOAD = {
    "total": 2,
    "_embedded": {"stash:files": [
        {"path": "salt_tolerance_data.csv", "size": 51234, "mimeType": "text/csv",
         "status": "copied", "digest": "abc123", "digestType": "sha-256",
         "_links": {"stash:download": {"href": "/api/v2/files/772999/download"}}},
        {"path": "old_version.csv", "size": 10, "status": "deleted",
         "_links": {"stash:download": {"href": "/api/v2/files/700000/download"}}},
    ]},
    "_links": {},
}

def datacite_ownership(deposit_doi, article_doi):
    """A DataCite record for `deposit_doi` that names `article_doi` -- the
    affirmative-ownership answer the download gate requires."""
    return json_response({"data": {"attributes": {
        "doi": deposit_doi,
        "relatedIdentifiers": [
            {"relationType": "IsSupplementTo", "relatedIdentifier": article_doi},
        ],
    }}})


# Shaped like the live Harvard Dataverse API: data.latestVersion.files[] with
# dataFile{id, filename, contentType, filesize, md5}.
DATAVERSE_PAYLOAD = {
    "status": "OK",
    "data": {"latestVersion": {"files": [
        {"restricted": False,
         "dataFile": {"id": 2498426, "filename": "rep_materials.zip",
                      "contentType": "application/zip", "filesize": 2545568,
                      "md5": "f57184de03766641c510cdc911d9fad1"}},
        {"restricted": True,
         "dataFile": {"id": 999, "filename": "restricted.dta", "filesize": 5}},
    ]}},
}


def epmc_link(pid, scheme, title="", relation="References", target_type="dataset"):
    return {
        "ObtainedBy": "tm_accession",
        "PublicationDate": "19-07-2026",
        "LinkProvider": {"Name": "Europe PMC"},
        "RelationshipType": {"Name": relation},
        "Source": {"Type": {"Name": "literature"},
                   "Identifier": {"ID": "38375968", "IDScheme": "MED"}},
        "Target": {"Type": {"Name": target_type},
                   "Identifier": {"ID": pid, "IDScheme": scheme},
                   "Title": title or pid,
                   "Publisher": {"Name": "Europe PMC"}},
    }


def epmc_payload(categories):
    return {
        "version": "6.9",
        "hitCount": sum(len(links) for _, links in categories),
        "dataLinkList": {"Category": [
            {"Name": name, "CategoryLinkCount": len(links),
             "Section": [{"ObtainedBy": "tm_accession", "Tags": ["supporting_data"],
                          "Linklist": {"Link": links}}]}
            for name, links in categories
        ]},
    }


# --------------------------------------------------------------------------
# ScholeXplorer
# --------------------------------------------------------------------------


def test_scholix_routes_an_owned_zenodo_dataset_into_files():
    http = FakeHttp(routes={
        "targetType=dataset": json_response(scholix_page(
            [scholix_link("10.5281/zenodo.123", "Study data")])),
        "targetType=software": json_response(scholix_page([])),
        "api.datacite.org/dois/": datacite_ownership("10.5281/zenodo.123", "10.1000/x"),
        "zenodo.org/api/records/123": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)

    assert [f.name for f in files] == ["analysis_data.csv"]
    assert files[0].provider == "scholix_related:zenodo_files"
    assert files[0].origin_doi == "10.5281/zenodo.123"

    links = ctx.scratch[SCRATCH_KEY]["links"]
    assert len(links) == 1
    assert links[0]["routed_to_download"] is True
    assert links[0]["classified"] == "owned"
    assert links[0]["target_pid"] == "10.5281/zenodo.123"


def test_scholix_citation_of_an_unconfirmed_deposit_is_never_downloaded():
    """A repo deposit the paper merely cites: DataCite does not tie it to the
    article, so it stays a sidecar link and the repository is never asked."""
    http = FakeHttp(routes={
        "targetType=dataset": json_response(scholix_page(
            [scholix_link("10.5281/zenodo.123", "Somebody else's data")])),
        "targetType=software": json_response(scholix_page([])),
        "api.datacite.org/dois/": datacite_ownership("10.5281/zenodo.123", "10.9999/other"),
        "zenodo.org/api/records/123": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)

    assert files == []
    assert not any("zenodo.org" in url for url, _ in http.requests)
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "related"
    assert link["routed_to_download"] is False


def test_scholix_tool_citation_is_recorded_but_never_downloaded():
    http = FakeHttp(routes={
        "scholexplorer": json_response(scholix_page(
            [scholix_link("10.5281/zenodo.999", "xgboost software on GitHub",
                          target_type="software")])),
        "zenodo.org": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)

    assert files == []
    assert not any("zenodo" in url for url, _ in http.requests)
    recorded = ctx.scratch[SCRATCH_KEY]["links"]
    # One link per targetType query; both pages replay the same canned answer.
    assert all(l["classified"] == "tool_citation" for l in recorded)
    assert all(l["routed_to_download"] is False for l in recorded)


def test_scholix_supplement_relation_classifies_as_owned():
    http = FakeHttp(routes={
        "scholexplorer": json_response(scholix_page(
            [scholix_link("10.5061/dryad.abc", "Raw data",
                          subtype="IsSupplementTo", publisher="DRYAD")])),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)

    # The Dryad lookup 404s in this double, so no files -- but the link survives.
    assert files == []
    assert all(l["classified"] == "owned" for l in ctx.scratch[SCRATCH_KEY]["links"])


def test_scholix_arxiv_doi_is_not_routed():
    http = FakeHttp(routes={
        "scholexplorer": json_response(scholix_page(
            [scholix_link("10.48550/arxiv.1810.03292", "10.48550/ARXIV.1810.03292")])),
    })
    ctx = ctx_with(http)
    enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)
    assert "_routed_deposit_dois" not in ctx.scratch or \
        not ctx.scratch["_routed_deposit_dois"]


def test_scholix_empty_answer_still_writes_a_query_record():
    http = FakeHttp(routes={"scholexplorer": json_response(scholix_page([]))})
    ctx = ctx_with(http)
    files = enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx)

    assert files == []
    queried = ctx.scratch[SCRATCH_KEY]["queried"]
    assert len(queried) == 2  # dataset + software
    assert all(q["status"] == "ok" for q in queried)


def test_scholix_http_error_is_an_error_record_not_a_crash():
    http = FakeHttp(default=FakeResponse(status=503))
    ctx = ctx_with(http)
    assert enumerate_scholix_related(IdentifierSet(doi="10.1000/x"), ctx) == []
    assert all(q["status"] == "error" for q in ctx.scratch[SCRATCH_KEY]["queried"])


def test_scholix_without_a_doi_asks_nothing():
    http = FakeHttp()
    ctx = ctx_with(http)
    assert enumerate_scholix_related(IdentifierSet(pmid="123"), ctx) == []
    assert http.requests == []
    assert SCRATCH_KEY not in ctx.scratch


# --------------------------------------------------------------------------
# Europe PMC datalinks
# --------------------------------------------------------------------------


def test_epmc_without_a_pmid_asks_nothing():
    http = FakeHttp()
    ctx = ctx_with(http)
    assert enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x"), ctx) == []
    assert http.requests == []


def test_epmc_registry_biostudies_and_data_citation_each_go_where_they_belong():
    payload = epmc_payload([
        ("Clinical Trials", [epmc_link("NCT03020186", "ClinicalTrials.gov")]),
        ("BioStudies: supplemental material and supporting data",
         [epmc_link("http://www.ebi.ac.uk/biostudies/studies/S-EPMC1", "URL")]),
        ("Data Citations", [epmc_link("10.5281/zenodo.123", "DOI")]),
        ("Altmetric", [epmc_link("something", "URL")]),
    ])
    http = FakeHttp(routes={
        "datalinks": json_response(payload),
        "api.datacite.org/dois/": datacite_ownership("10.5281/zenodo.123", "10.1000/x"),
        "zenodo.org/api/records/123": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="38375968"), ctx)

    # Only the ownership-confirmed data-citation DOI produced files.
    assert [f.provider for f in files] == ["epmc_datalinks:zenodo_files"]

    links = ctx.scratch[SCRATCH_KEY]["links"]
    registry = [l for l in links if l["classified"] == "registry"]
    assert registry[0]["target_pid"] == "NCT03020186"
    assert registry[0]["routed_to_download"] is False
    routed = [l for l in links if l["routed_to_download"]]
    assert [l["target_pid"] for l in routed] == ["10.5281/zenodo.123"]
    assert all(l["classified"] == "owned" for l in routed)
    # Altmetric is dropped entirely, so exactly three links were recorded.
    assert len(links) == 3


def test_epmc_biostudies_is_never_downloaded():
    payload = epmc_payload([
        ("BioStudies: supplemental material and supporting data",
         [epmc_link("http://www.ebi.ac.uk/biostudies/studies/S-EPMC1", "URL")]),
    ])
    http = FakeHttp(routes={"datalinks": json_response(payload)})
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="1"), ctx)
    assert files == []
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "owned"
    assert link["routed_to_download"] is False


# --------------------------------------------------------------------------
# The Dryad and Dataverse enumerators
# --------------------------------------------------------------------------


def test_dryad_enumerator_lists_files_and_skips_deleted_ones():
    from fetchpdf.retrieval.supplement_index import enumerate_dryad

    http = FakeHttp(routes={
        "datadryad.org/api/v2/datasets": json_response(DRYAD_DATASET_PAYLOAD),
        "/api/v2/versions/126667/files": json_response(DRYAD_FILES_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_dryad(IdentifierSet(doi="10.5061/dryad.h44j0zpwg"), ctx)

    assert [f.name for f in files] == ["salt_tolerance_data.csv"]
    assert files[0].url == "https://datadryad.org/api/v2/files/772999/download"
    assert files[0].size_bytes == 51234
    assert files[0].checksum == "sha256:abc123"
    # The DOI was URL-encoded into the dataset lookup.
    assert any("doi%3A10.5061%2Fdryad.h44j0zpwg" in url for url, _ in http.requests)


def test_dryad_enumerator_is_not_applicable_to_other_dois():
    from fetchpdf.retrieval.supplement_index import enumerate_dryad

    http = FakeHttp()
    ctx = ctx_with(http)
    assert enumerate_dryad(IdentifierSet(doi="10.1371/journal.pone.1"), ctx) == []
    assert http.requests == []


def test_dataverse_enumerator_lists_files_and_skips_restricted_ones():
    from fetchpdf.retrieval.supplement_index import enumerate_dataverse

    http = FakeHttp(routes={
        "dataverse.harvard.edu/api/datasets": json_response(DATAVERSE_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_dataverse(IdentifierSet(doi="10.7910/DVN/27221"), ctx)

    assert [f.name for f in files] == ["rep_materials.zip"]
    assert files[0].url == "https://dataverse.harvard.edu/api/access/datafile/2498426"
    assert files[0].checksum == "md5:f57184de03766641c510cdc911d9fad1"


def test_replication_title_confirms_ownership_when_datacite_has_no_relations():
    """The Harvard Dataverse reality: no relatedIdentifiers at all, ownership
    declared only by the 'Replication Data for: <title>' convention. Verified
    on two live cerebrolysin deposits (DVN/DOFCIV, DVN/PCRGJE)."""
    payload = epmc_payload([
        ("Data Citations", [epmc_link("10.7910/DVN/27221", "DOI")]),
    ])
    http = FakeHttp(routes={
        "datalinks": json_response(payload),
        "api.datacite.org/dois/": json_response({"data": {"attributes": {
            "relatedIdentifiers": [],
            "titles": [{"title": "Replication Data for: Cerebrolysin and rTMS "
                                 "in patients: a three-arm randomized trial"}],
        }}}),
        "api.crossref.org/works/": json_response({"message": {
            "title": ["Cerebrolysin and rTMS in patients: "
                      "a three-arm randomized trial"]}}),
        "dataverse.harvard.edu/api/datasets": json_response(DATAVERSE_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="1"), ctx)
    assert [f.provider for f in files] == ["epmc_datalinks:dataverse_files"]
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "owned"


def test_british_spelling_in_a_deposit_title_is_still_ownership():
    """The real DVN/Z8WAKW miss: the deposit says "randomised", the journal
    says "Randomized", and exact containment drops a trial's own replication
    data over one letter after 108 characters of agreement."""
    payload = epmc_payload([
        ("Data Citations", [epmc_link("10.7910/DVN/27221", "DOI")]),
    ])
    http = FakeHttp(routes={
        "datalinks": json_response(payload),
        "api.datacite.org/dois/": json_response({"data": {"attributes": {
            "relatedIdentifiers": [],
            "titles": [{"title": "Replication Data for: Speech therapy combined "
                                 "with Cerebrolysin in enhancing non-fluent aphasia "
                                 "recovery after acute ischemic stroke: ESCAS "
                                 "randomised pilot study"}],
        }}}),
        "api.crossref.org/works/": json_response({"message": {
            "title": ["Speech Therapy Combined With Cerebrolysin in Enhancing "
                      "Nonfluent Aphasia Recovery After Acute Ischemic Stroke: "
                      "ESCAS Randomized Pilot Study"]}}),
        "dataverse.harvard.edu/api/datasets": json_response(DATAVERSE_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="1"), ctx)
    assert [f.provider for f in files] == ["epmc_datalinks:dataverse_files"]
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "owned"


def test_replication_title_for_a_different_paper_is_not_ownership():
    payload = epmc_payload([
        ("Data Citations", [epmc_link("10.7910/DVN/27221", "DOI")]),
    ])
    http = FakeHttp(routes={
        "datalinks": json_response(payload),
        "api.datacite.org/dois/": json_response({"data": {"attributes": {
            "relatedIdentifiers": [],
            "titles": [{"title": "Replication Data for: An entirely different "
                                 "study about turtles"}],
        }}}),
        "api.crossref.org/works/": json_response({"message": {
            "title": ["Cerebrolysin and rTMS in patients"]}}),
        "dataverse.harvard.edu/api/datasets": json_response(DATAVERSE_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="1"), ctx)
    assert files == []
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "related"
    assert not any("dataverse.harvard.edu" in url for url, _ in http.requests)


def test_epmc_data_citation_routes_into_dataverse():
    payload = epmc_payload([
        ("Data Citations", [epmc_link("10.7910/DVN/27221", "DOI")]),
    ])
    http = FakeHttp(routes={
        "datalinks": json_response(payload),
        "api.datacite.org/dois/": datacite_ownership("10.7910/dvn/27221", "10.1000/x"),
        "dataverse.harvard.edu/api/datasets": json_response(DATAVERSE_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(IdentifierSet(doi="10.1000/x", pmid="1"), ctx)
    assert [f.provider for f in files] == ["epmc_datalinks:dataverse_files"]


# --------------------------------------------------------------------------
# Cross-provider dedup
# --------------------------------------------------------------------------


def test_the_same_deposit_is_listed_once_across_link_providers():
    """Scholix and EPMC both name the same Zenodo record; one listing call."""
    http = FakeHttp(routes={
        "targetType=dataset": json_response(scholix_page(
            [scholix_link("10.5281/zenodo.123", "Study data")])),
        "targetType=software": json_response(scholix_page([])),
        "datalinks": json_response(epmc_payload(
            [("Data Citations", [epmc_link("10.5281/zenodo.123", "DOI")])])),
        "api.datacite.org/dois/": datacite_ownership("10.5281/zenodo.123", "10.1000/x"),
        "zenodo.org/api/records/123": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    ids = IdentifierSet(doi="10.1000/x", pmid="1")

    first = enumerate_scholix_related(ids, ctx)
    second = enumerate_epmc_datalinks(ids, ctx)

    assert len(first) == 1 and second == []
    zenodo_calls = [url for url, _ in http.requests if "zenodo.org" in url]
    assert len(zenodo_calls) == 1
    # Both links are still in the sidecar record -- dedup is about downloads.
    assert len(ctx.scratch[SCRATCH_KEY]["links"]) == 2


# --------------------------------------------------------------------------
# The sidecar
# --------------------------------------------------------------------------


def test_no_link_provider_ran_means_no_sidecar(tmp_path):
    ctx = ctx_with(FakeHttp())
    stem = str(tmp_path / "10.1000--x")
    assert write_linked_sidecar(stem, IdentifierSet(doi="10.1000/x"), ctx) is None
    assert not os.path.exists(sidecar_path_for(stem))


def test_sidecar_records_the_empty_answer(tmp_path):
    http = FakeHttp(routes={"scholexplorer": json_response(scholix_page([]))})
    ctx = ctx_with(http)
    ids = IdentifierSet(doi="10.1000/x")
    enumerate_scholix_related(ids, ctx)

    stem = str(tmp_path / "10.1000--x")
    path = write_linked_sidecar(stem, ids, ctx)
    assert path == sidecar_path_for(stem)

    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    assert record["counts"] == {"links": 0, "routed_to_download": 0}
    assert len(record["queried"]) == 2
    assert record["identifier"] == "10.1000/x"


def test_sidecar_counts_routed_links(tmp_path):
    http = FakeHttp(routes={
        "scholexplorer": json_response(scholix_page(
            [scholix_link("10.5281/zenodo.123", "Study data")])),
        "api.datacite.org/dois/": datacite_ownership("10.5281/zenodo.123", "10.1000/x"),
        "zenodo.org/api/records/123": json_response(ZENODO_LISTING),
    })
    ctx = ctx_with(http)
    ids = IdentifierSet(doi="10.1000/x")
    enumerate_scholix_related(ids, ctx)

    path = write_linked_sidecar(str(tmp_path / "10.1000--x"), ids, ctx)
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    assert record["counts"]["links"] >= 1
    assert record["counts"]["routed_to_download"] == 1


def test_sidecar_write_failure_returns_none_rather_than_raising():
    http = FakeHttp(routes={"scholexplorer": json_response(scholix_page([]))})
    ctx = ctx_with(http)
    ids = IdentifierSet(doi="10.1000/x")
    enumerate_scholix_related(ids, ctx)
    # A stem whose directory cannot be created.
    impossible = os.path.join(os.devnull, "sub", "stem")
    assert write_linked_sidecar(impossible, ids, ctx) is None


# --------------------------------------------------------------------------
# Against the recorded API responses -- what the services actually send,
# not what we assume they send. Recaptured by tests/fixtures/capture.py.
# --------------------------------------------------------------------------

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def test_recorded_scholix_dataset_link_routes_into_the_dryad_enumerator():
    """The microbiome paper's Dryad deposit: relation 'cites', so a relation
    whitelist would drop it -- but its DataCite record names the article (the
    datacite_reverse_related fixture is the same deposit found from the other
    direction), so the ownership gate passes and the link resolves all the way
    to a downloadable file listing."""
    http = FakeHttp(routes={
        "targetType=dataset": json_response(_fixture("scholix_links_dataset.json")),
        "targetType=software": json_response(scholix_page([])),
        "api.datacite.org/dois/": datacite_ownership(
            "10.5061/dryad.h44j0zpwg", "10.1186/s40168-025-02261-0"),
        "datadryad.org/api/v2/datasets": json_response(DRYAD_DATASET_PAYLOAD),
        "/api/v2/versions/126667/files": json_response(DRYAD_FILES_PAYLOAD),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(
        IdentifierSet(doi="10.1186/s40168-025-02261-0"), ctx)

    assert [f.provider for f in files] == ["scholix_related:dryad_files"]
    assert files[0].url.startswith("https://datadryad.org/api/v2/files/")
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["target_pid"] == "10.5061/dryad.h44j0zpwg"
    assert link["classified"] == "owned"
    assert link["routed_to_download"] is True


def test_recorded_scholix_software_link_is_a_tool_citation():
    """The NumPy paper 'cites' a 'numpy software on GitHub' record -- no DOI at
    all, just OpenAIRE identifiers -- and must stay out of the download path."""
    http = FakeHttp(routes={
        "targetType=software": json_response(_fixture("scholix_links_software.json")),
        "targetType=dataset": json_response(scholix_page([])),
    })
    ctx = ctx_with(http)
    files = enumerate_scholix_related(
        IdentifierSet(doi="10.1038/s41586-020-2649-2"), ctx)

    assert files == []
    (link,) = ctx.scratch[SCRATCH_KEY]["links"]
    assert link["classified"] == "tool_citation"
    assert link["target_pid"] is None


def test_recorded_epmc_datalinks_classify_without_downloading_anything():
    """PMID 38375968: one NCT accession, five DOI data citations (three of them
    arXiv papers wearing a dataset costume), one BioStudies study URL."""
    http = FakeHttp(routes={"datalinks": json_response(_fixture("epmc_datalinks.json"))})
    ctx = ctx_with(http)
    files = enumerate_epmc_datalinks(
        IdentifierSet(doi="10.1002/hbm.26595", pmid="38375968"), ctx)

    # Nothing here lives in a repository we can enumerate.
    assert files == []
    links = ctx.scratch[SCRATCH_KEY]["links"]
    classes = sorted(l["classified"] for l in links)
    assert "registry" in classes
    registry = [l for l in links if l["classified"] == "registry"]
    assert registry[0]["target_pid"].startswith("NCT")
    arxiv = [l for l in links if (l["target_pid"] or "").startswith("10.48550")]
    assert arxiv and all(l["routed_to_download"] is False for l in arxiv)
    assert all(l["routed_to_download"] is False for l in links)


class TestRoutingPrecision:
    """Index routes must answer to the same standard as fulltext_scan.

    Every case is a real deposit from the nina_mazar corpus, labelled by
    checking its DataCite/Crossref record by hand. The two failures below were
    live: a PsyArXiv preprint and a 2006 commitment-savings dataset were both
    downloaded and filed under papers they do not belong to.
    """

    def _ctx(self, unverified=False, relations=None):
        """A ctx whose DataCite lookups are stubbed, so no network is touched."""
        import json as _json

        class _Resp:
            ok = True

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class _Http:
            def get(_self, url, **kw):
                return _Resp({"data": {"attributes": {
                    "relatedIdentifiers": relations or []}}})

        class _Ctx:
            download_data_artifacts = True
            download_related_unverified = unverified
            verbose = False

            def __init__(self):
                self.scratch = {}
                self.http = _Http()

            def log(self, message):
                pass

        return _Ctx()

    def _ids(self, doi):
        from fetchpdf.retrieval.identifiers import IdentifierSet
        return IdentifierSet(doi=doi)

    def test_preprint_dois_are_never_routed(self):
        """10.31234/osf.io/8r9p7 embeds "osf.io" and IS a paper, not a deposit."""
        from fetchpdf.retrieval.supplement_graph import _PAPER_DOI_PREFIXES
        for doi in ("10.31234/osf.io/8r9p7", "10.48550/arXiv.2401.00001",
                    "10.1101/2023.01.01.522000", "10.2139/ssrn.1234567"):
            assert doi.startswith(_PAPER_DOI_PREFIXES), doi

    def test_a_deposit_claiming_another_article_is_refused(self):
        """The Odysseus case: IsSupplementTo a DIFFERENT paper's DOI."""
        from fetchpdf.retrieval.linked_artifacts import CLASS_RELATED
        from fetchpdf.retrieval.supplement_graph import _should_route
        ctx = self._ctx(relations=[
            {"relationType": "IsSupplementTo",
             "relatedIdentifier": "10.1162/qjec.2006.121.2.635"}])
        assert not _should_route(CLASS_RELATED, ctx, "10.7910/dvn/27854",
                                 self._ids("10.1093/pnasnexus/pgaf280"))

    def test_a_deposit_with_no_relations_is_still_routed(self):
        """Harvard Dataverse fills in no relatedIdentifiers at all.

        Absence of a claim is not a claim to the contrary -- requiring the
        deposit to NAME this article refused dvn/8dkfyo, which is genuinely
        the paper's data.
        """
        from fetchpdf.retrieval.linked_artifacts import CLASS_RELATED
        from fetchpdf.retrieval.supplement_graph import _should_route
        ctx = self._ctx(relations=[])
        assert _should_route(CLASS_RELATED, ctx, "10.7910/dvn/8dkfyo",
                             self._ids("10.1038/s41562-024-02009-0"))

    def test_a_deposit_related_only_to_a_project_is_routed(self):
        """Zenodo's MegaOath declares IsSupplementTo an OSF PROJECT, not a paper.

        A deposit pointing at a project page or its own earlier version has not
        named another article, so it must not be refused on that basis.
        """
        from fetchpdf.retrieval.linked_artifacts import CLASS_RELATED
        from fetchpdf.retrieval.supplement_graph import _should_route
        ctx = self._ctx(relations=[
            {"relationType": "IsSupplementTo",
             "relatedIdentifier": "10.17605/OSF.IO/T3SM4"},
            {"relationType": "IsVersionOf",
             "relatedIdentifier": "10.5281/zenodo.10777159"}])
        assert _should_route(CLASS_RELATED, ctx, "10.5281/zenodo.12071155",
                             self._ids("10.1038/s41562-024-02009-0"))

    def test_owned_links_route_without_any_lookup(self):
        from fetchpdf.retrieval.linked_artifacts import CLASS_OWNED
        from fetchpdf.retrieval.supplement_graph import _should_route
        assert _should_route(CLASS_OWNED, self._ctx(), "10.5281/zenodo.1",
                             self._ids("10.1000/x"))

    def test_registry_links_never_route(self):
        from fetchpdf.retrieval.linked_artifacts import CLASS_REGISTRY
        from fetchpdf.retrieval.supplement_graph import _should_route
        assert not _should_route(CLASS_REGISTRY, self._ctx(), "10.5281/zenodo.1",
                                 self._ids("10.1000/x"))

    def test_nothing_related_routes_without_the_flag(self):
        from fetchpdf.retrieval.linked_artifacts import CLASS_RELATED
        from fetchpdf.retrieval.supplement_graph import _should_route
        ctx = self._ctx()
        ctx.download_data_artifacts = False
        assert not _should_route(CLASS_RELATED, ctx, "10.5281/zenodo.1",
                                 self._ids("10.1000/x"))

    def test_the_escape_hatch_restores_the_permissive_behaviour(self):
        from fetchpdf.retrieval.linked_artifacts import CLASS_RELATED
        from fetchpdf.retrieval.supplement_graph import _should_route
        ctx = self._ctx(unverified=True, relations=[
            {"relationType": "IsSupplementTo",
             "relatedIdentifier": "10.1162/qjec.2006.121.2.635"}])
        assert _should_route(CLASS_RELATED, ctx, "10.7910/dvn/27854",
                             self._ids("10.1093/pnasnexus/pgaf280"))


# --------------------------------------------------------------------------
# Repository bot-challenges: an empty HTTP 202 is not an empty deposit
#
# Harvard Dataverse behind AWS WAF answers 202 with no body on every endpoint,
# including the site root. `ok` is true for 202, the JSON parse fails, and the
# enumerator's bare `return []` then reads as "this deposit has no files" --
# which is how a trial's own replication data was skipped while the manifest
# recorded none_found. Measured 2026-08-12 on 10.7910/DVN/Z8WAKW.
# --------------------------------------------------------------------------


def test_an_empty_202_is_recognised_as_a_challenge_not_an_answer():
    from fetchpdf.retrieval.repository_waf import looks_like_challenge
    assert looks_like_challenge(b"", 202)


def test_an_aws_waf_interstitial_is_recognised_by_its_markers():
    from fetchpdf.retrieval.repository_waf import looks_like_challenge
    body = b"<html><script>window.awsWafCookieDomainList = []; window.gokuProps = {}</script>"
    assert looks_like_challenge(body, 200)


def test_a_genuine_empty_body_at_200_is_not_a_challenge():
    """Only 202 and the WAF markers count; a real empty 200 must stay a real answer."""
    from fetchpdf.retrieval.repository_waf import looks_like_challenge
    assert not looks_like_challenge(b"", 200)
    assert not looks_like_challenge(b'{"data": {}}', 200)


def test_only_known_waf_hosts_get_the_browser_fallback():
    from fetchpdf.retrieval.repository_waf import host_uses_waf
    assert host_uses_waf("https://dataverse.harvard.edu/api/datasets/x")
    assert host_uses_waf("https://zenodo.org/api/records/1") is None


def test_dataverse_challenge_triggers_the_browser_fallback(monkeypatch):
    """The 202 must reach the fallback, and its payload must be enumerated."""
    from fetchpdf.retrieval import supplement_index
    from fetchpdf.retrieval.supplement_index import enumerate_dataverse

    called = {}

    def fake_browser(api_url, doi, ctx):
        called["api_url"] = api_url
        called["doi"] = doi
        return {"data": {"latestVersion": {"files": [
            {"dataFile": {"id": 7, "filename": "ESCAS.xlsx",
                          "filesize": 38161, "md5": "ce11dd01137981eb99604e423e529c12"}},
        ]}}}

    monkeypatch.setattr("fetchpdf.retrieval.repository_waf.fetch_json_through_browser",
                        fake_browser)
    http = FakeHttp(default=FakeResponse(status=202, content=b""))
    ctx = ctx_with(http)
    files = enumerate_dataverse(IdentifierSet(doi="10.7910/DVN/Z8WAKW"), ctx)

    # Casing is preserved, not normalised: Dataverse persistent IDs are
    # case-sensitive, and the landing URL is built from this value.
    assert called["doi"] == "10.7910/DVN/Z8WAKW"
    assert [f.name for f in files] == ["ESCAS.xlsx"]


def test_a_dataverse_404_does_not_launch_a_browser(monkeypatch):
    """The fallback is for challenges only -- a real 404 must stay cheap."""
    from fetchpdf.retrieval.supplement_index import enumerate_dataverse

    def explode(*a, **k):
        raise AssertionError("browser must not be launched for a 404")

    monkeypatch.setattr("fetchpdf.retrieval.repository_waf.fetch_json_through_browser",
                        explode)
    http = FakeHttp(default=FakeResponse(status=404, content=b"nope"))
    assert enumerate_dataverse(IdentifierSet(doi="10.7910/DVN/Z8WAKW"), ctx_with(http)) == []
