"""Re-capture the recorded API responses the retrieval tests run against.

Run deliberately, never from the test suite:

    python tests/fixtures/capture.py

The tests are offline by design. These files exist so that the two failure modes
that look exactly like success -- a publisher denial stub served as 200, and a
wrong-server preprint query returning an empty collection as 200 -- are asserted
against what the APIs actually send rather than against a mock of what we assume
they send. Refreshing them is a decision; drifting into a mock is not.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EMAIL = os.getenv("EMAIL", "fetchpdf@example.org")
UA = {"User-Agent": f"fetchpdf-fixture-capture/1.0 (mailto:{EMAIL})"}

#: The UA the real client sends (fetchpdf._http.USER_AGENT). Some hosts answer
#: differently depending on it, so a fixture that exists to pin what *we* would
#: get has to ask the way we ask.
BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

TARGETS = [
    (
        "efetch_denial_stub.xml",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        "?db=pmc&id=3390974&retmode=xml",
        "NCBI efetch for a non-OA PMC record: HTTP 200, well-formed <article>, "
        "body is one sentence saying the publisher forbids full text.",
    ),
    (
        "epmc_search_preprint.json",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        + urllib.parse.urlencode(
            {"query": 'DOI:"10.1101/2020.01.30.927871"', "format": "json",
             "resultType": "core"}
        ),
        "Europe PMC PPR record: inEPMC=Y, isOpenAccess=Y, populated fullTextIdList "
        "-- and /PPR110986/fullTextXML is a 404.",
    ),
    (
        "epmc_fulltext_valid.xml",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1817752/fullTextXML",
        "A genuinely valid JATS full text with table-wrap elements and footnotes.",
    ),
    (
        "biorxiv_wrong_server.json",
        "https://api.biorxiv.org/details/biorxiv/10.1101/2020.09.09.20191205",
        "A medRxiv DOI queried against /details/biorxiv/: HTTP 200 with an empty "
        "collection. A miss that looks like a successful empty answer.",
    ),
    (
        "crossref_elsevier_links.json",
        "https://api.crossref.org/works/10.1016/j.jclinepi.2021.02.003?"
        + urllib.parse.urlencode({"mailto": EMAIL}),
        "Crossref link[]: content-type text/xml (not application/xml), "
        "intended-application text-mining, PII inline in the URL.",
    ),

    # -- --pull-supplementary -----------------------------------------------
    (
        "epmc_supplements_no_images.zip",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1817752/"
        "supplementaryFiles?includeInlineImage=no",
        "The supplementary bundle as the pass asks for it: 4 members, ~34 KB, "
        "exactly s001-s004. Paired with the fixture below to pin the difference.",
    ),
    (
        "epmc_supplements_default.zip",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1817752/"
        "supplementaryFiles",
        "The same endpoint without includeInlineImage=no: 14 members, ~132 KB, "
        "the article's whole media blob set with every figure image in it. Without "
        "that parameter the pass would write every figure as a supplementary file.",
    ),
    (
        "pmc_bin_not_a_file.html",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1817752/bin/pone.0000308.s001.doc",
        "The obvious base URL for a JATS xlink:href, and why it is not a fetch "
        "route: asked the way our client asks, it returns a 404 with 48 KB of "
        "\"Page not found\" HTML. Others have seen a 200 serving reCAPTCHA from the "
        "same path. Whichever it is on the day, it is never the file -- which is why "
        "the JATS manifest is a classifier and the bytes come from Europe PMC or S3.",
        BROWSER_UA,
    ),
    (
        "pmc_s3_metadata.json",
        "https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC1817752.1.json",
        "PMC AWS Open Data: media_urls[] with an ?md5= on every object, plus "
        "pdf_url/xml_url/text_url so the article's own renditions can be excluded. "
        "The replacement for oa.fcgi, whose FTP paths already 404 and whose files "
        "are removed entirely in August 2026.",
    ),
    (
        "pmc_s3_listing.xml",
        "https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC1817752.1/",
        "Anonymous ListBucket: <Key> plus <Size> per object, which is what lets the "
        "size cap refuse an oversized file before any of it is transferred.",
    ),
    (
        "datacite_reverse_related.json",
        "https://api.datacite.org/dois?"
        + urllib.parse.urlencode({
            "query": 'relatedIdentifiers.relatedIdentifier:"10.1186/s40168-025-02261-0"',
            "page[size]": 25,
        }),
        "The reverse related-identifier query, and the reason the filter is on "
        "types.resourceTypeGeneral rather than relationType: these datasets were "
        "deposited as IsCitedBy/IsSourceOf, so a relation whitelist drops all of them.",
    ),
    (
        "scholix_links_dataset.json",
        "https://api.scholexplorer.openaire.eu/v3/Links?"
        + urllib.parse.urlencode({
            "sourcePid": "10.1186/s40168-025-02261-0", "targetType": "dataset",
        }),
        "ScholeXplorer v3 for the same DOI as datacite_reverse_related.json, so "
        "the two link pools can be compared side by side. Scholix JSON: result[] "
        "with RelationshipType/source/target, Identifier[] arrays on each end.",
    ),
    (
        "scholix_links_software.json",
        "https://api.scholexplorer.openaire.eu/v3/Links?"
        + urllib.parse.urlencode({
            "sourcePid": "10.1038/s41586-020-2649-2", "targetType": "software",
        }),
        "A software link as OpenAIRE mints them: the NumPy paper 'cites' a "
        "'... software on GitHub' record. This is the tool-citation shape the "
        "classifier must keep out of the download path.",
    ),
    (
        "epmc_datalinks.json",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/38375968/datalinks"
        "?format=json",
        "Europe PMC datalinks with all three shapes at once: a text-mined "
        "ClinicalTrials.gov accession, DOI data citations (including arXiv DOIs "
        "wearing a dataset costume), and a BioStudies study URL that mirrors the "
        "supplementary bundle europepmc_supplements already fetches.",
    ),
    (
        "dryad_dataset.json",
        "https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.tx95x69xr",
        "Dryad v2 dataset lookup (the hbm.26595 deposit): _links['stash:version'] "
        "is the hop to the file listing. Case-insensitive on the DOI.",
    ),
    (
        "dryad_files.json",
        "https://datadryad.org/api/v2/versions/126667/files",
        "Dryad version file listing: _embedded['stash:files'] with declared size, "
        "sha-256 digest, status, and a stash:download href per file.",
    ),
    (
        "dataverse_dataset.json",
        "https://dataverse.harvard.edu/api/datasets/:persistentId/"
        "?persistentId=doi:10.7910/DVN/27221",
        "Harvard Dataverse dataset lookup: data.latestVersion.files[] with "
        "dataFile{id, filename, contentType, filesize, md5}; download is "
        "/api/access/datafile/{id}.",
    ),
]


def main():
    manifest = {}
    for target in TARGETS:
        name, url, why = target[:3]
        headers = target[3] if len(target) > 3 else UA
        print(f"capturing {name} ...", end=" ", flush=True)
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as e:
            # A non-2xx can be exactly the thing being pinned, so keep the body.
            body, status = e.read(), e.code
            print(f"{status} (kept), {len(body)} bytes")
            with open(os.path.join(HERE, name), "wb") as f:
                f.write(body)
            manifest[name] = {"url": url, "http_status": status,
                              "bytes": len(body), "why": why}
            continue
        except Exception as e:
            print(f"FAILED ({e})")
            continue
        with open(os.path.join(HERE, name), "wb") as f:
            f.write(body)
        manifest[name] = {"url": url, "http_status": status,
                          "bytes": len(body), "why": why}
        print(f"{status}, {len(body)} bytes")

    with open(os.path.join(HERE, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nwrote MANIFEST.json ({len(manifest)} fixtures)")


if __name__ == "__main__":
    main()
