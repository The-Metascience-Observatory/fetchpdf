"""Offline tests for the refused/absent distinction.

The substitution these forbid: recording a 401/403/429, or a bot-challenge
page, as anything that a reader can mistake for "the authors published
nothing". Measured cases behind it -- Atypon's
`/doi/suppl/10.1161/STROKEAHA.111.628537` returns 403 to this client and the
file to a browser, and `www.pnas.org/doi/suppl/10.1073/pnas.1118373109` returns
403 while the manifest recorded `nothing_listed`.

The load-bearing tests here:

  test_one_definition_of_a_bot_challenge   -- three modules used to hold three
  test_the_report_names_the_url_to_open    -- "fetch by hand" is only actionable
                                              if it says what to fetch
"""

import os

import pytest

from fetchpdf.retrieval import blocked


# --------------------------------------------------------------------------
# What "blocked" is
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refusal_of_this_client_is_blocked(status):
    assert blocked.classify(status) == "blocked"


@pytest.mark.parametrize("status", [200, 404, 410, 500, 0, None])
def test_everything_else_is_not(status):
    """The distinction has to cut both ways or it says nothing. 404 and 410 ARE
    answers about the file; 500 is the server's problem, not a refusal of us."""
    assert blocked.classify(status) == ""


def test_a_challenge_page_served_with_200_is_blocked():
    """The status code cannot be the check: every marker in the list arrives
    with a success status (or, at ACS, a 404 with 57 KB of HTML)."""
    body = (b'<html><head><base href="https://www.google.com/recaptcha/'
            b'challengepage/"></head><body>' + b"x" * 300 + b"</body></html>")
    assert blocked.classify(200, body) == "blocked"


def test_an_aws_waf_interstitial_is_blocked():
    assert blocked.classify(202, b"<html><script>window.gokuProps = {}</script>")


def test_a_real_document_is_not_a_challenge():
    assert not blocked.looks_like_challenge_body(b"%PDF-1.5\n" + b"x" * 400)
    assert not blocked.looks_like_challenge_body(b"")


def test_the_window_is_bounded():
    """A challenge announces itself in its head. Reading further would only be
    a chance to match a phrase inside a real document that quotes one."""
    quoting_document = b"%PDF-1.5\n" + b"x" * 4000 + b"Just a moment..."
    assert not blocked.looks_like_challenge_body(quoting_document)


# --------------------------------------------------------------------------
# One definition, not three
# --------------------------------------------------------------------------


def test_one_definition_of_a_bot_challenge():
    """Before this, "is this a challenge page" had three separate answers --
    supplementary.py's body markers, repository_waf.py's AWS WAF markers and
    request_drafts.py's title regex -- so a page one module learned to
    recognise stayed invisible to the other two.
    """
    from fetchpdf.retrieval import repository_waf, supplementary

    assert supplementary._CHALLENGE_MARKERS is blocked.CHALLENGE_BODY_MARKERS
    assert repository_waf._CHALLENGE_MARKERS is blocked.WAF_CHALLENGE_MARKERS


def test_the_draft_pass_reads_the_same_title_rule():
    """classify_block's own measurement stands: a 403 is a page that opens for
    a person, and only a challenge TITLE is a hard wall."""
    from fetchpdf.retrieval import request_drafts

    assert request_drafts.classify_block(403, "", 0) == request_drafts.MANUAL_LIKELY
    assert request_drafts.classify_block(403, "Just a moment...", 0) == \
        request_drafts.BLOCKED_HARD
    assert blocked.CHALLENGE_TITLE_RE.match("Just a moment...")


def test_the_figure_pass_reads_the_same_statuses():
    """`blocked:403` in a figure manifest and `blocked` in a supplement
    manifest must mean the same thing, or the report cannot merge them."""
    from fetchpdf.retrieval import figures

    class Outcome:
        def __init__(self, status):
            self.status = status

    assert figures._failure_status(Outcome(403)) == "blocked:403"
    assert figures._failure_status(Outcome(404)) == "download_failed:404"


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


class _Summary:
    def __init__(self, status="partial", blocked_urls=(), missing_declared=()):
        self.status = status
        self.blocked_urls = list(blocked_urls)
        self.missing_declared = list(missing_declared)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _report(tmp_path, si=None, figures=None):
    from fetchpdf.fetchpdf import _append_missing_si_to_report

    _append_missing_si_to_report(str(tmp_path), si or {}, "2026-09-07 12:00",
                                 _NullLock(), figure_statuses=figures or {})
    path = os.path.join(str(tmp_path), "missing_pdfs.html")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_report_names_the_url_to_open(tmp_path):
    """Sending somebody to the DOI to hunt for a supplement they cannot name
    leaves most of the work undone."""
    url = "https://www.ahajournals.org/doi/suppl/10.1161/STROKEAHA.111.628537"
    document = _report(tmp_path, si={"10.1161/x": _Summary(blocked_urls=[url])})

    assert url in document
    assert "fetch by hand" in document
    assert "CLICK-THROUGH" in document


def test_a_blocked_figure_reaches_the_same_report(tmp_path):
    """One list, not two: a figure PMC refused and a supplement Atypon refused
    are the same problem with the same answer."""
    document = _report(tmp_path, figures={
        "10.1073/pnas.1118373109": _Summary(
            status="partial", blocked_urls=["https://cdn.example/fig1.jpg"])})

    assert "https://cdn.example/fig1.jpg" in document
    assert "figures blocked" in document


def test_a_figure_that_is_simply_absent_is_not_listed(tmp_path):
    """Listing it would send a person after something no browser produces
    either -- the false-alarm failure the draft pass already refuses."""
    document = _report(tmp_path, figures={"10.1/x": _Summary(status="none_found")})

    assert document == ""
