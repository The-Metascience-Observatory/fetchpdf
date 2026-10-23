"""Tier gates. HTTP 200 is not evidence of full text.

Two failures this exists to catch, both of which look like success:

  * NCBI efetch db=pmc returns 200 and a well-formed document for non-OA
    records: <pmc-articleset><article> with complete front matter, journal
    metadata, title, authors -- and no <body> at all. The explanation is an XML
    *comment* immediately after the <article> tag:

        <!--The publisher of this article does not allow downloading of the
            full text in XML form.-->

    Verified on PMC3390974 and PMC2148499. The comment matters: ElementTree
    discards comments, so a parsed-tree search for that sentence finds nothing
    and the record fails with a puzzling "no <body>" instead of the real reason.
    That is why the denial check runs against the raw bytes.

  * Elsevier's TDM API returns 200 with a coredata-only payload when the caller
    is not entitled, and publisher landing pages routinely serve paywall
    interstitials and Cloudflare challenges as 200.

On failure the caller logs the reason and descends the ladder. Never accept
silently, never hard-fail the record.
"""

import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from .artifact import ValidationResult
from ._util import localname as _localname
from ._util import normalise_doi as _normalise_doi

#: Documents that parse but carry no full text. Matched against raw bytes, not
#: the parsed tree: PMC puts this in an XML comment (see module docstring).
_DENIAL_SIGNATURES = (
    b"does not allow downloading of the full text",
    b"the publisher of this article does not allow",
    b"full text is not available for this article",
)


def _denial_in(content: bytes) -> bool:
    lowered = content[:8192].lower()
    return any(signature in lowered for signature in _DENIAL_SIGNATURES)

#: HTML that is really an access wall or a bot challenge, served as 200.
#:
#: The second block was added after a real corpus run: publisher/journal
#: navigation pages, cookie-consent gates, and a JS-disabled fallback shell all
#: cleared the original signatures plus the table/char-count floors below (a
#: nav or metrics table trivially has >=1 populated <td>, and a page full of
#: menu/policy chrome trivially clears 5000 chars). Each phrase here was pulled
#: verbatim from a page that fooled the original list: a Journal-of-Neurosurgery
#: TOC/cookie page, a Dove Medical Press no-JS shell, and a Russian publisher's
#: profession-verification gate (matched on its own text, not a translation --
#: translating it would not match the byte content being checked).
_PAYWALL_SIGNATURES = (
    "just a moment",                 # Cloudflare interstitial
    "enable javascript and cookies",
    "access denied",
    "purchase pdf",
    "get access to the full version",
    "sign in to continue reading",
    "subscribe to view the full text",
    "your institution does not have access",
    "checking your browser before accessing",
    "this site uses cookies",
    "dismiss this warning",
    "javascript is currently disabled",
    "закрывая это сообщение",         # "by closing this message [you confirm...]"
)

#: Hostname/path fragments that mean "repository or aggregator record page",
#: not "publisher full text" -- checked against the FINAL, post-redirect URL.
#: An institutional-repository (DSpace/CRIS/IRIS) catalog page routinely shows
#: the real title and even the abstract, which is exactly why a text-content
#: check alone does not catch it; the URL is where the identity of the page
#: actually lives. `iris.` is deliberately a prefix/domain-label fragment
#: rather than a full hostname, since IRIS is a shared platform (`iris.<inst>.
#: it`) used by many Italian universities under different domains.
_REPOSITORY_URL_MARKERS = (
    "iris.",
    "/handle/",
    "dspace",
    "cris.",
)


def _looks_like_repository_url(url: Optional[str]) -> Optional[str]:
    """The matched marker if `url` looks like a repository/catalog page, else None."""
    if not url:
        return None
    lowered = url.lower()
    for marker in _REPOSITORY_URL_MARKERS:
        if marker in lowered:
            return marker
    return None


def _meta_content(tree, name: str) -> str:
    """The `content` attribute of a `<meta name="..."/>` or `<meta property="...">`, or ""."""
    for el in tree.iter("meta"):
        if (el.get("name") or el.get("property") or "").lower() == name.lower():
            return (el.get("content") or "").strip()
    return ""




def _citation_doi_mismatch(tree, requested_doi: Optional[str]) -> Optional[str]:
    """The page's own declared DOI if it contradicts `requested_doi`, else None.

    Present-and-different is the only signal used here: absence proves nothing
    (most publisher HTML omits these tags), so a missing meta tag must never be
    treated as a mismatch.
    """
    if not requested_doi:
        return None
    declared = _meta_content(tree, "citation_doi") or _meta_content(tree, "dc.identifier")
    if not declared:
        return None
    if _normalise_doi(declared) == _normalise_doi(requested_doi):
        return None
    return declared


_HTML_SNIFF_RE = re.compile(rb"^\s*(?:<!doctype\s+html|<html\b)", re.IGNORECASE)


def _iterfind(root, *names) -> List[ET.Element]:
    wanted = {n.lower() for n in names}
    return [el for el in root.iter() if _localname(el.tag) in wanted]


def _text_of(elements) -> str:
    return " ".join("".join(el.itertext()) for el in elements).strip()


def parse_xml(content: bytes) -> Tuple[Optional[ET.Element], str]:
    """Parse, returning (root, error). Never raises."""
    if not content:
        return None, "empty response"
    if _HTML_SNIFF_RE.match(content):
        # An HTML error page served where XML was requested. Worth its own
        # message: "not well-formed" would send someone hunting for an encoding
        # bug that is not there.
        return None, "HTML served where XML was expected"
    try:
        return ET.fromstring(content), ""
    except ET.ParseError as e:
        return None, f"not well-formed XML: {str(e)[:120]}"


def _is_complete_short_published_abstract(root, doi, pmid) -> bool:
    """Recognize a short publication, not an abstract-only research-article stub.

    Conference reports may themselves be published as JATS ``article-type=
    abstract`` with the complete report in ``body``. Keep the normal stub floor
    and require an exact requested identity, one primary article, and substantive
    methods/results/conclusions in that body. Never infer this exception from a
    title mentioning a conference, a DOI in references, or front-matter abstract.
    """
    if (_localname(root.tag) != "article" or root.get("article-type") not in
            {"abstract", "conference-abstract", "meeting-abstract"}):
        return False
    if any(_localname(el.tag) in {"article", "sub-article", "response", "error", "error-message"}
           for el in list(root.iter())[1:]):
        return False

    def direct(parent, name):
        return [child for child in parent if _localname(child.tag) == name]

    fronts, bodies = direct(root, "front"), direct(root, "body")
    if len(fronts) != 1 or len(bodies) != 1:
        return False
    metadata = direct(fronts[0], "article-meta")
    if len(metadata) != 1:
        return False
    ids = direct(metadata[0], "article-id")
    own_dois = {_normalise_doi("".join(el.itertext())) for el in ids
                if el.get("pub-id-type") == "doi"}
    own_pmids = {"".join(el.itertext()).strip() for el in ids
                 if el.get("pub-id-type") in {"pmid", "pubmed"}}
    if not doi and not pmid:
        return False
    if doi and own_dois != {_normalise_doi(doi)}:
        return False
    if pmid and own_pmids != {str(pmid)}:
        return False
    title_groups = direct(metadata[0], "title-group")
    titles = direct(title_groups[0], "article-title") if len(title_groups) == 1 else []
    if len(titles) != 1 or len(_text_of(titles)) < 20:
        return False
    # A front abstract copied into a nested <abstract> or error payload does
    # not become the publication's complete body merely by being long enough.
    if any(_localname(el.tag) in {"abstract", "ref-list", "ref", "error", "error-message"}
           for el in bodies[0].iter()):
        return False
    sections = set()
    for element in bodies[0].iter():
        name = _localname(element.tag)
        heading = None
        if name == "sec":
            headings = direct(element, "title")
            heading = headings[0] if len(headings) == 1 else None
        elif name == "p" and len(element) and not (element.text or "").strip():
            first = element[0]
            if _localname(first.tag) in {"bold", "label"}:
                heading = first
        if heading is None:
            continue
        label = re.sub(r"[^a-z]", "", _text_of([heading]).lower())
        body = _text_of([element])
        if label in {"methods", "results", "conclusion", "conclusions"} and len(body) - len(_text_of([heading])) >= 80:
            sections.add("conclusions" if label.startswith("conclusion") else label)
    return sections == {"methods", "results", "conclusions"}


def validate_t1(content: bytes, min_chars: int = 5000,
                abstract_stub_chars: int = 2500, doi: Optional[str] = None,
                pmid: Optional[str] = None) -> ValidationResult:
    """Gate for JATS/TEI structured full text."""
    # Against the raw bytes, before parsing: PMC states the denial in an XML
    # comment, which every parser drops. Checking the tree would report "no
    # <body>" and send someone looking for a parse bug that is not there.
    if _denial_in(content):
        return ValidationResult.failure(
            "publisher denial stub (200 with no full text)",
            checks_passed=[],
        )

    root, error = parse_xml(content)
    if root is None:
        return ValidationResult.failure(error)
    checks = ["parses", "not-a-denial-stub"]

    # Check only this article's front matter, never identifiers in references
    # or related-article links. A complete XML document can still be the wrong
    # report, and must not supersede a correct existing PDF on that basis.
    own_dois, own_pmids = set(), set()
    for front in _iterfind(root, "front", "teiHeader", "coredata"):
        for node in front.iter():
            name = _localname(node.tag)
            kind = (node.get("pub-id-type") or node.get("type") or "").lower()
            value = "".join(node.itertext()).strip()
            if (name == "article-id" and kind == "doi") or (name == "idno" and kind == "doi") or name == "doi":
                own_dois.add(_normalise_doi(value))
            if name == "article-id" and kind in ("pmid", "pubmed"):
                own_pmids.add(value)
    if doi and own_dois and own_dois != {_normalise_doi(doi)}:
        return ValidationResult.failure("XML declares a different or ambiguous article DOI: " + ", ".join(sorted(own_dois)), checks_passed=checks)
    if pmid and own_pmids and own_pmids != {str(pmid)}:
        return ValidationResult.failure("XML declares a different or ambiguous article PMID: " + ", ".join(sorted(own_pmids)), checks_passed=checks)
    checks.append("declared-article-identifiers-consistent")

    bodies = _iterfind(root, "body")
    if not bodies:
        # TEI puts the running text in <text>, not <body>.
        bodies = _iterfind(root, "text")
    if not bodies:
        return ValidationResult.failure("no <body> or TEI <text> element", checks_passed=checks)

    body_text = _text_of(bodies)
    n_chars = len(body_text)
    if not body_text:
        return ValidationResult.failure("<body> present but empty", checks_passed=checks)
    checks.append("body-populated")

    # <table-wrap> wraps a <table>; counting both would double every JATS table.
    n_tables = len(_iterfind(root, "table-wrap")) or len(_iterfind(root, "table"))
    n_footnotes = len(_iterfind(root, "table-wrap-foot", "fn", "note"))
    n_cells = len([c for c in _iterfind(root, "td") if "".join(c.itertext()).strip()])

    if n_chars < abstract_stub_chars:
        return ValidationResult.failure(
            f"abstract-length stub ({n_chars} chars < {abstract_stub_chars})",
            checks_passed=checks,
            n_chars=n_chars,
            n_tables=n_tables,
            n_footnotes=n_footnotes,
        )
    if n_chars < min_chars:
        if not _is_complete_short_published_abstract(root, doi, pmid):
            return ValidationResult.failure(
                f"below full-text threshold ({n_chars} chars < {min_chars})",
                checks_passed=checks,
                n_chars=n_chars,
                n_tables=n_tables,
                n_footnotes=n_footnotes,
            )
        checks.extend(["publisher-declared-short-abstract", "own-article-identity-verified",
                       "structured-abstract-body", f"abstract-body-chars>={abstract_stub_chars}"])
    else:
        checks.append(f"min-chars>={min_chars}")

    return ValidationResult(
        ok=True,
        reason="",
        checks_passed=checks,
        n_chars=n_chars,
        n_tables=n_tables,
        n_footnotes=n_footnotes,
        n_populated_cells=n_cells,
    )


def validate_t2(content: bytes, min_chars: int = 5000,
                abstract_stub_chars: int = 2500,
                min_populated_td: int = 1,
                url: Optional[str] = None,
                doi: Optional[str] = None) -> ValidationResult:
    """Gate for publisher HTML full text.

    The load-bearing check is populated <td> count, not table count. A static
    fetch of a JS-rendered article returns the table *container* with 200 and no
    cells; tables rendered as <img> likewise have zero <td>. Both must fail here
    rather than being handed to a model as if they held data.

    `url` and `doi` are optional because most callers of this module in tests
    and one-off scripts do not have them handy -- every check that uses them
    degrades to "not checked" rather than failing closed when they are absent.
    """
    if not content:
        return ValidationResult.failure("empty response")

    text = content.decode("utf-8", errors="replace")
    lowered = text.lower()
    for signature in _PAYWALL_SIGNATURES:
        if signature in lowered:
            return ValidationResult.failure(f"paywall/bot-challenge signature: {signature!r}")
    checks = ["no-paywall-signature"]

    repo_marker = _looks_like_repository_url(url)
    if repo_marker:
        return ValidationResult.failure(
            f"URL looks like a repository/catalog page (matched {repo_marker!r}), "
            "not publisher full text",
            checks_passed=checks,
        )
    checks.append("not-a-repository-url")

    tree = _parse_html(text)
    if tree is None:
        return ValidationResult.failure(
            "lxml not installed (a fetchpdf requirement; reinstall fetchpdf) -- no T2 HTML support",
            checks_passed=checks,
        )
    checks.append("parses")

    declared_doi = _citation_doi_mismatch(tree, doi)
    if declared_doi:
        return ValidationResult.failure(
            f"page declares a different article (citation_doi/dc.identifier="
            f"{declared_doi!r}, requested {doi!r})",
            checks_passed=checks,
        )
    checks.append("citation-doi-consistent")

    body_text = " ".join(tree.itertext()) if hasattr(tree, "itertext") else text
    body_text = re.sub(r"\s+", " ", body_text).strip()
    n_chars = len(body_text)

    tables = tree.findall(".//table")
    n_tables = len(tables)
    populated = 0
    for table in tables:
        for cell in table.iter():
            if _localname(cell.tag) in ("td", "th") and "".join(cell.itertext()).strip():
                populated += 1

    if n_tables == 0:
        return ValidationResult.failure(
            "no <table> elements", checks_passed=checks, n_chars=n_chars
        )
    checks.append("has-table")

    if populated < min_populated_td:
        return ValidationResult.failure(
            "table container with no populated cells "
            "(JS-rendered, separate table viewer, or tables as images)",
            checks_passed=checks,
            n_chars=n_chars,
            n_tables=n_tables,
        )
    checks.append(f"populated-cells>={min_populated_td}")

    if n_chars < abstract_stub_chars:
        return ValidationResult.failure(
            f"abstract-length stub ({n_chars} chars)",
            checks_passed=checks,
            n_chars=n_chars,
            n_tables=n_tables,
            n_populated_cells=populated,
        )
    if n_chars < min_chars:
        return ValidationResult.failure(
            f"below full-text threshold ({n_chars} chars < {min_chars})",
            checks_passed=checks,
            n_chars=n_chars,
            n_tables=n_tables,
            n_populated_cells=populated,
        )
    checks.append(f"min-chars>={min_chars}")

    return ValidationResult(
        ok=True,
        checks_passed=checks,
        n_chars=n_chars,
        n_tables=n_tables,
        n_populated_cells=populated,
        n_footnotes=len([e for e in tree.iter() if _localname(e.tag) == "tfoot"]),
    )


def has_empty_table_containers(content: bytes) -> bool:
    """True when HTML has <table> elements but no populated cells.

    The one condition under which a headless-browser retry is worth its cost:
    the page really does have a table, it just has not been filled in yet.
    """
    tree = _parse_html(content.decode("utf-8", errors="replace") if content else "")
    if tree is None:
        return False
    tables = tree.findall(".//table")
    if not tables:
        return False
    for table in tables:
        for cell in table.iter():
            if _localname(cell.tag) in ("td", "th") and "".join(cell.itertext()).strip():
                return False
    return True


def _parse_html(text: str):
    """Parse HTML with lxml if available, else return None.

    lxml is a requirement, so None here means a broken install. Real publisher
    HTML is malformed often enough that stdlib html.parser produces a tree that
    silently loses table rows, which is worse than not parsing at all -- so the
    T2 source demotes rather than guessing.
    """
    if not text:
        return None
    try:
        import lxml.html
    except ImportError:
        return None
    try:
        return lxml.html.fromstring(text)
    except ValueError:
        # lxml refuses a str that carries an <?xml encoding=...?> declaration
        # (common on XHTML publisher pages); the same text as bytes parses.
        try:
            return lxml.html.fromstring(text.encode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None
