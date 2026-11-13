"""File listings for the data repositories a paper's deposits live in, beyond
the five in supplement_index (Zenodo, figshare.com, OSF, Dryad, Dataverse).

Every enumerator here is reached through a *deposit* DOI that the link
services already tied to the article (supplement_graph._files_in_repository),
never through the article's own DOI. That is why none of them asks whether a
file is the paper: by the time one runs, the deposit is known to be data.

All were verified unauthenticated against live deposits on 2026-11-13. Counts
are DataCite records declaring IsSupplementTo, same date.

  figshare portals   publisher and institution figshares outside 10.6084
                     (GSA 10.25386/7, ASHA, Sheffield, Loughborough, UCL...),
                     plus figshare *collections* (`...figshare.c.NNN`), which
                     is how Springer Nature groups one article's files.
  Mendeley Data      10.17632, 28k.
  PANGAEA            10.1594/PANGAEA.N, 20k. One file per DOI.
  4TU.ResearchData   10.4121, 6k. Runs Djehuty, whose API is figshare-shaped.
  Science Data Bank  10.57760 / 10.11922, 5k.

Two repositories were tried and deliberately have no enumerator:

  SAGE figshare (10.25384, 314k) -- api.figshare.com answers 404 "Entity not
      found" for every SAGE item and collection id, ndownloader answers 401,
      and the landing pages sit behind a WAF. SAGE supplements have to come
      from the publisher's own page instead.
  ICPSR / openICPSR (10.3886, 11k) -- every URL is a Cloudflare challenge, and
      downloads need a login and an accepted terms-of-use besides.
"""

import re
import urllib.parse
from typing import List, Optional

from .supplement_index import (
    ROLE_SUPPLEMENT,
    SupplementFile,
    _as_int,
    enumerate_figshare,
)

FIGSHARE_API = "https://api.figshare.com/v2"
FOURTU_API = "https://data.4tu.nl/v2"
MENDELEY_API = "https://data.mendeley.com/public-api/datasets/{dataset_id}"
PANGAEA_DOI = "https://doi.pangaea.de/10.1594/PANGAEA.{number}"
SCIDB_LISTING = "https://www.scidb.cn/api/gin-sdb-filetree/public/file/childrenFileListByPath"
SCIDB_DOWNLOAD = "https://www.scidb.cn/api/sdb-download-service/downloadFileMole?fileId={file_id}"
DATACITE_DOI = "https://api.datacite.org/dois/{doi}"

#: Ceilings on how much one deposit may cost to list. A deposit past any of
#: these is a data release, not a paper's supplement.
_MAX_COLLECTION_ITEMS = 60
_MAX_FOLDERS = 60
_MAX_LISTING_PAGES = 20
_PAGE_SIZE = 100

_VERSION_SUFFIX_RE = re.compile(r"\.v\d+$")


def _same_deposit(left, right) -> bool:
    """Whether two DOIs are one deposit, whatever version each names."""
    def base(value):
        return _VERSION_SUFFIX_RE.sub("", str(value or "").strip().lower())
    return bool(base(left)) and base(left) == base(right)


def _json(ctx, url, **kwargs):
    """GET and decode, or None. Never raises into the supplementary pass."""
    try:
        response = ctx.http.get(url, timeout=30, polite=False, **kwargs)
        if not response.ok:
            return None
        return response.json()
    except Exception:
        return None


# -- figshare-shaped APIs (figshare portals, 4TU) -----------------------------


def _figshare_shaped_files(article: dict, provider: str, origin_doi: str,
                           offset: int = 0) -> List[SupplementFile]:
    """The `files` array of a figshare-API article record."""
    if not isinstance(article, dict):
        return []
    # Embargoed and confidential items list their files and refuse them;
    # metadata-only records have none worth the request.
    if article.get("is_embargoed") or article.get("is_confidential") \
            or article.get("is_metadata_record"):
        return []
    files = []
    for entry in article.get("files") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("download_url")
        # is_link_only entries are not files: download_url serves the landing
        # page of whatever external resource was linked, as HTML, with a 200.
        if not url or entry.get("is_link_only") or entry.get("is_incomplete"):
            continue
        checksum = entry.get("supplied_md5") or entry.get("computed_md5")
        files.append(SupplementFile(
            name=entry.get("name") or "",
            url=url,
            provider=provider,
            listing_index=offset + len(files),
            mimetype=entry.get("mimetype"),
            size_bytes=_as_int(entry.get("size")),
            checksum=f"md5:{checksum}" if checksum else None,
            role=ROLE_SUPPLEMENT,
            origin_doi=origin_doi,
            extra={"article": article.get("uuid") or article.get("id")},
        ))
    return files


def _collection_files(ctx, api: str, collection_id: str, provider: str,
                      origin_doi: str) -> List[SupplementFile]:
    """Every item of a figshare-shaped collection, one article request each."""
    listing = _json(ctx, f"{api}/collections/{collection_id}/articles",
                    params={"page_size": _PAGE_SIZE})
    if not isinstance(listing, list):
        return []
    if len(listing) > _MAX_COLLECTION_ITEMS:
        ctx.log(f"    {provider}: collection {collection_id} has {len(listing)}+ items, "
                f"listing the first {_MAX_COLLECTION_ITEMS}")
    routed = ctx.scratch.setdefault("_routed_deposit_dois", set())
    files: List[SupplementFile] = []
    for item in listing[:_MAX_COLLECTION_ITEMS]:
        if not isinstance(item, dict):
            continue
        item_id = item.get("uuid") or item.get("id")
        if not item_id:
            continue
        # The items usually carry their own IsSupplementTo and would be
        # routed again under their own DOI; this listing already has them.
        item_doi = str(item.get("doi") or "").strip().lower()
        if item_doi:
            routed.add(item_doi)
            routed.add(_VERSION_SUFFIX_RE.sub("", item_doi))
        article = _json(ctx, f"{api}/articles/{item_id}")
        files.extend(_figshare_shaped_files(article, provider, origin_doi,
                                            offset=len(files)))
    return files


#: `<anything>.c.<digits>`: a collection, on figshare.com and every portal.
_FIGSHARE_COLLECTION_RE = re.compile(r"[./]c\.(\d+)(?:\.v\d+)?$")
#: A portal DOI ends in the figshare item id: `genetics.13132976.v1`,
#: `shef.data.34975381`, `04/32676582`.
_FIGSHARE_PORTAL_ITEM_RE = re.compile(r"[./](\d{5,})(?:\.v(\d+))?$")


def enumerate_figshare_deposit(ids, ctx) -> List[SupplementFile]:
    """A figshare-hosted deposit of any kind: figshare.com item, publisher or
    institution portal item, or collection.

    The plain figshare.com item (`10.6084/m9.figshare.N`) goes to the original
    enumerator. The other two are what it could not see: its id pattern needs
    the literal `figshare.<digits>`, which a collection (`figshare.c.N`) and a
    portal DOI (`10.25387/g3.8230757`) both lack.

    A portal id is only a trailing number, so the article it resolves to must
    name this DOI back before its files are believed.
    """
    doi = (ids.doi or "").strip().lower()
    if not doi:
        return []

    collection = _FIGSHARE_COLLECTION_RE.search(doi)
    if collection:
        return _collection_files(ctx, FIGSHARE_API, collection.group(1),
                                 "figshare_files", doi)

    if re.search(r"figshare\.\d+", doi):
        return enumerate_figshare(ids, ctx)

    item = _FIGSHARE_PORTAL_ITEM_RE.search(doi)
    if not item:
        return []
    article_id, version = item.group(1), item.group(2)
    url = f"{FIGSHARE_API}/articles/{article_id}"
    if version:
        url += f"/versions/{version}"
    article = _json(ctx, url)
    if not isinstance(article, dict) or not _same_deposit(article.get("doi"), doi):
        return []
    return _figshare_shaped_files(article, "figshare_files", doi)


_FOURTU_COLLECTION_RE = re.compile(r"^10\.4121/c\.(\d+)(?:\.v\d+)?$")
_FOURTU_ITEM_RE = re.compile(
    r"^10\.4121/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+)"
    r"(?:\.v(\d+))?$")


def enumerate_4tu(ids, ctx) -> List[SupplementFile]:
    """4TU.ResearchData. Same record shape as figshare, different host.

    Three DOI forms: a bare UUID or numeric id (fetched directly), the legacy
    `10.4121/uuid:...` (resolved through ?doi=, which wants the DOI exactly as
    registered), and `10.4121/c.N` collections.
    """
    doi = (ids.doi or "").strip().lower()
    if not doi.startswith("10.4121/"):
        return []

    collection = _FOURTU_COLLECTION_RE.match(doi)
    if collection:
        return _collection_files(ctx, FOURTU_API, collection.group(1),
                                 "fourtu_files", doi)

    item = _FOURTU_ITEM_RE.match(doi)
    if item:
        url = f"{FOURTU_API}/articles/{item.group(1)}"
        if item.group(2):
            url += f"/versions/{item.group(2)}"
        article = _json(ctx, url)
    else:
        found = _json(ctx, f"{FOURTU_API}/articles", params={"doi": doi})
        first = found[0] if isinstance(found, list) and found else {}
        item_id = (first or {}).get("uuid") or (first or {}).get("id")
        article = _json(ctx, f"{FOURTU_API}/articles/{item_id}") if item_id else None

    if not isinstance(article, dict):
        return []
    return _figshare_shaped_files(article, "fourtu_files", doi)


# -- Mendeley Data ------------------------------------------------------------

_MENDELEY_RE = re.compile(r"^10\.17632/([a-z0-9]+)(?:\.(\d+))?$")


def _mendeley_files(entries, origin_doi: str, offset: int = 0) -> List[SupplementFile]:
    files = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        details = entry.get("content_details") or {}
        url = details.get("download_url")
        if not url:
            continue
        digest = details.get("sha256_hash")
        files.append(SupplementFile(
            name=entry.get("filename") or "",
            url=url,
            provider="mendeley_files",
            listing_index=offset + len(files),
            mimetype=details.get("content_type"),
            size_bytes=_as_int(details.get("size")),
            checksum=f"sha256:{digest}" if digest else None,
            role=ROLE_SUPPLEMENT,
            origin_doi=origin_doi,
            extra={"mendeley_file": entry.get("id")},
        ))
    return files


def enumerate_mendeley(ids, ctx) -> List[SupplementFile]:
    """Mendeley Data: `10.17632/<id>` or `10.17632/<id>.<version>`.

    The dataset record carries the latest version's files, flattened across
    folders, so the common case is one request. An older version is listed the
    way the site does it: the root folder, then each folder by id.
    """
    matched = _MENDELEY_RE.match((ids.doi or "").strip().lower())
    if not matched:
        return []
    dataset_id, version = matched.group(1), matched.group(2)
    base = MENDELEY_API.format(dataset_id=dataset_id)

    dataset = _json(ctx, base)
    if not isinstance(dataset, dict) or dataset.get("confidential"):
        return []
    latest = str(dataset.get("version") or "")
    if not version or version == latest:
        return _mendeley_files(dataset.get("files"), ids.doi)

    # `folder_id` and `version` are both required: without them the API
    # answers HTTP 200 with {"error": 400}.
    files = _mendeley_files(
        _json(ctx, base + "/files", params={"folder_id": "root", "version": version}),
        ids.doi)
    folders = _json(ctx, f"{base}/folders/{version}")
    for folder in (folders if isinstance(folders, list) else [])[:_MAX_FOLDERS]:
        folder_id = (folder or {}).get("id") if isinstance(folder, dict) else None
        if not folder_id:
            continue
        files.extend(_mendeley_files(
            _json(ctx, base + "/files",
                  params={"folder_id": folder_id, "version": version}),
            ids.doi, offset=len(files)))
    return files


# -- PANGAEA ------------------------------------------------------------------

_PANGAEA_RE = re.compile(r"^10\.1594/pangaea\.(\d+)$")
_PANGAEA_EXTENSIONS = {
    "text/tab-separated-values": ".tab",
    "application/zip": ".zip",
    "text/csv": ".csv",
}


def enumerate_pangaea(ids, ctx) -> List[SupplementFile]:
    """PANGAEA: one dataset per DOI, described as schema.org JSON-LD.

    The `distribution` entry that is not the HTML view is the data: a
    tab-separated table (?format=textfile), a binary file under
    download.pangaea.de, or for a parent series a zip of its members. PANGAEA
    declares neither a byte size nor a checksum, so the size cap can only act
    while the transfer is running.

    Datasets under moratorium say so (`isAccessibleForFree: false`) and answer
    401 to the download; they are skipped here rather than refused later.
    """
    matched = _PANGAEA_RE.match((ids.doi or "").strip().lower())
    if not matched:
        return []
    number = matched.group(1)
    record = _json(ctx, PANGAEA_DOI.format(number=number),
                   headers={"Accept": "application/ld+json"})
    if not isinstance(record, dict):
        return []
    if record.get("isAccessibleForFree") is False:
        ctx.log(f"    pangaea: {number} needs access rights, skipping")
        return []

    distribution = record.get("distribution") or []
    if isinstance(distribution, dict):
        distribution = [distribution]
    files = []
    for entry in distribution:
        if not isinstance(entry, dict):
            continue
        url = entry.get("contentUrl")
        mimetype = str(entry.get("encodingFormat") or "").strip().lower()
        if not url or mimetype == "text/html":
            continue
        basename = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        if "/files/" in url and basename:
            name = urllib.parse.unquote(basename)
        else:
            name = f"PANGAEA_{number}{_PANGAEA_EXTENSIONS.get(mimetype, '')}"
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="pangaea_files",
            listing_index=len(files),
            mimetype=mimetype or None,
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            extra={"pangaea_dataset": number},
        ))
    return files


# -- Science Data Bank --------------------------------------------------------

_SCIDB_RE = re.compile(r"^10\.(?:57760|11922)/sciencedb\.")
_SCIDB_DATASET_ID_RE = re.compile(r"[?&]dataSetId=([0-9a-z]+)", re.IGNORECASE)
_SCIDB_OK = 20000


def _scidb_dataset(doi: str, ctx):
    """(dataSetId, version) for a ScienceDB DOI, from its DataCite record.

    The DOI itself does not contain the id the file API wants; the registered
    landing URL does (`.../detail?dataSetId=<hex>`).
    """
    payload = _json(ctx, DATACITE_DOI.format(doi=urllib.parse.quote(doi, safe="")))
    attributes = (((payload or {}).get("data") or {}).get("attributes") or {}) \
        if isinstance(payload, dict) else {}
    matched = _SCIDB_DATASET_ID_RE.search(str(attributes.get("url") or ""))
    if not matched:
        return None, None
    # DataCite says "1.0.0" or "V2"; the file API says "V1", "V2".
    major = re.match(r"[vV]?(\d+)", str(attributes.get("version") or ""))
    return matched.group(1), f"V{major.group(1)}" if major else "V1"


def _scidb_children(ctx, dataset_id: str, version: str, path: str) -> list:
    """One folder's entries, following the listing's own paging."""
    entries: list = []
    for _ in range(_MAX_LISTING_PAGES):
        try:
            response = ctx.http.post_json(
                SCIDB_LISTING,
                {"dataSetId": dataset_id, "version": version, "path": path,
                 "lastIndex": len(entries), "pageSize": _PAGE_SIZE},
                timeout=30)
            payload = response.json() if response.ok else None
        except Exception:
            payload = None
        if not isinstance(payload, dict) or payload.get("code") != _SCIDB_OK:
            break
        batch = [entry for entry in payload.get("data") or [] if isinstance(entry, dict)]
        entries.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    return entries


def enumerate_sciencedb(ids, ctx) -> List[SupplementFile]:
    """Science Data Bank (scidb.cn), through the file tree the site itself reads.

    The tree is rooted at `/<version>` and listed a folder at a time by POST;
    files declare a size and an md5. Folders are walked breadth-first up to
    _MAX_FOLDERS.
    """
    doi = (ids.doi or "").strip().lower()
    if not _SCIDB_RE.match(doi):
        return []
    dataset_id, version = _scidb_dataset(doi, ctx)
    if not dataset_id:
        return []

    files: List[SupplementFile] = []
    pending, walked = [f"/{version}"], 0
    while pending and walked < _MAX_FOLDERS:
        path = pending.pop(0)
        walked += 1
        for entry in _scidb_children(ctx, dataset_id, version, path):
            entry_path = str(entry.get("path") or "")
            if entry.get("dir"):
                if entry_path:
                    pending.append(entry_path)
                continue
            file_id = entry.get("id")
            # `link` entries point at an external resource, not a stored file.
            if not file_id or entry.get("link"):
                continue
            root = f"/{version}/"
            name = entry_path[len(root):] if entry_path.startswith(root) \
                else (entry.get("fileName") or "")
            digest = entry.get("md5")
            files.append(SupplementFile(
                name=name or entry.get("fileName") or "",
                url=SCIDB_DOWNLOAD.format(file_id=file_id),
                provider="sciencedb_files",
                listing_index=len(files),
                size_bytes=_as_int(entry.get("size")),
                checksum=f"md5:{digest}" if digest else None,
                role=ROLE_SUPPLEMENT,
                origin_doi=ids.doi,
                extra={"sciencedb_dataset": dataset_id, "sciencedb_version": version},
            ))
    if pending:
        ctx.log(f"    sciencedb: {dataset_id} has more than {_MAX_FOLDERS} folders, "
                f"{len(pending)} not listed")
    return files


def figshare_portal_hint(landing_url: Optional[str]) -> bool:
    """Whether a deposit's registered landing page is a figshare portal the
    public API can list. SAGE's cannot be (see the module docstring)."""
    host = urllib.parse.urlparse(str(landing_url or "")).netloc.lower()
    return host.endswith("figshare.com") and not host.startswith("sage.")
