"""The precision contract for finding a paper's own data deposits.

Every case here is real prose taken from the nina_mazar corpus, hand-labelled.
A naive URL regex scores 50% on this set: half the OSF links a paper mentions
are preregistrations, other papers' deposits, or a PsyArXiv DOI that merely
contains "osf.io". Downloading those is the failure this module exists to
prevent -- somebody else's data filed under this paper.

If one of these flips, the tool has started downloading the wrong thing (or
stopped downloading the right thing), which is worth failing a build over.
"""

from fetchpdf.retrieval.fulltext_scan import (
    ACCEPT,
    REFUSE,
    UNCERTAIN,
    scan_text,
)


def _verdict(text, repo="osf"):
    """Best verdict for the single candidate in `text`."""
    found = [c for c in scan_text(text) if c.repo == repo]
    assert found, f"no {repo} candidate found in fixture"
    return found[0].verdict


# --- accepts: the paper's own deposit -------------------------------------

def test_data_availability_statement_is_accepted():
    text = (
        '<sec><title>Data availability</title><p>The data, analysis code, and '
        'stimuli for the main study are publicly available on OSF '
        '<ext-link xlink:href="https://osf.io/xpf87/">https://osf.io/xpf87/</ext-link>.</p></sec>'
    )
    assert _verdict(text) == ACCEPT


def test_deposited_language_is_accepted():
    text = (
        '<p>Materials, implementation protocol, and all data and code have been '
        'deposited at <ext-link xlink:href="https://osf.io/3javq/">https://osf.io/3javq/</ext-link>.</p>'
    )
    assert _verdict(text) == ACCEPT


def test_multi_artifact_deposit_containing_a_prereg_is_still_accepted():
    """The regression that motivated splitting rule 2.

    "preregistrations, the analysis code, anonymized data ... at <url>" lists
    the CONTENTS of one deposit. An earlier version matched the bare word
    "prereg" anywhere nearby and refused a real deposit (osf.io/wqrkv).
    """
    text = (
        '<p>All materials including survey instruments, recruitment information, '
        'preregistrations, the analysis code, anonymized data, and supplementary '
        'results have been shared and made publicly available online as an OSF '
        'project at <ext-link xlink:href="https://osf.io/wqrkv/">https://osf.io/wqrkv/</ext-link>.</p>'
    )
    assert _verdict(text) == ACCEPT


def test_deposit_before_a_reference_list_is_accepted():
    """Direction matters: <ref-list> AFTER the URL must not disqualify it.

    PNAS puts the availability sentence immediately before the bibliography, so
    a symmetric window sees "<ref" and wrongly refuses. Only citation markers
    that PRECEDE the URL mean we are inside an entry.
    """
    text = (
        '<p>Anonymized csv files have been deposited in Open Science Framework '
        '<ext-link xlink:href="https://osf.io/gyhw2/">https://osf.io/gyhw2/</ext-link>.'
        '</p></sec><ref-list><ref id="r1"><label>1</label><mixed-citation>Someone, '
        'A paper. 2020.</mixed-citation></ref></ref-list>'
    )
    assert _verdict(text) == ACCEPT


# --- refusals: somebody else's, or not data -------------------------------

def test_preregistration_link_is_refused():
    text = (
        '<p>b) 29 staff members (preregistration: '
        '<ext-link xlink:href="https://osf.io/2efnw">https://osf.io/2efnw</ext-link>), and c) ...</p>'
    )
    assert _verdict(text) == REFUSE


def test_preregistered_analysis_plan_is_refused():
    text = (
        '<p>Following our megastudy’s preregistered analysis plan '
        '<ext-link xlink:href="https://osf.io/dgpkn">https://osf.io/dgpkn</ext-link>, '
        'we restricted analyses to teachers who were assigned ...</p>'
    )
    assert _verdict(text) == REFUSE


def test_deposit_inside_a_reference_entry_is_refused():
    """A deposit cited in the bibliography belongs to the work being cited."""
    text = (
        '<ref-list><ref id="r12"><mixed-citation>Mazar N., Reply to Vogt et al., PNAS. '
        'Open Science Framework. <ext-link xlink:href="https://osf.io/jfq6k/">'
        'https://osf.io/jfq6k/</ext-link>.</mixed-citation></ref></ref-list>'
    )
    assert _verdict(text) == REFUSE


def test_psyarxiv_doi_is_not_an_osf_deposit():
    """10.31234/osf.io/8r9p7 is a preprint. The substring is a trap."""
    text = (
        '<p>Objecting to consensual experiments. PsyArXiv [Preprint], '
        '10.31234/osf.io/8r9p7 (Accessed 1 October 2023).</p>'
    )
    assert _verdict(text) == REFUSE


# --- GitHub: tool citations must never be downloaded ----------------------

def test_known_tool_org_is_refused():
    text = (
        '<p>Stan Development Team, Prior choice recommendations '
        '<ext-link xlink:href="https://github.com/stan-dev/stan/wiki">'
        'https://github.com/stan-dev/stan</ext-link>.</p>'
    )
    assert _verdict(text, repo="github") == REFUSE


def test_third_party_dataset_repo_in_a_citation_is_refused():
    text = (
        '<ref-list><ref><mixed-citation>US Census Bureau, American Community Survey. '
        '<ext-link xlink:href="https://github.com/MEDSL/2018-elections-unoffical">'
        'https://github.com/MEDSL/2018-elections-unoffical</ext-link>.'
        '</mixed-citation></ref></ref-list>'
    )
    assert _verdict(text, repo="github") == REFUSE


def test_github_in_a_code_availability_section_is_accepted():
    text = (
        '<sec><title>Code availability</title><p>All analysis code is publicly '
        'available at <ext-link xlink:href="https://github.com/ourlab/study-2024">'
        'https://github.com/ourlab/study-2024</ext-link>.</p></sec>'
    )
    assert _verdict(text, repo="github") == ACCEPT


def test_bare_github_mention_is_uncertain_not_refused():
    """Uncertain is the queue LLM adjudication drains -- not a silent drop."""
    text = '<p>We adapted the implementation from https://github.com/someone/toolkit.</p>'
    assert _verdict(text, repo="github") == UNCERTAIN


# --- shape ----------------------------------------------------------------

def test_repeated_mentions_collapse_to_the_strongest_verdict():
    """One deposit named in prose AND in the bibliography is still a deposit."""
    text = (
        '<p>Data have been deposited at '
        '<ext-link xlink:href="https://osf.io/abcde/">https://osf.io/abcde/</ext-link>.'
        '</p><ref-list><ref><mixed-citation>Author, Archive. '
        'https://osf.io/abcde/</mixed-citation></ref></ref-list>'
    )
    found = [c for c in scan_text(text) if c.repo == "osf"]
    assert len(found) == 1
    assert found[0].verdict == ACCEPT


def test_urls_are_normalized_to_something_resolvable():
    text = '<p>Data are available at https://osf.io/abcde/ for review.</p>'
    candidate = [c for c in scan_text(text) if c.repo == "osf"][0]
    assert candidate.url == "https://osf.io/abcde/"


def test_empty_text_is_not_an_error():
    assert scan_text("") == []
    assert scan_text(None) == []


# --- plain text: the same contract without the markup ----------------------
#
# _inside_citation is built entirely on JATS tags, so on a PDF or a converted
# body it returns False at every position and every reference-list URL becomes
# a live candidate. These pin the plain-text sibling that replaces it.

def test_plain_text_deposit_in_the_body_is_accepted():
    text = ("Data availability\n"
            "All data are deposited at https://osf.io/abcde/.\n")
    assert _verdict(text) == ACCEPT


def test_plain_text_url_in_the_reference_list_is_refused():
    text = ("Data availability\n"
            "All data are deposited at https://osf.io/abcde/.\n\n"
            "References\n"
            "1. Someone. A paper. https://osf.io/zzzzz/. 2020.\n")
    by_id = {c.ident: c.verdict for c in scan_text(text)}
    assert by_id["abcde"] == ACCEPT
    assert by_id["zzzzz"] == REFUSE


def test_every_reference_heading_form_is_recognised():
    """A PDF-to-text pass renders the heading half a dozen ways."""
    for heading in ("References", "REFERENCES", "## References",
                    "7. References", "Bibliography", "Literature Cited",
                    "Works Cited", "Reference List"):
        text = (f"Body prose naming nothing.\n\n{heading}\n"
                "1. Someone. https://osf.io/qqqqq/. 2020.\n")
        assert _verdict(text) == REFUSE, heading


def test_a_deposit_sentence_just_before_the_bibliography_survives():
    """The plain-text twin of test_deposit_before_a_reference_list_is_accepted.

    Truncating the document at the heading would discard exactly this prose,
    which is why the guard is position-based.
    """
    text = ("Anonymized csv files have been deposited in the Open Science "
            "Framework at https://osf.io/gyhw2/.\n\n"
            "References\n1. Someone. A paper. 2020.\n")
    assert _verdict(text) == ACCEPT


def test_an_availability_section_after_the_references_reopens_the_document():
    """Journals that put back matter after the bibliography still get read."""
    text = ("Body prose.\n\nReferences\n1. Someone. A paper. 2020.\n\n"
            "Data availability\n"
            "The dataset is deposited at https://osf.io/wwwww/.\n")
    assert _verdict(text) == ACCEPT


def test_markup_does_not_trip_the_plain_text_guard():
    """<title>References</title> is _inside_citation's job, not the heading's.

    A JATS reference-list title is not a line-anchored heading, and treating it
    as one would refuse every deposit in every XML document that has a
    bibliography -- which is all of them.
    """
    text = ('<sec><title>References</title></sec>'
            '<p>Data are deposited at '
            '<ext-link xlink:href="https://osf.io/eeeee/">'
            'https://osf.io/eeeee/</ext-link>.</p>')
    assert _verdict(text) == ACCEPT


def test_osf_preprints_prefix_is_refused_like_psyarxiv():
    """10.31219 is OSF Preprints' own prefix, and it was missing.

    Found live in a corpus reference list:
    "Bahrami MA (2023) ... OSF https://doi.org/10.31219/osf.io/k268q".
    Without this the scan downloads somebody else's preprint as a dataset.
    """
    text = ("Bahrami MA (2023) Some paper title. OSF "
            "https://doi.org/10.31219/osf.io/k268q\n")
    assert _verdict(text) == REFUSE
