"""Publisher supplementary routes that work over plain HTTP.

Which publishers those are is not a matter of taste. Verified with a browser
User-Agent on 2026-07-29:

  reachable   Springer/Nature (media.springernature.com, static-content.springer.com,
              www.nature.com article pages), PLOS (journals.plos.org article/file),
              bioRxiv/medRxiv (.supplementary-material), OUP/Silverchair, supp.apa.org
  403/blocked PNAS, Science, Taylor & Francis, Sage (all Atypon), Wiley,
              ScienceDirect, MDPI -- Cloudflare interstitials
  worse       ACS returns 404 *with* a 57 KB HTML body, so its 404 is not an answer

Only the reachable ones live here. The blocked families need a browser and are
Phase 2, gated on the existing --add-playwright rather than a new switch.

The one exception to "Elsevier is blocked" is worth its own note: ScienceDirect
pages are walled, but the Elsevier object API is not, and it names supplementary
material outright. Verified on S2589004225028421: 25 choices, 24 of them
IMAGE-{DOWNSAMPLED,THUMBNAIL,HIGH-RES}, and one @type "APPLICATION" whose $ is
the mmc1.pdf download URL. @type == "APPLICATION" *is* the marker.
"""

import re
from typing import List, Optional

from .._env import ELSEVIER_TDM_API_KEY
from .supplement_index import ROLE_SUPPLEMENT, SupplementFile, classify_role, jats_sets

ELSEVIER_OBJECT = "https://api.elsevier.com/content/object/pii/{pii}"
ELSEVIER_OBJECT_BY_DOI = "https://api.elsevier.com/content/object/doi/{doi}"
PLOS_FILE = "https://journals.plos.org/{slug}/article/file"
BIORXIV_DETAILS = "https://api.biorxiv.org/details/{server}/{doi}"
BIORXIV_SUPPL = "https://www.{server}.org/content/{doi}v{version}.supplementary-material"
APA_SUPPL = "https://supp.apa.org/psycarticles/supplemental/{code}/{code}_supp.html"

#: Nature/Springer electronic supplementary material, on either CDN host.
_SPRINGER_ESM_RE = re.compile(
    r"https://(?:media\.springernature\.com/original/springer-static"
    r"|static-content\.springer\.com)"
    r"/esm/[^\"'\s>]+?/MediaObjects/[^\"'\s>]+?_MOESM\d+_ESM\.\w+",
    re.IGNORECASE,
)
_SPRINGER_PREFIXES = ("10.1038", "10.1007", "10.1186", "10.1140", "10.1057", "10.1245")

#: PLOS journal slugs, keyed on the DOI's journal stem.
_PLOS_SLUGS = {
    "pone": "plosone", "pbio": "plosbiology", "pmed": "plosmedicine",
    "pcbi": "ploscompbiol", "pgen": "plosgenetics", "ppat": "plospathogens",
    "pntd": "plosntds", "pstr": "plossustain", "pclm": "climate",
    "pwat": "water", "pdig": "digitalhealth", "pgph": "globalpublichealth",
}

_BIORXIV_MEDIA_RE = re.compile(
    r"/content/(?:bio|med)rxiv/early/\d{4}/\d{2}/\d{2}/[^\"'\s>]+?/DC\d+/embed/"
    r"media-\d+\.\w+(?:\?download=true)?",
    re.IGNORECASE,
)

_APA_HREF_RE = re.compile(r"""href=["']([^"'>]+)["']""", re.IGNORECASE)

_MAX_PER_PUBLISHER = 40


# -- E10: Elsevier object API -----------------------------------------------


def enumerate_elsevier_objects(ids, ctx) -> List[SupplementFile]:
    """Attachments named by the Elsevier object API.

    The only route into Elsevier supplementary material that does not need a
    browser. Non-entitled requests come back 200 with an error payload rather
    than a 4xx, so the payload, not the status, decides.
    """
    # The DOI route is not a fallback for missing PIIs, it is the more reliable
    # of the two: a PII resolved from Crossref can be stale or simply wrong
    # (S0749597813001040 for 10.1016/j.obhdp.2013.09.001 is a 404), while the
    # DOI form answers 300 with the full choice list including the mmc1
    # supplement. Try the PII first only because it costs the same and keeps
    # the existing behaviour where it already works.
    for url in _elsevier_object_urls(ids):
        files = _elsevier_objects_at(url, ids, ctx)
        if files:
            return files
    return []


def _elsevier_object_urls(ids) -> List[str]:
    urls = []
    if ids.elsevier_pii:
        urls.append(ELSEVIER_OBJECT.format(pii=ids.elsevier_pii))
    if ids.doi:
        from urllib.parse import quote
        urls.append(ELSEVIER_OBJECT_BY_DOI.format(doi=quote(ids.doi, safe="")))
    return urls


def _elsevier_objects_at(url: str, ids, ctx) -> List[SupplementFile]:
    """One object-API lookup, by whichever identifier the URL carries."""
    response = ctx.http.get(
        url,
        headers={"X-ELS-APIKey": ELSEVIER_TDM_API_KEY, "Accept": "application/json"},
        timeout=30,
        polite=False,
    )
    # HTTP 300 Multiple Choices is this endpoint's SUCCESS response -- it is
    # literally a list of the representations on offer, which is what we came
    # for. Response.ok is 200 <= status < 300, so `if not response.ok` rejected
    # every Elsevier article ever passed to this provider. Six records in one
    # 46-paper corpus had supplements that were silently dropped this way.
    if not response.ok and response.status != 300:
        return []
    try:
        payload = response.json() or {}
    except ValueError:
        return []
    if "service-error" in payload or "error-response" in payload:
        ctx.log("    Elsevier objects: 200 with an error payload (not entitled)")
        return []

    entries = _elsevier_choices(payload)
    files = []
    for index, choice in enumerate(entries):
        if str(choice.get("@type") or "").upper() != "APPLICATION":
            continue
        url = choice.get("$") or choice.get("@href")
        if not url:
            continue
        ref = str(choice.get("@ref") or f"mmc{index + 1}")
        files.append(SupplementFile(
            name=_elsevier_name(ref, url),
            url=url,
            provider="elsevier_objects",
            listing_index=index,
            mimetype=choice.get("@mimetype"),
            size_bytes=_as_int(choice.get("@filesize")),
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            polite=False,
            extra={"pii": ids.elsevier_pii, "ref": ref},
        ))
        if len(files) >= _MAX_PER_PUBLISHER:
            break
    return files


def _elsevier_choices(payload) -> List[dict]:
    choices = ((payload.get("object-response") or payload).get("choices") or {})
    if isinstance(choices, dict):
        choices = choices.get("choice") or []
    if isinstance(choices, dict):
        choices = [choices]
    return [c for c in choices if isinstance(c, dict)]


def _elsevier_name(ref: str, url: str) -> str:
    """mmc1 plus whatever extension the URL admits to."""
    m = re.search(r"-(mmc\d+\.\w+)", url) or re.search(r"/([^/?]+\.\w{1,5})(?:\?|$)", url)
    if m:
        return m.group(1)
    m = re.search(r"httpAccept=\w+/([\w.+-]+)", url)
    return f"{ref}.{m.group(1)}" if m else ref


# -- E11: Springer / Nature ESM ---------------------------------------------


def enumerate_springer_esm(ids, ctx) -> List[SupplementFile]:
    """MOESM objects scraped off the article page.

    The CDN URL cannot be derived -- the MOESM stem encodes an internal article
    id -- so the page has to be read. Anchors carry data-test="supp-info-link";
    the regex on the CDN hosts is the fallback for pages that do not.
    """
    if not _has_prefix(ids.doi, _SPRINGER_PREFIXES):
        return []
    response = ctx.http.get(f"https://doi.org/{ids.doi}", timeout=45, polite=False)
    if not response.ok or not response.content:
        return []

    supplements, figures = jats_sets(ctx)
    files = []
    for index, url in enumerate(_unique(_SPRINGER_ESM_RE.findall(response.text))):
        name = url.rsplit("/", 1)[-1]
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="springer_esm",
            listing_index=index,
            role=classify_role(name, jats_supplements=supplements, jats_figures=figures),
            origin_doi=ids.doi,
            polite=False,
        ))
        if len(files) >= _MAX_PER_PUBLISHER:
            break
    return files


# -- E12: PLOS --------------------------------------------------------------


def enumerate_plos(ids, ctx) -> List[SupplementFile]:
    """PLOS supplementary files, enumerated rather than probed.

    The sNNN ids come from the JATS manifest or from Crossref components. Probing
    s001, s002, ... blindly would work and would also hammer the host for every
    article that has none, so it is not done.
    """
    if not ids.doi or not ids.doi.startswith("10.1371"):
        return []
    slug = _PLOS_SLUGS.get(_plos_stem(ids.doi))
    if not slug:
        return []

    supplements, _ = jats_sets(ctx)
    ids_seen = sorted({
        m.group(1)
        for name in supplements
        for m in [re.search(r"\.(s\d{3})\b", name)]
        if m
    })
    if not ids_seen:
        return []

    files = []
    for index, suffix in enumerate(ids_seen):
        files.append(SupplementFile(
            name=f"{ids.doi.rsplit('/', 1)[-1]}.{suffix}",
            url=PLOS_FILE.format(slug=slug) + f"?id={ids.doi}.{suffix}&type=supplementary",
            provider="plos",
            listing_index=index,
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            polite=False,
        ))
    return files


def _plos_stem(doi: str) -> str:
    m = re.search(r"journal\.([a-z]+)\.", doi, re.IGNORECASE)
    return m.group(1).lower() if m else ""


# -- E13: bioRxiv / medRxiv -------------------------------------------------


def enumerate_preprint_supplements(ids, ctx) -> List[SupplementFile]:
    """Preprint supplementary media, from the versioned supplement page.

    A nonexistent DOI or version 302s to /node/ and answers 200 with a generic
    page, so the final URL is checked for the DOI before anything is parsed.
    """
    if not ids.doi or not ids.doi.startswith("10.1101"):
        return []
    server = (ids.preprint_server or "").lower()
    servers = [server] if server in ("biorxiv", "medrxiv") else ["biorxiv", "medrxiv"]

    for candidate in servers:
        version = _preprint_version(ids, ctx, candidate)
        if version is None:
            continue
        url = BIORXIV_SUPPL.format(server=candidate, doi=ids.doi, version=version)
        response = ctx.http.get(url, timeout=45, polite=False)
        if not response.ok or not response.content:
            continue
        if ids.doi.lower() not in (response.url or "").lower():
            ctx.log(f"    {candidate}: redirected away from the DOI, not parsing")
            continue
        files = []
        for index, path in enumerate(_unique(_BIORXIV_MEDIA_RE.findall(response.text))):
            files.append(SupplementFile(
                name=path.split("/")[-1].split("?")[0],
                url=f"https://www.{candidate}.org{path}",
                provider="preprint_supplements",
                listing_index=index,
                role=ROLE_SUPPLEMENT,
                origin_doi=ids.doi,
                polite=False,
            ))
            if len(files) >= _MAX_PER_PUBLISHER:
                break
        if files:
            return files
    return []


def _preprint_version(ids, ctx, server) -> Optional[int]:
    response = ctx.http.get(
        BIORXIV_DETAILS.format(server=server, doi=ids.doi), timeout=30, polite=False
    )
    if not response.ok:
        return None
    try:
        collection = (response.json() or {}).get("collection") or []
    except ValueError:
        return None
    if not collection:
        # A 200 with an empty collection is how the wrong server answers.
        return None
    versions = [_as_int(entry.get("version")) for entry in collection
                if isinstance(entry, dict)]
    versions = [v for v in versions if v]
    return max(versions) if versions else 1


# -- E14: APA supplemental --------------------------------------------------


def enumerate_apa_supplemental(ids, ctx) -> List[SupplementFile]:
    """The APA supplemental index page.

    try_apa_supplemental_fallback in the legacy chain scrapes the same page, but
    it is built for a different job -- it returns a bool, writes one PDF to
    save_path, stops at the first success, and runs .doc files through
    LibreOffice. Here every file is wanted and wanted in its deposited format, so
    the scraping is lifted rather than the function reused; the legacy one keeps
    working unchanged for its own purpose.
    """
    if not ids.doi or not ids.doi.startswith("10.1037"):
        return []
    code = ids.doi.rsplit("/", 1)[-1].strip().lower()
    if not code:
        return []

    response = ctx.http.get(APA_SUPPL.format(code=code), timeout=30, polite=False)
    if not response.ok or not response.content:
        return []

    base = f"https://supp.apa.org/psycarticles/supplemental/{code}/"
    files = []
    for index, href in enumerate(_unique(_APA_HREF_RE.findall(response.text))):
        if href.startswith("#") or href.lower().startswith(("mailto:", "javascript:")):
            continue
        name = href.split("?")[0].rsplit("/", 1)[-1]
        if not name or "." not in name or name.lower().endswith((".html", ".htm", ".css", ".js")):
            continue
        url = href if href.startswith("http") else base + href.lstrip("/")
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="apa_supplemental",
            listing_index=index,
            role=ROLE_SUPPLEMENT,
            origin_doi=ids.doi,
            polite=False,
        ))
        if len(files) >= _MAX_PER_PUBLISHER:
            break
    return files


# -- shared -----------------------------------------------------------------


def _has_prefix(doi: Optional[str], prefixes) -> bool:
    return bool(doi) and any(str(doi).startswith(p) for p in prefixes)


def _unique(values) -> List[str]:
    """Order-preserving dedupe: a page lists the same href more than once."""
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _as_int(value) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
