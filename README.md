# fetchpdf

A Python package to download academic papers (PDFs) from DOIs or PMIDs using multiple fallback sources.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Examples](#usage-examples)
- [Batch Processing](#batch-processing)
- [API Reference](#api-reference)
- [Download Sources](#download-sources)
- [Troubleshooting](#troubleshooting)

## Features

- **Multiple Sources**: Automatically tries 10+ sources including PubMed Central, Europe PMC, OpenAlex, Unpaywall, Crossref, Semantic Scholar, Figshare, CORE, and more
- **Smart Fallback**: If one source fails, automatically tries the next
- **Batch Processing**: Process multiple DOIs/PMIDs from CSV files or lists
- **Abstract Fallback**: Optionally save title+abstract as Markdown when a PDF cannot be found

## Installation

```bash
git clone https://github.com/The-Metascience-Observatory/fetchpdf.git
cd fetchpdf
pip install -e .
```

### Configuration: `.env.local`

Create a `.env.local` file in the project root to configure API keys. All keys are **optional** but improve rate limits and reliability:

```bash
# Recommended — used by Unpaywall (mandatory) and Crossref polite pool (10 req/s vs 5)
EMAIL=your@email.com

# Optional — improves rate limits / avoids throttling
OPENALEXAPIKEY=your_openalex_key           # OpenAlex: avoids 429 rate-limit errors
SEMANTIC_SCHOLAR_API_KEY=your_s2_key        # Semantic Scholar: 1 → 100 req/s
ENTREZ_EUTILS_API_KEY=your_ncbi_key         # NCBI E-utilities: 3 → 10 req/s
COREAPIKEY=your_core_key                   # CORE: 40M+ OA papers from core.ac.uk

# Optional — publisher-specific
ELSEVIER_TDM_API_KEY=your_elsevier_key      # Elsevier text/data-mining access
```

**Rate limit improvements with API keys:**

| API | Without Key | With Key | How to Get |
|-----|-------------|----------|------------|
| Crossref | 5 req/s | 10 req/s (polite pool) | Just set `EMAIL` |
| OpenAlex | Severe throttling | Normal | [openalex.org/users](https://openalex.org/users) |
| Semantic Scholar | 1 req/s | 100 req/s | [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) |
| NCBI E-utilities | 3 req/s | 10 req/s | [ncbi.nlm.nih.gov/account](https://www.ncbi.nlm.nih.gov/account/) |
| CORE | Unavailable | 1,000 tokens/day | [core.ac.uk/services/api](https://core.ac.uk/services/api) |

## Quick Start

### Command Line Interface

```bash
# Batch download from CSV (auto-detected by .csv extension)
fetchpdf papers.csv -o ./pdfs

# Single DOI or PMID
fetchpdf "10.1038/nature12373"             # saves to ./pdfs/
fetchpdf "10.1038/nature12373" -o ./papers # custom output dir
fetchpdf "10.1038/nature12373" output.pdf  # custom filename
fetchpdf 33262244                          # PMID auto-resolved to DOI
```

### Python API

```python
from fetchpdf import fetch_pdf_from_doi

result = fetch_pdf_from_doi(
    doi="10.1038/nature12373",
    save_path="./papers/paper.pdf",
    verbose=True,
)
```

### PMID Support

You can provide a PMID anywhere a DOI is accepted. The tool resolves PMID → DOI via NCBI, then runs the normal download flow.

```bash
fetchpdf "33262244" -o ./pdfs -v
fetchpdf "PMID:33262244" -o ./pdfs
fetchpdf "https://pubmed.ncbi.nlm.nih.gov/33262244/" -o ./pdfs
```

**Batch from CSV:**
```bash
fetchpdf dois.csv -o ./pdfs
fetchpdf papers.csv --doi-column "paper_doi" -o ./pdfs -w 2
```

## Usage Examples

### Download Multiple Papers

```python
from fetchpdf import fetch_pdf_from_doi
import os

dois = [
    "10.1038/nature12373",
    "10.1126/science.1241224",
    "10.1016/j.cell.2019.05.031"
]

output_dir = "./papers"
os.makedirs(output_dir, exist_ok=True)

for doi in dois:
    safe_doi = doi.replace("/", "--")
    save_path = os.path.join(output_dir, f"{safe_doi}.pdf")
    result = fetch_pdf_from_doi(doi, save_path, verbose=True)
    print("✅" if result else "❌", doi)
```

### Batch Processing

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./downloaded_papers",
    delay=0.2,
)

failed_dois = [doi for doi, success, _ in results if not success]
print(f"Failed: {len(failed_dois)} DOIs")
```

### Download with Metadata

```python
from fetchpdf import fetch_pdf_from_doi, fetch_metadata_from_doi

doi = "10.1038/nature12373"
metadata = fetch_metadata_from_doi(doi)
print(f"Downloading: {metadata['title']}")

safe_title = metadata['title'][:50].replace(" ", "_").replace("/", "_")
result = fetch_pdf_from_doi(doi, f"./papers/{safe_title}.pdf")
```

### Missing PDFs Report

When batch processing completes, an HTML report is created for any failed downloads:

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(dois="papers.csv", output_dir="./papers")
# Check ./papers/missing_pdfs.html — includes title, authors, clickable DOI links
```

To disable:
```python
results = batch_fetch_pdfs(dois="papers.csv", output_dir="./papers", create_missing_report=False)
```

Or via CLI:
```bash
fetchpdf papers.csv -o ./papers --no-missing-report
```

## Batch Processing

### CSV Format

```csv
DOI
10.1038/nature12373
10.1126/science.1241224
10.1016/j.cell.2019.05.031
```

### Best Practices


### Retry Failed Downloads

```python
results = batch_fetch_pdfs(dois=dois, output_dir="./papers")
failed_dois = [doi for doi, success, _ in results if not success]

if failed_dois:
    retry_results = batch_fetch_pdfs(
        dois=failed_dois, output_dir="./papers", delay=1.0, verbose=True
    )
```

## API Reference

### `fetch_pdf_from_doi(doi, save_path, email=None, verbose=False, delay=0.1)`

Download a PDF from a DOI or PMID.

**Returns:** Path to the saved PDF if successful, `None` otherwise.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doi` | str | — | DOI or PMID identifier |
| `save_path` | str | — | Full path where the PDF should be saved |
| `email` | str | `.env.local` | Email for API calls (Unpaywall, Crossref) |
| `verbose` | bool | `False` | Print detailed progress |
| `delay` | float | `0.1` | Delay between API calls in seconds |

---

### `batch_fetch_pdfs(dois, output_dir, email=None, verbose=False, delay=0.1, create_missing_report=True, track_source=False, start_offset=0, abstract_if_no_pdf=False, abstract_only=False)`

Download PDFs for multiple DOI/PMID identifiers.

**Returns:** List of `(doi, success, save_path)` tuples. When `track_source=True`, returns `(doi, success, save_path, source)`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `dois` | list or str | — | List of identifiers or path to CSV file |
| `output_dir` | str | — | Directory to save PDFs |
| `email` | str | `.env.local` | Email for API calls |
| `verbose` | bool | `False` | Print detailed progress |
| `delay` | float | `0.1` | Delay between API calls per worker |
| `create_missing_report` | bool | `True` | Create `missing_pdfs.html` for failures |
| `track_source` | bool | `False` | Write `source_tracking.csv` and `source_counts.json` |
| `start_offset` | int | `0` | Skip first N rows in CSV (resume interrupted batch) |
| `abstract_if_no_pdf` | bool | `False` | Save abstract as `.md` when PDF download fails |
| `abstract_only` | bool | `False` | Skip PDF; only fetch title+abstract |

---

### `fetch_metadata_from_doi(doi, email=None, delay=0.2)`

Fetch metadata for a paper from its DOI.

**Returns:** Dict with keys: `authors`, `title`, `journal`, `volume`, `issue`, `pages`, `year`, `url` (all may be `None`).

Sources tried in order: OpenAlex → DataCite → Crossref → Unpaywall → Europe PMC → Semantic Scholar.

### CLI Reference

```
fetchpdf [input] [output] [OPTIONS]
```

| Flag | Short | Description |
|------|-------|-------------|
| `--csv FILE` | | CSV file with DOIs to process |
| `--doi-column NAME` | | DOI column name in CSV (default: `DOI`) |
| `--pmid-csv FILE` | | CSV file with PMIDs |
| `--pmid-column NAME` | | PMID column name (default: `pmid`) |
| `--start-from-row N` | | Skip to row N in CSV (resume interrupted batch) |
| `--output-dir DIR` | `-o` | Output directory (default: `./pdfs`) |
| `--delay SECONDS` | | Delay between API calls (default: 0.1) |
| `--email EMAIL` | | Email for API calls |
| `--abstract-only` | | Fetch title+abstract only; save as `{doi}_abstract.md` |
| `--abstract-if-no-pdf` | | Save abstract as fallback when PDF fails |
| `--tracksource` | | Write `source_tracking.csv` and `source_counts.json` |
| `--no-missing-report` | | Disable HTML report for failed downloads |
| `--verbose` | `-v` | Print detailed progress |

## Download Sources

The tool tries sources in sequence, stopping on first success. Order is optimized to check fast/reliable sources before rate-limited ones.

### Pattern-Matched Handlers (run first when DOI matches)

- **OSF** (`10.17605/osf.io/*`, `10.31234/osf.io/*`) — Open Science Framework projects and preprints
- **Figshare** (`10.6084/m9.figshare.*`) — Figshare API for file metadata and download URLs
- **PsychArchives** (`10.23668/psycharchives.*`) — Leibniz psychology repository

### Standard Fallback Chain

1. **PubMed Central (PMC)** — DOI/PMID → PMCID → PDF; fast and reliable for biomedical papers
2. **Unpaywall** — Comprehensive legal open access aggregator (~23M papers); requires `EMAIL`
3. **Crossref** — Publisher metadata with direct PDF links; landing page parsing; publisher-specific URL patterns (Wiley, T&F, SAGE, MIT Press, etc.)
4. **Europe PMC** — European PubMed Central; complements PMC direct access
5. **Semantic Scholar** — 215M+ papers with `openAccessPdf` field; OJS landing page fallback
6. **OpenAlex**  — 1,000 requests/day limit; placed after other sources to preserve quota
7. **CORE** — 40M+ open access papers from repositories worldwide (requires `COREAPIKEY`)
8. **Direct DOI Resolver** — Follows `doi.org/{doi}` redirect; extracts PDF links from HTML; publisher-specific URL patterns
9. **DataCite** — Preprints, datasets, grey literature; versioned DOI resolution
10. **DOI → PMID Fallback** — Converts DOI to PMID when DOI sources fail; tries PubMed landing page and eScholarship
11. **Elsevier Fulltext XML** (last resort) — Returns XML, not PDF; opt-in via `allow_xml_fallback=True`; requires `ELSEVIER_TDM_API_KEY`

**Total: 11 standard sources + 3 pattern-matched handlers**

## Output Files

| File | When Created |
|------|-------------|
| `{safe_doi}.pdf` | Every successful download |
| `{safe_doi}_abstract.md` | When `--abstract-only` or `--abstract-if-no-pdf` |
| `missing_pdfs.html` | Batch mode; any failures (unless `--no-missing-report`) |
| `failed_dois.csv` | Batch mode; log with timestamp, doi, category, detail |
| `source_tracking.csv` | When `--tracksource` |
| `source_counts.json` | When `--tracksource` |

## Requirements

- Python 3.8+
- requests
- python-dotenv

## Troubleshooting

### Rate Limiting

If you're hitting rate limits:
- Increase the `delay` parameter (e.g., `delay=1.0`)
- Set `EMAIL` and API keys in `.env.local`

## Ethical Considerations

This tool is intended for:
- Accessing open access papers
- Retrieving papers you have legal access to
- Academic research and education

Please respect copyright laws and publisher terms of service in your jurisdiction.

## Acknowledgments

This package aggregates access to multiple open academic resources. Thanks to:
OpenAlex, Unpaywall, Crossref, PubMed Central, Semantic Scholar, CORE, and other open science initiatives.
