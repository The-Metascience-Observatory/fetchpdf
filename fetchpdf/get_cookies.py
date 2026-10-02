"""fetchpdf cookies: set up institutional access for fetchpdf, once.

    fetchpdf cookies setup        # one-time wizard
    fetchpdf cookies check        # which publishers your sessions currently open
    fetchpdf cookies refresh      # re-open sign-in tabs for the ones that lapsed
    fetchpdf cookies disable      # stop using institutional access

Institutional access lets fetchpdf download a paper your library subscribes to
when no open-access copy exists. It is OFF until you run ``fetchpdf cookies setup``.

How it works. Each publisher keeps your library sign-in as a cookie on its own
site (wiley.com, tandfonline.com, ...). The wizard opens one subscription
article per publisher, through your library, as tabs **in your own everyday
browser**; you sign in on the first one (your library's single sign-on carries
you through the rest), and the wizard checks that each publisher now serves a
PDF. It saves your library and browser to ``~/.config/fetchpdf/access.json``.
From then on every fetchpdf run reads those publishers' cookies straight from
that browser, so sessions you keep alive by ordinary browsing are picked up
with nothing to re-export. When one lapses, fetchpdf says so at the end of the
run and ``fetchpdf cookies refresh`` re-opens just those tabs.

Why your own browser and not one this tool drives: identity providers refuse a
browser they can tell is automated -- HarvardKey answers "Unable to sign in".
The automated mode is still here as ``fetchpdf cookies window`` for libraries whose
sign-in tolerates it.

Only cookies for the covered publishers' own domains are read or sent, and the
files are written owner-only under ``~/.config/fetchpdf/``. Treat them as
passwords.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlsplit

from .retrieval import institutional as inst

#: Libraries with a known sign-in link. The OpenAthens redirector picks the
#: right way in for each publisher, so one prefix serves them all. Anyone else
#: pastes their own prefix, or signs in with each publisher's "Access through
#: your institution" button.
INSTITUTIONS: Dict[str, dict] = {
    "harvard": {
        "name": "Harvard Library",
        "redirector": "https://go.openathens.net/redirector/library.harvard.edu?url=",
        "login_hint": "sign in with your HarvardKey",
    },
}

#: One subscription (non-open-access) article per publisher, used to open a
#: session and to test it. A library that does not take that particular journal
#: shows "no access" for the publisher even with a good session.
PROBES: Dict[str, str] = {
    "wiley": "10.1111/desc.13561",
    "tandf": "10.1080/10410236.2024.2434955",
    "sage": "10.1177/01461672251411348",
    "springer": "10.1007/s10823-025-09558-5",
    "hogrefe": "10.1027/1618-3169/a000636",
    "informs": "10.1287/mnsc.2023.4866",
}

PUBLISHER_NAMES = {"wiley": "Wiley", "tandf": "Taylor & Francis", "sage": "SAGE",
                   "springer": "Springer", "hogrefe": "Hogrefe", "informs": "INFORMS"}

CONFIG_DIR = inst.CONFIG_DIR
ACCESS_CONFIG = inst.ACCESS_CONFIG

#: browser_cookie3 loader name -> (executables, profile dirs that prove it is used).
BROWSERS = {
    "chrome": (["google-chrome", "google-chrome-stable"],
               ["~/.config/google-chrome", "~/Library/Application Support/Google/Chrome",
                "~/AppData/Local/Google/Chrome/User Data"]),
    "firefox": (["firefox"],
                ["~/.mozilla/firefox", "~/Library/Application Support/Firefox",
                 "~/AppData/Roaming/Mozilla/Firefox"]),
    "edge": (["microsoft-edge", "microsoft-edge-stable"],
             ["~/.config/microsoft-edge", "~/Library/Application Support/Microsoft Edge",
              "~/AppData/Local/Microsoft/Edge/User Data"]),
    "brave": (["brave-browser", "brave"],
              ["~/.config/BraveSoftware/Brave-Browser",
               "~/Library/Application Support/BraveSoftware/Brave-Browser",
               "~/AppData/Local/BraveSoftware/Brave-Browser/User Data"]),
    "chromium": (["chromium", "chromium-browser"],
                 ["~/.config/chromium", "~/Library/Application Support/Chromium"]),
}
MAC_APP_NAMES = {"chrome": "Google Chrome", "firefox": "Firefox", "edge": "Microsoft Edge",
                 "brave": "Brave Browser", "chromium": "Chromium"}


def _site(url: str) -> str:
    return inst.registrable_domain(urlsplit(url).hostname or "")


def _publisher_site(name: str) -> str:
    return _site(inst.publisher_article_url(PROBES[name]))


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


# --------------------------------------------------------------------------
# Reading cookies from the user's own browser
# --------------------------------------------------------------------------
def _chrome_user_agent() -> Optional[str]:
    """The User-Agent the installed Chrome sends, built from its version."""
    import re
    import subprocess
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
        if m:
            platform = {"darwin": "Macintosh; Intel Mac OS X 10_15_7",
                        "win32": "Windows NT 10.0; Win64; x64"}.get(sys.platform,
                                                                    "X11; Linux x86_64")
            return (f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{m.group(1)}.0.0.0 Safari/537.36")
    return None


def detect_browsers() -> List[str]:
    """Browsers that look installed and used here, most likely first."""
    found = []
    for name, (exes, profiles) in BROWSERS.items():
        if any(Path(p).expanduser().exists() for p in profiles) or \
                any(shutil.which(e) for e in exes):
            found.append(name)
    return found


def export_from_browser(browser: str, out_path: Path, label: str,
                        quiet: bool = False) -> Dict[str, int]:
    """Copy the covered publishers' cookies from *browser* into *out_path*.

    Returns cookies per publisher site. Raises if the browser's cookie store
    cannot be read (browser_cookie3 missing, store locked, keyring refused).
    """
    import browser_cookie3
    loader = getattr(browser_cookie3, browser)
    sites = sorted({_site(t.format(doi="x/y"))
                    for t in inst.PUBLISHER_ARTICLE_TEMPLATES.values()})
    cookies, per_site, unreadable = [], {}, []
    for site in sites:
        try:
            jar = loader(domain_name=site)
        except Exception as exc:      # noqa: BLE001
            if not quiet:
                print(f"  {site}: could not read cookies ({type(exc).__name__})")
            unreadable.append(site)
            continue
        n = 0
        for c in jar:
            if not c.domain or not inst._domain_matches(c.domain, site):
                continue
            cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                            "path": c.path or "/", "secure": bool(c.secure),
                            "expires": float(c.expires) if c.expires else -1,
                            "httpOnly": False, "sameSite": "Lax"})
            n += 1
        per_site[site] = n
    if unreadable:
        # Never replace a good export with a worse one. Chrome's store is
        # encrypted with a key from the desktop keyring, so a process outside
        # the login session (cron, ssh, a detached service) cannot read it --
        # and writing what it did read would sign every later run out. The
        # caller keeps using the last export instead.
        raise RuntimeError(
            f"could not read {browser}'s cookies for {', '.join(unreadable)} "
            "(no desktop keyring in this session?); the previous export is kept")
    state = {"cookies": cookies, "origins": [],
             "user_agent": _chrome_user_agent() if browser != "firefox" else None,
             "fetchpdf": {"institution": label, "source": f"browser:{browser}",
                          "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "cookies_per_site": per_site}}
    _write_private_json(Path(out_path), state)
    return per_site


# --------------------------------------------------------------------------
# Checking which publishers serve a PDF
# --------------------------------------------------------------------------
def check_access(cookie_file, publishers: List[str], verbose: bool = False) -> Dict[str, str]:
    """For each publisher: 'ok', 'not signed in' or 'no access'.

    Fetches the probe article's PDF exactly as a run would (plain request with
    the browser's cookies and User-Agent, browser fallback behind Cloudflare),
    into a temporary file that is deleted. Does not count toward the cap.
    """
    cookies = inst.load_cookies(cookie_file)
    ua = inst.load_user_agent(cookie_file)

    def probe(name: str) -> str:
        doi = PROBES[name]
        site = _publisher_site(name)
        if not any(inst._domain_matches(c["domain"], site) for c in cookies):
            return "not signed in"
        target = Path(tmp) / f"{name}.pdf"
        url, challenged = inst.fetch_with_cookies(doi, target, cookies,
                                                  verbose=verbose, user_agent=ua)
        if not url and challenged:
            url = inst.fetch_in_browser(doi, target, cookies, user_agent=ua,
                                        verbose=verbose)
        return "ok" if url else "no access"

    # Probes run concurrently: each Cloudflare-fronted publisher can take 90 s
    # (page load + challenge wait in a hidden browser), and serially that is
    # minutes of silence. fetch_in_browser owns its own Playwright session and
    # the Xvfb display is shared under a lock, so the probes are independent --
    # the batch downloader already runs this path from many worker threads.
    # The bar reports each publisher as it finishes; tqdm when installed, one
    # line per publisher otherwise.
    out = {}
    bar = _progress(len(publishers))
    with tempfile.TemporaryDirectory() as tmp, \
            ThreadPoolExecutor(max_workers=max(1, len(publishers))) as pool:
        futures = {pool.submit(probe, name): name for name in publishers}
        for fut in as_completed(futures):
            name = futures[fut]
            out[name] = fut.result()
            bar.update(f"{PUBLISHER_NAMES[name]}: {out[name]}")
    bar.close()
    return {name: out[name] for name in publishers}


class _PlainProgress:
    """tqdm's shape without tqdm: one line per publisher as each finishes."""

    def __init__(self, total):
        self._total = total
        self._n = 0

    def update(self, text):
        self._n += 1
        print(f"  [{self._n}/{self._total}] {text}", flush=True)

    def close(self):
        pass


class _TqdmProgress:
    def __init__(self, total):
        from tqdm import tqdm
        self._bar = tqdm(total=total, desc="  Testing publishers", unit="publisher",
                         leave=False, dynamic_ncols=True)

    def update(self, text):
        self._bar.set_postfix_str(text)
        self._bar.update(1)

    def close(self):
        self._bar.close()


def _progress(total):
    try:
        return _TqdmProgress(total)
    except ImportError:
        return _PlainProgress(total)


def _print_status(status: Dict[str, str]) -> None:
    marks = {"ok": "✅ ok", "not signed in": "❌ not signed in", "no access": "⚠️  no access"}
    print()
    for name, st in status.items():
        print(f"    {PUBLISHER_NAMES[name]:<18} {marks.get(st, st)}")
    print()
    if any(st == "no access" for st in status.values()):
        print("  'no access': a session exists but the test article was refused -- it\n"
              "  expired, or your library does not take that particular journal.\n")


# --------------------------------------------------------------------------
# Opening sign-in tabs in the user's own browser
# --------------------------------------------------------------------------
def signin_url(cfg: dict, name: str) -> str:
    article = inst.publisher_article_url(PROBES[name])
    redirector = cfg.get("redirector")
    return redirector + quote(article, safe="") if redirector else article


def open_tabs(browser: str, urls: List[str]) -> None:
    """Open *urls* as tabs in the user's own browser (not a driven one)."""
    if sys.platform == "darwin" and browser in MAC_APP_NAMES:
        subprocess.Popen(["open", "-a", MAC_APP_NAMES[browser], *urls])
        return
    for exe in BROWSERS.get(browser, ([], []))[0]:
        path = shutil.which(exe)
        if path:
            subprocess.Popen([path, *urls], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    for url in urls:
        webbrowser.open_new_tab(url)


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    return answer or default


def _signin_round(cfg: dict, publishers: List[str]) -> Dict[str, str]:
    """Open tabs for *publishers*, wait for the user, export, check."""
    hint = cfg.get("login_hint") or "sign in with your institutional account"
    print(f"\n  Opening {len(publishers)} tab(s) in {cfg['browser']}:")
    for name in publishers:
        print(f"    {PUBLISHER_NAMES[name]}")
    open_tabs(cfg["browser"], [signin_url(cfg, n) for n in publishers])
    if cfg.get("redirector"):
        print(f"""
  In the FIRST tab, {hint}. The other tabs then let you straight in
  (reload a tab if it loaded before you signed in).""")
    else:
        print("""
  On each tab, click "Access through your institution" (or "Institutional
  login"), choose your library and sign in. After the first one your library
  usually remembers you.""")
    print("""  On each, check that the PDF opens -- that is what fetchpdf will download.
""")
    _ask("  Press Enter when you are done... ")
    # A tab still mid-redirect holds a fresh, not-yet-signed-in session cookie;
    # reading then reports "no access" for a publisher that works seconds later
    # (seen 2026-10-01: Wiley and T&F). Settle, test, and re-test what failed.
    time.sleep(5)
    per_site = export_from_browser(cfg["browser"], Path(cfg["cookie_file"]), cfg["name"])
    print(f"  Read {sum(per_site.values())} publisher cookies from {cfg['browser']}. Testing...")
    status = check_access(cfg["cookie_file"], publishers)
    retry = [n for n, st in status.items() if st != "ok"]
    if retry:
        time.sleep(15)
        export_from_browser(cfg["browser"], Path(cfg["cookie_file"]), cfg["name"], quiet=True)
        status.update(check_access(cfg["cookie_file"], retry))
    return status


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def cmd_setup(args) -> int:
    print("""
  fetchpdf institutional access -- one-time setup
  ================================================
  fetchpdf will download papers your library subscribes to, after it has
  looked for an open-access copy. You sign in once, in your own browser.
  Downloading is subject to your library's licence terms: keep to them.
""")
    try:
        import browser_cookie3  # noqa: F401
    except ImportError:
        print("  A required dependency is missing: browser_cookie3.\n"
              "  Run pip install 'browser_cookie3>=0.19', then run fetchpdf cookies setup again.")
        return 2

    # 1. Library
    if args.redirector:
        key, inst_cfg = "custom", {"name": args.name or "my library",
                                   "redirector": args.redirector}
    elif args.institution:
        key, inst_cfg = args.institution, INSTITUTIONS[args.institution]
    else:
        print("  1. Your library")
        keys = sorted(INSTITUTIONS)
        for i, k in enumerate(keys, 1):
            print(f"     {i}. {INSTITUTIONS[k]['name']}")
        print(f"     {len(keys) + 1}. Another library: I have its OpenAthens sign-in link")
        print(f"     {len(keys) + 2}. Another library: I'll use each publisher's "
              f"'Access through your institution' button")
        choice = _ask(f"     Choose [1]: ", "1")
        n = int(choice) if choice.isdigit() else 1
        if 1 <= n <= len(keys):
            key, inst_cfg = keys[n - 1], INSTITUTIONS[keys[n - 1]]
        elif n == len(keys) + 1:
            prefix = _ask("     Paste the prefix (ends in '?url='), e.g.\n"
                          "     https://go.openathens.net/redirector/<your-domain>?url=\n     > ")
            key, inst_cfg = "custom", {"name": _ask("     Library name: ", "my library"),
                                       "redirector": prefix}
        else:
            key, inst_cfg = "custom", {"name": _ask("     Library name: ", "my library"),
                                       "redirector": None}

    # 2. Browser
    found = detect_browsers()
    browser = args.browser
    if not browser:
        if not found:
            print("  No supported browser found (Chrome, Firefox, Edge, Brave, Chromium).")
            return 2
        print("\n  2. The browser you use every day (fetchpdf reads its cookies)")
        for i, b in enumerate(found, 1):
            print(f"     {i}. {b}")
        choice = _ask("     Choose [1]: ", "1")
        browser = found[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(found) \
            else found[0]

    cfg = {
        "key": key, "name": inst_cfg["name"], "redirector": inst_cfg.get("redirector"),
        "login_hint": inst_cfg.get("login_hint"), "browser": browser,
        "cookie_file": str(CONFIG_DIR / "cookies" / f"{key}.json"), "enabled": True,
    }

    # 3. Sign in and test, until the user is content
    print("\n  3. Sign in")
    publishers = [p for p in PROBES if p in (args.publishers or PROBES)]
    status = _signin_round(cfg, publishers)
    _print_status(status)
    while any(st != "ok" for st in status.values()):
        missing = [n for n, st in status.items() if st != "ok"]
        again = _ask("  Re-open the ones that did not work? [y/N]: ", "n").lower()
        if again not in ("y", "yes"):
            break
        status.update(_signin_round(cfg, missing))
        _print_status(status)

    if not any(st == "ok" for st in status.values()):
        print("  No publisher served a PDF, so institutional access is NOT turned on.\n"
              "  Check you can open a PDF in your browser, then run fetchpdf cookies setup again.")
        return 1

    cfg["publishers"] = status
    cfg["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_private_json(ACCESS_CONFIG, cfg)
    print(f"""  Done. Institutional access is ON ({ACCESS_CONFIG}).

  Every fetchpdf run now reads these publishers' cookies from {browser}, and
  tries your library's copy after the fast open-access sources. Nothing else to do.
    - Sessions lapse. A run tells you when; then:  fetchpdf cookies refresh
    - One run without it:  fetchpdf ... --no-cookies
    - Turn it off:         fetchpdf cookies disable
""")
    return 0


def _require_config() -> Optional[dict]:
    cfg = inst.load_access_config()
    if not cfg:
        print("Institutional access is not set up. Run:  fetchpdf cookies setup")
    return cfg


def cmd_check(args) -> int:
    cfg = _require_config()
    if not cfg:
        return 2
    export_from_browser(cfg["browser"], Path(cfg["cookie_file"]), cfg["name"], quiet=True)
    status = check_access(cfg["cookie_file"], list(PROBES), verbose=args.verbose)
    print(f"Institutional access via {cfg['name']} ({cfg['browser']}):")
    _print_status(status)
    return 0 if any(st == "ok" for st in status.values()) else 1


def cmd_refresh(args) -> int:
    cfg = _require_config()
    if not cfg:
        return 2
    export_from_browser(cfg["browser"], Path(cfg["cookie_file"]), cfg["name"], quiet=True)
    status = check_access(cfg["cookie_file"], list(PROBES))
    lapsed = [n for n, st in status.items() if st != "ok"]
    if not lapsed:
        print("Every publisher still serves a PDF; nothing to refresh.")
        _print_status(status)
        return 0
    status.update(_signin_round(cfg, lapsed))
    _print_status(status)
    cfg["publishers"] = status
    _write_private_json(ACCESS_CONFIG, cfg)
    return 0 if any(st == "ok" for st in status.values()) else 1


def cmd_disable(args) -> int:
    cfg = inst.load_access_config()
    if not cfg:
        print("Institutional access is already off.")
        return 0
    cfg["enabled"] = False
    _write_private_json(ACCESS_CONFIG, cfg)
    print(f"Institutional access is OFF. Turn it back on with: fetchpdf cookies setup")
    return 0


def cmd_export(args) -> int:
    """Write a cookie file from a browser, for --cookies FILE (no setup)."""
    out = args.out or CONFIG_DIR / "cookies" / f"{args.browser}.json"
    per_site = export_from_browser(args.browser, Path(out), args.browser)
    for site, n in per_site.items():
        print(f"  {site:<28} {n} cookies")
    print(f"Wrote {out} (treat it as a password). Use: fetchpdf ... --cookies {out}")
    return 0 if any(per_site.values()) else 1


def cmd_window(args) -> int:
    if args.redirector:
        institution = {"name": "library", "redirector": args.redirector,
                       "login_hint": "sign in with your institutional account"}
        key = "custom"
    else:
        key = args.institution or "harvard"
        institution = INSTITUTIONS[key]
    out = args.out or CONFIG_DIR / "cookies" / f"{key}.json"
    profile = args.profile or CONFIG_DIR / "browser" / key
    publishers = [p.strip() for p in (args.publishers or ",".join(PROBES)).split(",") if p.strip()]
    return run_window(institution, Path(out), Path(profile), publishers,
                      args.login_timeout, args.force_login)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetchpdf cookies",
        description="Set up institutional access for fetchpdf (off until you run setup).")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("setup", help="one-time wizard: sign in through your library")
    p.add_argument("--institution", choices=sorted(INSTITUTIONS))
    p.add_argument("--redirector", help="your library's sign-in prefix, ending in '?url='")
    p.add_argument("--name", help="library name, with --redirector")
    p.add_argument("--browser", choices=sorted(BROWSERS))
    p.add_argument("--publishers", type=lambda s: [x.strip() for x in s.split(",")],
                   help=f"subset of: {', '.join(PROBES)}")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("check", help="which publishers your sessions currently open")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("refresh", help="re-open sign-in tabs for lapsed publishers")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("disable", help="stop using institutional access")
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("export", help="write a cookie file for --cookies FILE (no setup)")
    p.add_argument("--browser", choices=sorted(BROWSERS), required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("window", help="advanced: sign in inside a browser this tool drives")
    p.add_argument("--institution", choices=sorted(INSTITUTIONS))
    p.add_argument("--redirector")
    p.add_argument("--out", type=Path)
    p.add_argument("--profile", type=Path)
    p.add_argument("--publishers")
    p.add_argument("--login-timeout", type=float, default=600)
    p.add_argument("--force-login", action="store_true")
    p.set_defaults(func=cmd_window)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0 if inst.load_access_config() else 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------
# Advanced: sign in inside a browser this tool drives
# --------------------------------------------------------------------------
def _probe_pdf(page, doi: str) -> bool:
    """True if the publisher serves this article's PDF to the current page."""
    for url in inst.publisher_pdf_urls(doi):
        try:
            data, _info = inst.fetch_pdf_in_page(page, url)
        except Exception:      # noqa: BLE001 - navigation raced the evaluate
            data = None
        if data:
            return True
    return False


def _goto(page, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:      # noqa: BLE001 - a slow IdP is not fatal
        print(f"    (navigation: {type(exc).__name__}; carrying on)")


def _wait_for_publisher(page, site: str, login_timeout: float, hint: str,
                        prompted: List[bool]) -> str:
    """Wait until the redirector chain lands back on the publisher's host.

    Returns 'publisher', 'proxy' (landed on a rewritten proxy host) or 'timeout'.
    Prompts once, the first time the chain parks on a sign-in page.
    """
    deadline = time.time() + login_timeout
    parked_since: Optional[float] = None
    while time.time() < deadline:
        try:
            host = urlsplit(page.url).hostname or ""
        except Exception:      # noqa: BLE001
            host = ""
        here = inst.registrable_domain(host)
        if here == site:
            inst.wait_past_challenge(page)
            return "publisher"
        if site.split(".")[0] in host and here != site:
            return "proxy"     # e.g. onlinelibrary-wiley-com.<proxy host>
        if host and not host.startswith("go.openathens"):
            parked_since = parked_since or time.time()
            if not prompted[0] and time.time() - parked_since > 3:
                print(f"\n  >>> Sign-in needed: {hint} in the browser window "
                      f"(waiting up to {int(login_timeout // 60)} min) <<<\n")
                prompted[0] = True
        page.wait_for_timeout(1000)
    return "timeout"


def run_window(institution: dict, out_path: Path, profile: Path, publishers: List[str],
        login_timeout: float, force_login: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("fetchpdf cookies needs Playwright: pip install playwright && "
              "playwright install chromium")
        return 2
    if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        print("fetchpdf cookies opens a browser window for you to sign in; no $DISPLAY here.")
        return 2

    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    redirector = institution["redirector"]
    results: Dict[str, str] = {}
    prompted = [False]

    state = None
    with sync_playwright() as driver:
        # Sign-in pages (Okta, Microsoft, Google) refuse a browser that announces
        # automation -- "couldn't sign you in" -- so use the installed Chrome when
        # there is one, and drop the flags that mark it as driven.
        launch = dict(headless=False, no_viewport=True,
                      ignore_default_args=["--enable-automation"],
                      args=["--disable-blink-features=AutomationControlled"])
        try:
            context = driver.chromium.launch_persistent_context(
                str(profile), channel="chrome", **launch)
        except Exception:      # noqa: BLE001 - no Chrome installed; use Playwright's
            context = driver.chromium.launch_persistent_context(str(profile), **launch)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.pages[0] if context.pages else context.new_page()
        hops: List[str] = []

        def note_hop(frame):
            if frame == page.main_frame:
                host = urlsplit(frame.url).hostname or ""
                if host and (not hops or hops[-1] != host):
                    hops.append(host)
        page.on("framenavigated", note_hop)
        try:
            for name in publishers:
                hops.clear()
                doi = PROBES[name]
                article = inst.publisher_article_url(doi)
                site = _site(article)
                print(f"[{name}] {doi}")

                if not force_login:
                    _goto(page, article)
                    inst.wait_past_challenge(page)
                    if _probe_pdf(page, doi):
                        results[name] = "ok (network)" if not prompted[0] else "ok (session)"
                        print(f"    {results[name]}")
                        continue

                _goto(page, redirector + quote(article, safe=""))
                where = _wait_for_publisher(page, site, login_timeout,
                                            institution["login_hint"], prompted)
                if where == "proxy":
                    results[name] = "proxy mode"
                elif where == "timeout":
                    results[name] = "sign-in timed out"
                elif _probe_pdf(page, doi):
                    results[name] = "ok (session)"
                else:
                    results[name] = "no access"
                print(f"    {results[name]}    via {' > '.join(hops) or '-'}")

            state = context.storage_state()
            state["user_agent"] = page.evaluate("navigator.userAgent")
        except Exception as exc:      # noqa: BLE001
            if "closed" not in str(exc).lower():
                raise
            print("\nThe browser window was closed before the run finished. Your "
                  "sign-in (if any) is kept in the profile; rerun fetchpdf cookies to "
                  "finish and write the cookie file.")
            return 1
        finally:
            try:
                context.close()
            except Exception:      # noqa: BLE001 - already closed by the user
                pass

    state["fetchpdf"] = {
        "institution": institution["name"],
        "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "publishers": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out_path.parent, 0o700)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)

    print("\nPublisher       access")
    for name, status in results.items():
        print(f"  {name:<13} {status}")
    if any(s == "ok (network)" for s in results.values()):
        print("\nSome access came from the network you are on, not a sign-in. Those "
              "cookies only work on this network; rerun with --force-login off-VPN "
              "for a portable session.")
    print(f"\nWrote {out_path} ({len(state.get('cookies', []))} cookies; treat it as a password).")
    print(f"Use it with:  fetchpdf <dois.csv> -o <dir> --cookies {out_path}")
    return 0 if any(s.startswith("ok") for s in results.values()) else 1

