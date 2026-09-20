#!/usr/bin/env python
"""
Write the machine-readable extraction outputs (deliverable 15).

One JSON file per transaction in the assignment's required field-level shape,
plus one spreadsheet carrying the whole audit record. Reads the database and
recomputes nothing, so the files cannot disagree with the application.

    .venv/bin/python scripts/export_outputs.py [database] [output-dir]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deallens.db import get_connection, initialize_schema
from deallens.export import required_schema_json, workbook_bytes


def main(db_path: str = "deallens.db", out_dir: str = "outputs") -> int:
    if not Path(db_path).exists():
        print(f"no such database: {db_path}", file=sys.stderr)
        return 1

    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    conn = initialize_schema(get_connection(db_path))

    documents = list(conn.execute("SELECT document_id, filename FROM documents ORDER BY filename"))
    if not documents:
        print("no documents in the database; nothing to export", file=sys.stderr)
        return 2

    for doc in documents:
        stem = re.sub(r"[^a-z0-9]+", "-", Path(doc["filename"]).stem.lower()).strip("-")
        payload = required_schema_json(conn, doc["document_id"])
        path = out / f"{stem}_extracted_fields.json"
        path.write_text(payload)
        print(f"  {path}  ({payload.count(chr(10))} lines)")

    workbook = out / "deallens_audit_record.xlsx"
    workbook.write_bytes(workbook_bytes(conn))
    print(f"  {workbook}  ({workbook.stat().st_size / 1e6:.2f} MB)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1] if len(sys.argv) > 1 else "deallens.db",
            sys.argv[2] if len(sys.argv) > 2 else "outputs",
        )
    )
