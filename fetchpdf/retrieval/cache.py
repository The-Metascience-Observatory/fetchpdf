"""Stage 0: the resolution cache.

Resolution results are far more reusable than artifacts. A PDF is a 2 MB file
you either have or do not; "this DOI maps to PMC1817752" is 30 bytes that stays
true, is needed by every subsequent run, and costs a network round-trip to
rediscover. So it is cached hard and separately from the artifacts.

This is also what makes a batch resumable: killing a run halfway and restarting
re-reads the cache instead of re-resolving thousands of DOIs.
"""

import json
import os
import tempfile
import threading
from typing import Dict, Optional
from ._util import unlink as _unlink

CACHE_FILENAME = ".fetchpdf_resolution.json"
_CACHE_VERSION = 1


class ResolutionCache:
    """A JSON dict of doi -> resolved identifier fields, persisted atomically.

    Writes go to a temp file in the same directory and are renamed into place,
    so a run killed mid-write leaves the previous cache intact rather than a
    truncated file that fails to parse on the next start.
    """

    def __init__(self, path: Optional[str], verbose: bool = False):
        self.path = path
        self.verbose = verbose
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self.hits = 0
        self.misses = 0
        if path:
            self._load()

    @classmethod
    def for_output_dir(cls, output_dir: Optional[str], verbose: bool = False) -> "ResolutionCache":
        if not output_dir:
            return cls(None, verbose=verbose)
        return cls(os.path.join(output_dir, CACHE_FILENAME), verbose=verbose)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # A corrupt cache must never stop a run; it is a speed-up, not state.
            if self.verbose:
                print(f"  resolution cache unreadable ({e}); starting empty")
            return
        if raw.get("version") != _CACHE_VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._data = entries

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            entry = self._data.get(_norm(key))
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return dict(entry)

    def put(self, key: str, values: dict) -> None:
        if not key:
            return
        with self._lock:
            existing = self._data.setdefault(_norm(key), {})
            for k, v in values.items():
                if v:
                    existing[k] = v
            self._dirty = True

    def flush(self) -> None:
        """Persist if anything changed. Safe to call repeatedly."""
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {"version": _CACHE_VERSION, "entries": self._data}
            data = self._data
            self._dirty = False
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".fetchpdf-cache-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp, self.path)
            except BaseException:
                _unlink(tmp)
                raise
        except OSError as e:
            if self.verbose:
                print(f"  could not persist resolution cache: {e}")
            with self._lock:
                self._dirty = True
        else:
            if self.verbose:
                print(f"  resolution cache: {len(data)} entries -> {self.path}")

    def __len__(self):
        with self._lock:
            return len(self._data)


def _norm(key: str) -> str:
    return str(key).strip().lower()



