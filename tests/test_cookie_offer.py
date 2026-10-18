"""The end-of-run offer to import library cookies. No browser, no network."""
import csv
import json
from pathlib import Path

import pytest

from fetchpdf import fetchpdf as fp
from fetchpdf import get_cookies as gc
from fetchpdf.retrieval import institutional as inst

WILEY = "10.1002/abc.123"
SAGE = "10.1177/0956797612345678"
UNCOVERED = "10.1038/nature12373"
ROYAL = "10.1098/rspb.2020.0001"   # covered, but no test article to sign in on


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inst, "ACCESS_CONFIG", tmp_path / "access.json")
    monkeypatch.setattr(gc, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(gc, "ACCESS_CONFIG", tmp_path / "access.json")
    inst.reset_cookie_source()
    yield
    inst.reset_cookie_source()


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(fp, "_stdio_is_interactive", lambda: True)


def _answer(monkeypatch, reply):
    asked = []

    def fake_input(prompt):
        asked.append(prompt)
        return reply
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _never_retry(dois):
    raise AssertionError("retried")


def test_covered_by_site_groups_by_publisher_and_drops_uncovered():
    got = inst.covered_by_site([WILEY, UNCOVERED, SAGE, "10.1111/x.1", "", "12345678"])
    assert got == {"wiley.com": [WILEY, "10.1111/x.1"], "sagepub.com": [SAGE]}


def test_no_covered_failures_says_nothing(capsys, interactive, monkeypatch):
    asked = _answer(monkeypatch, "y")
    assert fp._offer_cookie_import([UNCOVERED], _never_retry) == []
    assert not asked and capsys.readouterr().out == ""


def test_non_interactive_prints_the_command_and_does_not_ask(capsys, monkeypatch):
    monkeypatch.setattr(fp, "_stdio_is_interactive", lambda: False)
    asked = _answer(monkeypatch, "y")
    assert fp._offer_cookie_import([WILEY, UNCOVERED], _never_retry) == []
    out = capsys.readouterr().out
    assert not asked
    assert "Wiley 1" in out and "--cookies-only" in out
    assert "fetchpdf cookies setup --publishers wiley`" in out


@pytest.mark.parametrize("choice", ["no_cookies", "explicit_file"])
def test_user_managed_cookies_are_left_alone(choice, capsys, interactive, monkeypatch, tmp_path):
    if choice == "no_cookies":
        inst.disable_cookies()
    else:
        inst.use_cookies_file(tmp_path / "c.txt")
    asked = _answer(monkeypatch, "y")
    assert fp._offer_cookie_import([WILEY], _never_retry) == []
    assert not asked and capsys.readouterr().out == ""


def test_declining_does_not_import_or_retry(interactive, monkeypatch):
    asked = _answer(monkeypatch, "n")
    monkeypatch.setattr(gc, "main", lambda argv: pytest.fail("setup ran"))
    assert fp._offer_cookie_import([WILEY], _never_retry) == []
    assert asked and "Sign in to Wiley through your library" in asked[0]


def test_yes_runs_setup_for_those_publishers_then_retries_only_covered_dois_cookies_only(
        interactive, monkeypatch, tmp_path):
    asked = _answer(monkeypatch, "y")
    cookie_file = tmp_path / "cookies.json"

    def fake_setup(argv):
        assert argv == ["setup", "--publishers", "sage,wiley"]
        cookie_file.write_text("[]")
        inst.ACCESS_CONFIG.write_text(json.dumps(
            {"browser": "chrome", "cookie_file": str(cookie_file), "enabled": True}))
        return 0
    monkeypatch.setattr(gc, "main", fake_setup)
    monkeypatch.setattr(gc, "export_from_browser", lambda *a, **k: {})
    monkeypatch.setattr(fp, "_LAST_RESORTS_ENABLED", True)

    seen = {}

    def retry(dois):
        seen["dois"] = dois
        seen["cookies_only"] = inst.cookies_only()
        seen["last_resorts"] = fp._LAST_RESORTS_ENABLED
        return [SAGE]

    assert fp._offer_cookie_import([WILEY, UNCOVERED, SAGE, ROYAL], retry) == [SAGE]
    assert "Sign in to SAGE and Wiley" in asked[0]
    assert sorted(seen["dois"]) == sorted([WILEY, SAGE])
    assert seen["cookies_only"] is True and seen["last_resorts"] is False
    # Restored for whatever the process does next.
    assert inst.cookies_only() is False and fp._LAST_RESORTS_ENABLED is True


def test_refresh_with_every_session_working_does_not_retry(interactive, monkeypatch, capsys):
    inst.ACCESS_CONFIG.write_text(json.dumps({"browser": "chrome", "enabled": True}))
    asked = _answer(monkeypatch, "y")
    asked_for = []

    def fake_refresh(cfg, publishers):
        asked_for.append(publishers)
        return {"wiley": "ok"}, []
    monkeypatch.setattr(gc, "refresh_sessions", fake_refresh)
    assert fp._offer_cookie_import([WILEY, UNCOVERED], _never_retry) == []
    assert asked_for == [["wiley"]] and "Sign in to Wiley" in asked[0]
    assert "does not subscribe" in capsys.readouterr().out


def test_a_publisher_with_no_sign_in_page_is_not_offered(capsys, interactive, monkeypatch):
    asked = _answer(monkeypatch, "y")
    assert fp._offer_cookie_import([ROYAL], _never_retry) == []
    assert not asked and capsys.readouterr().out == ""


def test_refresh_checks_only_the_named_publishers(monkeypatch, tmp_path):
    checked = []
    monkeypatch.setattr(gc, "export_from_browser", lambda *a, **k: {})
    monkeypatch.setattr(gc, "check_access",
                        lambda f, names, **k: checked.append(names) or {n: "ok" for n in names})
    cfg = {"browser": "chrome", "cookie_file": str(tmp_path / "c.json"), "name": "lib"}
    status, lapsed = gc.refresh_sessions(cfg, ["sage", "wiley"])
    assert checked == [["wiley", "sage"]] and lapsed == []


def test_retry_batch_drops_recovered_rows_and_adds_none(monkeypatch, tmp_path):
    csv_path = tmp_path / "failed_dois.csv"
    rows = [["timestamp", "doi", "category", "detail"],
            ["t", WILEY, "all_sources_failed", ""],
            ["t", SAGE, "all_sources_failed", ""],
            ["t", UNCOVERED, "all_sources_failed", ""]]
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    def fake_batch(dois, **kw):
        assert kw["start_offset"] == 0 and kw["create_missing_report"] is False
        with open(csv_path, "a", newline="") as f:   # what a real retry would append
            csv.writer(f).writerow(["t2", SAGE, "all_sources_failed", ""])
        return [(WILEY, True, "/x.pdf"), (SAGE, False, None)]
    monkeypatch.setattr(fp, "batch_fetch_pdfs", fake_batch)

    got = fp._retry_batch([WILEY, SAGE], {"output_dir": str(tmp_path), "start_offset": 7})
    assert got == [WILEY]
    with open(csv_path, newline="") as f:
        after = list(csv.reader(f))
    assert after == [rows[0], rows[2], rows[3]]


def test_there_is_no_separate_get_cookies_command():
    # Text, not tomllib: the package still supports Python 3.10.
    scripts = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text() \
        .split("[project.scripts]")[1]
    assert not any(line.startswith("get-cookies") for line in scripts.splitlines())
