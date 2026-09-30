# fetchpdf

A comprehensive Python package to download academic papers (PDFs) from DOIs (or PMIDs resolved to DOIs) using multiple fallback sources.


## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Getting more than the PDF](#getting-more-than-the-pdf) — XML/HTML, supplementary material, linked datasets
  - [PMID Support](#pmid-support)
  - [Other commands](#other-commands)
- [Usage Examples](#usage-examples)
- [Batch Processing & Parallel Execution](#batch-processing--parallel-execution)
- [Format-Prioritized Retrieval](#format-prioritized-retrieval)
- [Supplementary Material](#supplementary-material)
- [`--pull-everything`: the second layer](#--pull-everything-the-second-layer)
- [Figure Images](#figure-images)
- [API Reference](#api-reference)
- [Download Sources](#download-sources-in-order-of-priority)
- [Requirements](#requirements)
- [Troubleshooting](#troubleshooting)
- [Ethical Considerations](#ethical-considerations)

## Features

- 🔍 **Multiple Sources**: Automatically tries a chain of sources in order ([full list](#download-sources-in-order-of-priority)), including:
  - OSF (Open Science Framework) - Projects & Preprints
  - PubMed Central / Europe PMC
  - Unpaywall
  - Crossref
  - Semantic Scholar
  - OpenAlex
  - CORE
  - Figshare
  - SSRN (Social Science Research Network)
  - Google Scholar via SerpApi (optional)

- 🔄 **Smart Fallback**: If one source fails, automatically tries the next
- 🧬 **[Format Prioritization](#format-prioritized-retrieval)**: `--prioritize-xml` prefers structured full text (JATS/TEI) over PDF
- 📎 **[Supplementary Material](#supplementary-material)**: `--pull-supplementary` fetches every supplementary file alongside the paper
- 🖼️ **[Figure Images](#figure-images)**: `--pull-figures` fetches the article's published figures from PMC with a manifest of labels, captions and hashes
- 🔗 **[Linked Datasets & Code](#linked-datasets-and-code-stem_linked_artifactsjson)**: the same pass discovers each paper's external datasets/software via ScholeXplorer, Europe PMC and DataCite, downloads ownership-confirmed deposits from figshare/Zenodo/OSF/Dryad/Dataverse, and records every link in a sidecar
- 🤖 **[LLM-assisted retrieval](#--pull-everything-the-second-layer)**: `--pull-everything` adds a second layer — a model reads the paper itself and goes after the SI and datasets the APIs missed
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

### Optional: Install Chromium for Browser Automation

You can use FetchPDF without installing a browser. Install Chromium only if you
want browser-based retrieval: the scrapers enabled by `--add-playwright`, plus a
few routes that always use a browser when one is available — SSRN downloads,
Atypon supplementary files (which also need a display or Xvfb), and retries of
Dataverse bot challenges:

```bash
playwright install chromium
```

Without Chromium, browser-based retrieval is unavailable, but downloads through
APIs and direct HTTP requests still work.

### Optional extras

```bash
pip install 'fetchpdf[html]'     # publisher HTML full text (T2); without it such pages are rejected with a logged reason
pip install 'fetchpdf[text]'     # PyMuPDF, a faster PDF text engine (pypdf is always installed)
pip install 'fetchpdf[images]'   # --extract-images and fetchpdf-images
```

### Configuration: `.env.local`

Create a `.env.local` file to configure API keys and settings. For a source
checkout, put it in the repository root; for a `pip install`, put it in (or
above) the directory you run `fetchpdf` from. Plain environment variables work
too. API keys are **optional** but improve rate limits and reliability:

```bash
# Required — used by Unpaywall (mandatory) and Crossref's polite pool
EMAIL=your@email.com

# Optional — improves rate limits / avoids throttling
OPENALEXAPIKEY=your_openalex_key           # OpenAlex: fewer 429 rate-limit errors
SEMANTIC_SCHOLAR_API_KEY=your_s2_key        # Semantic Scholar: 1 → 10 req/s
ENTREZ_EUTILS_API_KEY=your_ncbi_key         # NCBI E-utilities: 3 → 10 req/s
COREAPIKEY=your_core_key                   # CORE: 40M+ OA papers from core.ac.uk
SERPAPI_API_KEY=your_serpapi_key           # Optional Google Scholar PDF fallback

# Optional — publisher-specific
ELSEVIER_TDM_API_KEY=your_elsevier_key      # Elsevier text/data-mining access
SCOPUS_API_KEY=your_scopus_key              # Scopus abstract lookup during identifier resolution

# Optional — only for --pull-everything --llm-backend openrouter
OPENROUTER_API_KEY=your_openrouter_key      # the retrieval agent's second backend

# Required by fetchpdf-pubpeer — PubPeer rejects keyless requests
PUBPEER_DEVKEY=your_pubpeer_key
```

**What the keys change** (the rates are fetchpdf's own per-host throttles, set in
[`ladder.json`](fetchpdf/retrieval/ladder.json)):

| API | Without Key | With Key | How to Get |
|-----|-------------|----------|------------|
| Crossref | 10 req/s | 10 req/s; `EMAIL` puts requests in Crossref's polite pool | Just set `EMAIL` |
| OpenAlex | 5 req/s, more 429s | 5 req/s | [openalex.org/users](https://openalex.org/users) |
| Semantic Scholar | 1 req/s | 10 req/s | [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) |
| NCBI E-utilities | 3 req/s | 10 req/s | [ncbi.nlm.nih.gov/account](https://www.ncbi.nlm.nih.gov/account/) |
| CORE | Skipped | 2 req/s, 500 requests/day quota | [core.ac.uk/services/api](https://core.ac.uk/services/api) |
| SerpApi Google Scholar | Skipped | Account search quota | [serpapi.com/google-scholar-api](https://serpapi.com/google-scholar-api) |

## Quick Start

### Command Line Interface

```bash
# Batch download from CSV (auto-detected by .csv extension)
fetchpdf papers.csv -o ./pdfs
fetchpdf papers.csv -o ./pdfs -w 4        # 4 parallel workers

# Single DOI or PMID
fetchpdf 10.1038/nature12373            # saves to ./pdfs/
fetchpdf 10.1038/nature12373 -o ./papers # custom output dir
fetchpdf 10.1038/nature12373 output.pdf  # custom filename
fetchpdf 33262244                          # PMID auto-resolved to DOI
```

### Getting more than the PDF

By default a record yields one file: the best PDF the source chain finds. Three
flags widen that.

1. **Structured full text** — `--get-xml-or-html` keeps a JATS/TEI XML (else
   publisher HTML) copy alongside the PDF; `--to-markdown` also renders it to
   `{stem}_from_xml.md` (or `{stem}_from_html.md`). XML and HTML are great
   formats for using with AI (LLMs): complex tables usually survive better than
   in a PDF converted to Markdown, though a
   PDF can win when tables are published as images. Stricter variants and the
   format ladder: [Format-Prioritized Retrieval](#format-prioritized-retrieval).
2. **Supplementary material (SI/SM)** — `--pull-supplementary` saves every
   supplementary file as numbered siblings with a manifest recording what each
   number is. See [Supplementary Material](#supplementary-material).
3. **Linked datasets and code** — the supplementary pass also records every
   dataset/software link in `{stem}_linked_artifacts.json` and downloads deposits
   identified as the paper's own into `{stem}_data_artifacts/`;
   `--download-data-artifacts` widens that. See
   [Linked datasets and code](#linked-datasets-and-code-stem_linked_artifactsjson).

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
  10.1371--journal.pone.0000308_from_xml.md             <- prose + HTML tables
  10.1371--journal.pone.0000308_supplementary_info_1.xls
  10.1371--journal.pone.0000308_supplementary_info_2.doc
  10.1371--journal.pone.0000308_supplementary_info.json  <- what each number is
  10.1371--journal.pone.0000308_data_artifacts/          <- linked deposits, original filenames
      10.5061_dryad.xxxxx/data.csv                          (one folder per deposit)
  10.1371--journal.pone.0000308_linked_artifacts.json    <- every dataset/code link
  10.1371--journal.pone.0000308.provenance.json          <- source, tier, hashes
```

Supplements and datasets never change whether a record counts as a success —
see [below](#it-never-changes-whether-a-record-succeeded).

### Python API

```python
import os
from fetchpdf import fetch_pdf

# Download a PDF from a DOI (PMID also supported)
doi = "10.1038/nature12373"
save_path = "./papers/nature_paper.pdf"

os.makedirs("./papers", exist_ok=True)   # fetch_pdf does not create directories

result = fetch_pdf(
    doi=doi,
    save_path=save_path,
    verbose=True,
    delay=0.1  # Polite delay between API calls
)
# -> the saved path, or None. It can be a .xml/.html path when only structured
#    full text was available.
```

`fetch_pdf` has no supplementary option. The extra formats and the supplementary
pass are keyword arguments on the batch entry point, which works for a single DOI
too:

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois=["10.1371/journal.pone.0000308"],
    output_dir="./out",
    get_xml_or_html=True,        # structured copy AND the PDF
    to_markdown=True,            # plus {stem}_from_xml.md
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

`fetchpdf`, `fetch_pdf` and `batch_fetch_pdfs` accept a PMID wherever they accept a DOI. The tool first resolves PMID → DOI (NCBI, then Europe PMC and a Crossref title search as fallbacks), then runs the normal DOI download flow. (`fetch_metadata_from_doi` and `fetchpdf-pubpeer` take DOIs only.)

**CLI examples:**
```bash
# Plain PMID
fetchpdf "33262244" -o ./pdfs -v

# PMID with prefix
fetchpdf "PMID:33262244" -o ./pdfs

# PubMed URL
fetchpdf "https://pubmed.ncbi.nlm.nih.gov/33262244/" -o ./pdfs
```

If a PMID cannot be resolved to a DOI, the tool still tries PMID-native sources (the PubMed page's `citation_pdf_url`); if those fail too, the record is reported as failed.

### Other commands

| Command | What it does |
|---|---|
| `fetchpdf-md ./out` | Render XML/HTML artifacts already on disk to `{stem}_from_xml.md` / `{stem}_from_html.md`, without re-downloading. Reads one directory, not its subfolders — after a `--make-subfolder` run, point it at each record's folder |
| `fetchpdf-images` | Dump the original embedded image bitstreams from PDFs already on disk (`pip install 'fetchpdf[images]'`) |
| `fetchpdf-verify` | Offline check that supplementary files on disk still match the hashes their `_supplementary_info.json` manifests recorded |
| `fetchpdf-pubpeer` | Capture a dated record of a paper's PubPeer comments (an empty record when there are none). DOIs only; needs `PUBPEER_DEVKEY` |


## Usage Examples

### Example 1: Download with Metadata

```python
from fetchpdf import fetch_pdf, fetch_metadata_from_doi
import os

doi = "10.1038/nature12373"

# Get metadata first (any field can be None if no source had it)
metadata = fetch_metadata_from_doi(doi)
title = metadata['title'] or doi
print(f"Downloading: {title}")

# Use metadata for filename
safe_title = title[:50].replace(" ", "_").replace("/", "_")
os.makedirs("./papers", exist_ok=True)
save_path = f"./papers/{safe_title}.pdf"

# Download PDF
result = fetch_pdf(doi, save_path)

if result:
    print(f"✅ Downloaded: {title}")
    print(f"   Authors: {metadata['authors']}")
    print(f"   Year: {metadata['year']}")
```

### Example 2: Using the Missing PDFs Report

Batch runs write `missing_pdfs.html` to the output directory, adding a row as each record fails:

```python
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(
    dois="papers.csv",
    output_dir="./papers",
    workers=4
)

# After completion, check ./papers/missing_pdfs.html
```

Each failed record gets a row with its identifier, linked to doi.org or PubMed for
a manual download, and the filename to save it under so a re-run picks it up. At
the end of the run a second table lists
[supplementary material and figures that could not be obtained](#refused-is-not-absent).

To disable the report, pass `create_missing_report=False`, or on the command line:
```bash
fetchpdf papers.csv -o ./papers --no-missing-report
```

## Batch Processing & Parallel Execution

`batch_fetch_pdfs` (and the CLI) accepts either a list of DOI/PMID identifiers or
a path to a CSV file. It returns `[(id, success, save_path), ...]`, where `id` is
the resolved DOI (lowercased) or the bare PMID and `save_path` is `None` on
failure; with `track_source=True` each tuple gains a fourth element, the source
that succeeded.

**CSV format** — one identifier per row under a `DOI` column:
```csv
DOI
10.1038/nature12373
10.1126/science.1241224
10.1016/j.cell.2019.05.031
```

If your CSV uses a different column name:

```bash
fetchpdf data.csv --doi-column "paper_doi" -o ./pdfs
```

**Parallel execution** — `-w N` on the command line, `workers=N` in Python;
`delay` is the pause between API calls *per worker*:

```bash
fetchpdf papers.csv -o ./papers -w 4
```

```python
from fetchpdf import batch_fetch_pdfs

dois = ["10.1038/nature12373", "10.1126/science.1241224", "10.1016/j.cell.2019.05.031"]

results = batch_fetch_pdfs(
    dois=dois,          # or "papers.csv"
    output_dir="./papers",
    workers=4,          # 1 = sequential (default)
    delay=0.2,
)
# -> [(doi, success, save_path), ...]
```

### Best Practices

- **Sequential is fine for small batches** (< 20 identifiers).
- **Use 2–4 workers for large batches.** More runs into API rate limits, is less
  polite to servers, and gives diminishing returns beyond 4–8 workers.
- **Raise `delay` as you add workers** (e.g. `workers=8, delay=0.5`) — more
  workers means more simultaneous requests.
- **Retry failures sequentially**, and keep a record of the results:

```python
import csv
from fetchpdf import batch_fetch_pdfs

results = batch_fetch_pdfs(dois="papers.csv", output_dir="./papers", workers=4)

with open("download_results.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["DOI", "Success", "Path"])
    writer.writerows(results)

successes = sum(1 for _, success, _ in results if success)
print(f"Success: {successes}/{len(results)} ({successes/len(results)*100:.1f}%)")

failed_dois = [doi for doi, success, _ in results if not success]
if failed_dois:
    retry_results = batch_fetch_pdfs(
        dois=failed_dois,
        output_dir="./papers",
        workers=1,
        delay=1.0,
        verbose=True,  # see what's happening
    )
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

# Structured only — fail the record rather than write a PDF
fetchpdf papers.csv -o ./out --xml-html-only    # XML or publisher HTML
fetchpdf papers.csv -o ./out --xml-only         # XML, nothing else

# A structured copy AND the PDF for every record
fetchpdf papers.csv -o ./out --get-xml-or-html

# ...and a {stem}_from_xml.md / _from_html.md for each XML/HTML artifact
fetchpdf papers.csv -o ./out --get-xml-or-html --to-markdown

# Convert artifacts you already have, without re-downloading
fetchpdf-md ./out

# Re-run over an existing directory, writing only genuine upgrades
fetchpdf papers.csv -o ./out --upgrade-existing

# Audit sidecar per record
fetchpdf papers.csv -o ./out --prioritize-xml --provenance
```

Artifacts are written as suffixed siblings, one per tier: `{stem}.xml`,
`{stem}.fulltext.html`, `{stem}.source.tar.gz`, `{stem}.suppl.zip` (a single-file
supplement keeps its own extension, e.g. `{stem}.xlsx`), `{stem}.pdf`,
`{stem}.txt` and `{stem}.landing.html` — plus `{stem}_from_xml.md` /
`{stem}_from_html.md` under `--to-markdown`.

`--get-xml-or-html` doubles as a backfill: pointed at a directory of existing
PDFs it fetches only the missing structured half and re-downloads nothing.

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
| T2 | Publisher HTML | Real `<table>` with `<th>` and spans; at most OA publishers generated from the same JATS. Includes the PMC article page, tried when both PMC XML routes come up empty — measured over a 105-paper corpus, all six records the ladder had settled on a PDF for had a PMCID and a complete PMC page waiting. |
| T3 | LaTeX / e-print source | Lossless in principle, macro-hostile in practice. |
| T4 | Structured supplements | Often the underlying data; coverage is partial. |
| T5 | PDF | Structure must be reconstructed; introduces undetectable numeric corruption. |
| T6 | Plain text | Flattening destroys row/column association **silently**. Screening only. |
| T7 | Landing page | Last resort. |

`--target-task` selects the ladder. `screening` ranks plain text 4th;
`extraction` refuses it outright rather than degrading into it — every number is
present but anchored to nothing, which yields confident, plausible, wrong
arm-to-outcome assignments.

### Markdown output

JATS conversion includes the abstract, body, labeled publication/history dates,
author and funding notes, and back matter such as ethics statements, acknowledgments,
appendices, and supplementary-material descriptions. Bibliographic reference lists
are omitted. Tables remain embedded HTML with spans, captions, and footnotes;
external figures and supplementary files are referenced rather than fetched.
Partial dates retain their original precision. An abstract without a body is still
reported as a conversion failure, not a full-text success.

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

Each Markdown file opens with front matter recording its source and conversion time, table count, tables that were
published as images (not machine-readable), figures referenced but not included,
and separate prose/table token counts:

```yaml
---
source_artifact: 10.3390--biom12111676.xml
converted_at: 2026-09-14T10:02:31Z
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

### Re-running over a directory you already have

There is no ledger and nothing records which flags built a directory. Skipping
is decided per record from the filesystem: if an artifact already sits at the
record's stem, that record is skipped. On the default path that means a `.pdf`
or `.xml`; on the tiered (`--prioritize-xml`) path, any tier's suffix. This is
what makes an interrupted run resumable — re-run the same command and it picks
up where it stopped. It works the same flat or under `--make-subfolder`, but not
across the two: a subfolder run does not see files from an earlier flat run, and
re-downloads them.

The silent part is that a skip also skips the *choice*. A directory of papers
you fetched last month can gain its supplementary material without re-fetching a
single PDF, because the SI pass runs on the skip branch — but nothing says so.
So a CSV batch re-run that is about to skip records asks:

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
suppressed when the answer is already known: any flag that turns the
supplementary pass on (`--pull-supplementary`, `--refresh-supplementary`,
`--download-data-artifacts`, `--pull-everything`, `--llm-agent-retrieval`), an
explicit `--on-existing skip` or `supplement`, `--abstract-only`, or the
goal-aware flags (`--get-xml-or-html`, `--upgrade-existing` on the tiered path),
which do not skip records at all. Single-identifier runs never ask.

What is *not* remembered between runs: failures. `failed_dois.csv` is a report,
never read back, so every re-run retries every previous failure. Feed it back in
deliberately if you want only the retries.

### Retrieval and validation

FetchPDF checks downloaded PDFs for article identity and signs of truncation,
then tries other candidates when a file is rejected. If metadata cannot be
retrieved, the PDF is kept and reported as unverified.

When a PDF is unavailable, FetchPDF tries XML or HTML full text automatically.
Use `--no-xml-fallback` to disable this fallback.

Publisher HTML (T2) needs the `html` extra — see [Optional extras](#optional-extras).

Advanced users can customize retrieval order, source capabilities, and host rate
limits by copying [`ladder.json`](fetchpdf/retrieval/ladder.json) and setting
`FETCHPDF_LADDER` to the copy's path.

See [Retrieval and validation details](docs/retrieval.md) for validation rules,
examples, measurements, and rate-limit handling.

## Supplementary Material

`--pull-supplementary` fetches everything the authors deposited *alongside* the
paper — supplementary PDFs, spreadsheets, documents, images, archives, raw data —
and saves it as flat, numbered siblings of the main artifact. (Deposits reached
through link services go in a separate folder — see
[Linked datasets and code](#linked-datasets-and-code-stem_linked_artifactsjson).)

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
`{stem}_supplementary_info.json` is the only record of what `_4` actually is:

```json
{"index": 4, "filename": "..._supplementary_info_4.xls",
 "original_name": "pone.0000308.s004.xls", "label": "Table S1",
 "provider": "europepmc_supplements", "container": "PMC1817752_SupplementaryFiles.zip",
 "bytes": 42496, "sha256": "9f2c...", "role": "supplement"}
```

It also records what was *not* taken and why — `too-large` with the declared byte
count, `duplicate-of` with the index it duplicates, `not-a-document` for a body
that is not a plausible file, and `blocked` for a refusal or a bot-challenge page
served as HTTP 200 ([below](#refused-is-not-absent)). "This record has no supplementary
material" and "one 4 GB HDF5 was refused" are different facts, and the manifest is
where they stay distinguishable.

The manifest is also the skip signal: a second run over the same output directory
costs zero HTTP requests. Delete a numbered file and the next run restores it *at
its original index*, so removing `_2` never renumbers `_3`. Use
`--refresh-supplementary` to re-enumerate and pick up newly deposited files.

### Where the files come from

Twenty routes, tried in a fixed order so the numbering is reproducible:

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
| Figshare / Zenodo / OSF | repository DOIs, and datasets reached through link services |
| DataCite reverse relations | any DOI — finds deposits whose own DataCite record points at the article through a non-citation relation (e.g. `IsSupplementTo`); deposits that merely cite it are dropped |
| Crossref component DOIs | publishers that register components |
| JATS-declared URLs | absolute URLs the JATS itself declares (e.g. LWW permalinks) — recovery of last resort |
| OpenAIRE ScholeXplorer | any DOI — Scholix publication→dataset/software links |
| Europe PMC datalinks | any PMID — text-mined accessions and data citations |
| Full-text scan | deposits the paper names in its own prose — only under `--download-data-artifacts` |
| Atypon publisher SI | PNAS, Science, Annual Reviews, SAGE, T&F — only when Europe PMC withholds the SI or the record has no PMCID; drives a headed browser, so it needs Playwright, Chromium and a display (or Xvfb) |
| LLM retrieval agent | only under [`--pull-everything`](#--pull-everything-the-second-layer) or `--llm-agent-retrieval` |
| JCI | `10.1172/jci…` |

Wiley, ACS and MDPI serve HTTP 403 to plain requests and are not yet covered;
arXiv ancillary files are not yet covered either. Dryad (`10.5061`) and Harvard
Dataverse (`10.7910/DVN`) deposits are reached through the link services rather
than as top-level providers.

### Refused is not absent

A 401, 403 or 429, or a bot-challenge page served with a 200, says **nothing**
about whether the file exists. Those are recorded as `"reason": "blocked"` with
the URL that produced them, never as a download failure and never left to fall
through to `none_found` — Atypon returns 403 for
`/doi/suppl/10.1161/STROKEAHA.111.628537` while a browser fetches the file in
seconds, and that record used to come out of a run as "nothing published".

Blocked records appear in `missing_pdfs.html` with the URL to open, because the
fix is a person and not a retry:

```
Missing supplementary material and figures — 2026-09-07 12:00
[CLICK-THROUGH]  10.1161/strokeaha.111.628537  partial  —  fetch by hand
```

429 keeps its retries — transience is read off the status, which is where
"later" is actually written — and 404 stays a plain download failure, because
that one *is* an answer about the file. Old manifests are healed on the next
run: a 403 recorded before this distinction existed is reclassified in place,
with the original reason kept under `"was"`.

### Size cap

`--max-supplementary-mb` defaults to 300, per file. A file whose `Content-Length`
exceeds it is skipped without downloading anything; a server that under-reports is
aborted mid-stream and its partial removed. An archive such as the Europe PMC
bundle can only be opened once it has fully arrived, so it downloads under a
larger cap (4× the per-file cap, 1,200 MB by default) and the per-file cap is
then applied to each member. Nothing is ever truncated — a
truncated `.xlsx` is a corrupt zip that some readers open far enough to yield
wrong numbers, which is worse than not having the file.

### It never changes whether a record succeeded

A paper with no supplementary material is not a failure, and a repository serving
malformed JSON or timing out cannot turn a successful download into a failed one.
Supplementary files never appear in `failed_dois.csv`, in `source_tracking.csv`,
or in the format-composition tally; the missing-PDFs report lists gaps in them
only in its own separate section. The same holds for linked datasets,
[figures](#figure-images) and the
[`--pull-everything`](#--pull-everything-the-second-layer) agent.

### Linked datasets and code: `{stem}_linked_artifacts.json`

The link services (ScholeXplorer, Europe PMC datalinks) discover more than they
download, and the rest lands in a second sidecar. Every publication→dataset and
publication→software link they returned is recorded there with a
classification; the full-text scan and the retrieval agent record their
candidates there too. (The DataCite reverse query does not write to the
sidecar.)

- `owned` — affirmatively the paper's own material: a supplement-grade
  relation (`IsSupplementTo` and friends), or a repository deposit whose own
  DataCite record names this article
- `related` — everything citation-grade: datasets/software the paper cites,
  text-mined data citations that no metadata ties back to the article, and
  repositories with no file enumerator
- `tool_citation` — "xgboost software on GitHub" and friends: somebody's tool,
  not this paper's code. Recorded, never downloaded.
- `registry` — ClinicalTrials.gov and other registrations; a link, not a file

**By default only `owned` links are downloaded.** A "cites" link cannot
distinguish the paper's own deposit from a dataset the paper merely cites, so a
citation-grade link is promoted to `owned` only when the deposit's DataCite
record names the article. Under `--download-data-artifacts`, `related` links are
downloaded too, unless the deposit's own metadata says it belongs to a
*different* article (`--download-related-unverified` drops even that check).
Routed deposits in Figshare, Zenodo, OSF, Dryad or Harvard Dataverse go to those
enumerators and land under `{stem}_data_artifacts/<deposit>/` with their
original filenames (`routed_to_download: true` in the sidecar); they still get
an entry in the supplementary manifest. The same deposit named by several link
services is listed and downloaded once.

```bash
# Supplements + the paper's own linked deposits (Zenodo, Dryad, OSF, figshare, Dataverse)
fetchpdf papers.csv -o ./out --pull-supplementary

# Also take `related` deposits that do not claim another article,
# and repositories named in the paper's own full text
fetchpdf papers.csv -o ./out --download-data-artifacts

# Per-record dataset cap (default 500 MB), counted separately from supplements
fetchpdf papers.csv -o ./out --download-data-artifacts --max-data-artifact-mb 2000

# Expand replication-package zips (off by default — they have their own layout)
fetchpdf papers.csv -o ./out --download-data-artifacts --unpack-data-artifacts
```

`--download-data-artifacts` implies `--pull-supplementary`. What is *not*
downloaded still gets recorded: cited-but-not-owned datasets, tool citations,
trial registrations.

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

Download — repositories with file enumerators (links that pass the routing rules above):

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

**It is not literally everything, and the run says so.** The publishers listed
as uncovered under [Where the files come from](#where-the-files-come-from) and
the repositories under [Not yet downloadable](#where-dataset-links-come-from-and-what-can-be-downloaded)
are recorded in the sidecar, not fetched.

### What the agent is told

Three things a model does not have and layer 1 does:

1. the paper's own text, weighted toward its availability statements;
2. what has already been obtained, so it does not fetch a second copy;
3. **what the link services found and declined, with their reasons.** That is
   the richest input it gets. Measured over four corpora, those services
   returned 3,005 `related` links that were never routed, and of 280 `owned`
   ones 264 were BioStudies mirrors of supplements already in hand.

It proposes; the existing machinery disposes. A proposal naming a repository
routes through the same repository enumerators an index's link would, and a
GitHub repository is fetched as a tarball. Unlike an index's link, a proposal is
**not** currently put through the ownership check (`_should_route`), so review
what lands in `{stem}_data_artifacts/`. Anything it saves still meets the same
per-file size cap, sha256 deduplication and challenge-page sniff as every other
file. No failure of it can change whether a record succeeded.

### `--llm-backend`: two sandboxes, one result

| | `claude-cli` (default) | `openrouter` |
|---|---|---|
| auth | whatever the local Claude Code CLI already has | `OPENROUTER_API_KEY` in `.env.local` or the environment |
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

`--llm-backend` selects the retrieval agent's backend only. The adjudication of
uncertain full-text-scan candidates (on under `--pull-everything`, or with
`--llm-adjudicate-artifacts`) always calls the Claude Code CLI, as a single-turn
call with `--allowed-tools ""`; it does not get the `--disallowed-tools` /
`--strict-mcp-config` lockdown described above.

### What it costs, and what to expect

One agent session per record, of up to 12 model turns. `--max-llm-records N`
is the ceiling that stops an overnight batch spending without bound (a record
with no readable full text still uses up a slot, without a model call); records
past it still get every API route, and `{stem}_linked_artifacts.json` records
that the agent was *skipped* rather than that it found nothing.

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

## Figure Images

`--pull-figures` fetches the article's own figure images — the files the
publisher deposited, one per `<fig>` — into `{stem}_figures/`, with a
`{stem}_figures.json` manifest.

```bash
fetchpdf papers.csv -o ./out --pull-figures
fetchpdf "10.1371/journal.pone.0000308" -o ./out --pull-figures
```

```
out/
  10.1371--journal.pone.0000308.pdf
  10.1371--journal.pone.0000308_figures/pone.0000308.g001.jpg
  10.1371--journal.pone.0000308_figures/pone.0000308.g002.jpg
  10.1371--journal.pone.0000308_figures.json      <- the manifest
```

**This is not `--extract-images`.** That flag dumps the bitstreams embedded
*inside a PDF*, for byte-identity work that any re-encoding destroys. This one
fetches the publisher's image files, which is the only route available when
there is no PDF at all. Neither substitutes for the other, and they write to
separate directories.

**PMC only, and it says so.** The two routes are PMC's AWS Open Data mirror —
preferred, because the package holds the figure files themselves and declares
each object's size and md5 before a byte moves — and the PMC blob CDN, whose
per-file paths are listed nowhere but the article page. A file from the mirror
is recorded `"provenance": "original"`; one from the CDN is `"render"`, because
it is a re-encoded copy and calling it original would invite byte-identity
conclusions about PMC's pipeline dressed up as conclusions about the authors'.
A `{stem}.fulltext.html` already on disk is read instead of re-requesting the
page.

**Every refusal is named, per figure and per record**, and a manifest with zero
figures is still written — "we asked and PMC listed nothing" and "we never
asked" are different facts, and a missing file cannot tell them apart:

```json
{"id": "pone-0000308-g001", "figure_label": "Figure 1",
 "caption": "The 41 clinical trial publications which publicly shared…",
 "href": "pone.0000308.g001", "provenance": "original",
 "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1817752.1/pone.0000308.g001.jpg",
 "file": "pone.0000308.g001.jpg", "sha256": "…", "bytes": 84120,
 "content_type": "image/jpeg", "status": "ok"}
```

Record-level refusals are `no_pmcid`, `no_jats_or_page` and
`page_without_figure_links`; per-figure ones are `figure_not_on_page`,
`download_failed:<status>` and `blocked:<status>` — a publisher or CDN that
refuses this client while serving a person has not said the figure does not
exist, it has said to send a person.

Like the supplementary pass, this runs beside retrieval and never changes
whether a record succeeded, it runs for records already on disk, and a manifest
that fetched something makes a re-run free.

## API Reference

### `fetch_pdf(doi, save_path, email=None, verbose=False, delay=0.1, ...)`

Download a PDF from a DOI (or a PMID) using multiple fallback sources. `fetch_pdf` does not create `save_path`'s directory; create it first.

**Parameters:**
- `doi` (str): DOI or PMID identifier to download
- `save_path` (str): Path where the PDF should be saved
- `email` (str, optional): Email for API calls (default: `EMAIL` from `.env.local`)
- `verbose` (bool, optional): Print detailed progress (default: False)
- `delay` (float, optional): Delay between API calls in seconds (default: 0.1)

Further keyword arguments mirror the format flags of the CLI (`prioritize_xml`,
`xml_only`, `xml_html_only`, `get_xml_or_html`, `to_markdown`, `target_task`,
`upgrade_existing`, `want_provenance`, `allow_xml_fallback`, `use_playwright`) —
see `help(fetch_pdf)`.

**Returns:**
- `str`: Path to the saved file if successful, `None` otherwise. Usually the PDF,
  but it can be a `.xml` or `.html` path when only structured full text was
  available (disable that with `allow_xml_fallback=False`)

### `batch_fetch_pdfs(dois, output_dir, email=None, verbose=False, delay=0.1, workers=1, create_missing_report=True, ...)`

Download PDFs for multiple DOI/PMID identifiers with optional parallel processing.

**Parameters:**
- `dois` (list or str): List of DOI/PMID identifiers or path to CSV file
- `output_dir` (str): Directory to save PDFs
- `email` (str, optional): Email for API calls (default: `EMAIL` from `.env.local`)
- `verbose` (bool, optional): Print detailed progress (default: False)
- `delay` (float, optional): Delay between API calls per worker (default: 0.1)
- `workers` (int, optional): Number of parallel workers; 1 = sequential (default: 1)
- `create_missing_report` (bool, optional): Create HTML report for failed downloads (default: True)

Most retrieval flags of the CLI have a keyword argument of the same name in
snake_case — e.g. `pull_supplementary`, `download_data_artifacts`, `pull_figures`,
`make_subfolder`, `want_provenance` (`--provenance`), and
`max_supplementary_bytes` (bytes, where the CLI's `--max-supplementary-mb` takes
MB). See `help(batch_fetch_pdfs)` for the full list.

**Returns:**
- `list`: List of tuples `(id, success, save_path)` for each identifier processed —
  `id` is the resolved DOI or bare PMID, `save_path` is `None` on failure. With
  `track_source=True`, 4-tuples `(id, success, save_path, source)`.

**Side Effects:**
- Creates `output_dir` if needed
- Writes `missing_pdfs.html` in `output_dir` when downloads fail, or when
  supplementary material or figures could not be obtained (unless
  `create_missing_report=False`)

### `pull_supplementary_for(raw_identifier, doi, save_path, ...)`

Fetch every supplementary file for one record, as flat siblings of `save_path`.

A separate call rather than a keyword on `fetch_pdf`, deliberately: the
tiered engine re-enters that function with a temporary path, so a flag threaded
through it would write supplementary siblings next to a file that is about to be
deleted. Two calls make that impossible rather than merely discouraged.

```python
import os
from fetchpdf import fetch_pdf, pull_supplementary_for

doi = "10.1371/journal.pone.0000308"
os.makedirs("out", exist_ok=True)
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
- `SupplementarySummary`: `.status` (`ok` | `none_found` | `partial` | `incomplete` | `error` | `skipped`), `.written`, `.skipped`, `.bytes_written`, `.manifest_path`, `.paths`, `.missing_declared`, `.blocked_urls` (URLs a publisher refused this client but serves to a person)

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

On the default path the tool walks the chain below (`_fetch_pdf_chain` in
`fetchpdf/fetchpdf.py`) until a file is accepted. Every PDF is checked against the
record before it is accepted ([details](docs/retrieval.md)). The order puts faster,
more reliable sources first and saves rate-limited quotas for the records that
need them. Under `--prioritize-xml` and the other format flags, the tier ladder in
[`ladder.json`](fetchpdf/retrieval/ladder.json) governs the order instead — see
[Format-Prioritized Retrieval](#format-prioritized-retrieval).

### Special DOI Handlers (Pattern-Matched)
These run first when the DOI matches (by prefix, or by the name appearing anywhere in the DOI):

- **OSF** (Open Science Framework) - Projects (`10.17605/osf.io/*`), Preprints (`10.31234/osf.io/*`), any `osf.io` DOI
  - Direct download URLs, Playwright browser automation, API fallback
- **SSRN** (Social Science Research Network) - `10.2139/*`
  - Playwright with Cloudflare handling, download button detection
- **Figshare** - `10.6084/*`
  - Figshare API for file metadata and download URLs
- **PsychArchives** - `10.23668/*`
  - Leibniz psychology repository bitstream extraction
- **Zenodo** - `10.5281/*`
  - Files listed by Zenodo's InvenioRDM API

### Standard Fallback Chain (All DOIs)

1. **PubMed Central (PMC)**
   - NCBI ID converter → Europe PMC `?pdf=render`
   - If there is no PDF: Europe PMC JATS, then NCBI efetch JATS (skipped with `--no-xml-fallback`)
2. **eLife XML** — eLife DOIs only; skipped with `--no-xml-fallback`
3. **eScholarship** — via PubMed LinkOut
4. **Unpaywall** — legal open-access aggregator; requires `EMAIL`
5. **Crossref**
   - Direct PDF links in Crossref metadata, and text-mining XML links
   - Landing page scraping (`citation_pdf_url` and similar), plus a direct `/doi/pdf/` try for Taylor & Francis
   - Re-enters the chain on a related preprint DOI (PsyArXiv, bioRxiv, …), reported as `crossref_preprint`
6. **Europe PMC** — search API
7. **Semantic Scholar**
   - `openAccessPdf` field, with a landing page fallback for OJS sites
   - PsyArXiv → OSF URL conversion
8. **OpenAlex** — open-access locations; placed after Semantic Scholar to conserve OpenAlex's download quota
9. **CORE** (core.ac.uk) — 40M+ repository papers; needs `COREAPIKEY`
10. **DOAJ** — open-access full-text links
11. **DataCite content URLs** — direct content and related resource links
12. **Wiley rendered PDF** — only with `--add-playwright`
13. **APA supplemental** — APA supplemental files, converting `.doc`/`.docx` to PDF when available
14. **Direct DOI resolver**
    - Follows `https://doi.org/{doi}` to the landing page
    - Handles Crossref chooser pages (multiple resolution)
    - Scrapes PDF links from HTML (`citation_pdf_url`, href patterns)
    - Publisher-specific deterministic URL patterns (Wiley, T&F, SAGE, MIT Press, …)
15. **DataCite related identifiers**
    - Supplementary material → main paper
    - Versioned DOI resolution (`IsIdenticalTo`, `IsVersionOf`)
16. **DOI → PMID fallback**
    - Converts the DOI to a PMID and tries PMID-native sources (the PubMed page's `citation_pdf_url`)
17. **Google Scholar via SerpApi** (optional)
    - Enabled by `SERPAPI_API_KEY` in `.env.local` or the environment
    - Searches the quoted DOI, then the quoted article title if the DOI search returned results but no accepted PDF
    - At most two searches per unresolved DOI, with up to five results per search;
      searches consume your SerpApi account quota
    - Prioritizes PDF resource links, then tries article landing pages; uses the
      existing title matching and downloaded-PDF identity checks
    - Skips without a key. HTTP 401, 403 or 429 disables it for the rest of the
      process; any other request error, or an error response from SerpApi, ends
      the SerpApi attempt for that record
    - Reported as `serpapi_scholar` in source tracking

### Final Fallbacks

18. **Elsevier full-text API**
    - Elsevier records only (`10.1016/*`, or a Crossref landing page on sciencedirect.com / elsevier.com)
    - Tries the PDF first, then full-text XML (the XML half is skipped with `--no-xml-fallback`)
    - Requires `ELSEVIER_TDM_API_KEY`

If the whole chain yields no PDF, `fetch_pdf` then tries structured full text
(T1 XML / T2 HTML) from the tier ladder before giving up; `--no-xml-fallback`
turns this off.

---

**Total: 18 standard steps + 5 special handlers.**

## Requirements

- Python 3.10+
- requests, urllib3, python-dotenv
- playwright (Chromium itself is optional — see [Installation](#optional-install-chromium-for-browser-automation))
- pypdf (every retrieved PDF is identity-checked, so a PDF text engine is required)

Optional extras are listed under [Optional extras](#optional-extras).

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
