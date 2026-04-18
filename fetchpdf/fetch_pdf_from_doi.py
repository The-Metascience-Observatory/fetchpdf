import os
import re
import time
import difflib
import html
import shutil
import tempfile
import subprocess
import requests
import urllib3
from requests.exceptions import SSLError

# Load environment variables from .env.local
from pathlib import Path
from dotenv import load_dotenv
env_file = Path(__file__).parent.parent / '.env.local'
if env_file.exists():
    load_dotenv(env_file)

# Warn if EMAIL not configured
_DEFAULT_EMAIL = None
if not os.getenv("EMAIL"):
    print("\033[93m⚠️  Warning: EMAIL not set in .env.local")
    print("   Please create .env.local with: EMAIL=your@email.com")
    print("   This affects:")
    print("     - Crossref API rate limits (10 req/s with email, 5 req/s without)")
    print("     - Unpaywall API access (required)")
    print("     - Europe PMC contact info (optional)")
    print("   Continuing without email...\033[0m")
else:
    _DEFAULT_EMAIL = os.getenv("EMAIL")

# API keys (loaded from .env.local)
_S2_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY")    # Semantic Scholar: 1→100 req/s
_ENTREZ_API_KEY = os.getenv("ENTREZ_EUTILS_API_KEY")   # NCBI E-utils: 3→10 req/s

# Session-level state for providers that should be disabled after a fatal auth error.
# These are shared across all ThreadPoolExecutor workers in a single batch run.
_CORE_SESSION_DISABLED = False

# Suppress InsecureRequestWarning when we fall back to verify=False for sites with SSL issues
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import urljoin, quote_plus, urlparse
from .fetch_metadata_from_doi import fetch_metadata_from_doi

_UA_EMAIL = _DEFAULT_EMAIL or "anonymous"
headers = {
    "User-Agent": f"fetchpdf/0.1.0 (mailto:{_UA_EMAIL}; https://github.com/The-Metascience-Observatory/fetchpdf)",
    "Accept": (
        "application/pdf,application/octet-stream,"
        "application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

DOI_URL_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
FILENAME_SAFE_DOI_RE = re.compile(r"^(10\.\d{4,9})--(.+)$", re.IGNORECASE)
PMID_URL_RE = re.compile(r"^https?://(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?", re.IGNORECASE)
PMID_INPUT_RE = re.compile(r"^(?:pmid[\s:._-]*)?(\d{4,10})$", re.IGNORECASE)
DOI_CANDIDATE_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)


def _ncbi_url(base_url):
    """Append NCBI Entrez api_key param if available."""
    if _ENTREZ_API_KEY:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}api_key={_ENTREZ_API_KEY}"
    return base_url


def _get_with_retries(url, timeout=15, retries=3, backoff=0.5, **kwargs):
    """
    requests.get with exponential backoff on transient network errors.

    Retries on SSL EOF, connection reset, read/connect timeouts, chunked-encoding
    truncation. Non-transient errors (HTTP 4xx/5xx, JSON parse, etc.) are the
    caller's problem — we only retry low-level transport failures that tend to
    resolve themselves on the next attempt. Raises the last caught exception
    if every attempt fails.
    """
    from requests.exceptions import ConnectionError as _ReqConnErr, Timeout, ChunkedEncodingError
    last_exc = None
    for attempt in range(retries):
        try:
            return requests.get(url, timeout=timeout, **kwargs)
        except (SSLError, _ReqConnErr, Timeout, ChunkedEncodingError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc


def _record_source(_source_out, source):
    """If _source_out is provided (mutable list), set _source_out[0] = source."""
    if _source_out is not None:
        _source_out[0] = source


def canonicalize_doi(raw_doi):
    """Normalize DOI input and restore filename-safe form."""
    if not isinstance(raw_doi, str):
        return raw_doi

    doi = DOI_URL_PREFIX_RE.sub("", raw_doi.strip()).lower()
    match = FILENAME_SAFE_DOI_RE.match(doi)
    if match:
        doi = f"{match.group(1)}/{match.group(2)}"
    return doi


def sanitize_doi(raw_doi):
    """Strip common URL artifacts from DOI strings.

    Handles: query params (?...), /full/html, /html, trailing slashes,
    .pdf extensions, bioRxiv version suffixes, OUP trailing article IDs.
    """
    if not isinstance(raw_doi, str):
        return raw_doi

    original = raw_doi.strip()
    doi = original

    # Strip query parameters (?redirectedfrom=fulltext, ?journalcode=prxa, etc.)
    if '?' in doi:
        doi = doi.split('?')[0]

    # Strip common URL path suffixes
    for suffix in ['/full/html', '/full/pdf', '/html', '/pdf', '/abstract', '/summary', '/full']:
        if doi.lower().endswith(suffix):
            doi = doi[:len(doi) - len(suffix)]
            break

    # Strip trailing slashes
    doi = doi.rstrip('/')

    # Strip .pdf extension (e.g. 10.1101/2024.02.13.580153v1.full.pdf)
    if doi.lower().endswith('.pdf'):
        doi = doi[:-4]

    # Strip bioRxiv/medRxiv version suffixes (v1.full, v2.full, etc.)
    doi = re.sub(r'v\d+\.full$', '', doi, flags=re.IGNORECASE)

    # Strip OUP-style trailing numeric article IDs (e.g. 10.1093/abm/kaad072/7512904)
    # OUP DOIs are 10.1093/{journal}/{article} — trailing /\d+ is a URL artifact
    if doi.startswith('10.1093/'):
        doi = re.sub(r'/\d+$', '', doi)

    # Convert filename-format DOIs: replace -- with / (our filename separator)
    # e.g. "10.1093/eurheartj--ehaf339" → "10.1093/eurheartj/ehaf339"
    if '--' in doi:
        doi = doi.replace('--', '/')

    return doi


def extract_pmid(raw_identifier):
    """Extract PMID digits from plain or URL input."""
    if not isinstance(raw_identifier, str):
        return None
    text = raw_identifier.strip()
    if not text:
        return None

    m = PMID_URL_RE.match(text)
    if m:
        return m.group(1)

    m = PMID_INPUT_RE.match(text)
    if m:
        return m.group(1)
    return None


def doi_to_pmid(doi: str, verbose=False):
    """Resolve DOI to PMID when the article is indexed in PubMed."""
    if not isinstance(doi, str) or not doi.strip():
        return None
    doi = doi.strip()
    if not doi.startswith("10."):
        doi = canonicalize_doi(doi)
    if not doi or not doi.startswith("10."):
        return None

    # 1) NCBI idconv (accepts DOI, returns pmid when in PubMed)
    try:
        r = _get_with_retries(
            _ncbi_url(f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={quote_plus(doi)}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            records = (r.json() or {}).get("records") or []
            for rec in records:
                pmid = rec.get("pmid")
                if pmid is not None:
                    return str(pmid).strip()
    except Exception as e:
        if verbose:
            print(f"  DOI->PMID idconv lookup failed for {doi}: {str(e)[:100]}")

    # 2) Europe PMC search by DOI
    try:
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{quote_plus(doi)}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            hits = (r.json().get("resultList") or {}).get("result") or []
            for h in hits:
                if h.get("pmid"):
                    return str(h["pmid"]).strip()
    except Exception as e:
        if verbose:
            print(f"  DOI->PMID Europe PMC lookup failed: {str(e)[:100]}")
    return None


def pmid_to_doi(pmid: str, verbose=False):
    """Resolve PMID to DOI using multiple metadata sources."""

    def _clean(doi_value):
        if not isinstance(doi_value, str):
            return None
        doi = doi_value.strip()
        if doi.lower().startswith("doi:"):
            doi = doi[4:].strip()
        doi = doi.strip(" \t\r\n.;,)]}>\"'")
        doi = canonicalize_doi(doi)
        return doi if doi.startswith("10.") else None

    def _first_doi_from_text(text):
        if not isinstance(text, str):
            return None
        m = DOI_CANDIDATE_RE.search(text)
        if not m:
            return None
        return _clean(m.group(0))

    def _extract_pubmed_core_fields(xml_text: str):
        title = ""
        journal = ""
        year = None
        if not isinstance(xml_text, str) or not xml_text:
            return title, journal, year

        mt = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", xml_text, re.IGNORECASE | re.DOTALL)
        if mt:
            title = re.sub(r"<[^>]+>", " ", mt.group(1))
            title = html.unescape(title)
            title = re.sub(r"\s+", " ", title).strip()

        mj = re.search(r"<JournalTitle>(.*?)</JournalTitle>", xml_text, re.IGNORECASE | re.DOTALL)
        if mj:
            journal = re.sub(r"<[^>]+>", " ", mj.group(1))
            journal = html.unescape(journal)
            journal = re.sub(r"\s+", " ", journal).strip()

        # Prefer article publication year from PubDate/ArticleDate; first <Year> can be DateCompleted etc.
        my = re.search(
            r"<(?:PubDate|ArticleDate)[^>]*>.*?<Year>(\d{4})</Year>",
            xml_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not my:
            my = re.search(r"<Year>(\d{4})</Year>", xml_text, re.IGNORECASE)
        if my:
            try:
                year = int(my.group(1))
            except Exception:
                year = None
        return title, journal, year

    def _crossref_item_year(item: dict):
        for key in ("issued", "published-print", "published-online", "created"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and isinstance(parts[0], list) and parts[0]:
                try:
                    return int(parts[0][0])
                except Exception:
                    pass
        return None

    def _lookup_doi_via_crossref_title(title: str, journal: str, year, verbose=False):
        title = (title or "").strip()
        if len(title) < 8:
            return None

        candidates = {}
        queries = [
            {"query.title": title, "rows": 25},
            {"query.bibliographic": " ".join(x for x in [title, journal or "", str(year or "")] if x), "rows": 25},
        ]
        try:
            for params in queries:
                # Add mailto for polite pool (10 req/s vs 5 req/s)
                if _DEFAULT_EMAIL:
                    params["mailto"] = _DEFAULT_EMAIL
                r = requests.get("https://api.crossref.org/works", params=params, timeout=15)
                if r.status_code != 200:
                    continue
                items = ((r.json() or {}).get("message") or {}).get("items") or []
                for item in items:
                    doi = _clean(item.get("DOI"))
                    if not doi:
                        continue
                    if doi in candidates:
                        continue
                    ctitle = ((item.get("title") or [""])[0] or "").strip()
                    cjournal = ((item.get("container-title") or [""])[0] or "").strip()
                    cyear = _crossref_item_year(item)
                    t_sim = _text_similarity(title, ctitle)
                    j_sim = _text_similarity(journal, cjournal) if journal else 0.0
                    y_bonus = 0.15 if (year and cyear and year == cyear) else 0.0
                    score = (0.75 * t_sim) + (0.20 * j_sim) + y_bonus
                    candidates[doi] = {
                        "score": score,
                        "title_sim": t_sim,
                        "journal_sim": j_sim,
                        "year": cyear,
                        "crossref_title": ctitle,
                    }

            if not candidates:
                return None

            # When PubMed has a year, only consider Crossref candidates with that year
            # (avoids wrong match for common titles e.g. "Chronic fatigue syndrome" 2008 vs 2016)
            if year is not None:
                same_year = {k: v for k, v in candidates.items() if v["year"] == year}
                if not same_year:
                    if verbose:
                        print(
                            f"  Crossref title lookup: no candidate with year={year} for PMID {pmid}, skipping"
                        )
                    return None
                candidates = same_year

            best_doi, best = max(candidates.items(), key=lambda kv: kv[1]["score"])
            strong_title = best["title_sim"] >= 0.72
            year_mismatch = (
                year is not None
                and best["year"] is not None
                and year != best["year"]
            )
            plausible = strong_title and not year_mismatch and (
                (year and best["year"] and year == best["year"])
                or best["journal_sim"] >= 0.40
                or best["score"] >= 0.68
            )
            if not plausible:
                if verbose:
                    print(
                        f"  Crossref title lookup best match too weak for PMID {pmid}: "
                        f"sim={best['title_sim']:.2f}, journal={best['journal_sim']:.2f}, score={best['score']:.2f}"
                    )
                return None
            # When PubMed has a year, verify with Crossref works API (search response can be wrong)
            if year is not None:
                try:
                    crossref_params = {}
                    if _DEFAULT_EMAIL:
                        crossref_params["mailto"] = _DEFAULT_EMAIL
                    verify_r = requests.get(
                        f"https://api.crossref.org/works/{quote_plus(best_doi)}",
                        params=crossref_params,
                        timeout=10,
                    )
                    if verify_r.status_code == 200:
                        v_item = (verify_r.json() or {}).get("message") or {}
                        v_year = _crossref_item_year(v_item)
                        if v_year is not None and v_year != year:
                            if verbose:
                                print(
                                    f"  Crossref title lookup rejecting year mismatch (works API): "
                                    f"PMID {pmid} PubMed year={year}, {best_doi} year={v_year}"
                                )
                            return None
                except Exception:
                    pass
            if verbose:
                print(
                    f"  Crossref title lookup matched PMID {pmid} -> DOI {best_doi} "
                    f"(sim={best['title_sim']:.2f}, score={best['score']:.2f})"
                )
            return canonicalize_doi(best_doi)
        except Exception as e:
            if verbose:
                print(f"  Crossref title lookup failed for PMID {pmid}: {str(e)[:100]}")
            return None

    # 1) NCBI PMC idconv
    try:
        r = _get_with_retries(
            _ncbi_url(f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={pmid}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            records = (r.json() or {}).get("records") or []
            for rec in records:
                doi = _clean(rec.get("doi"))
                if doi:
                    return doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI idconv lookup failed for {pmid}: {str(e)[:100]}")

    # 2) PubMed EFetch XML (ArticleId IdType=doi / ELocationID)
    try:
        xml_text = ""
        r = requests.get(
            _ncbi_url(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"),
            timeout=12,
        )
        if r.status_code == 200 and r.text:
            xml_text = r.text
            article_ids = re.findall(
                r'<ArticleId[^>]*IdType=["\']doi["\'][^>]*>([^<]+)</ArticleId>',
                xml_text,
                re.IGNORECASE,
            )
            for candidate in article_ids:
                doi = _clean(candidate)
                if doi:
                    return doi

            e_location_ids = re.findall(
                r'<ELocationID[^>]*EIdType=["\']doi["\'][^>]*>([^<]+)</ELocationID>',
                xml_text,
                re.IGNORECASE,
            )
            for candidate in e_location_ids:
                doi = _clean(candidate)
                if doi:
                    return doi

            fallback_doi = _first_doi_from_text(xml_text)
            if fallback_doi:
                return fallback_doi

            # 2b) No DOI field present -> Crossref title-based recovery.
            title, journal, year = _extract_pubmed_core_fields(xml_text)
            crossref_doi = _lookup_doi_via_crossref_title(title, journal, year, verbose=verbose)
            if crossref_doi:
                return crossref_doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI efetch lookup failed for {pmid}: {str(e)[:100]}")

    # 3) Europe PMC
    try:
        q = f"EXT_ID:{pmid}%20AND%20SRC:MED"
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = ((r.json() or {}).get("resultList") or {}).get("result") or []
            for result in results:
                doi = _clean(result.get("doi"))
                if doi:
                    return doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI Europe PMC lookup failed for {pmid}: {str(e)[:100]}")

    # 4) PubMed HTML meta tag fallback
    try:
        r = requests.get(
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            headers=headers,
            timeout=12,
        )
        if r.status_code == 200 and r.text:
            m = re.search(
                r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']',
                r.text,
                re.IGNORECASE,
            )
            if m:
                doi = _clean(m.group(1))
                if doi:
                    return doi

            fallback_doi = _first_doi_from_text(r.text)
            if fallback_doi:
                return fallback_doi
    except Exception as e:
        if verbose:
            print(f"  PMID->DOI PubMed HTML lookup failed for {pmid}: {str(e)[:100]}")

    if verbose:
        print(f"  PMID->DOI resolution exhausted all sources for {pmid}")
    return None


def resolve_identifier_to_doi(identifier, verbose=False):
    """
    Normalize DOI-like inputs and resolve PMID inputs to DOI.
    Returns canonical DOI string on success, else None.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        return None

    raw = sanitize_doi(identifier.strip())
    doi_candidate = canonicalize_doi(raw)
    if doi_candidate.startswith("10."):
        return doi_candidate

    pmid = extract_pmid(raw)
    if pmid:
        resolved = pmid_to_doi(pmid, verbose=verbose)
        if verbose:
            if resolved:
                print(f"  Resolved PMID {pmid} -> DOI {resolved}")
            else:
                print(f"  Could not resolve PMID {pmid} to a DOI")
        return resolved

    return None


def doi_to_safe_filename(doi: str) -> str:
    """Convert DOI to filesystem-safe filename stem."""
    return doi.replace("/", "--").replace(":", "--")

def _save_pdf_response(r, save_path, verbose=False) -> bool:
    """Write a streaming response to save_path if it looks like a PDF."""
    content_type = r.headers.get("content-type", "").lower()
    if r.status_code != 200:
        return False
    if "pdf" in content_type:
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        if verbose:
            print("PDF downloaded OK.")
        return True
    if "octet-stream" in content_type:
        chunks = list(r.iter_content(8192))
        if chunks and chunks[0][:4] == b"%PDF":
            with open(save_path, "wb") as f:
                for chunk in chunks:
                    if chunk:
                        f.write(chunk)
            if verbose:
                print("PDF downloaded OK (octet-stream).")
            return True
    return False


#-----------------------------------------------------------------------------------------
def try_download(url, save_path, verbose=False):
    """Try downloading PDF from URL and save to save_path."""

    if not url:
        return False

    request_headers = {**headers, "Referer": url}

    from requests.exceptions import ConnectionError as _ReqConnErr, Timeout as _ReqTimeout
    try:

        # Retry once on transient connection errors (reset, read timeout).
        # SSL errors keep their existing verify=False fallback path.
        r = None
        for _attempt in range(2):
            try:
                r = requests.get(
                    url,
                    headers=request_headers,
                    timeout=20,
                    allow_redirects=True,
                    stream=True,
                )
                break
            except SSLError as e:
                if verbose:
                    print("SSL verification failed, retrying with verify=False (INSECURE):", e)
                r = requests.get(
                    url,
                    headers=request_headers,
                    timeout=25,
                    allow_redirects=True,
                    stream=True,
                    verify=False,        # last-resort bypass
                )
                break
            except (_ReqConnErr, _ReqTimeout) as e:
                if _attempt == 0:
                    if verbose:
                        print(f"Transient connection error, retrying once: {str(e)[:100]}")
                    time.sleep(1)
                    continue
                raise

        # Silently fail for non-200 or non-PDF responses (normal during fallback attempts)
        if _save_pdf_response(r, save_path, verbose):
            return True
    except Exception as e:
        if verbose:
            print(f"Failed downloading from {url[:80]}: {str(e)[:100]}")
    return False


def try_download_with_session(url: str, save_path: str, referer: str = None, verbose=False) -> bool:
    """Download PDF from URL using a persistent session."""
    if not url:
        return False
    try:
        session = requests.Session()
        session.headers.update(headers)
        r = session.get(url, timeout=20, allow_redirects=True, stream=True)
        if _save_pdf_response(r, save_path, verbose):
            return True
    except Exception as e:
        if verbose:
            print(f"Session download failed: {e}")
    return False


def _is_plausible_http_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if any(ch in u for ch in [" ", "{", "}", "<", ">", "\\n", "\\r", "\\t"]):
        return False
    return True


def _collect_landing_page_candidates(landing_url: str, html_text: str):
    """Extract and prioritize likely PDF/download URLs from landing page HTML."""
    candidates = []

    # High-signal metadata fields used by many publishers/repositories.
    for m in re.findall(
        r'<meta[^>]+(?:name|property)=["\'](?:citation_pdf_url|og:pdf|dc\.identifier)["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    ):
        candidates.append(urljoin(landing_url, html.unescape(m.strip())))

    # Common link-bearing attributes.
    for m in re.findall(
        r'(?:href|src|data-href|data-url)=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    ):
        candidates.append(urljoin(landing_url, html.unescape(m.strip())))

    # Bare URLs in inline scripts/JSON-LD.
    for m in re.findall(r'https?://[^\s"\'<>]+', html_text, re.IGNORECASE):
        candidates.append(html.unescape(m.strip()))

    blocked_domains = (
        "googletagmanager.com",
        "google-analytics.com",
        "doubleclick.net",
        "facebook.net",
        "twitter.com/i/",
        "youtube.com/",
        "vimeo.com/",
    )

    landing_host = (urlparse(landing_url).netloc or "").lower()

    # Keep only plausible, relevant URLs and dedupe.
    deduped = []
    seen = set()
    for c in candidates:
        if not _is_plausible_http_url(c):
            continue
        cl = c.lower()
        if any(d in cl for d in blocked_domains):
            continue
        # Keep links likely to lead to downloadable content.
        looks_downloadable = any(
            k in cl for k in [
                ".pdf", "download", "full.pdf", "/doi/pdf", "pdfdirect",
                "/bitstream/", "viewcontent.cgi", "/api/access/datafile",
                "/article/file/", "/content/", "/document/", "/files/",
            ]
        )
        same_host = (urlparse(c).netloc or "").lower() == landing_host
        if not looks_downloadable and not same_host:
            continue
        # Exclude obvious non-document assets.
        if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?|ttf)(\?|$)", cl):
            continue
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)

    # Score candidates by PDF-likelihood.
    def _score(url: str) -> float:
        u = url.lower()
        s = 0.0
        if ".pdf" in u:
            s += 4.0
        if "citation_pdf_url" in u:
            s += 3.0
        if any(k in u for k in ["/doi/pdf", "pdfdirect", "/download", "download=", "/bitstream/", "/content/", "viewcontent.cgi"]):
            s += 2.0
        if any(k in u for k in ["tandfonline.com", "sagepub.com", "wiley.com", "jneurosci.org", "direct.mit.edu", "econstor.eu", "canterbury.ac.nz"]):
            s += 0.8
        return s

    deduped.sort(key=_score, reverse=True)
    return deduped


def try_landing_page_pdf_fallback(doi: str, landing_url: str, save_path: str, verbose=False) -> bool:
    """Try publisher/repository-specific and extracted links from a landing page."""
    if not landing_url or not _is_plausible_http_url(landing_url):
        return False

    try:
        landing_resp = requests.get(landing_url, headers=headers, timeout=25, allow_redirects=True)
    except Exception as e:
        if verbose:
            print(f"  Landing page request failed: {str(e)[:120]}")
        return False

    # Landing already resolved to a PDF.
    if landing_resp.status_code == 200 and "pdf" in (landing_resp.headers.get("content-type", "").lower()):
        with open(save_path, "wb") as f:
            f.write(landing_resp.content)
        if verbose:
            print(f"✅ Landing page resolved directly to PDF for {doi}")
        return True

    if landing_resp.status_code != 200:
        return False

    final_landing = landing_resp.url or landing_url
    html_text = landing_resp.text or ""

    # Deterministic publisher URL patterns.
    host = (urlparse(final_landing).netloc or "").lower()
    deterministic = []
    if "tandfonline.com" in host:
        deterministic.extend([
            f"https://www.tandfonline.com/doi/pdf/{doi}",
            f"https://www.tandfonline.com/doi/pdf/{doi}?download=true",
        ])
    if "sagepub.com" in host:
        deterministic.extend([
            f"https://journals.sagepub.com/doi/pdf/{doi}",
            f"https://journals.sagepub.com/doi/pdf/{doi}?download=true",
        ])
    if "wiley.com" in host:
        deterministic.extend([
            f"https://onlinelibrary.wiley.com/doi/pdf/{doi}",
            f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
        ])
    if "emerald.com" in host or doi.lower().startswith("10.1108/"):
        doi_upper = doi.upper()
        deterministic.extend([
            f"https://www.emerald.com/insight/content/doi/{doi_upper}/full/pdf",
            f"https://www.emerald.com/insight/content/doi/{doi}/full/pdf",
        ])
    if "jneurosci.org" in host:
        suffix = doi.split("/", 1)[1].upper() if "/" in doi else doi.upper()
        deterministic.append(f"https://www.jneurosci.org/content/{suffix}.full.pdf")
    if "direct.mit.edu" in host or doi.lower().startswith("10.1162/"):
        mit_slug = doi.lower().replace("/imag.a.", "/imag_a_").replace("/imag.", "/imag_")
        deterministic.append(f"https://direct.mit.edu/doi/pdf/{mit_slug}")
    # Acta Biochimica Polonica moved to Frontiers Partnerships; doi.org can 404 but PDF lives at journal path
    if "frontierspartnerships.org" in host or doi.lower().startswith("10.18388/abp"):
        deterministic.append(
            f"https://www.frontierspartnerships.org/journals/acta-biochimica-polonica/articles/{doi}/pdf"
        )

    # Landing-page candidates from HTML parsing.
    page_candidates = _collect_landing_page_candidates(final_landing, html_text)
    candidate_urls = deterministic + [u for u in page_candidates if u not in deterministic]

    if verbose:
        print(f"  Landing-page candidate URLs: {len(candidate_urls)}")

    for candidate in candidate_urls[:40]:
        if try_download(candidate, save_path, verbose):
            if verbose:
                print(f"✅ Landing-page extracted PDF success for {doi}")
            return True
        if try_download_with_session(candidate, save_path, verbose=verbose):
            if verbose:
                print(f"✅ Landing-page session PDF success for {doi}")
            return True

    return False


#-----------------------------------------------------------------------------------------
def try_escholarship_via_pubmed(doi: str, save_path: str, verbose=False) -> bool:
    """
    Find eScholarship PDF via PubMed/Europe PMC LinkOut.
    UC and other universities deposit in eScholarship; PubMed abstract pages list these.
    UC and other universities deposit PDFs in eScholarship.
    """
    try:
        # Get PMID from Europe PMC search by DOI
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json",
            timeout=20,
        )
        if r.status_code != 200:
            return False
        hits = (r.json().get("resultList") or {}).get("result") or []
        pmid = None
        for h in hits:
            if h.get("pmid"):
                pmid = h["pmid"]
                break
        if not pmid:
            return False

        # Fetch PubMed abstract page for LinkOut full-text links (eScholarship etc.)
        # Europe PMC abstract often lacks LinkOut; PubMed has them
        abstract_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        r2 = requests.get(abstract_url, headers=headers, timeout=15)
        if r2.status_code != 200:
            return False

        # Find eScholarship item links
        escholarship_items = re.findall(
            r'https?://(?:www\.)?escholarship\.org/uc/item/([a-zA-Z0-9]+)',
            r2.text,
            re.IGNORECASE,
        )
        if not escholarship_items:
            return False

        for item_id in escholarship_items[:3]:  # try up to 3
            item_url = f"https://escholarship.org/uc/item/{item_id}"
            # eScholarship PDF URL pattern: /content/qt{item_id}/{item_id}.pdf
            pdf_url = f"https://escholarship.org/content/qt{item_id}/qt{item_id}.pdf"
            if try_download_with_session(pdf_url, save_path, verbose=verbose):
                if verbose:
                    print(f"✅ eScholarship (PubMed LinkOut) success for {doi}")
                return True
    except Exception as e:
        if verbose:
            print(f"Error with eScholarship/PubMed: {e}")
    return False


def try_pmid_direct_pdf_fallback(pmid: str, save_path: str, verbose=False) -> bool:
    """
    Try PMID-native PDF retrieval when DOI is unavailable.
    Uses PMCID routes + Europe PMC full-text links.
    """
    if not pmid:
        return False

    pmcid = None
    try:
        r = _get_with_retries(
            _ncbi_url(f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={pmid}&format=json"),
            timeout=15,
        )
        if r.status_code == 200:
            for rec in (r.json() or {}).get("records", []) or []:
                c = rec.get("pmcid")
                if c:
                    pmcid = c.upper()
                    if not pmcid.startswith("PMC"):
                        pmcid = f"PMC{pmcid}"
                    break
    except Exception as e:
        if verbose:
            print(f"  PMID native idconv lookup failed for {pmid}: {str(e)[:100]}")

    # PMCID-derived direct PDF endpoints.
    if pmcid:
        pmc_candidates = [
            f"https://europepmc.org/articles/{pmcid}?pdf=render",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf",
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/?pdf=1",
        ]
        for url in pmc_candidates:
            if try_download(url, save_path, verbose):
                if verbose:
                    print(f"✅ PMID native PMCID route success for PMID {pmid}")
                return True

    # Europe PMC record full-text URLs.
    try:
        query = f"EXT_ID:{pmid}%20AND%20SRC:MED"
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={query}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = (r.json().get("resultList", {}) or {}).get("result", []) or []
            for result in results:
                for u in (result.get("fullTextUrlList", {}) or {}).get("fullTextUrl", []) or []:
                    url = (u.get("url") or "").strip()
                    if not url:
                        continue
                    if try_download(url, save_path, verbose):
                        if verbose:
                            print(f"✅ PMID native Europe PMC URL success for PMID {pmid}")
                        return True
                    if try_landing_page_pdf_fallback(f"pmid:{pmid}", url, save_path, verbose):
                        if verbose:
                            print(f"✅ PMID native Europe PMC landing success for PMID {pmid}")
                        return True
    except Exception as e:
        if verbose:
            print(f"  PMID native Europe PMC lookup failed for {pmid}: {str(e)[:100]}")

    # PubMed provider links / citation_pdf_url fallback.
    try:
        r = requests.get(
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200 and r.text:
            html_text = r.text
            candidates = []

            # Direct PDF meta tag when available.
            for m in re.findall(
                r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
                re.IGNORECASE,
            ):
                candidates.append(html.unescape(m.strip()))

            # Outbound provider links shown on PubMed page.
            for m in re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text, re.IGNORECASE):
                u = html.unescape(m.strip())
                ul = u.lower()
                if any(k in ul for k in [".pdf", "pdf", "/article/", "fulltext", "/doi/", "springer", "wiley", "tandfonline", "sagepub", "sciencedirect", "nature.com"]):
                    candidates.append(u)

            # Deduplicate while preserving order.
            deduped = []
            seen = set()
            for c in candidates:
                if c and c not in seen:
                    seen.add(c)
                    deduped.append(c)

            if verbose:
                print(f"  PMID native PubMed provider candidates: {len(deduped)}")

            for url in deduped[:20]:
                if try_download(url, save_path, verbose):
                    if verbose:
                        print(f"✅ PMID native PubMed direct URL success for PMID {pmid}")
                    return True
                if try_landing_page_pdf_fallback(f"pmid:{pmid}", url, save_path, verbose):
                    if verbose:
                        print(f"✅ PMID native PubMed landing success for PMID {pmid}")
                    return True
    except Exception as e:
        if verbose:
            print(f"  PMID native PubMed provider lookup failed for {pmid}: {str(e)[:100]}")

    return False



def _print_yellow_warning(message: str):
    """Print warning in yellow text for terminal users."""
    print(f"\033[93m{message}\033[0m")


def _xml_path_for_pdf_path(save_path: str) -> str:
    base, _ = os.path.splitext(save_path)
    return f"{base}.xml"



#-----------------------------------------------------------------------------------------
def _download_pdf_from_osf_api(osf_id: str, save_path: str, verbose=False) -> bool:
    """Try OSF API file providers for directly uploaded PDF files."""
    try:
        guid_url = f"https://api.osf.io/v2/guids/{osf_id}/"
        g = requests.get(guid_url, timeout=20)
        if g.status_code != 200:
            return False

        gdata = (g.json() or {}).get("data") or {}
        gtype = gdata.get("type")
        if gtype == "registrations":
            files_url = f"https://api.osf.io/v2/registrations/{osf_id}/files/"
        elif gtype == "nodes":
            files_url = f"https://api.osf.io/v2/nodes/{osf_id}/files/"
        elif gtype == "preprints":
            files_url = f"https://api.osf.io/v2/preprints/{osf_id}/files/"
        else:
            return False

        providers = requests.get(files_url, timeout=20)
        if providers.status_code != 200:
            return False

        for provider in (providers.json() or {}).get("data", []):
            rel = (
                provider.get("relationships", {})
                .get("files", {})
                .get("links", {})
                .get("related", {})
                .get("href")
            )
            if not rel:
                continue
            listing = requests.get(rel, timeout=20)
            if listing.status_code != 200:
                continue
            for item in (listing.json() or {}).get("data", []):
                attrs = item.get("attributes", {})
                name = (attrs.get("name") or "").lower()
                if attrs.get("kind") != "file" or not name.endswith(".pdf"):
                    continue
                dl_url = (item.get("links") or {}).get("download")
                if dl_url and try_download(dl_url, save_path, verbose):
                    if verbose:
                        print(f"✅ OSF API file download success for {osf_id}")
                    return True
    except Exception as e:
        if verbose:
            print(f"  OSF API file fallback error: {str(e)[:120]}")
    return False



#-----------------------------------------------------------------------------------------
def _text_similarity(a: str, b: str) -> float:
    """Return normalized similarity score for two strings."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _extract_pdf_links_from_doaj_record(record: dict):
    """Extract candidate full-text links from a DOAJ API record."""
    candidates = []
    bibjson = record.get("bibjson", {}) if isinstance(record, dict) else {}
    for link in bibjson.get("link", []) or []:
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if not url:
            continue
        link_type = (link.get("type") or "").lower()
        if "fulltext" in link_type or ".pdf" in url.lower():
            candidates.append(url)
    return candidates


def try_core_fallback(doi: str, save_path: str, verbose=False):
    """Try CORE (core.ac.uk) API for open-access PDFs."""
    global _CORE_SESSION_DISABLED

    # Skip silently if CORE was disabled earlier in this session due to auth failure
    if _CORE_SESSION_DISABLED:
        return False

    core_api_key = os.getenv("COREAPIKEY")
    if not core_api_key:
        if verbose:
            print("  CORE: no COREAPIKEY in .env.local, skipping")
        return False
    try:
        core_headers = {
            "Authorization": f"Bearer {core_api_key}",
            "Accept": "application/json",
            "User-Agent": "MetascienceObservatory/1.0",
        }
        r = requests.get(
            "https://api.core.ac.uk/v3/search/works/",
            params={"q": f'doi:"{doi}"', "limit": 3},
            headers=core_headers,
            timeout=15,
        )
        if r.status_code == 429:
            if verbose:
                print("  CORE: rate limited (429)")
            return False
        if r.status_code in (401, 403):
            # Auth failure — disable CORE for the rest of the session (all workers)
            if not _CORE_SESSION_DISABLED:
                _CORE_SESSION_DISABLED = True
                _print_yellow_warning(
                    f"⚠️  CORE disabled for session: HTTP {r.status_code}. "
                    f"Check COREAPIKEY in .env.local"
                )
            return False
        if r.status_code != 200:
            if verbose:
                print(f"  CORE: HTTP {r.status_code}")
            return False
        data = r.json()
        results = data.get("results", [])
        if not results:
            if verbose:
                print(f"  CORE: no results for {doi}")
            return False

        for result in results:
            download_url = result.get("downloadUrl")
            if not download_url:
                continue
            if verbose:
                print(f"  CORE: trying downloadUrl: {download_url[:120]}")
            if try_download(download_url, save_path, verbose):
                if verbose:
                    print(f"✅ CORE success for {doi}")
                return True
            # downloadUrl might be a landing page
            if try_landing_page_pdf_fallback(doi, download_url, save_path, verbose):
                if verbose:
                    print(f"✅ CORE (landing-page) success for {doi}")
                return True
        return False
    except Exception as e:
        if verbose:
            print(f"  CORE error: {e}")
        return False


def try_doaj_fallback(doi: str, save_path: str, verbose=False):
    """Try DOAJ API for OA full-text links."""
    try:
        query = quote_plus(f"doi:{doi}")
        url = f"https://doaj.org/api/search/articles/{query}"
        r = requests.get(url, timeout=12)
        if r.status_code != 200:
            return False

        data = r.json() or {}
        results = data.get("results", []) or []
        for rec in results[:10]:
            for candidate in _extract_pdf_links_from_doaj_record(rec):
                if try_download(candidate, save_path, verbose):
                    if verbose:
                        print(f"✅ DOAJ success for {doi}")
                    return True
    except Exception as e:
        if verbose:
            print(f"Error with DOAJ: {e}")
    return False


def _collect_datacite_candidate_urls(datacite_attributes: dict):
    """Collect likely downloadable URLs from DataCite metadata."""
    urls = []
    if not isinstance(datacite_attributes, dict):
        return urls

    direct_url = datacite_attributes.get("url")
    if isinstance(direct_url, str) and direct_url:
        urls.append(direct_url)

    content_url = datacite_attributes.get("contentUrl")
    if isinstance(content_url, list):
        urls.extend([u for u in content_url if isinstance(u, str) and u])
    elif isinstance(content_url, str) and content_url:
        urls.append(content_url)

    for rid in datacite_attributes.get("relatedIdentifiers", []) or []:
        if not isinstance(rid, dict):
            continue
        identifier = rid.get("relatedIdentifier")
        relation = (rid.get("relationType") or "").lower()
        if isinstance(identifier, str) and identifier.startswith("http"):
            if relation in {"issupplementto", "isversionof", "isidenticalto", "iscitedby", "references"}:
                urls.append(identifier)

    deduped = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def try_datacite_fallback(doi: str, save_path: str, verbose=False):
    """Try DataCite for direct content URLs and related resource links."""
    try:
        r = requests.get(f"https://api.datacite.org/dois/{doi}", timeout=12)
        if r.status_code != 200:
            return False

        attributes = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        candidates = _collect_datacite_candidate_urls(attributes)
        for candidate in candidates[:12]:
            if try_download(candidate, save_path, verbose):
                if verbose:
                    print(f"✅ DataCite success for {doi}")
                return True
    except Exception as e:
        if verbose:
            print(f"Error with DataCite fallback: {e}")
    return False







def try_apa_supplemental_fallback(doi: str, save_path: str, verbose=False):
    """Try APA supplemental files and convert doc/docx to PDF when available."""
    if not doi.startswith("10.1037/"):
        return False

    article_code = doi.split("/", 1)[1].strip().lower()
    if not re.fullmatch(r"[a-z0-9]+", article_code or ""):
        return False

    supp_page = f"https://supp.apa.org/psycarticles/supplemental/{article_code}/{article_code}_supp.html"
    try:
        r = requests.get(supp_page, timeout=12)
        if r.status_code != 200:
            return False

        links = re.findall(r'href=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
        candidates = []
        for href in links:
            href_lower = href.lower()
            if href_lower.endswith(".pdf") or href_lower.endswith(".docx") or href_lower.endswith(".doc"):
                if href.startswith("http"):
                    candidates.append(href)
                else:
                    candidates.append(f"https://supp.apa.org/psycarticles/supplemental/{article_code}/{href.lstrip('/')}")

        if verbose:
            print(f"  APA supplemental candidates: {len(candidates)}")

        for candidate in candidates:
            # Direct PDF supplemental
            if candidate.lower().endswith(".pdf"):
                if try_download(candidate, save_path, verbose):
                    if verbose:
                        print(f"✅ APA supplemental PDF success for {doi}")
                    return True
                continue

            # DOC/DOCX supplemental -> convert to PDF
            if candidate.lower().endswith(".doc") or candidate.lower().endswith(".docx"):
                tmp = requests.get(candidate, timeout=20)
                if tmp.status_code != 200 or len(tmp.content) < 100:
                    continue

                with tempfile.TemporaryDirectory() as tmpdir:
                    ext = ".docx" if candidate.lower().endswith(".docx") else ".doc"
                    input_path = os.path.join(tmpdir, f"{article_code}{ext}")
                    with open(input_path, "wb") as f:
                        f.write(tmp.content)

                    if shutil.which("soffice") is None:
                        if verbose:
                            print("  LibreOffice not found; cannot convert APA supplemental DOCX.")
                        continue

                    proc = subprocess.run(
                        [
                            "soffice",
                            "--headless",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            tmpdir,
                            input_path,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        if verbose:
                            print(f"  DOCX->PDF conversion failed: {proc.stderr[:120]}")
                        continue

                    converted_pdf = os.path.join(tmpdir, f"{article_code}.pdf")
                    if not os.path.exists(converted_pdf):
                        pdf_candidates = [p for p in os.listdir(tmpdir) if p.lower().endswith(".pdf")]
                        if not pdf_candidates:
                            continue
                        converted_pdf = os.path.join(tmpdir, pdf_candidates[0])

                    if os.path.getsize(converted_pdf) < 500:
                        continue
                    with open(converted_pdf, "rb") as rf:
                        if rf.read(4) != b"%PDF":
                            continue

                    shutil.copyfile(converted_pdf, save_path)
                    if verbose:
                        print(f"✅ APA supplemental DOCX converted to PDF for {doi}")
                    return True

    except Exception as e:
        if verbose:
            print(f"Error with APA supplemental fallback: {e}")
    return False




#-----------------------------------------------------------------------------------------
def fetch_pdf_from_doi(doi,
                       save_path,
                       email=None,
                       verbose=False,
                       delay=0.1,
                       _source_out=None,
                       _visited=None,
                      ):
    """
    Try to download a PDF for a DOI (or PMID resolved to DOI) using multiple fallbacks:
      0. OSF, Figshare, PsychArchives (if DOI matches pattern)
      1. PubMed Central (PMC)
      2. Unpaywall
      3. Crossref (direct PDF links or landing page)
      4. Europe PMC
      5. Semantic Scholar
      6. OpenAlex (strict rate limits: 1,000/day)
      7. CORE (core.ac.uk)
      8. Direct DOI resolver (html parsing, Crossref chooser)
      9. DataCite related identifiers
     10. DOI→PMID fallback

    Saves PDF to save_dir as: doi.replace('/', '--') + '.pdf'
    Returns the path if successful, else None.
    """
    # Use email from .env.local if not provided
    if email is None:
        email = _DEFAULT_EMAIL

    if _visited is None:
        _visited = set()

    if not isinstance(doi, str) or not doi.strip():
        print(f"ERROR with identifier: {doi}")
        return None

    original_identifier = doi
    pmid_input = extract_pmid(original_identifier)
    doi = resolve_identifier_to_doi(doi, verbose=verbose)
    if doi:
        if doi in _visited:
            if verbose:
                print(f"  Skipping {doi} — already attempted (cycle detected)")
            return None
        _visited.add(doi)
    if not doi:
        if pmid_input:
            # Fallback path when PMID has no DOI in metadata indexes.
            xml_path = os.path.splitext(save_path)[0] + ".xml"
            if os.path.exists(save_path) or os.path.exists(xml_path):
                existing = save_path if os.path.exists(save_path) else xml_path
                if verbose:
                    print(f"✅ Skipping PMID {pmid_input} - file already exists: {existing}")
                _record_source(_source_out, "existing")
                return existing
            time.sleep(delay)
            if verbose:
                print(f"  DOI unavailable for PMID {pmid_input}; trying PMID-native PDF fallbacks...")
            if try_pmid_direct_pdf_fallback(pmid_input, save_path, verbose):
                _record_source(_source_out, "pmid_direct")
                return save_path
        if verbose:
            print(f"  Could not normalize/resolve identifier: {original_identifier}")
        return None
    if verbose and doi != canonicalize_doi(str(original_identifier)):
        print(f"  Using DOI: {doi}")

    _pmid_cache = [None]  # Cached from PMC idconv when available; used for DOI->PMID fallback

    # Skip if PDF or XML already exists
    xml_path = os.path.splitext(save_path)[0] + ".xml"
    if os.path.exists(save_path) or os.path.exists(xml_path):
        existing = save_path if os.path.exists(save_path) else xml_path
        if verbose:
            print(f"✅ Skipping {doi} - file already exists: {existing}")
        _record_source(_source_out, "existing")
        return existing

    time.sleep(delay)

    # ---------------- 0  OSF DOI handling ----------------
    # Handles both OSF Projects (10.17605/osf.io/xxxxx) and OSF Preprints (10.31234/osf.io/xxxxx)
    if doi.lower().startswith("10.17605/osf.io") or doi.lower().startswith("10.31234/osf.io") or "osf.io" in doi.lower():
        if verbose:
            print(f"🔍 Trying OSF download for {doi}...")
        try:
            # Normalize DOI → OSF identifier
            osf_id = doi.split("/")[-1].replace("%2F", "").replace("OSF.IO", "").strip().lower()
            if not osf_id:
                osf_id = re.findall(r"osf\.io/([a-z0-9]+)", doi.lower())
                osf_id = osf_id[0] if osf_id else None

            if verbose:
                print(f"  OSF ID extracted: {osf_id}")

            if osf_id:
                # Method 1: Try simple direct download URLs first (fast, no browser needed)
                if verbose:
                    print(f"  Trying OSF direct URLs...")
                candidate_urls = [
                    f"https://osf.io/{osf_id}/download",
                    f"https://osf.io/download/{osf_id}/",
                ]

                for url in candidate_urls:
                    if try_download(url, save_path, verbose):
                        print(f"✅ OSF direct download success for {doi}")
                        _record_source(_source_out, "osf")
                        return save_path

                # OSF API file-provider fallback
                if _download_pdf_from_osf_api(osf_id, save_path, verbose):
                    _record_source(_source_out, "osf")
                    return save_path

        except Exception as e:
            print(f"⚠️ OSF download failed for {doi}: {e}")

    # ---------------- Figshare DOI handling ----------------
    if "figshare" in doi or doi.startswith("10.6084/"):
        try:
            # Extract article ID from DOI: 10.6084/m9.figshare.XXXXXXX.vN → XXXXXXX
            m = re.search(r"figshare\.(\d+)", doi)
            if m:
                article_id = m.group(1)
                r = requests.get(
                    f"https://api.figshare.com/v2/articles/{article_id}/files",
                    timeout=15,
                )
                if r.status_code == 200:
                    for fobj in r.json():
                        if (fobj.get("mimetype", "") == "application/pdf"
                                or fobj.get("name", "").lower().endswith(".pdf")):
                            dl_url = fobj.get("download_url")
                            if dl_url and try_download(dl_url, save_path, verbose):
                                print(f"✅ Figshare API success for {doi}")
                                _record_source(_source_out, "figshare")
                                return save_path
        except Exception as e:
            print(f"⚠️ Figshare download failed for {doi}: {e}")

    # ---------------- PsychArchives DOI handling ----------------
    # 10.23668/psycharchives.* - Leibniz psychology repository, PDFs via bitstream
    if "psycharchives" in doi.lower() or doi.startswith("10.23668/"):
        try:
            resolved = requests.get(
                f"https://doi.org/{doi}",
                headers=headers,
                timeout=12,
                allow_redirects=True,
            )
            if resolved.status_code == 200 and "psycharchives" in resolved.url.lower():
                item_html = resolved.text
                # Extract bitstream URLs (pada.psycharchives.org/bitstream/UUID)
                bitstream_urls = list(dict.fromkeys(
                    re.findall(
                        r'https?://[^"\'<>\s]*psycharchives[^"\'<>\s]*/bitstream/[a-f0-9\-]+',
                        item_html,
                        re.IGNORECASE,
                    )
                ))
                for bitstream_url in bitstream_urls[:5]:
                    if try_download(bitstream_url, save_path, verbose):
                        if verbose:
                            print(f"✅ PsychArchives bitstream success for {doi}")
                        _record_source(_source_out, "psycharchives")
                        return save_path
        except Exception as e:
            if verbose:
                print(f"Error with PsychArchives: {e}")

    # ---------------- PubMed Central (PMC) via Europe PMC ----------------
    # Many OA papers are freely available via PMC. Europe PMC's pdf=render
    # endpoint reliably serves PDFs (NCBI PMC uses JS redirects).
    try:
        r = _get_with_retries(
            _ncbi_url(f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={doi}&format=json&tool=replication_search&email={email}"),
            timeout=15,
        )
        if r.status_code == 200:
            records = r.json().get("records", [])
            if records:
                rec0 = records[0]
                pmcid = rec0.get("pmcid")
                if rec0.get("pmid") is not None:
                    _pmid_cache[0] = str(rec0["pmid"]).strip()
                if pmcid:
                    epmc_pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
                    if try_download(epmc_pdf_url, save_path, verbose):
                        if verbose: print(f"✅ PMC/EuropePMC success for {doi} ({pmcid})")
                        _record_source(_source_out, "pmc")
                        return save_path
    except Exception as e:
        if verbose: print(f"Error with PMC: {e}")

    # ---------------- eScholarship via PubMed LinkOut ----------------
    # UC and other universities deposit in eScholarship; PubMed abstract lists these
    try:
        if try_escholarship_via_pubmed(doi, save_path, verbose):
            _record_source(_source_out, "escholarship")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with eScholarship: {e}")

    # ---------------- Unpaywall ----------------
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            best = data.get("best_oa_location") or {}
            pdf_url = best.get("url_for_pdf") or best.get("url")
            if try_download(pdf_url, save_path, verbose):
                if verbose: print(f"✅ Unpaywall success for {doi}")
                _record_source(_source_out, "unpaywall")
                return save_path
    except Exception as e:
        if verbose: print(f"Error with Unpaywall: {e}")

    # ----------------  Crossref ----------------
    try:
        # Use polite pool for higher rate limits (10 req/s vs 5 req/s)
        crossref_params = {}
        if _DEFAULT_EMAIL:
            crossref_params["mailto"] = _DEFAULT_EMAIL
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            params=crossref_params,
            timeout=10
        )
        if r.status_code == 200:
            m = r.json().get("message", {})
            # Direct PDF links in Crossref metadata
            for link in m.get("link", []):
                if link.get("content-type") == "application/pdf":
                    if try_download(link.get("URL"), save_path, verbose):
                        if verbose: print(f"✅ Crossref direct link success for {doi}")
                        _record_source(_source_out, "crossref")
                        return save_path
            # Landing page fallback
            landing = m.get("URL")
            if try_download(landing, save_path, verbose):
                if verbose: print(f"✅ Crossref landing page worked for {doi}")
                _record_source(_source_out, "crossref")
                return save_path
            if landing and try_landing_page_pdf_fallback(doi, landing, save_path, verbose):
                if verbose:
                    print(f"✅ Crossref landing-page extraction success for {doi}")
                _record_source(_source_out, "crossref")
                return save_path
    except Exception as e:
        if verbose: print(f"Error with Crossref: {e}")

    # ----------------  Europe PMC ----------------
    try:
        r = _get_with_retries(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json",
            timeout=20,
        )
        if r.status_code == 200:
            results = r.json().get("resultList", {}).get("result", [])
            if results:
                full_urls = results[0].get("fullTextUrlList", {}).get("fullTextUrl", [])
                for u in full_urls:
                    if "pdf" in (u.get("url", "").lower()):
                        if try_download(u["url"], save_path, verbose):
                            if verbose: print(f"✅ EuropePMC success for {doi}")
                            _record_source(_source_out, "europepmc")
                            return save_path
    except Exception as e:
        if verbose: print(f"Error with Europe PMC: {e}")

    # ----------------  Semantic Scholar ----------------
    try:
        s2_headers = {}
        if _S2_API_KEY:
            s2_headers["x-api-key"] = _S2_API_KEY
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf",
            timeout=10,
            headers=s2_headers,
        )
        if r.status_code == 200:
            pdf_url = r.json().get("openAccessPdf", {}).get("url")
            if pdf_url:
                if try_download(pdf_url, save_path, verbose):
                    if verbose: print(f"✅ Semantic Scholar success for {doi}")
                    _record_source(_source_out, "semantic_scholar")
                    return save_path
                # URL may return HTML (e.g. OJS redirect, landing page); try landing-page extraction
                if try_landing_page_pdf_fallback(doi, pdf_url, save_path, verbose):
                    if verbose: print(f"✅ Semantic Scholar (landing-page) success for {doi}")
                    _record_source(_source_out, "semantic_scholar")
                    return save_path
                # PsyArXiv preprints are on OSF: psyarxiv.com serves HTML, osf.io serves PDF
                if "psyarxiv.com" in pdf_url.lower():
                    m = re.search(r"psyarxiv\.com/([a-zA-Z0-9]+)", pdf_url, re.IGNORECASE)
                    if m:
                        osf_url = f"https://osf.io/{m.group(1).lower()}/download"
                        if try_download(osf_url, save_path, verbose):
                            if verbose:
                                print(f"✅ Semantic Scholar (PsyArXiv→OSF) success for {doi}")
                            _record_source(_source_out, "semantic_scholar")
                            return save_path
    except Exception as e:
        if verbose: print(f"Error with Semantic Scholar: {e}")

    # ----------------OpenAlex ----------------
    # Note: OpenAlex has strict rate limits (1,000 downloads/day even with API key)
    # Placed after Semantic Scholar to preserve quota for harder-to-find papers
    try:
        openalex_headers = {}
        openalex_api_key = os.getenv("OPENALEXAPIKEY")
        if openalex_api_key:
            openalex_headers["Authorization"] = f"Bearer {openalex_api_key}"
        r = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            headers=openalex_headers,
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            best = data.get("best_oa_location") or {}
            pdf_url = best.get("url_for_pdf") or best.get("url")
            if try_download(pdf_url, save_path, verbose):
                if verbose: print(f"✅ OpenAlex success for {doi}")
                _record_source(_source_out, "openalex")
                return save_path
        # 404 and other non-200 are normal (work not in OpenAlex); no need to print
    except Exception as e:
        if verbose: print(f"Error with OpenAlex: {e}")

    # ---------------- CORE (core.ac.uk) ----------------
    try:
        if try_core_fallback(doi, save_path, verbose):
            _record_source(_source_out, "core")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with CORE fallback: {e}")

    # ---------------- DOAJ ----------------
    try:
        if try_doaj_fallback(doi, save_path, verbose):
            _record_source(_source_out, "doaj")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with DOAJ fallback: {e}")

    # ---------------- DataCite relation/content URLs ----------------
    try:
        if try_datacite_fallback(doi, save_path, verbose):
            _record_source(_source_out, "datacite")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with DataCite fallback: {e}")

    # ---------------- APA supplemental fallback ----------------
    try:
        if try_apa_supplemental_fallback(doi, save_path, verbose):
            _record_source(_source_out, "apa_supplemental")
            return save_path
    except Exception as e:
        if verbose:
            print(f"Error with APA supplemental fallback: {e}")

    # ---------------- Direct DOI resolver ----------------
    try:
        resolved_url = f"https://doi.org/{doi}"
        request_headers = {**headers, "Referer": resolved_url}
        r = requests.get(resolved_url, headers=request_headers, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            # Direct PDF response
            if "application/pdf" in r.headers.get("content-type", "").lower():
                with open(save_path, "wb") as f:
                    f.write(r.content)
                if verbose:  print(f"✅ Direct DOI PDF success for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path

            # Handle Crossref chooser page (multiple resolution)
            if "chooser.crossref.org" in r.url:
                # Extract primary-resource from debug JSON
                primary_match = re.search(r"'primary-resource':\s*'([^']+)'", r.text)
                if primary_match:
                    primary_resource = primary_match.group(1)
                    if verbose:
                        print(f"  Crossref chooser detected, following primary resource: {primary_resource}")
                    if try_landing_page_pdf_fallback(doi, primary_resource, save_path, verbose):
                        if verbose: print(f"✅ Found PDF via Crossref chooser primary resource for {doi}")
                        _record_source(_source_out, "direct_doi")
                        return save_path

                # Fallback: try all resource-line links
                resource_links = re.findall(r'<div class="resource-line">.*?<a href="([^"]+)"', r.text, re.DOTALL)
                for resource_link in resource_links:
                    if verbose:
                        print(f"  Trying Crossref chooser resource: {resource_link}")
                    if try_landing_page_pdf_fallback(doi, resource_link, save_path, verbose):
                        if verbose: print(f"✅ Found PDF via Crossref chooser resource for {doi}")
                        _record_source(_source_out, "direct_doi")
                        return save_path

            # Parse and follow landing-page PDF/download links.
            if try_landing_page_pdf_fallback(doi, r.url, save_path, verbose):
                if verbose: print(f"✅ Found PDF via DOI landing-page parsing for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path
        # doi.org may redirect to a 404 (e.g. Acta Biochimica Polonica migrated to Frontiers Partnerships)
        if r.status_code == 404 and doi.lower().startswith("10.18388/abp"):
            fp_url = f"https://www.frontierspartnerships.org/journals/acta-biochimica-polonica/articles/{doi}/pdf"
            if try_download(fp_url, save_path, verbose):
                if verbose:
                    print(f"✅ Frontiers Partnerships (migrated ABP) success for {doi}")
                _record_source(_source_out, "direct_doi")
                return save_path
    except Exception as e:
        if verbose: print(f"Error with Direct DOI: {e}")

    # ---------------- DataCite related identifiers fallback ----------------
    # IsSupplementTo: supplemental material → try to get supplement first, then main paper as fallback
    # IsIdenticalTo: same content, different ID (e.g. versioned 10.25384/sage.11807913.v1 → 10.25384/sage.11807913)
    # IsVersionOf: newer version of this DOI (e.g. preprint → published)
    try:
        r = requests.get(f"https://api.datacite.org/dois/{quote_plus(doi)}", timeout=10)
        if r.status_code == 200:
            rels = (r.json().get("data") or {}).get("attributes") or {}
            fallback_types = [
                ("issupplementto", "main paper (IsSupplementTo)"),
                ("isidenticalto", "identical (IsIdenticalTo)"),
                ("isversionof", "newer version (IsVersionOf)"),
            ]
            for rel_type, label in fallback_types:
                for rid in (rels.get("relatedIdentifiers") or []):
                    if (rid.get("relationType") or "").lower() == rel_type:
                        alt_doi = rid.get("relatedIdentifier")
                        if alt_doi and alt_doi.startswith("10.") and alt_doi != doi:
                            if rel_type == "issupplementto":
                                # Try to get the actual supplementary material first (what user requested)
                                supp_url = rels.get("url")
                                if supp_url and verbose:
                                    print(f"  Attempting supplementary material from: {supp_url[:80]}...")
                                got_supplement = False
                                if supp_url:
                                    supp_candidates = _collect_datacite_candidate_urls(rels)
                                    if not supp_candidates:
                                        supp_candidates = [supp_url]
                                    for cand in supp_candidates[:5]:
                                        if try_download(cand, save_path, verbose):
                                            got_supplement = True
                                            break
                                        if try_download_with_session(cand, save_path, verbose=verbose):
                                            got_supplement = True
                                            break
                                    if not got_supplement:
                                        m = re.search(r"figshare\.com/articles/[^/]+/(\d+)", supp_url, re.I)
                                        if m:
                                            art_id = m.group(1)
                                            base = supp_url.split("/articles")[0]
                                            ndl_url = f"{base}/ndownloader/articles/{art_id}"
                                            if try_download(ndl_url, save_path, verbose):
                                                got_supplement = True
                                if got_supplement:
                                    if verbose:
                                        print(f"✅ Supplementary material success for {doi}")
                                    _record_source(_source_out, "datacite_related")
                                    return save_path
                                # Could not get supplementary material—warn and try main paper
                                _print_yellow_warning(
                                    f"⚠️  Could not access supplementary material ({doi}). "
                                    f"It may be behind access controls or require browser interaction. "
                                    f"Fetching main paper {alt_doi} instead."
                                )
                            if verbose:
                                print(f"  Trying {label}: {alt_doi}")
                            alt_result = fetch_pdf_from_doi(
                                alt_doi, save_path, email, verbose, delay=0, _source_out=_source_out, _visited=_visited
                            )
                            if alt_result:
                                if verbose:
                                    print(f"✅ {label} success for {doi}")
                                _record_source(_source_out, "datacite_related")
                                return save_path
                            break  # only try first match per type
    except Exception as e:
        if verbose:
            print(f"Error with DataCite related identifiers fallback: {e}")

    # ---------------- DOI→PMID fallback (before last resorts) ----------------
    # When DOI flow fails, try PMID-native sources (PubMed page citation_pdf_url, etc.)
    if not os.path.exists(save_path):
        pmid = _pmid_cache[0]
        if pmid is None:
            pmid = doi_to_pmid(doi, verbose=verbose)
        if pmid:
            pmid_str = str(pmid).strip()
            if try_pmid_direct_pdf_fallback(pmid_str, save_path, verbose):
                _record_source(_source_out, "pmid_direct")
                return save_path



def _append_missing_to_report(output_dir, identifier, run_timestamp, lock):
    """
    Append a single failed identifier to missing_pdfs.html on the fly.
    Thread-safe: uses lock for file read/modify/write.
    """
    from datetime import datetime

    html_file = os.path.join(output_dir, "missing_pdfs.html")
    marker = "<!-- MISSING_PDFS_RUNS -->"

    safe_id = doi_to_safe_filename(identifier)
    expected_filename = f"{safe_id}.pdf"
    escaped_id = html.escape(identifier)
    escaped_filename = html.escape(expected_filename)
    pmid = extract_pmid(identifier)
    if pmid:
        link_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    else:
        link_url = f"https://doi.org/{identifier}"
    escaped_link = html.escape(link_url)
    row_html = f"""            <tr>
                <td><a href="{escaped_link}" target="_blank" rel="noopener noreferrer">{escaped_id}</a></td>
                <td><code>{escaped_filename}</code></td>
            </tr>
"""

    run_section = f"""
    <section class="run">
        <h2>Run: {run_timestamp}</h2>
        <p><strong>Missing in this run:</strong> 1</p>
        <table>
            <thead>
                <tr>
                    <th>Identifier (DOI/PMID)</th>
                    <th>Filename</th>
                </tr>
            </thead>
            <tbody>
{row_html}            </tbody>
        </table>
    </section>
"""

    with lock:
        escaped_output_dir = html.escape(output_dir)
        if not os.path.exists(html_file):
            full_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Missing PDFs Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f7f7f7;
            color: #1f2937;
        }}
        h1 {{ margin-bottom: 8px; }}
        .summary {{ margin-bottom: 20px; color: #4b5563; }}
        .run {{
            background: #fff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 14px;
            margin-bottom: 16px;
        }}
        .run h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
        .run p {{ margin: 0 0 10px 0; color: #4b5563; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
        th {{ font-weight: 600; color: #374151; background: #f9fafb; }}
        td code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>Missing PDFs Report</h1>
    <p class="summary">Output directory: <code>{escaped_output_dir}</code></p>
{run_section}
{marker}
</body>
</html>
"""
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(full_doc)
            return

        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        run_header = f"Run: {run_timestamp}"
        run_start = content.find(run_header)
        if run_start != -1:
            tbody_end = content.find("</tbody>", run_start)
            if tbody_end != -1:
                new_content = content[:tbody_end] + row_html + content[tbody_end:]
                section_end = content.find("</section>", run_start)
                if section_end != -1:
                    section = new_content[run_start:section_end]
                    section = re.sub(
                        r"(Missing in this run:</strong> )(\d+)",
                        lambda m: m.group(1) + str(int(m.group(2)) + 1),
                        section,
                        count=1,
                    )
                    new_content = new_content[:run_start] + section + new_content[section_end:]
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(new_content)
                return

        updated = content.replace(
            marker,
            run_section.rstrip() + "\n" + marker,
            1,
        )
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(updated)


def _append_failed_to_csv(output_dir, doi, category, detail, lock):
    """
    Append a single failed DOI to failed_dois.csv for machine-readable retry.
    Thread-safe. The resulting CSV can be passed directly to batch_fetch_pdfs()
    on a retry run, since it contains a "doi" column.

    Columns: timestamp, doi, category, detail
    """
    from datetime import datetime
    import csv as _csv

    csv_path = os.path.join(output_dir, "failed_dois.csv")
    with lock:
        is_new = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            if is_new:
                writer.writerow(["timestamp", "doi", "category", "detail"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                doi,
                category or "all_sources_failed",
                (detail or "")[:500],
            ])


def batch_fetch_pdfs(dois, output_dir, email=None, verbose=False, delay=0.1, workers=1, create_missing_report=True, track_source=False, start_offset=0, abstract_if_no_pdf=False, abstract_only=False):
    """
    Download PDFs for multiple DOIs with optional parallel processing.

    Args:
        dois: List of DOIs to download or path to CSV file
        output_dir: Directory to save PDFs
        email: Email for API calls (defaults to EMAIL from .env.local)
        verbose: Print detailed progress
        delay: Delay between API calls per worker
        workers: Number of parallel workers (1 = sequential)
        create_missing_report: Create HTML report for missing PDFs (default: True)
        track_source: If True, write CSV (pdf, source) and JSON (source counts) to output_dir

    Returns:
        List of tuples: (doi, success, save_path) or (doi, success, save_path, source) when track_source
    """
    # Use email from .env.local if not provided
    if email is None:
        email = _DEFAULT_EMAIL

    import os
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from io import StringIO
    import io

    # Ensure stdout uses UTF-8 encoding to handle Unicode characters (Greek letters, mathematical symbols, etc.)
    # This prevents 'latin-1' codec errors when printing URLs or metadata with non-ASCII characters
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    os.makedirs(output_dir, exist_ok=True)

    # If dois is a string, assume it's a CSV file path
    if isinstance(dois, str):
        import pandas as pd
        df = pd.read_csv(dois, encoding='utf-8')
        # Case-insensitive column lookup for DOI
        col_map = {c.lower(): c for c in df.columns}
        doi_col = col_map.get('doi')
        if doi_col:
            dois = df[doi_col].dropna().tolist()
        else:
            raise ValueError("CSV must have a 'DOI' column (case-insensitive)")

    results = []
    started_at = time.monotonic()
    progress_interval = 20

    # Thread-local storage for prefix
    _thread_prefix = threading.local()
    _print_lock = threading.Lock()
    _missing_report_lock = threading.Lock()
    # Running counter of failures by category, for stats display.
    # Uses the missing_report_lock above for thread-safe updates.
    from collections import Counter as _Counter
    _failure_counter = _Counter()
    _run_timestamp = None
    if create_missing_report:
        from datetime import datetime
        _run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _original_stdout = sys.stdout

    class PrefixedOutput:
        """Wrapper that adds prefix to all output."""
        def __init__(self, original):
            self.original = original

        def write(self, text):
            if text.strip():
                prefix = getattr(_thread_prefix, 'value', '')
                if prefix:
                    with _print_lock:
                        self.original.write(f"{prefix} {text}")
                else:
                    self.original.write(text)
            else:
                self.original.write(text)

        def flush(self):
            self.original.flush()

    def download_one(doi, idx, total):
        """Download a single DOI/PMID with progress tracking."""
        # idx is 0-based within the processed list, add start_offset for actual CSV row
        actual_row = start_offset + idx + 1
        total_rows = start_offset + total
        prefix = f"[{actual_row}/{total_rows}]"
        _thread_prefix.value = prefix

        raw_identifier = str(doi).strip()
        canonical_doi = resolve_identifier_to_doi(raw_identifier, verbose=verbose)
        pmid = extract_pmid(raw_identifier)

        # Use PMID digits for filename when DOI resolution fails
        if canonical_doi:
            display_id = canonical_doi
        elif pmid:
            display_id = pmid  # Just the digits, not "pmid:..." or "PMID ..."
        else:
            display_id = raw_identifier

        safe_doi = doi_to_safe_filename(display_id)
        save_path = os.path.join(output_dir, f"{safe_doi}.pdf")

        source_out = [None] if track_source else None

        xml_path = os.path.splitext(save_path)[0] + ".xml"
        ab_path = os.path.splitext(save_path)[0] + "_abstract.md"

        if abstract_only:
            if os.path.exists(ab_path):
                print(f"⏭️  Skipping {display_id} (abstract already exists)")
                if track_source:
                    return (display_id, True, ab_path, "existing")
                return (display_id, True, ab_path)
            try:
                from fetchpdf.fetch_abstract_from_doi import save_abstract_markdown
                saved = save_abstract_markdown(display_id, output_dir, email=email, verbose=verbose)
                if saved:
                    print(f"📄 {display_id} - abstract saved")
                    if track_source:
                        return (display_id, True, saved, "abstract")
                    return (display_id, True, saved)
                else:
                    print(f"❌ {display_id} - no abstract found")
            except Exception as e:
                print(f"❌ {display_id} - abstract error: {e}")
            if track_source:
                return (display_id, False, None, None)
            return (display_id, False, None)

        existing_file = save_path if os.path.exists(save_path) else (xml_path if os.path.exists(xml_path) else None)
        if existing_file:
            print(f"⏭️  Skipping {display_id} (already exists: {os.path.basename(existing_file)})")
            if track_source:
                return (display_id, True, existing_file, "existing")
            return (display_id, True, existing_file)

        print(f"📥 Downloading {display_id}...")
        result = fetch_pdf_from_doi(
            canonical_doi or raw_identifier, save_path, email, verbose, delay,
            _source_out=source_out
        )

        success = result is not None
        if success:
            print(f"✅ {display_id}")
        else:
            if abstract_if_no_pdf:
                try:
                    from fetchpdf.fetch_abstract_from_doi import save_abstract_markdown
                    ab_path = save_abstract_markdown(display_id, output_dir, email=email, verbose=verbose)
                    if ab_path:
                        print(f"📄 {display_id} - no PDF, abstract saved: {os.path.basename(ab_path)}")
                    else:
                        print(f"❌ {display_id} - Nothing worked 🙃🙃🙃")
                except Exception as e:
                    if verbose:
                        print(f"  Abstract fallback error: {e}")
                    print(f"❌ {display_id} - Nothing worked 🙃🙃🙃")
            else:
                print(f"❌ {display_id} - Nothing worked 🙃🙃🙃")
            if create_missing_report and _run_timestamp:
                _append_missing_to_report(output_dir, display_id, _run_timestamp, _missing_report_lock)
            # Always write machine-readable retry CSV (even without HTML report)
            try:
                _append_failed_to_csv(
                    output_dir, display_id,
                    category="all_sources_failed",
                    detail="",
                    lock=_missing_report_lock,
                )
                # Track for running failure summary
                with _missing_report_lock:
                    _failure_counter["all_sources_failed"] += 1
            except Exception as _e:
                if verbose:
                    print(f"  failed_dois.csv append error: {_e}")

        if track_source:
            return (display_id, success, result if success else None, source_out[0] if success else None)
        return (display_id, success, result if success else None)

    total = len(dois)

    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hrs, rem = divmod(seconds, 3600)
        mins, secs = divmod(rem, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"

    def _print_rate_update(done_count: int, total_count: int, success_count: int):
        if done_count == 0:
            return
        elapsed = max(time.monotonic() - started_at, 1.0)
        rate_per_hour = (done_count / elapsed) * 3600.0
        pct = (success_count / done_count * 100) if done_count else 0
        remaining = total_count - done_count
        eta_seconds = (remaining / done_count) * elapsed if done_count else 0
        # Append top failure categories if any failures seen
        failures_str = ""
        with _missing_report_lock:
            if _failure_counter:
                top = _failure_counter.most_common(3)
                parts = [f"{cat}={cnt}" for cat, cnt in top]
                failures_str = f" | failures: {', '.join(parts)}"
        print(
            f"⏱️  processing {rate_per_hour:.1f} / hour  "
            f"({done_count}/{total_count}, elapsed {_format_elapsed(elapsed)}, "
            f"ETA {_format_elapsed(eta_seconds)}, success {success_count}/{done_count} = {pct:.0f}%){failures_str}"
        )

    SOURCE_TRACK_INTERVAL = 10  # Write CSV/JSON every N completions

    def _write_source_tracking(entries_to_add):
        """Append to source_tracking.csv and merge into source_counts.json."""
        if not entries_to_add:
            return
        import csv
        import json
        csv_path = os.path.join(output_dir, "source_tracking.csv")
        json_path = os.path.join(output_dir, "source_counts.json")
        # Append to CSV (write header only if file is new)
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["pdf", "source"])
            if write_header:
                w.writeheader()
            w.writerows([{"pdf": p, "source": s} for p, s in entries_to_add])
        # Merge into JSON counts
        counts = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    counts = dict((data.get("source_counts") or {}))
            except (json.JSONDecodeError, OSError):
                pass
        for _path, _src in entries_to_add:
            counts[_src] = counts.get(_src, 0) + 1
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"source_counts": counts}, f, indent=2)

    source_entries = []  # (path, source) for successful downloads
    source_entries_written = 0
    completed_count = 0

    if workers <= 1:
        # Sequential processing - no need for prefix wrapper
        for idx, doi in enumerate(dois):
            r = download_one(doi, idx, total)
            results.append(r)
            if track_source and len(r) >= 4 and r[1] and r[2] and r[3] and r[3] != "existing":
                source_entries.append((r[2], r[3]))
            completed_count += 1
            if completed_count % SOURCE_TRACK_INTERVAL == 0 and track_source:
                to_add = source_entries[source_entries_written:]
                _write_source_tracking(to_add)
                source_entries_written = len(source_entries)
            done = idx + 1
            if done % progress_interval == 0 or done == total:
                success_so_far = sum(1 for r in results if r[1])
                _print_rate_update(done, total, success_so_far)
    else:
        # Parallel processing - install prefix wrapper
        sys.stdout = PrefixedOutput(_original_stdout)
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(download_one, doi, idx, total): idx
                    for idx, doi in enumerate(dois)
                }
                ordered_results = [None] * total
                completed = 0
                for future in as_completed(futures):
                    idx = futures[future]
                    r = future.result()
                    ordered_results[idx] = r
                    if track_source and len(r) >= 4 and r[1] and r[2] and r[3] and r[3] != "existing":
                        source_entries.append((r[2], r[3]))
                    completed += 1
                    if completed % SOURCE_TRACK_INTERVAL == 0 and track_source:
                        to_add = source_entries[source_entries_written:]
                        _write_source_tracking(to_add)
                        source_entries_written = len(source_entries)
                    if completed % progress_interval == 0 or completed == total:
                        success_so_far = sum(1 for r in ordered_results if r is not None and r[1])
                        _print_rate_update(completed, total, success_so_far)
                results = ordered_results
        finally:
            sys.stdout = _original_stdout

    # Print summary
    total = len(results)
    succeeded = sum(1 for r in results if r[1])
    failed = total - succeeded
    print(f"\n{'='*60}")
    print(f"  BATCH DOWNLOAD COMPLETE")
    print(f"  Total: {total}  |  Success: {succeeded}  |  Failed: {failed}  |  Rate: {succeeded/total*100:.0f}%" if total > 0 else "  No DOIs processed")
    elapsed_total = time.monotonic() - started_at
    print(f"  Total time: {_format_elapsed(elapsed_total)}")
    print(f"{'='*60}")

    # Missing PDFs report is appended on the fly; just show path if there were failures
    if create_missing_report and failed > 0:
        report_path = os.path.join(output_dir, "missing_pdfs.html")
        print(f"\n📄 Missing PDFs report: {report_path}")

    # Final source tracking write (catches remainder when total % interval != 0)
    if track_source and source_entries:
        to_add = source_entries[source_entries_written:]
        _write_source_tracking(to_add)
        csv_path = os.path.join(output_dir, "source_tracking.csv")
        json_path = os.path.join(output_dir, "source_counts.json")
        print(f"\n📊 Source tracking: {csv_path}")
        print(f"📊 Source counts:  {json_path}")

    return results


def main():
    """Command-line interface for fetch_pdf_from_doi."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Download PDFs from DOIs using multiple fallback sources.",
        epilog=(
            "Examples:\n"
            "  fetchpdf papers.csv -o ./pdfs                      # batch from CSV\n"
            "  fetchpdf papers.csv -o ./pdfs -w 4                 # batch, 4 workers\n"
            "  fetchpdf papers.csv -o ./pdfs --start-from-row 100 # resume from row 100\n"
            "  fetchpdf \"10.1038/nature12373\"                      # single DOI\n"
            "  fetchpdf \"10.1038/nature12373\" -o ./papers          # single DOI, custom dir\n"
            "  fetchpdf \"39804400\"                                 # PMID (auto-resolved)\n"
            "  fetchpdf --csv papers.csv --output-dir ./pdfs --workers 4\n"
            "  fetchpdf --pmid-csv pmids.csv -o ./pdfs"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="DOI, PMID, or CSV file path (auto-detected)")
    parser.add_argument("output_pos", nargs="?", help="Output file path for single DOI download")
    parser.add_argument(
        "--csv",
        help="CSV file with DOIs to process (for batch download)"
    )
    parser.add_argument(
        "--doi-column",
        default="DOI",
        help="Column name for DOI/PMID identifiers in CSV (default: DOI)"
    )
    parser.add_argument(
        "--pmid-csv",
        dest="pmid_csv",
        help="CSV file with PMIDs to process (for batch download)"
    )
    parser.add_argument(
        "--pmid-column",
        default="pmid",
        help="Column name for PMIDs in CSV when using --pmid-csv (default: pmid)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default="./pdfs",
        help="Output directory for downloads (default: ./pdfs)"
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Email for API calls (default: EMAIL from .env.local)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between API calls in seconds (default: 0.1)"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel workers for batch processing (default: 1)"
    )
    parser.add_argument(
        "--no-missing-report",
        action="store_true",
        help="Do not create HTML report for missing PDFs"
    )
    parser.add_argument(
        "--tracksource",
        action="store_true",
        help="Create source_tracking.csv (pdf, source) and source_counts.json in output dir"
    )
    parser.add_argument(
        "--start-from-row",
        type=int,
        default=0,
        help="Skip to row X in the CSV (0-indexed, default: 0). Useful for resuming interrupted batches."
    )
    parser.add_argument(
        "--abstract-if-no-pdf",
        action="store_true",
        help="When a PDF cannot be downloaded, fetch title+abstract and save as {DOI}_abstract.md"
    )
    parser.add_argument(
        "--abstract-only",
        action="store_true",
        help="Only fetch title+abstract (no PDF download). Save as {DOI}_abstract.md, skip if already exists."
    )

    args = parser.parse_args()

    # Auto-detect CSV from positional input arg
    if args.input and not args.csv and not args.pmid_csv:
        if args.input.lower().endswith('.csv') and os.path.isfile(args.input):
            args.csv = args.input
            args.input = None
        elif args.input.lower().endswith('.csv'):
            # File doesn't exist but looks like a CSV path
            print(f"❌ CSV file not found: {args.input}")
            return 1

    # Map positional args to legacy names for compatibility
    args.doi = args.input
    args.output = args.output_pos

    # Batch mode from CSV
    if args.csv:
        import pandas as pd

        df = pd.read_csv(args.csv)

        # Case-insensitive column lookup
        col_map = {c.lower(): c for c in df.columns}
        actual_col = col_map.get(args.doi_column.lower())
        if not actual_col:
            print(f"❌ CSV must have a '{args.doi_column}' column (case-insensitive)")
            return 1

        dois = df[actual_col].dropna().tolist()

        # Apply start_from_row skip
        if args.start_from_row > 0:
            if args.start_from_row >= len(dois):
                print(f"❌ --start-from-row {args.start_from_row} is beyond CSV length ({len(dois)} rows)")
                return 1
            print(f"⏭️  Skipping first {args.start_from_row} rows")
            dois = dois[args.start_from_row:]

        print(f"📚 Processing {len(dois)} identifiers from {args.csv}")
        print(f"💾 Output directory: {args.output_dir}")
        print(f"⚙️  Workers: {args.workers}")
        print("-" * 60)

        results = batch_fetch_pdfs(
            dois=dois,
            output_dir=args.output_dir,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
            workers=args.workers,
            create_missing_report=not args.no_missing_report,
            track_source=args.tracksource,
            start_offset=args.start_from_row,
            abstract_if_no_pdf=args.abstract_if_no_pdf,
            abstract_only=args.abstract_only,
        )

        success_count = sum(1 for r in results if r[1])
        failed = [r[0] for r in results if not r[1]]

        print("\n" + "=" * 60)
        print(f"✅ Success: {success_count}/{len(dois)} ({success_count/len(dois)*100:.1f}%)")

        if failed:
            print(f"\n❌ Failed DOIs ({len(failed)}):")
            for doi in failed[:10]:  # Show first 10 failures
                print(f"   - {doi}")
            if len(failed) > 10:
                print(f"   ... and {len(failed)-10} more")

        return 0 if success_count == len(dois) else 1

    # Batch mode from PMID CSV
    elif args.pmid_csv:
        import pandas as pd

        df = pd.read_csv(args.pmid_csv)

        if args.pmid_column not in df.columns:
            print(f"❌ CSV must have a '{args.pmid_column}' column")
            return 1

        identifiers = df[args.pmid_column].dropna().astype(str).str.strip().tolist()

        # Apply start_from_row skip and end_at_row limit
        if args.start_from_row > 0:
            if args.start_from_row >= len(identifiers):
                print(f"❌ --start-from-row {args.start_from_row} is beyond CSV length ({len(identifiers)} rows)")
                return 1
            print(f"⏭️  Skipping first {args.start_from_row} rows")
            identifiers = identifiers[args.start_from_row:]

        print(f"📚 Processing {len(identifiers)} PMIDs from {args.pmid_csv}")
        print(f"💾 Output directory: {args.output_dir}")
        print(f"⚙️  Workers: {args.workers}")
        print("-" * 60)

        results = batch_fetch_pdfs(
            dois=identifiers,
            output_dir=args.output_dir,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
            workers=args.workers,
            create_missing_report=not args.no_missing_report,
            track_source=args.tracksource,
            start_offset=args.start_from_row,
            abstract_if_no_pdf=args.abstract_if_no_pdf,
            abstract_only=args.abstract_only,
        )

        success_count = sum(1 for r in results if r[1])
        failed = [r[0] for r in results if not r[1]]

        print("\n" + "=" * 60)
        print(f"✅ Success: {success_count}/{len(identifiers)} ({success_count/len(identifiers)*100:.1f}%)")

        if failed:
            print(f"\n❌ Failed PMIDs ({len(failed)}):")
            for pid in failed[:10]:
                print(f"   - {pid}")
            if len(failed) > 10:
                print(f"   ... and {len(failed)-10} more")

        return 0 if success_count == len(identifiers) else 1

    # Single DOI mode
    elif args.doi:
        resolved_identifier = resolve_identifier_to_doi(args.doi, verbose=args.verbose)
        display_id = resolved_identifier or args.doi

        # Abstract-only mode for single DOI
        if args.abstract_only:
            from fetchpdf.fetch_abstract_from_doi import save_abstract_markdown
            os.makedirs(args.output_dir, exist_ok=True)
            safe_doi = doi_to_safe_filename(display_id)
            ab_path = os.path.join(args.output_dir, f"{safe_doi}_abstract.md")
            if os.path.exists(ab_path):
                print(f"⏭️  Abstract already exists: {ab_path}")
                return 0
            result = save_abstract_markdown(display_id, args.output_dir, email=args.email, verbose=args.verbose)
            if result:
                print(f"\n📄 Abstract saved to {result}")
                return 0
            else:
                print(f"\n❌ No abstract found for {display_id}")
                return 1

        if args.output:
            save_path = args.output
        else:
            # Default single DOI output path if not provided
            # Use PMID digits for filename when DOI resolution fails
            if resolved_identifier:
                filename_id = resolved_identifier
            else:
                pmid = extract_pmid(args.doi)
                filename_id = pmid if pmid else canonicalize_doi(args.doi)
            safe_doi = doi_to_safe_filename(filename_id)
            os.makedirs(args.output_dir, exist_ok=True)
            save_path = os.path.join(args.output_dir, f"{safe_doi}.pdf")
            print(f"💾 No output path provided; using: {save_path}")

        result = fetch_pdf_from_doi(
            doi=resolved_identifier or args.doi,
            save_path=save_path,
            email=args.email,
            verbose=args.verbose,
            delay=args.delay,
        )

        if result:
            print(f"\n✅ Successfully downloaded to {result}")
            return 0
        else:
            print(f"\n❌ Failed to download PDF for {args.doi}")
            return 1

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
