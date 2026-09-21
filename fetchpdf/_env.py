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

# Elsevier full-text (TDM) retrieval.
ELSEVIER_TDM_API_KEY = os.getenv("ELSEVIER_TDM_API_KEY")


if not EMAIL:
    # stderr, not stdout: this fires at import, before any CLI code runs, so
    # under `fetchpdf --json` it would otherwise land in the results stream
    # ahead of the first JSON object and nothing downstream could parse it.
    # A note about the caller's own configuration is not a result either way.
    print("\033[93m⚠️  Warning: EMAIL not set in .env.local", file=sys.stderr)
    print("   Please create .env.local with: EMAIL=your@email.com", file=sys.stderr)
    print("   This affects:", file=sys.stderr)
    print("     - Crossref API rate limits (10 req/s with email, 5 req/s without)",
          file=sys.stderr)
    print("     - Unpaywall API access (required)", file=sys.stderr)
    print("     - Europe PMC contact info (optional)", file=sys.stderr)
    print("   Continuing without email...\033[0m", file=sys.stderr)
