"""The PubPeer record must keep "we could not ask" apart from "there is nothing".

Greg, 2026-08-17: "If there are no pubpeer comments, I want a doc that is
structured the same way but is empty."

The empty half is the one that carries the risk. A missing file is
indistinguishable from a paper nobody checked; an empty record dated today is a
positive claim -- we asked, and PubPeer had nothing. So the two cases have to
have the SAME SHAPE, and the failure case has to be distinguishable from both.

The convention that enforces it: `n_comments` is 0 only when PubPeer actually
answered with an empty list, and None whenever anything went wrong. Zero is an
assertion; None is the absence of one. A caller that renders a null as a zero
now has to do it on purpose.

Offline throughout -- the network paths are faked. A test that needs PubPeer to
be up is not testing this module.
"""

import json

import pytest
import requests

from fetchpdf.retrieval import pubpeer
from fetchpdf.retrieval.pubpeer import RECORD_KEYS, fetch

DOI = "10.1007/s11031-011-9216-y"

PAGE = (
    '<div class="vertical-container">'
    '<comment-timeline :data-comments="[{&quot;id&quot;:58615,'
    '&quot;inner_id&quot;:1,&quot;html&quot;:&quot;p &lt; .05 was scanned&lt;br&gt;line two&quot;,'
    '&quot;public_name&quot;:&quot;Statcheck &quot;,'
    '&quot;accepted_at&quot;:&quot;2016-08-25T18:55:29.000000Z&quot;,'
    '&quot;is_from_author&quot;:false}]" :other="x">'
)

FEEDBACK = {
    "status": "good",
    "feedbacks": [{
        "id": DOI, "title": "Happiness as alchemy",
        "journals": [{"title": "Motivation and Emotion", "publisher": "Springer"}],
        "total_comments": 1, "last_commented_at": "2025-05-08 20:39:00",
        "users": "Statcheck", "url": "https://pubpeer.com/publications/ABC",
    }],
}


class _Resp:
    def __init__(self, payload=None, text="", status=200):
        self._payload, self.text, self.status_code = payload, text, status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    """Fakes the two calls the module makes, in order."""

    def __init__(self, post=None, get=None):
        self._post, self._get = post, get
        self.headers = {}

    def setdefault(self, *a):  # headers.setdefault reaches the dict, not this
        pass

    def post(self, *a, **k):
        if isinstance(self._post, Exception):
            raise self._post
        return self._post

    def get(self, *a, **k):
        if isinstance(self._get, Exception):
            raise self._get
        return self._get


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("PUBPEER_DEVKEY", "unit-test")


class TestTheRecordShapeIsTheContract:
    def test_a_populated_record_has_exactly_the_contract_keys(self):
        r = fetch(DOI, session=_Session(_Resp(FEEDBACK), _Resp(text=PAGE)))
        assert tuple(r) == RECORD_KEYS

    def test_an_empty_record_has_exactly_the_same_keys(self):
        r = fetch(DOI, session=_Session(_Resp({"status": "good", "feedbacks": []})))
        assert tuple(r) == RECORD_KEYS

    def test_an_error_record_has_exactly_the_same_keys(self):
        r = fetch(DOI, session=_Session(requests.ConnectionError("down")))
        assert tuple(r) == RECORD_KEYS

    def test_every_record_is_dated(self):
        for sess in (_Session(_Resp({"status": "good", "feedbacks": []})),
                     _Session(requests.ConnectionError("down"))):
            r = fetch(DOI, session=sess)
            assert r["fetched_at_utc"].endswith("Z")
            assert r["fetched_at_utc"].startswith("20")


class TestZeroIsAClaimAndNullIsNot:
    """The distinction the whole module exists for."""

    def test_a_real_zero_is_zero_with_no_error(self):
        r = fetch(DOI, session=_Session(_Resp({"status": "good", "feedbacks": []})))
        assert r["n_comments"] == 0
        assert r["error"] is None
        assert r["source"] == "api"

    def test_a_missing_key_is_null_and_never_zero(self, monkeypatch):
        monkeypatch.delenv("PUBPEER_DEVKEY", raising=False)
        r = fetch(DOI)
        assert r["n_comments"] is None
        assert "NOT a finding of zero comments" in r["error"]

    def test_an_unreachable_api_is_null_and_never_zero(self):
        r = fetch(DOI, session=_Session(requests.ConnectionError("down")))
        assert r["n_comments"] is None and r["error"]

    def test_a_non_200_is_null_and_never_zero(self):
        r = fetch(DOI, session=_Session(_Resp(None, "nope", status=500)))
        assert r["n_comments"] is None and "500" in r["error"]

    def test_unparseable_json_is_null_and_never_zero(self):
        r = fetch(DOI, session=_Session(_Resp(None, "<html>")))
        assert r["n_comments"] is None and r["error"]


class TestCommentBodies:
    def test_they_are_parsed_out_of_the_page(self):
        r = fetch(DOI, session=_Session(_Resp(FEEDBACK), _Resp(text=PAGE)))
        assert r["n_comments"] == 1
        assert r["source"] == "api+html"
        c = r["comments"][0]
        assert c["author_display"] == "Statcheck"
        assert c["posted_at"] == "2016-08-25T18:55:29.000000Z"
        assert c["permalink"].endswith("#1")

    def test_entities_are_decoded_and_breaks_kept(self):
        """The payload is entity-escaped inside an HTML attribute, so a body
        arrives as `p &lt; .05`. The reader wants `p < .05`."""
        r = fetch(DOI, session=_Session(_Resp(FEEDBACK), _Resp(text=PAGE)))
        body = r["comments"][0]["body"]
        assert "p < .05" in body
        assert "&lt;" not in body
        assert body.splitlines()[-1] == "line two"

    def test_a_failed_page_fetch_keeps_the_count_and_says_so(self):
        """The API's count is real even when the bodies are not here. Dropping
        to zero would convert our fetch failure into a fact about the paper."""
        r = fetch(DOI, session=_Session(_Resp(FEEDBACK),
                                        requests.ConnectionError("blocked")))
        assert r["n_comments"] == 1
        assert r["comments"] == []
        assert "bodies could not be fetched" in r["error"]

    def test_markup_that_stopped_parsing_is_reported_not_swallowed(self):
        """If PubPeer changes its markup, the count and the parse disagree.
        That must surface as an error rather than as a quiet zero."""
        r = fetch(DOI, session=_Session(_Resp(FEEDBACK), _Resp(text="<div/>")))
        assert r["n_comments"] == 1
        assert "markup has probably changed" in r["error"]

    def test_no_bodies_requested_is_metadata_only(self):
        r = fetch(DOI, with_bodies=False, session=_Session(_Resp(FEEDBACK)))
        assert r["source"] == "api" and r["n_comments"] == 1


class TestWriting:
    def test_the_record_round_trips_as_json(self, tmp_path):
        r = fetch(DOI, session=_Session(_Resp({"status": "good", "feedbacks": []})))
        p = pubpeer.write_record(r, tmp_path / "x.pubpeer.json")
        assert json.loads(p.read_text(encoding="utf-8")) == r
