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

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from html import unescape
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.cookies import create_cookie

from .._http import POLITE_USER_AGENT, headers_for

#: Largest PDF this route will write. Read in chunks and abandoned past this,
#: so a misbehaving host cannot fill memory or disk.
MAX_PDF_BYTES = 300 * 1024 * 1024

SOURCE = "institutional_cookies"


# --------------------------------------------------------------------------
# Where the cookies come from
# --------------------------------------------------------------------------
# Off until the user opts in. Two ways to opt in:
#
# * `fetchpdf fetchcookies` writes ACCESS_CONFIG (library + everyday browser). From
#   then on every fetchpdf run re-reads the publisher cookies straight from that
#   browser, once per process, so sessions the user keeps alive by ordinary
#   browsing are picked up without re-exporting anything.
# * `--cookies FILE` / `cookies_file=` names a cookie file for this process.
#
# The choice is process state, not a parameter threaded through every call:
# the tiered engine's T5 rung re-enters the chain without the caller's
# arguments, and the route has to fire there too.
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "fetchpdf"
ACCESS_CONFIG = CONFIG_DIR / "access.json"

_source_lock = threading.Lock()
_explicit_file = None          # path set by --cookies / cookies_file=
_disabled = False              # --no-cookies
_cookies_only = False          # --cookies-only
_live_file: Optional[str] = None
_live_resolved = False


def load_access_config() -> Optional[dict]:
    """The `fetchpdf fetchcookies` configuration, or None if access is not set up."""
    try:
        cfg = json.loads(ACCESS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) and cfg.get("enabled", True) else None


def use_cookies_file(path) -> None:
    """Use this cookie file for the rest of the process (``--cookies``)."""
    global _explicit_file, _disabled
    with _source_lock:
        _explicit_file, _disabled = (str(path) if path else None), False


def disable_cookies() -> None:
    """Never use institutional access in this process (``--no-cookies``)."""
    global _disabled
    with _source_lock:
        _disabled = True


def set_cookies_only(on: bool = True) -> None:
    """Skip every other source: the cookie route alone (``--cookies-only``)."""
    global _cookies_only
    _cookies_only = bool(on)


def cookies_only() -> bool:
    return _cookies_only


def reset_cookie_source() -> None:
    """Forget every process-level choice (tests; long-lived library callers)."""
    global _explicit_file, _disabled, _cookies_only, _live_file, _live_resolved
    with _source_lock:
        _explicit_file, _disabled, _cookies_only = None, False, False
        _live_file, _live_resolved = None, False
    with _stats_lock:
        _site_stats.clear()


def reload_cookie_source() -> None:
    """Re-export the browser's cookies on next use (after a mid-run setup or refresh).

    Unlike reset_cookie_source, keeps --no-cookies, --cookies and the tally.
    """
    global _live_file, _live_resolved
    with _source_lock:
        _live_file, _live_resolved = None, False


def cookies_user_managed() -> bool:
    """True when this process was told how to treat cookies (--no-cookies, --cookies FILE).

    The end-of-run offer to import cookies stays out of the way of both.
    """
    return _disabled or bool(_explicit_file)


def active_cookies_file() -> Optional[str]:
    """The cookie file this process should use, or None when access is off.

    An explicit ``--cookies`` wins. Otherwise, if `fetchpdf fetchcookies` has been
    run, the configured browser's cookies are exported once per process to the
    config dir and that file is used; if the browser cannot be read (locked
    store, keyring prompt declined) the last export is used instead.
    """
    global _live_file, _live_resolved
    if _disabled:
        return None
    if _explicit_file:
        return _explicit_file
    with _source_lock:
        if _live_resolved:
            return _live_file
        _live_resolved = True
        cfg = load_access_config()
        if not cfg:
            return None
        out = Path(cfg.get("cookie_file") or CONFIG_DIR / "cookies" / f"{cfg.get('key', 'library')}.json")
        try:
            from ..get_cookies import export_from_browser
            export_from_browser(cfg["browser"], out, cfg.get("name", "library"), quiet=True)
        except Exception as exc:      # noqa: BLE001 - fall back to the last export
            print(f"  Institutional access: could not read {cfg.get('browser')} cookies "
                  f"({type(exc).__name__}); using the last export")
        _live_file = str(out) if out.exists() else None
        return _live_file


# --------------------------------------------------------------------------
# Per-publisher tally, for the end-of-run "session expired?" hint
# --------------------------------------------------------------------------
_stats_lock = threading.Lock()
_site_stats: dict = {}


def _note_attempt(doi: str, ok: bool) -> None:
    site = (publisher_sites(doi) or [""])[0]
    with _stats_lock:
        tried, won = _site_stats.get(site, (0, 0))
        _site_stats[site] = (tried + 1, won + (1 if ok else 0))


def _probe_still_works(site: str) -> Optional[bool]:
    """Fetch the publisher's known-good test article with the active cookies.

    True: the session is fine (the misses were titles the library does not
    take). False: the probe is refused too. None: no probe for this site.
    """
    try:
        from ..get_cookies import PROBES
    except Exception:      # noqa: BLE001
        return None
    doi = next((d for d in PROBES.values()
                if registrable_domain(urlsplit(publisher_article_url(d) or "").hostname or "") == site),
               None)
    cookie_file = active_cookies_file()
    if not doi or not cookie_file:
        return None
    import tempfile
    try:
        cookies = load_cookies(cookie_file)
        ua = load_user_agent(cookie_file)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "probe.pdf"
            url, challenged = fetch_with_cookies(doi, target, cookies, user_agent=ua)
            if not url and challenged:
                url = fetch_in_browser(doi, target, cookies, user_agent=ua)
        return bool(url)
    except Exception:      # noqa: BLE001
        return None


def session_report() -> List[str]:
    """Lines for the end of a run about publishers that refused every request.

    Five or more attempts and not one PDF is either a lapsed session or a run
    of titles the library does not subscribe to. The publisher's known-good
    test article tells the two apart, so the user is sent to `fetchpdf cookies
    refresh` only when signing in again would help.
    """
    with _stats_lock:
        stats = dict(_site_stats)
    lines = []
    for site, (tried, won) in sorted(stats.items()):
        if tried < 5 or won:
            continue
        works = _probe_still_works(site)
        if works:
            lines.append(f"{site}: 0 of {tried} PDFs, but the session works -- most "
                         f"likely titles your library does not subscribe to.")
        else:
            lines.append(f"{site}: 0 of {tried} PDFs -- the session has probably "
                         f"expired (or was never made). Run `fetchpdf cookies refresh`.")
    return lines


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


def cookie_jar(cookies: Sequence[dict], url: str, now: Optional[float] = None,
               sites: Optional[Sequence[str]] = None) -> requests.cookies.RequestsCookieJar:
    """A jar holding only the cookies that belong to *url*'s publisher.

    Each cookie keeps its own domain and path, so requests sends a cookie only
    where the browser would have. Scoping to the publisher's registrable domain
    as well is what makes a redirect safe: a hop to an unrelated host finds
    nothing in the jar to send. A real browser profile also holds thousands of
    cookies, and sending all of them earns ``400 Request Header Or Cookie Too
    Large``.

    *sites* widens the scope to several publisher sites, for a DOI link that
    redirects onto one of them; each cookie is still sent only to its own domain.
    """
    sites = [s for s in (sites or [registrable_domain(urlsplit(url).hostname or "")]) if s]
    now = time.time() if now is None else now
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        if not any(_domain_matches(c.get("domain", ""), s) for s in sites):
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
#: derivable from the DOI. Open-access hosts are absent unless they sit behind a
#: bot check the open-access chain cannot pass (PNAS, Science, AHA): there the
#: browser's own clearance cookie is what gets the PDF.
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
    # Atypon platforms, same /doi/pdf/ shape as SAGE and T&F.
    "10.1027": ["https://econtent.hogrefe.com/doi/pdf/{doi}"],
    "10.1287": ["https://pubsonline.informs.org/doi/pdf/{doi}"],
    "10.1073": ["https://www.pnas.org/doi/pdf/{doi}"],
    "10.1126": ["https://www.science.org/doi/pdf/{doi}"],
    "10.1200": ["https://ascopubs.org/doi/pdf/{doi}"],
    "10.1161": ["https://www.ahajournals.org/doi/pdf/{doi}"],
    "10.1152": ["https://journals.physiology.org/doi/pdf/{doi}"],
    "10.1128": ["https://journals.asm.org/doi/pdf/{doi}"],
    "10.1164": ["https://www.atsjournals.org/doi/pdf/{doi}"],
    "10.1021": ["https://pubs.acs.org/doi/pdf/{doi}"],
    # FASEB J is on Wiley; pdfdirect redirects to faseb.onlinelibrary.wiley.com.
    "10.1096": ["https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true",
                "https://onlinelibrary.wiley.com/doi/pdf/{doi}"],
}

#: DOI prefix -> the article's landing page on the publisher's own host. The
#: browser fallback lands here first: Cloudflare issues its challenge for the
#: site, not the PDF endpoint, and a PDF URL opened cold hands the challenge to
#: a fetch() that cannot run it. `fetchpdf cookies` also signs in by way of these.
PUBLISHER_ARTICLE_TEMPLATES = {
    "10.1007": "https://link.springer.com/article/{doi}",
    "10.1057": "https://link.springer.com/article/{doi}",
    "10.1002": "https://onlinelibrary.wiley.com/doi/{doi}",
    "10.1111": "https://onlinelibrary.wiley.com/doi/{doi}",
    "10.1080": "https://www.tandfonline.com/doi/full/{doi}",
    "10.1177": "https://journals.sagepub.com/doi/{doi}",
    "10.1098": "https://royalsocietypublishing.org/doi/{doi}",
    "10.1027": "https://econtent.hogrefe.com/doi/{doi}",
    "10.1287": "https://pubsonline.informs.org/doi/{doi}",
    "10.1073": "https://www.pnas.org/doi/{doi}",
    "10.1126": "https://www.science.org/doi/{doi}",
    "10.1200": "https://ascopubs.org/doi/{doi}",
    "10.1161": "https://www.ahajournals.org/doi/{doi}",
    "10.1152": "https://journals.physiology.org/doi/{doi}",
    "10.1128": "https://journals.asm.org/doi/{doi}",
    "10.1164": "https://www.atsjournals.org/doi/{doi}",
    "10.1021": "https://pubs.acs.org/doi/{doi}",
    "10.1096": "https://onlinelibrary.wiley.com/doi/{doi}",
}

#: DOI prefix -> the publisher's sites, for publishers whose PDF path is not
#: derivable from the DOI (Silverchair, HighWire, ScienceDirect, ...). The route
#: resolves the DOI with the cookies for these sites and takes the PDF link the
#: landing page itself declares (``citation_pdf_url``). Cookies go only to these
#: sites; a PDF served from another host (pdf.sciencedirectassets.com) is
#: fetched without any.
PUBLISHER_LANDING_SITES = {
    "10.1158": ["aacrjournals.org"],                    # AACR
    "10.1101": ["cshlp.org", "biorxiv.org", "medrxiv.org"],  # CSHL Press
    "10.18632": ["oncotarget.com"],
    "10.1093": ["oup.com"],
    "10.1210": ["oup.com"],                             # Endocrine Society
    "10.4049": ["oup.com", "aai.org"],                  # J Immunol
    "10.1001": ["jamanetwork.com"],
    "10.1182": ["ashpublications.org"],
    "10.1242": ["biologists.com"],
    "10.1183": ["ersjournals.com", "ersnet.org"],
    "10.1136": ["bmj.com"],
    "10.1530": ["bioscientifica.com"],
    "10.1097": ["lww.com"],
}

#: DOI prefix -> (pattern, replacement) turning the landing URL into the PDF
#: URL, for a publisher whose page declares no ``citation_pdf_url``.
LANDING_PDF_REWRITES = {
    "10.18632": (r"/article/(\d+)/.*$", r"/article/\1/pdf/"),   # Oncotarget
}


def _prefix(doi: str) -> str:
    return (doi or "").split("/")[0]


# --------------------------------------------------------------------------
# IEEE Xplore
# --------------------------------------------------------------------------
# IEEE keys every article by an "arnumber" that is not derivable from the DOI
# (10.1109/aina.2010.102 is document 5474692). Its landing pages sit behind
# AWS WAF -- a plain client gets HTTP 202 with an empty body -- so the arnumber
# cannot be scraped from them. doi.org's own redirect names it, though, and
# costs one request that never reaches IEEE. The PDF endpoint,
# stampPDF/getPDF.jsp, is not behind the WAF: without a session it redirects
# to Xplore/login.jsp, with one it serves the PDF. Measured 2026-11-08.
IEEE_PREFIX = "10.1109"
IEEE_SITE = "ieee.org"
_IEEE_ARNUMBER_RE = re.compile(r"ieeexplore\.ieee\.org/(?:abstract/)?document/(\d+)")
_ieee_lock = threading.Lock()
#: DOI -> arnumber. Only successful lookups are kept. Seeded with the
#: `fetchpdf cookies` test article so the wizard never needs the network to
#: know where IEEE lives.
_ieee_arnumbers = {"10.1109/embc.2014.6943774": "6943774"}


def ieee_arnumber(doi: str, timeout: float = 30) -> Optional[str]:
    """IEEE Xplore's article number for *doi*, from doi.org's redirect; None if unknown."""
    key = (doi or "").strip().lower()
    with _ieee_lock:
        if key in _ieee_arnumbers:
            return _ieee_arnumbers[key]
    url = f"https://doi.org/{key}"
    try:
        r = requests.get(url, allow_redirects=False, timeout=timeout,
                         headers=headers_for(url, {}))
        m = _IEEE_ARNUMBER_RE.search(r.headers.get("location") or "")
    except requests.RequestException:
        return None
    if not m:
        return None
    with _ieee_lock:
        _ieee_arnumbers[key] = m.group(1)
    return m.group(1)


def covers(doi: str) -> bool:
    """True if this route knows where the publisher keeps the DOI's PDF."""
    prefix = _prefix(doi)
    return (prefix in PUBLISHER_PDF_TEMPLATES or prefix in PUBLISHER_LANDING_SITES
            or prefix == IEEE_PREFIX)


def publisher_sites(doi: str) -> List[str]:
    """The publisher sites whose cookies this DOI's fetch may use, main one first."""
    prefix = _prefix(doi)
    if prefix == IEEE_PREFIX:
        return [IEEE_SITE]
    if prefix in PUBLISHER_LANDING_SITES:
        return list(PUBLISHER_LANDING_SITES[prefix])
    article = publisher_article_url(doi)
    return [registrable_domain(urlsplit(article).hostname or "")] if article else []


def cookie_sites() -> List[str]:
    """Every publisher site the route reads cookies for."""
    sites = {registrable_domain(urlsplit(t.format(doi="x/y")).hostname or "")
             for t in PUBLISHER_ARTICLE_TEMPLATES.values()}
    for more in PUBLISHER_LANDING_SITES.values():
        sites.update(more)
    sites.add(IEEE_SITE)
    return sorted(sites)


def publisher_article_url(doi: str) -> Optional[str]:
    """The article landing page on the publisher's own host, or None.

    For a publisher in PUBLISHER_LANDING_SITES that is the DOI link, which
    redirects there: its article URLs are not derivable from the DOI. For IEEE
    it is the Xplore document page, which costs a doi.org lookup the first time.
    """
    if _prefix(doi) == IEEE_PREFIX:
        n = ieee_arnumber(doi)
        return f"https://ieeexplore.ieee.org/document/{n}" if n else None
    template = PUBLISHER_ARTICLE_TEMPLATES.get(_prefix(doi))
    if template:
        return template.format(doi=doi)
    return f"https://doi.org/{doi}" if _prefix(doi) in PUBLISHER_LANDING_SITES else None


# --------------------------------------------------------------------------
# Download cap
# --------------------------------------------------------------------------
#: Most PDFs this route fetches per process. Subscription licences forbid
#: systematic downloading, and a publisher that sees it blocks the whole
#: institution; the cap bounds what a runaway batch can do. Staying within the
#: licence is the user's responsibility -- this is a backstop, not a judgement.
DEFAULT_MAX_DOWNLOADS = 5000

_cap_lock = threading.Lock()
_max_downloads = DEFAULT_MAX_DOWNLOADS
_downloads = 0


def set_max_downloads(n: Optional[int]) -> None:
    """Set the per-process cap (None or <= 0 restores the default) and reset the count."""
    global _max_downloads, _downloads
    with _cap_lock:
        _max_downloads = n if n and n > 0 else DEFAULT_MAX_DOWNLOADS
        _downloads = 0


def downloads_so_far() -> int:
    return _downloads


def _cap_reached() -> bool:
    return _downloads >= _max_downloads


def _count_download() -> None:
    global _downloads
    with _cap_lock:
        _downloads += 1


def publisher_pdf_urls(doi: str) -> List[str]:
    """Canonical publisher PDF URLs for a DOI, or ``[]`` if none is derivable."""
    if _prefix(doi) == IEEE_PREFIX:
        n = ieee_arnumber(doi)
        return [f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={n}"] if n else []
    return [t.format(doi=doi) for t in PUBLISHER_PDF_TEMPLATES.get(_prefix(doi), [])]


def covered_by_site(dois) -> dict:
    """The DOIs this route could try, grouped by publisher site (``wiley.com`` -> [...]).

    Prefix only: says cookies *could* fetch them, not that the library subscribes.
    """
    out: dict = {}
    for doi in dois:
        if not doi or not covers(doi):
            continue
        out.setdefault(publisher_sites(doi)[0], []).append(doi)
    return out


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


def load_user_agent(path) -> Optional[str]:
    """The browser User-Agent recorded in a `fetchpdf cookies` file, or None.

    Cloudflare binds its clearance cookie to the User-Agent that earned it, so
    replaying the cookies under a different one gets the challenge again.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if text.lstrip()[:1] != "{":
            return None
        ua = json.loads(text).get("user_agent")
        return str(ua) if ua else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_pdf_bytes(data: bytes, save_path) -> bool:
    if data[:4] != b"%PDF" or len(data) > MAX_PDF_BYTES:
        return False
    tmp = Path(str(save_path) + ".part")
    try:
        tmp.write_bytes(data)
        tmp.replace(save_path)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


_META_PDF_RES = (
    re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url', re.I),
)
#: linkinghub.elsevier.com answers a DOI with a page that forwards by script.
#: Elsevier itself is not covered (ScienceDirect declares no PDF link and its
#: /pdfft answers HTML even in a browser), but Cell Press and Lancet-hosted
#: society journals reach their own sites through it.
_ELSEVIER_REDIRECT_RE = re.compile(r'name=["\']redirectURL["\'][^>]*value=["\']([^"\']+)', re.I)


def _is_challenge(response) -> bool:
    """A Cloudflare challenge, or AWS WAF's (IEEE: 202 + x-amzn-waf-action)."""
    if response.status_code == 202:
        return bool(response.headers.get("x-amzn-waf-action"))
    return (response.status_code in (403, 429, 503)
            and "cloudflare" in (response.headers.get("server") or "").lower())


def _session(url, cookies, user_agent, accept, sites=None) -> requests.Session:
    # A fresh Session per request chain: requests.Session is not thread-safe,
    # the batch worker pool calls this concurrently, and each chain gets a jar
    # scoped to its own publisher.
    session = requests.Session()
    headers = headers_for(url, {"Accept": accept})
    # The browser's own UA, so Cloudflare honours its clearance cookie --
    # except where the host is known to want the polite UA (Springer serves
    # a Chrome UA an HTML interstitial on /content/pdf).
    if user_agent and headers.get("User-Agent") != POLITE_USER_AGENT:
        headers["User-Agent"] = user_agent
    session.headers.update(headers)
    session.cookies = cookie_jar(cookies, url, sites=sites)
    return session


def pdf_url_from_landing(html: str, base_url: str, doi: str = "") -> Optional[str]:
    """The PDF link a landing page declares for itself (``citation_pdf_url``)."""
    url = None
    for rx in _META_PDF_RES:
        m = rx.search(html or "")
        if m:
            url = urljoin(base_url, unescape(m.group(1).strip()))
            break
    rewrite = LANDING_PDF_REWRITES.get(_prefix(doi))
    if not url and rewrite and re.search(rewrite[0], urlsplit(base_url).path):
        url = re.sub(rewrite[0], rewrite[1], base_url.split("?")[0])
    # HighWire declares http:// links on https pages; a browser refuses the
    # mixed-content fetch, and the https URL serves the same file.
    if url and url.startswith("http://") and base_url.startswith("https://"):
        url = "https://" + url[len("http://"):]
    return url


def _is_crawl_gate(url: str) -> bool:
    """Silverchair's "Validate User" page, which a browser can also be sent to."""
    return "/crawlprevention/" in (url or "")


def _landing_pdf_urls(doi, cookies, verbose=False, timeout=90,
                      user_agent=None) -> Tuple[List[str], bool]:
    """Resolve the DOI with the publisher's cookies; return its declared PDF URL."""
    sites = publisher_sites(doi)
    url = f"https://doi.org/{doi}"
    session = _session(url, cookies, user_agent,
                       "text/html,application/xhtml+xml,*/*;q=0.8", sites=sites)
    try:
        for _hop in range(2):
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if verbose:
                print(f"  institutional: {url} -> HTTP {response.status_code} at {response.url}")
            if _is_challenge(response) or _is_crawl_gate(response.url):
                return [], True
            if response.status_code != 200:
                return [], False
            html = response.text[:2_000_000]
            forward = _ELSEVIER_REDIRECT_RE.search(html)
            if forward and "elsevier.com" in (urlsplit(response.url).hostname or ""):
                url = unquote(forward.group(1))
                continue
            pdf = pdf_url_from_landing(html, response.url, doi)
            return ([pdf] if pdf else []), False
    except requests.RequestException as exc:
        if verbose:
            print(f"  institutional: {url} -> {type(exc).__name__}")
    finally:
        session.close()
    return [], False


def fetch_with_cookies(doi, save_path, cookies, verbose=False,
                       timeout=90, user_agent=None) -> Tuple[Optional[str], bool]:
    """Try each canonical publisher PDF URL with the browser's own cookies.

    A publisher without a derivable PDF URL (PUBLISHER_LANDING_SITES) is asked
    for its landing page first, and the PDF it declares is tried.

    Returns ``(url, challenged)``: the URL that produced the PDF (the caller
    records provenance and runs the identity check) or None, and whether any
    answer was a bot challenge -- the cue for the browser fallback.
    """
    challenged = False
    urls = publisher_pdf_urls(doi)
    if not urls and _prefix(doi) in PUBLISHER_LANDING_SITES:
        urls, challenged = _landing_pdf_urls(doi, cookies, verbose=verbose,
                                             timeout=timeout, user_agent=user_agent)
    for url in urls:
        session = _session(url, cookies, user_agent, "application/pdf,*/*")
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True,
                                   stream=True)
            if verbose:
                print(f"  institutional: {url} -> HTTP {response.status_code}")
            if response.status_code == 200 and _save_pdf(response, save_path):
                return url, False
            if _is_challenge(response):
                challenged = True
        except requests.RequestException as exc:
            if verbose:
                print(f"  institutional: {url} -> {type(exc).__name__}")
        finally:
            session.close()
    return None, challenged


# --------------------------------------------------------------------------
# Browser fallback
# --------------------------------------------------------------------------
# Wiley, Taylor & Francis and SAGE sit behind Cloudflare, which answers 403 to
# a plain HTTP client whatever cookies it carries -- measured 2026-10-01 from a
# Harvard IP, all three refused requests while a headed Chromium loaded the
# same pages. So when the HTTP attempt meets a challenge, the same cookies go
# into a real browser: land on the article page (the challenge is issued for
# the site), then fetch() the PDF from inside the page, which sends the
# browser's own TLS fingerprint and cookies. Headed, on a private Xvfb display
# when one is available so batch workers do not open windows on the user's
# screen; the display is started once per process and shared.
_CHALLENGE_TITLES = ("just a moment", "attention required", "verify you are human")
_xvfb_lock = threading.Lock()
_xvfb_display: Optional[str] = None
_xvfb_proc = None


def _browser_display() -> Optional[str]:
    """An X display for headed Chromium: a shared private Xvfb, else $DISPLAY."""
    global _xvfb_display, _xvfb_proc
    if os.name == "nt" or sys.platform == "darwin":
        return ""
    if os.environ.get("FETCHPDF_INSTITUTIONAL_VISIBLE") and os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]
    with _xvfb_lock:
        if _xvfb_display and _xvfb_proc and _xvfb_proc.poll() is None:
            return _xvfb_display
        xvfb = shutil.which("Xvfb")
        if xvfb:
            for n in range(120, 140):
                if os.path.exists(f"/tmp/.X{n}-lock"):
                    continue
                try:
                    proc = subprocess.Popen(
                        [xvfb, f":{n}", "-screen", "0", "1400x900x24", "-nolisten", "tcp"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    break
                time.sleep(1.0)
                if proc.poll() is None:
                    _xvfb_proc, _xvfb_display = proc, f":{n}"
                    import atexit
                    atexit.register(proc.terminate)
                    return _xvfb_display
    return os.environ.get("DISPLAY")


def _playwright_cookies(cookies: Sequence[dict], sites) -> List[dict]:
    """The publisher's cookies, in the shape Playwright's add_cookies takes."""
    sites = [sites] if isinstance(sites, str) else list(sites)
    now = time.time()
    out = []
    for c in cookies:
        if not any(_domain_matches(c.get("domain", ""), s) for s in sites):
            continue
        if c.get("expires") and c["expires"] < now:
            continue
        out.append({
            "name": c["name"], "value": c["value"], "domain": c["domain"],
            "path": c.get("path") or "/", "secure": bool(c.get("secure")),
            "expires": float(c["expires"]) if c.get("expires") else -1,
        })
    return out


#: Runs inside the page. Returns the status, and the body as base64 only when
#: it really is a PDF, so a 400 KB paywall page is not shipped back.
_FETCH_JS = """async ([u, cap]) => {
  const r = await fetch(u, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  const head = String.fromCharCode(...b.slice(0, 4));
  if (head !== '%PDF' || b.length > cap) return {status: r.status, n: b.length, url: r.url};
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, n: b.length, url: r.url, b64: btoa(s)};
}"""


def wait_past_challenge(page, timeout_s: float = 30.0) -> bool:
    """Wait until the page is no longer a bot-challenge interstitial.

    Cloudflare names itself in the title; AWS WAF (IEEE Xplore) does not, but
    its page carries the challenge script's globals.
    """
    from .blocked import WAF_CHALLENGE_MARKERS
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            title = (page.title() or "").lower()
            body = page.content()[:20000] if "ieee.org" in (page.url or "") else ""
        except Exception:      # noqa: BLE001 - mid-navigation
            title, body = "just a moment", ""
        if not any(t in title for t in _CHALLENGE_TITLES) and \
                not any(m in body for m in WAF_CHALLENGE_MARKERS):
            return True
        page.wait_for_timeout(1000)
    return False


def _wait_off_linkinghub(page, timeout_s: float = 20.0) -> None:
    """Elsevier's DOI hop forwards by script; wait until it has."""
    deadline = time.time() + timeout_s
    while "linkinghub.elsevier.com" in (page.url or "") and time.time() < deadline:
        page.wait_for_timeout(1000)
    if "linkinghub.elsevier.com" not in (page.url or ""):
        wait_past_challenge(page)


def fetch_pdf_in_page(page, url: str) -> Tuple[Optional[bytes], dict]:
    """fetch() *url* from inside *page*; returns (pdf bytes or None, info)."""
    info = page.evaluate(_FETCH_JS, [url, MAX_PDF_BYTES])
    data = base64.b64decode(info.pop("b64")) if info.get("b64") else None
    return data, info


def fetch_in_browser(doi, save_path, cookies, user_agent=None,
                     verbose=False) -> Optional[str]:
    """The cookie route through a real browser. Returns the PDF URL, or None."""
    article = publisher_article_url(doi)
    pdf_urls = publisher_pdf_urls(doi)
    from_landing = not pdf_urls and _prefix(doi) in PUBLISHER_LANDING_SITES
    if not article or not (pdf_urls or from_landing):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("  institutional: playwright not installed; no browser fallback")
        return None
    display = _browser_display()
    if display is None:
        if verbose:
            print("  institutional: no display and no Xvfb; no browser fallback")
        return None
    sites = publisher_sites(doi)
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    try:
        with sync_playwright() as driver:
            # Headed is load-bearing. The installed Chrome when there is one: the
            # cookies usually come from it, and Cloudflare's clearance is bound to
            # the browser that earned it.
            launch = dict(headless=False, env=env,
                          ignore_default_args=["--enable-automation"],
                          args=["--disable-blink-features=AutomationControlled"])
            try:
                browser = driver.chromium.launch(channel="chrome", **launch)
            except Exception:      # noqa: BLE001 - no Chrome; Playwright's Chromium
                browser = driver.chromium.launch(**launch)
            try:
                context = browser.new_context(user_agent=user_agent or None,
                                              viewport={"width": 1400, "height": 900})
                context.add_cookies(_playwright_cookies(cookies, sites))
                page = context.new_page()
                page.goto(article, wait_until="domcontentloaded", timeout=60000)
                if not wait_past_challenge(page):
                    if verbose:
                        print(f"  institutional (browser): challenge did not clear on {article}")
                    return None
                if from_landing:
                    _wait_off_linkinghub(page)
                    pdf = pdf_url_from_landing(page.content(), page.url, doi)
                    pdf_urls = [pdf] if pdf else []
                    if verbose and not pdf:
                        print(f"  institutional (browser): no citation_pdf_url at {page.url}")
                for url in pdf_urls:
                    try:
                        data, info = fetch_pdf_in_page(page, url)
                    except Exception:      # noqa: BLE001 - the page was still settling
                        page.wait_for_timeout(3000)
                        data, info = fetch_pdf_in_page(page, url)
                    if not data and info.get("status") in (403, 429):
                        # Cloudflare refuses a burst of parallel workers (seen
                        # 2026-10-23 on PNAS: 2 of 6 at once, all fine singly).
                        page.wait_for_timeout(8000)
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        wait_past_challenge(page)
                        data, info = fetch_pdf_in_page(page, url)
                    if verbose:
                        print(f"  institutional (browser): {url} -> HTTP {info.get('status')}"
                              f"{' PDF' if data else ''}")
                    if data and _write_pdf_bytes(data, save_path):
                        return url
            finally:
                browser.close()
    except Exception as exc:      # noqa: BLE001 - never fail a record on this
        if verbose:
            print(f"  institutional (browser): failed ({type(exc).__name__})")
    return None


def fetch(doi, save_path, cookies_path, verbose=False,
          accept=None, on_download=None) -> Tuple[Optional[str], List[str]]:
    """Run the cookie route. Returns ``(saved_path or None, reasons)``.

    ``accept(path, url)`` is the caller's identity check; a PDF that fails it
    is not a result. ``on_download(path, url)`` records provenance.
    """
    if not cookies_path:
        return None, []
    if not covers(doi):
        return None, ["no publisher PDF URL is known for this DOI prefix"]
    if _cap_reached():
        return None, [f"download cap reached ({_max_downloads} this run); "
                      "raise it with --cookies-max"]
    try:
        cookies = load_cookies(cookies_path)
    except (OSError, ValueError) as exc:
        return None, [f"cookie file could not be read ({type(exc).__name__})"]
    user_agent = load_user_agent(cookies_path)
    url, challenged = fetch_with_cookies(doi, save_path, cookies, verbose=verbose,
                                         user_agent=user_agent)
    if not url and challenged:
        url = fetch_in_browser(doi, save_path, cookies, user_agent=user_agent,
                               verbose=verbose)
    if not url:
        _note_attempt(doi, False)
        return None, ["the publisher did not serve a PDF with these cookies "
                      "(expired session, or no subscription)"]
    if on_download:
        on_download(save_path, url)
    if accept is None or accept(save_path, url):
        _count_download()
        _note_attempt(doi, True)
        return save_path, []
    return None, ["the publisher's PDF failed the identity check"]
