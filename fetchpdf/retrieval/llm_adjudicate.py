"""Ask a local Claude Code CLI to settle the candidates the rules cannot.

fulltext_scan's rules answer most cases for free and deterministically, which
is what a forensic tool wants: a run should be re-derivable. What they cannot
settle is genuinely semantic --

    "see Table 1 here with two new conceptual replication studies; OSF: <url>"

-- is that deposit this paper's, or the replications it discusses? No pattern
decides that; a reader does. So UNCERTAIN candidates are handed to a model.

Three deliberate constraints:

1.  **Only rejections and uncertainties are sent.** Recall is only ever lost in
    a refusal, so the model may only ever PROMOTE a candidate to download. A
    bad verdict then costs one extra deposit, never a silently missing one.
2.  **This is optional.** Missing CLI, non-zero exit, timeout, garbage output --
    all mean "no verdict", and the rules stand. A download run must never fail
    because a helper is absent.
3.  **The subprocess gets no tools.** `--allowed-tools ""` with `--max-turns 1`
    makes it a pure text call. Without that we would be pointing an agent with
    filesystem access at the user's machine to answer a yes/no question.

Shelling out to the CLI rather than an SDK is deliberate: no new dependency, no
API key handling, and it reuses whatever auth the user already has.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import List, Optional

from .fulltext_scan import ACCEPT, REFUSE, UNCERTAIN, Candidate

#: Default model. Haiku is the right tier for a one-sentence classification,
#: and measured at ~2.3s / $0.009 per call.
DEFAULT_MODEL = "haiku"

#: Per-call ceiling. The measured call is ~2.3s; 60s is generous enough to
#: absorb a cold start without letting a hung subprocess stall a batch.
TIMEOUT_SECONDS = 60

#: One warning per process, not per record -- a 500-record batch should not
#: emit 500 identical notices. Mirrors the CORE session kill-switch.
_WARNED = False
_WARN_LOCK = threading.Lock()

_PROMPT = """You are classifying one URL found in a scientific paper's full text.

Decide whether the URL is THIS paper's own data/code deposit, or something else
(a preregistration, a different paper's deposit, or a third-party tool/library).

URL: {url}
REPOSITORY: {repo}
SURROUNDING TEXT: {context}

Answer with JSON only, no prose, no code fence:
{{"own_deposit": true or false, "confidence": "high" or "low", "reason": "<12 words"}}
"""


def _warn_once(message: str, log=None) -> None:
    global _WARNED
    with _WARN_LOCK:
        if _WARNED:
            return
        _WARNED = True
    text = f"⚠️  {message} Falling back to rule-based classification."
    if log is not None:
        log(text)
    else:
        # Yellow, matching the CORE / news-item notices elsewhere.
        print(f"\033[93m{text}\033[0m")


def reset_warning_state() -> None:
    """Exists for tests; a batch is one process."""
    global _WARNED
    with _WARN_LOCK:
        _WARNED = False


def find_cli() -> Optional[str]:
    """Absolute path to the Claude Code CLI, or None.

    shutil.which is the correct detector on all three platforms: on Windows it
    consults PATHEXT and returns the claude.cmd/.exe shim the npm installer
    creates. Returning the FULL PATH matters -- see _build_command.
    """
    return shutil.which("claude")


def _build_command(cli_path: str, model: str, allowed_tools: str = "",
                   max_turns: int = 1, extra_argv=()):
    """argv for a JSON-output call. Tool-less and single-turn by default.

    `allowed_tools` and `max_turns` are parameters rather than something a
    caller appends, because both CLI flags are variadic: passing
    `--allowed-tools ""` and then `--allowed-tools "WebFetch"` is not a
    last-one-wins override, it is two values whose combination nobody
    intended. The retrieval agent needs different values for both, so it sets
    them here.

    Windows: subprocess.run(["claude", ...]) raises WinError 193 when the
    target is a .cmd/.bat shim, because CreateProcess cannot execute a batch
    file directly. Running it through cmd.exe is the fix. We do NOT set
    shell=True generally -- on POSIX that would route the prompt through a
    shell, and the prompt contains arbitrary article prose.
    """
    argv = [
        cli_path,
        "-p",
        "--model", model,
        "--output-format", "json",
        "--allowed-tools", allowed_tools,
        "--max-turns", str(max_turns),
    ]
    argv.extend(extra_argv)
    if os.name == "nt" and cli_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + argv
    return argv


def run_cli(prompt: str, cli_path: str, model: str = DEFAULT_MODEL,
            timeout: int = None, allowed_tools: str = "", max_turns: int = 1,
            extra_argv=(), cwd: str = None, log=None) -> Optional[str]:
    """One CLI call. Returns raw stdout, or None if anything went wrong.

    Extracted from `adjudicate_one` so the retrieval agent can reuse the parts
    that are not about verdicts: the stdin feed, the Windows shim, the
    tempdir cwd, and the four ways this degrades. Every failure returns None
    and warns once -- a helper that is absent, slow or broken must never be
    able to fail a record.
    """
    try:
        # The prompt goes over STDIN, never argv: Windows caps a command line
        # near 8191 chars and applies its own quoting rules, and article prose
        # carries quotes, newlines and non-ASCII punctuation.
        #
        # cwd defaults to a temp dir so the subprocess cannot pick up an
        # unrelated project's CLAUDE.md from the corpus directory.
        with tempfile.TemporaryDirectory() as scratch:
            completed = subprocess.run(
                _build_command(cli_path, model, allowed_tools,
                               max_turns, extra_argv),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",       # explicit: Windows defaults to cp1252,
                errors="replace",       # which mangles curly quotes/en dashes
                timeout=timeout or TIMEOUT_SECONDS,
                cwd=cwd or scratch,
            )
    except subprocess.TimeoutExpired:
        _warn_once(f"Claude CLI timed out after {timeout or TIMEOUT_SECONDS}s.", log)
        return None
    except OSError as error:
        _warn_once(f"Claude CLI could not be run ({error}).", log)
        return None

    if completed.returncode != 0:
        _warn_once(
            f"Claude CLI exited {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:120]}",
            log,
        )
        return None
    return completed.stdout


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def unwrap_result(stdout: str, expect=dict):
    """Pull the model's JSON out of the CLI's JSON envelope.

    Two layers: the CLI wraps everything in {"result": "..."} , and the model
    tends to fence its answer in ```json even when told not to. Both observed
    against the real CLI, so both are handled rather than assumed away.

    `expect` is the type the caller requires -- a verdict is one object, a
    retrieval receipt is a list of them -- and anything else returns None
    rather than a shape the caller then has to re-check.
    """
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if envelope.get("is_error"):
        return None
    result = envelope.get("result")
    if not isinstance(result, str):
        return None
    return parse_json_body(result, expect=expect)


def parse_json_body(text: str, expect=dict):
    """The model's JSON out of its prose, fences and all. None if not `expect`.

    Shared with the OpenRouter backend, which gets the same fenced-JSON habit
    without the CLI's envelope around it.
    """
    body = _FENCE.sub("", (text or "").strip())
    try:
        parsed = json.loads(body)
    except ValueError:
        # Last resort: the first {...} or [...] block in the text.
        pattern = r"\[.*\]" if expect is list else r"\{.*\}"
        match = re.search(pattern, body, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return None
    return parsed if isinstance(parsed, expect) else None


def _parse_verdict(stdout: str) -> Optional[dict]:
    """The adjudicator's one-object answer. Kept as the name its tests use."""
    return unwrap_result(stdout, expect=dict)


def adjudicate_one(candidate: Candidate, cli_path: str,
                   model: str = DEFAULT_MODEL, log=None) -> Optional[dict]:
    """One yes/no verdict, or None if anything at all went wrong."""
    prompt = _PROMPT.format(
        url=candidate.url, repo=candidate.repo,
        context=(candidate.context or "")[:600],
    )
    stdout = run_cli(prompt, cli_path, model=model, log=log)
    if stdout is None:
        return None
    return _parse_verdict(stdout)


def adjudicate(candidates: List[Candidate], model: str = DEFAULT_MODEL,
               log=None, enabled: bool = True) -> List[Candidate]:
    """Let a model promote refused/uncertain candidates it judges to be owned.

    Mutates and returns the same Candidate objects, stamping `adjudicated_by`
    so a model-influenced decision stays distinguishable from a rules-only one
    in the sidecar. Candidates the rules ACCEPTED are never sent: rule
    precision on accepts is already the strong side, so asking again can only
    downgrade a correct answer while adding cost.
    """
    if not enabled or not candidates:
        return candidates

    cli_path = find_cli()
    if not cli_path:
        _warn_once(
            "--llm-adjudicate-artifacts was requested but the Claude Code CLI "
            "was not found on PATH.", log,
        )
        return candidates

    for candidate in candidates:
        if candidate.verdict == ACCEPT:
            continue
        verdict = adjudicate_one(candidate, cli_path, model=model, log=log)
        if not verdict:
            continue                       # rules stand
        candidate.adjudicated_by = model
        reason = str(verdict.get("reason") or "")[:120]
        if verdict.get("own_deposit") is True:
            candidate.verdict = ACCEPT
            candidate.reason = f"LLM: {reason}" if reason else "LLM: own deposit"
        else:
            # Only ever confirms a non-download; never turns an accept away.
            candidate.verdict = REFUSE
            candidate.reason = f"LLM: {reason}" if reason else "LLM: not this paper's"
    return candidates
