"""The Claude Code CLI shim: safety contracts and graceful degradation.

Nothing here calls the network or requires the CLI to be installed -- a test
suite that spends money or depends on a local install is not a test suite.
What is asserted instead is the shape of the call (tool-less, single-turn,
prompt over stdin) and that every failure mode leaves the rules in charge.
"""

import subprocess

import pytest

from fetchpdf.retrieval import llm_adjudicate as la
from fetchpdf.retrieval.fulltext_scan import ACCEPT, REFUSE, UNCERTAIN, Candidate


@pytest.fixture(autouse=True)
def _reset():
    la.reset_warning_state()
    yield
    la.reset_warning_state()


def _candidate(verdict=UNCERTAIN):
    return Candidate(
        repo="osf", ident="whz3b", url="https://osf.io/whz3b/",
        verdict=verdict, reason="no deposit language nearby",
        context="see Table 1 with two new conceptual replication studies; OSF: ...",
    )


def _envelope(payload, is_error=False):
    """What the CLI actually returns: a JSON envelope around the model's text."""
    import json
    return json.dumps({"is_error": is_error, "result": payload})


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


# --- graceful degradation -------------------------------------------------

def test_missing_cli_leaves_verdicts_untouched(monkeypatch, capsys):
    monkeypatch.setattr(la.shutil, "which", lambda _: None)
    candidates = [_candidate()]
    la.adjudicate(candidates, enabled=True)
    assert candidates[0].verdict == UNCERTAIN
    assert candidates[0].adjudicated_by is None
    assert "not found on PATH" in capsys.readouterr().out


def test_warning_is_printed_once_not_per_candidate(monkeypatch, capsys):
    monkeypatch.setattr(la.shutil, "which", lambda _: None)
    la.adjudicate([_candidate() for _ in range(5)], enabled=True)
    assert capsys.readouterr().out.count("Falling back") == 1


def test_disabled_flag_makes_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(la.shutil, "which", lambda _: called.append(1) or "/x/claude")
    la.adjudicate([_candidate()], enabled=False)
    assert not called


@pytest.mark.parametrize("failure", [
    _Completed(stdout="not json at all"),
    _Completed(stdout=_envelope("also not json")),
    _Completed(stdout=_envelope("{}", is_error=True)),
    _Completed(stdout="", returncode=1, stderr="boom"),
])
def test_bad_output_falls_back_to_rules(monkeypatch, failure):
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: failure)
    candidates = [_candidate()]
    la.adjudicate(candidates, enabled=True)
    assert candidates[0].verdict == UNCERTAIN


def test_timeout_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=60)

    monkeypatch.setattr(la.subprocess, "run", _boom)
    candidates = [_candidate()]
    la.adjudicate(candidates, enabled=True)
    assert candidates[0].verdict == UNCERTAIN


# --- parsing --------------------------------------------------------------

def test_fenced_json_is_parsed(monkeypatch):
    """The real CLI fences its answer even when told not to -- observed."""
    fenced = '```json\n{"own_deposit": true, "confidence": "high", "reason": "authors\' own"}\n```'
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(la.subprocess, "run",
                        lambda *a, **k: _Completed(stdout=_envelope(fenced)))
    candidates = [_candidate()]
    la.adjudicate(candidates, enabled=True)
    assert candidates[0].verdict == ACCEPT
    assert candidates[0].adjudicated_by == la.DEFAULT_MODEL
    assert "authors" in candidates[0].reason


def test_negative_verdict_confirms_refusal(monkeypatch):
    payload = '{"own_deposit": false, "confidence": "high", "reason": "third-party tool"}'
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(la.subprocess, "run",
                        lambda *a, **k: _Completed(stdout=_envelope(payload)))
    candidates = [_candidate()]
    la.adjudicate(candidates, enabled=True)
    assert candidates[0].verdict == REFUSE


def test_accepted_candidates_are_never_sent(monkeypatch):
    """Rule precision on accepts is the strong side; re-asking can only harm."""
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")
    calls = []
    monkeypatch.setattr(la.subprocess, "run",
                        lambda *a, **k: calls.append(1) or _Completed(stdout=_envelope("{}")))
    accepted = _candidate(verdict=ACCEPT)
    la.adjudicate([accepted], enabled=True)
    assert not calls
    assert accepted.verdict == ACCEPT


# --- safety and portability contracts -------------------------------------

def test_call_is_toolless_single_turn_and_uses_stdin(monkeypatch):
    """The three properties that keep this a text call, not an agent."""
    seen = {}
    monkeypatch.setattr(la.shutil, "which", lambda _: "/usr/bin/claude")

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Completed(stdout=_envelope('{"own_deposit": false, "reason": "x"}'))

    monkeypatch.setattr(la.subprocess, "run", _capture)
    la.adjudicate([_candidate()], enabled=True)

    argv = seen["argv"]
    assert "--allowed-tools" in argv and argv[argv.index("--allowed-tools") + 1] == ""
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1"
    # The prompt must travel by stdin, never as an argument.
    assert seen["kwargs"]["input"], "prompt was not passed via stdin"
    assert not any("osf.io/whz3b" in str(a) for a in argv)
    # Explicit encoding: Windows' cp1252 default mangles article punctuation.
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["timeout"] == la.TIMEOUT_SECONDS


def test_windows_cmd_shim_is_invoked_through_cmd(monkeypatch):
    """subprocess.run(['x.cmd']) raises WinError 193; cmd /c is the fix."""
    monkeypatch.setattr(la.os, "name", "nt")
    argv = la._build_command(r"C:\Users\dan\AppData\npm\claude.cmd", "haiku")
    assert argv[:2] == ["cmd", "/c"]
    assert argv[2].endswith("claude.cmd")


def test_posix_invokes_the_binary_directly(monkeypatch):
    monkeypatch.setattr(la.os, "name", "posix")
    argv = la._build_command("/usr/bin/claude", "haiku")
    assert argv[0] == "/usr/bin/claude"
    assert "cmd" not in argv[:2]


def test_windows_exe_needs_no_shell(monkeypatch):
    """Only .cmd/.bat need cmd.exe; a real .exe is directly executable."""
    monkeypatch.setattr(la.os, "name", "nt")
    argv = la._build_command(r"C:\Program Files\claude.exe", "haiku")
    assert argv[0].endswith("claude.exe")
    assert argv[:2] != ["cmd", "/c"]
