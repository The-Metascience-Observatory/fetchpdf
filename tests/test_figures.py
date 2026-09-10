"""Offline tests for --pull-figures.

No network. The doubles follow tests/test_supplementary.py: canned responses
replayed by URL substring, a fake resolver handing back a fixed IdentifierSet,
and a download double that writes real bytes so the manifest's hashes and sizes
are the ones a real transfer would have produced.

The load-bearing tests here:

  test_the_mirror_is_preferred_and_marked_original -- why there are two routes
  test_a_cdn_copy_is_marked_render                 -- and why they are labelled
  test_a_manifest_is_written_with_zero_figures     -- "asked and got nothing" is
                                                      not "never asked"
  test_a_refused_figure_is_blocked_not_a_download_failure
  test_the_manifest_is_the_skip_signal_only_when_it_got_something
"""

import hashlib
import json
import os

import pytest

from fetchpdf.retrieval import figures
from fetchpdf.retrieval.identifiers import IdentifierSet


# --------------------------------------------------------------------------
# Doubles
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


class FakeDownload:
    def __init__(self, url, status=200, content_type="image/jpeg", payload=b"",
                 detail=""):
        self.url = url
        self.request_url = url
        self.status = status
        self.content_type = content_type
        self.bytes_written = len(payload)
        self.sha256 = hashlib.sha256(payload).hexdigest() if payload else ""
        self.path = None
        self.outcome = "ok" if payload else "http-error"
        self.declared_length = None
        self.elapsed = 0.0
        self.detail = detail

    @property
    def ok(self):
        return self.path is not None


class FakeHttp:
    """Replays canned responses by URL substring, and records what was asked."""

    def __init__(self, routes=None, default=None, payloads=None, statuses=None):
        self.routes = routes or {}
        self.default = default or FakeResponse(status=404)
        self.payloads = payloads or {}
        self.statuses = statuses or {}
        self.requests = []
        self.downloads = []
        self.limiter = None

    def get(self, url, params=None, **kwargs):
        self.requests.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return self.default

    def download(self, url, dest, max_bytes=None, **kwargs):
        self.downloads.append(url)
        status = self.statuses.get(url, 200)
        payload = self.payloads.get(url)
        if status != 200 or payload is None:
            return FakeDownload(url, status=status, payload=b"")
        with open(dest, "wb") as handle:
            handle.write(payload)
        record = FakeDownload(url, payload=payload)
        record.path = dest
        return record


class FakeResolver:
    def __init__(self, ids, http):
        self._ids = ids
        self.http = http
        self.ladder = None

    def resolve(self, raw_identifier, doi=None, pmid=None):
        return self._ids


# --------------------------------------------------------------------------
# Fixtures, in the shapes the real endpoints serve
# --------------------------------------------------------------------------

PMCID = "PMC1817752"
DOI = "10.1371/journal.pone.0000308"

JATS = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <article-id pub-id-type="pmc">1817752</article-id>
  </article-meta></front>
  <body>
    <sec>
      <fig id="pone-0000308-g001">
        <label>Figure 1</label>
        <caption><p>Distribution of citation counts by data availability.</p></caption>
        <graphic xlink:href="pone.0000308.g001"/>
      </fig>
      <fig id="pone-0000308-g002">
        <label>Fig. 2</label>
        <caption><p>Citation counts over time.</p></caption>
        <graphic xlink:href="pone.0000308.g002.jpg"/>
      </fig>
      <fig id="scheme-1"><label>Scheme 1</label><p>drawn inline, no graphic</p></fig>
    </sec>
  </body>
</article>"""

BLOB = "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/0b7c/1817752/{h}/{name}"
G1_BLOB = BLOB.format(h="e5a245e0b165", name="pone.0000308.g001.jpg")
G2_BLOB = BLOB.format(h="2bc6b3724a48", name="pone.0000308.g002.jpg")

#: The shape of a PMC article page's figure block, verified live 2026-09-07 on
#: PMC1817752: the label sits in an `obj_head` heading, the caption in
#: `<figcaption>`, and the CDN URL is the `<img src>`.
PAGE = ('<html><body><section class="body main-article-body">'
        '<figure class="fig" id="pone-0000308-g001">'
        '<h3 class="obj_head">Figure 1. Distribution of citation counts.</h3>'
        f'<p><img class="graphic" src="{G1_BLOB}" alt="Figure 1"></p>'
        '<figcaption><p>The 41 trials which shared data.</p></figcaption>'
        '</figure>'
        '<figure class="fig" id="pone-0000308-g002">'
        '<h3 class="obj_head">Figure 2. Citation counts over time.</h3>'
        f'<p><img class="graphic" src="{G2_BLOB}" alt="Figure 2"></p>'
        '<figcaption><p>Counts by year.</p></figcaption>'
        '</figure></section></body></html>').encode()

S3_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
S3_VERSIONS = (b'<?xml version="1.0"?><ListBucketResult '
               b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               b"<CommonPrefixes><Prefix>PMC1817752.1/</Prefix></CommonPrefixes>"
               b"</ListBucketResult>")
S3_LISTING = (b'<?xml version="1.0"?><ListBucketResult '
              b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
              b"<Contents><Key>PMC1817752.1/pone.0000308.g001.jpg</Key>"
              b"<Size>84120</Size></Contents>"
              b"<Contents><Key>PMC1817752.1/pone.0000308.g002.jpg</Key>"
              b"<Size>91044</Size></Contents></ListBucketResult>")
S3_METADATA = {
    "pmcid": PMCID, "version": 1, "doi": DOI,
    "pdf_url": "s3://pmc-oa-opendata/PMC1817752.1/PMC1817752.1.pdf?md5=aa",
    "xml_url": "s3://pmc-oa-opendata/PMC1817752.1/PMC1817752.1.xml?md5=bb",
    "media_urls": [
        "s3://pmc-oa-opendata/PMC1817752.1/pone.0000308.g001.jpg?md5=e5a2",
        "s3://pmc-oa-opendata/PMC1817752.1/pone.0000308.g002.jpg?md5=2bc6",
        "s3://pmc-oa-opendata/PMC1817752.1/pone.0000308.s001.doc?md5=8849",
    ],
}
G1_MIRROR = f"{S3_BUCKET}/PMC1817752.1/pone.0000308.g001.jpg"
G2_MIRROR = f"{S3_BUCKET}/PMC1817752.1/pone.0000308.g002.jpg"

JPEG = b"\xff\xd8\xff\xe0" + b"figure bytes" * 8


def mirror_routes():
    return {
        "prefix=PMC1817752.&delimiter": FakeResponse(content=S3_VERSIONS),
        "metadata/PMC1817752.1.json": FakeResponse(
            content=json.dumps(S3_METADATA).encode(), content_type="application/json"),
        "prefix=PMC1817752.1/": FakeResponse(content=S3_LISTING),
    }


def run(tmp_path, http, pmcid=PMCID, **kwargs):
    ids = IdentifierSet(doi=DOI, pmcid=pmcid)
    return figures.pull_for_record(
        raw_identifier=DOI, doi=DOI, save_path=str(tmp_path / "rec.pdf"),
        resolver=FakeResolver(ids, http), http=http, **kwargs)


def manifest_of(tmp_path):
    with open(tmp_path / "rec_figures.json", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------
# The two routes, and why they are labelled
# --------------------------------------------------------------------------


def test_the_mirror_is_preferred_and_marked_original(tmp_path):
    """The Open Data package holds the figure files and declares size and md5
    before a byte moves, so it is asked first and its copy is not a render."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(routes=mirror_routes(),
                    payloads={G1_MIRROR: JPEG, G2_MIRROR: JPEG})

    summary = run(tmp_path, http)

    assert summary.status == "ok"
    assert summary.written == 2
    assert http.downloads == [G1_MIRROR, G2_MIRROR]
    rows = manifest_of(tmp_path)["figures"]
    assert [r["provenance"] for r in rows] == ["original", "original"]
    assert rows[0]["declared_bytes"] == 84120
    assert rows[0]["declared_checksum"] == "md5:e5a2"
    assert rows[0]["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert os.path.exists(tmp_path / "rec_figures" / "pone.0000308.g001.jpg")


def test_a_cdn_copy_is_marked_render(tmp_path):
    """A blob-CDN file is a re-encoded publisher render. Calling it `original`
    would invite byte-identity conclusions about PMC's pipeline."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})  # no mirror routes

    summary = run(tmp_path, http)

    assert summary.written == 2
    assert http.downloads == [G1_BLOB, G2_BLOB]
    assert {r["provenance"] for r in manifest_of(tmp_path)["figures"]} == {"render"}


def test_a_jats_href_without_an_extension_still_matches_the_page(tmp_path):
    """`<graphic xlink:href="pone.0000308.g001">` names no extension and PMC
    serves .jpg, so exact basename matching would lose most figures."""
    urls = figures.blob_urls_from_page(PAGE, ["pone.0000308.g001", "pone.0000308.g002.jpg"])
    assert urls == {"pone.0000308.g001": G1_BLOB, "pone.0000308.g002.jpg": G2_BLOB}


def test_a_page_on_disk_is_used_before_the_network(tmp_path):
    """A record whose text came from its PMC page already holds every blob
    link, so the common case costs no page request at all."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})

    run(tmp_path, http)

    assert not any("pmc.ncbi.nlm.nih.gov/articles" in url for url in http.requests)
    assert manifest_of(tmp_path)["page_source"] == "rec.fulltext.html"


def test_a_page_with_no_jats_enumerates_its_own_figures(tmp_path):
    """The page carries the label, the caption and the URL, so a record with no
    JATS is not a record with no figures."""
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})

    summary = run(tmp_path, http)

    manifest = manifest_of(tmp_path)
    assert summary.written == 2
    assert manifest["figures_from"] == "article_page"
    assert [r["figure_label"] for r in manifest["figures"]] == ["Figure 1", "Figure 2"]
    assert manifest["figures"][0]["caption"].startswith("The 41 trials")


# --------------------------------------------------------------------------
# Refusals are named
# --------------------------------------------------------------------------


def test_no_pmcid_is_a_named_refusal_with_a_manifest(tmp_path):
    http = FakeHttp()
    summary = run(tmp_path, http, pmcid=None)

    assert summary.status == "refused"
    assert summary.refusal == "no_pmcid"
    assert manifest_of(tmp_path)["refusal"]["kind"] == "no_pmcid"
    assert http.requests == []


def test_a_manifest_is_written_with_zero_figures(tmp_path):
    """"We asked and PMC listed nothing" and "we never asked" are different
    facts, and a missing file cannot tell them apart."""
    (tmp_path / "rec.xml").write_bytes(
        b"<article><body><sec><p>no figures here</p></sec></body></article>")
    http = FakeHttp()

    summary = run(tmp_path, http)

    manifest = manifest_of(tmp_path)
    assert summary.status == "none_found"
    assert manifest["figures"] == []
    assert manifest["counts"]["declared"] == 0
    assert manifest["refusal"] is None


def test_a_figure_missing_from_the_page_is_named_not_dropped(tmp_path):
    (tmp_path / "rec.xml").write_bytes(JATS)
    page_with_one = PAGE.replace(G2_BLOB.encode(), b"about:blank")
    (tmp_path / "rec.fulltext.html").write_bytes(page_with_one)
    http = FakeHttp(payloads={G1_BLOB: JPEG})

    summary = run(tmp_path, http)

    rows = manifest_of(tmp_path)["figures"]
    assert summary.status == "partial"
    assert [r["status"] for r in rows] == ["ok", "figure_not_on_page"]


def test_a_refused_figure_is_blocked_not_a_download_failure(tmp_path):
    """A CDN that refuses this client and serves a person has not said the
    figure does not exist. It has said to send a person."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(payloads={G1_BLOB: JPEG},
                    statuses={G2_BLOB: 403})

    summary = run(tmp_path, http)

    rows = manifest_of(tmp_path)["figures"]
    assert [r["status"] for r in rows] == ["ok", "blocked:403"]
    assert summary.blocked_urls == [G2_BLOB]


def test_a_404_is_a_download_failure(tmp_path):
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    http = FakeHttp(payloads={G1_BLOB: JPEG}, statuses={G2_BLOB: 404})

    summary = run(tmp_path, http)

    rows = manifest_of(tmp_path)["figures"]
    assert rows[1]["status"] == "download_failed:404"
    assert summary.blocked_urls == []


def test_neither_a_jats_nor_a_page_is_a_named_refusal(tmp_path):
    summary = run(tmp_path, FakeHttp())

    assert summary.status == "refused"
    assert summary.refusal == "no_jats_or_page"


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------


def test_a_fig_with_no_graphic_names_no_file(tmp_path):
    """A <fig> holding a table or an inline formula has nothing to fetch, so it
    is not a figure we failed to get."""
    found = figures.figures_from_jats(JATS)

    assert [f["id"] for f in found] == ["pone-0000308-g001", "pone-0000308-g002"]
    assert found[0]["caption"].startswith("Distribution of citation counts")


@pytest.mark.parametrize("raw,expected", [
    ("Figure 1", "Figure 1"),
    ("Fig. 2", "Figure 2"),
    ("FIGURE 3a", "Figure 3A"),
    ("Figure 10.", "Figure 10"),
    ("Scheme 1", "Scheme 1"),
])
def test_labels_are_normalised_to_one_spelling(raw, expected):
    """One spelling, so a consumer joining figures to captions is comparing
    labels rather than publisher house style -- and a label that does not parse
    is kept verbatim rather than renumbered into something the paper never said."""
    assert figures.normalise_label(raw) == expected


def test_an_unlabelled_figure_falls_back_to_its_position():
    assert figures.normalise_label(None, 3) == "Figure 3"
    assert figures.normalise_label(None) is None


# --------------------------------------------------------------------------
# Re-running
# --------------------------------------------------------------------------


def test_the_manifest_is_the_skip_signal_only_when_it_got_something(tmp_path):
    """A manifest that fetched nothing is re-asked: the last answer was PMC's,
    not ours, and PMC has served a 200 page with no figure links and then
    listed them minutes later."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)

    first = FakeHttp()          # every download fails
    assert run(tmp_path, first).written == 0

    second = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})
    assert run(tmp_path, second).written == 2

    third = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})
    assert run(tmp_path, third).status == "skipped"
    assert third.downloads == []


def test_a_no_pmcid_refusal_is_not_re_asked(tmp_path):
    """That refusal cannot change without a new input, so re-asking costs
    requests to learn the same thing."""
    assert run(tmp_path, FakeHttp(), pmcid=None).refusal == "no_pmcid"

    again = FakeHttp()
    assert run(tmp_path, again, pmcid=None).status == "skipped"


def test_refresh_re_asks_anyway(tmp_path):
    (tmp_path / "rec.xml").write_bytes(JATS)
    (tmp_path / "rec.fulltext.html").write_bytes(PAGE)
    assert run(tmp_path, FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})).written == 2

    http = FakeHttp(payloads={G1_BLOB: JPEG, G2_BLOB: JPEG})
    assert run(tmp_path, http, refresh=True).status == "ok"
    assert http.downloads


# --------------------------------------------------------------------------
# It is not the embedded-image dump
# --------------------------------------------------------------------------


def test_figures_and_embedded_images_never_share_an_output(tmp_path):
    """Different evidence, different directories. A PMC render must never land
    where a caller expects the authors' embedded bitstream."""
    from fetchpdf.retrieval import extract_images

    save_path = str(tmp_path / "rec.pdf")
    assert figures.figures_dir_for(save_path) != extract_images.images_dir_for(save_path)
    assert figures.manifest_path_for(save_path) != extract_images.manifest_path_for(save_path)


def test_a_route_that_raises_costs_that_route_not_the_record(tmp_path):
    """The contract every side pass here shares: the outcome is the manifest.

    Both network routes raise; the record comes back as a named refusal with a
    manifest on disk, not as an exception the caller has to catch.
    """
    class Exploding(FakeHttp):
        def get(self, url, params=None, **kwargs):
            raise RuntimeError("boom")

    (tmp_path / "rec.xml").write_bytes(JATS)
    summary = run(tmp_path, Exploding())

    assert summary.status == "refused"
    assert summary.refusal == "no_jats_or_page"
    assert manifest_of(tmp_path)["counts"]["declared"] == 2


def test_a_figure_the_mirror_holds_is_kept_when_another_is_lost(tmp_path):
    """Partial coverage is a shortfall to report, never a reason to refuse the
    record and drop the figures that were obtainable."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    routes = mirror_routes()
    routes["prefix=PMC1817752.1/"] = FakeResponse(content=S3_LISTING)
    routes["metadata/PMC1817752.1.json"] = FakeResponse(
        content=json.dumps({**S3_METADATA, "media_urls": S3_METADATA["media_urls"][:1]}
                           ).encode(), content_type="application/json")
    http = FakeHttp(routes=routes, payloads={G1_MIRROR: JPEG})

    summary = run(tmp_path, http)

    rows = manifest_of(tmp_path)["figures"]
    assert summary.status == "partial"
    assert [r["status"] for r in rows] == ["ok", "figure_not_on_page"]


def test_the_page_is_not_requested_when_the_mirror_covers_everything(tmp_path):
    """A request whose only purpose is to supply URLs already in hand is one
    PMC should not have to serve."""
    (tmp_path / "rec.xml").write_bytes(JATS)
    http = FakeHttp(routes=mirror_routes(),
                    payloads={G1_MIRROR: JPEG, G2_MIRROR: JPEG})

    assert run(tmp_path, http).written == 2
    assert not any("pmc.ncbi.nlm.nih.gov/articles" in url for url in http.requests)
