"""Repository APIs behind a JavaScript bot-challenge, via a real browser tab.

Harvard Dataverse sits behind AWS WAF. When the challenge is armed, *every*
endpoint -- the dataset API, `/api/access`, `/api/info/version`, even the site
root -- answers **HTTP 202 with an empty body** to a plain HTTP client. Measured
2026-08-12 against 10.7910/DVN/Z8WAKW, same machine and network:

    curl, no UA                             202, 0 bytes
    curl, browser UA                        202, 2071 bytes  <- the challenge page
    demo.dataverse.org (control)            200               <- not us, not the network
    Playwright HEADED, land then ask API    200, 1 file       <- works

202-with-no-body is the part that misleads. It is not 5xx, not 403, not a
timeout; `response.ok` is false and the enumerator's `return []` reads as "this
deposit has no files". A trial's own individual-level replication data was
skipped that way while the paper was reviewed against its summary statistics
alone, and the manifest recorded `none_found` -- a claim about the authors
written out of somebody else's bot protection.

The challenge page is recognisable: AWS WAF ships `window.gokuProps` and
`awsWafCookieDomainList` and needs JavaScript to solve. A real browser solves it
in a few seconds and receives a cookie; the API then answers normally *in that
browser context*, which is why the request is issued through `page.request`
rather than copied back out to the HTTP client.

Two things are load-bearing, both learned in supplement_atypon.py:

  * **Headed, not headless.** On a headless server that means xvfb. The helpers
    are imported from supplement_atypon rather than copied -- one xvfb
    implementation, not two that drift.
  * **Land on the page first.** The challenge is issued for the site, not for
    the endpoint. Navigating straight to the API URL hands the challenge to a
    JSON client that cannot run it.

Expensive -- one browser per record -- so it is the last resort, reached only
after the plain HTTP enumerator has already failed against a host known to use
this protection.
"""

import json
import time
from typing import Optional

from . import blocked

#: Hosts observed to serve a JS bot-challenge in front of their API. Keyed by
#: host so a repository that later drops the challenge can simply be removed.
#: The landing template gives the browser a page to solve the challenge on
#: before the API is asked.
WAF_HOSTS = {
    "dataverse.harvard.edu":
        "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:{doi}",
}

#: Markers of an AWS WAF interstitial. Present in the 202 body; absent once the
#: challenge is solved, which is how the wait knows it is done. Imported rather
#: than restated: blocked.py holds every "is this a challenge" definition, so a
#: page one module learns to recognise is not invisible to the others.
_CHALLENGE_MARKERS = blocked.WAF_CHALLENGE_MARKERS

#: A challenge that has not cleared in this long is not going to.
_CHALLENGE_TIMEOUT_S = 25


def host_uses_waf(url: str) -> Optional[str]:
    """The landing-page template for `url`'s host, or None."""
    for host, landing in WAF_HOSTS.items():
        if host in (url or ""):
            return landing
    return None


def looks_like_challenge(body: bytes, status: int) -> bool:
    """Whether this response is a bot-challenge rather than an answer.

    An empty 202 counts: that is what the WAF returns to a client it will not
    even hand the challenge page to, and treating it as a valid empty result is
    the whole failure this module exists to prevent.
    """
    if status == 202:
        return True
    if not body:
        return False
    try:
        head = body[:4096].decode("utf-8", "ignore")
    except Exception:
        return False
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _page_content(page) -> str:
    """page.content() while the WAF is mid-redirect raises; retry briefly."""
    for _ in range(10):
        try:
            return page.content()
        except Exception:
            time.sleep(1)
    return ""


def fetch_json_through_browser(api_url: str, doi: str, ctx) -> Optional[dict]:
    """Solve the host's challenge in a real browser, then GET `api_url` in it.

    Returns the decoded JSON, or None if a browser cannot run here, the
    challenge does not clear, or the API still refuses. Never raises: the
    caller's contract is that a supplementary failure cannot fail the record.
    """
    landing_template = host_uses_waf(api_url)
    if not landing_template:
        return None

    from .supplement_atypon import (_start_xvfb, _stop_xvfb,
                                    headed_browser_available)

    if not headed_browser_available():
        ctx.log("    repository WAF: no display and no xvfb; cannot solve challenge")
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        ctx.log("    repository WAF: playwright not installed; cannot solve challenge")
        return None

    virtual_display = None
    try:
        virtual_display = _start_xvfb(ctx)
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=False)  # load-bearing
            try:
                page = browser.new_context(
                    viewport={"width": 1400, "height": 900}).new_page()
                page.goto(landing_template.format(doi=doi),
                          wait_until="domcontentloaded", timeout=60000)

                deadline = time.time() + _CHALLENGE_TIMEOUT_S
                while time.time() < deadline:
                    time.sleep(1)
                    content = _page_content(page)
                    if content and not any(m in content for m in _CHALLENGE_MARKERS):
                        break
                else:
                    ctx.log("    repository WAF: challenge did not clear")
                    return None

                response = page.request.get(api_url, timeout=60000)
                if response.status != 200:
                    ctx.log(f"    repository WAF: API still {response.status} after challenge")
                    return None
                return json.loads(response.text())
            finally:
                browser.close()
    except Exception as exc:                       # noqa: BLE001 - never fail the record
        ctx.log(f"    repository WAF: browser fallback failed ({type(exc).__name__})")
        return None
    finally:
        _stop_xvfb(virtual_display)
