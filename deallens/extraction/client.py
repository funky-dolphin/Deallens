"""
Anthropic API access for extraction.

Isolated behind a small surface so that model choice, request shape, caching
and retry policy are configured in exactly one place -- and so the extraction
pipeline can be tested without a network call by substituting a fake.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader, PdfWriter

# Recorded on every extracted field. Workstream 8 requires an output be
# traceable to the model that produced it.
MODEL_ID = "claude-opus-5"

# The full field set with evidence quotes runs to roughly 20k output tokens;
# streaming avoids HTTP timeouts at this size and is required by the SDK for
# large ceilings.
MAX_OUTPUT_TOKENS = 32_000

# API limits for a base64 PDF document block.
MAX_PAGES_PER_REQUEST = 600
MAX_REQUEST_BYTES = 32 * 1024 * 1024


@dataclass
class ExtractionResponse:
    """Raw result of one extraction call."""

    data: dict
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stop_reason: str | None = None
    request_id: str | None = None
    warnings: list[str] = field(default_factory=list)


def slice_pdf(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """
    Build a new PDF containing only `pages` (1-based), in the order given.

    Used to send the model exactly one document layer. Sending the whole
    filing and naming a page range in the prompt would be cheaper to code and
    worse in practice: the model can and does cite across the boundary, which
    is precisely what Workstream 3 needs kept apart.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page_number in pages:
        writer.add_page(reader.pages[page_number - 1])
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def extract_structured(
    client,
    pdf_bytes: bytes,
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    model_id: str = MODEL_ID,
) -> ExtractionResponse:
    """
    One structured-extraction call against a PDF excerpt.

    Design choices worth stating:

      * Structured outputs rather than parsing JSON out of prose. The previous
        implementation stripped markdown fences and called `json.loads`, which
        fails whenever the model adds a sentence of preamble. The schema makes
        the response shape a server-side guarantee.

      * Native citations are deliberately not used. They are incompatible with
        structured outputs (the API returns 400 if both are set), and the
        schema is worth more here: we verify the model's page attribution
        ourselves against stored page text, which catches the same error and
        also catches a quote that does not appear in the document at all.

      * The document block and system prompt are cached. Each layer is queried
        once per run today, but Q&A and re-extraction hit the same prefix, and
        the document dominates the token count.

      * Adaptive thinking at high effort. Locating a burdensome-condition
        limitation across a 90-page agreement is not a lookup.
    """
    encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    with client.messages.stream(
        model=model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": user_prompt},
                ],
            }
        ],
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": output_schema},
        },
    ) as stream:
        message = stream.get_final_message()

    warnings: list[str] = []
    if message.stop_reason == "max_tokens":
        warnings.append(
            "Response hit the output token ceiling and may be truncated; "
            "fields near the end of the schema may be missing."
        )
    if message.stop_reason == "refusal":
        raise RuntimeError(
            "The model declined this request "
            f"({getattr(message.stop_details, 'category', 'unknown')})."
        )

    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise RuntimeError("No text block in response; cannot read structured output.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Should be unreachable with structured outputs, but a schema-guaranteed
        # response that will not parse is a control failure worth surfacing
        # loudly rather than swallowing.
        raise RuntimeError(f"Structured output did not parse as JSON: {exc}") from exc

    usage = message.usage
    return ExtractionResponse(
        data=data,
        model_id=model_id,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        stop_reason=message.stop_reason,
        request_id=getattr(message, "_request_id", None),
        warnings=warnings,
    )
