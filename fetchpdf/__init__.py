"""
fetchpdf: A comprehensive tool to download PDFs from DOIs using multiple sources.

This package provides functions to download academic papers (PDFs) from DOIs using
multiple fallback sources including OpenAlex, Unpaywall, PubMed Central, Crossref,
Europe PMC, Semantic Scholar
"""

from .fetch_pdf_from_doi import fetch_pdf_from_doi, batch_fetch_pdfs
from .fetch_metadata_from_doi import fetch_metadata_from_doi

__version__ = "0.1.0"
__all__ = ["fetch_pdf_from_doi", "batch_fetch_pdfs", "fetch_metadata_from_doi"]
