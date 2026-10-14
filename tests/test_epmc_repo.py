"""Offline tests for the Europe PMC render-repository sources.

The manifest below is a real capture (PMC12712832, JAMA) trimmed to the fields
the source reads, inline because it is small and only these tests read it.
"""

import json
import os

from fetchpdf.retrieval.context import RetrievalContext
from fetchpdf.retrieval.identifiers import IdentifierSet
from fetchpdf.retrieval.tiers import Tier, load_ladder
from fetchpdf.retrieval.validate import validate_t1

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name, mode="rb"):
    with open(os.path.join(FIXTURES, name), mode) as f:
        return f.read()


class FakeResponse:
    """Mirrors http.Response for the parts a source touches."""

    def __init__(self, content=b"", status=200, content_type=""):
        self.content = content
        self.status = status
        self.content_type = content_type
        self.url = "https://europepmc.org/api/fulltextRepo"
        self.request_url = self.url

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json_or(self, default):
        try:
            return json.loads(self.text)
        except ValueError:
            return default


def _ctx(http):
    return RetrievalContext(
        http=http, resolver=None, ladder=load_ladder(), save_path="/tmp/x.pdf",
    )


REPO_MANIFEST = {
        "id": "PMC12712832",
        "version": 1.0,
        "source": "PMC",
        "domain": "jamasd",
        "files": [
            {
                "filename": "jama-e2521738-s001.pdf",
                "type": "supplement",
                "mimeType": "application/pdf"
            },
            {
                "filename": "12712832.ssnexlite.xml",
                "type": "xml",
                "mimeType": "application/xml"
            },
            {
                "filename": "jama-e2521738-s003.pdf",
                "type": "supplement",
                "mimeType": "application/pdf"
            },
            {
                "filename": "jama-e2521738-s002.pdf",
                "type": "supplement",
                "mimeType": "application/pdf"
            },
            {
                "filename": "12712832.ssnex.xml",
                "type": "xml",
                "mimeType": "application/xml"
            },
            {
                "filename": "jama-e2521738-s004.pdf",
                "type": "supplement",
                "mimeType": "application/pdf"
            },
            {
                "filename": "jama-e2521738-g001.gif",
                "type": "image",
                "mimeType": "image/gif"
            },
            {
                "filename": "jama-e2521738-g002.gif",
                "type": "image",
                "mimeType": "image/gif"
            },
            {
                "filename": "jama-e2521738-g002.jpg",
                "type": "image",
                "mimeType": "image/jpeg"
            },
            {
                "filename": "jama-e2521738-g001.jpg",
                "type": "image",
                "mimeType": "image/jpeg"
            }
        ]
    }

#: Above the T1 gate's character floor, with the structure it checks for.
REPO_JATS = (
    b'<article xml:lang="en" article-type="research-article"><body><sec><p>'
    + b"Full text of the trial report. " * 400
    + b"</p></sec></body></article>"
)


class RoutingHttp:
    """Replays canned responses, routing on the `type` param.

    The repo's two calls share one URL and differ only by that param, so the
    substring-on-URL routing the other suites use cannot tell them apart.
    """

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        kind = (params or {}).get("type")
        for fragment, response in self.routes.items():
            if fragment == kind or fragment in url:
                return response
        return FakeResponse(status=404)


def _http(manifest=REPO_MANIFEST, xml=REPO_JATS, xml_status=200):
    routes = {}
    if manifest is not None:
        routes["METADATA"] = FakeResponse(
            content=json.dumps(manifest).encode(), content_type="application/json")
    if xml is not None:
        routes["FILE"] = FakeResponse(
            content=xml, status=xml_status, content_type="application/xml")
    return RoutingHttp(routes)


def test_the_rungs_are_last_in_their_tiers():
    """A backup for the documented routes, never preferred to one of them."""
    ladder = load_ladder()
    t1 = [s.name for s in ladder.sources_for(Tier.T1_XML)]
    t2 = [s.name for s in ladder.sources_for(Tier.T2_HTML)]

    assert t1[-1] == "epmc_repo_xml"
    assert t2[-1] == "epmc_transform_html"


def test_repo_xml_recovers_full_text_the_documented_routes_refuse():
    """The case the source exists for: efetch says no, the render repo says yes."""
    from fetchpdf.retrieval.sources import epmc_repo

    # The documented route really does refuse a record of this shape.
    assert not validate_t1(fixture("efetch_denial_stub.xml")).ok

    ctx = _ctx(_http())
    ids = IdentifierSet(doi="10.1001/jama.2025.21738", pmcid="PMC12712832")

    artifact = epmc_repo.fetch_repo_xml(ids, ctx)

    assert artifact is not None
    assert artifact.tier is Tier.T1_XML
    assert artifact.source == "epmc_repo_xml"
    assert validate_t1(artifact.content).ok
    assert ctx.pmc_xml_already_taken


def test_repo_xml_sends_the_version_the_manifest_declares():
    """Hardcoding version=1 works today and serves a stale revision later."""
    manifest = dict(REPO_MANIFEST, version=3.0)
    http = _http(manifest)

    from fetchpdf.retrieval.sources import epmc_repo
    epmc_repo.fetch_repo_xml(IdentifierSet(pmcid="PMC12712832"), _ctx(http))

    calls = [p for _, p in http.requests if p.get("type") == "FILE"]
    assert calls and calls[0]["version"] == 3
    # .ssnex before .ssnexlite, and never an image or a supplement PDF.
    assert calls[0]["fileName"] == "12712832.ssnex.xml"


def test_repo_xml_defers_to_a_pmc_copy_already_taken():
    """Same underlying JATS as Europe PMC: two copies is not corroboration."""
    from fetchpdf.retrieval.sources import epmc_repo

    http = _http()
    ctx = _ctx(http)
    ctx.pmc_xml_already_taken = True

    assert epmc_repo.fetch_repo_xml(IdentifierSet(pmcid="PMC1"), ctx) is None
    assert http.requests == []


def test_manifest_without_xml_is_a_miss_not_a_crash():
    from fetchpdf.retrieval.sources import epmc_repo

    manifest = dict(REPO_MANIFEST,
                    files=[f for f in REPO_MANIFEST["files"] if f["type"] != "xml"])

    assert epmc_repo.fetch_repo_xml(
        IdentifierSet(pmcid="PMC1"), _ctx(_http(manifest))) is None


def test_transform_html_needs_no_manifest():
    """Its own failure surface: the transform can render when the manifest cannot."""
    from fetchpdf.retrieval.sources import epmc_repo

    http = RoutingHttp({"fulltextXmlTransform": FakeResponse(
        content=b"<html><body><table><tr><td>x</td></tr></table></body></html>",
        content_type="text/html")})

    artifact = epmc_repo.fetch_transform_html(IdentifierSet(pmcid="PMC1"), _ctx(http))

    assert artifact is not None and artifact.tier is Tier.T2_HTML
    assert not any(p.get("type") == "METADATA" for _, p in http.requests)
    # Branding only, and it costs a request parameter that reveals the journal.
    assert not any("pmc_domain" in p for _, p in http.requests)


class _Resolver:
    def __init__(self, ids, http):
        self._ids = ids
        self.http = http

    def resolve(self, raw_identifier, doi=None, pmid=None):
        return self._ids

    def resolve_conditional(self, ids):
        return ids

    def crossref(self, ids):
        return {}


def _shipped_ladder_with_only_epmc_repo_live():
    """The shipped ladder, every other source declining without a request."""
    ladder = load_ladder()
    for name, spec in ladder.sources.items():
        if name not in ("epmc_repo_xml", "epmc_transform_html"):
            spec._fn = lambda ids, ctx: None
    return ladder


def test_the_structured_fallback_walk_reaches_the_repo_xml(tmp_path):
    """No PDF: fetch_pdf walks T1/T2 with xml_html_only, and this rung is in it."""
    from fetchpdf.retrieval.engine import retrieve_tiered

    ids = IdentifierSet(doi="10.1001/jama.2025.21738", pmcid="PMC12712832")
    result = retrieve_tiered(
        raw_identifier=ids.doi, doi=ids.doi,
        save_path=str(tmp_path / "rec.pdf"), xml_html_only=True,
        ladder=_shipped_ladder_with_only_epmc_repo_live(),
        resolver=_Resolver(ids, _http()),
    )

    assert result.path and result.path.endswith(".xml")
    assert result.artifact.tier is Tier.T1_XML
    assert result.artifact.source == "epmc_repo_xml"


def test_a_ladder_can_switch_the_rungs_off():
    """The opt-out the module docstring promises: enabled=false in the config."""
    ladder = load_ladder()
    ladder.sources["epmc_repo_xml"].enabled = False
    ladder.sources["epmc_transform_html"].enabled = False

    names = [s.name for t in (Tier.T1_XML, Tier.T2_HTML) for s in ladder.sources_for(t)]
    assert "epmc_repo_xml" not in names and "epmc_transform_html" not in names
