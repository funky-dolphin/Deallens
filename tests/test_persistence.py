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
