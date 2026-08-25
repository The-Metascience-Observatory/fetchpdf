"""The two sandboxes the retrieval agent runs in.

These pin claims that were MEASURED against a real CLI and a real endpoint and
that nothing in an offline suite can re-measure. That is the point: the
measurements are recorded in `backends/__init__.py`, and these tests stop the
configuration those measurements justified from drifting away silently.

The one that matters most is the argv. Against Claude Code 2.1.240,
`--allowed-tools` grants permission without restricting the tool set -- a
subprocess given `--allowed-tools "Bash(echo:*)"` read a file outside its
working directory -- and a default subprocess inherits every MCP server the
operator has connected, which on the machine this was written on included
Gmail, Google Drive and a brokerage account. Only `--disallowed-tools` and
`--strict-mcp-config` actually removed anything. If someone deletes one of
those flags because it looks redundant, this file is what says otherwise.
"""

import json

import pytest

from fetchpdf.retrieval.backends import get_backend, normalize_receipt
from fetchpdf.retrieval.backends import claude_cli as cli_backend
from fetchpdf.retrieval.backends import openrouter as or_backend


# --- resolution ------------------------------------------------------------

def test_the_named_backends_resolve_and_an_unknown_one_is_none():
    assert get_backend("claude-cli").name == "claude-cli"
    assert get_backend("openrouter").name == "openrouter"
    assert get_backend(None).name == "claude-cli"      # the default
    assert get_backend("gpt-in-a-box") is None


def test_only_one_backend_claims_to_download():
    """The difference is invisible in the output directory, not in the code."""
    assert get_backend("claude-cli").downloads is False
    assert get_backend("openrouter").downloads is True


# --- the CLI sandbox -------------------------------------------------------

def _argv(monkeypatch, tmp_path):
    """The argv a real run would use, captured without running anything."""
    seen = {}

    def _capture(prompt, cli_path, model=None, timeout=None, allowed_tools="",
                 max_turns=1, extra_argv=(), cwd=None, log=None):
        from fetchpdf.retrieval.llm_adjudicate import _build_command
        seen["argv"] = _build_command(cli_path, model, allowed_tools,
                                      max_turns, extra_argv)
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        seen["prompt"] = prompt
        return None

    monkeypatch.setattr(cli_backend, "find_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cli_backend, "run_cli", _capture)
    get_backend("claude-cli").run("BRIEF", str(tmp_path), model="sonnet")
    return seen


def test_the_cli_agent_gets_no_mcp_servers(monkeypatch, tmp_path):
    """Without this it inherits the operator's Gmail, Drive and brokerage."""
    argv = _argv(monkeypatch, tmp_path)["argv"]
    assert "--strict-mcp-config" in argv
    config = argv[argv.index("--mcp-config") + 1]
    # The file is written inside a TemporaryDirectory that is gone by now;
    # what matters is that one was passed at all.
    assert config.endswith("no_mcp.json")


def test_the_cli_agent_gets_no_settings_sources(monkeypatch, tmp_path):
    argv = _argv(monkeypatch, tmp_path)["argv"]
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_the_cli_agent_is_denied_every_file_and_shell_tool(monkeypatch, tmp_path):
    """A denylist, because --allowed-tools was measured not to restrict."""
    argv = _argv(monkeypatch, tmp_path)["argv"]
    denied = set(argv[argv.index("--disallowed-tools") + 1].split(","))
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep",
                 "Agent", "Skill", "Workflow", "SendMessage",
                 "ReadMcpResourceTool"):
        assert tool in denied, tool
    assert argv[argv.index("--allowed-tools") + 1] == "WebFetch"


def test_the_cli_agent_never_sees_the_corpus(monkeypatch, tmp_path):
    """cwd is staging, and no paper directory is passed anywhere in argv."""
    seen = _argv(monkeypatch, tmp_path)
    assert seen["cwd"] == str(tmp_path)
    assert not any("--add-dir" == part for part in seen["argv"])


def test_the_model_choice_reaches_the_cli(monkeypatch, tmp_path):
    argv = _argv(monkeypatch, tmp_path)["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_the_cli_agent_gets_a_turn_cap_and_a_wall_clock(monkeypatch, tmp_path):
    """--max-turns IS enforced, and exceeding it loses the whole answer."""
    seen = _argv(monkeypatch, tmp_path)
    assert int(seen["argv"][seen["argv"].index("--max-turns") + 1]) > 1
    assert seen["timeout"] == cli_backend.TIMEOUT_SECONDS


def test_a_missing_cli_is_a_reason_not_an_exception(monkeypatch):
    monkeypatch.setattr(cli_backend, "find_cli", lambda: None)
    ok, detail = get_backend("claude-cli").available()
    assert ok is False and "not found" in detail


# --- the OpenRouter sandbox ------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def json_or(self, default):
        return self._payload


class FakeHttp:
    """Replays a scripted sequence of OpenRouter responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.gets = []
        self.downloads = []

    def post_json(self, url, payload, headers=None, timeout=None):
        self.posts.append((url, payload, headers))
        return self.responses.pop(0) if self.responses else FakeResponse(None, 500)

    def get(self, url, **kwargs):
        self.gets.append(url)
        raise AssertionError("not exercised here")

    def download(self, url, dest, max_bytes, **kwargs):
        raise AssertionError("not exercised here")


def _answer(text):
    return FakeResponse({"choices": [{"message": {"content": text}}]})


def _tool_call(name, arguments):
    return FakeResponse({"choices": [{"message": {
        "role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": name,
                                      "arguments": json.dumps(arguments)}}]}}]})


def test_openrouter_without_a_key_is_a_reason_not_an_exception(monkeypatch):
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", None)
    ok, detail = get_backend("openrouter").available()
    assert ok is False and "OPENROUTER_API_KEY" in detail


def test_a_plain_answer_becomes_the_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "k")
    http = FakeHttp([_answer('[{"url": "https://x.example/a.csv", '
                             '"kind": "dataset"}]')])
    receipt = get_backend("openrouter").run("BRIEF", str(tmp_path), http=http)
    assert receipt == [{"url": "https://x.example/a.csv", "kind": "dataset"}]


def test_the_model_name_is_translated_to_a_slug(monkeypatch, tmp_path):
    """One --llm-model value has to work against either backend."""
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "k")
    http = FakeHttp([_answer("[]")])
    get_backend("openrouter").run("B", str(tmp_path), model="sonnet", http=http)
    assert http.posts[0][1]["model"] == "anthropic/claude-sonnet-5"


def test_the_key_travels_as_a_header_never_in_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "sk-secret")
    http = FakeHttp([_answer("[]")])
    get_backend("openrouter").run("B", str(tmp_path), http=http)
    url, payload, headers = http.posts[0]
    assert "sk-secret" not in url
    assert "sk-secret" not in json.dumps(payload)
    assert headers["Authorization"] == "Bearer sk-secret"


def test_the_tool_loop_stops_at_its_own_ceiling(monkeypatch, tmp_path):
    """Ours, so it is a counter rather than a flag to re-verify each release."""
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(or_backend, "MAX_ITERATIONS", 3)
    http = FakeHttp([_tool_call("fetch_url", {"url": "https://x.example/"})] * 3)
    monkeypatch.setattr(or_backend, "_run_tool",
                        lambda *a, **k: "some page text")
    assert get_backend("openrouter").run("B", str(tmp_path), http=http) == []
    assert len(http.posts) == 3


def test_an_http_error_falls_back_to_the_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "k")
    http = FakeHttp([FakeResponse(None, status=402)])
    assert get_backend("openrouter").run("B", str(tmp_path), http=http) is None


def test_prose_instead_of_json_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(or_backend, "OPENROUTER_API_KEY", "k")
    http = FakeHttp([_answer("I could not find any deposits for this paper.")])
    assert get_backend("openrouter").run("B", str(tmp_path), http=http) == []


# --- the tools, which are the actual security boundary ---------------------

def test_download_builds_its_own_path_and_the_model_only_names_the_file(tmp_path):
    """"../../.ssh/id_rsa" must become "id_rsa" INSIDE staging, or nothing."""
    for proposed, expected in (("../../.ssh/id_rsa", "id_rsa"),
                               ("/etc/passwd", "passwd"),
                               ("data.xlsx", "data.xlsx"),
                               ("a/b/c.csv", "c.csv")):
        assert or_backend._safe_basename(proposed) == expected


def test_a_filename_that_sanitizes_to_nothing_is_refused(tmp_path):
    class Http:
        def download(self, *a, **k):
            raise AssertionError("should never be reached")

    result = or_backend._tool_download("https://x.example/", "..", str(tmp_path),
                                       Http(), None)
    assert result.startswith("error:")


def test_the_tools_refuse_a_non_http_url(tmp_path):
    class Http:
        def get(self, *a, **k):
            raise AssertionError("should never be reached")

        def download(self, *a, **k):
            raise AssertionError("should never be reached")

    assert or_backend._tool_fetch("file:///etc/passwd", Http()).startswith("error:")
    assert or_backend._tool_download("file:///etc/passwd", "x", str(tmp_path),
                                     Http(), None).startswith("error:")


def test_an_unknown_tool_name_is_answered_not_raised(tmp_path):
    call = {"function": {"name": "rm_rf", "arguments": "{}"}}
    assert or_backend._run_tool(call, str(tmp_path), None, None).startswith("error:")


def test_malformed_tool_arguments_are_answered_not_raised(tmp_path):
    call = {"function": {"name": "fetch_url", "arguments": "{not json"}}
    assert or_backend._run_tool(call, str(tmp_path), None, None).startswith("error:")


# --- a repository tarball is not a zip -------------------------------------

def test_a_github_repo_is_kept_whole_not_handed_to_the_zip_expander():
    """`is_archive` means "expand with zipfile", and codeload serves gzip.

    Every GitHub repository fulltext_scan ever found was refused
    `not-an-archive` for this reason -- zero `fulltext_scan:github` files exist
    across 1,934 corpus manifests, while the URL itself returns 6.5 MB of valid
    gzip. Keeping it whole is also the standing policy for replication
    packages.
    """
    from fetchpdf.retrieval.supplement_graph import _github_tarball

    class Ctx:
        class http:
            @staticmethod
            def get(*a, **k):
                raise RuntimeError("offline")
        scratch = {}

        @staticmethod
        def log(*a, **k):
            pass

    entry = _github_tarball("owner/repo", Ctx())[0]
    assert entry.is_archive is False
    assert entry.url.endswith("/tar.gz/refs/heads/main")
    assert entry.name == "owner-repo-main.tar.gz"
