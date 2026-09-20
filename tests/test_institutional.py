"""The opt-in institutional routes: cookies, the publisher table, EBSCOhost.

No network. The EBSCO flow is exercised against a fake driver that answers the
two probe scripts from a lookup table, and the cookie route is tested through
`requests`' own request preparation rather than by sending anything.

The load-bearing tests here:

* `test_a_jar_sends_only_the_hosts_own_cookies` -- the reason the jar is
  domain-aware at all. A flat jar makes publishers answer 400 Request Header
  Or Cookie Too Large, and that failure is invisible from the parsing side.
* `test_an_httponly_prefixed_line_is_a_cookie_not_a_comment` -- Chrome
  cookies.txt extensions write `#HttpOnly_.domain`, which a naive comment skip
  silently drops. The dropped cookies are exactly the session ones.
* `test_a_record_without_a_content_url_is_linked_full_text_only` -- the two
  EBSCO failures mean different things to the user and must stay distinct.
* `test_nothing_institutional_runs_without_a_flag` -- the whole feature is
  opt-in, and that claim is only worth as much as a test of it.
"""
import json

import pytest
import requests

from fetchpdf import fetchpdf as fpd
from fetchpdf.retrieval import institutional as inst


# --------------------------------------------------------------------------
# Cookie parsing
# --------------------------------------------------------------------------
NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    "# This is a generated file!  Do not edit.",
    "",
    ".sagepub.com\tTRUE\t/\tTRUE\t1893456000\tsage_session\tsagevalue",
    "#HttpOnly_.tandfonline.com\tTRUE\t/\tTRUE\t1893456000\ttf_session\ttfvalue",
    "short\tline",
])

JSON_LIST = json.dumps([
    {"name": "sage_session", "value": "sagevalue", "domain": ".sagepub.com", "path": "/"},
    {"name": "tf_session", "value": "tfvalue", "domain": ".tandfonline.com", "path": "/"},
])

STORAGE_STATE = json.dumps({
    "cookies": [
        {"name": "sage_session", "value": "sagevalue", "domain": ".sagepub.com",
         "path": "/", "expires": -1, "httpOnly": True, "secure": True,
         "sameSite": "Lax"},
        {"name": "tf_session", "value": "tfvalue", "domain": ".tandfonline.com",
         "path": "/"},
    ],
    "origins": [],
})


class TestCookieParsing:
    @pytest.mark.parametrize("body,name", [
        (NETSCAPE, "cookies.txt"),
        (JSON_LIST, "cookies.json"),
        (STORAGE_STATE, "storage_state.json"),
    ])
    def test_both_export_formats_give_the_same_cookies(self, tmp_path, body, name):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        got = {(c["name"], c["value"], c["domain"]) for c in inst.load_cookies(path)}
        assert got == {
            ("sage_session", "sagevalue", ".sagepub.com"),
            ("tf_session", "tfvalue", ".tandfonline.com"),
        }

    def test_an_httponly_prefixed_line_is_a_cookie_not_a_comment(self, tmp_path):
        path = tmp_path / "cookies.txt"
        path.write_text(NETSCAPE, encoding="utf-8")
        names = [c["name"] for c in inst.load_cookies(path)]
        assert "tf_session" in names

    def test_a_malformed_row_is_skipped_rather_than_fatal(self, tmp_path):
        path = tmp_path / "cookies.txt"
        path.write_text(NETSCAPE, encoding="utf-8")
        assert len(inst.load_cookies(path)) == 2


class TestTheJarIsDomainAware:
    def _header_for(self, url, cookies):
        session = requests.Session()
        session.cookies = inst.cookie_jar(cookies)
        prepared = session.prepare_request(requests.Request("GET", url))
        return prepared.headers.get("Cookie", "")

    def test_a_jar_sends_only_the_hosts_own_cookies(self):
        cookies = json.loads(JSON_LIST)
        sage = self._header_for("https://journals.sagepub.com/doi/pdf/10.1177/x", cookies)
        assert "sage_session=sagevalue" in sage
        assert "tf_session" not in sage

        tf = self._header_for("https://www.tandfonline.com/doi/pdf/10.1080/x", cookies)
        assert "tf_session=tfvalue" in tf
        assert "sage_session" not in tf

    def test_a_host_with_no_matching_cookie_gets_no_cookie_header(self):
        header = self._header_for("https://example.org/",
                                  json.loads(JSON_LIST))
        assert header == ""

    def test_a_path_restricted_cookie_is_not_sent_to_another_path(self):
        cookies = [{"name": "scoped", "value": "v",
                    "domain": "journals.sagepub.com", "path": "/doi/pdf"}]
        assert "scoped" in self._header_for(
            "https://journals.sagepub.com/doi/pdf/10.1177/x", cookies)
        assert "scoped" not in self._header_for(
            "https://journals.sagepub.com/toc/", cookies)


# --------------------------------------------------------------------------
# The publisher table
# --------------------------------------------------------------------------
class TestPublisherUrls:
    @pytest.mark.parametrize("doi,expected", [
        ("10.1177/0956797613480187",
         "https://journals.sagepub.com/doi/pdf/10.1177/0956797613480187"),
        ("10.1080/00224545.2024.2439953",
         "https://www.tandfonline.com/doi/pdf/10.1080/00224545.2024.2439953?download=true"),
        ("10.1007/s11199-020-01137-x",
         "https://link.springer.com/content/pdf/10.1007/s11199-020-01137-x.pdf"),
        ("10.1111/jasp.12345",
         "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/jasp.12345?download=true"),
        ("10.1002/ejsp.2222",
         "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/ejsp.2222?download=true"),
        ("10.1098/rspb.2020.0001",
         "https://royalsocietypublishing.org/doi/pdf/10.1098/rspb.2020.0001"),
        ("10.1057/s41599-021-00001-1",
         "https://link.springer.com/content/pdf/10.1057/s41599-021-00001-1.pdf"),
    ])
    def test_the_first_url_for_each_prefix(self, doi, expected):
        assert inst.publisher_pdf_urls(doi)[0] == expected

    def test_an_unknown_prefix_yields_nothing_rather_than_a_guess(self):
        # APA (10.1037) has no derivable PDF path. Guessing one would spend a
        # request per record and never succeed; the EBSCO route covers APA.
        assert inst.publisher_pdf_urls("10.1037/edu0000827") == []
        assert inst.publisher_pdf_urls("") == []

    def test_open_access_hosts_are_not_in_the_table(self):
        # PLOS (10.1371) and SSRN are already handled by the open-access chain.
        assert "10.1371" not in inst.PUBLISHER_PDF_TEMPLATES


# --------------------------------------------------------------------------
# EBSCOhost, against a fake driver
# --------------------------------------------------------------------------
class FakeDriver:
    """Answers the two probe scripts from what the 'current page' is.

    `pages` maps a URL substring to (record_id_answer, content_url_answer).
    """

    def __init__(self, pages):
        self.pages = pages
        self.visited = []
        self.current = ("NONE", "NONE")
        self.closed = False

    def open_tab(self):
        self.visited.append("<new tab>")

    def navigate(self, url):
        self.visited.append(url)
        self.current = ("NONE", "NONE")
        for fragment, answers in self.pages.items():
            if fragment in url:
                self.current = answers
                break

    def eval_js(self, script):
        return self.current[1] if "performance" in script else self.current[0]

    def close(self):
        self.closed = True


CONTENT_URL = ("https://content.ebscohost.com/cds/retrieve?content=SIGNED-TOKEN"
               "&D=psyh&S=R")


def _download_to(path, calls):
    def download(url, save_path, **kwargs):
        calls.append(url)
        open(save_path, "wb").write(b"%PDF-1.7 fake")
        return True
    return download


class TestEbscoFlow:
    def test_a_record_with_a_content_url_is_downloaded(self, tmp_path):
        driver = FakeDriver({
            "/search/results": ("rid123", "NONE"),
            "/viewer/pdf/rid123": ("rid123", CONTENT_URL),
        })
        out = tmp_path / "paper.pdf"
        calls = []
        outcome, url = inst.fetch_via_ebsco(
            "10.1037/edu0000827", str(out), "s1234567", db="psyh",
            driver=driver, download=_download_to(out, calls),
            sleep=lambda s: None,
        )
        assert outcome == inst.EBSCO_OK
        assert calls == [CONTENT_URL]
        assert out.read_bytes().startswith(b"%PDF")
        # The token never leaves the function.
        assert url == "https://content.ebscohost.com/cds/retrieve"
        assert "SIGNED-TOKEN" not in url

    def test_a_doi_ebsco_does_not_index_is_reported_as_not_indexed(self, tmp_path):
        driver = FakeDriver({})
        outcome, url = inst.fetch_via_ebsco(
            "10.1037/none", str(tmp_path / "p.pdf"), "s1234567",
            driver=driver, sleep=lambda s: None, timeout=0.0,
        )
        assert outcome == inst.EBSCO_NOT_INDEXED
        assert url is None

    def test_a_record_without_a_content_url_is_linked_full_text_only(self, tmp_path):
        driver = FakeDriver({
            "/search/results": ("rid456", "NONE"),
            "/viewer/pdf/rid456": ("rid456", "NONE"),
        })
        outcome, _ = inst.fetch_via_ebsco(
            "10.1037/linked", str(tmp_path / "p.pdf"), "s1234567",
            driver=driver, sleep=lambda s: None, timeout=0.0,
        )
        assert outcome == inst.EBSCO_NO_HOSTED_PDF

    def test_a_named_database_is_tried_first_and_all_databases_second(self, tmp_path):
        # The record exists, but not in psyh. Falling back to every database
        # is what finds it.
        driver = FakeDriver({"q=10.1037%2Fx&db=": ("NONE", "NONE")})
        driver.pages = {}

        class OnlyAllDatabases(FakeDriver):
            def navigate(self, url):
                self.visited.append(url)
                if "/viewer/pdf/rid789" in url:
                    self.current = ("rid789", CONTENT_URL)
                elif "/search/results" in url and "db=" not in url:
                    self.current = ("rid789", "NONE")
                else:
                    self.current = ("NONE", "NONE")

        driver = OnlyAllDatabases({})
        out = tmp_path / "p.pdf"
        outcome, _ = inst.fetch_via_ebsco(
            "10.1037/x", str(out), "s1234567", db="psyh", driver=driver,
            download=_download_to(out, []), sleep=lambda s: None, timeout=0.0,
        )
        assert outcome == inst.EBSCO_OK
        searches = [u for u in driver.visited if "/search/results" in u]
        assert "db=psyh" in searches[0]
        assert "db=" not in searches[1]

    def test_a_missing_profile_is_reported_rather_than_guessed(self, tmp_path):
        outcome, _ = inst.fetch_via_ebsco(
            "10.1037/x", str(tmp_path / "p.pdf"), "", driver=FakeDriver({}))
        assert outcome == inst.EBSCO_NO_PROFILE

    def test_the_doi_is_url_encoded_into_the_search(self, tmp_path):
        driver = FakeDriver({})
        inst.fetch_via_ebsco("10.1037/edu0000827", str(tmp_path / "p.pdf"),
                             "s1234567", driver=driver, sleep=lambda s: None,
                             timeout=0.0)
        assert "q=10.1037%2Fedu0000827" in driver.visited[1]


class TestFetchEntryPoint:
    def test_an_empty_options_object_runs_nothing(self, tmp_path):
        result = inst.fetch("10.1037/x", str(tmp_path / "p.pdf"),
                            inst.InstitutionalOptions())
        assert not result.ok
        assert result.reasons == []

    def test_the_ebsco_failure_reason_reaches_the_caller(self, tmp_path, monkeypatch):
        monkeypatch.setattr(inst, "EBSCO_POLL_TIMEOUT", 0.0)
        options = inst.InstitutionalOptions(ebsco=True, ebsco_profile="s1234567")
        driver = FakeDriver({
            "/search/results": ("rid1", "NONE"),
            "/viewer/pdf/rid1": ("rid1", "NONE"),
        })
        result = inst.fetch("10.1037/x", str(tmp_path / "p.pdf"), options,
                            driver=driver)
        assert not result.ok
        assert "Linked Full Text" in result.reasons[0]
        # A driver the caller supplied is the caller's to close.
        assert not driver.closed

    def test_a_pdf_that_fails_the_identity_check_is_not_a_result(self, tmp_path, monkeypatch):
        out = tmp_path / "p.pdf"
        monkeypatch.setattr(inst, "fetch_with_cookies",
                            lambda *a, **k: "https://journals.sagepub.com/doi/pdf/10.1177/x")
        options = inst.InstitutionalOptions(cookies_path=str(tmp_path / "c.txt"))
        result = inst.fetch("10.1177/x", str(out), options,
                            accept=lambda path, url: False)
        assert not result.ok
        assert "identity check" in result.reasons[0]


class TestRedaction:
    def test_the_signed_token_is_stripped_before_anything_records_it(self):
        assert inst.redact_url(CONTENT_URL) == \
            "https://content.ebscohost.com/cds/retrieve"

    def test_a_url_without_a_query_survives_unchanged(self):
        assert inst.redact_url("https://example.org/a/b") == "https://example.org/a/b"


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------
class TestOptIn:
    def test_nothing_institutional_runs_without_a_flag(self, tmp_path, monkeypatch):
        """A default fetch_pdf must not reach the institutional module at all."""
        def explode(*args, **kwargs):
            raise AssertionError("institutional route ran without a flag")

        monkeypatch.setattr(inst, "fetch", explode)
        monkeypatch.setattr(fpd, "_fetch_pdf_chain", lambda *a, **k: None)
        monkeypatch.setattr(fpd, "resolve_identifier_to_doi", lambda d, **k: d)

        import fetchpdf.retrieval.engine as engine
        monkeypatch.setattr(engine, "retrieve_tiered", lambda **k: None)

        assert fetch_default(tmp_path) is None

    def test_the_route_is_reached_once_a_flag_is_given(self, tmp_path, monkeypatch):
        seen = {}

        def fake_fetch(doi, save_path, options, **kwargs):
            seen["doi"] = doi
            seen["ebsco"] = options.ebsco
            return inst.InstitutionalResult()

        monkeypatch.setattr(inst, "fetch", fake_fetch)
        monkeypatch.setattr(fpd, "_fetch_pdf_chain", lambda *a, **k: None)
        monkeypatch.setattr(fpd, "resolve_identifier_to_doi", lambda d, **k: d)
        import fetchpdf.retrieval.engine as engine
        monkeypatch.setattr(engine, "retrieve_tiered", lambda **k: None)

        fpd.fetch_pdf("10.1037/x", str(tmp_path / "p.pdf"),
                      institutional=inst.InstitutionalOptions(
                          ebsco=True, ebsco_profile="s1234567"))
        assert seen == {"doi": "10.1037/x", "ebsco": True}


def fetch_default(tmp_path):
    return fpd.fetch_pdf("10.1037/x", str(tmp_path / "p.pdf"))
