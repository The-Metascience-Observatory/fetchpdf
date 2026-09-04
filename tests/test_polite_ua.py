"""Per-host UA override + the HTML-interstitial retry.

Springer and OUP invert the usual bot check on their direct /content/pdf
routes: a browser-like UA gets a ~3 KB HTML interstitial (HTTP 200,
text/html) while any non-browser UA gets the real application/pdf. Verified
2026-09-03 against several 10.1186 and 10.1093 DOIs. Downloads for those
publishers failed silently -- try_download saw text/html and returned False,
so the paper was reported as "nothing worked" with a correct URL in hand.
"""
import tempfile, os
from unittest import mock

import pytest

from fetchpdf._http import (USER_AGENT, POLITE_USER_AGENT, user_agent_for,
                            headers_for, looks_like_pdf)
import fetchpdf.fetchpdf as F


@pytest.mark.parametrize("url,polite", [
    ("https://link.springer.com/content/pdf/10.1186/x.pdf", True),
    ("https://LINK.SPRINGER.COM/content/pdf/10.1186/x.pdf", True),  # case
    ("https://academic.oup.com/jrnl/article-pdf/1/1/1/x.pdf", True),
    ("https://sub.link.springer.com/x.pdf", True),                  # subdomain
    ("https://zenodo.org/record/1/files/x.pdf", False),   # 403s non-browser UAs
    ("https://www.mdpi.com/1/1/1/pdf", False),
    ("not a url", False),
])
def test_user_agent_for_selects_per_host(url, polite):
    assert user_agent_for(url) == (POLITE_USER_AGENT if polite else USER_AGENT)


def test_headers_for_swaps_only_the_ua():
    base = {"User-Agent": USER_AGENT, "Accept": "application/pdf"}
    out = headers_for("https://link.springer.com/content/pdf/x.pdf", base)
    assert out["User-Agent"] == POLITE_USER_AGENT
    assert out["Accept"] == "application/pdf"   # everything else preserved
    assert base["User-Agent"] == USER_AGENT     # caller's dict untouched


@pytest.mark.parametrize("body,ctype,expected", [
    (b"%PDF-1.7 ...", "application/pdf", True),
    (b"%PDF-1.4 ...", "text/html", True),          # magic beats a wrong header
    (b"<!DOCTYPE html>", "text/html; charset=utf-8", False),
    (b"<!DOCTYPE html>", "application/xhtml+xml", False),
    (b"\x00\x01binary", "application/octet-stream", True),
    (b"", "application/pdf", False),               # empty body is not a PDF
])
def test_looks_like_pdf(body, ctype, expected):
    assert looks_like_pdf(body, ctype) is expected


class _Resp:
    def __init__(self, ctype, body):
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self._body = body
    def iter_content(self, n=8192):
        yield self._body
    def close(self):
        pass


def _run(url, responder):
    seen = []
    def fake_get(u, headers=None, **kw):
        ua = (headers or {}).get("User-Agent", "")
        seen.append(ua)
        return responder(ua)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        path = tf.name
    try:
        with mock.patch.object(F.requests, "get", fake_get):
            ok = F.try_download(url, path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return ok, seen, size
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_springer_gets_polite_ua_on_the_first_request():
    """No wasted round trip on a host we already know about."""
    ok, seen, _ = _run(
        "https://link.springer.com/content/pdf/10.1186/x.pdf",
        lambda ua: _Resp("application/pdf", b"%PDF-1.7 body"),
    )
    assert ok is True
    assert seen == [POLITE_USER_AGENT]


def test_html_interstitial_triggers_one_polite_retry_on_unknown_host():
    """The allow-list is not exhaustive, so HTML on a PDF URL retries once."""
    ok, seen, size = _run(
        "https://unknown-publisher.example.org/article/x.pdf",
        lambda ua: (_Resp("application/pdf", b"%PDF-1.7 body")
                    if ua == POLITE_USER_AGENT
                    else _Resp("text/html; charset=utf-8", b"<!DOCTYPE html>")),
    )
    assert ok is True
    assert seen == [USER_AGENT, POLITE_USER_AGENT]
    assert size > 0


def test_retry_does_not_loop_when_both_uas_serve_html():
    """A genuine landing page must still fail, after exactly two attempts."""
    ok, seen, _ = _run(
        "https://paywalled.example.org/article/x.pdf",
        lambda ua: _Resp("text/html", b"<!DOCTYPE html>login"),
    )
    assert ok is False
    assert seen == [USER_AGENT, POLITE_USER_AGENT]


def test_no_retry_when_the_first_response_is_already_a_pdf():
    ok, seen, _ = _run(
        "https://ok.example.org/x.pdf",
        lambda ua: _Resp("application/pdf", b"%PDF-1.7 body"),
    )
    assert ok is True
    assert seen == [USER_AGENT]
