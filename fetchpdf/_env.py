"""Single source of truth for .env.local loading and API credentials.

Imported for its side effects at package import time, not lazily: callers such
as birds_eye_review_code/5_download_pdfs.py load their own .env before
importing fetchpdf precisely because these are read at import. Moving them to
call time would silently degrade those callers to no-email API calls.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv, find_dotenv


def _load_env_file():
    """Locate and load .env.local, returning its path (or None).

    Repo root is tried first so an editable install keeps resolving exactly as
    it always has. The CWD search is the fallback that makes a real (non-editable)
    install work at all: there, __file__ points into site-packages/ and the
    repo-root path simply does not exist, so every key silently came back None.
    """
    repo_root = Path(__file__).resolve().parent.parent / ".env.local"
    if repo_root.exists():
        load_dotenv(repo_root)
        return str(repo_root)

    found = find_dotenv(".env.local", usecwd=True)
    if found:
        load_dotenv(found)
        return found

    return None


ENV_FILE = _load_env_file()

EMAIL = os.getenv("EMAIL")

# Rate-limit lifts these keys buy, where the provider documents one:
S2_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY")    # Semantic Scholar: 1 -> 100 req/s
ENTREZ_API_KEY = os.getenv("ENTREZ_EUTILS_API_KEY")   # NCBI E-utils: 3 -> 10 req/s
OPENALEX_API_KEY = os.getenv("OPENALEXAPIKEY")
CORE_API_KEY = os.getenv("COREAPIKEY")
SCOPUS_API_KEY = os.getenv("SCOPUS_API_KEY")

# The retrieval agent's OpenRouter backend. Optional: without it that backend
# reports itself unavailable and the run falls back to the Claude Code CLI, or
# to the deterministic rules alone. A missing key is never a failed record.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# PubPeer developer key. Unlike the keys above this one is NOT a rate-limit
# lift -- the API rejects a keyless request outright with HTTP 422, so without
# it no PubPeer question can be asked at all. Absence is therefore reported as
# an error by fetchpdf.retrieval.pubpeer rather than as "no comments found":
# "we could not ask" and "there was nothing to find" are different facts, and
# only one of them is about the paper.
PUBPEER_DEVKEY = os.getenv("PUBPEER_DEVKEY")

# Elsevier full-text (TDM) retrieval.
ELSEVIER_TDM_API_KEY = os.getenv("ELSEVIER_TDM_API_KEY")


if not EMAIL:
    # stderr, not stdout: this runs at import time, before any CLI code, so on
    # stdout it would land ahead of whatever a caller is parsing there.
    print("\033[93m⚠️  Warning: EMAIL not set in .env.local\n"
          "   Please create .env.local with: EMAIL=your@email.com\n"
          "   This affects:\n"
          "     - Crossref's polite pool (requests are identified by email)\n"
          "     - Unpaywall API access (required)\n"
          "     - Europe PMC contact info (optional)\n"
          "   Continuing without email...\033[0m", file=sys.stderr)
