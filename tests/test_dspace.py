"""DSpace 7 repository walk: handle parsing and bundle selection.

No network. The handle forms below are the ones OpenAlex actually emits for
green open-access locations, and each mis-parse listed here was a real bug.
"""

from fetchpdf.retrieval.supplement_dspace import dspace_handle, enumerate_dspace
from fetchpdf.retrieval.identifiers import IdentifierSet


class TestHandleParsing:
    def test_harvard_purl_is_rewritten_to_the_repository(self):
        """nrs.harvard.edu is a resolver, not the repository, and the purl
        carries no handle at all -- both have to be rewritten."""
        assert dspace_handle("http://nrs.harvard.edu/urn-3:HUL.InstRepos:10976353") \
            == ("https://dash.harvard.edu", "1/10976353")

    def test_plain_handle_url(self):
        assert dspace_handle("https://dash.harvard.edu/handle/1/10976353") \
            == ("https://dash.harvard.edu", "1/10976353")

    def test_dotted_handle_prefixes_survive(self):
        """MIT's prefix is 1721.1; a \\d+/\\d+ pattern truncated it to 1/12345,
        which is a valid-looking handle for a completely different item."""
        assert dspace_handle("https://dspace.mit.edu/handle/1721.1/12345") \
            == ("https://dspace.mit.edu", "1721.1/12345")

    def test_a_bare_handle_has_no_repository_to_talk_to(self):
        """urlparse would otherwise yield "https://hdl:1721.1" as a base URL."""
        assert dspace_handle("hdl:1721.1/999") is None

    def test_non_repository_urls_decline(self):
        assert dspace_handle("https://example.org/paper.pdf") is None
        assert dspace_handle("not-a-repo") is None
        assert dspace_handle(None) is None


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload, self.ok, self.status = payload, ok, 200 if ok else 404

    def json(self):
        return self._payload


class _Ctx:
    """Serves the three-step DSpace walk from canned JSON."""

    verbose = False

    def __init__(self, bundles):
        self.scratch = {}
        self._bundles = bundles
        self.logged = []
        outer = self

        class _Http:
            def get(_s, url, **kw):
                if "/pid/find" in url:
                    return _Resp({"uuid": "item-uuid"})
                if url.endswith("/bundles"):
                    return _Resp({"_embedded": {"bundles": outer._bundles}})
                return _Resp({"_embedded": {"bitstreams": [{
                    "name": "paper.pdf", "sizeBytes": 4242,
                    "checkSum": {"checkSumAlgorithm": "MD5", "value": "abc"},
                    "_links": {"content": {"href": "https://x/bitstreams/b/content"}},
                }]}})

        self.http = _Http()

    def log(self, message):
        self.logged.append(message)


def _bundle(name):
    return {"name": name,
            "_links": {"bitstreams": {"href": "https://x/bundles/b/bitstreams"}}}


class TestBundleSelection:
    def test_original_bundle_is_taken(self):
        ctx = _Ctx([_bundle("ORIGINAL")])
        files = enumerate_dspace(IdentifierSet(doi="10.1/x"), ctx,
                                 landing_url="https://dash.harvard.edu/handle/1/1")
        assert [f.name for f in files] == ["paper.pdf"]
        assert files[0].checksum == "md5:abc"
        assert files[0].size_bytes == 4242

    def test_text_and_thumbnail_bundles_are_skipped(self):
        """TEXT is machine-extracted plaintext of the same PDF and THUMBNAIL a
        preview -- keeping either files a duplicate as a supplement."""
        ctx = _Ctx([_bundle("TEXT"), _bundle("THUMBNAIL")])
        assert enumerate_dspace(IdentifierSet(doi="10.1/x"), ctx,
                                landing_url="https://dash.harvard.edu/handle/1/1") == []

    def test_no_landing_url_declines(self):
        """DSpace has no DOI lookup, so without a URL there is nothing to resolve."""
        ctx = _Ctx([_bundle("ORIGINAL")])
        assert enumerate_dspace(IdentifierSet(doi="10.1/x"), ctx) == []
