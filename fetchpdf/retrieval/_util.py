"""Small helpers that more than one module in this package needs.

Each of these existed in two to four copies before. That is mostly harmless
duplication -- except for `localname`, where it was not: both copies need the
non-string guard for lxml comment and processing-instruction nodes, whose `.tag`
is a *callable* rather than a name. A copy written without that guard raised
TypeError on every real publisher page, which is how T2 validation was broken
until an integration run caught it. One definition, one guard.
"""

import os
from datetime import datetime, timezone
from typing import Optional


def localname(tag) -> str:
    """An element tag without its namespace.

    TEI is namespaced, JATS usually is not, and lxml hands back comments and
    processing instructions whose `.tag` is a function -- hence the isinstance
    check, which is the whole reason this lives in one place.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def as_int(value, default: int = 0) -> int:
    """int(value) for values that arrive from JSON as strings, or as None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def iso(timestamp: float) -> str:
    """A UTC ISO-8601 stamp. Used in every audit record this package writes."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unlink(path: Optional[str]) -> None:
    """Delete a path, tolerating its absence. Cleanup must never raise."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
