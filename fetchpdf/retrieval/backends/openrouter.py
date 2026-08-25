"""The retrieval agent on OpenRouter, with two tools we implement ourselves.

The security story here is the inverse of the CLI backend's. There is no
general-purpose agent harness to lock down, so there is nothing to deny: the
model can call exactly `fetch_url` and `download`, both defined below, and
`download` writes inside the staging directory by construction. It cannot read
the operator's disk because nothing here offers to, and it cannot reach a
connected service because none is wired in.

That makes this the backend that really does download, which is what the
design asked for. The CLI backend cannot, for reasons measured and recorded in
`backends/__init__.py`.

The loop is ours too, so the turn cap is an integer rather than a CLI flag
whose semantics have to be re-verified every release.

Optional by contract: no `OPENROUTER_API_KEY`, no backend. It says so once and
the run continues on layer 1 alone.
"""

import hashlib
import json
import os
import re
from typing import List, Optional

from ..._env import OPENROUTER_API_KEY
from ..llm_adjudicate import _warn_once, parse_json_body
from . import normalize_receipt

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: A slug, not a family name: OpenRouter needs "anthropic/claude-haiku-4.5",
#: and `--llm-model haiku` is the CLI backend's vocabulary. Translated rather
#: than rejected, so one --llm-model value works against either backend.
_MODEL_ALIASES = {
    "haiku": "anthropic/claude-haiku-4.5",
    "sonnet": "anthropic/claude-sonnet-5",
    "opus": "anthropic/claude-opus-5",
}
DEFAULT_MODEL = _MODEL_ALIASES["haiku"]

#: How many model turns the loop will take before it stops asking. Ours, so it
#: is enforced by the `for` below rather than by a flag.
MAX_ITERATIONS = 12

#: Per-call HTTP timeout. Inference is slow; a metadata lookup's 30s is not
#: the right number for a model call.
TIMEOUT_SECONDS = 180

#: What one `fetch_url` returns to the model. Enough of a landing page to find
#: the file list on it, bounded so a large HTML page cannot blow the context
#: window and the bill with it.
FETCH_CHARS = 20000

#: Per-file ceiling inside `download`. The real budget is enforced again on
#: ingest by `_take_file`; this one stops a runaway transfer before it costs
#: anything, and is deliberately the same order as the supplement cap.
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch a web page or API response as text. Use this to open a "
                "repository landing page and find the actual file listing."),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download",
            "description": (
                "Download one file into the working directory. Use only for a "
                "direct file URL, never for an HTML landing page."),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["url", "filename"],
            },
        },
    },
]


class OpenRouterBackend:
    """Downloads directly, through two tools this module implements."""

    name = "openrouter"
    downloads = True

    def available(self):
        if not OPENROUTER_API_KEY:
            return False, ("OPENROUTER_API_KEY is not set; add it to "
                           ".env.local or use --llm-backend claude-cli")
        return True, "openrouter"

    def run(self, brief: str, staging: str, model: str = None,
            log=None, http=None) -> Optional[List[dict]]:
        ok, detail = self.available()
        if not ok:
            _warn_once(detail, log)
            return None
        if http is None:
            _warn_once("openrouter backend needs an HTTP client", log)
            return None

        slug = _MODEL_ALIASES.get((model or "").strip().lower(),
                                  model or DEFAULT_MODEL)
        messages = [{"role": "user", "content": brief}]
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}

        for _iteration in range(MAX_ITERATIONS):
            response = http.post_json(
                ENDPOINT,
                {"model": slug, "messages": messages, "tools": TOOLS},
                headers=headers, timeout=TIMEOUT_SECONDS)
            if response.status != 200:
                _warn_once(
                    f"OpenRouter returned {response.status}; "
                    f"falling back to the deterministic rules", log)
                return None
            body = response.json_or(None)
            choice = _first_choice(body)
            if choice is None:
                _warn_once("OpenRouter returned no choices", log)
                return None

            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls:
                return normalize_receipt(
                    parse_json_body(message.get("content") or "", expect=list),
                    staging)

            messages.append(message)
            for call in calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": _run_tool(call, staging, http, log),
                })

        # Out of turns. Whatever it downloaded is on disk but unattributed, and
        # inventing provenance for it would be worse than saying so.
        if log:
            log(f"    llm_agent: stopped after {MAX_ITERATIONS} turns "
                f"without a final answer")
        return []


def _first_choice(body):
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    return choices[0] if isinstance(choices[0], dict) else None


def _run_tool(call, staging: str, http, log) -> str:
    """Dispatch one tool call. Always returns a string for the model to read."""
    function = call.get("function") or {}
    name = function.get("name")
    try:
        args = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return "error: arguments were not valid JSON"
    if not isinstance(args, dict):
        return "error: arguments were not an object"

    if name == "fetch_url":
        return _tool_fetch(args.get("url"), http)
    if name == "download":
        return _tool_download(args.get("url"), args.get("filename"),
                              staging, http, log)
    return f"error: no tool named {name!r}"


def _tool_fetch(url, http) -> str:
    if not _is_http_url(url):
        return "error: url must be http(s)"
    response = http.get(url, polite=False, stream_limit=FETCH_CHARS * 4)
    if response.status != 200:
        return f"error: HTTP {response.status}"
    return response.text[:FETCH_CHARS]


def _tool_download(url, filename, staging: str, http, log) -> str:
    """Save one file into staging. The path is built here, never by the model.

    `filename` is reduced to a bare, sanitized basename before it is joined, so
    "../../.ssh/id_rsa" becomes "id_rsa" inside staging rather than a path
    anywhere else. The model contributes a name, not a location.
    """
    if not _is_http_url(url):
        return "error: url must be http(s)"
    safe = _safe_basename(filename) or _safe_basename(url.rsplit("/", 1)[-1])
    if not safe:
        return "error: could not derive a filename"
    destination = os.path.join(staging, safe)

    result = http.download(url, destination, MAX_DOWNLOAD_BYTES, polite=False)
    if not result.ok:
        return f"error: {result.outcome} ({result.detail or 'no detail'})"
    if log:
        log(f"    llm_agent: staged {safe} ({result.bytes_written} bytes)")
    return json.dumps({
        "saved_as": safe,
        "bytes": result.bytes_written,
        "sha256": result.sha256 or _sha256(destination),
    })


def _is_http_url(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _safe_basename(name) -> str:
    if not isinstance(name, str):
        return ""
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = _UNSAFE_NAME.sub("_", base).strip("._")
    return base[:120]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
