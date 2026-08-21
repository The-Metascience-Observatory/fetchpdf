"""Shared HTTP constants and response checks.

Its own module because fetchpdf imports download_from_aa, so the
latter cannot import back from the former at module scope without a cycle.
"""

# Keep the Chrome version current: some hosts (Zenodo among them) 403 stale
# Chrome UAs as a bot signal, which silently breaks every source on that host.
#
# Defined once and referenced everywhere -- previously four copies drifted apart,
# leaving three Playwright/requests paths on Chrome/120 while the main header
# said 131, so the bot-signal problem this string exists to avoid still applied
# to exactly the browser-driven sources most likely to be fingerprinted.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def elsevier_first_page_only(headers) -> bool:
    """True when Elsevier's X-ELS-Status header marks a first-page-only PDF.

    A key entitled to a record's XML but not its PDF does not get an error
    from `Accept: application/pdf` -- it gets a genuine typeset FIRST PAGE:
    HTTP 200, %PDF magic, megabyte-scale, real text layer. Nothing in the body
    distinguishes it from the full article; the only in-band signal is

        X-ELS-Status: WARNING - Response limited to first page because
                      requestor not entitled to resource

    (observed 2026-08-16 on PII S0022510X13030578, where the "success" was
    1 of 8 pages). Matched case-insensitively on key and value: the live API
    serves the header lowercased over HTTP/2, while requests' own header dict
    is case-insensitive -- callers pass either.
    """
    for key, value in (headers or {}).items():
        if key.lower() == "x-els-status":
            return "limited to first page" in str(value).lower()
    return False
