"""Process-level contracts: exit codes and what reaches stdout."""
import os
import subprocess
import sys


def _run(args, **env):
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **env},
    )


def test_python_m_fetchpdf_propagates_the_exit_code(tmp_path):
    """`python -m fetchpdf` used to call main() and discard its return value."""
    missing = tmp_path / "missing.csv"
    result = _run(["-m", "fetchpdf", str(missing), "-o", str(tmp_path)])
    assert result.returncode != 0


def test_missing_email_warning_goes_to_stderr_not_stdout():
    """The warning fires at import time, ahead of anything a caller parses on stdout."""
    result = _run(["-c", "import fetchpdf"], EMAIL="")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "EMAIL not set" in result.stderr
