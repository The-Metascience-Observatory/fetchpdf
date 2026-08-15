"""Supplementary files from Atypon-hosted journals, via a real browser tab.

Europe PMC holds supplements for many non-open-access articles and is not
permitted to serve them: `supplementaryFiles` answers HTTP 200 wrapping
`<errorBean>...Article with id PMC... is not open access one</errorBean>`.
Those files still exist on the publisher's site, and for PNAS and its Atypon
siblings they are reachable -- but only from something that behaves like a
person with a browser open.

What was measured against 10.1073/pnas.1209746109, all on the same machine and
network:

    direct GET of the SI url (curl, guessed name)         403
    Playwright headless, GET the real url                 403
    Playwright headless, in-page fetch() with 6 cookies   403
    Playwright headless, persistent profile, click        download canceled
    Playwright HEADED (xvfb), click the link              261 KB %PDF   <-- works
    same, fresh profile, on an open-access control        341 KB %PDF   <-- works

Two things are load-bearing and neither is obvious:

  * **Headed, not headless.** This is the discriminating factor. Cookies, a
    persistent profile, a referer and a browser user-agent were all tried
    headless and all refused. On a headless server that means xvfb.
  * **Click, do not navigate.** A direct navigation to the same URL still 403s
    even when headed. The download has to originate from a user gesture on the
    listing page.

The redirect chain explains why: `302 -> ?cookieSet=1 -> 302 ->
/action/cookieAbsent` is Atypon's cookie handshake behind Cloudflare, and it
only completes for a client that looks entirely ordinary.

This is expensive -- one browser per record -- so it is deliberately the last
resort, and the caller only reaches it for the handful of records EPMC has
already said it is withholding.
"""

import os
import re
import shutil
import tempfile
from typing import List, Optional

from .supplement_index import ROLE_SUPPLEMENT, SupplementFile

#: Publishers on the Atypon platform, whose SI lives at /doi/suppl/<doi> and
#: whose files hang off a[href*="suppl_file"]. PNAS is the case that prompted
#: this; the others share the platform and the same URL shape.
_ATYPON_HOSTS = {
    "10.1073": "https://www.pnas.org",          # PNAS
    "10.1126": "https://www.science.org",       # Science / AAAS
    "10.1146": "https://www.annualreviews.org",  # Annual Reviews
    "10.1177": "https://journals.sagepub.com",  # SAGE
    "10.1080": "https://www.tandfonline.com",   # Taylor & Francis
}

_SUPPL_PATH = "{host}/doi/suppl/{doi}"

#: Long enough for Cloudflare's handshake plus a real page render. The listing
#: page took ~5 s to settle in testing; the download itself is fast.
_PAGE_TIMEOUT_MS = 90000
_SETTLE_MS = 5000
_DOWNLOAD_TIMEOUT_MS = 45000

#: One browser per record is the whole cost of this provider, so cap how many
#: files a single record may pull through it.
_MAX_FILES = 12


def atypon_host_for(doi: str) -> Optional[str]:
    """The publisher base URL for a DOI, or None if not an Atypon journal."""
    if not doi:
        return None
    prefix = str(doi).split("/", 1)[0].strip()
    return _ATYPON_HOSTS.get(prefix)


def headed_browser_available() -> bool:
    """Whether a headed Chromium can actually be launched here.

    On Linux that needs a display: either a real one ($DISPLAY) or xvfb. On
    Windows and macOS headed mode works directly. Returning False makes the
    caller degrade to its warning rather than raising.
    """
    if os.name == "nt" or os.sys.platform == "darwin":
        return True
    if os.environ.get("DISPLAY"):
        return True
    return shutil.which("xvfb-run") is not None or shutil.which("Xvfb") is not None


def _start_xvfb(ctx):
    """A virtual X display for headed Chromium, or None.

    Returns the Popen so the caller can stop it. Uses a high display number to
    avoid colliding with a real session, and verifies the server actually came
    up rather than assuming.
    """
    import subprocess
    import time as _time
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return None
    for display in range(99, 110):
        lock = f"/tmp/.X{display}-lock"
        if os.path.exists(lock):
            continue
        try:
            proc = subprocess.Popen(
                [xvfb, f":{display}", "-screen", "0", "1400x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        _time.sleep(1.0)
        if proc.poll() is None:
            os.environ["DISPLAY"] = f":{display}"
            return proc
        return None
    return None


def _stop_xvfb(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    os.environ.pop("DISPLAY", None)


def fetch_atypon_supplements(ids, ctx) -> List[SupplementFile]:
    """Download SI from an Atypon publisher into a temp dir, as local files.

    Unlike every other enumerator this cannot return remote URLs for the
    engine to fetch: the URLs only work from inside the browser session that
    clicked them. So the files are downloaded here and handed back as
    `file://` entries the normal pipeline can take.
    """
    doi = getattr(ids, "doi", None)
    host = atypon_host_for(doi)
    if not host:
        return []
    if not headed_browser_available():
        ctx.log("    atypon SI: no display and no xvfb; skipping headed fetch")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        ctx.log("    atypon SI: playwright not installed")
        return []

    # Headed Chromium needs a display. On a headless Linux box there is none,
    # so start a virtual one for the duration -- Playwright reads $DISPLAY at
    # launch. Windows/macOS need nothing.
    virtual_display = None
    if os.name != "nt" and os.sys.platform != "darwin" \
            and not os.environ.get("DISPLAY"):
        virtual_display = _start_xvfb(ctx)
        if virtual_display is None:
            ctx.log("    atypon SI: could not start a virtual display")
            return []

    listing = _SUPPL_PATH.format(host=host, doi=doi)
    scratch = tempfile.mkdtemp(prefix=".fetchpdf-atypon-")
    files: List[SupplementFile] = []

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                os.path.join(scratch, "profile"),
                headless=False,          # load-bearing; see module docstring
                accept_downloads=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1400, "height": 900},
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                response = page.goto(listing, wait_until="domcontentloaded",
                                     timeout=_PAGE_TIMEOUT_MS)
                status = response.status if response else None
                if status != 200:
                    ctx.log(f"    atypon SI: listing page HTTP {status}")
                    return []
                page.wait_for_timeout(_SETTLE_MS)

                links = page.eval_on_selector_all(
                    'a[href*="suppl_file"]', "els => els.map(e => e.href)")
                if not links:
                    ctx.log("    atypon SI: no suppl_file links on the listing page")
                    return []

                for index, href in enumerate(dict.fromkeys(links)):
                    if index >= _MAX_FILES:
                        ctx.log(f"    atypon SI: stopping at {_MAX_FILES} files")
                        break
                    name = href.rsplit("/", 1)[-1].split("?")[0] or f"si_{index+1}"
                    try:
                        # A click, not a navigation -- see the module docstring.
                        # Selected by nth match rather than by full href: the
                        # DOM href is often relative while eval returns it
                        # absolute, so an [href="<absolute>"] selector matches
                        # nothing and times out.
                        with page.expect_download(
                                timeout=_DOWNLOAD_TIMEOUT_MS) as download:
                            page.locator('a[href*="suppl_file"]').nth(index).click(
                                timeout=20000)
                        saved = os.path.join(scratch, name)
                        download.value.save_as(saved)
                    except Exception as error:
                        ctx.log(f"    atypon SI: {name}: "
                                f"{str(error).splitlines()[0][:80]}")
                        continue
                    if not os.path.exists(saved) or os.path.getsize(saved) == 0:
                        continue
                    files.append(SupplementFile(
                        name=name,
                        url="file://" + saved,
                        provider="atypon_suppl",
                        listing_index=index,
                        size_bytes=os.path.getsize(saved),
                        role=ROLE_SUPPLEMENT,
                        origin_doi=doi,
                        extra={"listing_url": listing, "local": saved,
                               "scratch_dir": scratch},
                    ))
                if files:
                    ctx.log(f"    atypon SI: {len(files)} file(s) via headed browser")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as error:
        ctx.log(f"    atypon SI: {str(error).splitlines()[0][:100]}")
    finally:
        _stop_xvfb(virtual_display)

    if not files:
        # Nothing to keep the scratch dir for.
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        # The browser profile is large and is dead weight once the files are
        # downloaded; the files themselves must survive until the pipeline has
        # copied them out, so only the profile goes now.
        shutil.rmtree(os.path.join(scratch, "profile"), ignore_errors=True)
        ctx.scratch.setdefault("_atypon_scratch_dirs", []).append(scratch)
    return files


def cleanup_scratch(ctx) -> None:
    """Remove the temp dirs this provider downloaded into.

    Called once the pipeline has copied the files to their real homes. Kept
    separate from fetch because the `file://` entries must stay readable until
    then -- deleting on the way out would race the download.
    """
    for path in ctx.scratch.pop("_atypon_scratch_dirs", []) or []:
        shutil.rmtree(path, ignore_errors=True)
