"""Render a retrieved article XML as one self-contained HTML file, and as a PDF.

The input is the `.xml` a run already saved: JATS from Europe PMC, PMC efetch
or a preprint server, or Elsevier's `full-text-retrieval-response`. The output
is `{stem}_from_xml.html` and, on request, `{stem}_from_xml.pdf`, beside it.

NOT THE PUBLISHER'S PDF, AND NAMED SO IT CANNOT BE MISTAKEN FOR ONE. A file at
`{stem}.pdf` means "the article as the publisher typeset it" to the skip check,
the validators and every downstream reader. A rendering of the XML is a
different object -- our layout, our fonts, figures as separate images -- so it
carries the `_from_xml` suffix the Markdown conversion already uses, and the
page itself says what it is in a banner above the title.

ONE WALKER, NOT TWO. The prose, section order, float placement and tables come
from to_markdown.jats_to_markdown, which already reads both vocabularies and
already emits every table as canonical HTML with colspan/rowspan intact. This
module hands it the figure images as `data:` URIs, renders its Markdown with a
CommonMark parser (raw HTML blocks pass through untouched, so the tables are
never re-parsed), and wraps the result in one page with inline CSS. A second
walker written for HTML would drift from the first on exactly the cases --
Elsevier floats, image-only tables, footnotes -- that took measurement to get
right there.

FIGURES COME FROM THE FIGURES PASS. JATS and Elsevier XML name their images and
never contain them. `{stem}_figures/` and its manifest, written by figures.py
(`--pull-figures`), are the source; `fetch_figures=True` runs that pass first.
A figure whose file is not there keeps the Markdown's visible placeholder, so
a rendering with a missing forest plot looks incomplete rather than complete.
TIFF, which no browser shows, is re-encoded as PNG when Pillow or PyMuPDF is
installed, and left as a placeholder (with a warning) when neither is.

NOTHING EXTERNAL. No stylesheet, font, script or image is fetched by the page;
an `http` URL in a `src` would make the file depend on a server that may not
be there when someone opens it. MathJax is therefore not used: formulas stay
as the walker's `$$ ... $$` text.

Also left out, as in the Markdown: the reference list. And XML from a
publisher with no figure route here (crossref_tdm from others than Elsevier)
renders with placeholders.

The PDF is the HTML printed by Playwright's Chromium (pip install
'fetchpdf[browser]' and `playwright install chromium`; the system Chrome is
used when Playwright's own build is absent).
"""

import base64
import html as html_lib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ._util import iso
from .to_markdown import Conversion, image_keys, jats_to_markdown, record_stem_for

HTML_SUFFIX = "_from_xml.html"
PDF_SUFFIX = "_from_xml.pdf"

#: Root elements of the article XML a run saves. Anything else ending `.xml` in
#: an output directory (a supplementary file, a PMC S3 listing) is not rendered.
_ARTICLE_ROOT_RE = re.compile(rb"<(?:\w+:)?(?:article|pmc-articleset|full-text-retrieval-response)\b")

_DOI_RES = (
    re.compile(rb'<article-id[^>]*pub-id-type="doi"[^>]*>\s*([^<\s]+)\s*<'),
    re.compile(rb"<prism:doi>\s*([^<\s]+)\s*<"),
    re.compile(rb"<(?:ce|xocs):doi>\s*([^<\s]+)\s*<"),
)


@dataclass
class Rendering:
    """The page, plus what a reader needs to trust or distrust it."""

    html: str
    conversion: Conversion
    doi: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.conversion.ok


# -- paths --------------------------------------------------------------------


def html_path_for(xml_path: str) -> str:
    return record_stem_for(xml_path) + HTML_SUFFIX


def pdf_path_for(xml_path: str) -> str:
    return record_stem_for(xml_path) + PDF_SUFFIX


def is_article_xml(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return bool(_ARTICLE_ROOT_RE.search(head))


# -- figures ------------------------------------------------------------------


def figure_map(xml_path: str, warnings: Optional[List[str]] = None) -> Dict[str, str]:
    """Image key -> `data:` URI, for every figure file on disk for this record.

    Keys are the manifest's href (the JATS `<graphic>` href, or the Elsevier
    locator) and the file's own name, each reduced by `image_keys`, so the
    walker finds an image under whichever name the XML used.
    """
    from .figures import FIGURES_DIR_SUFFIX, MANIFEST_SUFFIX

    warnings = warnings if warnings is not None else []
    stem = record_stem_for(xml_path)
    directory = stem + FIGURES_DIR_SUFFIX
    if not os.path.isdir(directory):
        return {}

    named: Dict[str, List[str]] = {}
    try:
        with open(stem + MANIFEST_SUFFIX, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    for row in manifest.get("figures") or []:
        if row.get("file"):
            named.setdefault(row["file"], []).append(str(row.get("href") or ""))
    # Files with no manifest row (copied in by hand, or a manifest lost) are
    # still matched by their own name.
    for name in sorted(os.listdir(directory)):
        named.setdefault(name, [])

    images: Dict[str, str] = {}
    for name, hrefs in named.items():
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        uri = data_uri(path, warnings)
        if not uri:
            continue
        for key in [k for href in hrefs for k in image_keys(href)] + image_keys(name):
            images.setdefault(key, uri)
    return images


def data_uri(path: str, warnings: Optional[List[str]] = None) -> str:
    """A file as a `data:` URI, typed by its bytes, never by its extension."""
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return ""
    mime = sniff_image(content)
    if mime == "image/tiff":
        content = _tiff_to_png(content)
        if content is None:
            if warnings is not None:
                warnings.append(f"{os.path.basename(path)}: TIFF, and neither Pillow "
                                f"nor PyMuPDF is installed to convert it")
            return ""
        mime = "image/png"
    if not mime:
        if warnings is not None:
            warnings.append(f"{os.path.basename(path)}: not a recognised image")
        return ""
    return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")


def sniff_image(content: bytes) -> str:
    head = content[:512]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if b"<svg" in head:
        return "image/svg+xml"
    return ""


def _tiff_to_png(content: bytes) -> Optional[bytes]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            out = io.BytesIO()
            image.convert("RGBA" if "A" in image.getbands() else "RGB").save(out, "PNG")
            return out.getvalue()
    except Exception:                           # noqa: BLE001 - try the next converter
        pass
    try:
        import fitz

        return fitz.open(stream=content, filetype="tiff")[0].get_pixmap().tobytes("png")
    except Exception:                           # noqa: BLE001 - no converter
        return None


# -- rendering ----------------------------------------------------------------


def xml_to_html(xml_path: str, fetch_figures: bool = False, verbose: bool = False,
                **figure_kwargs) -> Rendering:
    """Render one article XML to a self-contained HTML page.

    `fetch_figures` runs the figures pass first (network); otherwise only the
    images already in `{stem}_figures/` are used. `figure_kwargs` go to
    figures.pull_for_record (resolver, http, email, delay, ...), so a batch
    can lend its shared client.
    """
    with open(xml_path, "rb") as f:
        content = f.read()
    doi = doi_for(xml_path, content)
    warnings: List[str] = []

    if fetch_figures:
        from .figures import pull_for_record

        summary = pull_for_record(doi or os.path.basename(record_stem_for(xml_path)),
                                  doi=doi or None, save_path=xml_path,
                                  verbose=verbose, **figure_kwargs)
        if summary.refusal:
            warnings.append(f"figures: {summary.refusal} -- {summary.detail}")

    images = figure_map(xml_path, warnings)
    conversion = jats_to_markdown(content, name=os.path.basename(xml_path), images=images)
    rendering = Rendering(html="", conversion=conversion, doi=doi, warnings=warnings)
    if conversion.ok:
        rendering.html = _page(xml_path, content, conversion, doi, warnings)
    return rendering


def doi_for(xml_path: str, content: bytes = b"") -> str:
    """The article's DOI: from its own XML, else decoded from the filename."""
    for pattern in _DOI_RES:
        match = pattern.search(content or b"")
        if match:
            return html_lib.unescape(match.group(1).decode("utf-8", errors="replace"))
    from ..fetchpdf import safe_filename_to_doi_variants

    stem = os.path.basename(record_stem_for(xml_path))
    variants = safe_filename_to_doi_variants(stem)
    return variants[0] if variants and variants[0].startswith("10.") else ""


def markdown_to_html(markdown: str) -> str:
    """CommonMark with raw HTML on: the table and figure blocks pass through."""
    try:
        from markdown_it import MarkdownIt
    except ImportError as e:
        raise ImportError(
            "rendering HTML needs markdown-it-py: pip install 'fetchpdf[render]'"
        ) from e
    return MarkdownIt("commonmark", {"html": True}).render(markdown)


def _page(xml_path: str, content: bytes, conversion: Conversion, doi: str,
          warnings: List[str]) -> str:
    from .figures import is_elsevier_xml

    body = markdown_to_html(conversion.markdown)
    title_match = re.search(r"^# (.+)$", conversion.markdown, re.M)
    title = title_match.group(1).strip() if title_match else os.path.basename(xml_path)
    kind = "Elsevier full-text XML" if is_elsevier_xml(content) else "JATS XML"

    facts = [
        f"Rendered from <code>{_esc(os.path.basename(xml_path))}</code> ({kind}) "
        f"by fetchpdf on {iso(time.time())[:10]}. "
        f"This is not the publisher's typeset version.",
        f"Figures: {conversion.n_figures_embedded} of {conversion.n_figures} embedded. "
        f"Tables: {conversion.n_tables}"
        + (f", plus {conversion.n_tables_empty} published only as images"
           if conversion.n_tables_empty else "")
        + ". References omitted.",
    ]
    if doi:
        facts.insert(1, f"DOI: {_esc(doi)}")
    notes = list(dict.fromkeys(warnings + conversion.failures))
    banner = "".join(f"<p>{line}</p>" for line in facts)
    if notes:
        banner += "<ul>" + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul>"

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<meta name=\"generator\" content=\"fetchpdf to_html\">\n"
        + (f"<meta name=\"citation_doi\" content=\"{_esc(doi)}\">\n" if doi else "")
        + f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<aside class=\"provenance\">{banner}</aside>\n<main>\n{body}</main>\n"
        "</body>\n</html>\n"
    )


def _esc(text: str) -> str:
    return html_lib.escape(text or "", quote=True)


_CSS = """
:root { color-scheme: light; }
body { margin: 0 auto; max-width: 52rem; padding: 1.5rem 1rem 3rem;
  font: 16px/1.55 Georgia, "Times New Roman", serif; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.6rem; line-height: 1.25; margin: 1.2rem 0 1rem; }
h2 { font-size: 1.25rem; margin: 1.8rem 0 .6rem; border-bottom: 1px solid #ddd; padding-bottom: .2rem; }
h3, h4, h5, h6 { font-size: 1.05rem; margin: 1.3rem 0 .5rem; }
p { margin: .55rem 0; }
.provenance { font: 13px/1.45 system-ui, sans-serif; color: #444; background: #f5f5f0;
  border-left: 3px solid #b58900; padding: .5rem .8rem; margin-bottom: 1rem; }
.provenance p { margin: .15rem 0; }
.provenance ul { margin: .3rem 0 0 1rem; padding: 0; }
blockquote { margin: .8rem 0; padding: .4rem .8rem; border-left: 3px solid #c33;
  background: #fbf3f3; font-size: .92rem; }
figure { margin: 1.2rem 0; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figcaption { text-align: left; font-size: .9rem; margin-top: .4rem; }
table { border-collapse: collapse; margin: .6rem 0 1rem; font: 12.5px/1.35 system-ui, sans-serif;
  display: block; overflow-x: auto; max-width: 100%; }
th, td { border: 1px solid #bbb; padding: .2rem .4rem; vertical-align: top; text-align: left; }
th { background: #f0f0f0; }
tr { break-inside: avoid; }
code { font-size: .85em; }
@page { size: A4; margin: 16mm 14mm 18mm; }
@media print {
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  table { display: table; font-size: 8pt; }
  h2, h3 { break-after: avoid; }
}
"""


# -- PDF ----------------------------------------------------------------------


def html_to_pdf(page_html: str, out_path: str, timeout_ms: int = 120000) -> str:
    """Print a page to PDF with Chromium. Raises on failure; callers decide."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "printing a PDF needs Playwright: pip install 'fetchpdf[browser]' "
            "&& playwright install chromium"
        ) from e

    tmp = out_path + ".tmp"
    with sync_playwright() as p:
        launch = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
        try:
            browser = p.chromium.launch(**launch)
        except Exception:                       # noqa: BLE001 - Playwright's build absent
            browser = p.chromium.launch(channel="chrome", **launch)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_content(page_html, wait_until="load")
            page.pdf(
                path=tmp, format="A4", print_background=True,
                prefer_css_page_size=True, display_header_footer=True,
                header_template="<div></div>",
                footer_template=(
                    '<div style="font-size:8px;width:100%;text-align:center;color:#666">'
                    '<span class="pageNumber"></span> / <span class="totalPages"></span>'
                    "</div>"
                ),
            )
        finally:
            browser.close()
    with open(tmp, "rb") as f:
        if f.read(4) != b"%PDF":
            os.unlink(tmp)
            raise RuntimeError("Chromium did not produce a PDF")
    os.replace(tmp, out_path)
    return out_path


# -- writing ------------------------------------------------------------------


def render_record(xml_path: str, html: bool = True, pdf: bool = False,
                  fetch_figures: bool = False, overwrite: bool = False,
                  verbose: bool = False, **figure_kwargs) -> dict:
    """Write `{stem}_from_xml.html` and/or `{stem}_from_xml.pdf`. Never raises.

    Returns {"html": path|None, "pdf": path|None, "figures": n,
    "figures_embedded": k, "error": str}. A document that will not render is
    one bad record, not a dead batch -- the same contract as write_markdown.
    """
    result = {"html": None, "pdf": None, "figures": 0, "figures_embedded": 0, "error": ""}
    html_out, pdf_out = html_path_for(xml_path), pdf_path_for(xml_path)
    want_html = html and (overwrite or not os.path.exists(html_out))
    want_pdf = pdf and (overwrite or not os.path.exists(pdf_out))
    if html and not want_html:
        result["html"] = html_out
    if pdf and not want_pdf:
        result["pdf"] = pdf_out
    if not (want_html or want_pdf):
        return result

    name = os.path.basename(xml_path)
    try:
        rendering = xml_to_html(xml_path, fetch_figures=fetch_figures,
                                verbose=verbose, **figure_kwargs)
    except Exception as e:                      # noqa: BLE001 - one bad record
        result["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        if verbose:
            print(f"  render: {name} raised {result['error']}")
        return result
    result["figures"] = rendering.conversion.n_figures
    result["figures_embedded"] = rendering.conversion.n_figures_embedded
    if not rendering.ok:
        result["error"] = "; ".join(rendering.conversion.failures) or "nothing to render"
        if verbose:
            print(f"  render: {name} -> nothing ({result['error']})")
        return result

    if want_html:
        try:
            tmp = html_out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(rendering.html)
            os.replace(tmp, html_out)
            result["html"] = html_out
        except OSError as e:
            result["error"] = f"could not write {html_out}: {e}"
    if want_pdf:
        try:
            result["pdf"] = html_to_pdf(rendering.html, pdf_out)
        except Exception as e:                  # noqa: BLE001 - one bad record
            result["error"] = f"PDF: {type(e).__name__}: {str(e)[:150]}"
    if verbose:
        written = ", ".join(os.path.basename(p) for p in (result["html"], result["pdf"]) if p)
        print(f"  render: {written or 'nothing written'} "
              f"({result['figures_embedded']}/{result['figures']} figures embedded)"
              + (f" -- {result['error']}" if result["error"] else ""))
    return result


def _xml_paths(paths: List[str]) -> List[str]:
    found = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                found.extend(os.path.join(root, n) for n in sorted(files)
                             if n.endswith(".xml") and is_article_xml(os.path.join(root, n)))
        elif path.endswith(".xml"):
            found.append(path)
    return found


def main(argv=None):
    """Render article XML already on disk to self-contained HTML and/or PDF."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="fetchpdf-render",
        description=(
            "Render retrieved article .xml (JATS or Elsevier) to "
            "{stem}_from_xml.html, with figures and tables embedded, and/or "
            "{stem}_from_xml.pdf. A rendering of the XML, not the publisher's PDF."
        ),
    )
    parser.add_argument("paths", nargs="+", help="XML files, or directories searched recursively")
    parser.add_argument("--html", action="store_true", help="Write the HTML (default when neither is given)")
    parser.add_argument("--pdf", action="store_true", help="Write the PDF (needs Playwright)")
    parser.add_argument("--fetch-figures", action="store_true",
                        help="Run the figures pass first (PMC, or the Elsevier object API)")
    parser.add_argument("--overwrite", action="store_true", help="Rewrite outputs that exist")
    parser.add_argument("--email", default=None, help="Contact email for polite API use")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    want_html = args.html or not args.pdf
    paths = _xml_paths(args.paths)
    if not paths:
        print("❌ No article XML found")
        return 1

    counts = {"html": 0, "pdf": 0, "failed": 0, "figures": 0, "embedded": 0}
    for path in paths:
        print(f"🖨️  {path}")
        result = render_record(path, html=want_html, pdf=args.pdf,
                               fetch_figures=args.fetch_figures, overwrite=args.overwrite,
                               verbose=args.verbose, email=args.email)
        counts["html"] += bool(result["html"])
        counts["pdf"] += bool(result["pdf"])
        counts["failed"] += bool(result["error"])
        counts["figures"] += result["figures"]
        counts["embedded"] += result["figures_embedded"]
        for key in ("html", "pdf"):
            if result[key]:
                print(f"   {result[key]}")
        if result["error"]:
            print(f"   ⚠️  {result['error']}")
    print(f"\n{len(paths)} record(s): {counts['html']} HTML, {counts['pdf']} PDF, "
          f"{counts['failed']} with errors; {counts['embedded']} of "
          f"{counts['figures']} figures embedded")
    return 0 if not counts["failed"] else 2


if __name__ == "__main__":
    import sys

    sys.exit(main())
