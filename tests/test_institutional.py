"""The opt-in cookie route (--cookies). No network.

Parsing and publisher-table tests are adapted from PR #3 (Lukas Wallrich).
The rest defend the rules that make a cookie file safe to hand the tool:
cookies go only to the publisher's own domain, never to a redirect target or a
cookie with no domain, secure cookies stay on https, and nothing runs unless
the flag is given.
"""
import json

import pytest
import requests

from fetchpdf import fetchpdf as fpd
from fetchpdf._http import POLITE_USER_AGENT
from fetchpdf.retrieval import institutional as inst

#: 2100-01-01. A constant, not now-plus-something: the value lands in the
#: parametrize ids, and xdist workers must all collect identical ids.
FUTURE = 4102444800

NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    "",
    f".sagepub.com\tTRUE\t/\tTRUE\t{FUTURE}\tsage_session\tsagevalue",
    f"#HttpOnly_.tandfonline.com\tTRUE\t/\tTRUE\t{FUTURE}\ttf_session\ttfvalue",
    "short\tline",
])

JSON_LIST = json.dumps([
    {"name": "sage_session", "value": "sagevalue", "domain": ".sagepub.com", "path": "/"},
    {"name": "tf_session", "value": "tfvalue", "domain": ".tandfonline.com", "path": "/"},
])

STORAGE_STATE = json.dumps({"cookies": [
    {"name": "sage_session", "value": "sagevalue", "domain": ".sagepub.com",
     "path": "/", "expires": -1, "httpOnly": True, "secure": True},
    {"name": "tf_session", "value": "tfvalue", "domain": ".tandfonline.com", "path": "/"},
], "origins": []})


def _cookie_header(url, cookies):
    session = requests.Session()
    session.cookies = inst.cookie_jar(cookies, url)
    return session.prepare_request(requests.Request("GET", url)).headers.get("Cookie", "")


# -- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("body,name", [
    (NETSCAPE, "cookies.txt"), (JSON_LIST, "cookies.json"),
    (STORAGE_STATE, "storage_state.json"),
], ids=["netscape", "json-list", "storage-state"])
def test_every_export_format_gives_the_same_cookies(tmp_path, body, name):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    got = {(c["name"], c["value"], c["domain"]) for c in inst.load_cookies(path)}
    assert got == {("sage_session", "sagevalue", ".sagepub.com"),
                   ("tf_session", "tfvalue", ".tandfonline.com")}


def test_an_httponly_prefixed_line_is_a_cookie_not_a_comment(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE, encoding="utf-8")
    assert "tf_session" in [c["name"] for c in inst.load_cookies(path)]


def test_a_cookie_without_a_domain_is_dropped(tmp_path):
    """In a requests jar an empty domain matches every host."""
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([{"name": "orphan", "value": "secret"}]), encoding="utf-8")
    assert inst.load_cookies(path) == []


# -- where cookies go --------------------------------------------------------

def test_only_the_publishers_own_cookies_are_sent():
    cookies = json.loads(JSON_LIST)
    sage = _cookie_header("https://journals.sagepub.com/doi/pdf/10.1177/x", cookies)
    assert "sage_session=sagevalue" in sage and "tf_session" not in sage


def test_a_redirect_to_another_host_carries_no_cookies():
    """The jar is scoped to the publisher's domain, so the redirect target finds
    nothing to send -- even a cookie that would otherwise match it."""
    cookies = json.loads(JSON_LIST) + [
        {"name": "other", "value": "v", "domain": ".evil.example", "path": "/"}]
    session = requests.Session()
    session.cookies = inst.cookie_jar(cookies, "https://journals.sagepub.com/doi/pdf/x")
    prepared = session.prepare_request(requests.Request("GET", "https://evil.example/"))
    assert "Cookie" not in prepared.headers


def test_a_secure_cookie_is_not_sent_over_http():
    cookies = [{"name": "s", "value": "v", "domain": ".sagepub.com", "path": "/",
                "secure": True}]
    assert "s=v" in _cookie_header("https://journals.sagepub.com/x", cookies)
    assert _cookie_header("http://journals.sagepub.com/x", cookies) == ""


def test_an_expired_cookie_is_not_sent():
    cookies = [{"name": "old", "value": "v", "domain": ".sagepub.com", "path": "/",
                "expires": 1000}]
    assert _cookie_header("https://journals.sagepub.com/x", cookies) == ""


# -- publisher table ---------------------------------------------------------

@pytest.mark.parametrize("doi,expected", [
    ("10.1177/0956797613480187",
     "https://journals.sagepub.com/doi/pdf/10.1177/0956797613480187"),
    ("10.1080/00224545.2024.2439953",
     "https://www.tandfonline.com/doi/pdf/10.1080/00224545.2024.2439953?download=true"),
    ("10.1007/s11199-020-01137-x",
     "https://link.springer.com/content/pdf/10.1007/s11199-020-01137-x.pdf"),
    ("10.1111/jasp.12345",
     "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/jasp.12345?download=true"),
    ("10.1098/rspb.2020.0001",
     "https://royalsocietypublishing.org/doi/pdf/10.1098/rspb.2020.0001"),
])
def test_the_first_url_for_each_prefix(doi, expected):
    assert inst.publisher_pdf_urls(doi)[0] == expected


def test_an_unknown_prefix_yields_nothing_rather_than_a_guess():
    assert inst.publisher_pdf_urls("10.1037/xge0000001") == []


# -- fetching ------------------------------------------------------------------

class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def iter_content(self, size):
        yield self._body


def test_springer_is_asked_with_the_polite_user_agent(monkeypatch, tmp_path):
    """Springer answers a Chrome UA on /content/pdf with an HTML interstitial."""
    seen = {}

    def get(self, url, **kw):
        seen["ua"] = self.headers["User-Agent"]
        return _Response(200, b"%PDF-1.4 body")

    monkeypatch.setattr(requests.Session, "get", get)
    url = inst.fetch_with_cookies("10.1007/s1", tmp_path / "a.pdf", [])
    assert url and seen["ua"] == POLITE_USER_AGENT


def test_an_html_answer_is_not_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _Response(200, b"<html>login</html>"))
    target = tmp_path / "a.pdf"
    assert inst.fetch_with_cookies("10.1177/x", target, []) is None
    assert not target.exists()
    assert not (tmp_path / "a.pdf.part").exists()


def test_a_pdf_failing_the_identity_check_is_not_a_result(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.json"
    cookies.write_text(JSON_LIST, encoding="utf-8")
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _Response(200, b"%PDF-1.4 body"))
    path, reasons = inst.fetch("10.1177/x", tmp_path / "a.pdf", cookies,
                               accept=lambda p, u: False)
    assert path is None and "identity check" in reasons[0]


# -- opt-in ---------------------------------------------------------------------

def test_nothing_runs_without_the_flag(monkeypatch):
    monkeypatch.setattr(inst, "fetch", lambda *a, **kw: pytest.fail("ran without --cookies"))
    assert fpd._try_institutional("x.pdf", "10.1177/x", None) is None


def test_the_cli_refuses_a_missing_cookie_file(tmp_path):
    code = fpd.main(["10.1177/x", "-o", str(tmp_path), "--cookies",
                     str(tmp_path / "missing.txt")])
    assert code == 2
