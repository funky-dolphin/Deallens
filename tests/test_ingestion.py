"""
Tests for the ingestion layer (Workstream 1).

Two kinds of test here, deliberately separated:

  * Unit tests over synthetic PDFs built in-memory, which pin the behaviour of
    each control including the failure modes a real filing does not exhibit --
    duplicate pages, scanned pages, broken numbering.

  * Characterisation tests against the Bio-Techne filing, which assert the
    values this pipeline actually produces on the development document. They
    skip when the file is absent so the suite still runs in a clean checkout.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from deallens.ingestion import (
    check_integrity,
    classify_structure,
    compute_anchor,
    load_pdf,
    verify_evidence,
)
from deallens.ingestion.loader import extract_printed_page_label, page_label_to_int
from deallens.ingestion.locators import SourceLocator, normalize_text

from .conftest import requires_bio_techne


def _pdf(page_count: int = 3) -> bytes:
    """A minimal valid PDF with blank pages and no text layer."""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Page label recovery
# ---------------------------------------------------------------------------

def test_folio_read_from_foot_of_page():
    assert extract_printed_page_label("Some clause text.\n\n62") == "62"


def test_folio_accepts_dashed_form():
    assert extract_printed_page_label("Clause text.\n- 17 -") == "17"


def test_folio_rejects_four_digit_year():
    """A year at the page edge is not a folio."""
    assert extract_printed_page_label("dated as of June 25,\n2026") is None


def test_folio_suppressed_on_contents_page():
    """
    A table of contents ends with the page reference of its last entry, which
    is positionally identical to a folio. This is the real failure observed on
    Bio-Techne PDF page 8, which claimed printed page 62.
    """
    toc = "TABLE OF CONTENTS\n" + "\n".join(
        f"Section 1.0{i}\nSome Heading\n{50 + i}" for i in range(8)
    )
    assert extract_printed_page_label(toc) is None


def test_folio_allows_roman_on_contents_page():
    """Contents pages are themselves numbered in roman; those folios are real."""
    toc = "TABLE OF CONTENTS\n" + "\n".join(f"Section\nHeading\n{i}" for i in range(8)) + "\niii"
    assert extract_printed_page_label(toc) == "iii"


@pytest.mark.parametrize(
    "label,expected",
    [("12", 12), ("iv", 4), ("ix", 9), ("xlii", 42), (None, None), ("A-1", None)],
)
def test_page_label_to_int(label, expected):
    assert page_label_to_int(label) == expected


# ---------------------------------------------------------------------------
# Integrity controls
# ---------------------------------------------------------------------------

def test_blank_pages_are_not_treated_as_scanned():
    """A page with neither text nor images is blank, not OCR-blocked."""
    report = check_integrity(load_pdf(_pdf(3), "blank.pdf"))
    assert report.requires_ocr is False
    assert report.blank_pages == [1, 2, 3]
    assert report.ingestion_status != "blocked"


def test_identical_blank_pages_are_not_reported_as_duplicates():
    """Empty pages are excluded from duplicate detection; three blanks are normal."""
    report = check_integrity(load_pdf(_pdf(3), "blank.pdf"))
    assert report.duplicate_groups == []


def test_label_outside_any_run_is_rejected():
    """
    A lone folio with no consistent neighbours cannot be trusted. Fail closed:
    the citation degrades to PDF page rather than asserting a printed number.
    """
    inventory = load_pdf(_pdf(4), "x.pdf")
    inventory.pages[1].printed_page = "62"
    report = check_integrity(inventory)
    assert report.rejected_labels == {2: "62"}
    assert 2 not in report.reconciled_labels
    assert any(i.kind == "label_anomaly" for i in report.issues)


def test_consistent_run_is_reconciled():
    """Four pages at a constant offset form a trustworthy numbering run."""
    inventory = load_pdf(_pdf(5), "x.pdf")
    for index, label in enumerate(["1", "2", "3", "4"], start=1):
        inventory.pages[index].printed_page = label
    report = check_integrity(inventory)
    assert report.reconciled_labels == {2: "1", 3: "2", 4: "3", 5: "4"}
    assert report.rejected_labels == {}


# ---------------------------------------------------------------------------
# Locators and evidence verification
# ---------------------------------------------------------------------------

def test_locator_uri_round_trips():
    locator = SourceLocator(
        document_id="doc_abc123",
        layer_id="agreement-ex2.1",
        pdf_page=71,
        printed_page="62",
        anchor=compute_anchor("the Company Termination Fee"),
    )
    assert SourceLocator.parse(locator.to_uri()).to_uri() == locator.to_uri()


def test_citation_shows_both_page_numbers():
    """
    Showing one number invites the reader to assume it is the other. The
    nine-page offset in the Bio-Techne filing makes this concrete.
    """
    locator = SourceLocator("doc_a", "agreement-ex2.1", pdf_page=71, printed_page="62")
    assert "page 62" in locator.citation()
    assert "PDF page 71" in locator.citation()


def test_evidence_verification_passes_for_genuine_quote():
    quote = "the Company Termination Fee shall be $250,000,000"
    page = f"Section 7.02. Effect of Termination. {quote}. The parties agree."
    locator = SourceLocator("d", "agreement", 1, anchor=compute_anchor(quote))
    assert verify_evidence(locator, quote, page).ok


def test_evidence_verification_fails_when_quote_absent_from_cited_page():
    """A quote attributed to the wrong page is a citation failure, not a pass."""
    quote = "the Company Termination Fee shall be $250,000,000"
    locator = SourceLocator("d", "agreement", 1, anchor=compute_anchor(quote))
    check = verify_evidence(locator, quote, "An unrelated page about notices.")
    assert not check.ok
    assert check.anchor_intact and not check.found_on_cited_page


def test_evidence_verification_fails_closed_without_quote():
    locator = SourceLocator("d", "agreement", 1, anchor="deadbeefdeadbeef")
    assert not verify_evidence(locator, None, "any text").ok


def test_normalization_survives_typographic_variation():
    assert normalize_text("the “Company” — Parent's") == normalize_text(
        'the "Company" - Parent\'s'
    )


# ---------------------------------------------------------------------------
# Structure classification
# ---------------------------------------------------------------------------

def test_unrecognised_document_is_not_classified():
    """Fail closed: no recognised language means unknown, routed to review."""
    result = classify_structure(load_pdf(_pdf(2), "blank.pdf"))
    assert result.structure == "unknown"
    assert result.review_status == "exception"


# ---------------------------------------------------------------------------
# Characterisation against the development document
# ---------------------------------------------------------------------------

@requires_bio_techne
def test_bio_techne_is_fully_machine_readable(bio_techne_inventory):
    assert bio_techne_inventory.page_count == 99
    assert bio_techne_inventory.is_machine_readable
    assert not bio_techne_inventory.requires_ocr


@requires_bio_techne
def test_bio_techne_layer_boundaries(bio_techne_layers):
    layers = {l.layer_id: l for l in bio_techne_layers}
    assert layers["filing-summary"].start_page == 1
    assert layers["filing-summary"].end_page == 5
    assert layers["agreement"].start_page == 6
    assert layers["agreement"].end_page == 97
    assert layers["agreement"].exhibit_number == "2.1"


@requires_bio_techne
def test_bio_techne_every_page_is_assigned_to_a_layer(bio_techne_inventory, bio_techne_layers):
    covered = sum(l.page_count for l in bio_techne_layers)
    assert covered == bio_techne_inventory.page_count


@requires_bio_techne
def test_bio_techne_agreement_regions(bio_techne_layers):
    """Contents, body, signature and annex are separated within the exhibit."""
    agreement = next(l for l in bio_techne_layers if l.layer_id == "agreement")
    regions = {r.region_id: (r.start_page, r.end_page) for r in agreement.regions}
    assert regions["table-of-contents"] == (7, 9)
    assert regions["body"][0] == 10
    assert "defined-terms" in regions
    # Navigational pages are excluded from the pages we scan for meaning.
    assert 8 not in agreement.body_pages()
    assert 71 in agreement.body_pages()


@requires_bio_techne
def test_bio_techne_printed_page_offset_is_nine_in_agreement_body(bio_techne_ingested):
    """
    The agreement's table of contents places Article VII on page 62; it is PDF
    page 71. This nine-page offset is why citations carry both numbers.
    """
    assert bio_techne_ingested.integrity.reconciled_labels[71] == "62"
    assert bio_techne_ingested.locator_for(71).printed_page == "62"


@requires_bio_techne
def test_bio_techne_annex_restarts_numbering(bio_techne_ingested):
    """
    Annex I begins again at printed page 1, so a printed number alone does not
    identify a location within a composite filing.
    """
    assert bio_techne_ingested.integrity.reconciled_labels[84] == "1"
    assert bio_techne_ingested.integrity.reconciled_labels[11] == "2"


@requires_bio_techne
def test_bio_techne_locator_names_the_right_layer(bio_techne_ingested):
    """A citation must identify which sub-document it points into."""
    assert bio_techne_ingested.locator_for(3).layer_id == "filing-summary"
    assert bio_techne_ingested.locator_for(71).layer_id == "agreement-ex2.1"


@requires_bio_techne
def test_bio_techne_classified_as_merger(bio_techne_ingested):
    assert bio_techne_ingested.structure.structure == "merger"
    assert len(bio_techne_ingested.structure.evidence) >= 4


@requires_bio_techne
def test_bio_techne_is_extractable_and_comparable(bio_techne_ingested):
    result = bio_techne_ingested
    assert result.may_extract
    assert result.summary_layer is not None
    assert result.agreement_layer is not None
    assert result.warnings == []


@requires_bio_techne
def test_bio_techne_no_duplicate_or_unreadable_pages(bio_techne_ingested):
    report = bio_techne_ingested.integrity
    assert report.duplicate_groups == []
    assert report.unreadable_pages == []
    assert report.ingestion_status in {"ingested", "ingested_with_warnings"}
