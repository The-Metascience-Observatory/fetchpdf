# fetchpdf

A comprehensive Python package to download academic papers (PDFs) from DOIs (or PMIDs resolved to DOIs) using multiple fallback sources.


## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Getting more than the PDF](#getting-more-than-the-pdf) — XML/HTML, supplementary material, linked datasets
- [Usage Examples](#usage-examples)
- [Batch Processing & Parallel Execution](#batch-processing--parallel-execution)
- [Format-Prioritized Retrieval](#format-prioritized-retrieval)
- [Supplementary Material](#supplementary-material)
- [`--pull-everything`: the second layer](#--pull-everything-the-second-layer)
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
- 🤖 **LLM-assisted retrieval**: `--pull-everything` adds a second layer — a model reads the paper itself and goes after the SI and datasets the APIs missed — see [below](#--pull-everything-the-second-layer)
- 🎭 **Browser Automation**: Uses Playwright to bypass JavaScript-based protections
- 🚀 **Batch Processing**: Process multiple DOIs/PMIDs from CSV files or lists
- ⚡ **Parallel Execution**: Download multiple papers simultaneously with configurable workers

## Installation

```bash
pip install fetchpdf
```

Or, to work from source:

```bash
git clone https://github.com/The-Metascience-Observatory/fetchpdf.git
cd fetchpdf
pip install -e .
```

### Post-Installation: Install Playwright Browsers

After installing the package, you need to install Playwright browsers:

```bash
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

# Optional — only for --pull-everything --llm-backend openrouter
OPENROUTER_API_KEY=your_openrouter_key      # the retrieval agent's second backend
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

### Getting more than the PDF

By default a record yields one file: the best PDF the source chain finds. Three
flags widen that, and they compose — use all three together to get the paper in
both formats plus everything deposited with it.

**1. Structured full text (XML / HTML) instead of, or alongside, the PDF**

A PDF has no tables, only ink that looks like tables. For anything that parses
the paper, ask for markup:

```bash
# Keep BOTH a structured copy (JATS/TEI XML, else publisher HTML) AND the PDF
fetchpdf papers.csv -o ./out --get-xml-or-html

# Prefer structured full text, but accept a PDF when there is none
fetchpdf papers.csv -o ./out --prioritize-xml

# Structured only — fail the record rather than write a PDF
fetchpdf papers.csv -o ./out --xml-html-only    # XML or publisher HTML
fetchpdf papers.csv -o ./out --xml-only         # XML, nothing else

# Also render each XML/HTML artifact to {stem}.md for an LLM
fetchpdf papers.csv -o ./out --get-xml-or-html --to-markdown
```

Artifacts land as suffixed siblings: `{stem}.xml`, `{stem}.fulltext.html`,
`{stem}.pdf`, `{stem}.md`. `--get-xml-or-html` is also a backfill — pointed at a
directory of PDFs you already have, it fetches only the missing structured half.
Publisher HTML needs `pip install 'fetchpdf[html]'`; without it that tier is skipped
with a logged reason. Full detail: [Format-Prioritized Retrieval](#format-prioritized-retrieval).

**2. Supplementary material (SI/SM)**

```bash
# Every supplementary file the SI endpoints offer: PDFs, spreadsheets, docs, data
fetchpdf papers.csv -o ./out --pull-supplementary

# Raise or lower the per-file cap (default 300 MB)
fetchpdf papers.csv -o ./out --pull-supplementary --max-supplementary-mb 500

# Pick up files deposited since an earlier run
fetchpdf papers.csv -o ./out --refresh-supplementary
```

Files arrive numbered — `{stem}_supplementary_info_1.xlsx`, `_2.docx`, … — with
`{stem}_supplementary_info.json` recording what each number originally was, plus
what was *skipped and why*. The numbering discards original filenames, so
[read the manifest](#the-manifest-is-not-optional-reading). This runs for records
whose PDF is already on disk, so it collects supplements for an old corpus
without re-fetching a single paper, and a second run over the same directory
costs zero requests.

**3. Linked datasets and code**

The supplementary pass also asks the link services (ScholeXplorer, Europe PMC
datalinks, DataCite) what datasets and software each paper is connected to, and
writes every answer to `{stem}_linked_artifacts.json`. Deposits affirmatively
identified as *the paper's own* are downloaded and numbered alongside the
supplements. `--download-data-artifacts` goes further:

```bash
# Supplements + the paper's own linked deposits (Zenodo, Dryad, OSF, figshare, Dataverse)
fetchpdf papers.csv -o ./out --pull-supplementary

# Also take citation-grade deposits whose own metadata names this article,
# and repositories named in the paper's own full text
fetchpdf papers.csv -o ./out --download-data-artifacts

# Per-record dataset cap (default 500 MB), counted separately from supplements
fetchpdf papers.csv -o ./out --download-data-artifacts --max-data-artifact-mb 2000

# Expand replication-package zips (off by default — they have their own layout)
fetchpdf papers.csv -o ./out --download-data-artifacts --unpack-data-artifacts
```

`--download-data-artifacts` implies `--pull-supplementary`. What is *not*
downloaded still gets recorded: cited-but-not-owned datasets, tool citations,
trial registrations. See [Linked datasets and code](#linked-datasets-and-code-stem_linked_artifactsjson).

**All three at once**, which is the usual corpus-building invocation:

```bash
fetchpdf papers.csv -o ./out -w 4 \
    --get-xml-or-html --to-markdown \
    --download-data-artifacts \
    --make-subfolder --provenance
```

```
out/10.1371--journal.pone.0000308/
  10.1371--journal.pone.0000308.pdf                     <- the paper
  10.1371--journal.pone.0000308.xml                     <- structured full text
  10.1371--journal.pone.0000308.md                      <- prose + HTML tables
  10.1371--journal.pone.0000308_supplementary_info_1.xls
  10.1371--journal.pone.0000308_supplementary_info_2.csv <- from a Dryad deposit
  10.1371--journal.pone.0000308_supplementary_info.json  <- what each number is
  10.1371--journal.pone.0000308_linked_artifacts.json    <- every dataset/code link
  10.1371--journal.pone.0000308.provenance.json          <- source, tier, hashes
```

Supplements and datasets never change whether a record counts as a success: a
paper with no SI is not a failure, and a repository timing out cannot turn a
downloaded paper into a failed one.

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

The extra formats and the supplementary pass are keyword arguments on the batch
entry point — `--pull-supplementary` is plumbed through `batch_fetch_pdfs` only,
so use it even for a single DOI:

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois=["10.1371/journal.pone.0000308"],
    output_dir="./out",
    get_xml_or_html=True,        # structured copy AND the PDF
    to_markdown=True,            # plus {stem}.md
    pull_supplementary=True,     # SI/SM as numbered siblings + manifest
    download_data_artifacts=True,  # and the paper's linked deposits
    max_supplementary_bytes=300 * 1024 * 1024,
    want_provenance=True,
    verbose=True,
)
# -> [(doi, success, save_path), ...]
```

To collect supplements for one record you already have on disk, call
`pull_supplementary_for` directly — see [API Reference](#api-reference).



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
    import csv
    with open("failed_dois.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DOI"])
        writer.writerows([doi] for doi in failed_dois)
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
import csv

results = batch_fetch_pdfs(dois="papers.csv", output_dir="./papers", workers=4)

# Save results
with open("download_results.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["DOI", "Success", "Path"])
    writer.writerows(results)

# Statistics
successes = sum(1 for _, success, _ in results if success)
print(f"Success: {successes}/{len(results)}")
print(f"Success rate: {successes/len(results)*100:.1f}%")
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

### Re-running over a directory you already have

There is no ledger and nothing records which flags built a directory. Skipping
is decided per record from the filesystem: if an artifact already sits at the
record's stem, that record is skipped. This is what makes an interrupted run
resumable — re-run the same command and it picks up where it stopped — and it
works identically flat or under `--make-subfolder`, since the two layouts differ
by one path segment.

The silent part is that a skip also skips the *choice*. A directory of papers
you fetched last month can gain its supplementary material without re-fetching a
single PDF, because the SI pass runs on the skip branch — but nothing says so.
So a re-run that is about to skip records asks:

```
📁 1,284 of 5,000 records already have artifacts in ./out.

   [s] skip them             download only the 3,716 still missing   (default)
   [m] skip, but pull SI/SM  keep the papers, fetch supplementary material for all of them
   [a] abort
```

Answering `m` is exactly `--pull-supplementary`: every PDF already on disk stays
untouched, and records whose SI manifest is already written cost zero requests.

`--on-existing {skip,supplement,ask}` pre-answers it. **The prompt never appears
unless stdin and stdout are both terminals** — pipes, cron and CI get a one-line
count and today's behaviour, so no unattended run can block on it. It is also
suppressed when the answer is already known: `--pull-supplementary` given
explicitly, `--abstract-only`, or the goal-aware flags (`--get-xml-or-html`,
`--upgrade-existing` on the tiered path), which do not skip records at all.

What is *not* remembered between runs: failures. `failed_dois.csv` is a report,
never read back, so every re-run retries every previous failure. Feed it back in
deliberately if you want only the retries.

**Markdown output keeps tables as HTML, deliberately.** Markdown has no span
mechanism, so a `<th colspan="2">` over two treatment arms collapses and every
value to its right shifts one column — silently, in a way that still parses as a
valid table. Prose becomes Markdown; every table stays canonical minimal HTML at
its position in the document, with its caption and footnotes attached. Inline
HTML is part of the CommonMark spec, so this is ordinary Markdown, not a hybrid.
Elsevier full-text XML (`ce:` prose, CALS `tgroup`/`entry` tables with
`namest`/`nameend`/`morerows`) goes through the same walker, so its tables
arrive as the same canonical HTML with the spans resolved.

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

A PDF gets the same question asked of it: **is this the paper we asked for?**
`%PDF` and a size floor are a file-type check, not an identity check, and the
difference is not academic. A landing page's reference list is a list of other
people's PDFs; when the article's own copy is unavailable — paywalled,
bot-walled, moved — a link-scraping fallback will happily download one of them,
and it arrives as a genuine `application/pdf` with a 200. That is how
`10.1111/all.14949`, a Wiley allergy paper, came back as the USDA's 164-page
*Dietary Guidelines for Americans*, a work it cites, saved next to the correct
XML for the same record fetched in the same walk.

So every retrieved PDF is now checked against the record it was fetched for
(`retrieval/pdf_identity`). Any one signal is enough, because the documents
that legitimately fail one are common:

| signal | carries |
|---|---|
| the requested DOI in the front matter | essentially every version of record |
| the article title on the front pages | accepted manuscripts (`nihms-*.pdf`), which print the title and not the publisher's DOI |
| the title as a near match | Greek letters typeset in a Latin face, hyphenated line breaks, entity-mangled metadata |
| the title embedded in the PDF's own metadata | documents whose text layer is too thin to carry one |

The title costs no extra request: resolution has already memoised the Crossref
payload by the time a PDF is judged.

Refusals are kept distinct rather than collapsed into one message, because
*"this is a different paper"* is a bug signal, *"this document has no text
layer"* is a corpus-quality signal, and *"no PDF reader is installed"* is a
broken install — and they send whoever reads the report to three different
places. A document that says almost nothing machine-readable is reported as
unreadable, never as somebody else's: a scanned paper often extracts to nothing
but a library's download stamp, which is text, but not text that could ever
have carried a title.

The check refuses inside the candidate loop, not only at the end of the run.
Refusing only at the end would downgrade "wrong PDF" to "no PDF" — the right
copy is frequently two candidates further down, and the loop has to reach it.
Artifacts already on disk are never re-judged, so re-running over a corpus will
not delete files it did not fetch.

Measured over 500 main-article PDFs from real corpora: 95.6% verified, 3.4%
refused as a different document, 1.0% refused as illegible. The refusals were
inspected by hand and were overwhelmingly genuine — publisher marketing pages
("Why Publish in Spine?"), permissions boilerplate, a government citizens'
charter, an R package manual, a JSTOR literature article, and cited documents
like the one above.

Because verification is not optional, **a PDF text engine is no longer
optional**: `pypdf` is a base dependency and the CLI refuses to start without an
engine rather than rejecting every PDF one at a time. PyMuPDF remains in the
`text` extra as the faster reader and is preferred when installed.

#### The right article, but not all of it

A publisher's first-page preview is the hardest case, because every identity
signal passes it: it carries the article's own DOI, its own title, its own
journal furniture. It is simply two pages of seventeen, cut off mid-sentence,
with nothing anywhere in the text admitting as much. The same is true of an
article's own supplementary file, which is why an "APA supplemental PDF"
route can hand back four pages of a thirteen-page record and look like success.

Two structural signals catch these, both refusing with a `truncated` verdict
that is deliberately distinct from "wrong article" — it *is* the right article,
so the caller should keep walking the chain for a complete copy rather than
conclude the source was bad:

1. **Against the record's own structured full text**, when the walk already has
   it. The strongest signal, because it compares the document with *itself* in a
   format a paywall cannot truncate. Measured on records where both were
   retrieved: complete PDFs scored 0.98, 1.02 and 1.04; a two-page Brill preview
   of a thirty-two-page article scored **0.07**.
2. **Against the printed page range.** Available far more often, since most
   records never get a structured copy. Over 456 verified-correct corpus PDFs
   the ratios run 0.22, 0.31, 0.33, 0.33, 0.42 and then jump to 0.73, so the 0.5
   threshold sits in open space rather than on a slope.

Two other signals were measured and **rejected** rather than shipped: 77.6% of
known-good PDFs have no "References"/"Acknowledgements" marker in their last
4,000 characters, and 14.8% never print their own last page number. Both would
have thrown away one correct paper in seven.

URLs containing `/previewpdf/` are skipped before any transfer — Human Kinetics
and Brill both serve previews from that path — and a URL already refused for a
record is not fetched again, since `try_landing_page_pdf_fallback` runs from
nine call sites and several converge on the same publisher URL.

#### Crossref under parallel load

Verification needs a title for every record, which made metadata the most
rate-limit-hungry step in the tool at exactly the moment large runs got faster.
Crossref publishes its allowance in every response — `x-rate-limit-limit: 10`,
`x-rate-limit-interval: 1s` — and three things now keep a `--workers 10` run
inside it:

- **One process-wide token bucket — genuinely one.** The tiered engine, the
  supplementary pass and the legacy chain each used to build their own
  `HostRateLimiter`, so a run touching two of them politely allowed 10/s twice
  against an allowance of 10/s. They now share a single limiter
  (`ratelimit.shared_host_limiter`), and every Crossref call in the package
  draws from the same bucket. Politeness is a property of the process, not of a
  batch.
- **A 429 slows every worker.** The bucket is drained by `Retry-After` rather
  than the receiving thread sleeping alone — otherwise one worker backs off
  while the other nine keep spending the budget that just ran out.
- **Titles fetched in bulk, before any worker starts.** Crossref accepts a
  comma-joined `filter=doi:...` list; batches of 50 with `select=DOI,title,page`
  return every item in about a fifth of a second. A 10,000-record run needs
  **200 metadata requests instead of 10,000**.
- **429 means wait, not "no such record".** `Retry-After` is honoured (clamped
  to 60s), with bounded exponential backoff behind it. And **"not in Crossref"
  is not "no title"** — Crossref does not register Zenodo, OSF, figshare or
  Dryad DOIs, so those fall through to DataCite rather than being recorded as
  titleless.

Measured on 120 records at `--workers 10`: **19 Crossref requests, zero 429s,
peak 9 req/s.**

The bug that motivated this was worse than slowness. A failed lookup used to be
cached as "this record has no title" — so one 429 became permanent, and since a
record with no title cannot be verified and an unverifiable PDF is deleted, a
brief rate-limit blip **deleted correct files**. Now the two are kept strictly
apart: a 404 is an answer and is cached, while a 429 or a timeout is not an
answer and is never cached. Deleting now requires a **positive** finding (`wrong_article` or `truncated`)
**and** metadata we actually obtained. `no_reference`, `unreadable` and
`no_engine` all mean "nobody checked", which is not a statement about the file;
each of them used to delete it. If metadata cannot be fetched at all, the PDF is
**kept unverified rather than deleted** — a wrong file kept can be found again
by re-running the audit, a right file deleted cannot — and every such record is
named at the end of the run, because they are the only artifacts in it that
nobody checked.

#### When there is no PDF, there may still be the paper

A record whose PDF cannot be had falls back to structured full text **even when
it was not asked for**. That is not a consolation prize: XML is tier 1 on the
extraction ladder and the PDF is tier 5. "No PDF" and "no full text" are
different answers, and only one of them is worth a human's time. The fallback is
restricted to T1/T2, so it cannot re-enter the chain that called it, and
`--no-xml-fallback` opts out.

In practice this is what a bot-walled or preview-only record now returns: the
Brill record above yields 715 KB of publisher HTML where it previously yielded
two pages of PDF, and `10.1111/all.14949` yields the full JATS that NCBI efetch
serves for records Europe PMC's OA route refuses.

### T2 HTML support

Publisher HTML needs a forgiving parser:

```bash
pip install 'fetchpdf[html]'
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
GitLab repositories, and the institutional-repository long tail.
Adding an enumerator is the same small pattern as `enumerate_dryad` /
`enumerate_dataverse` in `fetchpdf/retrieval/supplement_index.py`.

## `--pull-everything`: the second layer

Everything above is layer 1 — APIs, indexes and enumerators. It is very good at
what publishers and repositories *declare*, and it cannot read. Layer 2 is a
model that reads the paper itself and goes after what layer 1 did not get.

```bash
fetchpdf papers.csv -o ./out --get-xml-or-html --make-subfolder --provenance \
         --pull-everything
```

`--pull-everything` implies `--pull-supplementary` and
`--download-data-artifacts`, turns on adjudication of the deterministic scan's
uncertain candidates, and adds the retrieval agent. Files land exactly where
they always did: supplements as numbered siblings of the PDF, linked deposits
in `{stem}_data_artifacts/`.

**It is not literally everything, and the run says so.** Wiley, ACS and MDPI
serve HTTP 403 to plain requests; Atypon needs the headed-browser path; arXiv
ancillary files are uncovered; the repositories in the table above still have
no enumerator. Those are recorded in the sidecar, not fetched.

### What the agent is told

Three things a model does not have and layer 1 does:

1. the paper's own text, weighted toward its availability statements;
2. what has already been obtained, so it does not fetch a second copy;
3. **what the link services found and declined, with their reasons.** That is
   the richest input it gets. Measured over four corpora, those services
   returned 3,005 `related` links that were never routed, and of 280 `owned`
   ones 264 were BioStudies mirrors of supplements already in hand.

It proposes; the existing gates dispose. A proposal naming a repository routes
through the same enumerator and the same ownership checks
(`_should_route`, `_deposit_claims_another_article`) an index's link would, and
anything it saves still meets `_commit`'s size cap, sha256, deduplication and
challenge-page sniff. No failure of it can change whether a record succeeded.

### `--llm-backend`: two sandboxes, one result

| | `claude-cli` (default) | `openrouter` |
|---|---|---|
| auth | whatever the local Claude Code CLI already has | `OPENROUTER_API_KEY` in `.env.local` |
| `--llm-model` | `haiku`, `sonnet`, … | the same names, translated to slugs |
| tools | WebFetch only | `fetch_url` and `download`, both implemented here |
| downloads? | **no** — it navigates and reports URLs, fetchpdf transfers | yes, into a staging directory |

The asymmetry is not a preference, it is a measurement. Against Claude Code
2.1.240:

- **`--allowed-tools` grants permission; it does not restrict the tool set.** A
  subprocess given `--allowed-tools "Bash(echo:*)"` ran
  `cat ../outside_canary.txt` and returned the contents. So did
  `--allowed-tools WebFetch`. With and without `--permission-mode
  bypassPermissions`.
- **`--disallowed-tools` does remove tools**, and was the only lever that
  worked.
- **A default subprocess inherits the operator's own MCP servers.** On the
  machine this was written on, that put Gmail, Google Drive, Calendar and a
  brokerage account in scope for a subprocess whose job is downloading
  spreadsheets. `--strict-mcp-config` with an empty config removes them.

So the CLI backend is locked to WebFetch and nothing else — verified by asking a
locked-down subprocess to list its own tools, which answered `WebFetch` — and a
process with no file tools cannot save a file. It reports URLs instead, which
loses nothing: navigating is the part that needs a model, and transferring bytes
under a cap with a hash is the part this package already does. The OpenRouter
backend has no general-purpose harness to lock down, because its two tools are
ours, so it downloads directly.

### What it costs, and what to expect

One model call per record. `--max-llm-records N` is the ceiling that stops an
overnight batch spending without bound; records past it still get every API
route, and the manifest records that the agent was *skipped* rather than that it
found nothing.

Measured on the corpora this was built against, **most calls will correctly find
nothing**, and that is not a defect in the agent. Of 606 PDF-only records, 6
name a repository at all and 17 carry a data-availability statement — the
biomedical sets skew to 1991–2006, before data-sharing norms existed. Expect the
yield on modern and social-science corpora instead: `fulltext_scan` alone
produced 83 OSF files from ten such records, and the link indexes had surfaced
none of the fourteen deposits those articles named in their own prose.

Recall is also **model-dependent and not deterministic**. On one record whose
GitHub URL is broken across a line by PDF layout — invisible to any regex —
`haiku` recovered it on two runs out of four and returned nothing on the other
two. Treat the agent as a second pass that sometimes finds what layer 1 missed,
not as a guarantee.

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
