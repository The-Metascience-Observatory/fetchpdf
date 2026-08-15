"""Finding the corresponding author's address in artifacts already on disk.

Every fixture below is a real shape observed across a 43-paper corpus, not an
invented one. Precision is what these protect: a wrong address emails a
stranger in the user's name, so the interesting cases are the ones where a
naive regex picks the wrong person or nothing at all.
"""

from fetchpdf.retrieval.corresponding import (
    SOURCE_HTML,
    SOURCE_JATS,
    SOURCE_PDF,
    Contact,
    decode_cfemail,
    from_html,
    from_jats,
    _scored_pick,
)


class TestCloudflareObfuscation:
    """Wiley renders the address as the literal text "[email protected]".

    The real value is in data-cfemail, XOR-ed with its own first byte. Without
    decoding, a page that DOES carry the address looks like it carries none --
    which is exactly the silent under-performance this guards.
    """

    def test_the_real_blob_decodes(self):
        assert decode_cfemail("244a49455e45566446510a414051") == "nmazar@bu.edu"

    def test_garbage_is_declined_not_guessed(self):
        assert decode_cfemail("zzzz") is None
        assert decode_cfemail("") is None
        assert decode_cfemail("41") is None            # key only, no payload

    def test_a_decode_that_is_not_an_email_is_rejected(self):
        # 0x41 key over "hello" -> not an address, must not be returned.
        blob = "41" + "".join(f"{ord(c) ^ 0x41:02x}" for c in "hello")
        assert decode_cfemail(blob) is None

    def test_html_uses_it_when_no_mailto_exists(self):
        html = ('<p>Nina Mazar , Corresponding Author Nina Mazar '
                '<a class="__cf_email__" data-cfemail="244a49455e45566446510a414051">'
                '[email&#160;protected]</a></p>')
        contact = from_html(html)
        assert contact.email == "nmazar@bu.edu"
        assert contact.source == SOURCE_HTML


class TestHtmlPrecedence:
    def test_mailto_wins(self):
        html = '<a href="mailto:real@uni.edu">write</a> other@elsewhere.org'
        assert from_html(html).email == "real@uni.edu"

    def test_a_cfemail_beside_the_corresponding_label_beats_a_bare_one(self):
        html = ('<span data-cfemail="' + "".join(
                    ["41"] + [f"{ord(c) ^ 0x41:02x}" for c in "other@x.edu"]) + '"></span>'
                '<p>Corresponding Author '
                '<span data-cfemail="244a49455e45566446510a414051"></span></p>')
        assert from_html(html).email == "nmazar@bu.edu"

    def test_visible_text_is_the_last_resort(self):
        html = "<p>Corresponding author: eyal.peer@mail.huji.ac.il</p>"
        contact = from_html(html)
        assert contact.email == "eyal.peer@mail.huji.ac.il"
        assert contact.cue


class TestJats:
    def test_corresp_block(self):
        xml = ('<front><corresp id="cor1">To whom correspondence may be '
               'addressed. Email: <email>nina@ninamazar.com</email></corresp>'
               '</front><body>...</body>')
        contact = from_jats(xml)
        assert contact.email == "nina@ninamazar.com"
        assert contact.source == SOURCE_JATS
        assert contact.confidence == "high"

    def test_reference_list_addresses_are_not_reached(self):
        """The front matter ends before <body>; a citation's email is not ours."""
        xml = ("<front><article-title>T</article-title></front>"
               "<body><ref-list><ref>someone@other.org</ref></ref-list></body>")
        assert from_jats(xml) is None


class TestScoredPick:
    """14 of 43 corpus PDFs carry more than one address, so first-match fails."""

    def test_the_cued_address_wins_over_an_earlier_bare_one(self):
        text = ("Some Journal 2024  production@publisher.org\n"
                "Corresponding author. E-mail address: real.author@uni.edu")
        assert _scored_pick(text, SOURCE_PDF).email == "real.author@uni.edu"

    def test_multi_author_lists_take_the_first(self):
        """Elsevier lists every author; the corresponding one leads."""
        text = ("Corresponding author. E-mail addresses: "
                "faschulz@wiwi.uni-frankfurt.de (F. Schulz), "
                "christian.schlereth@whu.edu (C. Schlereth)")
        assert _scored_pick(text, SOURCE_PDF).email == "faschulz@wiwi.uni-frankfurt.de"

    def test_publisher_boilerplate_never_wins(self):
        text = ("Corresponding author: permissions@wiley.com "
                "... author block ... real@uni.edu")
        assert _scored_pick(text, SOURCE_PDF).email == "real@uni.edu"

    def test_a_bare_address_is_still_returned_but_low_confidence(self):
        """15 of 43 PDFs have no cue at all; dropping them loses real contacts."""
        text = "ON AMIR Yale University oamir@ucsd.edu DAN ARIELY MIT"
        contact = _scored_pick(text, SOURCE_PDF)
        assert contact.email == "oamir@ucsd.edu"
        assert contact.confidence == "low"

    def test_no_email_is_none_not_an_exception(self):
        assert _scored_pick("no addresses here at all", SOURCE_PDF) is None
        assert _scored_pick("", SOURCE_PDF) is None


class TestProvenance:
    """A draft shows where the address came from, so a human can judge staleness.

    One author appeared under four addresses across this corpus (two of them
    dead), so "which paper said this, and when" is load-bearing.
    """

    def test_provenance_names_source_doi_and_year(self):
        contact = Contact(email="a@b.edu", source=SOURCE_PDF, cue="Corresponding author",
                          paper_doi="10.1016/x", paper_year=2022)
        text = contact.provenance
        assert "PDF" in text and "10.1016/x" in text and "2022" in text

    def test_confidence_reflects_how_explicit_the_source_was(self):
        assert Contact("a@b.edu", SOURCE_JATS).confidence == "high"
        assert Contact("a@b.edu", SOURCE_HTML, cue="Corresponding").confidence == "high"
        assert Contact("a@b.edu", SOURCE_PDF, cue="Corresponding").confidence == "medium"
        assert Contact("a@b.edu", SOURCE_PDF).confidence == "low"
