"""Institutional (subscribed) retrieval with the user's own browser cookies.

Opt-in only (``--cookies FILE``), and run only after the whole open-access
chain has returned nothing. Builds the publisher's canonical PDF URL from the
DOI prefix and fetches it with cookies exported from the user's own signed-in
browser. The PDF then meets the same identity check as every other source: a
subscription is a reason to be allowed the file, not a reason to trust it.

The cookie file is a credential. Three rules keep it from going anywhere else:

* **Only cookies scoped to the publisher's own domain are sent.** The jar is
  built per request from cookies whose domain matches the target host, so a
  redirect to another host cannot carry them. A cookie with no domain at all
  is dropped: requests' cookie jar would send it to every host.
* **Secure cookies stay on https**, and expired ones are not sent.
* **Nothing is logged** but URLs and HTTP statuses.

Ported from PR #3 by Lukas Wallrich (cookie route only; the EBSCOhost route
was left out of the public package).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from requests.cookies import create_cookie

from .._http import headers_for

#: Largest PDF this route will write. Read in chunks and abandoned past this,
#: so a misbehaving host cannot fill memory or disk.
MAX_PDF_BYTES = 300 * 1024 * 1024

SOURCE = "institutional_cookies"


# --------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------
def load_cookies(path) -> List[dict]:
    """Read a cookie export into dicts with name, value, domain, path, secure, expires.

    Two formats, because the two easy ways to get cookies out of a browser
    produce different ones:

    * **Netscape ``cookies.txt``** -- what the browser extensions write. Some
      put an ``#HttpOnly_`` prefix on the domain: those lines look like
      comments and are not, so the prefix is stripped rather than the line
      skipped.
    * **JSON** -- a bare list (most exporters) or an object with a ``cookies``
      key (Playwright ``storage_state``).

    Cookies without a domain are dropped here, not later: in a requests jar an
    empty domain matches every host, which would send the user's session to
    wherever a redirect points.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if text.lstrip()[:1] in ("[", "{"):
        raw = json.loads(text)
        items = raw if isinstance(raw, list) else raw.get("cookies", [])
        cookies = [_from_json(c) for c in items
                   if isinstance(c, dict) and "name" in c and "value" in c]
    else:
        cookies = _parse_netscape(text)
    return [c for c in cookies if c["domain"]]


def _from_json(c: dict) -> dict:
    expires = c.get("expires", c.get("expirationDate"))
    try:
        expires = int(float(expires)) if expires not in (None, "", -1) else None
    except (TypeError, ValueError):
        expires = None
    return {
        "name": str(c["name"]),
        "value": str(c["value"]),
        "domain": str(c.get("domain") or "").strip(),
        "path": str(c.get("path") or "/"),
        "secure": bool(c.get("secure", False)),
        "expires": expires if expires and expires > 0 else None,
    }


def _parse_netscape(text: str) -> List[dict]:
    out: List[dict] = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _subdomains, cpath, secure, expiry, name, value = parts[:7]
        try:
            expires = int(expiry) or None
        except ValueError:
            expires = None
        out.append({
            "name": name,
            "value": value,
            "domain": domain.strip(),
            "path": cpath.strip() or "/",
            "secure": secure.strip().upper() == "TRUE",
            "expires": expires,
        })
    return out


def registrable_domain(host: str) -> str:
    """The last two labels of a host: ``onlinelibrary.wiley.com`` -> ``wiley.com``.

    Good enough for the publisher hosts in PUBLISHER_PDF_TEMPLATES, all of which
    sit directly under a generic TLD. Not a public-suffix implementation.
    """
    labels = (host or "").lower().strip(".").split(".")
    return ".".join(labels[-2:])


def _domain_matches(cookie_domain: str, site: str) -> bool:
    domain = cookie_domain.lower().lstrip(".")
    return domain == site or domain.endswith("." + site)


def cookie_jar(cookies: Sequence[dict], url: str,
               now: Optional[float] = None) -> requests.cookies.RequestsCookieJar:
    """A jar holding only the cookies that belong to *url*'s publisher.

    Each cookie keeps its own domain and path, so requests sends a cookie only
    where the browser would have. Scoping to the publisher's registrable domain
    as well is what makes a redirect safe: a hop to an unrelated host finds
    nothing in the jar to send. A real browser profile also holds thousands of
    cookies, and sending all of them earns ``400 Request Header Or Cookie Too
    Large``.
    """
    site = registrable_domain(urlsplit(url).hostname or "")
    now = time.time() if now is None else now
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        if not site or not _domain_matches(c.get("domain", ""), site):
            continue
        if c.get("expires") and c["expires"] < now:
            continue
        try:
            jar.set_cookie(create_cookie(
                c["name"], c["value"], domain=c["domain"],
                path=c.get("path") or "/", secure=bool(c.get("secure")),
                expires=c.get("expires"),
            ))
        except Exception:      # noqa: BLE001 - one malformed row is not fatal
            continue
    return jar


# --------------------------------------------------------------------------
# Publisher PDF URLs, by DOI prefix
# --------------------------------------------------------------------------
#: DOI prefix -> canonical PDF URL templates for publishers whose PDF path is
#: derivable from the DOI. Open-access hosts are deliberately absent: the
#: open-access chain already handles them.
#:
#: SAGE, Taylor & Francis and Wiley duplicate patterns that also live in
#: `try_landing_page_pdf_fallback`, which keys them on the resolved landing
#: host. This route goes straight from a DOI to a URL, with no landing page to
#: resolve, so the two tables are not interchangeable.
PUBLISHER_PDF_TEMPLATES = {
    "10.1007": ["https://link.springer.com/content/pdf/{doi}.pdf"],
    "10.1057": ["https://link.springer.com/content/pdf/{doi}.pdf"],
    "10.1002": ["https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true",
                "https://onlinelibrary.wiley.com/doi/pdf/{doi}"],
    "10.1111": ["https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true",
                "https://onlinelibrary.wiley.com/doi/pdf/{doi}"],
    "10.1080": ["https://www.tandfonline.com/doi/pdf/{doi}?download=true"],
    "10.1177": ["https://journals.sagepub.com/doi/pdf/{doi}"],
    "10.1098": ["https://royalsocietypublishing.org/doi/pdf/{doi}"],
}


def publisher_pdf_urls(doi: str) -> List[str]:
    """Canonical publisher PDF URLs for a DOI, or ``[]`` if none is derivable."""
    prefix = (doi or "").split("/")[0]
    return [t.format(doi=doi) for t in PUBLISHER_PDF_TEMPLATES.get(prefix, [])]


def _save_pdf(response, save_path) -> bool:
    """Stream a response to disk if it is a PDF under the size cap."""
    first = True
    written = 0
    tmp = Path(str(save_path) + ".part")
    try:
        with open(tmp, "wb") as f:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                if first:
                    if chunk[:4] != b"%PDF":
                        return False
                    first = False
                written += len(chunk)
                if written > MAX_PDF_BYTES:
                    return False
                f.write(chunk)
        if first:
            return False
        tmp.replace(save_path)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


def fetch_with_cookies(doi, save_path, cookies, verbose=False,
                       timeout=90) -> Optional[str]:
    """Try each canonical publisher PDF URL with the browser's own cookies.

    Returns the URL that produced the PDF (the caller records provenance and
    runs the identity check), or None.
    """
    for url in publisher_pdf_urls(doi):
        # A fresh Session per URL: requests.Session is not thread-safe, the
        # batch worker pool calls this concurrently, and each URL gets a jar
        # scoped to its own publisher.
        session = requests.Session()
        session.headers.update(headers_for(url, {"Accept": "application/pdf,*/*"}))
        session.cookies = cookie_jar(cookies, url)
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True,
                                   stream=True)
            if verbose:
                print(f"  institutional: {url} -> HTTP {response.status_code}")
            if response.status_code == 200 and _save_pdf(response, save_path):
                return url
        except requests.RequestException as exc:
            if verbose:
                print(f"  institutional: {url} -> {type(exc).__name__}")
        finally:
            session.close()
    return None


def fetch(doi, save_path, cookies_path, verbose=False,
          accept=None, on_download=None) -> Tuple[Optional[str], List[str]]:
    """Run the cookie route. Returns ``(saved_path or None, reasons)``.

    ``accept(path, url)`` is the caller's identity check; a PDF that fails it
    is not a result. ``on_download(path, url)`` records provenance.
    """
    if not cookies_path:
        return None, []
    if not publisher_pdf_urls(doi):
        return None, ["no publisher PDF URL is known for this DOI prefix"]
    try:
        cookies = load_cookies(cookies_path)
    except (OSError, ValueError) as exc:
        return None, [f"cookie file could not be read ({type(exc).__name__})"]
    url = fetch_with_cookies(doi, save_path, cookies, verbose=verbose)
    if not url:
        return None, ["the publisher did not serve a PDF with these cookies "
                      "(expired session, or no subscription)"]
    if on_download:
        on_download(save_path, url)
    if accept is None or accept(save_path, url):
        return save_path, []
    return None, ["the publisher's PDF failed the identity check"]
