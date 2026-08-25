"""Every `pull_for_record` call site passes arguments the function accepts.

A static check, because the dynamic one cannot fire: the single-DOI branch
wraps its supplementary pass in `except Exception`, deliberately, so that a
repository serving malformed JSON can never turn a successful download into a
failed one. That same guard swallowed a `TypeError` -- the call passed
`draft_requests=`, which `pull_for_record` has never accepted -- and printed it
as "supplementary pass failed", so **every single-DOI `--pull-supplementary`
invocation retrieved nothing** and said so in the grammar of a remote problem.

Reading the signature rather than calling it keeps this offline and free, and
catches the next one at the same place: a kwarg that does not exist is a typo
the interpreter will only ever report from inside a swallowing handler.
"""

import ast
import inspect
import os

from fetchpdf.retrieval.supplementary import pull_for_record

_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fetchpdf", "fetchpdf.py")


def _call_sites(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called == name:
                yield node


def test_every_call_site_matches_the_signature():
    accepted = set(inspect.signature(pull_for_record).parameters)
    with open(_SOURCE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    sites = list(_call_sites(tree, "pull_for_record"))
    assert sites, "no pull_for_record call sites found -- has the CLI moved?"

    for call in sites:
        passed = {kw.arg for kw in call.keywords if kw.arg is not None}
        unknown = passed - accepted
        assert not unknown, (
            f"fetchpdf.py:{call.lineno} passes {sorted(unknown)} to "
            f"pull_for_record, which does not accept it")


def test_the_batch_helper_matches_its_own_signature_too():
    """batch_fetch_pdfs DOES take draft_requests -- the asymmetry is the trap."""
    import fetchpdf.fetchpdf as cli

    accepted = set(inspect.signature(cli.batch_fetch_pdfs).parameters)
    with open(_SOURCE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    for call in _call_sites(tree, "batch_fetch_pdfs"):
        passed = {kw.arg for kw in call.keywords if kw.arg is not None}
        unknown = passed - accepted
        assert not unknown, (
            f"fetchpdf.py:{call.lineno} passes {sorted(unknown)} to "
            f"batch_fetch_pdfs, which does not accept it")
