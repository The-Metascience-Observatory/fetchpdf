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

@pytest.fixture(autouse=True)
def _isolated_access(monkeypatch, tmp_path_factory):
    """No test sees the developer's real `fetchpdf cookies setup`, or another test's choices."""
    cfg_dir = tmp_path_factory.mktemp("fetchpdf_config")
    monkeypatch.setattr(inst, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(inst, "ACCESS_CONFIG", cfg_dir / "access.json")
    inst.reset_cookie_source()
    yield
    inst.reset_cookie_source()


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
    url, _ = inst.fetch_with_cookies("10.1007/s1", tmp_path / "a.pdf", [],
                                     user_agent="Mozilla/5.0 Chrome/149")
    assert url and seen["ua"] == POLITE_USER_AGENT


def test_an_html_answer_is_not_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _Response(200, b"<html>login</html>"))
    target = tmp_path / "a.pdf"
    assert inst.fetch_with_cookies("10.1177/x", target, []) == (None, False)
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


# -- browser UA, Cloudflare and the browser fallback ----------------------------

class _CfResponse(_Response):
    headers = {"server": "cloudflare"}


def test_the_browsers_user_agent_is_replayed(monkeypatch, tmp_path):
    """Cloudflare binds cf_clearance to the UA that earned it."""
    seen = {}

    def get(self, url, **kw):
        seen["ua"] = self.headers["User-Agent"]
        return _Response(200, b"%PDF-1.4 body")

    monkeypatch.setattr(requests.Session, "get", get)
    url, _ = inst.fetch_with_cookies("10.1111/x", tmp_path / "a.pdf", [],
                                     user_agent="Mozilla/5.0 Chrome/149")
    assert url and seen["ua"] == "Mozilla/5.0 Chrome/149"


def test_the_user_agent_is_read_from_a_get_cookies_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"cookies": [], "user_agent": "UA/1"}), encoding="utf-8")
    assert inst.load_user_agent(f) == "UA/1"
    g = tmp_path / "c.txt"
    g.write_text(NETSCAPE, encoding="utf-8")
    assert inst.load_user_agent(g) is None


def test_a_cloudflare_refusal_hands_over_to_the_browser(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.json"
    cookies.write_text(JSON_LIST, encoding="utf-8")
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _CfResponse(403, b"<html>challenge</html>"))

    def browser(doi, save_path, cookies, user_agent=None, verbose=False):
        save_path.write_bytes(b"%PDF-1.4 from the browser")
        return "https://journals.sagepub.com/doi/pdf/" + doi

    monkeypatch.setattr(inst, "fetch_in_browser", browser)
    path, reasons = inst.fetch("10.1177/x", tmp_path / "a.pdf", cookies)
    assert path and not reasons


def test_a_plain_refusal_does_not_launch_a_browser(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.json"
    cookies.write_text(JSON_LIST, encoding="utf-8")
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _Response(200, b"<html>no access</html>"))
    monkeypatch.setattr(inst, "fetch_in_browser",
                        lambda *a, **kw: pytest.fail("browser for a non-challenge"))
    path, _ = inst.fetch("10.1177/x", tmp_path / "a.pdf", cookies)
    assert path is None


def test_the_download_cap_stops_the_route(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.json"
    cookies.write_text(JSON_LIST, encoding="utf-8")
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **kw: _Response(200, b"%PDF-1.4 body"))
    inst.set_max_downloads(1)
    try:
        first, _ = inst.fetch("10.1177/a", tmp_path / "a.pdf", cookies)
        second, reasons = inst.fetch("10.1177/b", tmp_path / "b.pdf", cookies)
        assert first and second is None and "cap" in reasons[0]
    finally:
        inst.set_max_downloads(None)
    assert inst._max_downloads == inst.DEFAULT_MAX_DOWNLOADS


def test_every_pdf_publisher_has_a_landing_page():
    assert set(inst.PUBLISHER_PDF_TEMPLATES) == set(inst.PUBLISHER_ARTICLE_TEMPLATES)


# -- off by default; setup turns it on -----------------------------------------

def test_access_is_off_until_setup_has_run():
    assert inst.load_access_config() is None
    assert inst.active_cookies_file() is None


def test_after_setup_cookies_are_read_from_the_browser_once(monkeypatch):
    from fetchpdf import get_cookies
    inst.ACCESS_CONFIG.write_text(json.dumps({
        "key": "harvard", "name": "Harvard Library", "browser": "chrome",
        "cookie_file": str(inst.CONFIG_DIR / "c.json"), "enabled": True}), encoding="utf-8")
    calls = []

    def export(browser, out, label, quiet=False):
        calls.append(browser)
        out.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        return {}

    monkeypatch.setattr(get_cookies, "export_from_browser", export)
    assert inst.active_cookies_file() == str(inst.CONFIG_DIR / "c.json")
    assert inst.active_cookies_file() == str(inst.CONFIG_DIR / "c.json")
    assert calls == ["chrome"]


def test_a_disabled_setup_is_off():
    inst.ACCESS_CONFIG.write_text(json.dumps({"browser": "chrome", "enabled": False}),
                                  encoding="utf-8")
    assert inst.active_cookies_file() is None


def test_no_cookies_beats_an_explicit_file_and_setup(tmp_path):
    inst.use_cookies_file(tmp_path / "c.json")
    assert inst.active_cookies_file() == str(tmp_path / "c.json")
    inst.disable_cookies()
    assert inst.active_cookies_file() is None


def test_an_uncovered_publisher_is_silent_and_untouched(monkeypatch, capsys, tmp_path):
    inst.use_cookies_file(tmp_path / "c.json")
    monkeypatch.setattr(inst, "fetch", lambda *a, **kw: pytest.fail("fetched an APA DOI"))
    assert fpd._try_institutional("x.pdf", "10.1037/abc", verbose=True) is None
    assert capsys.readouterr().out == ""


def test_cookies_only_skips_the_chain(monkeypatch, tmp_path):
    inst.use_cookies_file(tmp_path / "c.json")
    inst.set_cookies_only(True)
    monkeypatch.setattr(fpd, "_fetch_pdf_chain",
                        lambda *a, **kw: pytest.fail("chain ran under --cookies-only"))
    monkeypatch.setattr(fpd, "resolve_identifier_to_doi", lambda d, verbose=False: d)
    seen = {}

    def route(save_path, resolved, cookies_file=None, verbose=False, _source_out=None):
        seen["args"] = (save_path, resolved)
        return save_path

    monkeypatch.setattr(fpd, "_try_institutional", route)
    target = str(tmp_path / "a.pdf")
    assert fpd.fetch_pdf("10.1177/x", target) == target
    assert seen["args"] == (target, "10.1177/x")


def test_a_publisher_that_refuses_everything_is_reported(monkeypatch):
    monkeypatch.setattr(inst, "_probe_still_works", lambda site: False)
    for i in range(5):
        inst._note_attempt(f"10.1111/x{i}", False)
    inst._note_attempt("10.1177/y", False)
    lines = inst.session_report()
    assert len(lines) == 1 and lines[0].startswith("wiley.com") and "fetchpdf cookies refresh" in lines[0]


def test_refusals_with_a_working_session_blame_the_subscription(monkeypatch):
    monkeypatch.setattr(inst, "_probe_still_works", lambda site: True)
    for i in range(5):
        inst._note_attempt(f"10.1177/x{i}", False)
    (line,) = inst.session_report()
    assert "session works" in line and "refresh" not in line
