"""The setup wizard's decisions. No browser, no network."""
import json

import pytest

from fetchpdf import get_cookies as gc
from fetchpdf import fetchpdf as fp
from fetchpdf.retrieval import institutional as inst


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inst, "ACCESS_CONFIG", tmp_path / "access.json")
    monkeypatch.setattr(gc, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(gc, "ACCESS_CONFIG", tmp_path / "access.json")
    inst.reset_cookie_source()


def test_every_probe_is_a_covered_publisher():
    for doi in gc.PROBES.values():
        assert inst.publisher_pdf_urls(doi) and inst.publisher_article_url(doi)


def test_signin_goes_through_the_library_redirector():
    url = gc.signin_url({"redirector": gc.INSTITUTIONS["harvard"]["redirector"]}, "wiley")
    assert url.startswith("https://go.openathens.net/redirector/library.harvard.edu?url=")
    assert "onlinelibrary.wiley.com" in url


def test_without_a_redirector_the_article_opens_directly():
    assert gc.signin_url({"redirector": None}, "sage").startswith("https://journals.sagepub.com/")


def test_a_publisher_with_no_cookies_is_not_signed_in(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"cookies": [
        {"name": "s", "value": "v", "domain": ".wiley.com", "path": "/"}]}), encoding="utf-8")
    assert gc.check_access(f, ["sage"]) == {"sage": "not signed in"}


def _run_setup(monkeypatch, status):
    monkeypatch.setattr(gc, "detect_browsers", lambda: ["chrome"])
    monkeypatch.setattr(gc, "_signin_round", lambda cfg, pubs: dict(status))
    monkeypatch.setattr(gc, "_ask", lambda prompt, default="": default)
    return gc.main(["setup", "--institution", "harvard", "--browser", "chrome"])


def test_setup_turns_access_on_when_a_publisher_serves_a_pdf(monkeypatch):
    assert _run_setup(monkeypatch, {"wiley": "ok", "sage": "no access"}) == 0
    cfg = json.loads(inst.ACCESS_CONFIG.read_text())
    assert cfg["enabled"] and cfg["browser"] == "chrome" and cfg["key"] == "harvard"
    assert oct(inst.ACCESS_CONFIG.stat().st_mode & 0o777) == "0o600"


def test_setup_leaves_access_off_when_nothing_works(monkeypatch):
    assert _run_setup(monkeypatch, {"wiley": "not signed in"}) == 1
    assert not inst.ACCESS_CONFIG.exists()


def test_disable_turns_it_off(monkeypatch):
    inst.ACCESS_CONFIG.write_text(json.dumps({"browser": "chrome", "enabled": True}))
    assert fp.main(["cookies", "disable"]) == 0
    assert inst.load_access_config() is None


@pytest.mark.parametrize("argv,code,text", [
    (["--help"], 0, "setup,check,refresh,disable,export,window"),
    (["setup", "--help"], 0, "--institution"),
    (["export"], 2, "--browser"),
    (["check", "--json"], 2, "unrecognized arguments: --json"),
])
def test_cookie_cli_help_and_errors(argv, code, text, capsys):
    with pytest.raises(SystemExit) as exc:
        fp.main(["cookies", *argv])
    assert exc.value.code == code
    captured = capsys.readouterr()
    output = captured.out if code == 0 else captured.err
    assert "usage: fetchpdf cookies" in output
    assert text in output


def test_cookie_cli_without_action_preserves_exit_status(capsys):
    assert fp.main(["cookies"]) == 2
    assert "fetchpdf cookies" in capsys.readouterr().out


def test_cookie_cli_reads_process_arguments(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["fetchpdf", "cookies", "check"])
    assert fp.main() == 2
    assert "fetchpdf cookies setup" in capsys.readouterr().out


def test_an_unreadable_browser_never_overwrites_the_last_export(monkeypatch, tmp_path):
    """Outside the desktop session Chrome's store cannot be decrypted (2026-10-01:
    a cron-like run replaced a working export with an empty one)."""
    import sys, types
    fake = types.SimpleNamespace(chrome=lambda domain_name: (_ for _ in ()).throw(KeyError("key")))
    monkeypatch.setitem(sys.modules, "browser_cookie3", fake)
    out = tmp_path / "c.json"
    out.write_text('{"cookies": [{"name": "s", "value": "good", "domain": ".wiley.com"}]}')
    with pytest.raises(RuntimeError):
        gc.export_from_browser("chrome", out, "x", quiet=True)
    assert "good" in out.read_text()
