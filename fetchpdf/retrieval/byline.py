"""Authors, affiliations and the journal citation, for under the rendered title.

Like references.py, this is for the page a person reads (to_html.py) and is
kept out of the Markdown walker, whose readers do not need a byline.

JATS carries them in `<article-meta>`: `<contrib-group>` (each author's
`<xref ref-type="aff">` points at an `<aff>` by id), and the volume, issue and
pages as its direct children, with the journal title in `<journal-meta>`.
Elsevier's full-text XML carries `<ce:author-group>` (`<ce:cross-ref refid>`
to `<ce:affiliation>`) in the article head and the citation in `prism:`
elements of its `coredata`; when the head is absent, `dc:creator` still names
the authors.
"""

import html as html_lib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

from ._util import localname
from .references import _compose
from .to_markdown import _clean, _first_text


@dataclass
class Byline:
    authors: List[tuple] = field(default_factory=list)       # (name, [markers])
    affiliations: List[tuple] = field(default_factory=list)  # (label, text)
    journal: str = ""                                         # HTML

    def html(self) -> str:
        if not (self.authors or self.affiliations or self.journal):
            return ""
        out = ['<div class="byline">']
        if self.authors:
            names = [_esc(name) + (f"<sup>{_esc(','.join(marks))}</sup>" if marks else "")
                     for name, marks in self.authors]
            out.append(f'<p class="authors">{", ".join(names)}</p>')
        if self.affiliations:
            out.append('<ul class="affiliations">' + "".join(
                "<li>" + (f"<sup>{_esc(label)}</sup> " if label else "") + f"{_esc(text)}</li>"
                for label, text in self.affiliations) + "</ul>")
        if self.journal:
            out.append(f'<p class="journal">{self.journal}</p>')
        out.append("</div>")
        return "\n".join(out) + "\n"


def byline_from_xml(content: bytes) -> Byline:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return Byline()
    article = next((e for e in root.iter() if localname(e.tag) == "article"), None)
    front = _child(article, "front")
    meta = _child(front, "article-meta")
    if meta is not None:
        return _jats(front, meta)
    return _elsevier(root)


# -- JATS ---------------------------------------------------------------------


def _jats(front, meta) -> Byline:
    byline = Byline()
    affs = []                                   # (id, label, text)
    for aff in _jats_affs(meta):
        label = _first_text(aff, "label", direct_only=True) or _first_text(aff, "sup", True)
        text = _clean(" ".join(_text_without(aff, ("label", "sup", "institution-id"))))
        if text:
            affs.append((aff.get("id") or "", label, text.strip(" ,;")))

    authors = []                                # (name, [(rid, kind, text)])
    for group in meta:
        if localname(group.tag) != "contrib-group":
            continue
        for contrib in group:
            if localname(contrib.tag) != "contrib":
                continue
            if (contrib.get("contrib-type") or "author").lower() != "author":
                continue
            name = _jats_name(contrib)
            if name:
                refs = [(x.get("rid") or "", (x.get("ref-type") or "").lower(),
                         _clean("".join(x.itertext())))
                        for x in contrib.iter() if localname(x.tag) == "xref"]
                authors.append((name, refs))

    # An editor's or reviewer's <aff> sits beside the authors'; when authors
    # point at theirs, only those belong in the byline.
    cited = {rid for _, refs in authors for rid, kind, _ in refs if kind == "aff"}
    if cited & {aff_id for aff_id, _, _ in affs}:
        affs = [a for a in affs if a[0] in cited]
    if len(affs) > 1 and not any(label for _, label, _ in affs):
        affs = [(aff_id, str(n), text) for n, (aff_id, _, text) in enumerate(affs, 1)]
    labels = {aff_id: label for aff_id, label, _ in affs if aff_id}
    byline.affiliations = [(label, text) for _, label, text in affs]

    for name, refs in authors:
        marks = []
        for rid, kind, text in refs:
            if kind not in ("aff", "corresp", "fn", "author-notes"):
                continue
            mark = labels.get(rid) if kind == "aff" and rid in labels else text
            if mark and mark not in marks:
                marks.append(mark)
        byline.authors.append((name, marks))

    journal_meta = _child(front, "journal-meta")
    journal = ""
    if journal_meta is not None:
        journal = (_first_text(journal_meta, "journal-title")
                   or _first_text(journal_meta, "abbrev-journal-title"))
    fpage, lpage = _direct(meta, "fpage"), _direct(meta, "lpage")
    byline.journal = _citation(journal, _jats_year(meta), _direct(meta, "volume"),
                               _direct(meta, "issue"),
                               _pages(fpage, lpage) or _direct(meta, "elocation-id"))
    return byline


def _jats_affs(meta):
    """Each `<aff>` once: under the contrib-groups or the meta, first of any alternatives."""
    found = []
    def visit(element):
        for child in element:
            tag = localname(child.tag)
            if tag == "aff":
                found.append(child)
            elif tag == "aff-alternatives":
                first = _child(child, "aff")
                if first is not None:
                    found.append(first)
            elif tag == "contrib-group":
                visit(child)
    visit(meta)
    return found


def _jats_name(contrib) -> str:
    for el in contrib:
        tag = localname(el.tag)
        if tag == "name-alternatives":
            el = next((n for n in el if localname(n.tag) in ("name", "string-name")), None)
            if el is None:
                continue
            tag = localname(el.tag)
        if tag == "name":
            return " ".join(filter(None, (_first_text(el, "prefix"), _first_text(el, "given-names"),
                                          _first_text(el, "surname"), _first_text(el, "suffix"))))
        if tag in ("string-name", "collab"):
            return _clean(" ".join(el.itertext()))
    return ""


def _jats_year(meta) -> str:
    dates = [d for d in meta if localname(d.tag) == "pub-date"]
    def kind(d):
        return (d.get("pub-type") or d.get("date-type") or "").lower()
    for wanted in ("ppub", "collection", "pub", "epub"):
        for date in dates:
            if kind(date) == wanted and _first_text(date, "year"):
                return _first_text(date, "year")
    return next((_first_text(d, "year") for d in dates if _first_text(d, "year")), "")


# -- Elsevier -----------------------------------------------------------------


def _elsevier(root) -> Byline:
    byline = Byline()
    labels = {}
    groups = [e for e in root.iter() if localname(e.tag) == "author-group"]
    for group in groups:
        for aff in group:
            if localname(aff.tag) != "affiliation":
                continue
            label = _first_text(aff, "label", direct_only=True)
            text = _first_text(aff, "textfn") or _clean(
                " ".join(_text_without(aff, ("label", "affiliation"))))
            if text:
                byline.affiliations.append((label, text))
                if aff.get("id"):
                    labels[aff.get("id")] = label
    for group in groups:
        for person in group:
            tag = localname(person.tag)
            if tag == "author":
                name = " ".join(filter(None, (_first_text(person, "given-name"),
                                              _first_text(person, "surname"))))
            elif tag == "collaboration":
                name = _first_text(person, "text") or _clean(" ".join(person.itertext()))
            else:
                continue
            marks = []
            for ref in person.iter():
                if localname(ref.tag) == "cross-ref":
                    # The affiliation's own label wins: some XML prints "1"
                    # at the author and "a" at the affiliation it points to.
                    mark = labels.get(ref.get("refid") or "") or _clean("".join(ref.itertext()))
                    if mark and mark not in marks:
                        marks.append(mark)
            if name:
                byline.authors.append((name, marks))
    if not byline.authors:
        for el in root.iter():
            if localname(el.tag) == "creator":
                surname, _, given = _clean("".join(el.itertext())).partition(", ")
                byline.authors.append((" ".join(filter(None, (given, surname))), []))

    coredata = next((e for e in root.iter() if localname(e.tag) == "coredata"), root)
    first, last = _first_text(coredata, "startingpage"), _first_text(coredata, "endingpage")
    byline.journal = _citation(
        _first_text(coredata, "publicationname"), _first_text(coredata, "coverdate")[:4],
        _first_text(coredata, "volume"), _first_text(coredata, "issueidentifier"),
        _pages(first, last) or _first_text(coredata, "pagerange"))
    return byline


# -- shared -------------------------------------------------------------------


def _citation(journal: str, year: str, volume: str, issue: str, pages: str) -> str:
    """`<i>Journal.</i> 2026;21(11):5256–5260.`, or "" when there is no journal."""
    if not journal:
        return ""
    return _compose(authors=[], etal=False, title="", editors=[], source=journal,
                    publisher="", year=year, volume=volume, issue=issue, pages=pages,
                    doi="", url="", comment="")


def _pages(first: str, last: str) -> str:
    return f"{first}–{last}" if first and last and first != last else first


def _text_without(element, skip) -> List[str]:
    """Text of `element`, leaving out the subtrees of the named descendants (tails kept)."""
    parts = [element.text or ""]
    for child in element:
        if localname(child.tag) not in skip:
            parts.extend(_text_without(child, skip))
        parts.append(child.tail or "")
    return parts


def _direct(element, name: str) -> str:
    return _first_text(element, name, direct_only=True)


def _child(element, name: str):
    if element is None:
        return None
    return next((c for c in element if localname(c.tag) == name), None)


def _esc(text: Optional[str]) -> str:
    return html_lib.escape(text or "", quote=True)
