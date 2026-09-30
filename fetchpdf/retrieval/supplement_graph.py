"""Reaching supplements through the citation graph rather than the publisher.

Both routes here run *backwards*, and that is the whole trick.

DataCite: forward traversal from an article DOI almost always fails, because the
article is registered with Crossref and has no DataCite record to traverse from.
The reverse query -- "which DataCite records name this DOI as related?" -- is one
request and works. Verified on 10.1186/s40168-025-02261-0: three hits, a Dryad
dataset and two Zenodo software records.

A deposit must affirmatively identify the article as its own source, supplement,
or another non-citation relation. Resource type alone does not establish ownership:
a dataset may carry the bibliography of a different paper. Citation-only and
unresolved records stay out of the supplementary files. Replication-title metadata
can separately establish ownership when an explicit relation is absent.

Crossref: article-level `relation` is empty at most publishers (is-supplemented-by
covers 99,154 works out of 185 million, about 0.05%). Component DOIs are a
different story -- 9.3 million works -- and the reverse filter finds them:
relation.object:{doi} returns the article's own components, of which the s001-style
ones are its supplementary files.

ScholeXplorer and Europe PMC datalinks run *forward* -- both index links FROM the
article, so sourcePid={doi} works where the DataCite forward traversal did not.
They earn their place beside the DataCite reverse query because their link pools
barely overlap it: ScholeXplorer folds in Crossref, DataCite AND OpenAIRE's
text-mining of ~14M PDFs, and Europe PMC adds accession-number mining (trial
registrations, GEO, PDB...) that no DOI-registry query can see. Measured on the
forensic corpora (2026-08): ~30% of a recent biomedical batch had ScholeXplorer
links -- mostly tool citations, with the occasional Dryad/figshare deposit that
is exactly the point -- and Europe PMC found links on most of that batch. Both
found nothing on an older clinical-trial corpus, which is a fact worth a sidecar
entry rather than a silence (see linked_artifacts.py).
"""

import os
import re
from typing import List, Optional

from .linked_artifacts import (
    CLASS_OWNED,
    CLASS_REGISTRY,
    CLASS_RELATED,
    CLASS_TOOL_CITATION,
    record_link,
    record_query,
)
from .supplement_index import (
    ROLE_SUPPLEMENT,
    SupplementFile,
    classify_role,
    jats_sets,
)

DATACITE_REVERSE = "https://api.datacite.org/dois"
CROSSREF_COMPONENTS = "https://api.crossref.org/works"
SCHOLEX_LINKS = "https://api.scholexplorer.openaire.eu/v3/Links"
EPMC_DATALINKS = "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/{pmid}/datalinks"

#: Resource types that are somebody's deposited data rather than somebody's paper.
KEEP_RESOURCE_TYPES = {
    "dataset", "software", "collection", "audiovisual", "image", "model",
    "text", "other", "workflow", "physicalobject",
}

#: Relations that mean "a different work referring to this one". A citing paper is
#: not a supplement, however generously the type field is filled in.
DROP_RELATIONS = {"cites", "references", "isreferencedby", "iscitedby", "isreviewof"}

#: Resource types that are the article, a version of it, or another paper.
DROP_RESOURCE_TYPES = {"preprint", "journalarticle", "article", "book", "bookchapter",
                       "conferencepaper", "peerreview", "publication"}

#: Component DOI suffixes that are a figure or a table, not a supplementary file.
_FIGURE_COMPONENT_RE = re.compile(r"\.(g|t)\d+$", re.IGNORECASE)

_MAX_RELATED = 25
_MAX_COMPONENTS = 100


# -- E4: DataCite reverse related-identifier query --------------------------


def enumerate_datacite_related(ids, ctx) -> List[SupplementFile]:
    """Data DOIs that name this article, resolved into files where we can.

    A hit here is a whole deposit, not a file, so the useful output is a set of
    repository DOIs handed to the repository enumerators. Everything inside a data
    DOI reached this way is supplementary by construction -- nobody deposits the
    paper as its own dataset -- so the article-vs-supplement heuristics are not
    consulted for these.
    """
    if not ids.doi:
        return []

    response = ctx.http.get(
        DATACITE_REVERSE,
        params={
            "query": f'relatedIdentifiers.relatedIdentifier:"{ids.doi}"',
            "page[size]": _MAX_RELATED,
        },
        timeout=30,
        polite=False,
    )
    if not response.ok:
        return []
    try:
        records = (response.json() or {}).get("data") or []
    except ValueError:
        return []

    files: List[SupplementFile] = []
    for record in records:
        related_doi = _accept_related(record, ids.doi, ctx)
        if not related_doi:
            continue
        files.extend(_files_in_repository(related_doi, ids, ctx))
        if len(files) >= _MAX_RELATED:
            break
    return files


def _canonical_relation_doi(value) -> str:
    from urllib.parse import unquote
    value = unquote(str(value or "").strip()).lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return re.sub(r"^doi:\s*", "", value)


def _accept_related(record, article_doi, ctx) -> Optional[str]:
    """The related DOI, if this record is data about our article."""
    if not isinstance(record, dict):
        return None
    attributes = record.get("attributes") or {}
    related_doi = (attributes.get("doi") or "").strip().lower()
    if not related_doi or related_doi == (article_doi or "").strip().lower():
        return None

    types = attributes.get("types") or {}
    general = str(types.get("resourceTypeGeneral") or "").strip().lower()
    specific = re.sub(r"[^a-z]", "", str(types.get("resourceType") or "").lower())
    if general in DROP_RESOURCE_TYPES or specific in DROP_RESOURCE_TYPES:
        return None
    if general and general not in KEEP_RESOURCE_TYPES:
        return None

    # Search hits and a dataset type are not evidence of ownership. In particular,
    # figshare supplements often inherit another paper's entire bibliography.
    needle = _canonical_relation_doi(article_doi)
    owns_article = any(
        isinstance(relation, dict)
        and _canonical_relation_doi(relation.get("relatedIdentifier")) == needle
        and bool(str(relation.get("relationType") or "").strip())
        and str(relation.get("relationType") or "").strip().lower() not in DROP_RELATIONS
        for relation in attributes.get("relatedIdentifiers") or []
    ) if needle else False
    if not owns_article and not _replication_title_matches(attributes, article_doi, ctx):
        return None
    ctx.log(f"    DataCite: {related_doi} ({general or 'untyped'})")
    return related_doi


def _files_in_repository(related_doi: str, ids, ctx,
                         via: str = "datacite_related") -> List[SupplementFile]:
    """Route a data DOI to the enumerator for the repository that holds it.

    Three providers fan out through here (DataCite reverse, ScholeXplorer,
    EPMC datalinks) and their link pools overlap, so a per-record seen-set
    stops the same deposit being listed -- and its files downloaded -- once per
    provider that mentions it. First mention wins the provider tag.
    """
    from .identifiers import IdentifierSet
    from .supplement_index import (
        enumerate_dataverse,
        enumerate_dryad,
        enumerate_figshare,
        enumerate_osf,
        enumerate_zenodo,
    )

    routed = ctx.scratch.setdefault("_routed_deposit_dois", set())
    key = related_doi.strip().lower()
    if key in routed:
        return []
    routed.add(key)

    proxy = IdentifierSet(doi=related_doi)
    if related_doi.startswith("10.5281") or "zenodo" in related_doi:
        enumerator, provider = enumerate_zenodo, "zenodo_files"
    elif related_doi.startswith("10.6084") or "figshare" in related_doi:
        enumerator, provider = enumerate_figshare, "figshare_files"
    elif "osf.io" in related_doi or related_doi.startswith("10.17605"):
        enumerator, provider = enumerate_osf, "osf_files"
    elif related_doi.startswith("10.5061") or "dryad" in related_doi:
        enumerator, provider = enumerate_dryad, "dryad_files"
    elif related_doi.startswith("10.7910/dvn"):
        enumerator, provider = enumerate_dataverse, "dataverse_files"
    else:
        # The long tail (institutional repos, subject databases) has no
        # enumerator; the link still lands in the linked-artifacts sidecar.
        ctx.log(f"    {via}: no enumerator for {related_doi}")
        return []

    files = enumerator(proxy, ctx) or []
    for entry in files:
        entry.provider = f"{via}:{provider}"
        entry.origin_doi = related_doi
        # Reached through a data DOI, so it is data. Nothing here is the paper.
        entry.role = ROLE_SUPPLEMENT
    return files


# -- E9: Crossref component DOIs --------------------------------------------


def enumerate_crossref_components(ids, ctx) -> List[SupplementFile]:
    """The article's own component DOIs, minus its figures and tables.

    Largely redundant with the PMC routes for anything in PubMed Central, and it
    earns its place on the publishers that register components but never reach
    PMC.
    """
    if not ids.doi:
        return []

    response = ctx.http.get(
        CROSSREF_COMPONENTS,
        params={"filter": f"relation.object:{ids.doi}", "rows": _MAX_COMPONENTS},
        timeout=30,
    )
    if not response.ok:
        return []
    try:
        items = ((response.json() or {}).get("message") or {}).get("items") or []
    except ValueError:
        return []

    supplements, figures = jats_sets(ctx)
    files = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "component":
            continue
        component_doi = str(item.get("DOI") or "")
        if _FIGURE_COMPONENT_RE.search(component_doi):
            continue
        url = (((item.get("resource") or {}).get("primary") or {}).get("URL"))
        if not url:
            continue
        name = component_doi.rsplit("/", 1)[-1]
        role = classify_role(name, jats_supplements=supplements, jats_figures=figures)
        files.append(SupplementFile(
            name=name,
            url=url,
            provider="crossref_components",
            listing_index=index,
            role=ROLE_SUPPLEMENT if role == "unknown" else role,
            label=" ".join(item.get("title") or [])[:200],
            origin_doi=ids.doi,
            extra={"component_doi": component_doi},
        ))
    return files


# -- E16: OpenAIRE ScholeXplorer (Scholix links) ------------------------------

#: OpenAIRE mints a software record for every package a paper cites, titled
#: "<name> software on GitHub". Those are somebody's tool, not this paper's
#: code -- on the biomedical corpus they were the *majority* of software links
#: (xgboost, ggthemes, table1...) -- so they are recorded but never routed.
_TOOL_CITATION_RE = re.compile(r"(?i)\bsoftware on git(hub|lab)$")

#: Relation subtypes that positively mean "deposited as this paper's material".
_OWNED_RELATIONS = {"issupplementto", "issupplementedby", "haspart", "ispartof"}

#: DOI prefixes that are papers wearing a dataset costume. EPMC's text-mined
#: "Data Citations" happily include arXiv DOIs.
#: DOIs that name a PAPER, never a data deposit. Routing one downloads someone
#: else's article and files it under this record -- which is exactly what
#: happened: 10.31234/osf.io/8r9p7 (a PsyArXiv preprint) was downloaded as a
#: "dataset" for 10.1073/pnas.2316670121.
#:
#: The OSF preprint servers are the trap here, because their DOIs embed
#: "osf.io" and so look like OSF deposits to every downstream check.
_PAPER_DOI_PREFIXES = (
    "10.48550",     # arXiv
    "10.31234",     # PsyArXiv
    "10.31235",     # SocArXiv
    "10.31219",     # OSF Preprints
    "10.31730",     # AfricArXiv
    "10.26434",     # ChemRxiv
    "10.1101",      # bioRxiv / medRxiv
    "10.20944",     # Preprints.org
    "10.21203",     # Research Square
    "10.2139",      # SSRN
)


#: Dataverse's convention for a paper's own deposit. Harvard Dataverse fills
#: in NO relatedIdentifiers at all, so for it the title is the declaration.
_REPLICATION_TITLE_RE = re.compile(r"(?i)^(?:replication\s+)?(data|code|materials?)\s+(?:for|from):?\s*(.+)")


def _deposit_names_article(repo_doi: str, article_doi: str, ctx) -> bool:
    """Whether the deposit's own metadata declares it belongs to this article.

    The ownership gate for text-mined and citation-graph links. A "cites" or
    "References" link cannot distinguish the paper's own deposit from a
    dataset the paper merely cites -- verified both ways on real links: a
    Dryad deposit Scholix tied to one article was IsCitedBy a *different*
    article (a cited work, not the paper's data), while two Dataverse
    deposits that genuinely were their papers' data carried no
    relatedIdentifiers at all.

    Two affirmative routes, both from the deposit's own DataCite record:
      1. a relatedIdentifier naming the article DOI (any non-citation
         relation -- see the module docstring for why relationType is not
         the filter), or
      2. a "Replication Data for: <title>" deposit title whose remainder
         matches the article's Crossref title (the Dataverse convention).
    Everything unconfirmed stays a sidecar link for the operator. Failing
    closed here is the difference between a corpus of each paper's own data
    and a corpus polluted with its bibliography.
    """
    if not repo_doi or not article_doi:
        return False
    cache = ctx.scratch.setdefault("_deposit_ownership", {})
    key = repo_doi.strip().lower()
    if key in cache:
        return cache[key]

    import urllib.parse
    response = ctx.http.get(
        "https://api.datacite.org/dois/" + urllib.parse.quote(key, safe=""),
        timeout=30, polite=False,
    )
    verdict = False
    if response.ok:
        try:
            attributes = ((response.json() or {}).get("data") or {}).get("attributes") or {}
        except ValueError:
            attributes = {}
        needle = article_doi.strip().lower()
        for relation in attributes.get("relatedIdentifiers") or []:
            if not isinstance(relation, dict):
                continue
            kind = str(relation.get("relationType") or "").strip().lower()
            identifier = str(relation.get("relatedIdentifier") or "").strip().lower()
            if (needle and _canonical_relation_doi(identifier) == _canonical_relation_doi(needle)
                    and kind and kind not in DROP_RELATIONS):
                verdict = True
                break
        if not verdict:
            verdict = _replication_title_matches(attributes, article_doi, ctx)
    cache[key] = verdict
    return verdict


#: How close a "Replication Data for: <title>" remainder must sit to the
#: article's own title. Exact containment is too strict by exactly one letter:
#: a Harvard Dataverse deposit titled "...ESCAS randomised pilot study" against
#: a journal title reading "...ESCAS Randomized Pilot Study" agrees for 108
#: characters and then diverges on British vs American spelling, which sent a
#: trial's own replication data to the sidecar unrouted.
#:
#: The margin is not delicate. That pair scores 0.99; an unrelated replication
#: title scores 0.12. Anything in between is not a near-miss worth arguing over.
_TITLE_MATCH_RATIO = 0.93


def _replication_title_matches(attributes: dict, article_doi: str, ctx) -> bool:
    """Route 2: 'Replication Data for: <title>' against the article's title."""
    import difflib

    for entry in attributes.get("titles") or []:
        title = str((entry or {}).get("title") or "")
        matched = _REPLICATION_TITLE_RE.match(title.strip())
        if not matched:
            continue
        claimed = _squash(matched.group(2))
        actual = _squash(_article_title(article_doi, ctx))
        if not claimed or not actual:
            continue
        if claimed in actual or actual in claimed:
            return True
        # Containment first (cheap, and the common case); near-identity only
        # for the spelling-variant tail it would otherwise drop.
        if difflib.SequenceMatcher(None, claimed, actual).ratio() >= _TITLE_MATCH_RATIO:
            return True
    return False


def _article_title(article_doi: str, ctx) -> str:
    cache = ctx.scratch.setdefault("_article_titles", {})
    key = article_doi.strip().lower()
    if key in cache:
        return cache[key]
    title = ""
    response = ctx.http.get(
        "https://api.crossref.org/works/" + key, timeout=30,
    )
    if response.ok:
        try:
            titles = ((response.json() or {}).get("message") or {}).get("title") or []
            title = str(titles[0]) if titles else ""
        except ValueError:
            pass
    cache[key] = title
    return title


def _squash(text: str) -> str:
    """Lowercased alphanumerics only, so punctuation and casing differences
    between a deposit title and a journal title cannot break the match."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def enumerate_scholix_related(ids, ctx) -> List[SupplementFile]:
    """Datasets and software ScholeXplorer links to this article, as files
    where a repository enumerator exists, and as sidecar links always.

    Free, unauthenticated, first page only: a paper with more than 100
    dataset links is not a paper whose links we can usefully triage.
    """
    if not ids.doi:
        return []

    files: List[SupplementFile] = []
    for target_type in ("dataset", "software"):
        response = ctx.http.get(
            SCHOLEX_LINKS,
            params={"sourcePid": ids.doi, "targetType": target_type},
            timeout=30,
            polite=False,
        )
        if not response.ok:
            record_query(ctx, "scholexplorer", response.request_url,
                         "error", f"http {response.status}")
            continue
        try:
            payload = response.json() or {}
        except ValueError:
            record_query(ctx, "scholexplorer", response.request_url,
                         "error", "not JSON")
            continue

        links = payload.get("result") or []
        record_query(ctx, "scholexplorer", response.request_url, "ok",
                     f"{payload.get('totalLinks', len(links))} {target_type} link(s)")

        for link in links:
            if not isinstance(link, dict):
                continue
            target = link.get("target") or {}
            target_doi = _scholix_doi(target)
            title = str(target.get("Title") or "")
            relation = _scholix_relation(link)
            classified = _classify_scholix(target_doi, title, relation)

            # Only the paper's own material is downloaded. "cites" cannot
            # distinguish the paper's deposit from a dataset the paper merely
            # cites, so a citation-grade link earns a download only when the
            # deposit's DataCite record names this article.
            if classified == CLASS_RELATED and _has_repo_enumerator(target_doi) \
                    and _deposit_names_article(target_doi, ids.doi, ctx):
                classified = CLASS_OWNED

            routed_files: List[SupplementFile] = []
            if _should_route(classified, ctx, target_doi, ids) and target_doi \
                    and not target_doi.startswith(_PAPER_DOI_PREFIXES):
                routed_files = _files_in_repository(
                    target_doi, ids, ctx, via="scholix_related")
                files.extend(routed_files)

            record_link(
                ctx, service="scholexplorer", target_pid=target_doi,
                target_type=target_type, relation=relation,
                classified=classified, title=title,
                publisher=_scholix_publisher(target),
                provider_name=_scholix_provider(link),
                harvest_date=str(link.get("HarvestDate") or ""),
                routed=bool(routed_files),
            )
    return files


def _scholix_doi(target: dict) -> Optional[str]:
    for identifier in target.get("Identifier") or []:
        if isinstance(identifier, dict) and \
                str(identifier.get("IDScheme") or "").lower() == "doi":
            doi = str(identifier.get("ID") or "").strip().lower()
            if doi:
                return doi
    return None


def _scholix_relation(link: dict) -> str:
    relationship = link.get("RelationshipType") or {}
    return str(relationship.get("SubType") or relationship.get("Name") or "").strip()


def _scholix_publisher(target: dict) -> str:
    publishers = target.get("Publisher")
    if isinstance(publishers, list) and publishers:
        return str((publishers[0] or {}).get("name") or "")
    if isinstance(publishers, dict):
        return str(publishers.get("name") or "")
    return ""


def _scholix_provider(link: dict) -> str:
    providers = link.get("LinkProvider")
    if isinstance(providers, list) and providers:
        return str((providers[0] or {}).get("name") or "")
    return ""


def _classify_scholix(target_doi: Optional[str], title: str, relation: str) -> str:
    if _TOOL_CITATION_RE.search(title or ""):
        return CLASS_TOOL_CITATION
    if target_doi and target_doi.startswith(_PAPER_DOI_PREFIXES):
        return CLASS_RELATED
    if relation.strip().lower() in _OWNED_RELATIONS:
        return CLASS_OWNED
    return CLASS_RELATED


def enumerate_fulltext_scan(ids, ctx) -> List[SupplementFile]:
    """E18: deposits the paper names in its own prose.

    Only runs under --download-data-artifacts, and only reads files already on
    disk, so it costs no network requests for discovery. Link indexes missed
    every one of the 14 OSF deposits found this way in a real corpus.

    Reads the record's PDF when there is no markup beside it. That was a
    silent coverage hole rather than a limitation: measured 2026-08-22, 606 of
    1,491 corpus records are PDF-only, and every one of them was reported as
    "scanned, nothing found" without ever being opened.

    Precision, not recall, is the risk here -- half the repository URLs in a
    paper belong to somebody else -- so fulltext_scan applies its rules first
    and only accepted candidates are routed. Refused and uncertain ones are
    recorded in the sidecar unrouted, where they can be audited.
    """
    if not getattr(ctx, "download_data_artifacts", False):
        return []

    from .fulltext_scan import ACCEPT, scan_record
    from .linked_artifacts import record_link, record_query

    stem = os.path.splitext(ctx.save_path)[0] if ctx.save_path else ""
    if not stem:
        return []
    try:
        candidates = scan_record(stem, log=ctx.log)
    except Exception as error:                      # never break a run over a scan
        ctx.log(f"    fulltext_scan: {error}")
        return []

    record_query(ctx, service="fulltext_scan", url="(local full text)",
                 status="ok", detail=f"{len(candidates)} repository mention(s)")
    if not candidates:
        return []

    if getattr(ctx, "llm_adjudicate_artifacts", False):
        from .llm_adjudicate import adjudicate
        adjudicate(candidates,
                   model=getattr(ctx, "llm_model", None) or "haiku",
                   log=ctx.log, enabled=True)

    files: List[SupplementFile] = []
    for candidate in candidates:
        routed: List[SupplementFile] = []
        if candidate.verdict == ACCEPT and candidate.repo != "github":
            # GitHub has no DOI, so it cannot go through _files_in_repository;
            # it is handled by its own enumerator below.
            routed = _files_in_repository(
                candidate.ident if "/" in candidate.ident else candidate.url,
                ids, ctx, via="fulltext_scan")
            files.extend(routed)
        elif candidate.verdict == ACCEPT and candidate.repo == "github":
            routed = _github_tarball(candidate.ident, ctx)
            files.extend(routed)

        record_link(
            ctx, service="fulltext_scan", target_pid=candidate.url,
            target_type="software" if candidate.repo == "github" else "dataset",
            relation="NamedInFullText",
            classified=CLASS_OWNED if candidate.verdict == ACCEPT else CLASS_RELATED,
            title=candidate.reason[:300], publisher=candidate.repo,
            provider_name=candidate.adjudicated_by or "rules",
            routed=bool(routed),
        )
    return files


#: A branch tarball is what a replication check needs; git history is not.
_GITHUB_TARBALL = "https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
_GITHUB_API = "https://api.github.com/repos/{repo}"


def _github_tarball(repo: str, ctx) -> List[SupplementFile]:
    """One archive for a GitHub repository, at its default branch."""
    branch = "main"
    try:
        response = ctx.http.get(_GITHUB_API.format(repo=repo), timeout=20,
                                polite=False)
        if response.ok:
            branch = ((response.json() or {}).get("default_branch")) or "main"
    except Exception:
        pass          # main is the right guess when the API is unavailable
    return [SupplementFile(
        name=f"{repo.replace('/', '-')}-{branch}.tar.gz",
        url=_GITHUB_TARBALL.format(repo=repo, branch=branch),
        provider="fulltext_scan:github",
        # NOT is_archive. That flag means "expand rather than keep", and the
        # expander is `_expand_zip` -- zipfile, on a gzip stream. Every GitHub
        # repository this provider has ever found was refused
        # `not-an-archive` because of it: zero `fulltext_scan:github` files
        # exist across 1,934 corpus manifests, and the tarball URL itself is
        # fine (verified: HTTP 200, 6.5 MB of valid gzip).
        #
        # Keeping it whole is also the right policy on its own terms. A
        # repository is a replication package, and this module already refuses
        # to explode those by default -- unpacking one produced 199 files from
        # a vendored Stata library in a single record.
        role=ROLE_SUPPLEMENT,
        extra={"github_repo": repo, "branch": branch},
    )]


def _should_route(classified: str, ctx, target_doi=None, ids=None) -> bool:
    """Whether a link of this class is downloaded, not merely recorded.

    Default: only the paper's own material. That is the conservative reading
    and the right one when the caller asked for supplementary files.

    Under --download-data-artifacts the gate widens to `related` as well,
    because on real corpora that is where the research data actually is: in a
    44-sidecar sample every `owned` link was a BioStudies mirror of
    supplementary files already fetched, while the Zenodo record (8 files,
    14 MB CSV) and the Dataverse replication packages were all `related`.

    `registry` never routes -- ClinicalTrials.gov and friends have nothing to
    download. `tool_citation` never routes either: those are libraries the
    authors used, and the Scholix ones carry no target_pid at all, so there is
    nothing to fetch even if we wanted it. The paper's OWN code is found by
    reading the paper (fulltext_scan), not by trusting a citation index.
    """
    if classified == CLASS_OWNED:
        return True
    if not getattr(ctx, "download_data_artifacts", False):
        return False
    if classified != CLASS_RELATED:
        return False

    # `related` means an index asserted a connection, not that the deposit is
    # this paper's. Downloading on that assertion alone fetched "Tying Odysseus
    # to the Mast" (Ashraf 2006) and filed it under a 2025 savings-reminder
    # megastudy. So the deposit has to name this article in its own DataCite
    # record before we take it -- the same oracle already used to upgrade
    # `related` to `owned`, simply consulted for the routing decision too.
    if getattr(ctx, "download_related_unverified", False):
        return True
    if not target_doi or not getattr(ids, "doi", None):
        return False
    # Requiring the deposit to NAME this article is too strict for real
    # metadata: Zenodo's MegaOath dataset declares IsSupplementTo an OSF
    # project rather than the paper, and Harvard Dataverse fills in no
    # relatedIdentifiers at all. Both are genuinely the article's data, and
    # both would be refused.
    #
    # What actually separates the false positive is the opposite signal: the
    # Odysseus deposit declares IsSupplementTo 10.1162/qjec.2006.121.2.635 --
    # it says out loud that it belongs to a DIFFERENT paper. So accept unless
    # the deposit claims another article, rather than demanding it claim ours.
    if _deposit_names_article(target_doi, ids.doi, ctx):
        return True
    return not _deposit_claims_another_article(target_doi, ids.doi, ctx)


#: Relations by which a deposit declares which article it belongs to. If one of
#: these points at a DIFFERENT paper, the deposit is that paper's, not ours.
_OWNERSHIP_RELATIONS = {"issupplementto", "ispartof", "isdescribedby",
                        "iscitedby", "isreferencedby"}


def _deposit_claims_another_article(target_doi: str, article_doi: str, ctx) -> bool:
    """True when the deposit says it belongs to some other paper.

    The precise signal that separates a wrongly-routed deposit from a correctly
    routed one on real data. "Tying Odysseus to the Mast" declares
    IsSupplementTo 10.1162/qjec.2006.121.2.635; it was being filed under a 2025
    savings-reminder megastudy. Deposits with no relatedIdentifiers at all
    (Harvard Dataverse fills in none) are NOT caught by this, which is correct
    -- absence of a claim is not a claim to the contrary.

    Fails OPEN on any lookup error: a DataCite outage should not silently start
    discarding real data.
    """
    cache = ctx.scratch.setdefault("_deposit_other_article", {})
    key = (target_doi or "").strip().lower()
    if key in cache:
        return cache[key]

    verdict = False
    try:
        response = ctx.http.get(f"https://api.datacite.org/dois/{key}",
                                timeout=25, polite=False)
        if response.ok:
            attributes = ((response.json() or {}).get("data") or {}).get("attributes") or {}
            mine = (article_doi or "").strip().lower()
            for relation in attributes.get("relatedIdentifiers") or []:
                if not isinstance(relation, dict):
                    continue
                kind = str(relation.get("relationType") or "").strip().lower()
                if kind not in _OWNERSHIP_RELATIONS:
                    continue
                other = str(relation.get("relatedIdentifier") or "").strip().lower()
                # Only a DOI naming a different *article* counts. A deposit
                # pointing at an OSF project or its own earlier version is not
                # claiming another paper.
                if not other.startswith("10.") or mine in other:
                    continue
                if other.startswith(_DEPOSIT_DOI_PREFIXES):
                    continue
                verdict = True
                ctx.log(f"    {key}: declares {kind} {other} -- not this article")
                break
    except Exception:
        verdict = False
    cache[key] = verdict
    return verdict


#: Prefixes that are DEPOSITS, not articles. A deposit relating to one of these
#: is relating to data or a project page, which says nothing about which paper
#: owns it.
_DEPOSIT_DOI_PREFIXES = ("10.5281", "10.6084", "10.17605", "10.5061", "10.7910",
                         "10.3886", "10.25384", "10.24433")


def _has_repo_enumerator(target_doi: Optional[str]) -> bool:
    """Whether a DOI would route somewhere in _files_in_repository.

    Gates the ownership lookup: a DOI that cannot be downloaded anyway is not
    worth a DataCite request to classify more finely.
    """
    if not target_doi:
        return False
    doi = target_doi.strip().lower()
    return (doi.startswith(("10.5281", "10.6084", "10.17605", "10.5061", "10.7910/dvn"))
            or "zenodo" in doi or "figshare" in doi or "osf.io" in doi or "dryad" in doi)


# -- E17: Europe PMC datalinks ------------------------------------------------


def enumerate_epmc_datalinks(ids, ctx) -> List[SupplementFile]:
    """Europe PMC's data links for this article: text-mined accessions,
    data citations, and BioStudies deposits.

    Registry accessions (ClinicalTrials.gov and friends) are sidecar-only by
    nature. BioStudies links are sidecar-only by choice: the study page mirrors
    the EPMC supplementary bundle that europepmc_supplements already fetches,
    so downloading it again would double every file. Data-citation DOIs route
    to the repository enumerators like every other deposit.
    """
    if not ids.pmid:
        return []

    response = ctx.http.get(
        EPMC_DATALINKS.format(pmid=ids.pmid),
        params={"format": "json"},
        timeout=30,
    )
    if not response.ok:
        record_query(ctx, "epmc_datalinks", response.request_url,
                     "error", f"http {response.status}")
        return []
    try:
        payload = response.json() or {}
    except ValueError:
        record_query(ctx, "epmc_datalinks", response.request_url,
                     "error", "not JSON")
        return []

    categories = ((payload.get("dataLinkList") or {}).get("Category")) or []
    record_query(ctx, "epmc_datalinks", response.request_url, "ok",
                 f"{payload.get('hitCount', 0)} link(s), "
                 f"{len(categories)} categorie(s)")

    files: List[SupplementFile] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_name = str(category.get("Name") or "")
        # Altmetric is attention, not data.
        if category_name.lower().startswith("altmetric"):
            continue
        for section in category.get("Section") or []:
            for link in ((section or {}).get("Linklist") or {}).get("Link") or []:
                if not isinstance(link, dict):
                    continue
                files.extend(_take_epmc_link(link, category_name, ids, ctx))
    return files


def _take_epmc_link(link: dict, category_name: str, ids, ctx) -> List[SupplementFile]:
    target = link.get("Target") or {}
    identifier = target.get("Identifier") or {}
    scheme = str(identifier.get("IDScheme") or "").strip()
    pid = str(identifier.get("ID") or "").strip()
    title = str(target.get("Title") or "")
    relation = str(((link.get("RelationshipType") or {}).get("Name")) or "")

    is_doi = scheme.lower() == "doi"
    target_doi = pid.lower() if is_doi else None
    if scheme.lower() == "clinicaltrials.gov" or "clinical trials" in category_name.lower():
        classified = CLASS_REGISTRY
    elif "biostudies" in category_name.lower():
        # The article's own deposit, but see the enumerator docstring.
        classified = CLASS_OWNED
    else:
        # EPMC "Data Citations" are text-mined from the whole paper -- the
        # data-availability statement AND the reference list, with nothing to
        # tell them apart. On a real corpus the majority were cited journal
        # articles wearing a dataset costume, so every one is a citation until
        # the deposit's own DataCite record names this article.
        classified = CLASS_RELATED
        if _has_repo_enumerator(target_doi) and \
                _deposit_names_article(target_doi, ids.doi, ctx):
            classified = CLASS_OWNED

    routed_files: List[SupplementFile] = []
    if target_doi and _should_route(classified, ctx, target_doi, ids) and \
            not target_doi.startswith(_PAPER_DOI_PREFIXES) and \
            "biostudies" not in category_name.lower():
        routed_files = _files_in_repository(target_doi, ids, ctx, via="epmc_datalinks")

    record_link(
        ctx, service="epmc_datalinks",
        target_pid=pid or None,
        target_type=str(((target.get("Type") or {}).get("Name")) or category_name),
        relation=relation, classified=classified, title=title,
        publisher=str(((target.get("Publisher") or {}).get("Name")) or ""),
        provider_name=str(((link.get("LinkProvider") or {}).get("Name")) or ""),
        harvest_date=str(link.get("PublicationDate") or ""),
        routed=bool(routed_files),
    )
    return routed_files
