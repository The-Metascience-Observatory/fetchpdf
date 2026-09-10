"""Refused is not absent, and the manifest has to be able to tell them apart.

A publisher or CDN that answers 401, 403 or 429, or that serves a bot-challenge
page instead of the file, has said nothing whatever about whether the file
exists. Recording that as a download failure -- or worse, letting a record with
nothing kept fall through to `none_found` -- turns somebody else's bot
protection into a claim about the authors: "this paper published no
supplementary material".

MEASURED, and both cases are in this repository's own notes:

  * Atypon (`ahajournals.org/doi/suppl/10.1161/STROKEAHA.111.628537`) returns
    403 to a plain client while a browser gets the file in seconds. The
    fetcher queried eighteen providers, found nothing, and recorded
    `none_found`.
  * `www.pnas.org/doi/suppl/10.1073/pnas.1118373109` returns 403 and the
    manifest recorded `nothing_listed`.

The fix is a person, not a retry, and deliberately not a headless browser:
`repository_waf.py` exists for the one repository API where a browser is the
only route, and it is the last resort for that one host rather than a general
strategy. Everything here does is NAME the refusal and carry the URL, so the
missing-materials report can hand it to a human.

This module holds the definition once. Before it, "is this a bot challenge"
had three separate answers in three modules -- supplementary.py's body markers,
repository_waf.py's AWS WAF markers, request_drafts.py's title regex -- and
nothing tied them together, so a page one of them recognised was invisible to
the other two.
"""

import re

#: The skip/status reason. A string constant rather than a literal at each
#: site, because the whole point is that these records are findable later.
BLOCKED = "blocked"

#: HTTP answers that mean "a person could have this and we could not". 429 is
#: here for the same reason as 403 -- it is a refusal of this client, not a
#: statement about the file -- and it stays retryable: see
#: supplementary._is_transient, which reads the status and not just the reason.
BLOCKED_STATUSES = frozenset({401, 403, 429})

#: Bodies that are a challenge page rather than the file that was asked for.
#: Every one of these is served with HTTP 200 (or, at ACS, a 404 with 57 KB of
#: HTML), which is why the status code cannot be the check.
CHALLENGE_BODY_MARKERS = (
    b"recaptcha/challengepage",
    b"cf-browser-verification",
    b"Just a moment...",
    b"Attention Required! | Cloudflare",
    b"_Incapsula_Resource",
    b"px-captcha",
)

#: AWS WAF's own markers. Kept beside the others so there is one list to add to,
#: but named separately because repository_waf.py uses them for a second job the
#: rest cannot do: watching an interstitial CLEAR inside a real browser.
WAF_CHALLENGE_MARKERS = ("gokuProps", "awsWafCookieDomainList")

#: A challenge page's <title>. Matched from the start of the title, because
#: these pages say it first and an article whose title merely mentions access
#: denial is not a wall. Used by request_drafts.classify_block to separate a
#: wall from a page that will open for a person.
CHALLENGE_TITLE_RE = re.compile(
    r"\s*(just a moment|attention required|access denied|are you a robot)", re.I)

#: How much of a body to look at. A challenge page announces itself in its head;
#: reading further would only be a chance to match a phrase inside a real
#: document that happens to quote one.
_WINDOW = 2048


def looks_like_challenge_body(head: bytes) -> bool:
    """Whether these bytes are a bot challenge rather than the file asked for."""
    window = (head or b"")[:_WINDOW]
    if any(marker in window for marker in CHALLENGE_BODY_MARKERS):
        return True
    try:
        text = window.decode("utf-8", errors="replace")
    except Exception:                           # noqa: BLE001 - a guess, never a raise
        return False
    return any(marker in text for marker in WAF_CHALLENGE_MARKERS)


def classify(status=None, head: bytes = b"") -> str:
    """`"blocked"` when this answer is a refusal of us, else `""`.

    Returns a reason string rather than a bool so a caller can write it
    straight into a manifest, and so the empty case reads as "nothing to say
    about this" instead of as a negative claim.
    """
    if status in BLOCKED_STATUSES:
        return BLOCKED
    if head and looks_like_challenge_body(head):
        return BLOCKED
    return ""
