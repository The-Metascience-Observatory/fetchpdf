"""What supplementary files exist for a record, before anything is fetched.

Pure enumeration: every function here asks a provider what it has and returns a
list of SupplementFile. Nothing is downloaded, nothing is written, nothing is
filtered on desirability. Two callers consume it and they want different subsets,
so the filtering lives with them rather than here:

  * T4_SUPPLEMENT (sources/supplements.py) wants only machine-readable data,
    because there a supplement stands *in place of* the paper's full text and a
    supplementary PDF is no better than the PDF it was standing in for.
  * --pull-supplementary (supplementary.py) wants everything, because there a
    supplement stands *alongside* the paper and the caller asked for the lot.

Keeping enumeration in one place is what stops those two from drifting: the
repository listings were already being fetched twice with two different
predicates applied to them, once in the legacy chain and once at T4.

Archives are enumerated as archives, not expanded here. A zip's central
directory is at the end of the file, so its members are unknowable until the
whole thing has landed -- which is a decision about byte budgets, and belongs
with the caller that owns the cap.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

FIGSHARE_FILES = "https://api.figshare.com/v2/articles/{article_id}/files"
FIGSHARE_ARTICLE = "https://api.figshare.com/v2/articles/{article_id}"
ZENODO_RECORD = "https://zenodo.org/api/records/{record_id}"
OSF_GUID = "https://api.osf.io/v2/guids/{osf_id}/"
EPMC_SUPPL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
DRYAD_BASE = "https://datadryad.org"
DRYAD_DATASET = DRYAD_BASE + "/api/v2/datasets/{encoded_doi}"
DATAVERSE_DATASET = ("https://dataverse.harvard.edu/api/datasets/:persistentId/"
                     "?persistentId=doi:{doi}")
DATAVERSE_FILE = "https://dataverse.harvard.edu/api/access/datafile/{file_id}"

#: Dryad file listings paginate; a deposit with more pages than this is a
#: deposit whose tail we drop rather than walk forever.
_DRYAD_MAX_PAGES = 10

#: Roles. "figure" and "article" are the two things --pull-supplementary drops;
#: everything else is kept, including "unknown", because the caller asked for as
#: much as possible and a spurious file costs disk while a dropped one loses data.
ROLE_SUPPLEMENT = "supplement"
ROLE_FIGURE = "figure"
ROLE_ARTICLE = "article"
ROLE_UNKNOWN = "unknown"

#: How deep to follow OSF folders, and how many nodes to visit in total. OSF
#: projects can nest arbitrarily and a runaway walk would spend a record's whole
#: request budget on one deposit.
_OSF_MAX_DEPTH = 4
_OSF_MAX_NODES = 200


@dataclass
class SupplementFile:
    """One file a provider says exists, before anything is fetched."""

    name: str                          # remote filename as listed
    url: str                           # direct download URL
    provider: str                      # "figshare_files", "pmc_s3", ...
    listing_index: int = 0             # position in the provider's listing, as returned
    mimetype: Optional[str] = None     # as declared by the listing, if any
    size_bytes: Optional[int] = None   # declared; lets the cap refuse before transferring
    checksum: Optional[str] = None     # "md5:..." as the source declares it
    role: str = ROLE_UNKNOWN
    label: str = ""                    # JATS <label>/<caption>, when known
    origin_doi: Optional[str] = None   # the DOI this file was enumerated under
    is_archive: bool = False           # expand rather than keep (EPMC zip, arXiv tarball)
    polite: bool = False               # pass through to HttpClient.download
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def basename(self) -> str:
        """The listed name with any path or query string removed.

        Never used to build an output path -- supplementary.py numbers files
        instead -- but it is what dedupe and extension-guessing key on.
        """
        name = (self.name or "").split("?")[0].replace("\\", "/")
        return name.rsplit("/", 1)[-1].strip()


# -- role heuristics --------------------------------------------------------
#
# Only meaningful when the thing being enumerated is the article's own DOI. When
# a data DOI was reached through DataCite, everything in it is supplementary by
# construction and these are not consulted.

#: Filenames that are the paper itself, as each publisher's deposit names it.
_ARTICLE_NAME_RE = re.compile(
    r"(?i)^("
    r"(article|paper|manuscript|main[_\- ]?text|full[_\- ]?text|preprint|ms)\.pdf"
    r"|1-s2\.0-S\d+-main\.pdf"
    r"|PMC\d+(\.\d+)?\.pdf"
    r"|s\d{5}-\d{3}-\d{5}-\w\.pdf"
    r")$"
)

#: Filenames that are positively supplementary, across the publishers we reach.
_SUPPLEMENT_NAME_RE = re.compile(
    r"(?i)("
    r"_s\d+|_si_\d+|MOESM\d+|-sup-\d+|^mmc\d+|\.s\d{3}\b"
    r"|suppl|supporting|appendix|additional[_\- ]file"
    r"|(data|table|fig(ure)?|movie|video|dataset)[_\- ]?s\d+"
    r")"
)

#: Repository item types that mean "this record *is* the paper".
_ARTICLE_ITEM_TYPES = {"journal contribution", "preprint", "publication", "journalarticle"}


def classify_role(name: Optional[str], item_type: Optional[str] = None,
                  jats_supplements=None, jats_figures=None) -> str:
    """Best guess at whether a listed file is a supplement, a figure or the paper.

    Ordered cheapest-first and deliberately biased toward ROLE_SUPPLEMENT: the
    caller asked for as much as possible, so ambiguity resolves toward keeping.
    JATS ground truth wins when we have it, because it is the article's own
    statement about its own files rather than a guess at a filename.
    """
    base = (name or "").split("?")[0].rsplit("/", 1)[-1].strip()
    stem = base.lower()

    if jats_supplements and _matches_href(stem, jats_supplements):
        return ROLE_SUPPLEMENT
    if jats_figures and _matches_href(stem, jats_figures):
        return ROLE_FIGURE

    if _ARTICLE_NAME_RE.search(base):
        return ROLE_ARTICLE
    if _SUPPLEMENT_NAME_RE.search(base):
        return ROLE_SUPPLEMENT
    if item_type and str(item_type).strip().lower() in _ARTICLE_ITEM_TYPES:
        # The item is the paper, but a "publication" record still carries its
        # supplements alongside the PDF. Only the PDF itself is the article.
        return ROLE_ARTICLE if stem.endswith(".pdf") else ROLE_UNKNOWN
    return ROLE_UNKNOWN


def jats_sets(ctx) -> Tuple[set, set]:
    """The supplement/figure href sets E1 left in ctx.scratch, if it ran.

    Empty when there was no JATS to read, in which case classify_role falls back
    to its filename heuristics.
    """
    return (ctx.scratch.get("jats_supplements") or set(),
            ctx.scratch.get("jats_figures") or set())


def _matches_href(basename: str, hrefs) -> bool:
    """True if a filename matches a JATS xlink:href, extension-insensitively.

    JATS routinely omits the extension -- href "pone.0000308.s001" against a
    blob named "pone.0000308.s001.doc" -- so a bare equality test would miss
    almost every match.
    """
    if not basename:
        return False
    stem = basename.rsplit(".", 1)[0]
    for href in hrefs:
        other = str(href).split("?")[0].rsplit("/", 1)[-1].strip().lower()
        if not other:
            continue
        if basename == other or stem == other.rsplit(".", 1)[0]:
            return True
    return False


# -- Europe PMC -------------------------------------------------------------


def enumerate_epmc_archive(ids, ctx, include_inline_images: bool = False
                           ) -> List[SupplementFile]:
    """The EPMC supplementary bundle, as one archive to expand.

    Not fetched here: the caller downloads it under its own byte budget and
    expands the members, because a zip's member sizes are only knowable once the
    whole file has landed.

    includeInlineImage=no is the right default and the difference is not
    cosmetic. Verified on PMC1817752: the default returns 14 members / 132 KB,
    the article's entire media blob set with every figure and table image in it;
    with the flag it returns 4 members / 34 KB, exactly s001-s004. It also turns
    a vague answer into a clean negative -- PMC3339580 gives 404 with the flag
    and 371 KB of figures without it, and that article genuinely has no
    supplementary material (zero <supplementary-material> in its JATS, and its
    S3 media_urls are nine Fig*.jpg).

    The listing is speculative: the URL is constructed for any PMCID without
    checking that a bundle exists, so the endpoint's "nothing here" answers (a
    bare 404, or HTTP 200 wrapping an <errorBean> stub for non-OA articles) are
    negatives, not losses. extra["speculative"] tells the download side to file
    them under "no-bundle" instead of a lost reason -- on a 99-record corpus the
    old classification turned 40 clean negatives into phantom "partial" statuses.
    """
    if not ids.pmcid:
        return []
    url = EPMC_SUPPL.format(pmcid=ids.pmcid)
    if not include_inline_images:
        url += "?includeInlineImage=no"
    return [
        SupplementFile(
            name=f"{ids.pmcid}_SupplementaryFiles.zip",
            url=url,
            provider="europepmc_supplements",
            mimetype="application/zip",
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            is_archive=True,
            extra={"pmcid": ids.pmcid, "speculative": True},
        )
    ]


# -- Figshare ---------------------------------------------------------------


def enumerate_figshare(ids, ctx, with_item_type: bool = True) -> List[SupplementFile]:
    """Figshare's file listing.

    with_item_type costs one extra request and only affects `role`, so T4 -- which
    filters on _is_structured and never reads a role -- turns it off rather than
    paying for it on every figshare record.
    """
    article_id = match_id(ids.doi, r"figshare\.(\d+)")
    if not article_id:
        return []
    response = ctx.http.get(
        FIGSHARE_FILES.format(article_id=article_id), timeout=30, polite=False
    )
    if not response.ok:
        return []
    try:
        listing = response.json() or []
    except ValueError:
        return []

    item_type = _figshare_item_type(ctx, article_id) if with_item_type else None
    files = []
    for index, entry in enumerate(listing):
        if not isinstance(entry, dict):
            continue
        url = entry.get("download_url")
        if not url:
            continue
        # is_link_only entries are not files: download_url serves the landing
        # page of whatever external resource was linked, as HTML, with a 200.
        if entry.get("is_link_only"):
            continue
        name = entry.get("name") or ""
        checksum = entry.get("supplied_md5") or entry.get("computed_md5")
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="figshare_files",
            listing_index=index,
            mimetype=entry.get("mimetype"),
            size_bytes=_as_int(entry.get("size")),
            checksum=f"md5:{checksum}" if checksum else None,
            role=classify_role(name, item_type),
            origin_doi=ids.doi,
            extra={"figshare_article": article_id},
        ))
    return files


def _figshare_item_type(ctx, article_id) -> Optional[str]:
    """defined_type_name, which says whether the item *is* the paper.

    One extra request per figshare record; worth it because "journal
    contribution" vs "dataset" settles the article-vs-supplement question that
    filename heuristics only guess at.
    """
    response = ctx.http.get(
        FIGSHARE_ARTICLE.format(article_id=article_id), timeout=30, polite=False
    )
    if not response.ok:
        return None
    try:
        return ((response.json() or {}).get("defined_type_name")) or None
    except ValueError:
        return None


# -- Zenodo -----------------------------------------------------------------


def enumerate_zenodo(ids, ctx) -> List[SupplementFile]:
    record_id = match_id(ids.doi, r"zenodo\.(\d+)")
    if not record_id:
        return []
    response = ctx.http.get(
        ZENODO_RECORD.format(record_id=record_id), timeout=30, polite=False
    )
    if not response.ok:
        return []
    try:
        payload = response.json() or {}
    except ValueError:
        return []

    item_type = (((payload.get("metadata") or {}).get("resource_type") or {}).get("type"))
    files = []
    for index, entry in enumerate(payload.get("files") or []):
        if not isinstance(entry, dict):
            continue
        # Restricted records list their files with no links at all.
        url = ((entry.get("links") or {}).get("self"))
        if not url:
            continue
        name = entry.get("key") or ""
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="zenodo_files",
            listing_index=index,
            mimetype=entry.get("mimetype") or entry.get("type"),
            size_bytes=_as_int(entry.get("size")),
            checksum=_normalize_checksum(entry.get("checksum")),
            role=classify_role(name, item_type),
            origin_doi=ids.doi,
            extra={"zenodo_record": record_id},
        ))
    return files


# -- OSF --------------------------------------------------------------------


def enumerate_osf(ids, ctx) -> List[SupplementFile]:
    osf_id = match_id(ids.doi, r"osf\.io/([a-z0-9]+)") or match_id(ids.doi, r"/([a-z0-9]{5})$")
    if not osf_id:
        return []

    guid = ctx.http.get(OSF_GUID.format(osf_id=osf_id), timeout=30, polite=False)
    if not guid.ok:
        return []
    try:
        data = (guid.json() or {}).get("data") or {}
    except ValueError:
        return []

    files_url = _related_href(data, "files")
    if not files_url:
        return []

    files: List[SupplementFile] = []
    budget = [_OSF_MAX_NODES]
    for provider_url in _osf_provider_urls(ctx, files_url):
        _osf_walk(ctx, provider_url, ids, files, budget, depth=0, seen=set())
        if budget[0] <= 0:
            ctx.log(f"    OSF: stopped after {_OSF_MAX_NODES} nodes")
            break
    for index, entry in enumerate(files):
        entry.listing_index = index
    return files


def _osf_provider_urls(ctx, files_url) -> List[str]:
    response = ctx.http.get(files_url, timeout=30, polite=False)
    if not response.ok:
        return []
    try:
        entries = (response.json() or {}).get("data") or []
    except ValueError:
        return []
    urls = []
    for entry in entries:
        href = _related_href(entry, "files")
        if href:
            urls.append(href)
    return urls


def _osf_walk(ctx, url, ids, out: List[SupplementFile], budget, depth: int, seen: set) -> None:
    """Collect files under an OSF storage node, following folders.

    Folders arrive as entries with kind "folder" and links.download null. The
    T4 enumerator dropped them, and everything inside them with them -- fine when
    you only want a spreadsheet if one happens to be at the top level, wrong when
    the ask is "as many as possible" and the deposit is organised into
    directories.
    """
    if depth > _OSF_MAX_DEPTH or budget[0] <= 0 or url in seen:
        return
    seen.add(url)
    budget[0] -= 1

    response = ctx.http.get(url, timeout=30, polite=False)
    if not response.ok:
        return
    try:
        entries = (response.json() or {}).get("data") or []
    except ValueError:
        return

    for entry in entries:
        if budget[0] <= 0:
            return
        if not isinstance(entry, dict):
            continue
        attributes = entry.get("attributes") or {}
        name = attributes.get("name") or ""
        if (attributes.get("kind") or "").lower() == "folder":
            child = _related_href(entry, "files")
            if child:
                _osf_walk(ctx, child, ids, out, budget, depth + 1, seen)
            continue
        download = ((entry.get("links") or {}).get("download"))
        if not download:
            continue
        out.append(SupplementFile(
            name=name,
            url=download,
            provider="osf_files",
            mimetype=attributes.get("contentType") or attributes.get("content_type"),
            size_bytes=_as_int(attributes.get("size")),
            role=classify_role(name),
            origin_doi=ids.doi,
            extra={
                "osf_path": attributes.get("materialized_path") or attributes.get("path") or "",
            },
        ))


# -- Dryad ------------------------------------------------------------------


def enumerate_dryad(ids, ctx) -> List[SupplementFile]:
    """Dryad's file listing for the latest version of a deposit.

    Two hops (dataset -> version -> files), both free and unauthenticated, and
    case-insensitive on the DOI (verified live 2026-08-09). Every file carries
    a declared size and a sha-256, so the size cap can refuse before
    transferring. Reached almost exclusively through the link services --
    a paper's own DOI is never a Dryad DOI -- which is why this exists:
    both real dataset links the sweeps found were Dryad deposits the router
    could see and not fetch.
    """
    import urllib.parse

    dryad_doi = match_id(ids.doi or "", r"(10\.5061/dryad\.[a-z0-9./]+)")
    if not dryad_doi:
        return []
    encoded = urllib.parse.quote(f"doi:{dryad_doi}", safe="")

    dataset = ctx.http.get(DRYAD_DATASET.format(encoded_doi=encoded),
                           timeout=30, polite=False)
    if not dataset.ok:
        return []
    try:
        version_href = (((dataset.json() or {}).get("_links") or {})
                        .get("stash:version") or {}).get("href")
    except ValueError:
        return []
    if not version_href:
        return []

    files: List[SupplementFile] = []
    url = DRYAD_BASE + version_href + "/files"
    for _ in range(_DRYAD_MAX_PAGES):
        response = ctx.http.get(url, timeout=30, polite=False)
        if not response.ok:
            break
        try:
            payload = response.json() or {}
        except ValueError:
            break
        for entry in ((payload.get("_embedded") or {}).get("stash:files")) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "").lower() == "deleted":
                continue
            download = ((entry.get("_links") or {}).get("stash:download") or {}).get("href")
            if not download:
                continue
            name = entry.get("path") or ""
            digest = entry.get("digest")
            digest_type = str(entry.get("digestType") or "").replace("-", "")
            files.append(SupplementFile(
                name=name,
                url=DRYAD_BASE + download,
                provider="dryad_files",
                listing_index=len(files),
                mimetype=entry.get("mimeType"),
                size_bytes=_as_int(entry.get("size")),
                checksum=f"{digest_type}:{digest}" if digest and digest_type else None,
                role=classify_role(name, "dataset"),
                origin_doi=ids.doi,
                extra={"dryad_doi": dryad_doi},
            ))
        next_href = ((payload.get("_links") or {}).get("next") or {}).get("href")
        if not next_href:
            break
        url = DRYAD_BASE + next_href
    return files


# -- Dataverse --------------------------------------------------------------


def enumerate_dataverse(ids, ctx) -> List[SupplementFile]:
    """Harvard Dataverse (the 10.7910 prefix), latest version's files.

    One request; files declare size and md5. Restricted files are listed
    without being downloadable, so they are skipped the same way Zenodo's
    restricted records are.
    """
    dvn_doi = match_id(ids.doi or "", r"(10\.7910/dvn/[a-z0-9]+)")
    if not dvn_doi:
        return []

    api_url = DATAVERSE_DATASET.format(doi=dvn_doi)
    response = ctx.http.get(api_url, timeout=30, polite=False)
    data = None
    if response.ok:
        try:
            data = (response.json() or {}).get("data") or {}
        except ValueError:
            data = None

    if data is None:
        # An empty HTTP 202 is Harvard Dataverse's AWS WAF, not an empty
        # deposit -- and `return []` here is indistinguishable from "this
        # dataset has no files", which is how a trial's own replication data
        # went unfetched while the manifest recorded none_found.
        from .repository_waf import fetch_json_through_browser, looks_like_challenge

        body = getattr(response, "content", b"") or b""
        status = getattr(response, "status", 0)
        if not looks_like_challenge(body, status):
            return []
        ctx.log(f"    dataverse: HTTP {status} looks like a bot-challenge; "
                f"retrying through a headed browser")
        payload = fetch_json_through_browser(api_url, dvn_doi, ctx)
        if not payload:
            return []
        data = (payload or {}).get("data") or {}

    files: List[SupplementFile] = []
    for entry in ((data.get("latestVersion") or {}).get("files")) or []:
        if not isinstance(entry, dict) or entry.get("restricted"):
            continue
        datafile = entry.get("dataFile") or {}
        file_id = datafile.get("id")
        name = datafile.get("filename") or ""
        if not file_id or not name:
            continue
        checksum = datafile.get("md5") or \
            ((datafile.get("checksum") or {}).get("value")
             if isinstance(datafile.get("checksum"), dict) else None)
        files.append(SupplementFile(
            name=name,
            url=DATAVERSE_FILE.format(file_id=file_id),
            provider="dataverse_files",
            listing_index=len(files),
            mimetype=datafile.get("contentType"),
            size_bytes=_as_int(datafile.get("filesize")),
            checksum=_normalize_checksum(checksum),
            role=classify_role(name, "dataset"),
            origin_doi=ids.doi,
            extra={"dataverse_doi": dvn_doi},
        ))
    return files


def _related_href(entry: dict, relationship: str) -> Optional[str]:
    href = (
        ((entry.get("relationships") or {}).get(relationship) or {})
        .get("links", {})
        .get("related", {})
    )
    if isinstance(href, dict):
        return href.get("href")
    return None


# -- the registry -----------------------------------------------------------

#: Provider order, and therefore the order files are numbered on disk. A tuple
#: rather than a dict or a set because that order is load-bearing: it is the
#: primary sort key in supplementary.py, so appending a provider here must not be
#: able to renumber the files an earlier one already produced.
#:
#: The ordering rationale, in three parts:
#:
#:   * jats_manifest is first because it carries no files at all. It leaves the
#:     article's own supplement/figure sets in ctx.scratch, and every provider
#:     after it classifies against that ground truth instead of guessing from a
#:     filename.
#:   * Routes that declare a size and a checksum *before* transferring come next,
#:     so the size cap can refuse an oversized file for no bandwidth.
#:   * Archival copies (Europe PMC, PMC S3) beat publisher copies, because their
#:     filenames are stable and their bytes are the deposited originals.
#:
#: datacite_related is late because it fans out into the repository enumerators,
#: so anything it finds that was already reachable directly has been taken.
def _atypon_if_withheld(ids, ctx) -> List[SupplementFile]:
    """E19: the publisher's own SI, but only when Europe PMC refused ours.

    Costs a headed browser, so it is not something to run 46 times to find the
    one record that needs it. The EPMC provider leaves a breadcrumb in
    ctx.scratch when it hits the "not open access" refusal, and this reads it.

    Enumeration all happens before any download, so the refusal has not been
    seen yet at this point -- asking EPMC's search index directly is what makes
    the gate work here. `hasSuppl=Y` with `isOpenAccess=N` is exactly the state
    that produces the "not open access one" errorBean later, and it costs one
    cheap JSON call against a host we are already rate-limited against.
    """
    from .supplement_atypon import atypon_host_for, fetch_atypon_supplements

    doi = getattr(ids, "doi", None)
    if not atypon_host_for(doi):
        return []
    if not _epmc_withholds_supplements(ids, ctx):
        return []
    return fetch_atypon_supplements(ids, ctx)


def _epmc_withholds_supplements(ids, ctx, default_without_pmcid: bool = True) -> bool:
    """True when EPMC says supplements exist but the article is not open access.

    `default_without_pmcid` exists because the two callers want opposite things
    from a missing PMCID, and conflating them produced a real bug:

      * The E19 browser gate wants True -- an Atypon DOI that never reached
        Europe PMC should still get the publisher attempt.
      * The "SI WITHHELD" warning wants False. Claiming files exist and are
        being withheld, with no evidence that they exist at all, is exactly the
        false alarm this parameter prevents.
    """
    pmcid = getattr(ids, "pmcid", None)
    if not pmcid:
        return default_without_pmcid
    cached = ctx.scratch.get("_epmc_withholds")
    if cached is not None:
        return cached
    verdict = False
    try:
        response = ctx.http.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"PMCID:{pmcid}", "resultType": "core",
                    "format": "json"},
            timeout=25,
        )
        if response.ok:
            results = ((response.json() or {}).get("resultList") or {}).get("result") or []
            if results:
                record = results[0]
                verdict = (str(record.get("hasSuppl") or "").upper() == "Y"
                           and str(record.get("isOpenAccess") or "").upper() != "Y")
    except Exception:
        verdict = False          # never let a metadata lookup break the record
    ctx.scratch["_epmc_withholds"] = verdict
    return verdict


def _providers() -> Tuple[Tuple[str, Any], ...]:
    """Built lazily so the provider modules can import from this one."""
    from .supplement_graph import (
        enumerate_crossref_components,
        enumerate_datacite_related,
        enumerate_epmc_datalinks,
        enumerate_fulltext_scan,
        enumerate_scholix_related,
    )
    from .supplement_pmc import (
        enumerate_jats_declared,
        enumerate_jats_manifest,
        enumerate_pmc_s3,
    )
    from .supplement_atypon import fetch_atypon_supplements
    from .supplement_publishers import (
        enumerate_apa_supplemental,
        enumerate_elsevier_objects,
        enumerate_plos,
        enumerate_preprint_supplements,
        enumerate_springer_esm,
    )

    return (
        ("jats_manifest", enumerate_jats_manifest),          # E1  classifier only
        ("europepmc_supplements", enumerate_epmc_archive),   # E2
        ("pmc_s3", enumerate_pmc_s3),                        # E3
        ("elsevier_objects", enumerate_elsevier_objects),     # E10
        ("springer_esm", enumerate_springer_esm),             # E11
        ("plos", enumerate_plos),                             # E12
        ("preprint_supplements", enumerate_preprint_supplements),  # E13
        ("apa_supplemental", enumerate_apa_supplemental),     # E14
        ("figshare_files", enumerate_figshare),                # E5
        ("zenodo_files", enumerate_zenodo),                    # E6
        ("osf_files", enumerate_osf),                          # E7
        ("datacite_related", enumerate_datacite_related),      # E4
        ("crossref_components", enumerate_crossref_components),  # E9
        ("jats_declared", enumerate_jats_declared),            # E15 recovery
        ("scholix_related", enumerate_scholix_related),        # E16
        ("epmc_datalinks", enumerate_epmc_datalinks),          # E17
        ("fulltext_scan", enumerate_fulltext_scan),            # E18
        # E19 LAST on purpose: one headed browser per record. Only reached for
        # records Europe PMC has already said it is withholding, which on a
        # 46-record corpus was 1, not 46.
        ("atypon_suppl", _atypon_if_withheld),                 # E19
    )


#: The provider names, in order, without importing the provider modules. Kept in
#: step with _providers() by test_provider_names_match_the_registry.
PROVIDER_NAMES: Tuple[str, ...] = (
    "jats_manifest",
    "europepmc_supplements",
    "pmc_s3",
    "elsevier_objects",
    "springer_esm",
    "plos",
    "preprint_supplements",
    "apa_supplemental",
    "figshare_files",
    "zenodo_files",
    "osf_files",
    "datacite_related",
    "crossref_components",
    "jats_declared",
    "scholix_related",
    "epmc_datalinks",
    "fulltext_scan",
    "atypon_suppl",
)


def provider_rank(name: str) -> int:
    """Where a provider sits in the numbering order.

    Tolerant of the "datacite_related:zenodo_files" form, so a file reached by
    fanning out through DataCite ranks with DataCite rather than falling to the
    end -- the fan-out is where it came from.
    """
    root = str(name or "").split(":", 1)[0]
    try:
        return PROVIDER_NAMES.index(root)
    except ValueError:
        return len(PROVIDER_NAMES)


def enumerate_all(ids, ctx, providers=None) -> Tuple[List[SupplementFile], List[dict]]:
    """Ask every applicable provider what it has.

    Each provider is guarded individually, the same way engine._run_source
    guards a source: one repository serving malformed JSON must cost us that
    repository, not the record. The reports are what lets the manifest say
    "figshare was not applicable" and "zenodo errored" rather than collapsing
    both into an empty list.
    """
    providers = providers if providers is not None else _providers()
    found: List[SupplementFile] = []
    reports: List[dict] = []
    for name, enumerator in providers:
        try:
            files = enumerator(ids, ctx) or []
        except Exception as e:
            ctx.log(f"    ✗ {name} raised: {str(e)[:150]}")
            reports.append({
                "name": name,
                "status": "error",
                "reason": f"{type(e).__name__}: {str(e)[:150]}",
            })
            continue
        for entry in files:
            entry.provider = entry.provider or name
        found.extend(files)
        reports.append({
            "name": name,
            "status": "ok" if files else "nothing_listed",
            "listed": len(files),
        })
    return found, reports


# -- shared helpers ---------------------------------------------------------


def match_id(value: Optional[str], pattern: str) -> Optional[str]:
    if not value:
        return None
    m = re.search(pattern, value, re.IGNORECASE)
    return m.group(1) if m else None


def _as_int(value) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_checksum(value) -> Optional[str]:
    """Zenodo already sends "md5:...", others send a bare hex digest."""
    if not value:
        return None
    text = str(value).strip()
    return text if ":" in text else f"md5:{text}"
