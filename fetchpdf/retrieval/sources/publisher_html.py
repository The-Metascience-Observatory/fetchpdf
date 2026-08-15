"""T2: publisher and repository HTML full text.

HTML sits second, not last. At most OA publishers the article HTML is generated
from the same JATS the XML endpoint serves, so it carries real <table> with
<th>, colspan and rowspan -- fidelity close to T1. Ranking it below PDF, as the
original ladder did, threw away the second-best structured source in favour of
the one that has to be reconstructed.

Two routes here:

  * Unpaywall oa_locations[]. The `url` field is not always a PDF -- it is
    frequently the publisher's HTML full text, and sometimes XML. Inspecting
    what comes back rather than assuming from the field name is what makes this
    a T2 source at all. publishedVersion is preferred over acceptedVersion, and
    publisher hosts over repositories, because repository copies are more often
    author manuscripts whose tables have been reflowed.

  * Plain DOI resolution to whatever the publisher serves.

A headless-browser retry is available but deliberately narrow: it runs only when
the static fetch returned a page that has <table> elements with no populated
cells, which is the signature of tables loaded by JS or hosted in a separate
table viewer. It reuses the existing --add-playwright flag rather than adding a
dependency or a new switch.
"""

from typing import List, Optional

from ..artifact import Artifact
from ..tiers import Tier
from ..validate import has_empty_table_containers

UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
DOI_RESOLVER = "https://doi.org/{doi}"

_VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
_HOST_RANK = {"publisher": 0, "repository": 1}


def fetch_unpaywall_fulltext(ids, ctx) -> Optional[Artifact]:
    """Walk oa_locations looking for HTML or XML full text, best version first."""
    if not ids.doi:
        return None

    # Shared with the resolver, which reads the same oa_locations[] to harvest an
    # arXiv id. Whichever runs first pays for the request; the other gets it free.
    data = ctx.resolver.unpaywall(ids)
    if not data:
        return None

    locations = data.get("oa_locations") or []
    best = data.get("best_oa_location")
    if best and best not in locations:
        locations = [best] + list(locations)
    if not locations:
        return None

    for location in sorted(locations, key=_location_rank):
        # url_for_pdf is explicitly a PDF; this source is looking for the
        # landing/full-text URL, which is where the HTML lives.
        url = location.get("url_for_landing_page") or location.get("url")
        if not url or url == location.get("url_for_pdf"):
            continue
        artifact = _fetch_and_wrap(
            ctx, url, ids, source="unpaywall_html",
            license_=location.get("license"),
            extra={
                "version": location.get("version"),
                "host_type": location.get("host_type"),
            },
        )
        if artifact is not None:
            return artifact
    return None


def fetch_publisher_html(ids, ctx) -> Optional[Artifact]:
    """Whatever the DOI resolves to, if it turns out to be full text with tables."""
    if not ids.doi:
        return None
    return _fetch_and_wrap(
        ctx, DOI_RESOLVER.format(doi=ids.doi), ids,
        source="publisher_html", license_=ids.license,
    )


def _location_rank(location: dict):
    return (
        _VERSION_RANK.get(location.get("version"), 3),
        _HOST_RANK.get(location.get("host_type"), 2),
    )


def _fetch_and_wrap(ctx, url, ids, source, license_=None, extra=None) -> Optional[Artifact]:
    response = ctx.http.get(url, timeout=45, polite=False)
    if not response.ok or not response.content:
        return None

    content = response.content
    notes: List[str] = []

    if has_empty_table_containers(content):
        rendered = _render_with_playwright(ctx, url)
        if rendered:
            content = rendered
            notes.append("re-fetched with headless browser: tables were JS-loaded")
        else:
            notes.append("tables present but unpopulated; no browser retry available")

    return Artifact(
        content=content,
        tier=Tier.T2_HTML,
        source=source,
        url=response.url,
        http_status=response.status,
        served_content_type=response.content_type,
        identifier_used=ids.doi,
        license=license_ or ids.license,
        extra=dict(extra or {}, notes=notes),
    )


def _render_with_playwright(ctx, url: str) -> Optional[bytes]:
    """Render the page in the browser this package already ships with.

    Gated on --add-playwright, and only reached when a static fetch produced
    table containers with no cells. Anything broader would put a browser launch
    in the hot path of every record.
    """
    if not ctx.use_playwright:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    from ..._http import USER_AGENT

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            try:
                page = browser.new_context(user_agent=USER_AGENT).new_page()
                page.set_default_timeout(30000)
                page.goto(url, wait_until="networkidle", timeout=45000)
                html = page.content()
            finally:
                browser.close()
        return html.encode("utf-8") if html else None
    except Exception as e:
        ctx.log(f"    browser render failed: {str(e)[:100]}")
        return None
