"""E22: publisher supplements as archived by the Internet Archive.

A last-ditch fallback. Several publishers refuse every automated request for
their supplement pages (SAGE, INFORMS, AOM and PNAS answer 403 from a bot check;
Elsevier's file host and supp.apa.org are often unreachable too), yet the
Wayback Machine has crawled some of those same files. Its CDX index is an open
API, so this asks it one question per record: "which captures do you hold under
this article's supplement URL prefix?"

Measured on a 26-paper SAGE set: 5 papers had at least one archived supplement
file, 8 had only an archived listing page, 13 had nothing. So this recovers a
minority -- which is why it only runs when no publisher-supplement provider
listed anything (see supplement_index._wayback_if_nothing_found).

What comes back is a capture, not the live file: the manifest keeps both the
original publisher URL and the capture timestamp so a reader can tell.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import unquote, urljoin

from .supplement_index import ROLE_SUPPLEMENT, SupplementFile

CDX = "https://web.archive.org/cdx/search/cdx"
#: `id_` asks for the capture's original bytes, without the Wayback toolbar or
#: rewritten links.
RAW = "https://web.archive.org/web/{timestamp}id_/{url}"

#: Atypon-platform hosts, by DOI prefix. Wider than supplement_atypon's table on
#: purpose: that one lists hosts a headed browser gets through, this one lists
#: hosts whose /doi/suppl/ URLs the Archive may hold.
_ATYPON_HOSTS = {
    "10.1073": "www.pnas.org",
    "10.1126": "www.science.org",
    "10.1146": "www.annualreviews.org",
    "10.1177": "journals.sagepub.com",
    "10.1080": "www.tandfonline.com",
    "10.1287": "pubsonline.informs.org",
    "10.5465": "journals.aom.org",
}

_MAX_FILES = 20
#: Listing pages worth opening to find file links the prefix query missed.
_MAX_LISTINGS = 2
_SUPPL_HREF_RE = re.compile(r'href="([^"]*suppl_file/[^"]+)"', re.IGNORECASE)
_WAYBACK_PREFIX_RE = re.compile(r"^(?:https?:)?//web\.archive\.org/web/\d+[a-z_]*/", re.IGNORECASE)
_PAGE_SUFFIXES = (".html", ".htm", ".css", ".js", ".php", ".asp", ".aspx")


def url_prefixes(ids) -> List[str]:
    """The URL prefixes under which this article's supplements are published."""
    doi = (getattr(ids, "doi", None) or "").strip()
    if not doi:
        return []
    prefix = doi.split("/", 1)[0]
    out = []
    host = _ATYPON_HOSTS.get(prefix)
    if host:
        out.append(f"{host}/doi/suppl/{doi}")
    pii = getattr(ids, "elsevier_pii", None)
    if pii:
        out.append(f"ars.els-cdn.com/content/image/1-s2.0-{pii}-mmc")
    if prefix == "10.1037":
        code = doi.rsplit("/", 1)[-1].lower()
        if code:
            out.append(f"supp.apa.org/psycarticles/supplemental/{code}/")
    return out


def _captures(ctx, url: str, prefix: bool) -> List[dict]:
    """Successful captures at (or, with prefix, under) a URL; one per distinct URL."""
    params = {
        "url": url + "*" if prefix else url,
        "output": "json",
        "fl": "timestamp,original,mimetype,length",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "limit": "100" if prefix else "-1",
    }
    # polite=False: the Archive needs no contact address, so none is sent.
    response = ctx.http.get(CDX, params=params, timeout=60, polite=False)
    rows = None
    if response.ok:
        try:
            rows = response.json() if response.content.strip() else []
        except ValueError:
            rows = None   # the Archive's "temporarily offline" page, served as HTML
    if not isinstance(rows, list):
        # An outage is not an answer. Without this the manifest would say
        # "nothing listed" for a record the Archive was never actually asked about.
        from .supplement_atypon import note_blocked
        note_blocked(ctx, "wayback", f"https://web.archive.org/web/*/{url}*",
                     f"Internet Archive index unavailable (HTTP {response.status})")
        return []
    out = []
    for row in rows[1:]:
        if isinstance(row, list) and len(row) >= 4:
            out.append({"timestamp": row[0], "original": row[1],
                        "mimetype": row[2] or "", "length": row[3]})
    return out


def _is_page(capture: dict) -> bool:
    name = capture["original"].split("?")[0].lower()
    return "html" in capture["mimetype"].lower() or name.endswith(_PAGE_SUFFIXES)


def _listed_file_urls(ctx, capture: dict) -> List[str]:
    """Supplement-file links on an archived Atypon listing page."""
    response = ctx.http.get(
        RAW.format(timestamp=capture["timestamp"], url=capture["original"]),
        timeout=60, polite=False,
    )
    if not response.ok:
        return []
    urls = []
    for href in _SUPPL_HREF_RE.findall(response.text or ""):
        url = urljoin(capture["original"], href.replace("&amp;", "&"))
        url = _WAYBACK_PREFIX_RE.sub("", url)
        if url not in urls:
            urls.append(url)
    return urls


def _name(url: str) -> str:
    return unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def enumerate_wayback(ids, ctx) -> List[SupplementFile]:
    """Archived copies of the article's publisher-hosted supplement files."""
    files: List[SupplementFile] = []
    seen = set()

    def add(capture: dict) -> None:
        key = capture["original"].split("://", 1)[-1].lower()
        name = _name(capture["original"])
        if key in seen or not name or len(files) >= _MAX_FILES:
            return
        seen.add(key)
        files.append(SupplementFile(
            name=name,
            url=RAW.format(timestamp=capture["timestamp"], url=capture["original"]),
            provider="wayback",
            listing_index=len(files),
            mimetype=capture["mimetype"] or None,
            # CDX `length` is the compressed record size, not the file's: leave
            # size_bytes unset rather than declare a number that will not match.
            role=ROLE_SUPPLEMENT,
            origin_doi=getattr(ids, "doi", None),
            polite=False,
            extra={"original_url": capture["original"],
                   "wayback_timestamp": capture["timestamp"]},
        ))

    for prefix in url_prefixes(ids):
        captures = _captures(ctx, prefix, prefix=True)
        for capture in captures:
            if not _is_page(capture):
                add(capture)
        # A listing page may name files the prefix query did not return (they
        # were captured under another scheme or letter case). Look each one up.
        for listing in [c for c in captures if _is_page(c)][:_MAX_LISTINGS]:
            for url in _listed_file_urls(ctx, listing):
                if url.split("://", 1)[-1].lower() in seen:
                    continue
                for capture in _captures(ctx, url, prefix=False)[-1:]:
                    if not _is_page(capture):
                        add(capture)
    return files
