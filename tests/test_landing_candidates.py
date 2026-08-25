"""Offline tests for which links on a landing page are worth downloading.

Following the policy in `test_landing_page_detection`, the pages here are
minimal synthetic reproductions of the exact signal rather than copies of real
pages. The signal being reproduced is the one from PMC9292464: an article page
whose `<meta citation_pdf_url>` names its own PDF, and whose reference list
carries two PDFs belonging to somebody else.
"""

import pytest

import fetchpdf.fetchpdf as fpd

PMC = "https://pmc.ncbi.nlm.nih.gov/articles/PMC9292464/"
DOI = "10.1111/all.14949"

OWN_PDF = "https://pmc.ncbi.nlm.nih.gov/articles/PMC9292464/pdf/nihms-1795017.pdf"
USDA = ("https://www.dietaryguidelines.gov/sites/default/files/2020-12/"
        "Dietary_Guidelines_for_Americans_2020-2025.pdf")
EAACI = "http://www.eaaci.org/globalatlas/GlobalAtlasAllergy.pdf"

PMC_PAGE = f"""
<html><head>
  <meta name="citation_pdf_url" content="{OWN_PDF}">
</head><body>
  <a href="/articles/PMC9292464/">Article</a>
  <section class="ref-list"><ol>
    <li>U.S. Department of Agriculture. <a href="{USDA}">Dietary Guidelines</a></li>
    <li>EAACI. <a href="{EAACI}">Global Atlas of Allergy</a></li>
  </ol></section>
</body></html>
"""


def _urls(landing, page, doi=DOI):
    return [c.url for c in fpd._collect_landing_page_candidates(landing, page, doi=doi)]


# --------------------------------------------------------------------------
# The regression
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cited", [USDA, EAACI])
def test_a_pdf_the_paper_cites_is_not_a_candidate(cited):
    """ONLY this paper's own material. A PDF the paper CITES belongs to
    somebody else. Both of these were downloadable, both were kept on the
    strength of a substring, and the second one was saved as the paper."""
    assert cited not in _urls(PMC, PMC_PAGE)


def test_the_pages_own_declared_pdf_ranks_first():
    assert _urls(PMC, PMC_PAGE)[0] == OWN_PDF


def test_the_declared_pdf_outranks_a_same_host_pdf():
    """This is the dead-bonus test. `_score` awarded +3.0 for the string
    "citation_pdf_url" appearing *inside a URL*, where it never appears, so a
    declared PDF and any other .pdf tied at 4.0 and the winner was decided by
    sort stability. Provenance is now assigned at harvest time.
    """
    page = PMC_PAGE.replace(
        "</body>",
        '<a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC9292464/other.pdf">x</a></body>')
    candidates = fpd._collect_landing_page_candidates(PMC, page, doi=DOI)
    assert candidates[0].url == OWN_PDF
    assert candidates[0].origin == fpd.ORIGIN_DECLARED


def test_the_reference_list_does_not_crowd_out_the_real_pdf():
    """Only the first `_LANDING_MAX_CANDIDATES` are ever tried, so a
    bibliography that fills those slots is a denial of service on the article's
    own copy even when that copy is present and reachable."""
    refs = "".join(f'<a href="https://ref{i}.example.org/paper{i}.pdf">r</a>'
                   for i in range(40))
    urls = _urls(PMC, PMC_PAGE.replace("</body>", refs + "</body>"))
    assert OWN_PDF in urls[:fpd._LANDING_MAX_CANDIDATES]


# --------------------------------------------------------------------------
# What must NOT regress: off-host copies that are legitimately ours
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://arxiv.org/pdf/2401.00001",
    "https://other.univ.edu/bitstream/handle/1/thesis.pdf",
    "https://escholarship.org/content/qt1234abcd/qt1234abcd.pdf",
    "https://www.psycharchives.org/bitstream/20.500.12034/1/article.pdf",
    "https://www.econstor.eu/bitstream/10419/1/dp123.pdf",
    "https://core.ac.uk/download/pdf/12345.pdf",
])
def test_off_host_repository_copies_still_survive(url):
    """The landing-page route exists for these. Excluding off-host links
    wholesale would fix the bug by deleting the feature."""
    page = f'<html><body><a href="{url}">copy</a></body></html>'
    assert url in _urls("https://journal.example/article", page)


def test_a_publisher_pattern_carrying_our_doi_survives_off_host():
    url = f"https://onlinelibrary.wiley.com/doi/pdfdirect/{DOI}"
    page = f'<html><body><a href="{url}">pdf</a></body></html>'
    candidates = fpd._collect_landing_page_candidates(
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC9292464/", page, doi=DOI)
    assert [c.url for c in candidates] == [url]
    assert candidates[0].origin == fpd.ORIGIN_DOI_PATH


@pytest.mark.parametrize("url", [
    "https://who.int/publications/report-2019.pdf",
    "https://www.gov.example/sites/default/files/guidance.pdf",
])
def test_a_bare_off_host_pdf_with_no_repository_signature_is_dropped(url):
    page = f'<html><body><a href="{url}">cited work</a></body></html>'
    assert _urls("https://journal.example/article", page) == []


def test_same_host_navigation_does_not_consume_candidate_slots():
    """A PMC page carries hundreds of same-host nav links. Admitting them
    spends the eight slots on account settings before reaching a file."""
    nav = "".join(f'<a href="https://journal.example/section/{i}">nav</a>'
                  for i in range(50))
    page = f'<html><body>{nav}<a href="https://journal.example/x/full.pdf">pdf</a></body></html>'
    assert _urls("https://journal.example/article", page) == [
        "https://journal.example/x/full.pdf"]
