"""
database.py
Handles SQLite setup and all read/write operations.
Each Streamlit session gets its own in-memory database.
"""

import sqlite3
import json
from datetime import datetime


def get_connection():
    """Create an in-memory SQLite database for this session."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_schema(conn):
    """Create all tables."""
    cursor = conn.cursor()

    # Document registry — one row per ingested PDF
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT UNIQUE NOT NULL,
            source_url TEXT,
            filename TEXT,
            checksum TEXT,
            filing_date TEXT,
            ingestion_timestamp TEXT,
            transaction_type TEXT,
            page_count INTEGER,
            is_machine_readable BOOLEAN,
            run_id TEXT
        )
    """)

    # Extracted fields — one row per extracted field per document
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS extracted_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            normalized_value TEXT,
            currency TEXT,
            raw_value TEXT,
            document_layer TEXT,
            page INTEGER,
            section TEXT,
            evidence TEXT,
            extraction_method TEXT,
            confidence REAL,
            review_status TEXT DEFAULT 'unreviewed',
            run_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES documents(document_id)
        )
    """)

    # Comparison results — filing summary vs agreement
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS field_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            summary_value TEXT,
            summary_page INTEGER,
            agreement_value TEXT,
            agreement_page INTEGER,
            comparison_status TEXT,  -- match, conflict, summary_only, etc.
            run_id TEXT
        )
    """)

    # Hedging scenarios
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hedging_scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            scenario_name TEXT NOT NULL,
            strategy TEXT NOT NULL,
            rate_shift_bps REAL,
            credit_spread_shift_bps REAL,
            dv01_exposure REAL,
            pnl_impact REAL,
            assumptions TEXT,  -- JSON blob
            run_id TEXT
        )
    """)

    conn.commit()
    return conn


def insert_document(conn, doc_meta):
    """Insert a document record."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO documents
        (document_id, source_url, filename, checksum, filing_date,
         ingestion_timestamp, transaction_type, page_count, is_machine_readable, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        doc_meta.get("document_id"),
        doc_meta.get("source_url"),
        doc_meta.get("filename"),
        doc_meta.get("checksum"),
        doc_meta.get("filing_date"),
        datetime.utcnow().isoformat(),
        doc_meta.get("transaction_type"),
        doc_meta.get("page_count"),
        doc_meta.get("is_machine_readable", True),
        doc_meta.get("run_id")
    ))
    conn.commit()


def insert_extracted_fields(conn, document_id, fields, run_id):
    """Insert a list of extracted field dicts."""
    cursor = conn.cursor()
    for field in fields:
        cursor.execute("""
            INSERT INTO extracted_fields
            (document_id, field_name, normalized_value, currency, raw_value,
             document_layer, page, section, evidence, extraction_method, confidence, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            document_id,
            field.get("field_name"),
            str(field.get("normalized_value", "")),
            field.get("currency"),
            field.get("raw_value"),
            field.get("document_layer"),
            field.get("page"),
            field.get("section"),
            field.get("evidence"),
            field.get("extraction_method", "llm"),
            field.get("confidence", 0.0),
            run_id
        ))
    conn.commit()


def get_all_fields(conn, document_id):
    """Retrieve all extracted fields for a document."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM extracted_fields WHERE document_id = ?
        ORDER BY field_name
    """, (document_id,))
    return [dict(row) for row in cursor.fetchall()]


def get_documents(conn):
    """Retrieve all ingested documents."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM documents ORDER BY ingestion_timestamp DESC")
    return [dict(row) for row in cursor.fetchall()]
