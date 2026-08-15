# fetchpdf

A comprehensive Python package to download academic papers (PDFs) from DOIs (or PMIDs resolved to DOIs) using multiple fallback sources.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Examples](#usage-examples)
- [Batch Processing & Parallel Execution](#batch-processing--parallel-execution)
- [Format-Prioritized Retrieval](#format-prioritized-retrieval)
- [Supplementary Material](#supplementary-material)
- [API Reference](#api-reference)
- [Download Sources](#download-sources-in-order-of-priority)
- [Troubleshooting](#troubleshooting)

## Features

- 🔍 **Multiple Sources**: Automatically tries 10+ sources including:
  - OSF (Open Science Framework) - Projects & Preprints
  - PubMed Central / Europe PMC
  - OpenAlex
  - Unpaywall
  - Crossref
  - Semantic Scholar
  - Figshare
  - SSRN (Social Science Research Network)

- 🔄 **Smart Fallback**: If one source fails, automatically tries the next
- 🧬 **Format Prioritization**: `--prioritize-xml` prefers structured full text (JATS/TEI) over PDF — see [below](#format-prioritized-retrieval)
- 📎 **Supplementary Material**: `--pull-supplementary` fetches every supplementary file alongside the paper — see [below](#supplementary-material)
- 🔗 **Linked Datasets & Code**: the same pass discovers each paper's external datasets/software via ScholeXplorer, Europe PMC and DataCite, downloads ownership-confirmed deposits from figshare/Zenodo/OSF/Dryad/Dataverse, and records every link in a sidecar — see [below](#linked-datasets-and-code-stem_linked_artifactsjson)
- 🎭 **Browser Automation**: Uses Playwright to bypass JavaScript-based protections
- 🚀 **Batch Processing**: Process multiple DOIs/PMIDs from CSV files or lists
- ⚡ **Parallel Execution**: Download multiple papers simultaneously with configurable workers

## Installation

```bash
git clone https://github.com/yourusername/fetchpdf.git
cd fetchpdf
pip install -e .
```

### Post-Installation: Install Playwright Browsers

After installing the package, you need to install Playwright browsers:

```bash
pip install playwright
playwright install chromium
```

### Configuration: `.env.local`

Create a `.env.local` file in the project root to configure API keys and settings. API keys are **optional** but significantly improve rate limits and reliability:

```bash
# Required — used by Unpaywall (mandatory) and Crossref polite pool (10 req/s vs 5)
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
fetchpdf papers.csv -o ./pdfs -w 4        # 4 parallel workers

# Single DOI or PMID
fetchpdf "10.1038/nature12373"             # saves to ./pdfs/
fetchpdf "10.1038/nature12373" -o ./papers # custom output dir
fetchpdf "10.1038/nature12373" output.pdf  # custom filename
fetchpdf 33262244                          # PMID auto-resolved to DOI
```

### Python API

```python
from fetchpdf import fetch_pdf

# Download a PDF from a DOI (PMID also supported)
doi = "10.1038/nature12373"
save_path = "./papers/nature_paper.pdf"

result = fetch_pdf(
    doi=doi,
    save_path=save_path,
    verbose=True,
    delay=0.1  # Polite delay between API calls
)

```



### PMID Support

You can provide a PMID anywhere a DOI is accepted. The tool first resolves PMID -> DOI (via NCBI), then runs the normal DOI download flow.

**CLI examples:**
```bash
# Plain PMID
fetchpdf "33262244" -o ./pdfs -v

# PMID with prefix
fetchpdf "PMID:33262244" -o ./pdfs

# PubMed URL
fetchpdf "https://pubmed.ncbi.nlm.nih.gov/33262244/" -o ./pdfs
```

If a PMID cannot be resolved to a DOI, that identifier will fail quickly and be reported in batch mode.

**Batch Processing from CSV:**
```bash
# Sequential processing
fetchpdf dois.csv -o ./pdfs

# Parallel processing with 4 workers
fetchpdf dois.csv -o ./pdfs -w 4

# Custom DOI column name
fetchpdf papers.csv --doi-column "paper_doi" -o ./pdfs -w 2
```


## Usage Examples

### Example 1: Download Multiple Papers

```python
from fetchpdf import fetch_pdf
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

    print(f"Downloading {doi}...")
    result = fetch_pdf(doi, save_path, verbose=True)

    if result:
        print(f"✅ Success: {save_path}")
    else:
        print(f"❌ Failed: {doi}")
```

### Example 2: Batch Processing with Parallel Execution From CSV

```python
from fetchpdf import batch_fetch_pdfs

# Parallel processing with 4 workers
results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./downloaded_papers",
    verbose=False,
    workers=4,  # 4 parallel downloads
    delay=0.2   # Delay per worker
)

# Get failed DOIs
failed_dois = [doi for doi, success, _ in results if not success]
print(f"Failed: {len(failed_dois)} DOIs")

# Save failed DOIs for retry
if failed_dois:
    import pandas as pd
    pd.DataFrame({"DOI": failed_dois}).to_csv("failed_dois.csv", index=False)
```

### Example 3: Batch Processing with DOI List

```python
from fetchpdf import batch_fetch_pdfs

# Process a list of DOIs directly
dois = [
    "10.1038/nature12373",
    "10.1126/science.1241224",
    "10.1016/j.cell.2019.05.031"
]

results = batch_fetch_pdfs(
    dois=dois,  # Pass list directly
    output_dir="./papers",
    workers=3  # Use 3 parallel workers
)
```


### Example 3: Download with Metadata

```python
from fetchpdf import fetch_pdf, fetch_metadata_from_doi
import os

doi = "10.1038/nature12373"

# Get metadata first
metadata = fetch_metadata_from_doi(doi)
print(f"Downloading: {metadata['title']}")

# Use metadata for filename
safe_title = metadata['title'][:50].replace(" ", "_").replace("/", "_")
save_path = f"./papers/{safe_title}.pdf"

# Download PDF
result = fetch_pdf(doi, save_path)

if result:
    print(f"✅ Downloaded: {metadata['title']}")
    print(f"   Authors: {metadata['authors']}")
    print(f"   Year: {metadata['year']}")
```

### Example 4: Using the Missing PDFs Report

When batch processing completes, an HTML report is automatically created for any failed downloads:

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./papers",
    workers=4
)

# After completion, check ./papers/missing_pdfs.html
# The report includes:
# - Paper title, authors, journal, year
# - Clickable DOI links
# - Expected filename for manual download
```

**The report helps you:**
- Quickly identify which papers failed
- Click DOI links to manually download
- See expected filenames for manual organization
- View paper metadata even without the PDF

To disable the report:
```python
results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./papers",
    create_missing_report=False  # Disable report
)
```

Or via command line:
```bash
fetchpdf papers.csv -o ./papers --no-missing-report
```

## Batch Processing & Parallel Execution

This package supports both **batch processing** and **parallel execution** for downloading multiple PDFs efficiently.

### Batch Processing Capabilities

#### 1. Process List of DOIs

**Python API:**
```python
from fetchpdf import batch_fetch_pdfs

dois = ["10.1038/nature12373", "10.1126/science.1241224", "10.1016/j.cell.2019.05.031"]

results = batch_fetch_pdfs(
    dois=dois,
    output_dir="./papers",
    workers=1  # Sequential
)
```

#### 2. Process CSV File

**CSV format:**
```csv
DOI
10.1038/nature12373
10.1126/science.1241224
10.1016/j.cell.2019.05.031
```

**Python API:**
```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois="papers.csv",  # Path to CSV
    output_dir="./papers",
    workers=1
)
```

**Command Line:**
```bash
fetchpdf papers.csv -o ./papers
```

#### 3. Custom DOI Column Name

If your CSV uses a different column name:

```bash
fetchpdf data.csv --doi-column "paper_doi" -o ./pdfs
```

### Parallel Execution

#### Why Use Parallel Execution?

- **Faster downloads**: Download multiple PDFs simultaneously
- **Better resource utilization**: Make use of network I/O waiting time
- **Configurable workers**: Control parallelism based on your needs

#### Performance Comparison

**Example: Downloading 100 PDFs**

| Workers | Time | Speedup |
|---------|------|---------|
| 1 (sequential) | ~500 seconds | 1x |
| 2 workers | ~260 seconds | 1.9x |
| 4 workers | ~140 seconds | 3.6x |
| 8 workers | ~80 seconds | 6.3x |

*Note: Actual speedup depends on network speed, API rate limits, and source availability.*

#### Python API - Parallel

```python
from fetchpdf import batch_fetch_pdfs

# Use 4 parallel workers
results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./papers",
    workers=4,  # 4 parallel downloads
    delay=0.2   # Delay between API calls per worker
)
```

#### Command Line - Parallel

```bash
# Use 4 parallel workers
fetchpdf papers.csv -o ./papers -w 4
```

### Best Practices

#### 1. Start with Sequential Processing

For small batches (< 20 DOIs), sequential processing is sufficient:

```python
results = batch_fetch_pdfs(dois=dois, output_dir="./papers", workers=1)
```

#### 2. Use Moderate Parallelism

For large batches, use 2-4 workers:

```python
results = batch_fetch_pdfs(dois=dois, output_dir="./papers", workers=4)
```

**Why not more?**
- API rate limits
- Respectful to servers
- Diminishing returns beyond 4-8 workers

#### 3. Increase Delay for Larger Workers

More workers = more simultaneous requests. Be polite to APIs:

```python
results = batch_fetch_pdfs(
    dois=dois,
    output_dir="./papers",
    workers=8,
    delay=0.5  # Longer delay with more workers
)
```

#### 4. Handle Failures and Retry

```python
# First attempt
results = batch_fetch_pdfs(dois=dois, output_dir="./papers", workers=4)

# Get failed DOIs
failed_dois = [doi for doi, success, _ in results if not success]

if failed_dois:
    print(f"Retrying {len(failed_dois)} failed DOIs...")

    # Retry with sequential processing and longer delay
    retry_results = batch_fetch_pdfs(
        dois=failed_dois,
        output_dir="./papers",
        workers=1,
        delay=1.0,
        verbose=True  # See what's happening
    )
```

#### 5. Save Results for Tracking

```python
import pandas as pd

results = batch_fetch_pdfs(dois="papers.csv", output_dir="./papers", workers=4)

# Create results DataFrame
df = pd.DataFrame([
    {"DOI": doi, "Success": success, "Path": path}
    for doi, success, path in results
])

# Save results
df.to_csv("download_results.csv", index=False)

# Statistics
print(f"Success: {df['Success'].sum()}/{len(df)}")
print(f"Success rate: {df['Success'].mean()*100:.1f}%")
```

## Format-Prioritized Retrieval

The default chain walks *sources* and takes the first PDF each one yields. That
is right for a paper you want to read, and wrong for feeding a table-extraction
pipeline: what breaks there is not character accuracy but row/column/header
association, and that only survives losslessly in markup.

`--prioritize-xml` inverts the loop — format tiers outer, sources inner — so a
JATS copy from a worse-ranked source beats a PDF from a better-ranked one.

```bash
# Prefer structured full text, fall back through the normal chain
fetchpdf papers.csv -o ./out --prioritize-xml

# Structured XML or nothing
fetchpdf papers.csv -o ./out --xml-only

# XML or publisher HTML, but never a PDF
fetchpdf papers.csv -o ./out --xml-html-only

# Re-run over an existing directory, writing only genuine upgrades
fetchpdf papers.csv -o ./out --upgrade-existing

# Audit sidecar per record
fetchpdf papers.csv -o ./out --prioritize-xml --provenance
```

### Getting both formats, and Markdown for the model

```bash
# A structured copy AND the PDF for every record
fetchpdf papers.csv -o ./out --get-xml-or-html

# ...and a {stem}.md for each XML/HTML artifact
fetchpdf papers.csv -o ./out --get-xml-or-html --to-markdown

# Convert artifacts you already have, without re-downloading
fetchpdf-md ./out
```

`--get-xml-or-html` doubles as a backfill: pointed at a directory of existing
PDFs it fetches only the missing structured half and re-downloads nothing.

**Markdown output keeps tables as HTML, deliberately.** Markdown has no span
mechanism, so a `<th colspan="2">` over two treatment arms collapses and every
value to its right shifts one column — silently, in a way that still parses as a
valid table. Prose becomes Markdown; every table stays canonical minimal HTML at
its position in the document, with its caption and footnotes attached. Inline
HTML is part of the CommonMark spec, so this is ordinary Markdown, not a hybrid.

Every table is HTML, including simple ones with no spans. Mixing pipes and HTML
would make a three-column header ambiguous — no spans, or spans lost in
conversion? — and that ambiguity is worse than either format alone.

Each `.md` opens with front matter recording table count, tables that were
published as images (not machine-readable), figures referenced but not included,
and separate prose/table token counts:

```yaml
---
source_artifact: 10.3390--biom12111676.xml
table_format: canonical-html
tables: 2
tables_not_machine_readable: 0
figures_referenced_not_included: 1
prose_tokens: 11980
table_tokens: 189
---
```

JATS references images and never contains them, so figures appear as visible
placeholders rather than vanishing — a record whose key outcome sits in a forest
plot should look incomplete rather than complete.

### Choosing a flag

| Flag | Accepts | Falls back to PDF? |
|------|---------|--------------------|
| `--prioritize-xml` | everything, best format first | yes |
| `--xml-html-only` | T1 XML, T2 HTML | **no** — record fails |
| `--xml-only` | T1 XML | **no** — record fails |
| `--get-xml-or-html` | structured **and** PDF, both kept | yes, and keeps it |

`--xml-only` and `--xml-html-only` both imply `--prioritize-xml`. Given both,
`--xml-only` is stricter and wins (with a warning).

### The tier ladder

| Tier | Format | Why it sits here |
|------|--------|------------------|
| T1 | JATS/TEI XML | Cells, spans, headers and footnotes are markup. The only lossless tier. |
| T2 | Publisher HTML | Real `<table>` with `<th>` and spans; at most OA publishers generated from the same JATS. |
| T3 | LaTeX / e-print source | Lossless in principle, macro-hostile in practice. |
| T4 | Structured supplements | Often the underlying data; coverage is partial. |
| T5 | PDF | Structure must be reconstructed; introduces undetectable numeric corruption. |
| T6 | Plain text | Flattening destroys row/column association **silently**. Screening only. |
| T7 | Landing page | Last resort. |

`--target-task` selects the ladder. `screening` ranks plain text 4th;
`extraction` refuses it outright rather than degrading into it — every number is
present but anchored to nothing, which yields confident, plausible, wrong
arm-to-outcome assignments.

Artifacts are written as suffixed siblings: `{stem}.xml`, `{stem}.fulltext.html`,
`{stem}.source.tar.gz`, `{stem}.suppl.zip`, `{stem}.pdf`, `{stem}.txt`.

### Reordering without editing code

The ladder, the per-source capability map and the per-host rate limits live in
[`fetchpdf/retrieval/ladder.json`](fetchpdf/retrieval/ladder.json). Point
`FETCHPDF_LADDER` at your own copy to override it.

### Validation

HTTP 200 is not evidence of full text. Before a T1 artifact is accepted it must
parse, have a populated `<body>`, clear a character threshold, and not be a
publisher denial stub — NCBI returns those with status 200, full metadata, no
body, and the explanation in an XML comment. T2 additionally requires at least
one `<table>` with populated `<td>`, which is what rejects JS-rendered table
containers and tables shipped as images. Failures demote and the walk continues;
nothing is written unclassified or unvalidated.

### T2 HTML support

Publisher HTML needs a forgiving parser:

```bash
pip install -e '.[html]'
```

Without it the T2 rung demotes with a logged reason and the ladder descends
normally. Every other tier needs only the standard library.

## Supplementary Material

`--pull-supplementary` fetches everything the authors deposited *alongside* the
paper — supplementary PDFs, spreadsheets, documents, images, archives, raw data —
and saves it as flat siblings of the main artifact.

```bash
fetchpdf papers.csv -o ./out --pull-supplementary
fetchpdf papers.csv -o ./out --pull-supplementary -w 4
fetchpdf "10.1371/journal.pone.0000308" -o ./out --pull-supplementary
```

```
out/
  10.1371--journal.pone.0000308.pdf
  10.1371--journal.pone.0000308_supplementary_info_1.doc
  10.1371--journal.pone.0000308_supplementary_info_2.doc
  10.1371--journal.pone.0000308_supplementary_info_3.txt
  10.1371--journal.pone.0000308_supplementary_info_4.xls
  10.1371--journal.pone.0000308_supplementary_info.json    <- the manifest
```

It works with or without `--prioritize-xml`, and it runs for records whose PDF is
already on disk — so pointing it at a corpus you downloaded months ago collects
the supplements for all of it without re-fetching a single paper.

### The manifest is not optional reading

Flat numbering discards the original filenames, so
`{stem}_supplementary_info.json` is the only record of what `_2` actually is:

```json
{"index": 2, "filename": "..._supplementary_info_2.xls",
 "original_name": "pone.0000308.s004.xls", "label": "Table S1",
 "provider": "europepmc_supplements", "container": "PMC1817752_SupplementaryFiles.zip",
 "bytes": 42496, "sha256": "9f2c...", "role": "supplement"}
```

It also records what was *not* taken and why — `too-large` with the declared byte
count, `duplicate-of` with the index it duplicates, `not-a-document` for a
Cloudflare interstitial served as HTTP 200. "This record has no supplementary
material" and "one 4 GB HDF5 was refused" are different facts, and the manifest is
where they stay distinguishable.

The manifest is also the skip signal: a second run over the same output directory
costs zero HTTP requests. Delete a numbered file and the next run restores it *at
its original index*, so removing `_2` never renumbers `_3`. Use
`--refresh-supplementary` to re-enumerate and pick up newly deposited files.

### Where the files come from

Fifteen routes, tried in a fixed order so the numbering is reproducible:

| Route | Applies to |
|---|---|
| JATS `<supplementary-material>` | any PMC record — *classifies*, telling supplements from figures |
| Europe PMC `supplementaryFiles` | any PMCID (asked with `includeInlineImage=no`, or it returns every figure too) |
| PMC AWS Open Data | the PMC OA subset; declares size and md5 before transfer |
| Elsevier object API | Elsevier DOIs — the only ScienceDirect route that needs no browser |
| Springer / Nature ESM | `10.1038`, `10.1007`, `10.1186`, … |
| PLOS | `10.1371` |
| bioRxiv / medRxiv | `10.1101` |
| APA supplemental | `10.1037` |
| Figshare / Zenodo / OSF / Dryad / Dataverse | repository DOIs, and datasets reached through link services |
| DataCite reverse relations | any DOI — finds the deposited datasets that cite it |
| Crossref component DOIs | publishers that register components |
| OpenAIRE ScholeXplorer | any DOI — Scholix publication→dataset/software links |
| Europe PMC datalinks | any PMID — text-mined accessions and data citations |

Atypon (PNAS, Science, T&F, Sage), Wiley, ACS and MDPI serve HTTP 403 to plain
requests and are not yet covered; arXiv ancillary files are not yet covered
either. Dryad (`10.5061`) and Harvard Dataverse (`10.7910/DVN`) deposits are
reached through the link services rather than as top-level providers.

### Size cap

`--max-supplementary-mb` defaults to 300, per file. A file whose `Content-Length`
exceeds it is skipped without downloading anything; a server that under-reports is
aborted mid-stream and its partial removed. Nothing is ever truncated — a
truncated `.xlsx` is a corrupt zip that some readers open far enough to yield
wrong numbers, which is worse than not having the file.

### It never changes whether a record succeeded

A paper with no supplementary material is not a failure, and a repository serving
malformed JSON cannot turn a successful download into a failed one. Supplementary
files never appear in `failed_dois.csv`, in the missing-PDFs report, in
`source_tracking.csv`, or in the format-composition tally.

### Linked datasets and code: `{stem}_linked_artifacts.json`

The two link services (ScholeXplorer, Europe PMC datalinks) discover more than
they download, and the rest lands in a second sidecar. Every
publication→dataset and publication→software link they returned is recorded
there with a classification:

- `owned` — affirmatively the paper's own material: a supplement-grade
  relation (`IsSupplementTo` and friends), or a repository deposit whose own
  DataCite record names this article
- `related` — everything citation-grade: datasets/software the paper cites,
  text-mined data citations that no metadata ties back to the article, and
  repositories with no file enumerator
- `tool_citation` — "xgboost software on GitHub" and friends: somebody's tool,
  not this paper's code. Recorded, never downloaded.
- `registry` — ClinicalTrials.gov and other registrations; a link, not a file

**Only `owned` links are downloaded.** A "cites" link cannot distinguish the
paper's own deposit from a dataset the paper merely cites, so citation-grade
links earn a download only when the deposit's DataCite record names the
article — otherwise they stay sidecar links for the operator to judge.
Ownership-confirmed deposits in Figshare, Zenodo, OSF, Dryad or Harvard
Dataverse route to those enumerators and land as numbered supplements
(`routed_to_download: true` in the sidecar). The same deposit named by several
link services is listed and downloaded once.

Empty answers are recorded too — `"queried": [...]` with zero links is a
different fact from a sidecar that does not exist. On the corpora this was
measured against (2026-08), most papers have no links at all: ~30% of a recent
biomedical batch had any ScholeXplorer link, an older clinical-trial corpus had
none, and Europe PMC's text-mined accessions were the widest net. The sidecar
is written by the same `--pull-supplementary` pass, after the manifest; a run
that skips enumeration (manifest already present, no `--refresh-supplementary`)
does not rewrite it.

#### Where dataset links come from, and what can be downloaded

Discovery — the link services queried per record:

| Service | What it contributes |
|---|---|
| OpenAIRE ScholeXplorer | Scholix links aggregated from Crossref, DataCite, EMBL-EBI, repositories, plus OpenAIRE's text-mining of ~14M PDFs |
| Europe PMC datalinks | Text-mined accessions (trial registrations, GEO/SRA/PDB…), DOI data citations, BioStudies deposits — the widest net for biomedical papers (needs a PMID) |
| DataCite reverse query | Deposits whose own metadata names the article — depositor-declared ground truth |

Download — repositories with file enumerators (ownership-confirmed links only):

| Repository | Routed on | Notes |
|---|---|---|
| figshare | `10.6084` / "figshare" | declared size + md5 before transfer |
| Zenodo | `10.5281` / "zenodo" | restricted records skipped |
| OSF | `10.17605` / "osf.io" | walks folder trees (depth/node caps) |
| Dryad | `10.5061` / "dryad" | declared size + sha-256, paginated |
| Harvard Dataverse | `10.7910/DVN` | declared md5; restricted files skipped |
| BioStudies | — | content arrives via the Europe PMC supplementary bundle instead, never fetched twice |

Not yet downloadable (recorded in the sidecar, left to the operator):
Mendeley Data (`10.17632`), non-Harvard Dataverse installations, subject
databases (GEO, SRA, PDB, UniProt — accession records, not file bundles),
GitHub/GitLab repositories, and the institutional-repository long tail.
Adding an enumerator is the same small pattern as `enumerate_dryad` /
`enumerate_dataverse` in `fetchpdf/retrieval/supplement_index.py`.

## API Reference

### `fetch_pdf(doi, save_path, email, verbose, delay)`

Download a PDF from a DOI (or a PMID that can be resolved to a DOI) using multiple fallback sources.

**Parameters:**
- `doi` (str): DOI or PMID identifier to download
- `save_path` (str): Path where the PDF should be saved
- `email` (str, optional): Email for API calls (default: `EMAIL` from `.env.local`)
- `verbose` (bool, optional): Print detailed progress (default: False)
- `delay` (float, optional): Delay between API calls in seconds (default: 0.1)

**Returns:**
- `str`: Path to downloaded PDF if successful, `None` otherwise

### `batch_fetch_pdfs(dois, output_dir, email, verbose, delay, workers, create_missing_report)`

Download PDFs for multiple DOI/PMID identifiers with optional parallel processing.

**Parameters:**
- `dois` (list or str): List of DOI/PMID identifiers or path to CSV file
- `output_dir` (str): Directory to save PDFs
- `email` (str, optional): Email for API calls (default: `EMAIL` from `.env.local`)
- `verbose` (bool, optional): Print detailed progress (default: False)
- `delay` (float, optional): Delay between API calls per worker (default: 0.1)
- `workers` (int, optional): Number of parallel workers; 1 = sequential (default: 1)
- `create_missing_report` (bool, optional): Create HTML report for failed downloads (default: True)

**Returns:**
- `list`: List of tuples `(doi, success, save_path)` for each DOI processed

**Side Effects:**
- Creates `missing_pdfs.html` in `output_dir` if any downloads fail (unless `create_missing_report=False`)

### `pull_supplementary_for(raw_identifier, doi, save_path, ...)`

Fetch every supplementary file for one record, as flat siblings of `save_path`.

A separate call rather than a keyword on `fetch_pdf_from_doi`, deliberately: the
tiered engine re-enters that function with a temporary path, so a flag threaded
through it would write supplementary siblings next to a file that is about to be
deleted. Two calls make that impossible rather than merely discouraged.

```python
from fetchpdf import fetch_pdf, pull_supplementary_for

doi = "10.1371/journal.pone.0000308"
path = fetch_pdf(doi, "out/paper.pdf")
summary = pull_supplementary_for(doi, doi=doi, save_path="out/paper.pdf")
print(summary.status, summary.written, summary.manifest_path)
```

**Key parameters:**
- `raw_identifier` (str): the identifier as given (DOI or PMID)
- `doi` (str, optional): the resolved DOI, when known
- `save_path` (str): the main artifact's path; siblings and the manifest derive from its stem
- `max_file_bytes` (int, optional): per-file cap (default 300 MB)
- `refresh` (bool, optional): re-enumerate even when a manifest exists (default: False)
- `resolver` / `http` (optional): reuse a batch's `BatchResolver` / `HttpClient` so the per-host rate budget stays shared

**Returns:**
- `SupplementarySummary`: `.status` (`ok` | `none_found` | `partial` | `error` | `skipped`), `.written`, `.skipped`, `.bytes_written`, `.manifest_path`, `.paths`

Never raises for a network or provider failure, and its result must not be used to
decide whether the record succeeded.

### `fetch_metadata_from_doi(doi, email, delay)`

Fetch metadata for a paper from its DOI.

**Parameters:**
- `doi` (str): The DOI to fetch metadata for
- `email` (str, optional): Email for API calls (default: `EMAIL` from `.env.local`)
- `delay` (float, optional): Delay between API calls in seconds (default: 0.2)

**Returns:**
- `dict`: Metadata dictionary with keys: `authors`, `title`, `journal`, `volume`, `issue`, `pages`, `year`, `url`

## Download Sources (in order of priority)

The tool tries multiple sources in sequence until a PDF is successfully downloaded. The order is optimized to check faster/more reliable sources first and preserve rate-limited API quotas.

### Special DOI Handlers (Pattern-Matched)
These run first if the DOI matches specific patterns:

- **OSF** (Open Science Framework) - Projects (`10.17605/osf.io/*`) & Preprints (`10.31234/osf.io/*`)
  - Direct download URLs, Playwright browser automation, API fallback
- **SSRN** (Social Science Research Network) - `10.2139/ssrn.*`
  - Playwright with Cloudflare bypass, download button detection
- **Figshare** - `10.6084/m9.figshare.*`
  - Figshare API for file metadata and download URLs
- **PsychArchives** - `10.23668/psycharchives.*`
  - Leibniz psychology repository bitstream extraction

### Standard Fallback Chain (All DOIs)

1. **PubMed Central (PMC)** via Europe PMC
   - Open access papers via NCBI idconv → Europe PMC PDF endpoint
   - Fast, reliable for biomedical papers

2. **Unpaywall**
   - Comprehensive legal open access aggregator
   - Highly reliable, requires email

3. **Crossref**
   - Publisher metadata with direct PDF links
   - Landing page scraping with `citation_pdf_url` extraction
   - **NEW: Crossref chooser page handler** for multi-resolution DOIs
   - Publisher-specific URL patterns (Wiley, T&F, SAGE, MIT Press, etc.)

4. **Europe PMC**
   - European PubMed Central search API
   - Complementary to PMC direct access

5. **Semantic Scholar**
   - AI-powered academic search with `openAccessPdf` field
   - Landing page fallback for OJS sites
   - PsyArXiv→OSF URL conversion

6. **OpenAlex** ⚠️ **Rate Limited**
   - **1,000 downloads/day limit** (even with API key)
   - Placed after other sources to preserve quota for harder-to-find papers
   - Comprehensive metadata aggregator with open access locations

7. **CORE** (core.ac.uk)
   - 40M+ open access papers from repositories worldwide
   - Search API with download URLs

8. **Direct DOI Resolver**
   - Follows `https://doi.org/{doi}` redirect to landing page
   - Handles Crossref chooser pages (multiple resolution)
   - Scrapes PDF links from HTML (`citation_pdf_url`, href patterns)
   - Publisher-specific deterministic URL patterns

9. **DataCite Related Identifiers**
   - Supplementary material → main paper fallback
   - Versioned DOI resolution (`IsIdenticalTo`, `IsVersionOf`)
   - Figshare supplement handling

10. **ResearchGate**
    - DuckDuckGo search: `title + site:researchgate.net`
    - Landing page PDF extraction

11. **DOI → PMID Fallback**
    - Convert DOI to PMID when DOI sources fail
    - Try PMID-native sources (PubMed landing page `citation_pdf_url`)


### Final Fallback

12. **Elsevier XML API**
    - Text/data-mining API for Elsevier articles
    - Returns XML instead of PDF (last resort)
    - Requires `ELSEVIER_TDM_API_KEY`

---

**Total: 12 standard sources + 4 special handlers** = 16 different download strategies

## Requirements

- Python 3.8+
- requests
- playwright
- duckduckgo-search

## Troubleshooting

### Rate Limiting

If you're downloading many papers, consider:
- Increasing the `delay` parameter (e.g., `delay=1.0`)
- Using the batch downloader with controlled parallelism


## Ethical Considerations

This tool is intended for:
- ✅ Accessing open access papers
- ✅ Retrieving papers you have legal access to
- ✅ Academic research and education
- ✅ Fair use purposes

Please respect copyright laws and publisher terms of service in your jurisdiction.

## Acknowledgments

This package aggregates access to multiple open academic resources. Thanks to:
- OpenAlex, Unpaywall, Crossref, PubMed Central, Semantic Scholar, and other open science initiatives
- The Playwright team for browser automation tools
