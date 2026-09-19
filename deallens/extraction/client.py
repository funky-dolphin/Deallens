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

# The full field set with evidence quotes runs to roughly 20k output tokens;
# streaming avoids HTTP timeouts at this size and is required by the SDK for
# large ceilings.
MAX_OUTPUT_TOKENS = 32_000

# Hard API limits, which apply to base64 PDF document blocks only.
MAX_PAGES_PER_PDF_REQUEST = 600
MAX_REQUEST_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ModelProfile:
    """A model offered for extraction, and what it costs."""

    model_id: str
    label: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float


# Both take the same request shape -- 1M context, adaptive thinking, effort --
# so choosing between them is a price decision and nothing else. Pricing still
# has to follow the choice, because the estimate feeds the spend ceiling.
MODEL_PROFILES: dict[str, ModelProfile] = {
    "claude-opus-5": ModelProfile("claude-opus-5", "Opus 5", 5.00, 25.00),
    "claude-sonnet-5": ModelProfile("claude-sonnet-5", "Sonnet 5 (2.5x cheaper)", 2.00, 10.00),
}

# Recorded on every extracted field: Workstream 8 requires an output be
# traceable to the model that produced it, so whichever model ran is stamped.
DEFAULT_MODEL_ID = "claude-opus-5"
MODEL_ID = DEFAULT_MODEL_ID


def profile_for(model_id: str | None = None) -> ModelProfile:
    """The profile for a model id, refusing anything unrecognised."""
    resolved = model_id or DEFAULT_MODEL_ID
    if resolved not in MODEL_PROFILES:
        raise ValueError(
            f"Unknown model {resolved!r}. Extraction is configured for: "
            f"{', '.join(sorted(MODEL_PROFILES))}."
        )
    return MODEL_PROFILES[resolved]


# In text mode the context window, not a page count, is the binding
# constraint -- an important distinction, because page size varies by more
# than 20x within a single filing (217 to 4,379 characters on the development
# document), so a page-count limit corresponds to no particular quantity of
# content.
CONTEXT_WINDOW_TOKENS = 1_000_000

# Characters per token for dense legal prose, measured on the development
# filing: 315,982 characters of agreement body billed as 108,768 tokens.
# Notably denser than the ~4.0 rule of thumb for ordinary English; using that
# figure would understate a request by 27%, which is the wrong direction for
# a budget guard. Re-measure if extraction moves to a different model or a
# materially different document family.
CHARS_PER_TOKEN = 2.9

# Headroom against the estimate being wrong. Chunking is not free -- the
# schema is re-sent with every request -- so we want as few chunks as
# possible, but never one that overflows.
CONTEXT_SAFETY_MARGIN = 0.85

# Default (Opus 5) list pricing, USD per million tokens. Pricing for a run is
# taken from its ModelProfile; these remain for callers that only ever price
# the default.
INPUT_USD_PER_MTOK = MODEL_PROFILES[DEFAULT_MODEL_ID].input_usd_per_mtok
OUTPUT_USD_PER_MTOK = MODEL_PROFILES[DEFAULT_MODEL_ID].output_usd_per_mtok


def estimate_tokens(char_count: int) -> int:
    """Token estimate for a quantity of legal prose."""
    return int(char_count / CHARS_PER_TOKEN) + 1


def max_input_tokens(overhead_tokens: int = 0) -> int:
    """
    Largest input we will put in one request.

    Derived from the context window rather than a page count, and net of what
    the response and the per-request overhead will occupy.
    """
    available = CONTEXT_WINDOW_TOKENS - MAX_OUTPUT_TOKENS - overhead_tokens
    return int(available * CONTEXT_SAFETY_MARGIN)


def build_text_content(pages: list[tuple[int, str]]) -> str:
    """
    Render an excerpt as marked-up plain text.

    Each page is prefixed with its position WITHIN THE EXCERPT, which is what
    the prompt asks the model to report, so the page it returns maps back to
    the source with no arithmetic on either side.

    Sending text rather than a PDF document block is the single largest cost
    lever in this pipeline. A PDF block is billed as extracted text *and* a
    rendered image of every page; on the development filing that was ~2,700
    tokens per page against ~1,200 for the same content as text. We already
    hold the text -- ingestion extracted and verified it -- so paying to have
    the pages rendered and read again buys nothing on a machine-readable
    document.
    """
    return "\n\n".join(
        f"[PAGE {position}]\n{text}" for position, (_source_page, text) in enumerate(pages, 1)
    )


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
    system_prompt: str,
    user_prompt: str,
    output_schema: dict,
    document_text: str | None = None,
    pdf_bytes: bytes | None = None,
    model_id: str | None = None,
    profile: ModelProfile | None = None,
) -> ExtractionResponse:
    """
    One structured-extraction call against a PDF excerpt.

    Design choices worth stating:

      * Text is sent in preference to a PDF document block whenever ingestion
        found the document machine-readable, because a PDF block is billed for
        page images we do not need. `pdf_bytes` remains the path for documents
        whose text layer is incomplete, where the rendered page is the only
        way to read them. Exactly one of `document_text` / `pdf_bytes` is used.

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
        limitation across a 90-page agreement is not a lookup. Every model
        offered here accepts the same request shape, so only the model name
        changes between them.
    """
    if document_text is None and pdf_bytes is None:
        raise ValueError("extract_structured requires either document_text or pdf_bytes")

    profile = profile or profile_for(model_id)

    if document_text is not None:
        source_block = {
            "type": "text",
            "text": document_text,
            "cache_control": {"type": "ephemeral"},
        }
    else:
        source_block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode("utf-8"),
            },
            "cache_control": {"type": "ephemeral"},
        }

    with client.messages.stream(
        model=profile.model_id,
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
                "content": [source_block, {"type": "text", "text": user_prompt}],
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
        model_id=profile.model_id,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        stop_reason=message.stop_reason,
        request_id=getattr(message, "_request_id", None),
        warnings=warnings,
    )
