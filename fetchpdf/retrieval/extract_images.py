"""Embedded-image extraction: the authors' original bitstreams, with placements.

Why this exists at all: downstream byte-identity work (figure-duplication
forensics in particular) needs the image bytes AS THE AUTHORS EMBEDDED THEM.
Anything that renders and re-encodes -- screenshots, pdf2image, docling crops --
destroys exactly the evidence that matters. PyMuPDF's ``extract_image`` is the
one surveyed route that hands back the embedded stream with its xref identity,
so this module is a thin dump of that: one file per xref, every placement
recorded, hashes of both the extracted bytes and the raw xref stream.

Two hashes because ``extract_image`` is NOT always a passthrough. It returns
the original buffer only for recognized compressed formats (JPEG, JPX, and the
rare embedded BMP/GIF/PNG/TIFF); Flate/CCITT/JBIG2 streams are repacked to PNG,
and -- the trap -- CMYK JPEG is re-encoded LOSSILY at quality 95 with ``ext``
still reporting "jpeg". So ``ext`` cannot prove originality. Only
``sha256(extracted) == sha256(doc.xref_stream_raw(xref))`` can, which is what
the ``passthrough`` flag records; the raw-stream hash is the byte-identity
anchor either way.

Deliberately NOT here: perceptual hashing, panel segmentation, figure/caption
joins, or any judgement about what an image IS. This module reports what is
embedded; deciding what it means belongs to the consumer.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from ._util import iso, unlink
from .http import apply_default_mode

IMAGES_DIR_SUFFIX = "_images"
MANIFEST_SUFFIX = "_images.json"
_SCHEMA_VERSION = 1
_MISSING_DEP_MSG = ("PyMuPDF not installed; install fetchpdf[images] "
                    "to extract embedded images")

#: Files this module owns inside {stem}_images/, and therefore the only files
#: the stale-cleanup pass may ever touch there.
_OWNED_FILE_RE = re.compile(r"^xref\d{6}\.")


def _pymupdf():
    """PyMuPDF if installed, else None. Optional extra: fetchpdf[images]."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz
            return fitz
        except ImportError:
            return None


# -- paths ------------------------------------------------------------------


def _stem(pdf_path: str) -> str:
    from .engine import _stem_for
    return _stem_for(pdf_path)


def images_dir_for(pdf_path: str) -> str:
    """`{stem}_images/`, beside the PDF."""
    return _stem(pdf_path) + IMAGES_DIR_SUFFIX


def manifest_path_for(pdf_path: str) -> str:
    """`{stem}_images.json`, beside the PDF."""
    return _stem(pdf_path) + MANIFEST_SUFFIX


# -- data model -------------------------------------------------------------


@dataclass
class Placement:
    page: int                                   # 0-based
    rect: Tuple[float, float, float, float]
    matrix: Tuple[float, float, float, float, float, float]


@dataclass
class EmbeddedImage:
    xref: int
    ext: str                    # extract_image's ext, verbatim
    width: int
    height: int
    bpc: int
    colorspace_n: int
    colorspace_name: str
    filter: str
    smask_xref: int             # 0 when none
    role: str                   # "image" | "smask"
    smask_for: List[int]        # xrefs this smask belongs to (role=="smask")
    size: int                   # len(extracted bytes)
    sha256: str                 # of the extracted bytes
    raw_stream_size: int
    raw_stream_sha256: str      # of doc.xref_stream_raw(xref) -- byte identity
    passthrough: bool           # extracted bytes ARE the raw stream
    notes: List[str] = field(default_factory=list)
    placements: List[Placement] = field(default_factory=list)
    image_bytes: Optional[bytes] = None   # nulled after writing, to bound memory
    file: str = ""              # filename inside {stem}_images/, set by the writer


@dataclass
class ImagesResult:
    status: str                 # ok | encrypted | corrupt | error | skipped | unavailable
    detail: str = ""
    pdf_path: str = ""
    page_count: int = 0
    images: List[EmbeddedImage] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)
    images_dir: str = ""
    manifest_path: str = ""
    written: int = 0


# -- enumeration core (no filesystem writes) --------------------------------


def _filter_of(doc, xref: int) -> str:
    """The /Filter entry as a plain string, or ""."""
    try:
        kind, value = doc.xref_get_key(xref, "Filter")
    except Exception:
        return ""
    if kind in ("null", "none") or not value or value == "null":
        return ""
    return value.strip().strip("/")


def _record_for(doc, xref: int, filt: str, role: str,
                smask_xref: int, smask_for: List[int],
                placements: List[Placement]) -> EmbeddedImage:
    extracted = doc.extract_image(xref) or {}
    data = extracted.get("image") or b""
    raw = doc.xref_stream_raw(xref) or b""
    sha = hashlib.sha256(data).hexdigest()
    raw_sha = hashlib.sha256(raw).hexdigest()
    passthrough = bool(data) and sha == raw_sha
    ext = extracted.get("ext") or "bin"
    colorspace_n = int(extracted.get("colorspace") or 0)

    notes = []
    # A PNG whose bytes differ from the raw stream means fitz converted a
    # Flate/CCITT/JBIG2/unknown stream. Judged by bytes, not by /Filter,
    # because a genuinely embedded PNG buffer passes through and earns no note.
    if ext == "png" and not passthrough:
        notes.append("repacked-to-png")
    # The lossy trap: CMYK JPEG comes back re-encoded at q95, ext still "jpeg".
    if ext == "jpeg" and colorspace_n == 4 and not passthrough:
        notes.append("cmyk-jpeg-reencoded-lossy")
    if role == "image" and not placements:
        notes.append("never-drawn")

    return EmbeddedImage(
        xref=xref,
        ext=ext,
        width=int(extracted.get("width") or 0),
        height=int(extracted.get("height") or 0),
        bpc=int(extracted.get("bpc") or 0),
        colorspace_n=colorspace_n,
        colorspace_name=str(extracted.get("cs-name") or ""),
        filter=filt,
        smask_xref=smask_xref,
        role=role,
        smask_for=smask_for,
        size=len(data),
        sha256=sha,
        raw_stream_size=len(raw),
        raw_stream_sha256=raw_sha,
        passthrough=passthrough,
        notes=notes,
        placements=placements,
        image_bytes=data,
    )


def iter_embedded_images(doc, errors: Optional[List[dict]] = None) -> Iterator[EmbeddedImage]:
    """Every embedded image (then every smask) of an open Document, one per xref.

    The caller owns the Document: opening, password handling and closing stay
    outside so this stays importable as a pure enumeration core. A failing
    xref is reported into `errors` (when given) and skipped -- one corrupt
    stream is one lost image, never a lost document.
    """
    # Pass 1: which xrefs exist, on which pages. get_images(full=True) lists
    # an xref once PER PLACEMENT NAME, so dedup is required even within a page.
    seen: Dict[int, dict] = {}
    for page_index in range(doc.page_count):
        page = doc[page_index]
        for entry in page.get_images(full=True):
            xref, smask = int(entry[0]), int(entry[1])
            filt = str(entry[8]) if len(entry) > 8 else ""
            info = seen.setdefault(xref, {"pages": [], "smask": smask, "filter": filt})
            if page_index not in info["pages"]:
                info["pages"].append(page_index)

    # Pass 2: one record per image xref, carrying every placement.
    smask_map: Dict[int, List[int]] = {}
    for xref in sorted(seen):
        info = seen[xref]
        try:
            placements = []
            for page_index in info["pages"]:
                page = doc[page_index]
                for rect, matrix in page.get_image_rects(xref, transform=True):
                    placements.append(Placement(
                        page=page_index,
                        rect=tuple(float(v) for v in rect),
                        matrix=tuple(float(v) for v in matrix),
                    ))
            record = _record_for(doc, xref, info["filter"], "image",
                                 info["smask"], [], placements)
        except Exception as e:
            if errors is not None:
                errors.append({"xref": xref, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        if info["smask"]:
            smask_map.setdefault(info["smask"], []).append(xref)
        yield record

    # Pass 3: the smasks those images referenced. Not listed by get_images and
    # never drawn, but they carry the original alpha -- without these bytes it
    # is unrecoverable later. Never composited: compositing re-encodes.
    for smask_xref in sorted(smask_map):
        try:
            yield _record_for(doc, smask_xref, _filter_of(doc, smask_xref),
                              "smask", 0, sorted(smask_map[smask_xref]), [])
        except Exception as e:
            if errors is not None:
                errors.append({"xref": smask_xref,
                               "error": f"{type(e).__name__}: {str(e)[:120]}"})


def enumerate_pdf_images(pdf_path: str) -> ImagesResult:
    """Open a PDF and enumerate its embedded images. Never raises.

    Every non-answer is a recorded outcome ("encrypted", "corrupt", "error",
    "unavailable"), mirroring the batch discipline elsewhere in this package:
    one bad record must never take down a run.
    """
    result = ImagesResult(status="error", pdf_path=pdf_path)
    fitz = _pymupdf()
    if fitz is None:
        result.status = "unavailable"
        result.detail = _MISSING_DEP_MSG
        return result
    if not os.path.isfile(pdf_path):
        result.detail = "no such file"
        return result

    doc = None
    try:
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            result.status = "corrupt"
            result.detail = f"{type(e).__name__}: {str(e)[:120]}"
            return result

        # Empty-user-password PDFs auto-authenticate on open; only a real
        # password leaves the document encrypted after this.
        if doc.needs_pass:
            doc.authenticate("")
        if doc.is_encrypted:
            result.status = "encrypted"
            result.detail = "password-protected; no images readable"
            return result

        result.page_count = doc.page_count
        result.images = list(iter_embedded_images(doc, errors=result.errors))
        result.status = "ok"
        return result
    except Exception as e:
        result.status = "error"
        result.detail = f"{type(e).__name__}: {str(e)[:120]}"
        return result
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


# -- fetchpdf-layout writer -------------------------------------------------


def _write_bytes_atomic(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp_path = tempfile.mkstemp(prefix=".fetchpdf-img-", dir=directory)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(payload)
        apply_default_mode(temp_path)
        os.replace(temp_path, path)
    except BaseException:
        unlink(temp_path)
        raise


def _sha256_file(path: str) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _versions() -> Tuple[str, str]:
    try:
        from importlib.metadata import version
        own = version("fetchpdf")
    except Exception:
        own = "unknown"
    mod = _pymupdf()
    lib = str(getattr(mod, "__version__", "unknown")) if mod else "absent"
    return own, lib


def _manifest_is_current(manifest_path: str, source_size: int, source_sha256: str) -> bool:
    """True when the manifest on disk already describes this exact PDF.

    Keyed on the PDF's bytes, not its mtime: copies and Dropbox syncs shuffle
    mtimes freely. "encrypted"/"corrupt" manifests count as current too --
    "nothing there" is a fact worth recording once -- but "error" does not,
    because those are usually environmental and deserve a retry.
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return False
    return (
        manifest.get("schema_version") == _SCHEMA_VERSION
        and manifest.get("source_size") == source_size
        and manifest.get("source_sha256") == source_sha256
        and manifest.get("status") in ("ok", "encrypted", "corrupt")
    )


def _manifest_payload(result: ImagesResult, pdf_path: str,
                      source_size: int, source_sha256: str) -> bytes:
    own_version, lib_version = _versions()
    images = []
    n_placements = 0
    for img in result.images:
        n_placements += len(img.placements)
        images.append({
            "xref": img.xref,
            "file": img.file,
            "role": img.role,
            "ext": img.ext,
            "width": img.width,
            "height": img.height,
            "bpc": img.bpc,
            "colorspace_n": img.colorspace_n,
            "colorspace_name": img.colorspace_name,
            "filter": img.filter,
            "size_bytes": img.size,
            "sha256": img.sha256,
            "raw_stream_size": img.raw_stream_size,
            "raw_stream_sha256": img.raw_stream_sha256,
            "passthrough": img.passthrough,
            "notes": img.notes,
            "smask_xref": img.smask_xref,
            "smask_for": img.smask_for,
            "placements": [
                {"page": p.page, "rect": list(p.rect), "matrix": list(p.matrix)}
                for p in img.placements
            ],
        })
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "generator": "fetchpdf.retrieval.extract_images",
        "fetchpdf_version": own_version,
        "pymupdf_version": lib_version,
        "created_at": iso(time.time()),
        "source_pdf": os.path.basename(pdf_path),
        "source_size": source_size,
        "source_sha256": source_sha256,
        "status": result.status,
        "detail": result.detail,
        "page_count": result.page_count,
        "images_dir": os.path.basename(images_dir_for(pdf_path)),
        "n_images": len(result.images),
        "n_placements": n_placements,
        "images": images,
        "errors": result.errors,
    }
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def extract_images(pdf_path: str, overwrite: bool = False,
                   verbose: bool = False) -> ImagesResult:
    """Extract one PDF's embedded images into `{stem}_images/` + manifest.

    Idempotent: an existing manifest describing this exact PDF (by size and
    sha256) short-circuits to "skipped". The manifest is written LAST and
    atomically -- it is the commit point; a crash mid-write leaves stale
    bitstreams a re-run will replace, never a manifest describing files that
    are not there. Never raises.
    """
    try:
        return _extract_images_inner(pdf_path, overwrite=overwrite, verbose=verbose)
    except Exception as e:
        return ImagesResult(status="error", pdf_path=pdf_path,
                            detail=f"{type(e).__name__}: {str(e)[:120]}")


def _extract_images_inner(pdf_path: str, overwrite: bool, verbose: bool) -> ImagesResult:
    manifest_path = manifest_path_for(pdf_path)
    if not os.path.isfile(pdf_path):
        return ImagesResult(status="error", pdf_path=pdf_path, detail="no such file")
    source_size, source_sha256 = _sha256_file(pdf_path)

    if not overwrite and os.path.exists(manifest_path) and \
            _manifest_is_current(manifest_path, source_size, source_sha256):
        return ImagesResult(status="skipped", pdf_path=pdf_path,
                            manifest_path=manifest_path,
                            detail="manifest current for these PDF bytes")

    result = enumerate_pdf_images(pdf_path)
    if result.status == "unavailable":
        # A missing dependency is run state, not a fact about the PDF. Writing
        # a manifest here would satisfy skip-if-current forever after the user
        # installs the extra -- so write nothing at all.
        return result

    images_dir = images_dir_for(pdf_path)
    if result.images:
        try:
            os.makedirs(images_dir, exist_ok=True)
        except OSError as e:
            # No flat fallback (unlike SI, a re-run costs nothing but CPU),
            # and flat xref-named files would lose their record association.
            result.status = "error"
            result.detail = f"could not create {os.path.basename(images_dir)}: {e}"
            result.images = []

    kept = set()
    for img in result.images:
        filename = "xref{:06d}.{}".format(img.xref, img.ext)
        try:
            _write_bytes_atomic(os.path.join(images_dir, filename), img.image_bytes or b"")
        except OSError as e:
            result.errors.append({"xref": img.xref, "error": f"write failed: {e}"})
            img.image_bytes = None
            continue
        img.file = filename
        img.image_bytes = None      # keep batch memory bounded
        kept.add(filename)
        result.written += 1

    # An upgraded PDF renumbers xrefs; orphaned bitstreams beside a fresh
    # manifest would poison downstream byte-identity pooling. Scoped to the
    # filename pattern this module owns, so nothing else is ever touched.
    if os.path.isdir(images_dir):
        for name in os.listdir(images_dir):
            if _OWNED_FILE_RE.match(name) and name not in kept:
                unlink(os.path.join(images_dir, name))

    result.images_dir = images_dir if result.written else ""
    result.manifest_path = manifest_path
    _write_bytes_atomic(manifest_path,
                        _manifest_payload(result, pdf_path, source_size, source_sha256))
    if verbose:
        print(f"  images: {os.path.basename(pdf_path)} -> "
              f"{result.written} stream(s), status {result.status}")
    return result


# -- directory mode + CLI ---------------------------------------------------


def extract_directory(directory: str, overwrite: bool = False, verbose: bool = False,
                      include_supplementary: bool = False) -> dict:
    """Extract images for every record PDF under a directory. Returns a summary.

    Recursive, unlike to_markdown's flat listing, because --make-subfolder
    corpora put each record in its own folder. Supplementary-info PDFs are
    siblings in the same directories and are skipped by default: they are a
    different kind of artifact, and sweeping them in silently would double
    counts for records whose SI happens to be a PDF.
    """
    from .supplementary import INFIX as _SI_INFIX
    from .supplementary import DATA_ARTIFACT_INFIX as _DATA_INFIX

    summary = {"extracted": 0, "skipped": 0, "zero_image": 0, "encrypted": 0,
               "failed": 0, "supplementary_skipped": 0, "images": 0, "placements": 0}

    for root, dirnames, filenames in os.walk(directory):
        # Never descend into our own output or unpacked data deposits.
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.endswith(IMAGES_DIR_SUFFIX) and _DATA_INFIX not in d
        )
        for name in sorted(filenames):
            if not name.endswith(".pdf"):
                continue
            if _SI_INFIX in name and not include_supplementary:
                summary["supplementary_skipped"] += 1
                continue
            result = extract_images(os.path.join(root, name),
                                    overwrite=overwrite, verbose=verbose)
            if result.status == "skipped":
                summary["skipped"] += 1
            elif result.status == "ok" and not result.images:
                summary["zero_image"] += 1
            elif result.status == "ok":
                summary["extracted"] += 1
                summary["images"] += len(result.images)
                summary["placements"] += sum(len(i.placements) for i in result.images)
            elif result.status == "encrypted":
                summary["encrypted"] += 1
            else:
                summary["failed"] += 1
                if verbose:
                    print(f"  images: {name} -> {result.status} ({result.detail})")
    return summary


def main(argv=None):
    """Extract embedded images from PDFs already on disk."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="fetchpdf-images",
        description=(
            "Dump every embedded image bitstream from retrieved PDFs into "
            "{stem}_images/ with a {stem}_images.json manifest (xref identity, "
            "sha256 of extracted and raw stream bytes, every page placement). "
            "Authors' original streams -- nothing is rendered or re-encoded."
        ),
    )
    parser.add_argument("path", help="Directory of artifacts (the -o dir from a run), or one PDF")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-extract even when the manifest matches the PDF bytes")
    parser.add_argument("--include-supplementary", action="store_true",
                        help="Also process *_supplementary_info_* PDFs (skipped by default)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if _pymupdf() is None:
        print(f"❌ {_MISSING_DEP_MSG}")
        return 1

    if os.path.isfile(args.path):
        result = extract_images(args.path, overwrite=args.overwrite, verbose=args.verbose)
        print(f"\n🖼️  {os.path.basename(args.path)}: {result.status}"
              + (f" — {result.detail}" if result.detail else ""))
        if result.status in ("ok", "skipped"):
            print(f"   streams written  {result.written}")
            print(f"   manifest         {result.manifest_path}")
            return 0
        return 1

    if not os.path.isdir(args.path):
        print(f"❌ Not a file or directory: {args.path}")
        return 1

    summary = extract_directory(args.path, overwrite=args.overwrite,
                                verbose=args.verbose,
                                include_supplementary=args.include_supplementary)
    print(f"\n🖼️  Image extraction — {args.path}")
    print(f"   extracted        {summary['extracted']}  ({summary['images']} streams, "
          f"{summary['placements']} placements)")
    print(f"   already current  {summary['skipped']}")
    print(f"   zero images      {summary['zero_image']}")
    if summary["encrypted"]:
        print(f"   encrypted        {summary['encrypted']}")
    if summary["supplementary_skipped"]:
        print(f"   SI PDFs skipped  {summary['supplementary_skipped']}  "
              f"(--include-supplementary to process)")
    if summary["failed"]:
        print(f"   failed           {summary['failed']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
