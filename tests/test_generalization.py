"""
Generalization tests (supports Workstream 7).

The development filing is one shape of document: an SEC 8-K wrapping a US
merger agreement, with a roman-numbered table of contents and arabic body
folios. Every threshold and pattern in the ingestion layer risks being tuned
to that shape.

These tests exercise shapes the development filing does not exhibit, using
synthetic documents so that the out-of-sample validation filings stay sealed
until Workstream 7. They assert what the pipeline *should* do; where it
cannot yet, the test states the required behaviour rather than the current
behaviour.
"""

from __future__ import annotations

import pytest

from deallens.ingestion import check_integrity, classify_structure, ingest, load_pdf, segment_layers
from deallens.ingestion.loader import extract_printed_page_label

from .pdf_factory import (
    agreement_pages,
    contents_pages,
    exhibit_cover,
    make_pdf,
    sec_cover_page,
)


def _ingest(pages, **kwargs):
    return ingest(make_pdf(pages, **kwargs), "synthetic.pdf", run_id="gen-test")


# ---------------------------------------------------------------------------
# Document assembly variants
# ---------------------------------------------------------------------------

def test_standalone_agreement_without_sec_wrapper():
    """
    A merger agreement filed on its own, with no 8-K summary and no exhibit
    cover. Every page must still be assigned to a layer, and the absence of a
    comparison layer must be reported rather than passing silently -- WS3
    cannot run on a document with only one layer.
    """
    result = _ingest(contents_pages(1) + agreement_pages(body_pages=6))
    assert sum(l.page_count for l in result.layers) == result.inventory.page_count
    assert result.structure.structure == "merger"
    assert any("filing-summary" in w for w in result.warnings)


def test_composite_filing_with_exhibit_cover():
    """The development document's own shape, rebuilt synthetically."""
    pages = (
        [sec_cover_page(), "Item 1.01 Entry into a Material Definitive Agreement."]
        + [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER")]
        + contents_pages(1)
        + agreement_pages(body_pages=6)
    )
    result = _ingest(pages)
    layers = {l.layer_id: l for l in result.layers}
    assert layers["filing-summary"].start_page == 1
    assert layers["agreement"].start_page == 3
    assert layers["agreement"].exhibit_number == "2.1"
    assert result.warnings == []


def test_exhibit_cover_behind_a_long_running_header():
    """
    Registrants put varying amounts of boilerplate above the exhibit label.
    A longer header than Bio-Techne's must not cause the exhibit cover to be
    missed, which would merge the agreement into the filing summary and make
    the WS3 comparison compare a document with itself.
    """
    long_header = (
        "EXAMPLE PHARMACEUTICALS INTERNATIONAL HOLDINGS CORPORATION "
        "FORM 8-K CURRENT REPORT FILED JUNE 26 2026 COMMISSION FILE NUMBER 001-12345"
    )
    pages = (
        [sec_cover_page(), "Item 1.01 Entry into a Material Definitive Agreement."]
        + [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER", header=long_header)]
        + agreement_pages(body_pages=5)
    )
    result = _ingest(pages)
    layers = {l.layer_id: l for l in result.layers}
    assert "agreement" in layers, "exhibit cover missed behind a long running header"
    assert layers["agreement"].start_page == 3


def test_arabic_numbered_table_of_contents():
    """
    Not every drafter numbers front matter in roman. Arabic contents folios
    must not be discarded wholesale, and the contents page reference column
    must still not be mistaken for a folio.
    """
    pages = contents_pages(2, numbering="arabic", start=1) + agreement_pages(
        body_pages=6, first_folio=3
    )
    result = _ingest(pages)
    labels = result.integrity.reconciled_labels
    assert labels.get(1) == "1", "arabic contents folio was discarded"
    assert labels.get(3) == "3"


def test_annex_style_folios_are_recognised():
    """Annex and schedule pages are commonly numbered A-1, A-2, not 1, 2."""
    assert extract_printed_page_label("Some annex text.\nA-1") == "A-1"
    assert extract_printed_page_label("Schedule text.\nI-12") == "I-12"


def test_annex_style_folios_reconcile_as_a_run():
    pages = agreement_pages(body_pages=6, folio_style="annex", first_folio=1)
    result = _ingest(pages)
    assert result.integrity.reconciled_labels.get(1) == "A-1"
    assert result.integrity.reconciled_labels.get(6) == "A-6"


# ---------------------------------------------------------------------------
# Integrity across document shapes
# ---------------------------------------------------------------------------

def test_scanned_page_requires_ocr_and_blocks_extraction():
    """
    A page with raster content and no text layer is the OCR case. It must be
    distinguished from a blank page and must block extraction: extracting
    around an unreadable page yields a result that looks complete.
    """
    pages = agreement_pages(body_pages=4) + [""]
    result = ingest(
        make_pdf(pages, with_image_on={5}), "scanned.pdf", run_id="gen-test"
    )
    assert result.inventory.page(5).text_layer_status == "image_only"
    assert result.integrity.requires_ocr
    assert result.integrity.ingestion_status == "blocked"
    assert not result.may_extract


def test_duplicated_substantial_page_is_flagged():
    """A page repeated by a broken assembly step shifts every page after it."""
    body = agreement_pages(body_pages=4)
    result = _ingest(body + [body[2]])
    assert result.integrity.duplicate_groups
    assert any(i.kind == "duplicate_page" and i.severity == "error" for i in result.integrity.issues)


def test_very_short_document_degrades_citations_rather_than_inventing_them():
    """
    A three-page filing cannot form a numbering run. The correct outcome is
    that citations fall back to PDF page, not that unverified folios are
    asserted.
    """
    result = _ingest([sec_cover_page(), "Item 8.01 Other Events.\n1", "Signature.\n2"])
    for pdf_page in (1, 2, 3):
        locator = result.locator_for(pdf_page)
        assert locator.pdf_page == pdf_page
        assert locator.printed_page is None or locator.pdf_page is not None


# ---------------------------------------------------------------------------
# Transaction structures other than a US one-step merger
# ---------------------------------------------------------------------------

def test_tender_offer_is_classified():
    vocabulary = (
        "Purchaser commenced an Offer to Purchase all outstanding Shares. "
        "The Minimum Condition requires Shares validly tendered and not "
        "withdrawn. A Schedule TO has been filed."
    )
    result = _ingest(
        [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER")]
        + agreement_pages(body_pages=5, vocabulary=vocabulary)
    )
    assert result.structure.structure in {"tender_offer", "merger"}


def test_german_takeover_offer_is_classified():
    """
    A cross-border voluntary public takeover offer has acceptance mechanics
    where a US merger has a shareholder vote. Misclassifying it cascades into
    every timing and conditionality question downstream.
    """
    vocabulary = (
        "Bidder announced a voluntary public takeover offer pursuant to the "
        "WpUeG. The Acceptance Period commences upon publication of the Offer "
        "Document approved by BaFin. The minimum acceptance threshold is 75%."
    )
    result = _ingest(
        [exhibit_cover("2.1", "BUSINESS COMBINATION AGREEMENT")]
        + agreement_pages(
            title="BUSINESS COMBINATION AGREEMENT", body_pages=5, vocabulary=vocabulary
        )
    )
    assert result.structure.structure == "takeover_offer"


def test_two_step_tender_offer_with_back_end_merger_is_not_discarded():
    """
    A tender offer followed by a 251(h) back-end merger genuinely exhibits
    both structures. Collapsing that to `unknown` throws away a correct and
    confident reading; the primary structure should be reported with the
    secondary recorded alongside it.
    """
    vocabulary = (
        "Purchaser shall commence an Offer to Purchase all Shares. Following "
        "acceptance of Shares validly tendered, Merger Sub shall merge with "
        "and into the Company pursuant to Section 251(h) of the DGCL, and at "
        "the Effective Time the Surviving Corporation shall continue. The "
        "Minimum Condition must be satisfied."
    )
    result = _ingest(
        [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER")]
        + agreement_pages(body_pages=5, vocabulary=vocabulary)
    )
    assert result.structure.structure != "unknown", (
        "a hybrid two-step deal was discarded as unclassifiable"
    )
    assert result.structure.secondary_structures, "the secondary structure was not recorded"


def test_unrecognised_structure_still_fails_closed():
    """Genuine absence of signal must remain `unknown`, routed to review."""
    result = _ingest(["A notice about an annual general meeting.\n1"] * 4)
    assert result.structure.structure == "unknown"
    assert result.structure.review_status == "exception"
