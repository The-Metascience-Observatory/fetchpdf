"""Files from a DSpace 7 institutional repository.

The long tail of green open access lives in institutional repositories, and a
large share of them run DSpace 7, which exposes a uniform REST API. That
uniformity is the point: one walk covers Harvard DASH, MIT, and hundreds of
others, with no scraping and no per-site special-casing.

    /server/api/pid/find?id=hdl:1/10976353   -> the item (302, then JSON)
    /server/api/core/items/<uuid>/bundles    -> ORIGINAL, TEXT, THUMBNAIL, ...
    .../bundles/<uuid>/bitstreams            -> the files
    .../bitstreams/<uuid>/content            -> the bytes

Verified end to end against Harvard DASH for 10.1073/pnas.1209746109: handle
1/10976353 resolves to item 73120378-b728-6bd4-e053-0100007fdf3b, whose
ORIGINAL bundle yields a real 403 KB PDF.

Only the ORIGINAL bundle is taken. TEXT holds a machine-extracted plaintext
copy of the same PDF -- keeping it would file a lossy duplicate of the article
as a supplementary file -- and THUMBNAIL is a preview image.

What this is NOT: a way to get supplementary material a publisher withholds.
The DASH deposit for that PNAS paper is the accepted manuscript, 23 pages,
article only -- no SI. Repositories hold the paper far more often than they
hold its supplements. This earns its place as a full-text source of last
resort, not as a supplement route, and the caller should treat what it returns
accordingly.
"""

import re
from typing import List, Optional
from urllib.parse import urlparse

from .supplement_index import (
    ROLE_SUPPLEMENT,
    ROLE_UNKNOWN,
    SupplementFile,
    classify_role,
)

#: DSpace's own paths, identical across installations.
_PID_FIND = "{base}/server/api/pid/find"
_BUNDLES = "{base}/server/api/core/items/{uuid}/bundles"

#: ORIGINAL is the deposited files. TEXT is an extracted-plaintext derivative
#: of them and THUMBNAIL a preview, so both would be duplicates of something we
#: already have.
_WANTED_BUNDLES = {"ORIGINAL"}

#: A handle looks like "1/10976353" or "hdl:1/10976353", possibly inside a URL.
#: Handle prefixes are not always bare integers: MIT's is "1721.1", so
#: (\d+/\d+) silently truncated 1721.1/12345 to 1/12345 -- a valid-looking
#: handle for a different item.
_HANDLE_RE = re.compile(r"(?:hdl:|/handle/)([\d.]+/\w+)")

#: Harvard's purl form carries the item id with no handle prefix at all:
#: nrs.harvard.edu/urn-3:HUL.InstRepos:10976353. The DASH handle is always
#: "1/<that id>", and the purl host is not the repository, so both have to be
#: rewritten rather than parsed out of the URL.
_HARVARD_PURL_RE = re.compile(r"urn-3:HUL\.InstRepos:(\d+)", re.I)

_MAX_FILES = 40

#: classify_role returns ROLE_UNKNOWN for most repository filenames, which the
#: supplementary pass keeps anyway. Defaulting to SUPPLEMENT is the safer of
#: the two errors: a deposit's extra files are usually appendices, and dropping
#: one loses more than keeping a duplicate costs.
ROLE_UNKNOWN_FALLBACK = ROLE_SUPPLEMENT


def dspace_handle(url_or_handle: Optional[str]):
    """(base_url, handle) for a DSpace landing page, or None.

    Accepts what OpenAlex actually gives out -- an nrs.harvard.edu purl, a
    /handle/ URL, or a bare handle with an explicit base.
    """
    if not url_or_handle:
        return None
    text = str(url_or_handle).strip()
    purl = _HARVARD_PURL_RE.search(text)
    if purl:
        return "https://dash.harvard.edu", f"1/{purl.group(1)}"
    match = _HANDLE_RE.search(text)
    if not match:
        return None
    handle = match.group(1)
    # Only a real URL carries a repository to talk to. A bare "hdl:1721.1/999"
    # has no host, and urlparse would otherwise hand back the handle itself as
    # the netloc -- giving "https://hdl:1721.1" as a base URL.
    if "//" not in text:
        return None
    parsed = urlparse(text)
    host = parsed.netloc
    if not host or "." not in host:
        return None
    return f"{parsed.scheme or 'https'}://{host}", handle


def enumerate_dspace(ids, ctx, landing_url: Optional[str] = None) -> List[SupplementFile]:
    """Files in a DSpace 7 item, resolved from its handle.

    `landing_url` is the repository URL (OpenAlex's `landing_page_url` for a
    green location). Without one there is nothing to resolve -- DSpace has no
    DOI-based lookup -- so this declines rather than guessing.
    """
    target = landing_url or (ctx.scratch.get("dspace_landing_url") if ctx else None)
    parsed = dspace_handle(target)
    if not parsed:
        return []
    base, handle = parsed

    response = ctx.http.get(_PID_FIND.format(base=base),
                            params={"id": f"hdl:{handle}"},
                            headers={"Accept": "application/json"},
                            timeout=30, polite=False)
    if not response.ok:
        ctx.log(f"    dspace: {base} handle {handle} -> HTTP {response.status}")
        return []
    try:
        item = response.json() or {}
    except ValueError:
        return []
    uuid = item.get("uuid")
    if not uuid:
        return []

    bundles = ctx.http.get(_BUNDLES.format(base=base, uuid=uuid),
                           headers={"Accept": "application/json"},
                           timeout=30, polite=False)
    if not bundles.ok:
        return []
    try:
        embedded = (bundles.json() or {}).get("_embedded") or {}
    except ValueError:
        return []

    files: List[SupplementFile] = []
    for bundle in embedded.get("bundles") or []:
        if str(bundle.get("name") or "").upper() not in _WANTED_BUNDLES:
            continue
        href = ((bundle.get("_links") or {}).get("bitstreams") or {}).get("href")
        if not href:
            continue
        listing = ctx.http.get(href, headers={"Accept": "application/json"},
                               timeout=30, polite=False)
        if not listing.ok:
            continue
        try:
            bitstreams = ((listing.json() or {}).get("_embedded") or {}).get("bitstreams") or []
        except ValueError:
            continue
        for index, bitstream in enumerate(bitstreams):
            if len(files) >= _MAX_FILES:
                break
            content = ((bitstream.get("_links") or {}).get("content") or {}).get("href")
            name = bitstream.get("name") or ""
            if not content or not name:
                continue
            # A repository deposit is usually the ARTICLE, not a supplement, so
            # let the shared heuristic decide rather than assuming either way.
            role = classify_role(name)
            if role == ROLE_UNKNOWN:
                # "unknown" is truthy, so an `or` fallback would never fire.
                role = ROLE_UNKNOWN_FALLBACK
            files.append(SupplementFile(
                name=name,
                url=content,
                provider="dspace_files",
                listing_index=index,
                size_bytes=bitstream.get("sizeBytes"),
                checksum=_checksum_of(bitstream),
                role=role,
                origin_doi=getattr(ids, "doi", None),
                extra={"dspace_base": base, "handle": handle, "item": uuid},
            ))
    if files:
        ctx.log(f"    dspace: {len(files)} file(s) from {base} {handle}")
    return files


def _checksum_of(bitstream) -> Optional[str]:
    """DSpace's checkSum block as the "md5:..." form SupplementFile expects."""
    block = bitstream.get("checkSum") or {}
    value = block.get("value")
    algorithm = str(block.get("checkSumAlgorithm") or "").lower()
    if value and algorithm:
        return f"{algorithm}:{value}"
    return None
