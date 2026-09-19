"""
Tests for human review and manual correction (Workstream 8).

The point of a review queue is that a person can resolve what the pipeline
would not assert. Before this, marking a field verified left its value null,
so the reviewer's judgement was recorded and then ignored downstream.
"""

from __future__ import annotations

import json

import pytest

from deallens.db import (
    get_connection,
    get_extracted_fields,
    get_review_queue,
    initialize_schema,
    save_extraction,
    save_ingestion,
)
from deallens.extraction import extract_document, models
from deallens.ingestion import ingest
from deallens.review import HYBRID, MANUAL, apply_correction

from .pdf_factory import agreement_pages, exhibit_cover, make_pdf, sec_cover_page
from .test_extraction import EVIDENCE, FakeClient, _found


@pytest.fixture
def conn_with_extraction():
    """A document extracted with one withheld field, ready for review."""
    body = agreement_pages(body_pages=5)
    body[1] = f"ARTICLE VII TERMINATION\n{EVIDENCE}. The parties agree.\n2"
    pages = [
        sec_cover_page(),
        "Item 1.01 Entry into a Material Definitive Agreement.",
        exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER"),
    ] + body
    pdf = make_pdf(pages)

    connection = initialize_schema(get_connection())
    ingestion = ingest(pdf, "filing.pdf", run_id="review-test")
    save_ingestion(connection, ingestion)
    # A low-confidence reading: found, but withheld by the threshold.
    client = FakeClient(
        {"company_termination_fee": _found("$250,000,000", 3, EVIDENCE, confidence=0.30)}
    )
    save_extraction(connection, extract_document(client, ingestion, pdf))
    yield connection, ingestion.document_id
    connection.close()


def _field(conn, document_id, field_name, layer="agreement-ex2.1"):
    return next(
        r
        for r in get_extracted_fields(conn, document_id)
        if r["field_name"] == field_name and r["document_layer"] == layer
    )


# ---------------------------------------------------------------------------
# The gap this closes
# ---------------------------------------------------------------------------

def test_a_withheld_field_starts_with_no_value(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    assert row["status"] == models.UNRESOLVED
    assert row["normalized_value"] is None
    assert row["raw_value"] == "$250,000,000", "the raw reading is kept for review"


def test_a_correction_restores_the_value_downstream(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    result = apply_correction(
        conn, row["id"], "$250,000,000", evidence=EVIDENCE, pdf_page=5,
        reviewer_note="Checked against the executed agreement.",
    )
    assert result.accepted

    corrected = _field(conn, document_id, "company_termination_fee")
    assert corrected["status"] == models.FOUND
    assert corrected["normalized_value"] == 250_000_000.0
    assert corrected["review_status"] == models.VERIFIED
    assert corrected["confidence"] == 1.0


def test_a_corrected_row_leaves_the_review_queue_and_the_other_layer_stays(
    conn_with_extraction,
):
    """
    The queue holds one row per layer, so correcting the agreement's reading
    resolves that row and leaves the filing summary's alone. They are readings
    of two different documents, and Workstream 3 depends on that separation
    surviving review.
    """
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")
    summary_row = _field(conn, document_id, "company_termination_fee", "filing-summary")
    queued = {q["id"] for q in get_review_queue(conn, document_id)}
    assert {row["id"], summary_row["id"]} <= queued

    apply_correction(conn, row["id"], "$250,000,000", evidence=EVIDENCE, pdf_page=5)

    remaining = {q["id"] for q in get_review_queue(conn, document_id)}
    assert row["id"] not in remaining
    assert summary_row["id"] in remaining, "the other layer still needs a look"


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------

def test_the_models_original_reading_is_preserved(conn_with_extraction):
    """
    The assignment requires material manual corrections be disclosed. An audit
    record that overwrites what the model said cannot do that.
    """
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    apply_correction(conn, row["id"], "$255,000,000", evidence=EVIDENCE, pdf_page=5)

    corrected = _field(conn, document_id, "company_termination_fee")
    disclosure = " ".join(corrected["notes"])
    assert "Manual correction" in disclosure
    assert "'$250,000,000'" in disclosure, "the superseded reading is named"
    assert "confidence 0.30" in disclosure


def test_correcting_a_reading_is_hybrid_and_supplying_one_is_manual(conn_with_extraction):
    """Both are in the assignment's required extraction_method vocabulary."""
    conn, document_id = conn_with_extraction

    read = _field(conn, document_id, "company_termination_fee")
    assert apply_correction(
        conn, read["id"], "$255,000,000", evidence=EVIDENCE, pdf_page=5
    ).extraction_method == HYBRID

    never_found = _field(conn, document_id, "fee_tail")
    assert not (never_found["raw_value"] or "").strip()
    assert apply_correction(
        conn, never_found["id"], "12 months following termination"
    ).extraction_method == MANUAL


def test_the_reviewer_note_is_recorded(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    apply_correction(
        conn, row["id"], "$250,000,000", evidence=EVIDENCE, pdf_page=5,
        reviewer_note="Confirmed against Section 7.02.",
    )
    notes = _field(conn, document_id, "company_termination_fee")["notes"]
    assert any("Confirmed against Section 7.02." in n for n in notes)


# ---------------------------------------------------------------------------
# A correction is still normalized and still checked
# ---------------------------------------------------------------------------

def test_a_value_that_will_not_normalize_is_refused_not_stored(conn_with_extraction):
    """
    A reviewer who types "next June" into a date field should be told the
    field could not be read, not have it silently stored as text.
    """
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "outside_date")

    result = apply_correction(conn, row["id"], "next June sometime")

    assert not result.accepted
    assert "could not be read as a date" in result.reason
    assert _field(conn, document_id, "outside_date")["normalized_value"] is None


def test_an_empty_value_is_refused(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "outside_date")
    result = apply_correction(conn, row["id"], "   ")

    assert not result.accepted
    assert "value is required" in result.reason.lower()


def test_a_manual_value_is_normalized_by_the_same_code(conn_with_extraction):
    """Downstream consumers must get the same types a model value produces."""
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "outside_date")

    apply_correction(conn, row["id"], "June 25, 2027")
    assert _field(conn, document_id, "outside_date")["normalized_value"] == "2027-06-25"


def test_a_manual_money_value_picks_up_its_currency(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    apply_correction(conn, row["id"], "€250,000,000", evidence=EVIDENCE, pdf_page=5)
    assert _field(conn, document_id, "company_termination_fee")["currency"] == "EUR"


def test_manual_evidence_is_checked_against_the_cited_page(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    result = apply_correction(conn, row["id"], "$250,000,000", evidence=EVIDENCE, pdf_page=5)
    assert result.evidence_verified is True


def test_a_quote_that_is_not_on_the_page_is_recorded_not_blocked(conn_with_extraction):
    """
    A human reading the filing outranks our matcher, so the correction stands
    -- but the failed check is disclosed rather than hidden.
    """
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    result = apply_correction(
        conn, row["id"], "$250,000,000",
        evidence="a quote that appears nowhere in this document", pdf_page=5,
    )

    assert result.accepted
    assert result.evidence_verified is False
    corrected = _field(conn, document_id, "company_termination_fee")
    assert corrected["status"] == models.FOUND
    assert any("did not match the cited page" in n for n in corrected["notes"])


def test_the_locator_follows_a_corrected_page(conn_with_extraction):
    conn, document_id = conn_with_extraction
    row = _field(conn, document_id, "company_termination_fee")

    apply_correction(conn, row["id"], "$250,000,000", evidence=EVIDENCE, pdf_page=5)
    corrected = _field(conn, document_id, "company_termination_fee")

    assert corrected["locator_uri"].startswith(f"deallens://{document_id}/")
    assert "/pdf:5" in corrected["locator_uri"]


def test_an_unknown_row_is_refused(conn_with_extraction):
    conn, _ = conn_with_extraction
    assert not apply_correction(conn, 999_999, "$1").accepted
