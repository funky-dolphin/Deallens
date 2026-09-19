"""Tests for ingestion persistence and the audit record (WS1 + WS8)."""

from __future__ import annotations

import sqlite3

import pytest

from deallens.db import (
    document_exists,
    get_document,
    get_integrity_issues,
    get_layers,
    get_page_text,
    get_regions,
    get_connection,
    initialize_schema,
    save_ingestion,
)

from .conftest import requires_bio_techne


@pytest.fixture
def conn():
    connection = initialize_schema(get_connection())
    yield connection
    connection.close()


@requires_bio_techne
def test_ingestion_round_trips_to_database(conn, bio_techne_ingested):
    document_id = save_ingestion(conn, bio_techne_ingested)
    record = get_document(conn, document_id)
    assert record["page_count"] == 99
    assert record["transaction_structure"] == "merger"
    assert record["is_machine_readable"] == 1
    assert record["requires_ocr"] == 0
    assert record["run_id"] == "test-run"
    assert record["source_url"].startswith("https://investors.bio-techne.com/")


@requires_bio_techne
def test_layers_and_regions_persist(conn, bio_techne_ingested):
    document_id = save_ingestion(conn, bio_techne_ingested)
    layers = {l["layer_id"]: l for l in get_layers(conn, document_id)}
    assert layers["agreement"]["start_page"] == 6
    assert layers["agreement"]["exhibit_number"] == "2.1"
    regions = get_regions(conn, document_id)
    assert any(r["region_id"] == "table-of-contents" for r in regions)


@requires_bio_techne
def test_printed_page_provenance_is_recorded(conn, bio_techne_ingested):
    """
    Each page records *why* its printed number is or is not trusted, so a
    reviewer can tell a missing folio from a rejected one.
    """
    document_id = save_ingestion(conn, bio_techne_ingested)
    rows = {
        r["pdf_page"]: r
        for r in conn.execute(
            "SELECT pdf_page, printed_page, printed_page_source FROM document_pages "
            "WHERE document_id = ?",
            (document_id,),
        )
    }
    assert rows[71]["printed_page"] == "62"
    assert rows[71]["printed_page_source"] == "reconciled"
    assert rows[7]["printed_page_source"] == "rejected"
    assert rows[7]["printed_page"] is None
    assert rows[10]["printed_page_source"] == "absent"


@requires_bio_techne
def test_page_text_is_retrievable_for_evidence_verification(conn, bio_techne_ingested):
    document_id = save_ingestion(conn, bio_techne_ingested)
    text = get_page_text(conn, document_id, 10)
    assert "AGREEMENT AND PLAN OF MERGER" in text


@requires_bio_techne
def test_reingestion_replaces_rather_than_duplicates(conn, bio_techne_ingested):
    save_ingestion(conn, bio_techne_ingested)
    save_ingestion(conn, bio_techne_ingested)
    (count,) = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
    assert count == 1
    (pages,) = conn.execute("SELECT COUNT(*) FROM document_pages").fetchone()
    assert pages == 99


def test_reingestion_survives_an_existing_extraction():
    """
    Re-uploading a document that has already been extracted must not fail.

    `extracted_fields` holds a foreign key to `documents`, and ingestion does
    not regenerate extractions, so the document row is updated in place rather
    than deleted and re-inserted. Deleting it raised `FOREIGN KEY constraint
    failed` -- and because document_id is derived from the file checksum,
    re-uploading the same PDF hit that every time.

    The extraction must also survive: the file is byte-identical, so its
    layers and page numbering are unchanged and the stored fields still refer
    to something real.
    """
    from deallens.db import get_extracted_fields, save_extraction
    from deallens.extraction import extract_document
    from deallens.ingestion import ingest

    from .pdf_factory import agreement_pages, exhibit_cover, make_pdf, sec_cover_page
    from .test_extraction import FakeClient

    pdf = make_pdf(
        [
            sec_cover_page(),
            "Item 1.01 Entry into a Material Definitive Agreement.",
            exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER"),
        ]
        + agreement_pages(body_pages=3)
    )

    connection = initialize_schema(get_connection())
    try:
        first = ingest(pdf, "filing.pdf", run_id="run-one")
        document_id = save_ingestion(connection, first)
        save_extraction(connection, extract_document(FakeClient(), first, pdf))
        extracted = len(get_extracted_fields(connection, document_id))
        assert extracted > 0

        # The user uploads the same file again.
        second = ingest(pdf, "filing.pdf", run_id="run-two")
        assert second.document_id == document_id, "same bytes, same document"
        save_ingestion(connection, second)

        assert get_document(connection, document_id)["run_id"] == "run-two"
        assert len(get_extracted_fields(connection, document_id)) == extracted
    finally:
        connection.close()


@requires_bio_techne
def test_duplicate_detected_by_checksum_not_filename(conn, bio_techne_ingested):
    save_ingestion(conn, bio_techne_ingested)
    assert document_exists(conn, bio_techne_ingested.inventory.checksum)
    assert document_exists(conn, "0" * 64) is None


@requires_bio_techne
def test_integrity_issues_are_part_of_the_audit_record(conn, bio_techne_ingested):
    document_id = save_ingestion(conn, bio_techne_ingested)
    issues = get_integrity_issues(conn, document_id)
    assert any(i["kind"] == "label_anomaly" for i in issues)
    assert all(isinstance(i["pdf_pages"], list) for i in issues)


def test_failed_write_leaves_no_partial_document(conn):
    """
    A document must not be left half-written. If page insertion fails, the
    document row must roll back with it -- otherwise the filing presents as
    ingested while its citations cannot resolve to any page or layer.

    The failure is induced for real by removing the target table, rather than
    by patching, so the rollback path exercised is the one that would run in
    production.
    """
    import io

    from pypdf import PdfWriter

    from deallens.ingestion import ingest

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = ingest(buffer.getvalue(), "blank.pdf", run_id="r1")

    conn.execute("DROP TABLE document_pages")

    with pytest.raises(RuntimeError, match="Failed to persist"):
        save_ingestion(conn, result)

    (documents,) = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
    (layers,) = conn.execute("SELECT COUNT(*) FROM document_layers").fetchone()
    assert documents == 0
    assert layers == 0


# ---------------------------------------------------------------------------
# Extracted fields (Workstream 2)
# ---------------------------------------------------------------------------

def _extraction_run(document_id="doc_x", run_id="r1"):
    from deallens.extraction.extractor import ExtractionRun, LayerExtraction
    from deallens.extraction.models import ExtractedField, CONFLICT, EXCEPTION, FOUND

    summary = ExtractedField(
        field_name="consideration_per_share", document_id=document_id, run_id=run_id,
        document_layer="filing-summary", normalized_value=73.0, currency="USD",
        raw_value="$73.00", pdf_page=2, printed_page=None, section="Merger Consideration",
        evidence="will be converted into the right to receive $73.00 in cash",
        confidence=0.98, status=FOUND, evidence_verified=True,
    )
    agreement = ExtractedField(
        field_name="consideration_per_share", document_id=document_id, run_id=run_id,
        document_layer="agreement-ex2.1", normalized_value=73.0, currency="USD",
        raw_value="$73.00", pdf_page=12, printed_page="3", section="Section 2.01",
        evidence="the right to receive $73.00 in cash", confidence=0.99,
        status=FOUND, evidence_verified=True,
    )
    conflicted = ExtractedField(
        field_name="company_termination_fee", document_id=document_id, run_id=run_id,
        document_layer="agreement-ex2.1", raw_value="$250,000,000", evidence="quote",
        pdf_page=71, status=CONFLICT, review_status=EXCEPTION, confidence=0.8,
    )
    conflicted.add_note("Conflicting values found within the same layer.")

    run = ExtractionRun(
        document_id=document_id, run_id=run_id,
        model_id="claude-opus-5", prompt_version="2.0.0",
    )
    run.layers = [LayerExtraction(layer_id="filing-summary", layer_label="Filing summary", input_tokens=10, output_tokens=5)]
    run.fields = [summary, agreement, conflicted]
    return run


@requires_bio_techne
def test_extracted_fields_round_trip(conn, bio_techne_ingested):
    from deallens.db import get_extracted_fields, save_extraction

    document_id = save_ingestion(conn, bio_techne_ingested)
    run = _extraction_run(document_id, bio_techne_ingested.run_id)
    assert save_extraction(conn, run) == 3

    rows = get_extracted_fields(conn, document_id)
    assert len(rows) == 3
    per_share = [r for r in rows if r["field_name"] == "consideration_per_share"]
    assert len(per_share) == 2, "both layers' readings must persist separately"
    assert {r["document_layer"] for r in per_share} == {"filing-summary", "agreement-ex2.1"}
    # Typed round-trip: a float must not come back as a string.
    assert per_share[0]["normalized_value"] == 73.0
    assert isinstance(per_share[0]["normalized_value"], float)


@requires_bio_techne
def test_review_queue_surfaces_conflicts_critical_first(conn, bio_techne_ingested):
    from deallens.db import get_review_queue, save_extraction

    document_id = save_ingestion(conn, bio_techne_ingested)
    save_extraction(conn, _extraction_run(document_id, bio_techne_ingested.run_id))

    queue = get_review_queue(conn, document_id)
    assert len(queue) == 1
    assert queue[0]["field_name"] == "company_termination_fee"
    assert queue[0]["status"] == "conflict"
    assert queue[0]["is_critical"] is True
    assert queue[0]["notes"]


@requires_bio_techne
def test_review_decision_is_recorded(conn, bio_techne_ingested):
    from deallens.db import get_review_queue, save_extraction, set_review_status

    document_id = save_ingestion(conn, bio_techne_ingested)
    save_extraction(conn, _extraction_run(document_id, bio_techne_ingested.run_id))
    row_id = get_review_queue(conn, document_id)[0]["id"]

    set_review_status(conn, row_id, "verified", note="Confirmed $250m against Section 7.02.")
    assert get_review_queue(conn, document_id) == []
    row = conn.execute("SELECT * FROM extracted_fields WHERE id = ?", (row_id,)).fetchone()
    assert row["review_status"] == "verified"
    assert "Confirmed $250m" in row["notes"]
