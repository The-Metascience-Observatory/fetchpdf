"""Institutional (subscribed) retrieval, entirely opt-in.

Two routes, both riding an institutional subscription the user already holds.
Neither runs unless a flag asks for it, and both run only after the
open-access chain has returned nothing.

  1. **Cookie route** (``--cookies FILE``). Build the publisher's canonical PDF
     URL from the DOI prefix and fetch it with cookies exported from the user's
     own signed-in browser.
  2. **EBSCOhost route** (``--ebsco``). Drive a signed-in Chrome: search
     EBSCO by DOI, read the record id, open the PDF viewer, take the signed
     ``content.ebscohost.com/cds/retrieve`` URL out of the viewer's resource
     timings, and download it with a plain HTTP GET. That signed URL is
     self-authenticating -- no cookies, no CORS workaround.

Two constraints shape the code rather than the other way round.

**A real browser holds thousands of cookies.** Sending all of them to one host
returns ``400 Request Header Or Cookie Too Large``. ``cookie_jar`` therefore
sets each cookie with its own domain and path, so requests sends only the
cookies that belong to the host being asked.

**Headless browsers are flagged by Cloudflare at most publishers**, which is
why the EBSCO route uses a visible browser and there is no automated tier.

Contributed by Lukas Wallrich; the field observations quoted in the README come
from running the original of this code over a paywalled psychology corpus.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .._http import USER_AGENT

#: Where the Playwright driver keeps its own signed-in Chrome profile. A
#: dedicated directory, never the user's default profile: Chrome refuses to
#: expose the default profile to automation, and a shared profile would mean
#: fetchpdf and the user fighting over the same lock file.
DEFAULT_PROFILE_DIR = Path(
    os.environ.get("FETCHPDF_CHROME_PROFILE")
    or Path.home() / ".fetchpdf" / "chrome-profile"
)

#: How long to keep re-asking a page for the record id / content URL. The
#: EBSCO viewer fetches both after load, so there is nothing to read for the
#: first few seconds; polling beats guessing a sleep.
EBSCO_POLL_TIMEOUT = 25.0

#: One browser, one front tab. `_batch_fetch_pdfs_inner` runs its records in a
#: ThreadPoolExecutor, and two workers navigating the same tab would read each
#: other's page. The EBSCO step is therefore serialised across workers.
_EBSCO_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------
@dataclass
class InstitutionalOptions:
    """Everything the two routes need, threaded as one keyword argument.

    ``enabled`` is false for the default construction, so a caller that always
    passes an options object still gets the default behaviour: nothing runs.
    """

    cookies_path: Optional[str] = None
    ebsco: bool = False
    ebsco_db: Optional[str] = None          # None == every database
    driver: str = "playwright"              # or "chrome-osascript"
    ebsco_profile: Optional[str] = None     # the <cluster> id; read from env
    profile_dir: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.cookies_path or self.ebsco)


@dataclass
class InstitutionalResult:
    """What happened, in terms a user can act on."""

    path: Optional[str] = None
    source: Optional[str] = None            # "institutional_cookies" | "ebsco"
    reasons: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None


# Outcome strings for the EBSCO route. They are distinguishable on purpose:
# each one tells the user something different about the record.
EBSCO_OK = "ok"
EBSCO_NOT_INDEXED = "not-indexed"           # no record for this DOI at all
EBSCO_NO_HOSTED_PDF = "linked-full-text-only"  # a record, but a link-out only
EBSCO_DOWNLOAD_FAILED = "download-failed"
EBSCO_NO_PROFILE = "no-profile"
EBSCO_NO_DRIVER = "no-driver"

_EBSCO_REASONS = {
    EBSCO_NOT_INDEXED: "EBSCO has no record for this DOI in the database(s) searched",
    EBSCO_NO_HOSTED_PDF: "EBSCO has the record but only 'Linked Full Text', "
                         "not a hosted PDF",
    EBSCO_DOWNLOAD_FAILED: "EBSCO returned a content URL but the download was "
                           "not a PDF",
    EBSCO_NO_PROFILE: "EBSCO_PROFILE is unset -- set it to your library's "
                      "cluster id (the <cluster> in research.ebsco.com/c/<cluster>/...)",
    EBSCO_NO_DRIVER: "no usable browser driver",
}


# --------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------
def load_cookies(path) -> List[dict]:
    """Read a cookie export into ``[{name, value, domain, path}, ...]``.

    Two formats, because the two easy ways to get cookies out of a browser
    produce different ones:

    * **Netscape ``cookies.txt``** -- what the browser extensions write. Note
      the ``#HttpOnly_`` prefix some of them put on a domain: those lines look
      like comments and are not, so the prefix is stripped rather than the line
      skipped.
    * **JSON** -- either a bare list (``browser-use cookies export``) or an
      object with a ``cookies`` key (Playwright ``storage_state``).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        raw = json.loads(text)
        items = raw if isinstance(raw, list) else raw.get("cookies", [])
        return [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", "") or "",
                "path": c.get("path", "/") or "/",
            }
            for c in items
            if isinstance(c, dict) and "name" in c and "value" in c
        ]
    return _parse_netscape(text)


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
        domain, _flag, cpath, _secure, _expiry, name, value = parts[:7]
        out.append({
            "name": name,
            "value": value,
            "domain": domain.strip(),
            "path": cpath.strip() or "/",
        })
    return out


def cookie_jar(cookies: Sequence[dict]) -> requests.cookies.RequestsCookieJar:
    """A domain-aware jar: each cookie keeps its own domain and path.

    This is the whole point of the route. A flat jar built by assigning
    ``session.cookies[name] = value`` sends every cookie in the export to every
    host, and a real browser profile holds enough of them that publishers
    answer ``400 Request Header Or Cookie Too Large``.
    """
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        try:
            jar.set(c["name"], c["value"],
                    domain=c.get("domain", "") or "",
                    path=c.get("path", "/") or "/")
        except Exception:      # noqa: BLE001 - one malformed row is not fatal
            continue
    return jar


# --------------------------------------------------------------------------
# Publisher PDF URLs, by DOI prefix
# --------------------------------------------------------------------------
#: DOI prefix -> canonical PDF URL templates for publishers whose PDF path is
#: derivable from the DOI. Open-access hosts are deliberately absent: PLOS and
#: SSRN are already handled by the open-access chain, and a subscription route
#: for them would only re-fetch what the chain has.
#:
#: SAGE, Taylor & Francis and Wiley duplicate patterns that also live in
#: `try_landing_page_pdf_fallback`, which keys them on the *resolved landing
#: host*. This route has no landing page to resolve -- it goes straight from a
#: DOI to a URL -- so the two tables are not interchangeable today. Unifying
#: them is a follow-up, not part of this route.
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


def fetch_with_cookies(doi, save_path, cookies_path, verbose=False,
                       timeout=90) -> Optional[str]:
    """Try each canonical publisher PDF URL with the browser's own cookies.

    Returns the URL that produced the bytes (the caller records provenance and
    runs the identity check), or None.
    """
    urls = publisher_pdf_urls(doi)
    if not urls:
        return None
    jar = cookie_jar(load_cookies(cookies_path))
    # A fresh Session per call: requests.Session is not thread-safe and the
    # batch worker pool calls this concurrently.
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/pdf,*/*"})
    session.cookies = jar
    try:
        for url in urls:
            try:
                response = session.get(url, timeout=timeout, allow_redirects=True)
            except requests.RequestException as exc:
                if verbose:
                    print(f"  institutional: {url} -> {exc}")
                continue
            if verbose:
                print(f"  institutional: {url} -> HTTP {response.status_code}, "
                      f"{len(response.content)} bytes")
            if response.status_code == 200 and response.content[:4] == b"%PDF":
                Path(save_path).write_bytes(response.content)
                return url
    finally:
        session.close()
    return None


# --------------------------------------------------------------------------
# Browser drivers
# --------------------------------------------------------------------------
# The EBSCO flow needs a signed-in, visible browser. It talks to one through
# four methods only -- open_tab, navigate, eval_js, close -- so the flow itself
# is driver-agnostic and the tests can hand it a fake.
class BrowserDriver:
    """The four things the EBSCO flow asks of a browser."""

    def open_tab(self) -> None:                 # pragma: no cover - interface
        raise NotImplementedError

    def navigate(self, url: str) -> None:       # pragma: no cover - interface
        raise NotImplementedError

    def eval_js(self, script: str) -> str:      # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:                    # pragma: no cover - interface
        raise NotImplementedError


class ChromeOsascriptDriver(BrowserDriver):
    """The user's already-running Chrome, driven through AppleScript.

    macOS only, and it needs *View > Developer > Allow JavaScript from Apple
    Events* switched on once. In exchange it uses the session the user is
    already signed into, with no second sign-in and no second profile. This is
    the field-tested path.
    """

    name = "chrome-osascript"

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    def _osa(self, script: str, timeout: int = 60) -> str:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=timeout)
        return (result.stdout or "").strip()

    def open_tab(self) -> None:
        self._osa('tell application "Google Chrome" to make new tab at end of '
                  'tabs of front window with properties {URL:"about:blank"}')
        self._osa('tell application "Google Chrome" to set active tab index of '
                  'front window to (count of tabs of front window)')

    def navigate(self, url: str) -> None:
        self._osa('tell application "Google Chrome" to set URL of '
                  f'(active tab of front window) to "{url}"')

    def eval_js(self, script: str) -> str:
        escaped = script.replace("\\", "\\\\").replace('"', '\\"')
        return self._osa('tell application "Google Chrome" to return execute '
                         f'(active tab of front window) javascript "{escaped}"')

    def close(self) -> None:
        # The tab is left open on purpose: it is the user's own window, and a
        # tab that closes itself hides whatever the page was showing when the
        # flow gave up.
        return None


class PlaywrightChromeDriver(BrowserDriver):
    """A dedicated, persistent Chrome profile driven by Playwright.

    Cross-platform, and it does not take over the window the user is working
    in. The profile is signed in once through ``fetchpdf --institutional-login``
    and keeps its session cookies from then on.

    Headed, not headless: publishers' bot detection flags headless Chrome. On
    Linux without a display it borrows the xvfb helpers the Atypon route
    already uses -- one xvfb implementation, not two that drift.
    """

    name = "playwright"

    def __init__(self, profile_dir=None, verbose=False):
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR)
        self.verbose = verbose
        self._playwright = None
        self._context = None
        self._page = None
        self._display = None

    @staticmethod
    def available() -> bool:
        from .supplement_atypon import headed_browser_available
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return headed_browser_available()

    def start(self) -> None:
        from playwright.sync_api import sync_playwright
        from .supplement_atypon import _start_xvfb
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._display = _start_xvfb(_XvfbLog(self.verbose))
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = (self._context.pages[0] if self._context.pages
                      else self._context.new_page())
        self._page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

    def open_tab(self) -> None:
        if self._context is None:
            self.start()

    def navigate(self, url: str) -> None:
        self.open_tab()
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:       # noqa: BLE001 - a slow page is not fatal
            if self.verbose:
                print(f"  institutional: navigation to {url} -> {exc}")

    def eval_js(self, script: str) -> str:
        try:
            return str(self._page.evaluate(script) or "")
        except Exception as exc:       # noqa: BLE001
            if self.verbose:
                print(f"  institutional: eval failed -> {exc}")
            return ""

    def close(self) -> None:
        from .supplement_atypon import _stop_xvfb
        for shutdown in (getattr(self._context, "close", None),
                         getattr(self._playwright, "stop", None)):
            try:
                if shutdown:
                    shutdown()
            except Exception:          # noqa: BLE001 - teardown never raises
                pass
        self._context = self._playwright = self._page = None
        _stop_xvfb(self._display)
        self._display = None


class _XvfbLog:
    """`_start_xvfb` wants a ctx with a `.log`; this route has no ctx."""

    def __init__(self, verbose=False):
        self.verbose = verbose

    def log(self, *args, **kwargs):
        if self.verbose and args:
            print(f"  institutional: {args[0]}")


def make_driver(name: str, profile_dir=None, verbose=False) -> Optional[BrowserDriver]:
    """Build the requested driver, or None when it cannot run here."""
    if name == ChromeOsascriptDriver.name:
        return ChromeOsascriptDriver() if ChromeOsascriptDriver.available() else None
    if name == PlaywrightChromeDriver.name:
        if not PlaywrightChromeDriver.available():
            return None
        return PlaywrightChromeDriver(profile_dir=profile_dir, verbose=verbose)
    return None


def institutional_login(driver_name="playwright", profile_dir=None,
                        ebsco_profile=None, wait=None) -> int:
    """Open the dedicated Chrome profile so the user can sign in once.

    Prints the page URL on exit, because that URL carries the ``<cluster>`` id
    the EBSCO route needs in ``EBSCO_PROFILE``.
    """
    if driver_name != PlaywrightChromeDriver.name:
        print("--institutional-login sets up the Playwright profile. The "
              "chrome-osascript driver uses your ordinary Chrome, so just sign "
              "in there as you normally would.")
        return 0
    driver = make_driver(PlaywrightChromeDriver.name, profile_dir=profile_dir,
                         verbose=True)
    if driver is None:
        print("Playwright with a real Chrome channel is not available here. "
              "Install Chrome and `playwright install chrome`, or use "
              "--ebsco-driver chrome-osascript on macOS.")
        return 1
    start = (f"https://research.ebsco.com/c/{ebsco_profile}"
             if ebsco_profile else "https://research.ebsco.com/")
    try:
        driver.navigate(start)
        print(f"Chrome is open at {start}.")
        print("Sign in through your institution / OpenAthens, then press Enter "
              "here. The profile keeps the session for later runs.")
        (wait or input)("")
        landed = driver.eval_js("location.href")
        if landed:
            print(f"Signed-in page: {landed}")
            match = re.search(r"research\.ebsco\.com/c/([A-Za-z0-9]+)", landed)
            if match:
                print(f"Set EBSCO_PROFILE={match.group(1)} in your .env.local.")
    finally:
        driver.close()
    return 0


# --------------------------------------------------------------------------
# EBSCOhost
# --------------------------------------------------------------------------
# Read the first /search/details/ link on the results page; 'NONE' when the
# search returned nothing we can use.
_RID_JS = (
    "(function(){var a=Array.from(document.querySelectorAll('a')).find("
    "function(e){return /\\/search\\/details\\//.test(e.href)});if(!a)return 'NONE';"
    "var m=a.href.match(/\\/details\\/([a-z0-9]+)/i);return m?m[1]:'NONE';})()"
)
# The signed content URL is not in the DOM -- the viewer fetches it, so it
# shows up in the page's resource timings.
_CONTENT_JS = (
    "(function(){var res=performance.getEntriesByType('resource')"
    ".map(function(r){return r.name});"
    "var c=res.find(function(n){return /content\\.ebscohost\\.com\\/cds\\/retrieve/.test(n)});"
    "return c||'NONE';})()"
)


def _poll(driver, script, timeout=None, interval=0.5, sleep=time.sleep) -> str:
    """Re-run a probe until it answers, instead of sleeping a fixed guess."""
    timeout = EBSCO_POLL_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        value = (driver.eval_js(script) or "").strip()
        if value and value != "NONE":
            return value
        if time.monotonic() >= deadline:
            return "NONE"
        sleep(interval)


def _http_download(url, save_path, timeout=180) -> bool:
    """Plain GET. The EBSCO content URL signs itself; cookies would add nothing."""
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return False
    if response.status_code != 200 or response.content[:4] != b"%PDF":
        return False
    Path(save_path).write_bytes(response.content)
    return True


def redact_url(url: str) -> str:
    """Drop the query string.

    The EBSCO content URL carries a token that grants the download. It must not
    reach a provenance file or a verbose log.
    """
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_via_ebsco(doi, save_path, ebsco_profile, db=None, driver=None,
                    verbose=False, download=_http_download, sleep=time.sleep,
                    timeout=None):
    """Search EBSCO by DOI and download the hosted PDF.

    Returns ``(outcome, url)``. ``url`` is redacted and only set on success.

    The failure outcomes are worth reading: ``not-indexed`` means EBSCO has no
    record for the DOI, while ``linked-full-text-only`` means it has the record
    but links out to the publisher instead of hosting the file. The first is a
    reason to look elsewhere; the second is a reason to try the cookie route.
    """
    if not ebsco_profile:
        return EBSCO_NO_PROFILE, None
    if driver is None:
        return EBSCO_NO_DRIVER, None

    base = f"https://research.ebsco.com/c/{ebsco_profile}"
    query = quote(doi, safe="")
    # A named database narrows the search; falling back to all databases costs
    # one more page load and finds records the named one does not carry.
    attempts = [f"&db={db}"] if db else []
    attempts.append("")

    with _EBSCO_LOCK:
        driver.open_tab()
        record_id = None
        for db_query in attempts:
            driver.navigate(f"{base}/search/results?q={query}{db_query}")
            found = _poll(driver, _RID_JS, timeout=timeout, sleep=sleep)
            if found != "NONE":
                record_id = found
                break
        if record_id is None:
            return EBSCO_NOT_INDEXED, None

        driver.navigate(f"{base}/viewer/pdf/{record_id}")
        content_url = _poll(driver, _CONTENT_JS, timeout=timeout, sleep=sleep)

    if content_url == "NONE":
        return EBSCO_NO_HOSTED_PDF, None
    if verbose:
        print(f"  institutional: EBSCO content URL {redact_url(content_url)}")
    if not download(content_url, save_path):
        return EBSCO_DOWNLOAD_FAILED, None
    return EBSCO_OK, redact_url(content_url)


# --------------------------------------------------------------------------
# Entry point used by the chain
# --------------------------------------------------------------------------
def fetch(doi, save_path, options: InstitutionalOptions, verbose=False,
          accept=None, on_download=None, driver=None) -> InstitutionalResult:
    """Run whichever institutional routes the options enabled.

    ``accept(path, url)`` is the caller's identity check; a PDF that fails it
    is not a result, and the next route is tried. ``on_download(path, url)``
    records provenance.
    """
    result = InstitutionalResult()
    if not options.enabled:
        return result

    if options.cookies_path:
        try:
            url = fetch_with_cookies(doi, save_path, options.cookies_path,
                                     verbose=verbose)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result.reasons.append(f"cookie file could not be read: {exc}")
            url = None
        if url:
            if on_download:
                on_download(save_path, url)
            if accept is None or accept(save_path, url):
                result.path = save_path
                result.source = "institutional_cookies"
                return result
            result.reasons.append("publisher PDF failed the identity check")
        else:
            result.reasons.append("no publisher PDF for this DOI with these cookies")

    if options.ebsco:
        own_driver = driver is None
        if own_driver:
            driver = make_driver(options.driver,
                                 profile_dir=options.profile_dir,
                                 verbose=verbose)
        try:
            outcome, url = fetch_via_ebsco(
                doi, save_path, options.ebsco_profile, db=options.ebsco_db,
                driver=driver, verbose=verbose,
            )
        finally:
            if own_driver and driver is not None:
                driver.close()
        if outcome == EBSCO_OK:
            if on_download:
                on_download(save_path, url)
            if accept is None or accept(save_path, url):
                result.path = save_path
                result.source = "ebsco"
                return result
            result.reasons.append("EBSCO PDF failed the identity check")
        else:
            result.reasons.append(_EBSCO_REASONS.get(outcome, outcome))

    return result
