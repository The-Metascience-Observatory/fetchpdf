import os
import re
import time
import requests
from pathlib import Path
from urllib.parse import quote_plus
from dotenv import load_dotenv

from .fetch_pdf_from_doi import _get_with_retries

# Load environment variables from .env.local
env_file = Path(__file__).parent.parent / '.env.local'
if env_file.exists():
    load_dotenv(env_file)

_DEFAULT_EMAIL    = os.getenv("EMAIL")
_S2_API_KEY       = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
_NCBI_API_KEY     = os.getenv("ENTREZ_EUTILS_API_KEY")
_OPENALEX_API_KEY = os.getenv("OPENALEXAPIKEY")
_CORE_API_KEY     = os.getenv("COREAPIKEY")
_SCOPUS_API_KEY   = os.getenv("SCOPUS_API_KEY")

_HEADERS = {"User-Agent": "MetascienceObservatory/1.0"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_ABSTRACT_PATTERNS = re.compile(
    r'^(abstract\s+)?not\s+available|^no\s+abstract|^n/a$|^none$|^\[no\s+abstract\]',
    re.IGNORECASE,
)


_JUNK_ABSTRACT_KEYWORDS = [
    'cookie policy', 'cookies to enhance', 'sign in', 'purchase options',
    'subscribe to the', 'privacy policy', 'terms of use', 'access through your institution',
    'create an account', 'buy this article', 'rent this article',
]


def _is_empty_abstract(text):
    """Return True if text is a placeholder or junk content (cookie banners, login pages)."""
    if not text:
        return True
    t = text.strip()
    if _EMPTY_ABSTRACT_PATTERNS.match(t):
        return True
    tl = t.lower()
    # Website junk: cookie banners, login walls, nav menus
    if sum(1 for kw in _JUNK_ABSTRACT_KEYWORDS if kw in tl) >= 3:
        return True
    return False


def _strip_jats(text):
    """Remove JATS/HTML tags from abstract text, converting section headings to bold."""
    if not text:
        return None
    # Section headings → **Heading** (handles <h4>, <jats:title>, <strong>, <b>)
    text = re.sub(
        r'<(?:h\d|jats:title|strong|b)[^>]*>([^<]+)</(?:h\d|jats:title|strong|b)>',
        r'**\1** ', text, flags=re.IGNORECASE,
    )
    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or _is_empty_abstract(text):
        return None
    return text


def _reconstruct_openalex_abstract(inverted_index):
    """Reconstruct abstract string from OpenAlex inverted index format."""
    if not inverted_index:
        return None
    words = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words.keys())) or None


def _arxiv_id_from_doi(doi):
    """Extract arXiv ID from a 10.48550/arXiv.* DOI, or None."""
    m = re.search(r'10\.48550/[Aa]r[Xx]iv\.(\S+)', doi)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def fetch_abstract_from_doi(doi, email=None, delay=0.1, verbose=False):
    """
    Fetch title and abstract for a DOI using multiple API sources.

    Sources tried in order (stops as soon as title + abstract are both found):
      1.  Europe PMC          (abstractText, biomedical focus)
      2.  Crossref            (abstract field, JATS/HTML cleaned)
      3.  Semantic Scholar    (abstract field)
      4.  OpenAlex            (abstract_inverted_index reconstructed)
      5.  arXiv API           (for 10.48550/arXiv.* DOIs)
      6.  DataCite            (descriptions array, good for preprints/datasets)
      7.  CORE                (description field, requires COREAPIKEY)
      8.  DOAJ                (bibjson.abstract, open access journals)
      9.  Scopus              (dc:description, requires SCOPUS_API_KEY)
     10.  PubMed efetch       (DOI→PMID→plain-text abstract, biomedical fallback)

    Returns a dict with keys:
      title, abstract, authors, journal, year, doi
    All values may be None if unavailable.
    """
    if email is None:
        email = _DEFAULT_EMAIL

    doi = doi.strip()

    # Normalize filename-format DOIs: -- is our filename separator for /
    # e.g. "10.1093/eurheartj--ehaf339" → "10.1093/eurheartj/ehaf339"
    if '--' in doi:
        doi = doi.replace('--', '/')

    result = {
        "title":    None,
        "abstract": None,
        "authors":  None,
        "journal":  None,
        "year":     None,
        "doi":      doi,
    }

    def _enrich(new):
        for k, v in new.items():
            if v in (None, "", "NaN"):
                continue
            if k == "abstract" and _is_empty_abstract(v):
                continue
            cur = result.get(k)
            if cur in (None, "", "NaN") or (k == "abstract" and _is_empty_abstract(cur)):
                result[k] = v

    def _done():
        return bool(result["title"] and result["abstract"])

    # ------------------------------------------------------------------
    # 1. Europe PMC
    # ------------------------------------------------------------------
    try:
        r = _get_with_retries(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=DOI:{doi}&format=json&resultType=core",
            headers=_HEADERS, timeout=20,
        )
        if r.status_code == 200:
            hits = r.json().get("resultList", {}).get("result", [])
            if hits:
                d = hits[0]
                _enrich({
                    "title":    d.get("title"),
                    "abstract": _strip_jats(d.get("abstractText")) or None,
                    "authors":  d.get("authorString"),
                    "journal":  d.get("journalTitle"),
                    "year":     d.get("pubYear"),
                })
                if verbose and result["abstract"]:
                    print("  ✅ Abstract from Europe PMC")
    except Exception as e:
        if verbose: print(f"  Europe PMC error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 2. Crossref
    # ------------------------------------------------------------------
    try:
        params = {}
        if _DEFAULT_EMAIL:
            params["mailto"] = _DEFAULT_EMAIL
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            params=params, headers=_HEADERS, timeout=10,
        )
        if r.status_code == 200:
            m = r.json()["message"]
            authors = []
            for a in m.get("author", []):
                name = " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
                if name:
                    authors.append(name)
            year = (
                m.get("published-print", {}).get("date-parts", [[None]])[0][0]
                or m.get("published-online", {}).get("date-parts", [[None]])[0][0]
            )
            abstract = _strip_jats(m.get("abstract"))
            _enrich({
                "title":    (m.get("title") or [None])[0],
                "abstract": abstract,
                "authors":  "; ".join(authors) or None,
                "journal":  (m.get("container-title") or [None])[0],
                "year":     year,
            })
            if verbose and abstract:
                print("  ✅ Abstract from Crossref")
    except Exception as e:
        if verbose: print(f"  Crossref error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 3. Semantic Scholar
    # ------------------------------------------------------------------
    try:
        s2_headers = dict(_HEADERS)
        if _S2_API_KEY:
            s2_headers["x-api-key"] = _S2_API_KEY
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
            "?fields=title,abstract,year,authors,venue",
            headers=s2_headers, timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            _enrich({
                "title":    d.get("title"),
                "abstract": d.get("abstract"),
                "authors":  "; ".join(a.get("name", "") for a in d.get("authors", [])) or None,
                "journal":  d.get("venue"),
                "year":     d.get("year"),
            })
            if verbose and d.get("abstract"):
                print("  ✅ Abstract from Semantic Scholar")
    except Exception as e:
        if verbose: print(f"  Semantic Scholar error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 4. OpenAlex
    # ------------------------------------------------------------------
    try:
        oa_headers = dict(_HEADERS)
        if _OPENALEX_API_KEY:
            oa_headers["Authorization"] = f"Bearer {_OPENALEX_API_KEY}"
        r = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            headers=oa_headers, timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            abstract = _reconstruct_openalex_abstract(d.get("abstract_inverted_index"))
            authors = "; ".join(
                a["author"]["display_name"]
                for a in d.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ) or None
            _enrich({
                "title":    d.get("title"),
                "abstract": abstract,
                "authors":  authors,
                "journal":  (d.get("primary_location") or {}).get("source", {}).get("display_name"),
                "year":     d.get("publication_year"),
            })
            if verbose and abstract:
                print("  ✅ Abstract from OpenAlex")
    except Exception as e:
        if verbose: print(f"  OpenAlex error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 5. arXiv API  (only for 10.48550/arXiv.* DOIs)
    # ------------------------------------------------------------------
    arxiv_id = _arxiv_id_from_doi(doi)
    if arxiv_id:
        try:
            r = requests.get(
                f"http://export.arxiv.org/api/query?id_list={arxiv_id}",
                headers=_HEADERS, timeout=10,
            )
            if r.status_code == 200:
                xml = r.text
                title_m = re.search(r'<title>(.+?)</title>', xml, re.DOTALL)
                summary_m = re.search(r'<summary>(.+?)</summary>', xml, re.DOTALL)
                authors_m = re.findall(r'<name>(.+?)</name>', xml)
                if title_m and summary_m:
                    _enrich({
                        "title":    re.sub(r'\s+', ' ', title_m.group(1)).strip(),
                        "abstract": re.sub(r'\s+', ' ', summary_m.group(1)).strip(),
                        "authors":  "; ".join(authors_m) or None,
                        "journal":  "arXiv",
                    })
                    if verbose:
                        print(f"  ✅ Abstract from arXiv ({arxiv_id})")
        except Exception as e:
            if verbose: print(f"  arXiv error: {e}")
        time.sleep(delay)
        if _done(): return result

    # ------------------------------------------------------------------
    # 6. DataCite  (good for preprints, datasets, grey literature)
    # ------------------------------------------------------------------
    try:
        r = requests.get(
            f"https://api.datacite.org/dois/{quote_plus(doi)}",
            headers=_HEADERS, timeout=10,
        )
        if r.status_code == 200:
            attrs = r.json().get("data", {}).get("attributes", {})
            # Abstract is in descriptions array with descriptionType "Abstract"
            abstract = None
            for desc in attrs.get("descriptions", []):
                if (desc.get("descriptionType") or "").lower() == "abstract":
                    abstract = _strip_jats(desc.get("description"))
                    break
            # Fallback: first description of any type
            if not abstract and attrs.get("descriptions"):
                abstract = _strip_jats(attrs["descriptions"][0].get("description"))
            creators = attrs.get("creators", [])
            authors = "; ".join(
                c.get("name") or f"{c.get('givenName','')} {c.get('familyName','')}".strip()
                for c in creators if c.get("name") or c.get("familyName")
            ) or None
            _enrich({
                "title":    (attrs.get("titles") or [{}])[0].get("title"),
                "abstract": abstract,
                "authors":  authors,
                "journal":  attrs.get("publisher"),
                "year":     attrs.get("publicationYear"),
            })
            if verbose and abstract:
                print("  ✅ Abstract from DataCite")
    except Exception as e:
        if verbose: print(f"  DataCite error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 7. CORE  (40M+ OA papers, requires COREAPIKEY)
    # ------------------------------------------------------------------
    if _CORE_API_KEY:
        try:
            r = requests.get(
                f"https://api.core.ac.uk/v3/search/works"
                f"?q=doi:{quote_plus(doi)}&limit=1",
                headers={**_HEADERS, "Authorization": f"Bearer {_CORE_API_KEY}"},
                timeout=10,
            )
            if r.status_code == 200:
                hits = r.json().get("results", [])
                if hits:
                    d = hits[0]
                    abstract = _strip_jats(d.get("abstract") or d.get("description"))
                    _enrich({
                        "title":    d.get("title"),
                        "abstract": abstract,
                        "authors":  "; ".join(
                            a.get("name", "") for a in d.get("authors", [])
                        ) or None,
                        "journal":  (d.get("journals") or [{}])[0].get("title") or d.get("publisher"),
                        "year":     d.get("yearPublished"),
                    })
                    if verbose and abstract:
                        print("  ✅ Abstract from CORE")
        except Exception as e:
            if verbose: print(f"  CORE error: {e}")
        time.sleep(delay)
        if _done(): return result

    # ------------------------------------------------------------------
    # 8. DOAJ  (Directory of Open Access Journals, no key required)
    # ------------------------------------------------------------------
    try:
        r = requests.get(
            f"https://doaj.org/api/search/articles/doi:{quote_plus(doi)}",
            headers=_HEADERS, timeout=10,
        )
        if r.status_code == 200:
            hits = r.json().get("results", [])
            if hits:
                bib = hits[0].get("bibjson", {})
                abstract = _strip_jats(bib.get("abstract"))
                authors = "; ".join(
                    a.get("name", "") for a in bib.get("author", [])
                ) or None
                journal_info = bib.get("journal", {})
                _enrich({
                    "title":    bib.get("title"),
                    "abstract": abstract,
                    "authors":  authors,
                    "journal":  journal_info.get("title"),
                    "year":     bib.get("year"),
                })
                if verbose and abstract:
                    print("  ✅ Abstract from DOAJ")
    except Exception as e:
        if verbose: print(f"  DOAJ error: {e}")
    time.sleep(delay)
    if _done(): return result

    # ------------------------------------------------------------------
    # 9. Scopus  (Elsevier, requires SCOPUS_API_KEY)
    # ------------------------------------------------------------------
    if _SCOPUS_API_KEY:
        try:
            r = requests.get(
                f"https://api.elsevier.com/content/abstract/doi/{doi}",
                params={"apiKey": _SCOPUS_API_KEY, "httpAccept": "application/json"},
                headers=_HEADERS, timeout=10,
            )
            if r.status_code == 200:
                core = (
                    r.json()
                    .get("abstracts-retrieval-response", {})
                    .get("coredata", {})
                )
                abstract = _strip_jats(core.get("dc:description"))
                # Authors: authorship list
                auth_list = core.get("dc:creator") or []
                if isinstance(auth_list, dict):
                    auth_list = [auth_list]
                authors = "; ".join(
                    a.get("$", "") for a in auth_list if a.get("$")
                ) or None
                _enrich({
                    "title":    core.get("dc:title"),
                    "abstract": abstract,
                    "authors":  authors,
                    "journal":  core.get("prism:publicationName"),
                    "year":     (core.get("prism:coverDate") or "")[:4] or None,
                })
                if verbose and abstract:
                    print("  ✅ Abstract from Scopus")
        except Exception as e:
            if verbose: print(f"  Scopus error: {e}")
        time.sleep(delay)
        if _done(): return result

    # ------------------------------------------------------------------
    # 10. PubMed efetch  (DOI → PMID → structured XML abstract)
    # ------------------------------------------------------------------
    try:
        # Step 1: DOI → PMID via esearch
        search_params = {"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"}
        if _NCBI_API_KEY:
            search_params["api_key"] = _NCBI_API_KEY
        if email:
            search_params["email"] = email
        r = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=search_params, headers=_HEADERS, timeout=10,
        )
        if r.status_code == 200:
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            if ids:
                pmid = ids[0]
                # Step 2: PMID → structured XML via efetch
                fetch_params = {
                    "db": "pubmed", "id": pmid,
                    "rettype": "xml", "retmode": "xml",
                }
                if _NCBI_API_KEY:
                    fetch_params["api_key"] = _NCBI_API_KEY
                r2 = requests.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params=fetch_params, headers=_HEADERS, timeout=10,
                )
                if r2.status_code == 200:
                    xml = r2.text
                    # Extract abstract text (may have multiple AbstractText elements with labels)
                    parts = re.findall(
                        r'<AbstractText(?:[^>]*Label="([^"]*)")?[^>]*>([^<]+)</AbstractText>',
                        xml, re.DOTALL,
                    )
                    if parts:
                        sections = []
                        for label, text in parts:
                            text = re.sub(r'\s+', ' ', text).strip()
                            if label:
                                sections.append(f"**{label}** {text}")
                            else:
                                sections.append(text)
                        abstract = " ".join(sections)
                        _enrich({"abstract": abstract or None})
                        if verbose and abstract:
                            print(f"  ✅ Abstract from PubMed (PMID {pmid})")
    except Exception as e:
        if verbose: print(f"  PubMed efetch error: {e}")

    return result


# ---------------------------------------------------------------------------
# Formatting & saving
# ---------------------------------------------------------------------------

def format_abstract_markdown(meta, doi=None):
    """Format a metadata dict as a Markdown string."""
    lines = []

    title = meta.get("title") or "Untitled"
    lines.append(f"# {title}\n")

    info = []
    if meta.get("authors"):
        info.append(f"**Authors:** {meta['authors']}")
    if meta.get("journal"):
        info.append(f"**Journal:** {meta['journal']}")
    if meta.get("year"):
        info.append(f"**Year:** {meta['year']}")
    doi_val = doi or meta.get("doi")
    if doi_val:
        info.append(f"**DOI:** [{doi_val}](https://doi.org/{doi_val})")

    if info:
        lines.append("  \n".join(info))
        lines.append("")

    abstract = meta.get("abstract")
    if abstract:
        lines.append("## Abstract\n")
        lines.append(abstract)
    else:
        lines.append("*Abstract not available.*")

    return "\n".join(lines) + "\n"


def save_abstract_markdown(doi, output_dir, email=None, verbose=False):
    """
    Fetch abstract for a DOI and save as {safe_doi}_abstract.md in output_dir.
    Returns the path if saved, else None.
    """
    from fetchpdf.fetch_pdf_from_doi import doi_to_safe_filename
    os.makedirs(output_dir, exist_ok=True)
    safe = doi_to_safe_filename(doi)
    path = os.path.join(output_dir, f"{safe}_abstract.md")

    meta = fetch_abstract_from_doi(doi, email=email, verbose=verbose)
    if not meta.get("title") and not meta.get("abstract"):
        return None

    md = format_abstract_markdown(meta, doi=doi)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path
