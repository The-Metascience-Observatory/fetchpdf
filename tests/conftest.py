"""Shared pytest configuration.

The suite is offline and CPU-bound, so it parallelizes cleanly; `-n auto` is
set in pyproject.toml and the hook below decides what "auto" means.
"""

import os

import pytest


# optionalhook: without pytest-xdist installed this hook is unknown, and pytest
# would refuse to start rather than run serially (`-o addopts=""`).
@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """min(8, cores-1) workers: leave one core for the rest of the machine,
    and stop at 8 -- past that, worker startup outweighs a 400-test suite.

    (An explicit -n or PYTEST_XDIST_AUTO_NUM_WORKERS still wins over this.)
    """
    return max(1, min(8, (os.cpu_count() or 2) - 1))


#: A PDF that is actually a PDF. The engine now checks that a retrieved PDF is
#: the article it was fetched for (retrieval/pdf_identity), so `%PDF` followed
#: by filler no longer stands in for one -- it has the magic bytes and no text,
#: which is precisely the shape the identity check exists to refuse. Built here
#: rather than shipped: a binary in the tree is a fixture nobody can read the
#: diff of, and hand-rolling keeps these tests free of an authoring dependency.
def text_pdf(lines=(), doi="10.1234/test"):
    def esc(text):
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    # The default body is sized against VALID_JATS in test_retrieval, because
    # the engine now refuses a PDF holding far less prose than the same
    # record's structured copy -- a real first-page preview scores 0.07 there.
    # A stub PDF next to a full JATS artifact would trip that legitimately.
    body = list(lines) or ([f"doi:{doi}"] + ["Full text of the trial report."] * 250)

    # Real pagination, so a caller asking for a long document gets a long
    # document rather than one page with the rest drawn off the bottom edge --
    # text past the media box does not extract, which silently made a "250 line"
    # fixture a 55-line one.
    per_page = 52
    pages = [body[i:i + per_page] for i in range(0, len(body), per_page)] or [[""]]

    streams = []
    for page_lines in pages:
        ops = ["BT", "/F1 11 Tf", "72 720 Td", "13 TL"]
        for line in page_lines:
            ops.append(f"({esc(line)}) Tj")
            ops.append("T*")
        ops.append("ET")
        streams.append("\n".join(ops).encode("latin-1", "replace"))

    # Object layout: 1 catalog, 2 pages, then per page a page object and its
    # content stream, then the font.
    n_pages = len(streams)
    page_ids = [3 + 2 * i for i in range(n_pages)]
    content_ids = [4 + 2 * i for i in range(n_pages)]
    font_id = 3 + 2 * n_pages

    objs = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: ("<</Type/Pages/Kids[" + " ".join(f"{i} 0 R" for i in page_ids)
            + f"]/Count {n_pages}>>").encode(),
        font_id: b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    }
    for pid, cid, stream in zip(page_ids, content_ids, streams):
        objs[pid] = (f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                     f"/Contents {cid} 0 R/Resources<</Font<</F1 {font_id} 0 R>>>>>>").encode()
        objs[cid] = (b"<</Length " + str(len(stream)).encode() + b">>\nstream\n"
                     + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += str(num).encode() + b" 0 obj\n" + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    highest = max(objs)
    out += b"xref\n0 " + str(highest + 1).encode() + b"\n0000000000 65535 f \n"
    for num in range(1, highest + 1):
        out += ("%010d 00000 n \n" % offsets.get(num, 0)).encode()
    out += (b"trailer\n<</Size " + str(highest + 1).encode() + b"/Root 1 0 R>>\n"
            b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n")
    return bytes(out)
