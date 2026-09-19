#!/usr/bin/env python
"""
Estimate the API cost of extracting from a document, without spending anything.

Uses the token-counting endpoint, which is free, so a run can be priced before
it is authorised. Run as:

    .venv/bin/python scripts/estimate_cost.py "8k bio-techne.pdf"
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from dotenv import load_dotenv

from deallens.extraction.client import MODEL_ID, build_text_content, slice_pdf
from deallens.extraction.extractor import EXTRACTABLE_LAYERS
from deallens.extraction.prompts import SYSTEM_PROMPT, build_output_schema, build_user_prompt
from deallens.extraction.registry import fields_for
from deallens.ingestion import ingest

# Claude Opus 5 list pricing, USD per million tokens.
INPUT_PER_MTOK = 5.00
OUTPUT_PER_MTOK = 25.00


def main(path: str) -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    pdf_bytes = Path(path).read_bytes()
    ingestion = ingest(pdf_bytes, Path(path).name, run_id="estimate")
    specs = fields_for(ingestion.structure.structure)
    schema = build_output_schema(specs)
    client = anthropic.Anthropic()

    print(f"document  : {path}")
    print(f"structure : {ingestion.structure.structure}")
    print(f"fields    : {len(specs)} applicable\n")

    total_input = 0
    layers = [l for l in ingestion.layers if l.layer_id in EXTRACTABLE_LAYERS]
    for layer in layers:
        pages = layer.body_pages()
        if not pages:
            continue
        prompt = build_user_prompt(
            specs, layer.label, ingestion.structure.structure, (pages[0], pages[-1])
        )
        if ingestion.inventory.is_machine_readable:
            block = {
                "type": "text",
                "text": build_text_content(
                    [(page, ingestion.inventory.text_for(page)) for page in pages]
                ),
            }
        else:
            block = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(slice_pdf(pdf_bytes, pages)).decode("utf-8"),
                },
            }
        counted = client.messages.count_tokens(
            model=MODEL_ID,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}],
        ).input_tokens
        print(f"  {layer.qualified_id:<22} {len(pages):>3} pages  {counted:>9,} input tokens")
        total_input += counted

    # count_tokens does not account for output_config, so the schema is
    # approximated at the usual 4 characters per token.
    schema_tokens = len(json.dumps(schema)) // 4
    total_input += schema_tokens * len(layers)
    # The schema is re-sent with every request, not once per run -- which is
    # why a layer that has to be chunked costs more than its content alone.
    label = f"schema x{len(layers)} requests"
    print(f"  {label:<22} {'':>3}         {schema_tokens * len(layers):>9,} input tokens (approx)")
    print(f"  {'TOTAL INPUT':<22} {'':>3}         {total_input:>9,}\n")

    input_cost = total_input * INPUT_PER_MTOK / 1_000_000
    low_output, high_output = 40_000, 90_000
    mode = "text" if ingestion.inventory.is_machine_readable else "PDF page images"
    print(f"source mode: {mode}\n")
    print(f"input  : ${input_cost:.2f}")
    print(
        f"output : ${low_output * OUTPUT_PER_MTOK / 1e6:.2f} - "
        f"${high_output * OUTPUT_PER_MTOK / 1e6:.2f}   "
        f"({len(specs)} fields with evidence, plus adaptive thinking at high effort)"
    )
    print(
        f"TOTAL  : ${input_cost + low_output * OUTPUT_PER_MTOK / 1e6:.2f} - "
        f"${input_cost + high_output * OUTPUT_PER_MTOK / 1e6:.2f} per full run"
    )
    print("\nRe-runs within the cache TTL read the document at ~10% of input cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "8k bio-techne.pdf"))
