"""Did-you-mean hints for mistyped command-line flags (fetchpdf/_cli.py)."""

import pytest

from fetchpdf._cli import ArgumentParser, suggest


def _parser():
    p = ArgumentParser(prog="fetchpdf")
    p.add_argument("--fetch-everything", "--pull-everything", action="store_true")
    p.add_argument("--pull-figures", action="store_true")
    p.add_argument("--pull-supplementary", action="store_true")
    p.add_argument("--refresh-supplementary", action="store_true")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    s = sub.add_parser("setup")
    s.add_argument("--institution")
    w = sub.add_parser("window")
    w.add_argument("--force-login", action="store_true")
    return p


def _error(capsys, argv):
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize("typo, expected", [
    ("--fetch-everyting", "--fetch-everything"),
    ("--pull-everthing", "--pull-everything"),
    ("--pull_supplementary", "--pull-supplementary"),
    ("--Verbose", "--verbose"),
    ("-verbose", "--verbose"),
    ("--everything", "--pull-everything"),
])
def test_suggest_finds_the_intended_flag(typo, expected):
    opts = ["--fetch-everything", "--pull-everything", "--pull-figures",
            "--pull-supplementary", "--refresh-supplementary", "--verbose"]
    assert suggest(typo, opts)[0] == expected


def test_clear_typo_suggests_only_the_close_flag():
    assert suggest("--pull-everthing", ["--pull-everything", "--pull-figures"]) == ["--pull-everything"]


def test_unrelated_flag_gets_no_suggestion():
    assert suggest("--frobnicate", ["--fetch-everything", "--verbose"]) == []


def test_error_names_one_spelling_per_option(capsys):
    err = _error(capsys, ["--fetch-everyhting"])
    assert "unrecognized arguments: --fetch-everyhting" in err
    assert "did you mean --fetch-everything?" in err


def test_value_after_equals_is_ignored(capsys):
    assert "did you mean --verbose?" in _error(capsys, ["--verbse=1"])


def test_subcommand_scope_excludes_sibling_flags(capsys):
    err = _error(capsys, ["setup", "--force-logn"])
    assert "--force-login" not in err
    assert "did you mean --institution?" in _error(capsys, ["setup", "--institutoin", "x"])


def test_mistyped_subcommand(capsys):
    assert "did you mean setup?" in _error(capsys, ["setpu"])


def test_valid_args_still_parse():
    args = _parser().parse_args(["--pull-everything", "setup", "--institution", "x"])
    assert args.fetch_everything and args.institution == "x"
