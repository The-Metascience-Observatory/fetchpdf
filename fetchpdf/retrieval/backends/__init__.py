"""Backends for the retrieval agent: one contract, two very different sandboxes.

The agent's job is the same either way -- read the paper, work out which
supplementary files and datasets we do not already have, and get them -- but
what it is *allowed to do* differs so much between the two that the difference
is the interesting part.

MEASURED 2026-08-22, against Claude Code 2.1.240, because the containment story
in the design turned out to be wrong and only testing found it:

  * `--allowed-tools` GRANTS permission, it does not restrict the tool set. A
    subprocess run with `--allowed-tools "Bash(echo:*)"` happily ran
    `cat ../outside_canary.txt` and returned the contents. So did
    `--allowed-tools WebFetch`. Both with and without
    `--permission-mode bypassPermissions`.
  * `--disallowed-tools` DOES remove tools, and is the only lever that worked.
  * A default subprocess inherits the operator's own MCP servers. On the
    machine this was written on that meant Gmail, Google Drive, Google Calendar
    and a brokerage account were in scope for a subprocess whose job is
    downloading spreadsheets. `--strict-mcp-config` with an empty config
    removes them.

The consequence: the CLI backend gets WebFetch and nothing else -- no shell, no
file tools, no MCP -- and therefore *cannot write a file*. So it does not
download. It navigates, which is the part that actually needs a model (open the
landing page, find the real file link, decide whether this deposit belongs to
this paper), and reports URLs. fetchpdf does the transfer, through the same
capped, hashed, deduplicated path every other provider uses.

The OpenRouter backend is the opposite: there is no general-purpose agent
harness to lock down, because the two tools it can call are ours. `download`
writes inside the staging directory by construction and enforces the byte cap
itself. That backend really does download.

Both return the same receipt, and the caller cannot tell which one ran by
looking at the output directory. That is the point.
"""

from typing import Optional

#: The receipt one entry at a time. Keys a backend may set:
#:
#:   url        the direct file URL, or the deposit landing page
#:   kind       "supplement" (the paper's own SI) or "dataset" (a deposit)
#:   deposit    the repository landing page this file came from, if any
#:   name       the filename to record it under, when the URL does not say
#:   why        one short line: why this belongs to THIS paper
#:   saved_as   staging-relative path, set only by a backend that downloads
RECEIPT_KEYS = ("url", "kind", "deposit", "name", "why", "saved_as")

KIND_SUPPLEMENT = "supplement"
KIND_DATASET = "dataset"

#: Registered backends, resolved by `--llm-backend`.
BACKENDS = ("claude-cli", "openrouter")
DEFAULT_BACKEND = "claude-cli"


def get_backend(name: Optional[str] = None):
    """A backend instance by name, or None when the name is unknown.

    Imported lazily so a run that never asks for an agent never imports one.
    """
    name = (name or DEFAULT_BACKEND).strip().lower()
    if name == "claude-cli":
        from .claude_cli import ClaudeCliBackend
        return ClaudeCliBackend()
    if name == "openrouter":
        from .openrouter import OpenRouterBackend
        return OpenRouterBackend()
    return None


def normalize_receipt(raw, staging: str = "") -> list:
    """Whatever the model returned, reduced to entries we can act on.

    Defensive by policy rather than by taste: this is the one place a model's
    free-form output crosses into code that downloads things. An entry without
    a usable URL or staged file is dropped rather than guessed at, and a
    `saved_as` that escapes the staging directory is dropped too -- a path is
    the one field a model could use to reach outside its sandbox.
    """
    import os

    if not isinstance(raw, list):
        return []
    staging_real = os.path.realpath(staging) if staging else ""
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = {k: item.get(k) for k in RECEIPT_KEYS if item.get(k)}
        saved = entry.get("saved_as")
        if saved:
            candidate = os.path.realpath(os.path.join(staging, str(saved)))
            inside = (staging_real and
                      (candidate == staging_real or
                       candidate.startswith(staging_real + os.sep)))
            if not inside or not os.path.isfile(candidate):
                entry.pop("saved_as", None)
            else:
                entry["saved_as"] = candidate
        url = str(entry.get("url") or "")
        if not entry.get("saved_as") and not url.lower().startswith(("http://", "https://")):
            continue
        kind = str(entry.get("kind") or "").lower()
        entry["kind"] = KIND_DATASET if kind.startswith("data") else KIND_SUPPLEMENT
        entries.append(entry)
    return entries
