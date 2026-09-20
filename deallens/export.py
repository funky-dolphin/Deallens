"""
Exportable audit record (Workstream 8, deliverable 15).

Two shapes, for two readers.

`required_schema_json` emits the exact field-level record the assignment
specifies -- `field_name`, `normalized_value`, `currency`, `raw_value`,
`document_id`, `document_layer`, `page`, `section`, `evidence`,
`extraction_method`, `confidence`, `review_status`, `run_id` -- and nothing
else. It is the machine-readable output, and its shape is fixed by the
assignment rather than by us.

`workbook_bytes` emits the whole database as a spreadsheet, one sheet per
table, plus the derived analyses. That is for a person: an analyst checking a
citation wants to sort, filter and read across tables, and a directory of
JSON files is a poor way to do that. It is a convenience over the same data,
not a second source of truth.

Nothing here recomputes anything. Both read what extraction already wrote, so
an export cannot disagree with the application that produced it.
"""

from __future__ import annotations

import io
import json
import sqlite3

import pandas as pd

from .analytics.hedging import (
    ADDITIONAL_ASSUMPTIONS,
    ASSUMPTIONS_VERSION,
    BIO_TECHNE_ASSUMPTIONS,
    deal_from_rows,
    run_scenarios,
)
from .comparison import compare_layers
from .db.repository import get_extracted_fields
from .timeline import build_timeline

# Every table in the schema, in the order a reader would work through them:
# what the document is, what is in it, what we extracted, what it cost.
TABLES = (
    "documents",
    "document_layers",
    "document_pages",
    "document_regions",
    "integrity_issues",
    "structure_evidence",
    "extracted_fields",
    "extraction_runs",
    "runs",
)

# Excel refuses a cell over 32,767 characters. Stored page text is normally
# 2-9k, but a dense exhibit page can exceed it, and a failed export is worse
# than a truncated cell that says it was truncated.
_CELL_LIMIT = 32_000

# The assignment's field-level output keys, in its order.
REQUIRED_KEYS = (
    "field_name",
    "normalized_value",
    "currency",
    "raw_value",
    "document_id",
    "document_layer",
    "page",
    "section",
    "evidence",
    "extraction_method",
    "confidence",
    "review_status",
    "run_id",
)


def _truncate(value):
    if isinstance(value, str) and len(value) > _CELL_LIMIT:
        return value[:_CELL_LIMIT] + f"… [truncated from {len(value):,} characters]"
    return value


def required_schema_records(
    conn: sqlite3.Connection, document_id: str
) -> list[dict]:
    """
    Every extracted field in the assignment's required output shape.

    `page` is the printed page where one was reconciled, falling back to the
    PDF page, because that is the number a reader would cite. The unambiguous
    pair is in the full record and in the locator URI.
    """
    records = []
    for row in get_extracted_fields(conn, document_id):
        records.append(
            {
                "field_name": row["field_name"],
                "normalized_value": row["normalized_value"],
                "currency": row["currency"],
                "raw_value": row["raw_value"],
                "document_id": row["document_id"],
                "document_layer": row["document_layer"],
                "page": row["printed_page"] or row["pdf_page"],
                "section": row["section"],
                "evidence": row["evidence"],
                "extraction_method": row["extraction_method"],
                "confidence": row["confidence"],
                "review_status": row["review_status"],
                "run_id": row["run_id"],
            }
        )
    return records


def required_schema_json(conn: sqlite3.Connection, document_id: str) -> str:
    return json.dumps(required_schema_records(conn, document_id), indent=2, default=str)


def _derived_frames(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """
    The analyses built on top of the tables.

    Comparison, timeline and scenarios are computed on demand rather than
    stored, so they have no table of their own -- but they are what a reviewer
    is here to read, and deliverables 17 and 18 ask for them. They are
    rebuilt at export time from the same rows the application uses.
    """
    comparisons, timelines, scenarios = [], [], []
    for doc in conn.execute("SELECT document_id, filename FROM documents"):
        rows = get_extracted_fields(conn, doc["document_id"])
        if not rows:
            continue
        label = doc["filename"]

        for comparison in compare_layers(rows):
            comparisons.append({"filename": label, **comparison.to_dict()})

        timeline = build_timeline(rows)
        for entry in timeline.anchored + timeline.unanchored:
            timelines.append({"filename": label, **entry.to_dict()})

        for result in run_scenarios(
            horizon=timeline.horizon, deal=deal_from_rows(rows)
        ):
            scenarios.append({"filename": label, **result.to_dict()})

    return {
        "comparison": pd.DataFrame(comparisons),
        "timeline": pd.DataFrame(timelines),
        "hedging_scenarios": pd.DataFrame(scenarios),
    }


def _assumptions_frame() -> pd.DataFrame:
    """The synthetic inputs, so a figure can be traced to what produced it."""
    rows = [
        {"group": group, "assumption": name, "value": value, "why_required": "assignment-supplied"}
        for group, block in BIO_TECHNE_ASSUMPTIONS.items()
        for name, value in block.items()
    ]
    rows += [
        {"group": "additional", "assumption": name, "value": value, "why_required": reason}
        for name, (value, reason) in ADDITIONAL_ASSUMPTIONS.items()
    ]
    return pd.DataFrame(rows)


def workbook_bytes(conn: sqlite3.Connection) -> bytes:
    """
    The whole audit record as one spreadsheet, a sheet per table.

    Sheet order follows how a reviewer works: the documents, what ingestion
    found in them, the extracted fields, what the run cost, then the analyses
    built on top and the assumptions behind them.
    """
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for table in TABLES:
            frame = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            frame = frame.map(_truncate)
            # Excel caps a sheet name at 31 characters.
            frame.to_excel(writer, sheet_name=table[:31], index=False)

        for name, frame in _derived_frames(conn).items():
            # Written even when empty, so the workbook has the same shape
            # every time. A missing sheet reads as "not produced"; a sheet
            # saying nothing was produced is a different, truer statement.
            if frame.empty:
                frame = pd.DataFrame(
                    [{"note": f"No {name.replace('_', ' ')} rows for the documents in this database."}]
                )
            frame.map(_truncate).to_excel(writer, sheet_name=name[:31], index=False)

        _assumptions_frame().to_excel(
            writer, sheet_name="assumptions", index=False
        )
        pd.DataFrame(
            [{"assumptions_version": ASSUMPTIONS_VERSION}]
        ).to_excel(writer, sheet_name="versions", index=False)

    return buffer.getvalue()
