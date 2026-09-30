# Retrieval and validation details

Technical details, examples, and measurements for FetchPDF retrieval.
For usage and format priorities, see the [README](../README.md#the-tier-ladder).

## Reordering without editing code

The ladder, the per-source capability map and the per-host rate limits live in
[`fetchpdf/retrieval/ladder.json`](../fetchpdf/retrieval/ladder.json). Point
`FETCHPDF_LADDER` at your own copy to override it.

## Validation

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

### The right article, but not all of it

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

### Crossref under parallel load

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

### When there is no PDF, there may still be the paper

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

## T2 HTML support

Publisher HTML needs a forgiving parser, the `html` extra — see
[Optional extras](../README.md#optional-extras). Without it the T2 rung demotes
with a logged reason and the ladder descends normally.
