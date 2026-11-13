"""Repository enumerators reached through a deposit DOI: figshare portals and
collections, Mendeley Data, PANGAEA, 4TU and Science Data Bank.

Fixtures under tests/fixtures/repos are trimmed copies of live API responses
captured 2026-11-13.
"""
import json
import os
from types import SimpleNamespace

import pytest

from fetchpdf.retrieval import supplement_graph as graph
from fetchpdf.retrieval import supplement_repos as repos
from fetchpdf.retrieval.supplement_index import ROLE_SUPPLEMENT, enumerate_figshare

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "repos")


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
    """Answers by exact URL; a callable answer receives the request params."""

    def __init__(self, routes=None, posts=None):
        self.routes, self.posts = routes or {}, posts
        self.gets, self.posted = [], []

    def get(self, url, params=None, headers=None, **kwargs):
        self.gets.append((url, dict(params or {}), dict(headers or {})))
        answer = self.routes.get(url)
        if callable(answer):
            answer = answer(dict(params or {}))
        return Response(answer) if answer is not None else Response(ok=False)

    def post_json(self, url, payload, **kwargs):
        self.posted.append((url, dict(payload)))
        answer = self.posts(payload) if self.posts else None
        return Response(answer) if answer is not None else Response(ok=False)


def context(http):
    logs = []
    return SimpleNamespace(http=http, scratch={}, log=logs.append, logs=logs)


def ids(doi):
    return SimpleNamespace(doi=doi)


# -- routing ------------------------------------------------------------------


@pytest.mark.parametrize("doi, provider", [
    ("10.17632/tcpb72scrs.1", "mendeley_files"),
    ("10.1594/PANGAEA.962980", "pangaea_files"),
    ("10.4121/2702bc54-def0-4daf-8cd0-7f8e85453616.v1", "fourtu_files"),
    ("10.4121/uuid:a165560f-0000-0000-0000-000000000000", "fourtu_files"),
    ("10.57760/sciencedb.018bv", "sciencedb_files"),
    ("10.11922/sciencedb.00001", "sciencedb_files"),
    ("10.25387/g3.8230757.v1", "figshare_files"),
    ("10.25386/genetics.13132976", "figshare_files"),
    ("10.15131/shef.data.34975381.v2", "figshare_files"),
    ("10.5522/04/32676582", "figshare_files"),
    ("10.6084/m9.figshare.c.6436802.v1", "figshare_files"),
    ("10.25452/figshare.plus.30694124", "figshare_files"),
    # No enumerator, on purpose: see the supplement_repos docstring.
    ("10.25384/sage.26786716", None),
    ("10.3886/e114599v1", None),
    ("10.1594/gfz.sddb.1234", None),          # 10.1594, but not PANGAEA
])
def test_deposit_dois_route_to_their_repository(doi, provider):
    assert graph._repo_provider(doi) == provider
    assert graph._has_repo_enumerator(doi) is (provider is not None)


def test_routed_files_are_tagged_with_route_and_origin():
    doi = "10.17632/2ysyhzww6t.2"
    http = Http({"https://data.mendeley.com/public-api/datasets/2ysyhzww6t":
                 load("mendeley_dataset.json")})
    files = graph._files_in_repository(doi, ids(doi), context(http))
    assert [(f.provider, f.origin_doi, f.role) for f in files] == \
        [("datacite_related:mendeley_files", doi, ROLE_SUPPLEMENT)]


def test_unknown_prefix_routes_by_its_figshare_landing_page():
    """A portal whose prefix is not in the table is known by where DataCite
    says it lives -- but never SAGE's, which the public API does not serve."""
    article = load("figshare_portal_article.json")
    doi = "10.99999/unlisted.8230757"
    article["doi"] = doi
    http = Http({"https://api.figshare.com/v2/articles/8230757": article})
    ctx = context(http)
    assert graph._files_in_repository(doi, ids(doi), ctx) == []        # no hint yet
    ctx = context(http)
    ctx.scratch["_deposit_landing_urls"] = {
        doi: "https://unlisted.figshare.com/articles/dataset/x/8230757"}
    assert len(graph._files_in_repository(doi, ids(doi), ctx)) == 12

    sage = "10.25384/sage.26786716"
    ctx = context(Http())
    ctx.scratch["_deposit_landing_urls"] = {sage: "https://sage.figshare.com/articles/x/26786716"}
    assert graph._files_in_repository(sage, ids(sage), ctx) == []
    assert ctx.http.gets == []


# -- figshare portals and collections ----------------------------------------


def test_figshare_portal_item_lists_inline_files():
    doi = "10.25387/g3.8230757.v1"
    http = Http({"https://api.figshare.com/v2/articles/8230757/versions/1":
                 load("figshare_portal_article.json")})
    files = repos.enumerate_figshare_deposit(ids(doi), context(http))
    assert len(files) == 12
    first = files[0]
    assert first.name == "Figure_S11.pdf"
    assert first.url == "https://ndownloader.figshare.com/files/15512459"
    assert first.size_bytes == 433989 and first.checksum.startswith("md5:")
    assert [f.listing_index for f in files] == list(range(12))


def test_figshare_portal_item_must_name_the_doi_back():
    """The id is only a trailing number; another deposit's article with the
    same number must not lend its files."""
    article = load("figshare_portal_article.json")       # doi 10.25387/g3.8230757.v1
    http = Http({"https://api.figshare.com/v2/articles/8230757": article})
    assert repos.enumerate_figshare_deposit(
        ids("10.15131/shef.data.8230757"), context(http)) == []
    assert len(repos.enumerate_figshare_deposit(
        ids("10.25387/g3.8230757"), context(http))) == 12   # version-insensitive


@pytest.mark.parametrize("flag", ["is_embargoed", "is_confidential", "is_metadata_record"])
def test_figshare_shaped_restricted_items_list_nothing(flag):
    article = load("figshare_portal_article.json")
    article[flag] = True
    assert repos._figshare_shaped_files(article, "figshare_files", "10.1/x") == []


def test_figshare_shaped_skips_links_and_incomplete_uploads():
    article = {"files": [
        {"name": "a.csv", "download_url": "https://x/1", "size": 5, "computed_md5": "aa"},
        {"name": "site", "download_url": "https://x/2", "is_link_only": True},
        {"name": "half", "download_url": "https://x/3", "is_incomplete": True},
        {"name": "nourl"},
    ]}
    files = repos._figshare_shaped_files(article, "fourtu_files", "10.1/x")
    assert [(f.name, f.checksum) for f in files] == [("a.csv", "md5:aa")]


def test_figshare_collection_lists_every_item_once():
    doi = "10.6084/m9.figshare.c.6436802.v1"
    items = load("figshare_collection_articles.json")
    routes = {"https://api.figshare.com/v2/collections/6436802/articles": items}
    for n, item in enumerate(items):
        routes[f"https://api.figshare.com/v2/articles/{item['id']}"] = {
            "id": item["id"], "doi": item["doi"],
            "files": [{"name": f"item{n}.csv", "download_url": f"https://nd/{n}",
                       "size": 10 + n, "computed_md5": f"{n:032x}"}]}
    ctx = context(Http(routes))
    files = graph._files_in_repository(doi, ids(doi), ctx)
    assert [f.name for f in files] == ["item0.csv", "item1.csv", "item2.csv"]
    assert {f.origin_doi for f in files} == {doi}
    assert [f.listing_index for f in files] == [0, 1, 2]
    # The items' own DOIs are now spoken for, with and without the version.
    requests_before = len(ctx.http.gets)
    for item in items:
        versioned = item["doi"].lower()
        assert graph._files_in_repository(versioned, ids(versioned), ctx) == []
        base = versioned.rsplit(".v", 1)[0]
        assert graph._files_in_repository(base, ids(base), ctx) == []
    assert len(ctx.http.gets) == requests_before


def test_plain_figshare_item_still_uses_the_original_enumerator(monkeypatch):
    called = []
    monkeypatch.setattr(repos, "enumerate_figshare",
                        lambda i, ctx: called.append(i.doi) or [])
    repos.enumerate_figshare_deposit(ids("10.6084/m9.figshare.34735259.v2"), context(Http()))
    assert called == ["10.6084/m9.figshare.34735259.v2"]


def test_figshare_file_listing_reads_past_the_first_ten():
    """/files serves 10 entries unless page_size is given."""
    def page(params):
        assert params["page_size"] == 100
        count = 100 if params["page"] == 1 else 21
        start = (params["page"] - 1) * 100
        return [{"name": f"f{start + n}.csv", "download_url": f"https://nd/{start + n}",
                 "size": 1} for n in range(count)]
    http = Http({"https://api.figshare.com/v2/articles/34975381/files": page})
    files = enumerate_figshare(ids("10.6084/m9.figshare.34975381"), context(http),
                               with_item_type=False)
    assert len(files) == 121 and files[-1].name == "f120.csv"
    assert [params["page"] for _, params, _ in http.gets] == [1, 2]


# -- 4TU ------------------------------------------------------------------------


def test_4tu_uuid_item():
    doi = "10.4121/2702bc54-def0-4daf-8cd0-7f8e85453616.v1"
    http = Http({"https://data.4tu.nl/v2/articles/2702bc54-def0-4daf-8cd0-7f8e85453616/versions/1":
                 load("fourtu_article.json")})
    files = repos.enumerate_4tu(ids(doi), context(http))
    assert [(f.name, f.size_bytes, f.checksum) for f in files] == [
        ("c3c3b.tar.gz", 12263679427, "md5:3b79f46b62578a18699b7abd7e2ad65f")]
    assert files[0].url.startswith("https://data.4tu.nl/file/2702bc54-")
    assert files[0].provider == "fourtu_files"


def test_4tu_legacy_uuid_doi_is_resolved_through_the_doi_lookup():
    doi = "10.4121/uuid:a165560f-1111-2222-3333-444444444444"
    article = load("fourtu_article.json")
    http = Http({
        "https://data.4tu.nl/v2/articles":
            lambda params: [{"uuid": "abc"}] if params == {"doi": doi} else [],
        "https://data.4tu.nl/v2/articles/abc": article,
    })
    assert len(repos.enumerate_4tu(ids(doi.upper()), context(http))) == 1
    assert repos.enumerate_4tu(ids("10.4121/uuid:unknown"), context(http)) == []


def test_4tu_ignores_other_registrants():
    assert repos.enumerate_4tu(ids("10.5281/zenodo.12345"), context(Http())) == []


# -- Mendeley Data ------------------------------------------------------------

MENDELEY = "https://data.mendeley.com/public-api/datasets/2ysyhzww6t"


def test_mendeley_latest_version_is_one_request():
    http = Http({MENDELEY: load("mendeley_dataset.json")})
    for doi in ("10.17632/2ysyhzww6t.2", "10.17632/2ysyhzww6t"):
        http.gets.clear()
        files = repos.enumerate_mendeley(ids(doi), context(http))
        assert [(f.name, f.size_bytes) for f in files] == [("green_mbpt_v1.0.0.zip", 47145476)]
        assert files[0].checksum == \
            "sha256:6861773e41f632e27409f923e771c458efb764a99fcd7cca56e03197f7c9e90a"
        assert files[0].url.endswith("/file_downloaded")
        assert len(http.gets) == 1


def test_mendeley_older_version_walks_root_and_folders():
    listing = load("mendeley_files_version.json")
    folders = load("mendeley_folders.json")
    asked = []

    def files(params):
        asked.append((params["folder_id"], params["version"]))
        return listing if params["folder_id"] == "root" else []

    http = Http({MENDELEY: load("mendeley_dataset.json"),      # latest is 2
                 MENDELEY + "/files": files,
                 MENDELEY + "/folders/1": folders})
    found = repos.enumerate_mendeley(ids("10.17632/2ysyhzww6t.1"), context(http))
    assert len(found) == len(listing)
    assert asked == [("root", "1")] + [(folder["id"], "1") for folder in folders]


def test_mendeley_confidential_or_missing_dataset_lists_nothing():
    dataset = load("mendeley_dataset.json")
    dataset["confidential"] = True
    assert repos.enumerate_mendeley(ids("10.17632/2ysyhzww6t"),
                                    context(Http({MENDELEY: dataset}))) == []
    assert repos.enumerate_mendeley(ids("10.17632/2ysyhzww6t"), context(Http())) == []


def test_mendeley_error_body_with_http_200_is_not_a_file_list():
    http = Http({MENDELEY: load("mendeley_dataset.json"),
                 MENDELEY + "/files": {"error": 400},
                 MENDELEY + "/folders/1": {"error": 400}})
    assert repos.enumerate_mendeley(ids("10.17632/2ysyhzww6t.1"), context(http)) == []


# -- PANGAEA ------------------------------------------------------------------


def pangaea(number, fixture):
    return Http({f"https://doi.pangaea.de/10.1594/PANGAEA.{number}": load(fixture)})


def test_pangaea_tabular_dataset():
    http = pangaea("962980", "pangaea_962980.json")
    files = repos.enumerate_pangaea(ids("10.1594/PANGAEA.962980"), context(http))
    assert [(f.name, f.url, f.mimetype) for f in files] == [(
        "PANGAEA_962980.tab",
        "https://doi.pangaea.de/10.1594/PANGAEA.962980?format=textfile",
        "text/tab-separated-values")]
    assert files[0].size_bytes is None and files[0].checksum is None
    assert http.gets[0][2] == {"Accept": "application/ld+json"}


def test_pangaea_parent_series_is_a_zip():
    files = repos.enumerate_pangaea(ids("10.1594/pangaea.910198"),
                                    context(pangaea("910198", "pangaea_910198_parent.json")))
    assert [(f.name, f.url) for f in files] == [
        ("PANGAEA_910198.zip", "https://doi.pangaea.de/10.1594/PANGAEA.910198?format=zip")]


def test_pangaea_binary_file_keeps_its_name():
    files = repos.enumerate_pangaea(ids("10.1594/PANGAEA.995426"),
                                    context(pangaea("995426", "pangaea_binary.json")))
    assert [(f.name, f.url) for f in files] == [(
        "mwr_processed_2025.zip",
        "https://download.pangaea.de/dataset/995426/files/mwr_processed_2025.zip")]


def test_pangaea_dataset_under_moratorium_is_skipped():
    ctx = context(pangaea("996035", "pangaea_996035.json"))
    assert repos.enumerate_pangaea(ids("10.1594/PANGAEA.996035"), ctx) == []
    assert any("needs access rights" in line for line in ctx.logs)


def test_pangaea_single_distribution_object_and_other_10_1594_dois():
    record = load("pangaea_910198_parent.json")
    record["distribution"] = record["distribution"][0]
    http = Http({"https://doi.pangaea.de/10.1594/PANGAEA.910198": record})
    assert len(repos.enumerate_pangaea(ids("10.1594/PANGAEA.910198"), context(http))) == 1
    assert repos.enumerate_pangaea(ids("10.1594/gfz.sddb.1234"), context(http)) == []


# -- Science Data Bank --------------------------------------------------------

SCIDB_DOI = "10.57760/sciencedb.018bv"
SCIDB_ID = "eb551586f6154b4f9968eb4a6ad97dac"
SCIDB_DATACITE = "https://api.datacite.org/dois/10.57760%2Fsciencedb.018bv"


def scidb_entry(name, path, index, is_dir=False, **extra):
    return {"id": f"id{index}{name}", "fileName": name, "path": path, "dir": is_dir,
            "link": False, "size": 100 + index, "md5": f"{index:032x}", **extra}


def test_sciencedb_lists_the_version_root_from_the_live_capture():
    listing = load("sciencedb_listing.json")
    http = Http({SCIDB_DATACITE: load("sciencedb_datacite.json")},
                posts=lambda payload: listing if payload["lastIndex"] == 0 else None)
    files = repos.enumerate_sciencedb(ids(SCIDB_DOI), context(http))
    assert http.posted[0] == (repos.SCIDB_LISTING, {
        "dataSetId": SCIDB_ID, "version": "V1", "path": "/V1",
        "lastIndex": 0, "pageSize": 100})
    assert [f.name for f in files][0] == "2025_12_05_00_00.tar.gz"
    first = files[0]
    assert first.size_bytes == 5262515423
    assert first.checksum == "md5:874898749943a9c50bab343c11bcb170"
    assert first.url == ("https://www.scidb.cn/api/sdb-download-service/"
                         "downloadFileMole?fileId=8ab5de6379a5b9934a91844a041ca608")
    assert first.provider == "sciencedb_files"


def test_sciencedb_pages_and_walks_folders():
    root = [scidb_entry(f"f{n}.csv", f"/V2/f{n}.csv", n) for n in range(100)]
    more = [scidb_entry("last.csv", "/V2/last.csv", 100),
            scidb_entry("sub", "/V2/sub", 101, is_dir=True),
            scidb_entry("away", "/V2/away", 102, link=True)]
    sub = [scidb_entry("deep.csv", "/V2/sub/deep.csv", 0)]

    def posts(payload):
        assert payload["version"] == "V2"
        if payload["path"] == "/V2":
            data = root if payload["lastIndex"] == 0 else more if payload["lastIndex"] == 100 else []
        else:
            data = sub if payload["lastIndex"] == 0 else []
        return {"code": 20000, "data": data}

    datacite = load("sciencedb_datacite.json")
    datacite["data"]["attributes"]["version"] = "2.0.0"
    http = Http({SCIDB_DATACITE: datacite}, posts=posts)
    files = repos.enumerate_sciencedb(ids(SCIDB_DOI), context(http))
    names = [f.name for f in files]
    assert len(names) == 102 and names[100:] == ["last.csv", "sub/deep.csv"]
    assert [p["path"] for _, p in http.posted] == ["/V2", "/V2", "/V2/sub"]


def test_sciencedb_refusals_list_nothing():
    datacite = {SCIDB_DATACITE: load("sciencedb_datacite.json")}
    denied = Http(datacite, posts=lambda payload: {"code": 70001, "data": []})
    assert repos.enumerate_sciencedb(ids(SCIDB_DOI), context(denied)) == []
    assert repos.enumerate_sciencedb(ids(SCIDB_DOI), context(Http())) == []   # no DataCite record
    no_id = {SCIDB_DATACITE: {"data": {"attributes": {"url": "https://www.scidb.cn/en"}}}}
    assert repos.enumerate_sciencedb(ids(SCIDB_DOI), context(Http(no_id))) == []
    assert repos.enumerate_sciencedb(ids("10.57760/other.1"), context(Http())) == []


# -- failure handling ---------------------------------------------------------


@pytest.mark.parametrize("enumerator, doi", [
    (repos.enumerate_figshare_deposit, "10.25387/g3.8230757"),
    (repos.enumerate_figshare_deposit, "10.6084/m9.figshare.c.6436802"),
    (repos.enumerate_4tu, "10.4121/12826154.v1"),
    (repos.enumerate_mendeley, "10.17632/2ysyhzww6t.1"),
    (repos.enumerate_pangaea, "10.1594/PANGAEA.962980"),
    (repos.enumerate_sciencedb, SCIDB_DOI),
])
def test_enumerators_never_raise_into_the_supplementary_pass(enumerator, doi):
    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")
    ctx = SimpleNamespace(http=SimpleNamespace(get=boom, post_json=boom),
                          scratch={}, log=lambda message: None)
    assert enumerator(ids(doi), ctx) == []
    assert enumerator(ids(doi), context(Http())) == []        # every request 404s
    assert enumerator(ids(""), context(Http())) == []
