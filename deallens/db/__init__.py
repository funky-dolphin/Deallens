"""SQLite persistence and audit record."""

from .repository import (
    document_exists,
    get_extracted_fields,
    get_review_queue,
    save_extraction,
    set_review_status,
    get_document,
    get_documents,
    get_integrity_issues,
    get_layers,
    get_page_text,
    get_regions,
    record_run,
    save_ingestion,
)
from .schema import SCHEMA_VERSION, get_connection, initialize_schema

__all__ = [
    "SCHEMA_VERSION",
    "document_exists",
    "get_connection",
    "get_document",
    "get_documents",
    "get_integrity_issues",
    "get_layers",
    "get_extracted_fields",
    "get_page_text",
    "get_review_queue",
    "get_regions",
    "initialize_schema",
    "record_run",
    "save_extraction",
    "save_ingestion",
    "set_review_status",
]
