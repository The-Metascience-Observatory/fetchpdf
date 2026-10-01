"""Turn a retrieved XML or HTML artifact into Markdown for an LLM.

Prose becomes Markdown. **Tables do not.** Every table is emitted as canonical
minimal HTML, inline at its position in the document, with its caption and
footnotes attached.

That is the one non-obvious decision here, so the reasoning, plainly:

  Markdown has no span mechanism. A header cell spanning two treatment arms
  (`<th colspan="2">Cerebrolysin</th>`) cannot be expressed, so the header row
  collapses and every value to its right shifts one column. The result is still
  a valid Markdown table, and a model reads it confidently. Converting XML to
  Markdown tables would reintroduce exactly the corruption the tier ladder was
  built to avoid, one step after paying to avoid it.

  Inline HTML is part of the Markdown spec (CommonMark defines HTML blocks), so
  this is ordinary Markdown, not a hybrid format. Models see this shape
  constantly in READMEs and docs.

  Uniformity is the real requirement: EVERY table is HTML, including simple ones
  with no spans where pipes would do. Mixing pipes and HTML would make a
  three-column header ambiguous -- no spans, or spans lost in conversion? -- and
  that ambiguity is worse than either format alone.

Figures are recorded, never inlined. JATS references images by filename and
never contains them, so a `<fig>` becomes a visible placeholder naming what is
missing. A record whose key outcome lives in a forest plot should look
incomplete rather than complete.

JATS abstracts, publication history, author/funding notes, and non-bibliographic
back matter are included for internal-consistency screening. References are omitted.
"""

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

from ._util import iso, localname
from .normalize import html_table_to_canonical, jats_table_to_canonical, token_count

_WS_RE = re.compile(r"\s+")

#: Elsevier's Dublin Core title, matched on the full tag: `localname` alone would
#: make every `<title>` in the document a candidate.
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"

#: Block children an Elsevier `ce:para` mixes into its running text. They are
#: excluded from the paragraph's inline text and dispatched right after it, so
#: a table lands after the sentence that introduces it and a footnote's body is
#: never spliced into the middle of the sentence that cites it.
_PARA_BLOCKS = (
    "display", "list", "table", "figure", "e-component", "footnote",
    "float-anchor", "textbox", "enunciation",
)

#: Emitted above every table so a reader (human or model) knows the format is
#: deliberate and that the span attributes carry meaning.
TABLE_FORMAT_NOTE = (
    "Tables below are HTML, not Markdown: `colspan` and `rowspan` are "
    "authoritative and Markdown cannot express them."
)


@dataclass
class Conversion:
    """The Markdown, plus what a reader needs to trust or distrust it."""

    markdown: str
    n_tables: int = 0
    n_tables_empty: int = 0        # table-wrap with no convertible <table>
    n_figures: int = 0             # referenced, never included
    prose_tokens: int = 0
    table_tokens: int = 0
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.markdown.strip())


def convert_file(path: str) -> Optional[Conversion]:
    """Convert one artifact by extension. Returns None for formats with no route."""
    with open(path, "rb") as f:
        content = f.read()
    stem = os.path.basename(path)
    if path.endswith(".xml"):
        return jats_to_markdown(content, name=stem)
    if path.endswith((".fulltext.html", ".html")):
        return html_to_markdown(content, name=stem)
    return None


# -- JATS -------------------------------------------------------------------


def jats_to_markdown(content: bytes, name: str = "") -> Conversion:
    """JATS/TEI/Elsevier XML -> Markdown, walking the body in document order.

    Elsevier's `full-text-retrieval-response` shares no tag names with JATS
    (`ce:section`/`ce:para`/`ce:table` against `sec`/`p`/`table-wrap`), so one
    walker carries both vocabularies without either branch shadowing the other.
    """
    result = Conversion(markdown="")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        result.failures.append(f"XML unparseable: {str(e)[:100]}")
        return result

    parts: List[str] = []
    title = _document_title(root)
    if title:
        parts.append(f"# {title}\n")

    # `or` would call bool() on the Element, which ElementTree deprecates (an
    # element with no children is falsey, so `or` would also silently fall
    # through on an empty-but-present <body>).
    body = _find_one(root, "body")
    if body is None:
        body = _find_one(root, "text")
        # A TEI <text> holds divisions. Elsevier's scanned-legacy envelopes
        # (`xocs:rawtext` only, no body) carry a childless <ce:text> topic label
        # under <doctopics>, which used to pass here and yield a title-only file
        # with no warning (43 of 428 corpus files, 2026-09-02).
        if body is not None and not len(body):
            body = None
    if body is None:
        result.failures.append("no <body> or TEI <text>; nothing to convert")
        return result

    floats = _FloatMap(root)
    # Scope metadata to the article, never to cited articles in its references.
    article = _find_one(root, "article")
    if article is not None:
        front = next((e for e in article if localname(e.tag) == "front"), None)
        if front is not None:
            meta = next((e for e in front if localname(e.tag) == "article-meta"), None)
            if meta is not None:
                _emit_article_metadata(meta, parts, result, floats)
    _walk_jats(body, parts, result, depth=2, floats=floats)
    if article is not None:
        back = next((e for e in article if localname(e.tag) == "back"), None)
        if back is not None:
            back_parts = []
            _walk_jats(back, back_parts, result, depth=3, floats=floats)
            if back_parts:
                parts.append("\n## Back matter\n")
                parts.extend(back_parts)
    _emit_leftover_floats(floats, parts, result, depth=2)
    return _finish(parts, result)


def _emit_article_metadata(meta, parts, result, floats) -> None:
    """Keep screening evidence, including partial dates without inventing precision."""
    dates = []
    for child in meta:
        tag = localname(child.tag)
        if tag in ("abstract", "trans-abstract"):
            heading = _first_text(child, "title", direct_only=True) or "Abstract"
            language = child.get("{http://www.w3.org/XML/1998/namespace}lang")
            if language:
                heading += f" ({language})"
            parts.append(f"\n## {heading}\n")
            _walk_jats(child, parts, result, depth=3, floats=floats)
        elif tag in ("pub-date", "history"):
            for date in ([child] if tag == "pub-date" else list(child)):
                if localname(date.tag) not in ("date", "pub-date"):
                    continue
                kind = date.get("date-type") or date.get("pub-type") or "unspecified"
                prefix = "Publication" if tag == "pub-date" else "History"
                medium = date.get("publication-format")
                label = f"{prefix}: {kind}" + (f" ({medium})" if medium else "")
                # Component labels avoid ambiguous numeric month/day ordering,
                # and preserve seasons, string-date, and month-only precision.
                values = [f"{localname(e.tag)}={_clean(''.join(e.itertext()))}"
                          for e in date if _clean(''.join(e.itertext()))]
                if date.get("iso-8601-date"):
                    values.append(f"iso-8601-date={date.get('iso-8601-date')}")
                value = "; ".join(values) or _inline_text(date)
                if value:
                    dates.append(f"- {label}: {value}")
    if dates:
        parts.append("\n## Publication dates and history\n")
        parts.extend(dates)
    for child in meta:
        if localname(child.tag) in ("author-notes", "funding-group", "support-group"):
            _emit_one(child, meta, parts, result, 2, floats)


def _document_title(root) -> str:
    """JATS <article-title>, else Elsevier's Dublin Core title, else <head>/<title>."""
    title = _first_text(root, "article-title")
    if title:
        return title
    for el in root.iter():
        if el.tag == _DC_TITLE:
            title = _clean(" ".join(el.itertext()))
            if title:
                return title
    for el in root.iter():
        if localname(el.tag) in ("head", "simple-head"):
            title = _first_text(el, "title", direct_only=True)
            if title:
                return title
    return ""


class _FloatMap:
    """Elsevier floats, keyed by id, each emitted once.

    `ce:floats` sits outside `ja:body` and holds nearly every table and figure
    (715/764 tables and 705/748 figures across 240 inbox files, 2026-09-02);
    the body only carries a `<ce:float-anchor refid>` where each belongs. A
    body-only walk therefore sees almost no tables at all.
    """

    def __init__(self, root):
        self.by_id = {}
        self.in_order = []
        self.emitted = set()
        for container in root.iter():
            if localname(container.tag) != "floats":
                continue
            for el in container:
                self.in_order.append(el)
                if el.get("id"):
                    self.by_id[el.get("id")] = el

    def take(self, refid: str):
        """The float for an anchor, or None if unknown or already emitted."""
        el = self.by_id.get(refid or "")
        if el is None or el in self.emitted:
            return None
        self.emitted.add(el)
        return el

    def leftovers(self):
        return [el for el in self.in_order if el not in self.emitted]


def _emit_leftover_floats(floats, parts: List[str], result: Conversion, depth: int) -> None:
    """Floats nothing anchored still belong to the paper; better late than lost."""
    for el in floats.leftovers():
        floats.emitted.add(el)
        _emit_one(el, None, parts, result, depth, floats)


def _walk_jats(element, parts: List[str], result: Conversion, depth: int,
               floats=None) -> None:
    """Emit prose and tables in the order they appear.

    Order matters: a table extracted out of position loses the sentence that
    introduces it, which is often where the units and the sample live.
    """
    for child in element:
        _emit_one(child, element, parts, result, depth, floats)


def _emit_one(child, parent, parts: List[str], result: Conversion, depth: int,
              floats) -> None:
    """Dispatch one element. JATS branches first, then Elsevier's `ce:` names."""
    tag = localname(child.tag)
    parent_tag = localname(parent.tag) if parent is not None else ""

    if tag in ("ref-list", "bibliography"):
        return  # Cited papers are not evidence about the screened cohort.

    elif tag in ("ack", "fn-group", "fn", "app-group", "app", "notes",
                 "author-notes", "funding-group", "support-group", "supplementary-material"):
        defaults = {"ack": "Acknowledgments", "fn-group": "Notes", "fn": "Note",
                    "app-group": "Appendices", "app": "Appendix", "notes": "Notes",
                    "author-notes": "Author notes", "funding-group": "Funding",
                    "support-group": "Support", "supplementary-material": "Supplementary material"}
        heading = _first_text(child, "title", direct_only=True) or defaults[tag]
        label = _first_text(child, "label", direct_only=True)
        parts.append(f"\n{'#' * min(depth, 6)} {heading}" + (f" ({label})" if label else "") + "\n")
        # Preserve mixed inline notes while dispatching structural children normally.
        blocks = {"p", "sec", "table-wrap", "fig", "list", "fn", "app", "fn-group",
                  "app-group", "supplementary-material", "ref-list", "notes",
                  "caption", "media"}
        text = _inline_text(child, skip=tuple(blocks | {"title", "label"}))
        if text:
            parts.append(text + "\n")
        href = child.get("{http://www.w3.org/1999/xlink}href")
        if href:
            parts.append(f"Referenced file (not included): `{href}`\n")
        for element in child:
            if localname(element.tag) in blocks:
                _emit_one(element, child, parts, result, depth + 1, floats)

    elif tag == "media":
        text = _inline_text(child)
        if text:
            parts.append(text + "\n")
        href = child.get("{http://www.w3.org/1999/xlink}href")
        if href:
            parts.append(f"Referenced file (not included): `{href}`\n")

    elif tag == "sec":
        heading = _first_text(child, "title", direct_only=True)
        if heading:
            parts.append("\n{} {}\n".format("#" * min(depth, 6), heading))
        _walk_jats(child, parts, result, depth + 1, floats)

    elif tag == "title":
        return  # already consumed by the enclosing <sec>

    elif tag == "p":
        text = _inline_text(child)
        if text:
            parts.append(text + "\n")

    elif tag == "table-wrap":
        parts.append(_table_block(child, result))

    elif tag == "fig":
        label = _first_text(child, "label") or "Figure"
        caption = _first_text(child, "caption")
        graphic = _graphic_href(child)
        result.n_figures += 1
        # Named, never inlined -- JATS references images and never holds them.
        parts.append(
            f"\n> **[{label} — image not included]** {caption}"
            f"{f' (source file: `{graphic}`)' if graphic else ''}\n"
        )

    elif tag in ("list",):
        for item in child:
            if localname(item.tag) == "list-item":
                # Elsevier numbers items through a <label>; "(1)" or "a" is worth
                # keeping (the prose says "see point (1)"), a bullet glyph is not.
                label = _first_text(item, "label", direct_only=True)
                if not any(ch.isalnum() for ch in label):
                    label = ""
                text = _inline_text(item, skip=("label",))
                line = " ".join(filter(None, [label, text]))
                if line:
                    parts.append(f"- {line}")
        parts.append("")

    elif tag in ("disp-quote", "boxed-text"):
        text = _inline_text(child)
        if text:
            parts.append(f"> {text}\n")

    elif tag in ("disp-formula", "inline-formula", "formula"):
        formula = _formula_text(child)
        if formula:
            parts.append(f"\n$$ {formula} $$\n")

    # -- Elsevier (`ce:`) vocabulary ----------------------------------------

    elif tag == "section":
        heading = _first_text(child, "section-title", direct_only=True)
        if heading:
            parts.append("\n{} {}\n".format("#" * min(depth, 6), heading))
        _walk_jats(child, parts, result, depth + 1, floats)

    elif tag in ("section-title", "label") and parent_tag == "section":
        # The title is the heading above; the label is the section number,
        # which Markdown headings do not carry ("see Section 2.1" no longer
        # maps to a number -- a one-line change if that ever matters).
        return

    elif tag == "section-title":
        # Under <acknowledgment>, <appendices>, <textbox-head>: still a heading.
        heading = _clean(" ".join(child.itertext()))
        if heading:
            parts.append("\n{} {}\n".format("#" * min(depth, 6), heading))

    elif tag in ("para", "simple-para", "note-para"):
        _emit_paragraph(child, parts, result, depth, floats)

    elif tag == "table":
        parts.append(_table_block(child, result))

    elif tag in ("figure", "e-component"):
        default = "Figure" if tag == "figure" else "Supplementary material"
        label = _first_text(child, "label", direct_only=True) or default
        caption = _first_text(child, "caption")
        graphic = _graphic_href(child)
        result.n_figures += 1
        parts.append(
            f"\n> **[{label} — image not included]** {caption}"
            f"{f' (source file: `{graphic}`)' if graphic else ''}\n"
        )

    elif tag == "float-anchor":
        if floats is not None:
            el = floats.take(child.get("refid"))
            if el is not None:
                _emit_one(el, None, parts, result, depth, floats)

    elif tag == "floats":
        return  # emitted at their anchors, leftovers at the end

    elif tag == "footnote":
        label = _first_text(child, "label", direct_only=True)
        marker = f"footnote {label}" if label else "footnote"
        # A note-para can itself embed a display table (1 of 428 corpus files,
        # 2026-09-02); it is emitted after the note, never flattened into it.
        texts, blocks = [], []
        for part in child:
            if localname(part.tag) == "label":
                continue
            texts.append(_inline_text(part, skip=_PARA_BLOCKS))
            blocks.extend(b for b in part if localname(b.tag) in _PARA_BLOCKS)
        text = " ".join(t for t in texts if t)
        if text:
            parts.append(f"> **[{marker}]** {text}\n")
        for block in blocks:
            _emit_one(block, child, parts, result, depth, floats)

    else:
        # Unknown wrapper: descend rather than drop. Losing a whole section
        # because of an unrecognised container is the failure mode to avoid.
        if len(child):
            _walk_jats(child, parts, result, depth, floats)
        else:
            text = _inline_text(child)
            if text:
                parts.append(text + "\n")


def _emit_paragraph(para, parts: List[str], result: Conversion, depth: int,
                    floats) -> None:
    """An Elsevier paragraph: its running text, then the blocks it embeds.

    Recursing into a <ce:para> as a wrapper (what the else-branch does) drops
    `para.text` and every child's tail, leaving only the citation labels -- the
    husk that made Elsevier renditions unusable (median prose line 11
    characters, measured 2026-09-02).
    """
    text = _inline_text(para, skip=_PARA_BLOCKS)
    if text:
        parts.append(text + "\n")
    for block in para:
        if localname(block.tag) in _PARA_BLOCKS:
            _emit_one(block, para, parts, result, depth, floats)


def _table_block(wrap, result: Conversion) -> str:
    """One table, as canonical HTML, with caption and footnotes attached.

    Kept together deliberately: JATS <table-wrap-foot> carries the semantics of
    the values above it ("mean +/- SD unless stated", per-column denominators).
    Separate them and every number in the table becomes misinterpretable.

    `wrap` is a JATS <table-wrap> or a bare Elsevier <ce:table>, whose label,
    caption and footnotes are its own children.
    """
    failures: List[str] = []
    table_el = _find_one(wrap, "table")
    html = jats_table_to_canonical(table_el, failures)
    result.failures.extend(failures)

    # Direct child only: a footnote's own <label> ("a", "*") must never title
    # the table.
    label = _first_text(wrap, "label", direct_only=True) or "Table"
    caption = _first_text(wrap, "caption")
    lines = ["", f"**{label}.** {caption}".rstrip(".").rstrip() + ""]

    if html:
        result.n_tables += 1
        result.table_tokens += token_count(html)
        lines.append("")
        lines.append(html)
    else:
        result.n_tables_empty += 1
        graphic = _graphic_href(wrap)
        # A <table-wrap> holding only a <graphic> is a table shipped as an image:
        # zero cells, nothing extractable. Say so rather than emit an empty block.
        lines.append("")
        lines.append(
            f"> **[table not machine-readable — published as an image]**"
            f"{f' (source file: `{graphic}`)' if graphic else ''}"
        )

    footnotes = _jats_footnotes(wrap)
    if footnotes:
        lines.append("")
        lines.append("*Table footnotes:*")
        lines.extend(f"- {note}" for note in footnotes)
    lines.append("")
    return "\n".join(lines)


def _jats_footnotes(wrap) -> List[str]:
    notes, seen = [], set()
    for el in wrap.iter():
        if localname(el.tag) in ("table-wrap-foot", "fn", "table-footnote", "legend"):
            text = _clean(" ".join(el.itertext()))
            if text and text not in seen:
                seen.add(text)
                notes.append(text)
    return notes


def _graphic_href(element) -> str:
    for el in element.iter():
        name = localname(el.tag)
        if name in ("graphic", "inline-graphic"):
            for key, value in el.attrib.items():
                if key.endswith("href"):
                    return value
        # Elsevier: <ce:link locator="gr1" xlink:href="pii:S.../gr1">. The
        # locator is the name a reader can match against the PDF's figures.
        elif name == "link" and el.get("locator"):
            return el.get("locator")
    return ""


def _formula_text(element) -> str:
    for el in element.iter():
        for key, value in el.attrib.items():
            # LaTeX preserved in MathML alttext -- arXiv's LaTeXML HTML always
            # carries it, and it is strictly better than the rendered glyphs.
            if key.endswith("alttext") and value.strip():
                return value.strip()
    return _clean(" ".join(element.itertext()))


# -- publisher HTML ---------------------------------------------------------


def html_to_markdown(content: bytes, name: str = "") -> Conversion:
    """Publisher HTML -> Markdown, after stripping page chrome."""
    result = Conversion(markdown="")
    try:
        import lxml.html
    except ImportError:
        result.failures.append("lxml not installed; install fetchpdf[html]")
        return result

    text = content.decode("utf-8", errors="replace")
    try:
        tree = lxml.html.fromstring(text)
    except Exception as e:
        result.failures.append(f"HTML unparseable: {str(e)[:100]}")
        return result

    before = token_count(text)
    _strip_chrome(tree)
    after = token_count(" ".join(tree.itertext()))
    if before and after / before > 0.95:
        # A stripper that removed nothing means nav menus and cookie banners are
        # about to be fed to the model as if they were the paper.
        result.failures.append(
            f"boilerplate stripper removed almost nothing (ratio {after / before:.2f})"
        )

    parts: List[str] = []
    root = _main_content(tree)
    title = tree.findtext(".//title") or ""
    if title.strip():
        parts.append(f"# {_clean(title)}\n")

    _walk_html(root, parts, result)
    return _finish(parts, result)


def _strip_chrome(tree) -> None:
    from .normalize import _BOILERPLATE_HINTS, _BOILERPLATE_TAGS

    for el in tree.xpath("//*"):
        name = localname(el.tag)
        parent = el.getparent()
        if parent is None:
            continue
        if name in _BOILERPLATE_TAGS:
            parent.remove(el)
            continue
        marker = " ".join(
            filter(None, [el.get("class") or "", el.get("id") or "", el.get("role") or ""])
        ).lower()
        if marker and any(hint in marker for hint in _BOILERPLATE_HINTS):
            parent.remove(el)


def _main_content(tree):
    """The article subtree if the page marks one, else the whole body."""
    for xpath in ('//article', '//*[@role="main"]', '//main', '//body'):
        found = tree.xpath(xpath)
        if found:
            return found[0]
    return tree


def _walk_html(element, parts: List[str], result: Conversion) -> None:
    for child in element:
        tag = localname(child.tag)

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _clean(" ".join(child.itertext()))
            if text:
                parts.append("\n{} {}\n".format("#" * int(tag[1]), text))

        elif tag == "p":
            text = _clean(" ".join(child.itertext()))
            if text:
                parts.append(text + "\n")

        elif tag == "table":
            parts.append(_html_table_block(child, result))

        elif tag in ("ul", "ol"):
            for item in child:
                if localname(item.tag) == "li":
                    text = _clean(" ".join(item.itertext()))
                    if text:
                        parts.append(f"- {text}")
            parts.append("")

        elif tag in ("figure", "img"):
            alt = child.get("alt") or ""
            src = child.get("src") or _first_img_src(child)
            result.n_figures += 1
            parts.append(
                f"\n> **[figure — image not included]** {_clean(alt)}"
                f"{f' (source: `{src}`)' if src else ''}\n"
            )

        elif tag in ("script", "style"):
            continue

        else:
            if len(child):
                _walk_html(child, parts, result)
            else:
                text = _clean(child.text or "")
                if text:
                    parts.append(text + "\n")


def _html_table_block(table, result: Conversion) -> str:
    failures: List[str] = []
    html = html_table_to_canonical(table, failures)
    result.failures.extend(failures)
    if not html:
        result.n_tables_empty += 1
        return "\n> **[table had no readable cells]**\n"

    result.n_tables += 1
    result.table_tokens += token_count(html)

    caption = ""
    for el in table.iter():
        if localname(el.tag) == "caption":
            caption = _clean(" ".join(el.itertext()))
            break

    lines = [""]
    lines.append(f"**Table.** {caption}" if caption else "**Table.**")
    lines.append("")
    lines.append(html)

    footer = [
        _clean(" ".join(el.itertext()))
        for el in table.iter()
        if localname(el.tag) == "tfoot"
    ]
    if any(footer):
        lines.append("")
        lines.append("*Table footnotes:*")
        lines.extend(f"- {note}" for note in footer if note)
    lines.append("")
    return "\n".join(lines)


def _first_img_src(element) -> str:
    for el in element.iter():
        if localname(el.tag) == "img" and el.get("src"):
            return el.get("src")
    return ""


# -- shared -----------------------------------------------------------------


def _finish(parts: List[str], result: Conversion) -> Conversion:
    body = "\n".join(p for p in parts if p is not None)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    result.markdown = body
    result.prose_tokens = max(token_count(body) - result.table_tokens, 0)
    return result


def _find_one(root, name: str):
    for el in root.iter():
        if localname(el.tag) == name:
            return el
    return None


def _first_text(element, name: str, direct_only: bool = False) -> str:
    source = list(element) if direct_only else element.iter()
    for el in source:
        if localname(el.tag) == name:
            return _clean(" ".join(el.itertext()))
    return ""


def _inline_text(element, skip=()) -> str:
    """Paragraph text with superscript markers kept as literal characters.

    A superscript 'a' is the link between a value and the footnote defining it,
    so it is bracketed to survive as visible text. <xref> is NOT bracketed: JATS
    prose almost always supplies its own delimiters around a citation, and adding
    ours turned every reference into "[[1]]". Elsevier's <cross-ref> is left
    alone for the same reason: its text is the citation as typeset ("Gombrich
    (1963)", "Bullough, 1907" inside the prose's own parentheses, sometimes
    "[1]"), so the verbatim text is already right (measured 2026-09-02).

    `skip` names direct children whose subtree is left out -- the blocks an
    Elsevier paragraph embeds, emitted separately -- while their tail, the prose
    that continues after the block, is kept.
    """
    parts = []
    if element.text:
        parts.append(element.text)
    for child in element:
        _inline_node(child, parts, skip)
    return _clean("".join(parts))


def _inline_node(node, parts: List[str], skip) -> None:
    tag = localname(node.tag)
    if tag in skip:
        pass
    elif tag == "sup":
        if node.text:
            parts.append(f"[{node.text.strip()}]")
        for child in node:
            _inline_node(child, parts, ())
    elif tag == "xref":
        if node.text:
            parts.append(node.text.strip())
        for child in node:
            _inline_node(child, parts, ())
    else:
        if node.text:
            parts.append(node.text)
        for child in node:
            _inline_node(child, parts, ())
    if node.tail:
        parts.append(node.tail)


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def front_matter(path: str, conversion: Conversion, extra: Optional[dict] = None) -> str:
    """YAML header recording what this file is and how far to trust it."""
    import time

    fields = {
        "source_artifact": os.path.basename(path),
        "converted_at": iso(time.time()),
        "table_format": "canonical-html",
        "tables": conversion.n_tables,
        "tables_not_machine_readable": conversion.n_tables_empty,
        "figures_referenced_not_included": conversion.n_figures,
        "prose_tokens": conversion.prose_tokens,
        "table_tokens": conversion.table_tokens,
    }
    fields.update(extra or {})
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    if conversion.failures:
        lines.append("conversion_warnings:")
        lines.extend(f"  - {f}" for f in dict.fromkeys(conversion.failures))
    lines.append("---")
    lines.append("")
    lines.append(f"<!-- {TABLE_FORMAT_NOTE} -->")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


# -- writing them out -------------------------------------------------------

#: Artifact suffixes this module can convert, mapped to the markdown suffix each
#: produces. The source is in the FILENAME, not just the front matter, because a
#: consumer that globs a directory reads nothing else -- and JATS from the
#: publisher and scraped publisher HTML do not deserve equal trust. One is the
#: markup the publisher deposited; the other is what their web page happened to
#: render, after a boilerplate stripper had a go at it.
CONVERTIBLE_SUFFIXES = {
    ".xml": "_from_xml.md",
    ".fulltext.html": "_from_html.md",
}

#: Best source first, for callers choosing between two artifacts of one record.
CONVERTIBLE = tuple(CONVERTIBLE_SUFFIXES)

MARKDOWN_SUFFIX = "_from_xml.md"   # the default when the source is unknown


def markdown_path_for(artifact_path: str) -> str:
    """`{stem}_from_xml.md` / `{stem}_from_html.md`, beside its source artifact."""
    for suffix, markdown_suffix in CONVERTIBLE_SUFFIXES.items():
        if artifact_path.endswith(suffix):
            return artifact_path[: -len(suffix)] + markdown_suffix
    return os.path.splitext(artifact_path)[0] + MARKDOWN_SUFFIX


def record_stem_for(artifact_path: str) -> str:
    """The record's stem, with any artifact suffix removed."""
    for suffix in CONVERTIBLE_SUFFIXES:
        if artifact_path.endswith(suffix):
            return artifact_path[: -len(suffix)]
    return os.path.splitext(artifact_path)[0]


def write_markdown(artifact_path: str, overwrite: bool = False,
                   verbose: bool = False) -> Optional[str]:
    """Convert one artifact and write `{stem}_from_xml.md` / `{stem}_from_html.md`.

    Returns the path, or None.

    Never raises into a batch: a document that will not convert is one bad
    record, not a dead run.
    """
    out_path = markdown_path_for(artifact_path)
    if os.path.exists(out_path) and not overwrite:
        return out_path
    try:
        conversion = convert_file(artifact_path)
    except Exception as e:
        if verbose:
            print(f"  markdown: {os.path.basename(artifact_path)} raised {type(e).__name__}: {str(e)[:90]}")
        return None
    if conversion is None or not conversion.ok:
        if verbose:
            reason = "; ".join(conversion.failures) if conversion else "unsupported format"
            print(f"  markdown: {os.path.basename(artifact_path)} -> nothing ({reason})")
        return None

    text = front_matter(artifact_path, conversion) + conversion.markdown
    tmp = out_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, out_path)
    except OSError as e:
        if verbose:
            print(f"  markdown: could not write {out_path}: {e}")
        return None
    if verbose:
        print(
            f"  markdown: {os.path.basename(out_path)} "
            f"({conversion.n_tables} tables, {conversion.n_figures} figures referenced)"
        )
    return out_path


def convert_directory(directory: str, overwrite: bool = False, verbose: bool = False) -> dict:
    """Convert every convertible artifact in a directory. Returns a summary."""
    summary = {"converted": 0, "skipped": 0, "failed": 0,
               "tables": 0, "image_tables": 0, "figures": 0, "no_structured": 0}

    # Group by record first, then pick the best source for each. Iterating
    # os.listdir() order instead would let ".fulltext.html" win on a record that
    # has both, purely because it sorts before ".xml" -- silently preferring
    # scraped publisher HTML over the publisher's own JATS.
    best_for_record = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not name.endswith(CONVERTIBLE) or not os.path.isfile(path):
            continue
        record = record_stem_for(path)
        rank = next(i for i, suffix in enumerate(CONVERTIBLE) if name.endswith(suffix))
        existing = best_for_record.get(record)
        if existing is None or rank < existing[0]:
            if existing is not None:
                summary["skipped"] += 1
            best_for_record[record] = (rank, path)
        else:
            summary["skipped"] += 1

    for record, (_rank, path) in sorted(best_for_record.items()):
        out_path = markdown_path_for(path)
        existed = os.path.exists(out_path)
        conversion = None
        try:
            conversion = convert_file(path)
        except Exception:
            pass
        written = write_markdown(path, overwrite=overwrite, verbose=verbose)
        if written is None:
            summary["failed"] += 1
        elif existed and not overwrite:
            summary["skipped"] += 1
        else:
            summary["converted"] += 1
        if conversion:
            summary["tables"] += conversion.n_tables
            summary["image_tables"] += conversion.n_tables_empty
            summary["figures"] += conversion.n_figures

    # A PDF with no structured sibling has no markdown route here at all.
    pdfs = {n[:-4] for n in os.listdir(directory) if n.endswith(".pdf")}
    summary["no_structured"] = len(pdfs - {os.path.basename(r) for r in best_for_record})
    return summary


def main(argv=None):
    """Convert a directory of retrieved artifacts to Markdown."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="fetchpdf-md",
        description=(
            "Convert retrieved .xml / .fulltext.html artifacts into Markdown. "
            "Prose becomes Markdown; tables stay canonical HTML so colspan and "
            "rowspan survive, because Markdown cannot express them."
        ),
    )
    parser.add_argument("directory", help="Directory of artifacts (the -o dir from a run)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Rewrite .md files that already exist")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.directory):
        print(f"❌ Not a directory: {args.directory}")
        return 1

    summary = convert_directory(args.directory, overwrite=args.overwrite, verbose=args.verbose)
    print(f"\n📝 Markdown conversion — {args.directory}")
    print(f"   converted        {summary['converted']}")
    print(f"   already present  {summary['skipped']}")
    print(f"   failed           {summary['failed']}")
    print(f"   tables           {summary['tables']}")
    if summary["image_tables"]:
        print(f"   tables as images {summary['image_tables']}  (not machine-readable)")
    print(f"   figures noted    {summary['figures']}  (referenced, never embedded)")
    if summary["no_structured"]:
        print(
            f"   PDF-only records {summary['no_structured']}  "
            f"(no XML/HTML to convert — these still need the PDF pipeline)"
        )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
