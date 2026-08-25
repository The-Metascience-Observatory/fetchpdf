"""The retrieval agent on the local Claude Code CLI, locked down to WebFetch.

Reuses `llm_adjudicate`'s subprocess plumbing -- `find_cli`, `run_cli`,
`unwrap_result`, `_warn_once` -- so there is one place that knows about the
Windows .cmd shim, the stdin feed and the JSON envelope.

WHY IT DOES NOT DOWNLOAD. See `backends/__init__.py` for the measurements: in
this CLI, `--allowed-tools` grants and does not restrict, and a default
subprocess inherits the operator's MCP servers. The only configuration that
verifiably contained the agent removes every file and shell tool -- which also
removes its ability to save anything. That is a fair trade. Navigating is the
part that needs a model; transferring bytes under a cap with a hash is the part
fetchpdf already does better than a shell command would.

The lockdown is a DENYLIST, and denylists rot: a tool added to Claude Code in a
later release is allowed here until someone adds it below. The two structural
guards are what this actually rests on -- no MCP config, no settings sources --
plus the fact that a WebFetch-only agent has nothing to write to.
"""

import json
import os
import tempfile
from typing import List, Optional

from ..llm_adjudicate import (
    DEFAULT_MODEL,
    _warn_once,
    find_cli,
    run_cli,
    unwrap_result,
)
from . import normalize_receipt

#: The one tool it needs, and every built-in that could read the operator's
#: disk, spawn another agent, or reach a connected service. Verified against
#: Claude Code 2.1.240 by asking a locked-down subprocess to list its own
#: tools: it answered "WebFetch" and nothing else.
ALLOWED_TOOLS = "WebFetch"
DISALLOWED_TOOLS = ",".join((
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "Agent", "Skill", "Workflow", "SendMessage", "ListAgents",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "WebSearch", "Monitor", "ScheduleWakeup", "ReportFindings", "ToolSearch",
    "CronCreate", "CronDelete", "CronList", "DesignSync",
    "EnterWorktree", "ExitWorktree", "PushNotification", "RemoteTrigger",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
))

#: Enough turns to open a landing page, follow it to a file listing, and
#: answer. Measured: a single OSF node lookup takes 2. The cap IS enforced --
#: a run that exceeds it comes back is_error with no result at all, which is
#: why this is generous rather than tight.
MAX_TURNS = 12

#: Wall clock for the whole conversation. The adjudicator's 60s is sized for
#: one classification; this one makes several network round trips.
TIMEOUT_SECONDS = 300


class ClaudeCliBackend:
    """Navigates with WebFetch and reports URLs. Never writes a file."""

    name = "claude-cli"
    downloads = False

    def available(self):
        """`(ok, detail)`. Absence is a reason, never an exception."""
        if find_cli() is None:
            return False, ("Claude Code CLI not found on PATH; install it or "
                           "use --llm-backend openrouter")
        return True, "claude"

    def run(self, brief: str, staging: str, model: str = DEFAULT_MODEL,
            log=None) -> Optional[List[dict]]:
        cli_path = find_cli()
        if cli_path is None:
            ok, detail = self.available()
            _warn_once(detail, log)
            return None

        with tempfile.TemporaryDirectory() as empty_config_dir:
            config = os.path.join(empty_config_dir, "no_mcp.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"mcpServers": {}}, handle)

            stdout = run_cli(
                brief,
                cli_path,
                model=model,
                timeout=TIMEOUT_SECONDS,
                allowed_tools=ALLOWED_TOOLS,
                max_turns=MAX_TURNS,
                extra_argv=[
                    # Structural, not a denylist: without these the subprocess
                    # inherits every MCP server the operator has connected.
                    "--strict-mcp-config", "--mcp-config", config,
                    "--setting-sources", "",
                    "--disallowed-tools", DISALLOWED_TOOLS,
                    # Nothing can prompt in print mode, so an un-preapproved
                    # WebFetch would simply be denied and the agent would
                    # report nothing. The tool set above is the boundary.
                    "--permission-mode", "bypassPermissions",
                ],
                cwd=staging,
                log=log,
            )

        if stdout is None:
            return None
        return normalize_receipt(unwrap_result(stdout, expect=list), staging)
