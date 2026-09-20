"""
Database schema.

The schema is the audit record. Every table carries the `run_id` that produced
its rows, so a reviewer can reconstruct exactly what a given run saw and
concluded, and re-running with changed logic produces a new run rather than
overwriting the old one.

Page text is stored alongside the structural records. This costs a few hundred
kilobytes per filing and buys two things that matter for Workstream 8: evidence
quotes can be verified against their cited page without re-parsing the PDF, and
a citation stays resolvable even if the source file is no longer to hand.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "1.0.0"

_STATEMENTS = (
    # -- Document registry ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS documents (
        document_id             TEXT PRIMARY KEY,
        filename                TEXT NOT NULL,
        source_url              TEXT,
        checksum                TEXT NOT NULL,
        byte_size               INTEGER,
        filing_date             TEXT,
        ingestion_timestamp     TEXT NOT NULL,
        page_count              INTEGER NOT NULL,
        is_machine_readable     INTEGER NOT NULL,
        requires_ocr            INTEGER NOT NULL,
        ingestion_status        TEXT NOT NULL,
        transaction_structure   TEXT,
        structure_confidence    REAL,
        structure_review_status TEXT DEFAULT 'unreviewed',
        ingestion_version       TEXT,
        schema_version          TEXT,
        run_id                  TEXT NOT NULL
    )
    """,
    # Checksum is the deduplication key: the same filing ingested under a
    # different filename must be recognised as the same document.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_checksum ON documents(checksum)",

    # -- Physical pages ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS document_pages (
        document_id       TEXT NOT NULL,
        pdf_page          INTEGER NOT NULL,
        printed_page      TEXT,
        printed_page_source TEXT,
        char_count        INTEGER NOT NULL,
        text_layer_status TEXT NOT NULL,
        content_hash      TEXT NOT NULL,
        has_images        INTEGER NOT NULL DEFAULT 0,
        text              TEXT,
        PRIMARY KEY (document_id, pdf_page),
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,

    # -- Layers and regions --------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS document_layers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id      TEXT NOT NULL,
        qualified_id     TEXT NOT NULL,
        layer_id         TEXT NOT NULL,
        label            TEXT,
        exhibit_number   TEXT,
        start_page       INTEGER NOT NULL,
        end_page         INTEGER NOT NULL,
        detection_method TEXT NOT NULL,
        evidence         TEXT,
        run_id           TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_regions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id  TEXT NOT NULL,
        layer_id     TEXT NOT NULL,
        region_id    TEXT NOT NULL,
        label        TEXT,
        start_page   INTEGER NOT NULL,
        end_page     INTEGER NOT NULL,
        run_id       TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,

    # -- Controls ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS integrity_issues (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL,
        kind        TEXT NOT NULL,
        severity    TEXT NOT NULL,
        pdf_pages   TEXT NOT NULL,
        detail      TEXT NOT NULL,
        run_id      TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS structure_evidence (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL,
        structure   TEXT NOT NULL,
        note        TEXT,
        weight      INTEGER,
        pdf_page    INTEGER,
        matched_text TEXT,
        run_id      TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,
    # -- Extracted fields (Workstream 2) -------------------------------------
    # One row per field PER LAYER. The same field extracted from the filing
    # summary and from the agreement produces two rows, deliberately: the
    # comparison between them is a deliverable, so collapsing them here would
    # destroy it before Workstream 3 ever runs.
    """
    CREATE TABLE IF NOT EXISTS extracted_fields (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id          TEXT NOT NULL,
        field_name           TEXT NOT NULL,
        category             TEXT,
        document_layer       TEXT,
        normalized_value     TEXT,
        value_json           TEXT,
        currency             TEXT,
        raw_value            TEXT,
        pdf_page             INTEGER,
        printed_page         TEXT,
        section              TEXT,
        evidence             TEXT,
        locator_uri          TEXT,
        extraction_method    TEXT NOT NULL,
        confidence           REAL NOT NULL,
        status               TEXT NOT NULL,
        review_status        TEXT NOT NULL,
        normalization_status TEXT,
        evidence_verified    INTEGER,
        is_critical          INTEGER NOT NULL DEFAULT 0,
        notes                TEXT,
        model_id             TEXT,
        prompt_version       TEXT,
        run_id               TEXT NOT NULL,
        UNIQUE (document_id, field_name, document_layer, run_id),
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fields_lookup ON extracted_fields(document_id, field_name)",
    "CREATE INDEX IF NOT EXISTS idx_fields_review ON extracted_fields(document_id, review_status)",
    """
    CREATE TABLE IF NOT EXISTS extraction_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id     TEXT NOT NULL,
        layer_id        TEXT NOT NULL,
        chunk_count     INTEGER,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER,
        model_id        TEXT,
        prompt_version  TEXT,
        run_id          TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents(document_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id            TEXT PRIMARY KEY,
        started_at        TEXT NOT NULL,
        ingestion_version TEXT,
        schema_version    TEXT,
        note              TEXT
    )
    """,
)


def get_connection(path: str = ":memory:") -> sqlite3.Connection:
    """
    Open a connection with row access by name and foreign keys enforced.

    Defaults to an in-memory database: each Streamlit session gets its own,
    so concurrent users cannot see one another's documents. Passing a path
    gives a durable database for CLI and test use.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Statuses are derived at ingestion and stored, so a change to how they are
# derived leaves earlier rows holding the old vocabulary. `blocked` became
# `ocr_required` when an unreadable page stopped being a whole-document
# refusal: it now describes the document rather than announcing a decision,
# and whether it stops a run depends on which layer the page falls in.
_MIGRATIONS = (
    "UPDATE documents SET ingestion_status = 'ocr_required' "
    "WHERE ingestion_status = 'blocked'",
)

# Columns added after rows already existed. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is attempted and its duplicate-column error ignored.
_ADDED_COLUMNS = (
    ("extraction_runs", "cache_creation_tokens", "INTEGER"),
)


def initialize_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """
    Create all tables and indexes, and bring stored values up to date.

    Safe to call repeatedly, which is what lets the app call it on every
    session against a database that may predate the current code.
    """
    cursor = conn.cursor()
    for statement in _STATEMENTS:
        cursor.execute(statement)
    for table, column, decl in _ADDED_COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # already present
    for statement in _MIGRATIONS:
        cursor.execute(statement)
    conn.commit()
    return conn
