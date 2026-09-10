"""PubPeer comments for a paper, captured as a dated record.

WHY A RECORD AND NOT A LOOKUP. Greg, 2026-08-17: "I would want this timestamped
because it captures a static timepoint in pubpeer. If there are no pubpeer
comments, I want a doc that is structured the same way but is empty."

Both halves of that matter, and the second is the one that is easy to get wrong.

A MISSING FILE SAYS NOTHING. An empty record dated today is a positive claim:
we asked PubPeer on this date and it had nothing. A paper with no record at all
is indistinguishable from a paper nobody checked, and the two get treated the
same way by anyone reading the corpus later -- which is how an absence quietly
becomes evidence.

"NO COMMENTS" IS ALWAYS A STATEMENT ABOUT A MOMENT. PubPeer accumulates: a paper
can acquire its first comment years after publication, and one of the three
comments on the Stapel paper this module was tested against was posted in 2025,
fourteen years after the article and nine after the first. A clean PubPeer record
quoted without its date is a claim that may already be false.

THE ONE FAILURE THIS MODULE EXISTS TO PREVENT is "we could not ask" being
recorded as "there was nothing to find". They are different facts and only one
of them is about the paper. So `n_comments` is None on any error and 0 only when
PubPeer actually answered with an empty list -- a caller that renders a null as
a zero has to do it deliberately, rather than by not noticing.

TWO SOURCES, because one endpoint does not carry everything:

  the v3 API      POST /v3/publications with a devkey. Returns the publication,
                  a comment COUNT, commenter names and the PubPeer URL. It does
                  NOT return comment bodies.
  the public page GET the publication URL. Its markup embeds the full comment
                  payload as entity-escaped JSON in a Vue attribute,
                  `<comment-timeline :data-comments="[...]">`.

`source` records which of them answered, because their completeness guarantees
differ and a record that does not say where it came from cannot be audited.

MEASURED 2026-08-17 while building this:
  10.1007/s11031-011-9216-y   3 comments, bodies recovered, earliest from
                              Statcheck in 2016 and the latest May 2025
  10.3349/ymj.2009.50.1.55    {"status":"good","feedbacks":[]} -- a real zero
  10.1073/pnas.0801268105     a real zero
  10.1002/eat.24415           a real zero
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

API = "https://pubpeer.com/v3/publications"

#: The API rejects a request with no devkey outright (HTTP 422,
#: {"errors":{"devkey":["The devkey field is required."]}}), so this is not
#: optional. Request one from PubPeer rather than inventing a value: the field
#: was observed 2026-08-17 not to be validated, and building on that would be
#: relying on a bug in someone else's service to keep working.
DEVKEY_ENV = "PUBPEER_DEVKEY"

_TIMEOUT = 30
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) fetchpdf/pubpeer "
       "(research use; contact via repository)")

#: The Vue attribute the publication page carries its comments in. Matched
#: non-greedily up to the quote that closes the attribute.
_COMMENTS_ATTR = re.compile(r':data-comments="(.*?)"\s*(?:[:a-zA-Z-]+=|>)', re.S)

#: Keys every record carries, present or absent, so a consumer can read the
#: empty case with the same code as the full one. THIS TUPLE IS THE CONTRACT.
RECORD_KEYS = (
    "doi", "fetched_at_utc", "source", "endpoint",
    "n_comments", "publication", "comments", "error",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(doi: str, *, source: Optional[str] = None,
            n_comments: Optional[int] = None,
            publication: Optional[dict] = None,
            comments: Optional[list] = None,
            error: Optional[str] = None) -> dict:
    """One record, always the same shape.

    `n_comments` defaults to None rather than 0 on purpose. Zero is an
    assertion; None is the absence of one, and only PubPeer answering can turn
    the second into the first.
    """
    return {
        "doi": doi,
        "fetched_at_utc": _now(),
        "source": source,
        "endpoint": API,
        "n_comments": n_comments,
        "publication": publication or {},
        "comments": comments or [],
        "error": error,
    }


def devkey(explicit: Optional[str] = None) -> Optional[str]:
    return explicit or os.getenv(DEVKEY_ENV)


def _strip_html(fragment: str) -> str:
    """Comment bodies arrive as HTML. Keep the text, keep the line breaks."""
    text = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _parse_comments(page_html: str, permalink: str) -> list[dict]:
    m = _COMMENTS_ATTR.search(page_html)
    if not m:
        return []
    try:
        raw = json.loads(_html.unescape(m.group(1)))
    except (ValueError, TypeError):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        inner = c.get("inner_id")
        out.append({
            "id": c.get("id"),
            "inner_id": inner,
            "posted_at": c.get("accepted_at"),
            "author_display": (c.get("public_name") or "").strip() or None,
            "is_from_author": bool(c.get("is_from_author")),
            "body": _strip_html(c.get("html") or ""),
            "permalink": f"{permalink}#{inner}" if permalink and inner else permalink,
        })
    return out


def fetch(doi: str, *, key: Optional[str] = None, with_bodies: bool = True,
          session: Optional[requests.Session] = None) -> dict:
    """The PubPeer record for one DOI. Never raises; failures become `error`."""
    doi = (doi or "").strip()
    if not doi:
        return _record(doi, error="no DOI given")

    k = devkey(key)
    if not k:
        # NOT an empty record. "We had no credential" and "the paper has no
        # comments" are the two facts this module exists to keep apart.
        return _record(doi, error=(
            f"{DEVKEY_ENV} is not set, so PubPeer was never asked. This is NOT "
            "a finding of zero comments. Request a developer key from PubPeer "
            f"and put it in .env.local as {DEVKEY_ENV}=..."))

    s = session or requests.Session()
    s.headers.setdefault("User-Agent", _UA)

    try:
        r = s.post(API, params={"devkey": k}, json={"dois": [doi]},
                   timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return _record(doi, error=f"PubPeer API unreachable: "
                                  f"{type(exc).__name__}: {exc}")
    if r.status_code != 200:
        return _record(doi, error=f"PubPeer API returned HTTP {r.status_code}: "
                                  f"{r.text[:200]}")
    try:
        body = r.json()
    except ValueError:
        return _record(doi, error="PubPeer API returned unparseable JSON")
    if body.get("status") != "good":
        return _record(doi, error=f"PubPeer API status {body.get('status')!r}: "
                                  f"{json.dumps(body)[:200]}")

    feedbacks = body.get("feedbacks") or []
    if not feedbacks:
        # THE EMPTY RECORD. PubPeer answered and had nothing, so 0 is a fact.
        return _record(doi, source="api", n_comments=0)

    fb = feedbacks[0]
    journals = fb.get("journals") or [{}]
    permalink = fb.get("url") or ""
    publication = {
        "title": fb.get("title"),
        "journal": (journals[0] or {}).get("title"),
        "publisher": (journals[0] or {}).get("publisher"),
        "pubpeer_url": permalink,
        "last_commented_at": fb.get("last_commented_at"),
        "commenters": fb.get("users"),
        "total_comments_reported": fb.get("total_comments"),
    }
    reported = fb.get("total_comments")

    if not with_bodies or not permalink:
        return _record(doi, source="api", n_comments=reported,
                       publication=publication)

    try:
        page = s.get(permalink, timeout=_TIMEOUT)
        page.raise_for_status()
    except requests.RequestException as exc:
        # The count is real even though the bodies are not here. Say so rather
        # than dropping to zero or pretending the fetch was complete.
        return _record(doi, source="api", n_comments=reported,
                       publication=publication,
                       error=(f"comment COUNT came from the API but the bodies "
                              f"could not be fetched from {permalink}: "
                              f"{type(exc).__name__}: {exc}"))

    comments = _parse_comments(page.text, permalink)
    if reported and not comments:
        return _record(doi, source="api", n_comments=reported,
                       publication=publication,
                       error=(f"the API reports {reported} comment(s) and none "
                              "could be parsed from the publication page; its "
                              "markup has probably changed"))
    return _record(doi, source="api+html", n_comments=len(comments),
                   publication=publication, comments=comments)


def write_record(record: dict, out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return p


def _summary(rec: dict) -> str:
    if rec.get("error"):
        return f"UNKNOWN -- {rec['error']}"
    n = rec.get("n_comments")
    if n == 0:
        return f"no PubPeer comments as of {rec['fetched_at_utc']}"
    return (f"{n} PubPeer comment(s) as of {rec['fetched_at_utc']} -- "
            f"{(rec.get('publication') or {}).get('pubpeer_url')}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetchpdf-pubpeer",
        description="Capture a dated record of a paper's PubPeer comments.")
    ap.add_argument("doi", nargs="+", help="one or more DOIs")
    ap.add_argument("-o", "--out-dir", help="write <doi>.pubpeer.json per DOI")
    ap.add_argument("--devkey", help=f"overrides ${DEVKEY_ENV}")
    ap.add_argument("--no-bodies", action="store_true",
                    help="API metadata and counts only; skip the page fetch")
    a = ap.parse_args(argv)

    session = requests.Session()
    worst = 0
    for doi in a.doi:
        rec = fetch(doi, key=a.devkey, with_bodies=not a.no_bodies,
                    session=session)
        if a.out_dir:
            name = doi.replace("/", "--").replace(":", "-")
            path = write_record(rec, Path(a.out_dir) / f"{name}.pubpeer.json")
            print(f"{doi}: {_summary(rec)}\n  -> {path}")
        else:
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        if rec.get("error"):
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
