"""The paper's figure images, fetched rather than merely classified.

WHY THIS IS NOT extract_images.py. That module dumps the bitstreams a PDF
carries inside it, for byte-identity work that any re-encoding destroys. This
one fetches the image files the publisher deposited for the article, for
records where there is no PDF to open -- or where the PDF's embedded stream is
a downsampled placement of an original that PMC still holds. Different input,
different evidence, and neither substitutes for the other.

WHAT ALREADY EXISTED, AND WHAT DID NOT. E1 in supplement_pmc.py reads the
article's JATS and separates `<fig>` hrefs from `<supplementary-material>`
hrefs. It then fetches neither: the figure set exists so the supplement
providers can tell a figure apart from data, and its own module docstring says
"E1 contributes no bytes". So the article's own statement about which files are
its figures was being computed and thrown away.

THE TWO ROUTES, IN ORDER, AND WHY THAT ORDER

  1. PMC's AWS Open Data mirror. For an open-access record the package holds
     the figure files themselves, and the bucket listing declares each object's
     size while the metadata document declares its md5 -- both before a single
     byte moves. Verified 2026-09-07 on PMC1817752: `media_urls` carries
     `pone.0000308.g001.jpg` and `.g002.jpg` beside the article's own PDF, XML
     and text renditions. A file from here is marked `original`.

  2. The PMC blob CDN. Its per-file paths (`cdn.ncbi.nlm.nih.gov/pmc/blobs/…`)
     are listed nowhere but the article page, so this route costs a page
     request -- unless `{stem}.fulltext.html` is already on disk, which it is
     for every record the pmc_html source served. A file from here is a
     publisher render, re-encoded, and is marked `render` rather than
     `original`: calling it original would invite byte-identity conclusions
     about PMC's pipeline dressed up as conclusions about the authors'.

  3. No JATS at all, but a page: the `<figure>` elements on the page carry both
     the label and the blob URL, so the page enumerates its own figures.

REFUSALS ARE NAMED, per record and per figure, and a manifest with zero
figures is still written. "We asked and PMC listed nothing" and "we never
asked" are different facts, and a missing file cannot tell them apart -- the
same rule the supplementary manifest is built on.

Nothing here can fail a record. Like the supplementary pass, this runs beside
retrieval and its outcome is the manifest, never the verdict.
"""

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree

from ._util import iso as _iso
from .http import redact

#: `{stem}_figures/` and `{stem}_figures.json`, mirroring the embedded-image
#: dump's `{stem}_images/` + `{stem}_images.json` so a reader who has seen one
#: layout can read the other.
FIGURES_DIR_SUFFIX = "_figures"
MANIFEST_SUFFIX = "_figures.json"
_SCHEMA_VERSION = 1

#: Per-file ceiling. Figures are small -- the largest in the working corpus is
#: under 4 MB -- so this is not a budget, it is a guard against a mislabelled
#: link handing back a multi-gigabyte video.
DEFAULT_MAX_FIGURE_BYTES = 100 * 1024 * 1024

#: HTTP answers that mean "a person could fetch this and we could not", as
#: opposed to "there is nothing here". Recorded per figure so the manifest
#: never presents a refusal as an absence.
_BLOCKED_STATUSES = frozenset({401, 403, 429})

#: The extensions PMC actually serves figures under, used only to match one
#: name against another -- never to decide what a file IS.
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff")

_BLOB_RE = re.compile(rb"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\"'\s<>]+")
_FIG_LABEL_RE = re.compile(r"(?i)\b(?:fig(?:ure)?s?\.?)\s*([0-9]+)\s*([A-Za-z])?")

_HTML_FIG_RE = re.compile(r"<figure\b([^>]*)>(.*?)</figure>", re.I | re.S)
_HTML_ID_RE = re.compile(r'\bid="([^"]+)"')
_HTML_IMG_RE = re.compile(
    r'<img\b[^>]*\bsrc="(https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^"]+)"', re.I)
_HTML_LABEL_RE = re.compile(
    r"<(?:h[1-6]|span|div|strong|b)\b[^>]*>\s*((?:Fig(?:ure)?\.?)\s*[0-9]+\s*[A-Za-z]?)", re.I)
_HTML_CAPTION_RE = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

#: Captions are recorded to make the manifest readable, not to be parsed. A
#: cap keeps one methods-length caption from dominating the file.
_CAPTION_MAX_CHARS = 1200


@dataclass
class FiguresSummary:
    """What the pass did. Never an input to whether the record succeeded."""

    #: ok | partial | none_found | refused | skipped | error. "refused" means we
    #: could not ask -- a fact about us -- and carries the reason in `refusal`;
    #: "none_found" means we asked and the article names no figures.
    status: str = "none_found"
    written: int = 0
    failed: int = 0
    bytes_written: int = 0
    manifest_path: Optional[str] = None
    directory: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    refusal: str = ""
    detail: str = ""
    #: Figure URLs a publisher or CDN refused to this client but serves to a
    #: person, for the missing-materials report.
    blocked_urls: List[str] = field(default_factory=list)

    def __bool__(self):
        return self.written > 0


# -- paths -------------------------------------------------------------------


def stem_for(save_path: str) -> str:
    from .engine import _stem_for

    return _stem_for(save_path)


def figures_dir_for(save_path: str) -> str:
    return stem_for(save_path) + FIGURES_DIR_SUFFIX


def manifest_path_for(save_path: str) -> str:
    return stem_for(save_path) + MANIFEST_SUFFIX


# -- enumeration -------------------------------------------------------------


def normalise_label(label: Optional[str], index: Optional[int] = None) -> Optional[str]:
    """`Fig. 3` / `Figure 3.` / `FIGURE 3a` -> `Figure 3A`.

    One spelling, so a consumer joining figures to captions or to a PDF harvest
    is comparing labels rather than publisher house style. A label that does not
    parse is kept verbatim: inventing `Figure 4` for something the paper calls
    `Scheme 1` would be worse than an unfamiliar string.
    """
    if label:
        match = _FIG_LABEL_RE.search(str(label))
        if match:
            return f"Figure {int(match.group(1))}{(match.group(2) or '').upper()}"
        return str(label).strip() or None
    return f"Figure {index}" if index else None


def figures_from_jats(content: bytes) -> List[dict]:
    """Every `<fig>` carrying a `<graphic>`: id, label, caption, href.

    Namespace-agnostic, because PMC article sets and Europe PMC's fullTextXML
    do not agree on whether the JATS namespace is declared.
    """
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []

    found: List[dict] = []
    for block in root.iter():
        if not _tag_is(block, "fig"):
            continue
        index = len(found) + 1
        label = caption = href = None
        for element in block.iter():
            if _tag_is(element, "label") and label is None:
                label = "".join(element.itertext()).strip()
            elif _tag_is(element, "caption") and caption is None:
                caption = " ".join("".join(element.itertext()).split())
            elif _tag_is(element, "graphic") and href is None:
                href = _href_of(element)
        if not href:
            # A <fig> whose content is a table or an inline formula names no
            # image, so there is nothing to fetch and nothing to refuse.
            continue
        found.append({
            "id": block.get("id") or f"fig{index}",
            "figure_label": normalise_label(label, index),
            "raw_label": label or "",
            "href": href,
            "caption": (caption or "")[:_CAPTION_MAX_CHARS],
        })
    return found


def figures_from_page(html: str) -> List[dict]:
    """Every `<figure>` on a PMC article page that carries a blob image.

    The enumeration for a record with a page and no JATS. Verified 2026-09-07
    on PMC1817752: each `<figure>` carries its label in an `obj_head` heading,
    its caption in `<figcaption>`, and the CDN URL as the `<img src>`.
    """
    found: List[dict] = []
    for match in _HTML_FIG_RE.finditer(html or ""):
        attributes, body = match.group(1), match.group(2)
        image = _HTML_IMG_RE.search(body)
        if not image:
            continue
        index = len(found) + 1
        identifier = _HTML_ID_RE.search(attributes)
        label = _HTML_LABEL_RE.search(body)
        caption = _HTML_CAPTION_RE.search(body)
        url = image.group(1)
        found.append({
            "id": identifier.group(1) if identifier else f"fig{index}",
            "figure_label": normalise_label(label.group(1) if label else None, index),
            "raw_label": label.group(1) if label else "",
            "href": url.rsplit("/", 1)[-1],
            "caption": (" ".join(_TAG_RE.sub(" ", caption.group(1)).split())
                        if caption else "")[:_CAPTION_MAX_CHARS],
            "url": url,
        })
    return found


def blob_urls_from_page(html: bytes, hrefs) -> Dict[str, str]:
    """href -> CDN URL, from the blob links the article page lists."""
    by_basename: Dict[str, str] = {}
    for url in _BLOB_RE.findall(html or b""):
        text = url.decode("utf-8", errors="replace")
        by_basename.setdefault(text.rsplit("/", 1)[-1].lower(), text)

    resolved: Dict[str, str] = {}
    for href in hrefs:
        for candidate in _basename_candidates(href):
            if candidate in by_basename:
                resolved[href] = by_basename[candidate]
                break
    return resolved


def _basename_candidates(href) -> List[str]:
    """Every filename the page might list this href under, best first.

    A JATS `<graphic xlink:href="pone.0000308.g001">` names no extension while
    PMC serves `pone.0000308.g001.jpg`, so exact matching alone loses most
    figures -- and "does it contain a dot" cannot tell the two apart, because
    the stem contains three. Where the href DOES carry an image extension the
    served one may still differ (a .tif deposited, a .jpg served), so the
    extension-stripped forms are tried last.
    """
    base = os.path.basename(str(href or "").replace("\\", "/")).lower()
    if not base:
        return []
    root, extension = os.path.splitext(base)
    candidates = [base] + [base + e for e in _IMAGE_EXTENSIONS]
    if extension in _IMAGE_EXTENSIONS:
        candidates += [root + e for e in _IMAGE_EXTENSIONS]
    return candidates


# -- the pass ----------------------------------------------------------------


def pull_for_record(raw_identifier, doi=None, pmid=None, save_path=None,
                    resolver=None, ladder=None, http=None,
                    max_file_bytes=DEFAULT_MAX_FIGURE_BYTES,
                    refresh=False, email=None, verbose=False,
                    delay=0.1) -> FiguresSummary:
    """Fetch one record's figure images into `{stem}_figures/`.

    Never raises for a network or provider failure; the outcome is the returned
    summary and the manifest beside the artifact.
    """
    if not save_path:
        return FiguresSummary(status="error", detail="no save_path")

    # Wiring borrowed rather than rebuilt, for the reason _wire's own docstring
    # gives: HostRateLimiter is lock-guarded but not a singleton, so a client
    # built per record multiplies the configured per-host rate by the worker
    # count. Same argument, same helper -- see repository_waf.py, which imports
    # supplement_atypon's browser helpers on the same reasoning.
    from .supplementary import _context, _resolve, _wire, read_manifest, write_manifest

    stem = stem_for(save_path)
    manifest_path = stem + MANIFEST_SUFFIX
    directory = stem + FIGURES_DIR_SUFFIX

    existing = read_manifest(manifest_path)
    if existing is not None and not refresh:
        reuse = _reusable(existing)
        if reuse is not None:
            return reuse

    ladder, http, resolver, _owns = _wire(resolver, http, ladder, save_path,
                                          email, verbose)
    ctx = _context(http, resolver, ladder, save_path, verbose, email, delay,
                   use_playwright=False)
    ids = _resolve(resolver, raw_identifier, doi, pmid)

    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "identifier": str(ids.doi or ids.best_id or ""),
        "identifiers_resolved": ids.to_dict() if hasattr(ids, "to_dict") else {},
        "stem": os.path.basename(stem),
        "completed_at": None,
        "status": "none_found",
        "pmcid": None,
        "figures_from": None,
        "page_source": None,
        "page_url": None,
        "limits": {"max_file_bytes": max_file_bytes},
        "figures": [],
        "counts": {"declared": 0, "written": 0, "failed": 0, "bytes_written": 0},
        "refusal": None,
    }

    try:
        return _run(manifest, manifest_path, directory, stem, ids, ctx, http,
                    max_file_bytes, write_manifest)
    except Exception as e:                      # noqa: BLE001 - never fail the record
        ctx.log(f"    figures: pass failed ({type(e).__name__}: {str(e)[:120]})")
        manifest["status"] = "error"
        manifest["refusal"] = {"kind": "error",
                               "detail": f"{type(e).__name__}: {str(e)[:150]}"}
        manifest["completed_at"] = _iso(time.time())
        written = write_manifest(manifest_path, manifest, verbose)
        return FiguresSummary(status="error", manifest_path=written,
                              directory=directory, refusal="error",
                              detail=f"{type(e).__name__}: {str(e)[:150]}")


def _run(manifest, manifest_path, directory, stem, ids, ctx, http,
         max_file_bytes, write_manifest) -> FiguresSummary:
    from .sources.pmc_html import fetch_pmc_article_page, pmcid_for

    def finish() -> FiguresSummary:
        manifest["completed_at"] = _iso(time.time())
        path = write_manifest(manifest_path, manifest, ctx.verbose)
        refusal = (manifest.get("refusal") or {}).get("kind", "")
        rows = manifest["figures"]
        return FiguresSummary(
            status=manifest["status"],
            written=manifest["counts"]["written"],
            failed=manifest["counts"]["failed"],
            bytes_written=manifest["counts"]["bytes_written"],
            manifest_path=path,
            directory=directory,
            refusal=refusal,
            detail=(manifest.get("refusal") or {}).get("detail", ""),
            paths=[os.path.join(directory, r["file"]) for r in rows if r.get("file")],
            blocked_urls=[r["url"] for r in rows
                          if str(r.get("status", "")).startswith("blocked:") and r.get("url")],
        )

    def refuse(kind: str, detail: str) -> FiguresSummary:
        manifest["status"] = "refused"
        manifest["refusal"] = {"kind": kind, "detail": detail}
        ctx.log(f"    figures: refused ({kind}) -- {detail}")
        return finish()

    pmcid = pmcid_for(ids, ctx)
    if not pmcid:
        return refuse("no_pmcid",
                      "no PMCID resolved and none in a JATS file on disk; "
                      "PMC is the only figure route this package has")
    manifest["pmcid"] = pmcid
    # Recorded on the identifier set rather than kept local: `learn` never
    # overwrites, so this only ever fills a blank, and the PMC providers below
    # read ids.pmcid directly.
    ids.learn("figures:pmcid", pmcid=pmcid)

    jats = _jats_bytes(ids, ctx)
    page = b""
    if jats:
        declared = figures_from_jats(jats)
        manifest["figures_from"] = "jats"
    else:
        # No article list without the page, so this is the one branch that has
        # to have it before it knows whether there is anything to fetch.
        page = _page_bytes(stem, ids, ctx, manifest, fetch_pmc_article_page) or b""
        if not page:
            return refuse("no_jats_or_page",
                          f"neither a JATS document nor the article page could "
                          f"be read for {pmcid}; the figure list lives in one "
                          f"and the file URLs in the other")
        declared = figures_from_page(page.decode("utf-8", errors="replace"))
        manifest["figures_from"] = "article_page"

    manifest["counts"]["declared"] = len(declared)
    if not declared:
        ctx.log(f"    figures: {pmcid} names no figure with an image")
        manifest["status"] = "none_found"
        return finish()

    mirror = _mirror_objects(ids, ctx)
    manifest["mirror_objects"] = len(mirror)

    # The page is asked for only when the mirror does not already cover every
    # figure. On an open-access record it usually does, and requesting a page
    # whose only purpose is to supply URLs we already have is a request PMC
    # should not have to serve.
    unresolved = [f for f in declared
                  if not any(c in mirror for c in _basename_candidates(f["href"]))
                  and not f.get("url")]
    urls: Dict[str, str] = {}
    if unresolved:
        if not page:
            page = _page_bytes(stem, ids, ctx, manifest, fetch_pmc_article_page) or b""
        if page:
            manifest["page_blob_links"] = len(set(_BLOB_RE.findall(page)))
            urls = blob_urls_from_page(page, [f["href"] for f in unresolved])
        elif len(unresolved) == len(declared):
            return refuse("no_jats_or_page",
                          f"the Open Data package holds none of the "
                          f"{len(declared)} figure(s) {pmcid} declares and the "
                          f"article page could not be read; the file URLs live "
                          f"in one or the other")

    # Only a record where NOTHING is obtainable is refused. Where the mirror
    # covers some of the figures the rest are recorded per figure, because
    # "we got three of five" is a better outcome than a refusal and has to be
    # reported as the shortfall it is.
    if not urls and len(unresolved) == len(declared):
        return refuse("page_without_figure_links",
                      f"nothing lists a file for any of the {len(declared)} "
                      f"figure(s) {pmcid} declares: the Open Data package holds "
                      f"none of them and the article page carries no "
                      f"cdn.ncbi.nlm.nih.gov/pmc/blobs link")

    _fetch_all(declared, mirror, urls, directory, manifest, ctx, http, max_file_bytes)

    counts = manifest["counts"]
    if counts["failed"] and counts["written"]:
        manifest["status"] = "partial"
    elif counts["written"]:
        manifest["status"] = "ok"
    elif counts["failed"]:
        manifest["status"] = "partial"
    ctx.log(f"    🖼️  {counts['written']} figure(s), {counts['failed']} not obtained "
            f"({manifest['status']})")
    return finish()


def _fetch_all(declared, mirror, urls, directory, manifest, ctx, http,
               max_file_bytes) -> None:
    """One file at a time, through the shared limiter. Never in parallel."""
    for index, figure in enumerate(declared, start=1):
        record = dict(figure)
        record.pop("url", None)
        basename = os.path.basename(str(figure["href"]).replace("\\", "/"))
        listed = next((mirror[c] for c in _basename_candidates(basename)
                       if c in mirror), None)

        if listed is not None:
            # The mirror's copy: size from the bucket listing, md5 from the
            # metadata document, both known before a byte moves.
            url, provenance = listed.url, "original"
            record["declared_bytes"] = listed.size_bytes
            record["declared_checksum"] = listed.checksum
        else:
            url, provenance = urls.get(figure["href"]) or figure.get("url"), "render"

        record.update({"url": redact(url), "provenance": provenance,
                       "file": None, "sha256": None, "bytes": 0,
                       "content_type": "", "status": None})
        if not url:
            record["status"] = "figure_not_on_page"
            manifest["counts"]["failed"] += 1
            manifest["figures"].append(record)
            ctx.log(f"    ✗ {figure.get('figure_label') or basename}: not listed on the page")
            continue

        os.makedirs(directory, exist_ok=True)
        filename = _filename_for(figure["href"], url, index)
        destination = os.path.join(directory, filename)
        outcome = http.download(url, destination, max_bytes=max_file_bytes,
                                polite=False)
        if not outcome.ok:
            record["status"] = _failure_status(outcome)
            record["detail"] = outcome.detail
            manifest["counts"]["failed"] += 1
            manifest["figures"].append(record)
            ctx.log(f"    ✗ {figure.get('figure_label') or basename}: {record['status']}")
            continue

        record.update(file=filename, sha256=outcome.sha256,
                      bytes=outcome.bytes_written,
                      content_type=outcome.content_type, status="ok",
                      retrieved_at=_iso(time.time()))
        manifest["counts"]["written"] += 1
        manifest["counts"]["bytes_written"] += outcome.bytes_written
        manifest["figures"].append(record)


def _filename_for(href, url, index: int) -> str:
    """The name a figure is written under, taken from the URL that served it.

    Not from the JATS href: `<graphic xlink:href="pone.0000308.g001"/>` names
    no extension while the file served is a JPEG, so naming from the href alone
    writes an image that nothing will open. Both routes carry the real filename
    in the URL path -- the mirror's S3 key and the CDN's blob path -- and the
    href is the fallback for a URL that somehow does not.

    Its own name inside its own directory, rather than the flat numbered scheme
    the supplementary pass uses: `pone.0000308.g001.jpg` beside its manifest is
    unambiguous, and it is the name the article's own markup refers to.
    """
    from .supplementary import _safe_member_name

    base = os.path.basename(str(url or "").split("?")[0].replace("\\", "/"))
    if not os.path.splitext(base)[1]:
        base = os.path.basename(str(href or "").replace("\\", "/"))
    return _safe_member_name(base, os.path.splitext(base)[1], index)


def _failure_status(outcome) -> str:
    """`blocked:403` and `download_failed:404` are different facts.

    A publisher or CDN that refuses this client while serving a person has not
    told us the figure does not exist -- it has told us to send a person. That
    belongs in the manifest as its own status, not folded into a download
    failure that reads as absence.
    """
    if outcome.status in _BLOCKED_STATUSES:
        return f"blocked:{outcome.status}"
    return f"download_failed:{outcome.status}"


def _reusable(existing: dict) -> Optional[FiguresSummary]:
    """Whether an existing manifest answers this run, or has to be re-asked.

    A manifest that GOT something is final, and so is one whose refusal cannot
    change without a new input (`no_pmcid`). A manifest that fetched nothing is
    re-attempted, because the last answer was PMC's rather than ours: measured
    on the reference implementation's first live run, PMC served a 200 page
    with no figure links for two articles and listed them minutes later.
    """
    counts = existing.get("counts") or {}
    refusal = (existing.get("refusal") or {}).get("kind")
    if not (counts.get("written") or refusal == "no_pmcid"):
        return None
    return FiguresSummary(
        status="skipped",
        written=int(counts.get("written") or 0),
        failed=int(counts.get("failed") or 0),
        bytes_written=int(counts.get("bytes_written") or 0),
        refusal=refusal or "",
        detail="manifest already on disk",
    )


def _jats_bytes(ids, ctx) -> Optional[bytes]:
    """The article's JATS, and the supplement/figure sets it implies.

    enumerate_jats_manifest is called for its side effect on ctx.scratch as
    well as for the fetch: it is the article's own statement about which of its
    files are figures, and running it here means the two passes agree about
    that rather than each deciding for itself.
    """
    from .supplement_pmc import _jats_for, enumerate_jats_manifest

    try:
        enumerate_jats_manifest(ids, ctx)
        return _jats_for(ids, ctx)
    except Exception as e:                      # noqa: BLE001 - a route, not the record
        ctx.log(f"    figures: JATS unavailable ({type(e).__name__}: {str(e)[:100]})")
        return None


def _page_bytes(stem, ids, ctx, manifest, fetch_pmc_article_page) -> Optional[bytes]:
    """The article page: the copy on disk first, then the network.

    A record whose text came from its PMC page already has every blob link
    sitting beside the PDF, so the common case costs no request at all. The
    network path reuses the pmc_html source, which means one implementation of
    the "200 with no article body" retry rather than two.
    """
    on_disk = stem + ".fulltext.html"
    try:
        with open(on_disk, "rb") as f:
            content = f.read()
    except OSError:
        content = b""
    if content and _BLOB_RE.search(content):
        manifest["page_source"] = os.path.basename(on_disk)
        manifest["page_url"] = None
        return content

    try:
        artifact = fetch_pmc_article_page(ids, ctx)
    except Exception as e:                      # noqa: BLE001 - a route, not the record
        ctx.log(f"    figures: article page unavailable "
                f"({type(e).__name__}: {str(e)[:100]})")
        return None
    if artifact is None:
        return None
    manifest["page_source"] = "pmc_article_page"
    manifest["page_url"] = artifact.url
    manifest["page_attempts"] = artifact.extra.get("page_attempts")
    return artifact.content


def _mirror_objects(ids, ctx) -> Dict[str, object]:
    """basename -> the Open Data mirror's listing for it, when it has one.

    Keyed on the filename the article's own JATS names, not on the role
    classifier: a `<fig><graphic xlink:href>` is the article stating which file
    that figure is, which is stronger evidence than any guess from a filename.
    """
    from .supplement_pmc import enumerate_pmc_s3

    try:
        listed = enumerate_pmc_s3(ids, ctx) or []
    except Exception as e:                      # noqa: BLE001 - a route, not the record
        ctx.log(f"    figures: Open Data mirror unavailable "
                f"({type(e).__name__}: {str(e)[:100]})")
        return {}
    objects: Dict[str, object] = {}
    for entry in listed:
        base = os.path.basename(str(entry.name or "")).lower()
        if not base:
            continue
        objects.setdefault(base, entry)
        objects.setdefault(os.path.splitext(base)[0], entry)
    return objects


# -- shared helpers ----------------------------------------------------------


def _tag_is(element, name: str) -> bool:
    tag = element.tag
    if not isinstance(tag, str):
        return False
    return tag.rsplit("}", 1)[-1] == name


def _href_of(element) -> Optional[str]:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == "href" and value:
            return str(value).strip()
    return None
