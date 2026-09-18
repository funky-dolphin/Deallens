"""
extractor.py
Sends PDF to Claude API and returns structured extraction results.
Handles arbitrarily large PDFs via chunked extraction with field merging.
"""

import anthropic
import base64
import hashlib
import json
import uuid
from datetime import datetime
from io import BytesIO

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


EXTRACTION_PROMPT = """
You are a financial document analyst specializing in M&A transaction agreements.

Analyze this document and extract the following fields. For each field, return a JSON object with exactly this structure:

{
  "field_name": "<field name>",
  "normalized_value": "<cleaned, normalized value or null if not found>",
  "currency": "<currency code if applicable, else null>",
  "raw_value": "<exact text from document>",
  "document_layer": "<8-k-summary | merger-agreement | exhibit | unknown>",
  "page": <page number as integer or null>,
  "section": "<section heading where found>",
  "evidence": "<direct quote from document supporting this value, max 200 chars>",
  "extraction_method": "llm",
  "confidence": <0.0 to 1.0>
}

If a field cannot be found IN THIS SECTION, return the object with normalized_value as null, confidence as 0.0, and evidence as null.

FIELDS TO EXTRACT:

Transaction Identity:
- target_company
- acquirer_company
- merger_subsidiary
- guarantors
- agreement_date
- transaction_type (merger | tender_offer | takeover | other)
- consideration_per_share
- consideration_currency
- total_transaction_value

Timing:
- expected_closing_date
- outside_date
- long_stop_date
- extension_conditions

Conditions:
- shareholder_approval_threshold
- antitrust_approvals_required
- financing_condition (yes | no | null)
- material_adverse_effect_condition

Termination:
- target_termination_fee
- parent_termination_fee
- fee_triggers

Financing:
- funding_sources
- bridge_financing_amount
- bridge_financing_currency
- debt_commitment

Return ONLY a valid JSON array of field objects. No explanation text outside the JSON.
"""

CHUNK_SIZE = 40  # pages per API call


def compute_checksum(pdf_bytes):
    """SHA-256 checksum of PDF bytes."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def split_pdf(pdf_bytes, chunk_size=CHUNK_SIZE):
    """
    Split a PDF into chunks of `chunk_size` pages.
    Returns list of (start_page, pdf_bytes_chunk) tuples.
    Page numbers are 0-indexed internally, 1-indexed in output metadata.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    chunks = []

    for start in range(0, total_pages, chunk_size):
        end = min(start + chunk_size, total_pages)
        writer = PdfWriter()
        for page_num in range(start, end):
            writer.add_page(reader.pages[page_num])

        buf = BytesIO()
        writer.write(buf)
        chunks.append((start + 1, buf.getvalue()))  # 1-indexed start page

    return chunks, total_pages


def parse_json_response(raw_response):
    """Strip markdown fences and parse JSON array from Claude response."""
    clean = raw_response.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        # parts[1] is the content between first pair of fences
        clean = parts[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()
    return json.loads(clean)


def extract_chunk(client, pdf_bytes_chunk, chunk_start_page):
    """
    Send one PDF chunk to Claude and return extracted fields.
    Adjusts page numbers to reflect position in the original document.
    """
    pdf_b64 = base64.standard_b64encode(pdf_bytes_chunk).decode("utf-8")

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT
                    }
                ]
            }
        ]
    )

    fields = parse_json_response(response.content[0].text)

    # Offset page numbers so they reflect position in the full document
    for field in fields:
        if field.get("page") is not None:
            field["page"] = field["page"] + chunk_start_page - 1

    return fields


def merge_fields(all_chunk_fields):
    """
    Merge field lists from multiple chunks.
    For each field_name, keep the result with the highest confidence.
    If two results tie, prefer the one with non-null normalized_value.
    """
    best = {}  # field_name -> best field dict

    for fields in all_chunk_fields:
        for field in fields:
            name = field.get("field_name")
            if not name:
                continue

            existing = best.get(name)
            if existing is None:
                best[name] = field
                continue

            existing_conf = existing.get("confidence", 0.0) or 0.0
            new_conf = field.get("confidence", 0.0) or 0.0
            existing_has_value = existing.get("normalized_value") not in (None, "null", "")
            new_has_value = field.get("normalized_value") not in (None, "null", "")

            # Prefer higher confidence; break ties by preferring non-null value
            if new_conf > existing_conf or (new_conf == existing_conf and new_has_value and not existing_has_value):
                best[name] = field

    return list(best.values())


def extract_from_pdf(pdf_bytes, filename, source_url=None, api_key=None, progress_callback=None):
    """
    Extract structured fields from a PDF of any size.
    Large PDFs are split into chunks; results are merged by highest confidence.

    Args:
        pdf_bytes: Raw PDF bytes
        filename: Original filename
        source_url: Optional source URL
        api_key: Anthropic API key
        progress_callback: Optional callable(chunk_num, total_chunks, message)

    Returns:
        dict with keys: document_meta, fields, run_id, error, page_count, chunk_count
    """
    run_id = str(uuid.uuid4())
    checksum = compute_checksum(pdf_bytes)
    document_id = f"doc_{checksum[:12]}"

    if not PYPDF_AVAILABLE:
        return {
            "document_meta": None,
            "fields": [],
            "run_id": run_id,
            "error": "pypdf is not installed. Run: pip install pypdf"
        }

    try:
        client = anthropic.Anthropic(api_key=api_key)

        # Split PDF into chunks
        chunks, total_pages = split_pdf(pdf_bytes, chunk_size=CHUNK_SIZE)
        total_chunks = len(chunks)

        if progress_callback:
            progress_callback(0, total_chunks, f"Split into {total_chunks} chunks ({total_pages} pages total)")

        # Extract from each chunk
        all_chunk_fields = []
        for i, (start_page, chunk_bytes) in enumerate(chunks):
            if progress_callback:
                progress_callback(i, total_chunks, f"Processing pages {start_page}–{min(start_page + CHUNK_SIZE - 1, total_pages)} (chunk {i+1}/{total_chunks})")

            chunk_fields = extract_chunk(client, chunk_bytes, start_page)
            all_chunk_fields.append(chunk_fields)

        if progress_callback:
            progress_callback(total_chunks, total_chunks, "Merging results...")

        # Merge: best result per field across all chunks
        merged_fields = merge_fields(all_chunk_fields)

        document_meta = {
            "document_id": document_id,
            "filename": filename,
            "source_url": source_url,
            "checksum": checksum,
            "ingestion_timestamp": datetime.utcnow().isoformat(),
            "is_machine_readable": True,
            "run_id": run_id,
            "page_count": total_pages,
            "chunk_count": total_chunks
        }

        return {
            "document_meta": document_meta,
            "fields": merged_fields,
            "run_id": run_id,
            "error": None,
            "page_count": total_pages,
            "chunk_count": total_chunks
        }

    except json.JSONDecodeError as e:
        return {
            "document_meta": None,
            "fields": [],
            "run_id": run_id,
            "error": f"JSON parsing error: {str(e)}"
        }
    except Exception as e:
        return {
            "document_meta": None,
            "fields": [],
            "run_id": run_id,
            "error": str(e)
        }
