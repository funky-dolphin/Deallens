"""
Persistence for ingestion results.

Writes are transactional per document: either the whole ingestion record lands
or none of it does. A half-written document -- pages stored but layers missing
-- would present as a successfully ingested filing whose citations silently
resolve to the wrong layer, which is worse than a visible failure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..ingestion.pipeline import IngestionResult
from .schema import SCHEMA_VERSION


def record_run(conn: sqlite3.Connection, run_id: str, ingestion_version: str, note: str | None = None) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO runs (run_id, started_at, ingestion_version, schema_version, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, datetime.now(timezone.utc).isoformat(), ingestion_version, SCHEMA_VERSION, note),
    )


def document_exists(conn: sqlite3.Connection, checksum: str) -> str | None:
    """
    Return the document_id of an already-ingested file with this checksum.

    Deduplication is by content, not filename: the same filing saved under two
    names is one document, and re-ingesting it should be recognised rather than
    silently duplicated.
    """
    row = conn.execute(
        "SELECT document_id FROM documents WHERE checksum = ?", (checksum,)
    ).fetchone()
    return row["document_id"] if row else None


def save_ingestion(
    conn: sqlite3.Connection,
    result: IngestionResult,
    store_page_text: bool = True,
) -> str:
    """
    Persist a complete ingestion result. Returns the document_id.

    Re-ingesting the same document replaces its prior records rather than
    accumulating them, so the database always reflects the most recent run for
    a given file while the `runs` table preserves the history of runs.
    """
    document_id = result.document_id
    try:
        with conn:  # transactional: rolls back on any exception
            record_run(conn, result.run_id, result.ingestion_version)

            for table in (
                "document_pages",
                "document_layers",
                "document_regions",
                "integrity_issues",
                "structure_evidence",
            ):
                conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))

            record = result.document_record()
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, filename, source_url, checksum, byte_size,
                    filing_date, ingestion_timestamp, page_count,
                    is_machine_readable, requires_ocr, ingestion_status,
                    transaction_structure, structure_confidence,
                    structure_review_status, ingestion_version, schema_version, run_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["document_id"],
                    record["filename"],
                    record["source_url"],
                    record["checksum"],
                    record["byte_size"],
                    record["filing_date"],
                    record["ingestion_timestamp"],
                    record["page_count"],
                    int(record["is_machine_readable"]),
                    int(record["requires_ocr"]),
                    record["ingestion_status"],
                    record["transaction_structure"],
                    record["structure_confidence"],
                    record["structure_review_status"],
                    record["ingestion_version"],
                    SCHEMA_VERSION,
                    record["run_id"],
                ),
            )

            reconciled = result.integrity.reconciled_labels
            rejected = result.integrity.rejected_labels
            conn.executemany(
                """
                INSERT INTO document_pages (
                    document_id, pdf_page, printed_page, printed_page_source,
                    char_count, text_layer_status, content_hash, has_images, text
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        document_id,
                        page.pdf_page,
                        reconciled.get(page.pdf_page),
                        # Why we do or do not trust this page's printed number.
                        "reconciled"
                        if page.pdf_page in reconciled
                        else ("rejected" if page.pdf_page in rejected else "absent"),
                        page.char_count,
                        page.text_layer_status,
                        page.content_hash,
                        int(page.has_images),
                        page.text if store_page_text else None,
                    )
                    for page in result.inventory.pages
                ],
            )

            for layer in result.layers:
                conn.execute(
                    """
                    INSERT INTO document_layers (
                        document_id, qualified_id, layer_id, label, exhibit_number,
                        start_page, end_page, detection_method, evidence, run_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        document_id,
                        layer.qualified_id,
                        layer.layer_id,
                        layer.label,
                        layer.exhibit_number,
                        layer.start_page,
                        layer.end_page,
                        layer.detection_method,
                        layer.evidence,
                        result.run_id,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO document_regions (
                        document_id, layer_id, region_id, label, start_page, end_page, run_id
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            document_id,
                            layer.qualified_id,
                            region.region_id,
                            region.label,
                            region.start_page,
                            region.end_page,
                            result.run_id,
                        )
                        for region in layer.regions
                    ],
                )

            conn.executemany(
                """
                INSERT INTO integrity_issues (
                    document_id, kind, severity, pdf_pages, detail, run_id
                ) VALUES (?,?,?,?,?,?)
                """,
                [
                    (
                        document_id,
                        issue.kind,
                        issue.severity,
                        json.dumps(issue.pdf_pages),
                        issue.detail,
                        result.run_id,
                    )
                    for issue in result.integrity.issues
                ],
            )

            conn.executemany(
                """
                INSERT INTO structure_evidence (
                    document_id, structure, note, weight, pdf_page, matched_text, run_id
                ) VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        document_id,
                        item["structure"],
                        item["note"],
                        item["weight"],
                        item["pdf_page"],
                        item["matched_text"],
                        result.run_id,
                    )
                    for item in result.structure.evidence
                ],
            )
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to persist ingestion of {document_id}: {exc}") from exc

    return document_id


def get_documents(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM documents ORDER BY ingestion_timestamp DESC"
        )
    ]


def get_document(conn: sqlite3.Connection, document_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    return dict(row) if row else None


def get_layers(conn: sqlite3.Connection, document_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM document_layers WHERE document_id = ? ORDER BY start_page",
            (document_id,),
        )
    ]


def get_regions(conn: sqlite3.Connection, document_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM document_regions WHERE document_id = ? ORDER BY start_page",
            (document_id,),
        )
    ]


def get_integrity_issues(conn: sqlite3.Connection, document_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM integrity_issues WHERE document_id = ? ORDER BY severity, kind",
        (document_id,),
    )
    issues = []
    for row in rows:
        issue = dict(row)
        issue["pdf_pages"] = json.loads(issue["pdf_pages"])
        issues.append(issue)
    return issues


def get_page_text(conn: sqlite3.Connection, document_id: str, pdf_page: int) -> str | None:
    """
    Retrieve a stored page's text so a citation can be verified.

    Returns None when the page is unknown or its text was not stored, which the
    caller must treat as "cannot verify" rather than "verified".
    """
    row = conn.execute(
        "SELECT text FROM document_pages WHERE document_id = ? AND pdf_page = ?",
        (document_id, pdf_page),
    ).fetchone()
    return row["text"] if row else None
