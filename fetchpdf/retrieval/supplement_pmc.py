"""PMC supplementary routes: what the article says it has, and where to get it.

Three things live here and they divide along a line worth being explicit about.

E1, the JATS manifest, is a **classifier**, not a fetcher. The markup names every
supplementary file:

    <supplementary-material content-type="local-data" id="pone.0000308.s001">
      <media xlink:href="pone.0000308.s001.doc"/>

but xlink:href is a bare filename with no resolvable base. The obvious base URL
does not work: pmc.ncbi.nlm.nih.gov/articles/PMC1817752/bin/{name} returns HTTP
200, text/html, 21 KB -- a Google reCAPTCHA challenge page (verified 2026-07-29);
www.ncbi.nlm.nih.gov 301s to the same, and europepmc.org/articles/PMC../bin/..
redirects to a render endpoint that hangs. So E1 contributes no bytes. What it
contributes is the distinction between the article's supplements and its figures,
which is the one thing the filename heuristics cannot know for certain, and it
leaves that in ctx.scratch for the routes that do carry bytes.

E3, PMC's AWS Open Data mirror, is the fetcher. It is also the replacement for a
route that is about to disappear: oa.fcgi still returns
ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/../PMC1817752.tar.gz, that path now
404s over both https and ftp, and the readme dated 2026-04-10 says all legacy FTP
files are removed in August 2026. An endpoint that returns 200 and a dead link is
exactly the failure this package exists to catch, so it is not built on. The S3
mirror is better than the tarball ever was anyway: it declares every object's
size and md5 before transfer, which is what lets the size cap refuse an oversized
file for nothing.
"""

import os
import re
from typing import Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree

from .supplement_index import (
    ROLE_SUPPLEMENT,
    SupplementFile,
    classify_role,
    jats_sets,
)

EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

S3_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
S3_VERSIONS = S3_BUCKET + "/?list-type=2&prefix=PMC{number}.&delimiter=/"
S3_LISTING = S3_BUCKET + "/?list-type=2&prefix=PMC{number}.{version}/"
S3_METADATA = S3_BUCKET + "/metadata/PMC{number}.{version}.json"

_XLINK = "{http://www.w3.org/1999/xlink}href"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


# -- E1: the JATS manifest --------------------------------------------------


def enumerate_jats_manifest(ids, ctx) -> List[SupplementFile]:
    """Seed ctx.scratch with the article's own supplement/figure sets.

    Returns no files by design -- see the module docstring. Runs first so every
    later provider can classify against ground truth instead of a filename guess.
    """
    if not ids.pmcid:
        return []

    content = _jats_for(ids, ctx)
    if not content:
        return []
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []

    # A zero count is only trustworthy from a document that actually has a body.
    # An abstract stub or an EPMC error document parses cleanly and has no
    # <supplementary-material> either, and the two are otherwise identical.
    if root.find(".//{*}body") is None and root.find(".//body") is None:
        ctx.log("    JATS manifest: no <body>, so a zero supplement count proves nothing")
        return []

    supplements, labels, raw = _supplementary_hrefs(root)
    figures = _figure_hrefs(root)
    ctx.scratch["jats_supplements"] = supplements
    ctx.scratch["jats_figures"] = figures - supplements
    ctx.scratch["jats_labels"] = labels
    # basename -> raw href, for two consumers that need more than the set: the
    # completeness gate (declared vs obtained) and the jats_declared recovery
    # provider (an absolute href is directly fetchable; a bare filename is not).
    ctx.scratch["jats_declared"] = raw
    ctx.log(f"    JATS manifest: {len(supplements)} supplementary, {len(figures)} figure href(s)")
    return []


def enumerate_jats_declared(ids, ctx) -> List[SupplementFile]:
    """Declared hrefs that are themselves URLs. Recovery of last resort.

    Runs last so it only matters when every real provider came up short. Most
    declared hrefs are bare filenames with no resolvable base (see the module
    docstring on the /bin/ captcha), but some publishers declare an absolute
    URL -- LWW's links.lww.com permalinks, verified live to serve the file
    directly -- and those are fetchable exactly as written. Anything already
    obtained dedupes away by sha256 downstream, so listing here is free.
    """
    declared = ctx.scratch.get("jats_declared") or {}
    files: List[SupplementFile] = []
    for basename, href in sorted(declared.items()):
        if not re.match(r"^https?://", str(href), re.IGNORECASE):
            continue
        files.append(SupplementFile(
            name=basename,
            url=str(href),
            provider="jats_declared",
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            listing_index=len(files),
        ))
    return files


def _jats_for(ids, ctx) -> Optional[bytes]:
    """The article XML, preferring the copy a T1 pass already wrote."""
    from .supplementary import stem_for

    on_disk = stem_for(ctx.save_path) + ".xml"
    try:
        with open(on_disk, "rb") as f:
            return f.read()
    except OSError:
        pass

    response = ctx.http.get(EPMC_FULLTEXT.format(pmcid=ids.pmcid), timeout=60)
    if response.ok and response.content:
        return response.content
    return None


def declared_from_xml(content: bytes) -> Optional[Dict[str, str]]:
    """basename -> raw href of every declared supplement, or None if unverifiable.

    None, not {}, for an unparseable document or one with no <body>: the same
    rule enumerate_jats_manifest applies, because a zero count from an abstract
    stub proves nothing and must not read as "verified complete".
    """
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    if root.find(".//{*}body") is None and root.find(".//body") is None:
        return None
    _, _, raw = _supplementary_hrefs(root)
    return raw


def _supplementary_hrefs(root) -> Tuple[Set[str], Dict[str, str], Dict[str, str]]:
    """Every href under a <supplementary-material>: basenames, labels, raw hrefs."""
    hrefs: Set[str] = set()
    labels: Dict[str, str] = {}
    raw: Dict[str, str] = {}
    for block in root.iter():
        if not _tag_is(block, "supplementary-material"):
            continue
        label = _label_of(block)
        for element in block.iter():
            # <ext-link> included: LWW declares its supplement as a prose link
            # (<supplementary-material><p>... <ext-link xlink:href=
            # "http://links.lww.com/PR9/A213">) with no <media> at all, and a
            # declaration the parser cannot see is a completeness gate that
            # reads "verified complete" over a missing file.
            if not (_tag_is(element, "media") or _tag_is(element, "graphic")
                    or _tag_is(element, "ext-link")):
                continue
            href = element.get(_XLINK) or element.get("href")
            if not href:
                continue
            key = _basename(href)
            if not key:
                continue
            hrefs.add(key)
            raw.setdefault(key, str(href).strip())
            if label:
                labels[key] = label
    return hrefs, labels, raw


def _figure_hrefs(root) -> Set[str]:
    """Every href under a <fig> or <table-wrap>: images of the paper, not data."""
    hrefs: Set[str] = set()
    for block in root.iter():
        if not (_tag_is(block, "fig") or _tag_is(block, "table-wrap")):
            continue
        for element in block.iter():
            if not (_tag_is(element, "graphic") or _tag_is(element, "media")):
                continue
            href = element.get(_XLINK) or element.get("href")
            if href:
                hrefs.add(_basename(href))
    return hrefs


def _label_of(block) -> str:
    for child in block:
        if _tag_is(child, "label") and (child.text or "").strip():
            return (child.text or "").strip()
    for child in block.iter():
        if _tag_is(child, "title") and (child.text or "").strip():
            return (child.text or "").strip()
    return ""


def _tag_is(element, name: str) -> bool:
    tag = element.tag
    if not isinstance(tag, str):
        return False
    return tag.rsplit("}", 1)[-1] == name


def _basename(href: str) -> str:
    return str(href).split("?")[0].replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


# -- E3: PMC AWS Open Data --------------------------------------------------


def enumerate_pmc_s3(ids, ctx) -> List[SupplementFile]:
    """Media objects from the PMC Open Access S3 mirror.

    Sizes come from the bucket listing and md5s from the metadata document, both
    before a single file byte moves, which is what makes the cap free here.
    """
    number = _pmcid_number(ids.pmcid)
    if not number:
        return []

    version = _latest_version(ctx, number)
    if version is None:
        return []

    metadata = ctx.http.get(
        S3_METADATA.format(number=number, version=version), timeout=30, polite=False
    )
    if not metadata.ok:
        return []
    try:
        payload = metadata.json() or {}
    except ValueError:
        return []

    media = payload.get("media_urls") or []
    if not media:
        # Not an error: author manuscripts without a CC licence have their media
        # withheld while the article record itself exists.
        ctx.log("    PMC S3: record exists but lists no media")
        return []

    # The article's own text renditions are not supplements.
    own = {
        _basename(payload.get(key) or "")
        for key in ("pdf_url", "xml_url", "text_url")
        if payload.get(key)
    }
    sizes = _listing_sizes(ctx, number, version)
    supplements, figures = jats_sets(ctx)

    files = []
    for index, s3_url in enumerate(media):
        if not isinstance(s3_url, str):
            continue
        key, checksum = _key_and_md5(s3_url)
        if not key:
            continue
        name = _basename(key)
        if name in own:
            continue
        files.append(SupplementFile(
            name=os.path.basename(key),
            url=f"{S3_BUCKET}/{key}",
            provider="pmc_s3",
            listing_index=index,
            size_bytes=sizes.get(key),
            checksum=f"md5:{checksum}" if checksum else None,
            role=classify_role(name, jats_supplements=supplements, jats_figures=figures),
            label=(ctx.scratch.get("jats_labels") or {}).get(name, ""),
            origin_doi=ids.doi,
            extra={"pmcid": ids.pmcid, "s3_key": key},
        ))
    return files


def _latest_version(ctx, number: str) -> Optional[int]:
    """The highest PMC{n}.{v} prefix in the bucket.

    Versions matter: .1 is sometimes the author manuscript and .2 the published
    version, and they carry different media.
    """
    response = ctx.http.get(S3_VERSIONS.format(number=number), timeout=30, polite=False)
    if not response.ok:
        return None
    versions = [
        int(m.group(1))
        for m in re.finditer(rf"<Prefix>PMC{number}\.(\d+)/</Prefix>", response.text)
    ]
    return max(versions) if versions else None


def _listing_sizes(ctx, number: str, version: int) -> Dict[str, int]:
    """key -> byte size, from one anonymous ListBucket call."""
    response = ctx.http.get(
        S3_LISTING.format(number=number, version=version), timeout=30, polite=False
    )
    if not response.ok:
        return {}
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return {}
    sizes = {}
    for contents in root.iter():
        if not _tag_is(contents, "Contents"):
            continue
        key = size = None
        for child in contents:
            if _tag_is(child, "Key"):
                key = (child.text or "").strip()
            elif _tag_is(child, "Size"):
                try:
                    size = int((child.text or "").strip())
                except ValueError:
                    size = None
        if key and size is not None:
            sizes[key] = size
    return sizes


def _key_and_md5(s3_url: str) -> Tuple[Optional[str], Optional[str]]:
    """s3://pmc-oa-opendata/PMC1.1/x.xls?md5=abc -> ("PMC1.1/x.xls", "abc")."""
    without_scheme = re.sub(r"^s3://[^/]+/", "", s3_url.strip())
    if not without_scheme or without_scheme == s3_url.strip() and "://" in s3_url:
        return None, None
    key, _, query = without_scheme.partition("?")
    checksum = None
    for part in query.split("&"):
        if part.startswith("md5="):
            checksum = part[4:].strip() or None
    return (key or None), checksum


def _pmcid_number(pmcid: Optional[str]) -> Optional[str]:
    if not pmcid:
        return None
    m = re.search(r"(\d+)", str(pmcid))
    return m.group(1) if m else None
