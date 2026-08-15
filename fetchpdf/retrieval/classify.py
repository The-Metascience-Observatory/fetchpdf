"""Assign a tier from the bytes, never from the Content-Type header.

Servers lie, and they lie in the direction that costs us most: an Elsevier TDM
endpoint advertising text/xml can return an HTML entitlement page, and a
publisher "PDF" link can return a landing page as application/pdf. Trusting the
header means a T7 landing page enters the pipeline labelled T1.

The served Content-Type is still recorded in provenance -- knowing that a server
claimed XML and delivered HTML is diagnostic. It just never decides anything.

One genuine ambiguity: CSV/TSV bytes are both a structured supplement (T4) and
plain text (T6), and nothing in the bytes distinguishes them. There the source's
declared tier breaks the tie, since it knows whether it fetched a file listing
or a full-text endpoint. Everywhere else the content wins outright.
"""

import re
from typing import Optional

from .tiers import Tier

_PDF_MAGIC = b"%PDF"
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"
_BZIP2_MAGIC = b"BZh"

_HTML_RE = re.compile(rb"^\s*(?:<!doctype\s+html|<html\b)", re.IGNORECASE)
_XML_DECL_RE = re.compile(rb"^\s*(?:<\?xml|<!doctype\s+(?!html))", re.IGNORECASE)

#: Root/marker elements that mean "structured full text", not merely "is XML".
_STRUCTURED_MARKERS = (
    b"<article",          # JATS
    b"<TEI",              # TEI
    b"<tei",
    b"<pmc-articleset",   # efetch db=pmc wrapper
    b"<full-text-retrieval-response",  # Elsevier TDM
    b"<xocs:doc",         # Elsevier
)

#: Elements that mean the document carries real document structure.
_STRUCTURAL_BODY_MARKERS = (
    b"<body", b":body", b"<sec", b"ce:sections", b"ce:para", b"<text",
)

#: Elsevier serves a "full text XML" whose only content is <xocs:rawtext>: the
#: article as one flat string, no sections, no tables. It is an XML envelope
#: around plain text, and calling it T1 because it parses as XML is how a
#: document with zero recoverable table structure gets treated as lossless.
_FLATTENED_MARKERS = (b"xocs:rawtext",)

_TABLE_RE = re.compile(rb"<table[\s>]", re.IGNORECASE)
_DELIMITED_RE = re.compile(r"^[^\n]*[,\t;][^\n]*(?:\n[^\n]*[,\t;][^\n]*){2,}")


def classify(content: bytes, declared: Optional[Tier] = None) -> Tier:
    """The tier these bytes actually are."""
    if not content:
        return Tier.T7_LANDING

    head = content[:4096]

    if head.startswith(_PDF_MAGIC):
        return Tier.T5_PDF
    if head.startswith(_GZIP_MAGIC) or head.startswith(_BZIP2_MAGIC):
        # gzip/bzip2 in this pipeline means an e-print source tarball.
        return Tier.T3_SOURCE
    if head.startswith(_ZIP_MAGIC):
        # zip covers supplement bundles and the OOXML formats (.xlsx/.docx).
        return Tier.T4_SUPPLEMENT

    if _HTML_RE.match(head):
        return Tier.T2_HTML if _TABLE_RE.search(content) else Tier.T7_LANDING

    if _XML_DECL_RE.match(head) or head.lstrip().startswith(b"<"):
        window = content[:262144]
        if any(marker in window for marker in _STRUCTURED_MARKERS):
            flattened = any(m in window for m in _FLATTENED_MARKERS)
            structured = any(m in window for m in _STRUCTURAL_BODY_MARKERS)
            if flattened and not structured:
                return Tier.T6_PLAINTEXT
            return Tier.T1_XML
        # XML, but not a structured full-text document: an API error envelope,
        # an OAI wrapper, an RSS feed.
        return Tier.T7_LANDING

    text = content[:65536].decode("utf-8", errors="replace")
    if _DELIMITED_RE.match(text):
        return Tier.T4_SUPPLEMENT if declared == Tier.T4_SUPPLEMENT else Tier.T6_PLAINTEXT

    if declared in (Tier.T4_SUPPLEMENT, Tier.T6_PLAINTEXT):
        return declared
    return Tier.T6_PLAINTEXT


def describe(content: bytes) -> str:
    """Short human-readable shape, for demotion log lines."""
    if not content:
        return "empty"
    head = content[:512]
    if head.startswith(_PDF_MAGIC):
        return "PDF"
    if head.startswith(_GZIP_MAGIC):
        return "gzip"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if _HTML_RE.match(head):
        return "HTML"
    if head.lstrip().startswith(b"<"):
        return "XML"
    return "text"
