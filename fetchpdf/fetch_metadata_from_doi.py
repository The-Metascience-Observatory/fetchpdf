import requests
import time

# .env.local is loaded here, at import time -- see _env.py.
from ._env import (
    EMAIL as _DEFAULT_EMAIL,
    S2_API_KEY as _S2_API_KEY,
    OPENALEX_API_KEY as _OPENALEX_API_KEY,
)

def fetch_metadata_from_doi(doi, email=None, delay=0.2):
    """
    Progressive multi-API metadata enrichment:
    OpenAlex → DataCite → Crossref → Unpaywall → EuropePMC → Semantic Scholar
    Stops early if all fields are filled.
    """
    # Use email from .env.local if not provided
    if email is None:
        email = _DEFAULT_EMAIL

    if not isinstance(doi, str) or not doi.strip():
        return None

    doi = doi.strip()
    headers = {"User-Agent": "MetascienceObservatory/1.0"}
    meta = {k: None for k in ["authors", "title", "journal", "volume", "issue", "pages", "year", "url"]}

    def enrich(current, new):
        """Fill missing fields in current dict with non-empty values from new dict."""
        if not new:
            return current
        for k, v in new.items():
            if (current.get(k) in [None, "", "NaN"]) and (v not in [None, "", "NaN"]):
                current[k] = v
        return current

    def is_complete(m):
        """Check if all metadata fields are filled."""
        return all(m.get(k) not in [None, "", "NaN"] for k in m)

    # ---------- 1️⃣ OpenAlex ----------
    try:
        openalex_headers = dict(headers)
        if _OPENALEX_API_KEY:
            openalex_headers["Authorization"] = f"Bearer {_OPENALEX_API_KEY}"
        r = requests.get(
            f"https://api.openalex.org/works/https://doi.org/{doi}",
            timeout=10,
            headers=openalex_headers
        )
        if r.status_code == 200:
            data = r.json()
            # host_venue was removed from the OpenAlex schema; primary_location
            # replaces it. Reading the old key silently yielded journal=None and
            # a doi.org url for every record.
            primary = data.get("primary_location") or {}
            source = primary.get("source") or {}
            biblio = data.get("biblio") or {}
            oa = {
                "authors": "; ".join([a["author"]["display_name"] for a in data.get("authorships", [])]) or None,
                "title": data.get("title"),
                "journal": source.get("display_name"),
                "volume": biblio.get("volume"),
                "issue": biblio.get("issue"),
                "pages": biblio.get("first_page"),
                "year": data.get("publication_year"),
                "url": primary.get("landing_page_url") or f"https://doi.org/{doi}",
            }
            meta = enrich(meta, oa)
            if is_complete(meta):
                return meta
    except Exception:
        pass
    time.sleep(delay)

    # ---------- 2️⃣ DataCite ----------
    try:
        r = requests.get(f"https://api.datacite.org/dois/{doi.lower()}", timeout=10, headers=headers)
        if r.status_code == 200:
            d = r.json().get("data", {}).get("attributes", {})
            authors = []
            for a in d.get("creators", []):
                name = a.get("name") or f"{a.get('givenName','')} {a.get('familyName','')}".strip()
                if name:
                    authors.append(name)
            dc = {
                "authors": "; ".join(authors) or None,
                "title": (d.get("titles") or [{}])[0].get("title"),
                "journal": d.get("publisher"),
                "year": d.get("publicationYear"),
                "url": d.get("url") or f"https://doi.org/{doi}",
            }
            meta = enrich(meta, dc)
            if is_complete(meta):
                return meta
    except Exception:
        pass
    time.sleep(delay)

    # ---------- 3️⃣ Crossref ----------
    try:
        # Use polite pool for higher rate limits (10 req/s vs 5 req/s)
        crossref_params = {}
        if _DEFAULT_EMAIL:
            crossref_params["mailto"] = _DEFAULT_EMAIL
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            params=crossref_params,
            timeout=10,
            headers=headers
        )
        if r.status_code == 200:
            m = r.json()["message"]
            authors = []
            for a in m.get("author", []):
                parts = []
                if "given" in a: parts.append(a["given"])
                if "family" in a: parts.append(a["family"])
                name = " ".join(parts).strip()
                if name:
                    authors.append(name)
            year = (
                m.get("published-print", {}).get("date-parts", [[None]])[0][0]
                or m.get("published-online", {}).get("date-parts", [[None]])[0][0]
            )
            cr = {
                "authors": "; ".join(authors) or None,
                "title": (m.get("title") or [None])[0],
                "journal": (m.get("container-title") or [None])[0],
                "volume": m.get("volume"),
                "issue": m.get("issue"),
                "pages": m.get("page"),
                "year": year,
                "url": f"https://doi.org/{doi}",
            }
            meta = enrich(meta, cr)
            if is_complete(meta):
                return meta
    except Exception:
        pass
    time.sleep(delay)

    # ---------- 4️⃣ Unpaywall ----------
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=10, headers=headers)
        if r.status_code == 200:
            u = r.json()
            best_loc = u.get("best_oa_location") or {}
            authors = "; ".join(
                [f"{a.get('given','')} {a.get('family','')}".strip() for a in u.get("z_authors", [])]
            ) or None
            up = {
                "authors": authors,
                "title": u.get("title"),
                "journal": u.get("journal_name"),
                "volume": u.get("journal_volume"),
                "issue": u.get("journal_issue"),
                "pages": u.get("journal_pages"),
                "year": u.get("year"),
                "url": best_loc.get("url") or u.get("doi_url") or f"https://doi.org/{doi}",
            }
            meta = enrich(meta, up)
            if is_complete(meta):
                return meta
    except Exception:
        pass
    time.sleep(delay)

    # ---------- 5️⃣ Europe PMC ----------
    try:
        r = requests.get(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json().get("resultList", {}).get("result", [])
            if data:
                d = data[0]
                ep = {
                    "authors": d.get("authorString"),
                    "title": d.get("title"),
                    "journal": d.get("journalTitle"),
                    "volume": d.get("journalVolume"),
                    "issue": d.get("issue"),
                    "pages": d.get("pageInfo"),
                    "year": d.get("pubYear"),
                    "url": d.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0].get("url", f"https://doi.org/{doi}"),
                }
                meta = enrich(meta, ep)
                if is_complete(meta):
                    return meta
    except Exception:
        pass
    time.sleep(delay)

    # ---------- 6️⃣ Semantic Scholar ----------
    try:
        s2_headers = dict(headers)
        if _S2_API_KEY:
            s2_headers["x-api-key"] = _S2_API_KEY
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
            "?fields=title,year,venue,url,authors",
            timeout=10,
            headers=s2_headers,
        )
        if r.status_code == 200:
            s = r.json()
            ss = {
                "authors": "; ".join(a.get("name", "") for a in s.get("authors", [])) or None,
                "title": s.get("title"),
                "journal": s.get("venue"),
                "year": s.get("year"),
                "url": s.get("url") or f"https://doi.org/{doi}",
            }
            meta = enrich(meta, ss)
            if is_complete(meta):
                return meta
    except Exception:
        pass

    # ---------- Default fallback ----------
    if not meta["url"]:
        meta["url"] = f"https://doi.org/{doi}"
    return meta
