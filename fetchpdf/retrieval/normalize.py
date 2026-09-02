"""Every tier converges on one canonical table format.

The model must never see a raw artifact and must not be able to infer which tier
produced it -- otherwise tier becomes a hidden variable in the extraction, and
comparing runs across sources stops meaning anything.

Canonical format is minimal HTML: <table>/<tr>/<th>/<td>, colspan and rowspan
preserved, nothing else. Markdown is prohibited as a table format. Markdown has
no span mechanism, so a header cell spanning two treatment arms collapses to one
column and every value to its right shifts one position -- silently, and in a
way that still parses as a valid table.

No LLM anywhere in this module. Deterministic transforms only: a model here
would reintroduce exactly the generation channel this design exists to close.
"""

import re
from typing import List, Optional, Tuple
from ._util import as_int, localname as _localname

#: The only attributes that survive into canonical form.
_KEPT_ATTRS = ("colspan", "rowspan")

#: Elements stripped from publisher HTML before anything else is looked at.
_BOILERPLATE_TAGS = (
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "iframe", "svg", "video", "audio",
)

#: Class/id substrings that mark chrome rather than content.
_BOILERPLATE_HINTS = (
    "cookie", "consent", "gdpr", "banner", "advert", "advertisement", "promo",
    "sidebar", "related-article", "related_content", "recommend", "socialmedia",
    "social-share", "share-tools", "newsletter", "breadcrumb", "skip-link",
    "reference-popover", "tooltip", "site-header", "site-footer", "masthead",
    "navigation", "menu", "subscribe", "paywall",
)

_WS_RE = re.compile(r"\s+")


def token_count(text: str) -> int:
    """Whitespace tokens. A proxy, but a stable one, and it needs no tokenizer."""
    return len(_WS_RE.sub(" ", text or "").strip().split()) if text else 0


# -- JATS -------------------------------------------------------------------


def jats_table_to_canonical(table_el, failures: Optional[List[str]] = None) -> str:
    """JATS <table> (XHTML table model) -> canonical minimal HTML.

    JATS also permits the CALS model (<entry> with namest/nameend rather than
    <td colspan>), and Elsevier's `ce:table` uses nothing else. Those go
    through `_cals_to_canonical`, which resolves the named spans; a CALS span
    is never flattened, because flattening a span is precisely the corruption
    this pipeline exists to prevent.
    """
    failures = failures if failures is not None else []
    if table_el is None:
        return ""

    if any(_localname(el.tag) == "entry" for el in table_el.iter()):
        return _cals_to_canonical(table_el, failures)

    rows = []
    for tr in table_el.iter():
        if _localname(tr.tag) != "tr":
            continue
        cells = []
        for cell in list(tr):
            name = _localname(cell.tag)
            if name not in ("td", "th"):
                continue
            attrs = _span_attrs(cell)
            cells.append("<{0}{1}>{2}</{0}>".format(name, attrs, _cell_text(cell)))
        if cells:
            rows.append("<tr>{}</tr>".format("".join(cells)))
    if not rows:
        return ""
    return "<table>{}</table>".format("".join(rows))


def _cals_to_canonical(table_el, failures: List[str]) -> str:
    """CALS table model (Elsevier `ce:table`) -> canonical minimal HTML.

    CALS names columns rather than counting them: <colspec colname="col2"/>
    declares a column, an <entry> sits in it by `colname`, spans it by
    `namest`/`nameend`, and reaches down with `morerows`. Most entries carry no
    column at all and are positional (2,929 of them across 240 Elsevier inbox
    files, 2026-09-02), so a cursor walks each row and steps over columns a
    `morerows` cell above still occupies -- otherwise a positional cell under a
    rowspan lands one column to the left, and every value in the row with it.
    Columns the source omits become empty cells for the same reason: the
    canonical form has no other way to keep the ones after them aligned.

    Failures are notes, not refusals: the table still renders and the note
    says what could not be resolved.
    """

    def note(message: str) -> None:
        if message not in failures:
            failures.append(message)

    rows_html = []
    groups = [g for g in table_el.iter() if _localname(g.tag) == "tgroup"] or [table_el]
    for group in groups:
        # Direct children in source order: their position is the column index.
        colnames = [c.get("colname") for c in group if _localname(c.tag) == "colspec"]
        col_index = {name: i for i, name in enumerate(colnames) if name}
        if any(e.get("spanname") for e in group.iter() if _localname(e.tag) == "entry"):
            note("CALS spanspec/spanname not supported; those spans dropped")

        covered_until = {}   # column -> last row index a morerows cell above covers
        row_no = 0

        def next_free(col: int) -> int:
            while covered_until.get(col, -1) >= row_no:
                col += 1
            return col

        order = ("thead", "tbody", "tfoot")
        sections = sorted(
            (s for s in group if _localname(s.tag) in order),
            key=lambda s: order.index(_localname(s.tag)),
        )
        for section in sections:
            cell_tag = "th" if _localname(section.tag) == "thead" else "td"
            for row in (r for r in section if _localname(r.tag) == "row"):
                cells, cursor = [], 0
                for entry in (e for e in row if _localname(e.tag) == "entry"):
                    start_name = entry.get("namest") or entry.get("colname")
                    start = col_index.get(start_name) if start_name else None
                    if start is None:
                        if start_name and colnames:
                            note(f"CALS column {start_name!r} not in colspec; cell placed positionally")
                        start = next_free(cursor)

                    colspan = 1
                    if entry.get("nameend"):
                        end = col_index.get(entry.get("nameend"))
                        if not colnames:
                            note("CALS tgroup has no colspec; namest/nameend spans cannot be resolved")
                        elif end is None or end < start:
                            note("CALS nameend not in colspec; span dropped")
                        else:
                            colspan = end - start + 1

                    for col in range(next_free(cursor), start):
                        if covered_until.get(col, -1) < row_no:
                            cells.append("<{0}></{0}>".format(cell_tag))

                    more = as_int(entry.get("morerows"), 0)
                    rowspan = more + 1
                    attrs = (
                        (' colspan="{}"'.format(colspan) if colspan > 1 else "")
                        + (' rowspan="{}"'.format(rowspan) if rowspan > 1 else "")
                    )
                    cells.append("<{0}{1}>{2}</{0}>".format(cell_tag, attrs, _cell_text(entry)))
                    for col in range(start, start + colspan):
                        covered_until[col] = row_no + more
                    cursor = start + colspan
                if cells:
                    rows_html.append("<tr>{}</tr>".format("".join(cells)))
                row_no += 1

    if not rows_html:
        return ""
    return "<table>{}</table>".format("".join(rows_html))


def _span_attrs(cell) -> str:
    """colspan/rowspan, but only when they actually span something.

    A span of 1 is a no-op that some publishers (MDPI among them) write on every
    single cell. Emitting them costs tokens in the model's input and says nothing
    -- and a table of all-1 spans reads as though spans were considered and found
    absent, which is not the same as the publisher being verbose.
    """
    parts = []
    for attribute in _KEPT_ATTRS:
        value = (cell.get(attribute) or "").strip()
        if value and value != "1":
            parts.append(' {}="{}"'.format(attribute, _esc(value)))
    return "".join(parts)


def _cell_text(cell) -> str:
    """Cell content as escaped text, with superscript markers kept inline.

    Superscript footnote markers are the link between a value and the note that
    says what it means ("mean +/- SD unless stated"). Dropping them makes the
    note unresolvable, so they are preserved as literal characters rather than
    as markup that the canonical format does not allow.
    """
    parts = []
    for node in cell.iter():
        name = _localname(node.tag)
        # <sup> is bracketed: a superscript marker must survive as visible text or
        # its footnote becomes unresolvable. <xref> is NOT: the source almost
        # always supplies its own delimiters, and adding ours produced "[[351]]".
        if node is not cell and name == "sup" and node.text:
            parts.append("[{}]".format(node.text.strip()))
            if node.tail:
                parts.append(node.tail)
            continue
        if node is not cell and name == "xref":
            if node.text:
                parts.append(node.text.strip())
            if node.tail:
                parts.append(node.tail)
            continue
        if node is cell and node.text:
            parts.append(node.text)
        elif node is not cell:
            if node.text:
                parts.append(node.text)
            if node.tail:
                parts.append(node.tail)
    return _esc(_WS_RE.sub(" ", "".join(parts)).strip())


# -- publisher HTML ---------------------------------------------------------


def html_table_to_canonical(table_el, failures: Optional[List[str]] = None) -> str:
    """Strip a publisher <table> subtree down to the canonical form."""
    failures = failures if failures is not None else []
    if table_el is None:
        return ""
    rows = []
    for tr in table_el.iter():
        if _localname(tr.tag) != "tr":
            continue
        cells = []
        for cell in tr.iter():
            name = _localname(cell.tag)
            if name not in ("td", "th") or cell is tr:
                continue
            attrs = _span_attrs(cell)
            text = _WS_RE.sub(" ", "".join(cell.itertext())).strip()
            cells.append("<{0}{1}>{2}</{0}>".format(name, attrs, _esc(text)))
        if cells:
            rows.append("<tr>{}</tr>".format("".join(cells)))
    if not rows:
        failures.append("HTML table had no rows after stripping")
        return ""
    return "<table>{}</table>".format("".join(rows))


def strip_boilerplate(html_text: str) -> Tuple[str, float, List[str]]:
    """Remove page chrome; return (text, before/after token ratio, failures).

    The ratio is the point of the return value. A ratio near 1.0 means the
    stripper matched nothing and the model is about to be fed nav menus, cookie
    banners and related-article rails as though they were the paper.
    """
    failures = []
    before = token_count(html_text)
    try:
        import lxml.html
    except ImportError:
        failures.append("lxml not installed; boilerplate not stripped")
        return html_text, 1.0, failures

    try:
        tree = lxml.html.fromstring(html_text)
    except Exception as e:
        failures.append(f"HTML unparseable: {str(e)[:80]}")
        return html_text, 1.0, failures

    for el in tree.xpath("//*"):
        name = _localname(el.tag)
        if name in _BOILERPLATE_TAGS:
            _drop(el)
            continue
        marker = " ".join(
            filter(None, [el.get("class") or "", el.get("id") or "", el.get("role") or ""])
        ).lower()
        if marker and any(hint in marker for hint in _BOILERPLATE_HINTS):
            _drop(el)

    text = _WS_RE.sub(" ", " ".join(tree.itertext())).strip()
    after = token_count(text)
    ratio = (after / before) if before else 1.0
    if before and ratio > 0.95:
        failures.append(
            f"boilerplate stripper removed almost nothing (ratio {ratio:.2f})"
        )
    return text, ratio, failures


def _drop(el) -> None:
    parent = el.getparent()
    if parent is not None:
        # Keep the tail text: dropping it would glue neighbouring words together.
        tail = el.tail
        parent.remove(el)
        if tail:
            previous = parent
            children = list(parent)
            if children:
                previous = children[-1]
                previous.tail = (previous.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail


# -- LaTeX ------------------------------------------------------------------

_TABULAR_RE = re.compile(
    r"\\begin\{(tabular|tabularx|longtable)\}(.*?)\\end\{\1\}", re.DOTALL
)
_MULTICOLUMN_RE = re.compile(r"\\multicolumn\{(\d+)\}\{[^}]*\}\{(.*?)\}", re.DOTALL)
_MULTIROW_RE = re.compile(r"\\multirow\{(\d+)\}\{[^}]*\}\{(.*?)\}", re.DOTALL)
_TEX_COMMAND_RE = re.compile(r"\\([a-zA-Z@]+)\s*(\{[^{}]*\})?")
_TEX_STRIPPABLE = {
    "hline", "toprule", "midrule", "bottomrule", "cmidrule", "centering",
    "small", "footnotesize", "scriptsize", "tiny", "bfseries", "itshape",
    "textbf", "textit", "emph", "text", "mathrm", "num", "SI", "si", "phantom",
    "rule", "addlinespace", "noalign", "label", "caption",
}


def latex_tables_to_canonical(tex: str) -> Tuple[List[str], List[str]]:
    """Extract LaTeX tabulars as canonical HTML.

    Deliberately partial. Real papers reach for \\multirow, booktabs, siunitx
    and preamble macros defined three files away; expanding all of that is a TeX
    implementation, not a normalizer. Unexpandable macros are reported rather
    than dropped, so a table that silently lost a column is visible as a
    normalization failure instead of arriving looking clean.
    """
    tables, failures = [], []
    for match in _TABULAR_RE.finditer(tex or ""):
        body = match.group(2)
        rows = []
        for raw_row in re.split(r"\\\\", body):
            raw_row = raw_row.strip()
            if not raw_row:
                continue
            cells = []
            for raw_cell in raw_row.split("&"):
                span_attrs = ""
                mc = _MULTICOLUMN_RE.search(raw_cell)
                if mc:
                    span_attrs += ' colspan="{}"'.format(mc.group(1))
                    raw_cell = _MULTICOLUMN_RE.sub(r"\2", raw_cell)
                mr = _MULTIROW_RE.search(raw_cell)
                if mr:
                    span_attrs += ' rowspan="{}"'.format(mr.group(1))
                    raw_cell = _MULTIROW_RE.sub(r"\2", raw_cell)
                text, unexpanded = _strip_tex(raw_cell)
                failures.extend(unexpanded)
                if text or span_attrs:
                    cells.append("<td{}>{}</td>".format(span_attrs, _esc(text)))
            if cells:
                rows.append("<tr>{}</tr>".format("".join(cells)))
        if rows:
            tables.append("<table>{}</table>".format("".join(rows)))
    return tables, sorted(set(failures))


def _strip_tex(fragment: str) -> Tuple[str, List[str]]:
    unexpanded = []

    def replace(match):
        name = match.group(1)
        argument = match.group(2) or ""
        if name in _TEX_STRIPPABLE:
            return argument[1:-1] if argument else ""
        unexpanded.append(f"unexpanded macro \\{name}")
        return argument[1:-1] if argument else ""

    text = _TEX_COMMAND_RE.sub(replace, fragment or "")
    text = text.replace("{", "").replace("}", "").replace("~", " ").replace("$", "")
    return _WS_RE.sub(" ", text).strip(), unexpanded


def _esc(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
