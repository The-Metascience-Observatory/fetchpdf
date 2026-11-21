"""An argparse parser that answers a mistyped flag with what was probably meant.

    $ fetchpdf 10.1038/nature12373 --fetch-everyting
    fetchpdf: error: unrecognized arguments: --fetch-everyting
      --fetch-everyting: did you mean --fetch-everything?

Stock argparse only says the argument is unrecognized. The suggestion is
drawn from the long options of the parsers that actually took part in the
parse -- the top-level one and, for `fetchpdf cookies setup ...`, the chosen
subcommand's -- so it never names a flag from a sibling subcommand that
would be rejected in turn. A mistyped subcommand or choice value gets the
same treatment ("invalid choice: 'setpu'" -> setup).
"""

from __future__ import annotations

import argparse
import difflib
import re
from typing import Iterable, List, Sequence

__all__ = ["ArgumentParser", "suggest"]

# Parsers whose parse_known_args ran during the current parse_args call, in
# order: the top-level parser, then each subcommand parser it dispatched to.
# Module-level because a subparser has no link back to its parent; the CLI
# parses once per process, on one thread.
_active: List["ArgumentParser"] = []


def _normalize(token: str) -> str:
    """`--Fetch_Everything=yes` and `-fetch-everything` both -> `--fetch-everything`."""
    flag = token.split("=", 1)[0].lower().replace("_", "-")
    if not flag.startswith("--") and len(flag) > 2:
        flag = "-" + flag  # a long name typed with one dash
    return flag


def suggest(token: str, options: Iterable[str], n: int = 3) -> List[str]:
    """Up to `n` long options close to `token`, best first.

    String similarity first (typos, transpositions, a swapped verb such as
    --pull-/--fetch-); failing that, options sharing a whole word with it,
    so `--everything` still finds --fetch-everything.
    """
    flag = _normalize(token)
    longs = [o for o in dict.fromkeys(options) if o.startswith("--")]
    ratios = sorted(((difflib.SequenceMatcher(None, flag, o).ratio(), o) for o in longs),
                    reverse=True)
    if ratios and ratios[0][0] >= 0.6:
        # Only the near-best: a clear typo of one flag shouldn't drag in
        # whatever else happens to share its prefix.
        best = ratios[0][0]
        return [o for r, o in ratios[:n] if r >= max(0.6, best - 0.1)]
    words = {w for w in flag.strip("-").split("-") if len(w) >= 4}
    if not words:
        return []
    scored = []
    for opt in longs:
        shared = len(words & set(opt[2:].split("-")))
        if shared:
            scored.append((-shared, difflib.SequenceMatcher(None, flag, opt).ratio() * -1, opt))
    return [opt for *_, opt in sorted(scored)[:n]]


def _or_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


class ArgumentParser(argparse.ArgumentParser):
    """argparse.ArgumentParser plus did-you-mean hints for unknown flags."""

    def parse_known_args(self, args=None, namespace=None):
        _active.append(self)
        return super().parse_known_args(args, namespace)

    def parse_args(self, args=None, namespace=None):
        _active.clear()
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error(self._unrecognized_message(extras))
        return parsed

    def _scope(self) -> List["ArgumentParser"]:
        return list(_active) or [self]

    def _unrecognized_message(self, extras: Sequence[str]) -> str:
        lines = ["unrecognized arguments: " + " ".join(extras)]
        actions = {}  # option string -> action, to show one name per action
        for parser in self._scope():
            for opt, action in parser._option_string_actions.items():
                actions.setdefault(opt, action)
        for token in extras:
            if not token.startswith("-") or len(token) <= 2 or _looks_numeric(token):
                continue
            seen, picks = set(), []
            for opt in suggest(token, actions):
                if id(actions[opt]) not in seen:  # --pull-everything = --fetch-everything
                    seen.add(id(actions[opt]))
                    picks.append(opt)
            if picks:
                lines.append(f"  {token}: did you mean {_or_list(picks)}?")
        return "\n".join(lines)

    def error(self, message):
        # Python 3.14+ can already suggest choices itself (suggest_on_error).
        match = re.search(r"invalid choice: '([^']*)'", message)
        if match and "maybe you meant" not in message:
            choices = [str(c) for p in self._scope() for a in p._actions
                       if a.choices for c in a.choices]
            picks = difflib.get_close_matches(match.group(1), choices, n=3, cutoff=0.6)
            if picks:
                message += f"\n  did you mean {_or_list(picks)}?"
        super().error(message)


def _looks_numeric(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False
