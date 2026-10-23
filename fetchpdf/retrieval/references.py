"""The article's reference list, as HTML, for the rendered page (to_html.py).

The Markdown route leaves references out on purpose: cited papers are not
evidence about the screened cohort, and a model reading them conflates the
two. A page a person reads is different -- "(Fig. 1)" and "[3]" in the prose
point at something, and a rendering without the list looks like a broken
copy. So this lives beside the walker, never inside it, and only to_html.py
calls it.

Two vocabularies, one shape. JATS `<ref>` holds an `<element-citation>`
(fields only), a `<mixed-citation>` (fields with the publisher's own
punctuation between them) or both; Elsevier `<ce:bib-reference>` holds an
`<sb:reference>` (fields only) or `<ce:other-ref><ce:textref>` (typeset text).
Typeset text is kept verbatim. Fields are assembled in Vancouver order --
authors. title. source. year;volume(issue):pages. doi -- since a structured
citation carries no style of its own to preserve.
"""

import html as html_lib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

from ._util import localname
from .to_markdown import _clean, _first_text

_CITATIONS = ("element-citation", "mixed-citation", "nlm-citation", "citation")
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


@dataclass
class Reference:
    id: str
    label: str
    html: str          # the citation, escaped, with a DOI link when there is one


def references(root) -> List[Reference]:
    """Every entry of every reference list in the document, in order."""
    found: List[Reference] = []
    for container in root.iter():
        tag = localname(container.tag)
        # Direct children only: a nested ref-list or bibliography-sec is
        # visited by iter() in its own turn, so nothing is listed twice.
        if tag == "ref-list":
            found.extend(_jats_ref(el) for el in container if localname(el.tag) == "ref")
        elif tag in ("bibliography", "bibliography-sec"):
            found.extend(_elsevier_ref(el) for el in container
                         if localname(el.tag) == "bib-reference")
    return [ref for ref in found if ref.html]


def references_from_xml(content: bytes) -> List[Reference]:
    try:
        return references(ET.fromstring(content))
    except ET.ParseError:
        return []


def references_section(refs: List[Reference]) -> str:
    """`<section>` holding the list, or "" when there is none."""
    if not refs:
        return ""
    items = []
    for ref in refs:
        label = ref.label
        if label and label[-1].isalnum():
            label += "."
        anchor = f' id="{_esc(ref.id)}"' if ref.id else ""
        items.append(f"<li{anchor}>"
                     + (f'<span class="ref-label">{_esc(label)}</span> ' if label else "")
                     + f"{ref.html}</li>")
    return ("<section class=\"references\">\n<h2>References</h2>\n<ol>\n"
            + "\n".join(items) + "\n</ol>\n</section>\n")


# -- JATS ---------------------------------------------------------------------


def _jats_ref(ref) -> Reference:
    label = _first_text(ref, "label", direct_only=True)
    citations = [c for c in ref if localname(c.tag) in _CITATIONS]
    # A <ref> with both kinds carries the same citation twice; the typeset one wins.
    mixed = [c for c in citations if _is_typeset(c)]
    chosen = mixed or [c for c in citations if localname(c.tag) != "mixed-citation"] or citations
    if chosen:
        text = " ".join(filter(None, (_jats_citation(c) for c in chosen)))
    else:
        text = _esc(_clean(" ".join(t for el in ref if localname(el.tag) != "label"
                                    for t in el.itertext())))
    return Reference(id=ref.get("id") or "", label=label, html=text)


def _is_typeset(citation) -> bool:
    """A mixed-citation whose punctuation is in the XML, not left to the renderer.

    Some publishers tag a mixed-citation field by field with nothing between
    the fields; read verbatim, that runs "SmithJ2020Title" together.
    """
    if localname(citation.tag) != "mixed-citation":
        return False
    return bool((citation.text or "").strip()) or any((c.tail or "").strip() for c in citation)


def _jats_citation(citation) -> str:
    doi = _pub_id(citation, "doi")
    if _is_typeset(citation):
        text = _esc(_clean("".join(citation.itertext())))
        if doi and doi.lower() not in text.lower():
            text += " " + _doi_link(doi)
        return text

    authors, editors, etal = [], [], False
    groups = [g for g in citation.iter() if localname(g.tag) == "person-group"]
    for group in groups or [citation]:
        names = [_jats_name(n) for n in group
                 if localname(n.tag) in ("name", "string-name", "collab", "name-alternatives")]
        etal = etal or any(localname(n.tag) == "etal" for n in group)
        kind = (group.get("person-group-type") or "author").lower()
        (editors if kind == "editor" else authors).extend(n for n in names if n)
    title = (_first_text(citation, "article-title") or _first_text(citation, "chapter-title")
             or _first_text(citation, "part-title"))
    fpage, lpage = _first_text(citation, "fpage"), _first_text(citation, "lpage")
    pages = f"{fpage}–{lpage}" if fpage and lpage and fpage != lpage else fpage
    url = next((el.get(_XLINK_HREF) for el in citation.iter()
                if localname(el.tag) in ("ext-link", "uri") and el.get(_XLINK_HREF)), "")
    return _compose(
        authors=authors, etal=etal, title=title, editors=editors,
        source=_first_text(citation, "source"),
        publisher=_join(": ", _first_text(citation, "publisher-loc"),
                        _first_text(citation, "publisher-name")),
        year=_first_text(citation, "year"), volume=_first_text(citation, "volume"),
        issue=_first_text(citation, "issue"),
        pages=pages or _first_text(citation, "elocation-id"),
        doi=doi, url=url, comment=_first_text(citation, "comment"),
    )


def _jats_name(name) -> str:
    tag = localname(name.tag)
    if tag == "name-alternatives":
        first = next((n for n in name if localname(n.tag) in ("name", "string-name")), None)
        return _jats_name(first) if first is not None else ""
    if tag == "name":
        return _join(" ", _first_text(name, "surname"), _first_text(name, "given-names"),
                     _first_text(name, "suffix"))
    return _clean(" ".join(name.itertext()))


def _pub_id(citation, kind: str) -> str:
    for el in citation.iter():
        if localname(el.tag) == "pub-id" and (el.get("pub-id-type") or "").lower() == kind:
            return _clean("".join(el.itertext()))
    return ""


# -- Elsevier -----------------------------------------------------------------


def _elsevier_ref(bib) -> Reference:
    label = _first_text(bib, "label", direct_only=True)
    texts = []
    for child in bib:
        tag = localname(child.tag)
        if tag == "reference":
            texts.append(_sb_reference(child))
        elif tag == "other-ref":
            texts.append(_esc(_clean(" ".join(child.itertext()))))
    return Reference(id=bib.get("id") or "", label=label,
                     html=" ".join(t for t in texts if t))


def _sb_reference(ref) -> str:
    contribution = _child(ref, "contribution")
    host = _child(ref, "host")
    authors, etal = [], False
    if contribution is not None:
        authors, etal = _sb_people(_child(contribution, "authors"))
    title = _first_text(contribution, "maintitle") if contribution is not None else ""

    source = volume = issue = year = publisher = ""
    editors: List[str] = []
    pages = ""
    if host is not None:
        kind = next((c for c in host if localname(c.tag) in
                     ("issue", "book", "edited-book", "e-host")), None)
        if kind is not None:
            series = _child(kind, "series")
            title_el = _child(series if series is not None else kind, "title")
            source = _first_text(title_el, "maintitle") if title_el is not None else ""
            volume = _first_text(kind, "volume-nr")
            issue = _first_text(kind, "issue-nr")
            year = _first_text(kind, "date")
            editors, _ = _sb_people(_child(kind, "editors"))
            pub = _child(kind, "publisher")
            if pub is not None:
                publisher = _join(": ", _first_text(pub, "location"), _first_text(pub, "name"))
        first, last = _first_text(host, "first-page"), _first_text(host, "last-page")
        pages = (f"{first}–{last}" if first and last and first != last else first) \
            or _first_text(host, "article-number")
    if not title and source and not editors:
        title, source = source, ""          # a book cited whole: its title is the title
    url = next((el.get(_XLINK_HREF) for el in ref.iter()
                if localname(el.tag) == "inter-ref" and el.get(_XLINK_HREF)), "")
    return _compose(
        authors=authors, etal=etal, title=title, editors=editors, source=source,
        publisher=publisher, year=year, volume=volume, issue=issue, pages=pages,
        doi=_first_text(ref, "doi"), url=url, comment=_first_text(ref, "comment"),
    )


def _sb_people(group):
    names, etal = [], False
    if group is None:
        return names, etal
    for person in group:
        tag = localname(person.tag)
        if tag in ("author", "editor"):
            names.append(_join(" ", _first_text(person, "surname"),
                               _first_text(person, "given-name")))
        elif tag == "collaboration":
            names.append(_clean(" ".join(person.itertext())))
        elif tag == "et-al":
            etal = True
    return [n for n in names if n], etal


def _child(element, name: str):
    if element is None:
        return None
    return next((c for c in element if localname(c.tag) == name), None)


# -- composing ----------------------------------------------------------------


def _compose(authors, etal, title, editors, source, publisher, year, volume, issue,
             pages, doi, url, comment) -> str:
    parts = []
    if authors:
        parts.append(_sentence(_esc(", ".join(authors) + (", et al" if etal else ""))))
    if title:
        parts.append(_sentence(_esc(title)))
    if editors:
        parts.append(f"In: {_esc(', '.join(editors))}, editor{'s' if len(editors) > 1 else ''}.")
    if source:
        parts.append(f"<i>{_sentence(_esc(source))}</i>")
    if publisher:
        parts.append(_sentence(_esc(publisher)))
    detail = _esc(year)
    if volume or issue:
        detail += (";" if detail else "") + _esc(volume) + (f"({_esc(issue)})" if issue else "")
    if pages:
        detail += (":" if volume or issue else (", " if detail else "")) + _esc(pages)
    if detail:
        parts.append(detail + ".")
    if comment:
        parts.append(_sentence(_esc(comment)))
    if doi:
        parts.append(_doi_link(doi))
    elif url:
        parts.append(f'<a href="{_esc(url)}">{_esc(url)}</a>')
    return " ".join(parts)


def _doi_link(doi: str) -> str:
    return f'doi: <a href="https://doi.org/{_esc(quote(doi, safe="/:;()._-"))}">{_esc(doi)}</a>'


def _sentence(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else text + "."


def _join(sep: str, *values: Optional[str]) -> str:
    return sep.join(v for v in values if v)


def _esc(text: str) -> str:
    return html_lib.escape(text or "", quote=True)
