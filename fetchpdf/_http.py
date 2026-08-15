"""Shared HTTP constants.

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
