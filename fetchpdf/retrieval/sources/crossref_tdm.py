"""Crossref text-and-data-mining links, and the Elsevier endpoint most of them
point at.

Three things the obvious implementation gets wrong:

1. The content-type for XML is "text/xml", not "application/xml". Verified on
   10.1016/S0140-6736(20)30183-5, 10.1016/j.jclinepi.2021.02.003 and
   10.1016/j.ijnurstu.2019.103466 -- all "text/xml".

2. intended-application matters. "similarity-checking" links feed iThenticate
   and are usually landing pages: 10.1371/journal.pone.0000308 exposes exactly
   one link, content-type "unspecified", pointing at dx.plos.org. Only
   "text-mining" links are candidates.

3. There is no Crossref token to obtain. Crossref retired the click-through
   token service in December 2020; entitlement is entirely publisher-side now.
   So 401/403 is the expected outcome on a subscription title, not an error
   condition, and it must never abort a run.

For Elsevier the Crossref link already carries the PII inline, which is why
Scopus is not needed to resolve one. The API key is appended here because the
link Crossref publishes does not include it.
"""

import re
from typing import Optional

from ..._env import ELSEVIER_TDM_API_KEY
from ..artifact import Artifact
from ..tiers import Tier

_XML_CONTENT_TYPES = {"text/xml", "application/xml"}
_TEXT_MINING = "text-mining"
_ELSEVIER_HOSTS = ("api.elsevier.com",)

ELSEVIER_ARTICLE = "https://api.elsevier.com/content/article/PII:{pii}"

#: Elsevier answers a non-entitled request with 200 and a coredata-only payload.
#: Cheap pre-filter; the T1 validator is what actually decides.
_ENTITLEMENT_SIGNATURES = (
    b"<service-error",
    b"AUTHENTICATION_ERROR",
    b"APIKEY_INVALID",
    b"RESOURCE_NOT_FOUND",
)


def fetch_elsevier_tdm(ids, ctx) -> Optional[Artifact]:
    """Elsevier full-text XML by PII."""
    if not ids.elsevier_pii:
        return None
    if not ELSEVIER_TDM_API_KEY:
        ctx.log("    Elsevier TDM: no ELSEVIER_TDM_API_KEY")
        return None

    url = ELSEVIER_ARTICLE.format(pii=ids.elsevier_pii)
    response = ctx.http.get(
        url,
        params={"httpAccept": "text/xml", "apiKey": ELSEVIER_TDM_API_KEY},
        timeout=60,
    )
    if response.status in (401, 403):
        ctx.log(f"    Elsevier TDM: not entitled (HTTP {response.status})")
        return None
    if not response.ok or not response.content:
        ctx.log(f"    Elsevier TDM: HTTP {response.status}")
        return None
    if any(sig in response.content[:4096] for sig in _ENTITLEMENT_SIGNATURES):
        ctx.log("    Elsevier TDM: 200 with an error payload (not entitled)")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T1_XML,
        source="elsevier_tdm",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.elsevier_pii,
        license=ids.license,
    )


def fetch_elsevier_pdf(ids, ctx) -> Optional[Artifact]:
    """Elsevier full-text PDF by PII -- the same endpoint, a different Accept.

    Entitlement at Elsevier is negotiated per representation, not per article:
    the XML view of a record can come back as coredata-only metadata (no
    <body>, or a 400-character corrigendum notice) while `Accept:
    application/pdf` on the *same* PII returns the complete typeset article.
    Verified across four records that the XML route rejected -- all four
    returned real PDFs of 237-446 KB.

    So this is not a retry of fetch_elsevier_tdm, it is a different question
    asked of the same URL, and it belongs at T5 because what comes back is a
    PDF. Registered after legacy_pdf_chain in ladder.json: the chain's own
    routes are free, and this spends Elsevier quota.
    """
    if not ids.elsevier_pii:
        return None
    if not ELSEVIER_TDM_API_KEY:
        return None

    url = ELSEVIER_ARTICLE.format(pii=ids.elsevier_pii)
    response = ctx.http.get(
        url,
        headers={"X-ELS-APIKey": ELSEVIER_TDM_API_KEY,
                 "Accept": "application/pdf"},
        timeout=90,
    )
    if response.status in (401, 403):
        ctx.log(f"    Elsevier PDF: not entitled (HTTP {response.status})")
        return None
    if not response.ok or not response.content:
        ctx.log(f"    Elsevier PDF: HTTP {response.status}")
        return None
    # A non-entitled PDF request answers 200 with an XML error payload, so the
    # magic bytes -- not the status -- are what decide. The engine's T5
    # validator checks this too; failing here keeps a junk artifact from
    # occupying the PDF goal in the first place.
    if not response.content.startswith(b"%PDF"):
        ctx.log("    Elsevier PDF: 200 but not a PDF (not entitled)")
        return None

    return Artifact(
        content=response.content,
        tier=Tier.T5_PDF,
        source="elsevier_pdf",
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.elsevier_pii,
        license=ids.license,
    )


def fetch_crossref_tdm(ids, ctx) -> Optional[Artifact]:
    """Any other publisher's TDM XML, as advertised in Crossref's link[]."""
    if not ids.doi:
        return None

    # Lazily resolve Crossref: a record that short-circuited on a PMCID never
    # got here, and never paid for this call.
    ctx.resolver.crossref(ids)
    candidates = [
        link for link in (ids.crossref_links or [])
        if (link.get("content-type") or "").lower() in _XML_CONTENT_TYPES
        and (link.get("intended-application") or "").lower() == _TEXT_MINING
    ]
    if not candidates:
        return None

    # Version of record before accepted manuscript, when the publisher says.
    candidates.sort(key=lambda link: 0 if (link.get("content-version") == "vor") else 1)

    for link in candidates:
        url = link.get("URL")
        if not url:
            continue
        if any(host in url for host in _ELSEVIER_HOSTS):
            # Elsevier has its own entry point that knows about the API key.
            m = re.search(r"PII:([A-Z0-9]+)", url, re.IGNORECASE)
            if m and not ids.elsevier_pii:
                ids.learn("crossref.link", url=url, elsevier_pii=m.group(1))
            continue

        response = ctx.http.get(url, timeout=45)
        if response.status in (401, 403):
            ctx.log(f"    Crossref TDM: publisher requires entitlement (HTTP {response.status})")
            continue
        if not response.ok or not response.content:
            ctx.log(f"    Crossref TDM: HTTP {response.status}")
            continue
        return Artifact(
            content=response.content,
            tier=Tier.T1_XML,
            source="crossref_tdm",
            url=response.url,
            http_status=response.status,
            served_content_type=response.content_type,
            identifier_used=ids.doi,
            license=ids.license,
            extra={"content_version": link.get("content-version")},
        )
    return None
