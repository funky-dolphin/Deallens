#!/usr/bin/env python
"""
Price an extraction run without spending anything, or sending any request.

Reports exactly what `extract_document`'s spend gate will decide, because it
calls the same `estimate_run`. A separate estimator here would be a second
opinion that could disagree with the one that actually blocks the run, which
is worse than no estimate at all. Run as:

    .venv/bin/python scripts/estimate_cost.py "8k bio-techne.pdf"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deallens.extraction.extractor import estimate_run
from deallens.extraction.registry import fields_for
from deallens.ingestion import ingest


def main(path: str) -> int:
    pdf_path = Path(path)
    if not pdf_path.exists():
        print(f"no such file: {pdf_path}", file=sys.stderr)
        return 1

    ingestion = ingest(pdf_path.read_bytes(), pdf_path.name, run_id="estimate")
    specs = fields_for(ingestion.structure.structure)
    estimate = estimate_run(ingestion, specs)

    print(f"document  : {pdf_path.name}")
    print(f"structure : {ingestion.structure.structure}")
    print(f"fields    : {len(specs)} applicable")
    print(f"source    : {'text' if ingestion.inventory.is_machine_readable else 'page images'}")
    print(f"status    : {ingestion.integrity.ingestion_status}")
    if not ingestion.may_extract:
        print("\nExtraction is blocked for this document; no run would be priced.")
        return 2

    print()
    for layer in estimate.layers:
        print(
            f"  {layer.layer_id:<22} {layer.pages:>3} pages  "
            f"{layer.input_tokens:>9,} input tokens  "
            f"{layer.requests} request(s)  "
            f"${layer.cost_low:.2f}-${layer.cost_high:.2f}"
        )
    print(
        f"  {'TOTAL':<22} {'':>3}         {estimate.input_tokens:>9,} input tokens  "
        f"{estimate.requests} request(s)  "
        f"${estimate.cost_low:.2f}-${estimate.cost_high:.2f}"
    )
    print("\nRe-runs within the cache TTL read the document at ~10% of input cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "8k bio-techne.pdf"))
