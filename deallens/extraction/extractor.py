"""
Structured transaction extraction (Workstream 2).

Extraction runs **per document layer**, and results from different layers are
never merged. This is the central design change from the original scaffold,
which extracted across the whole filing and collapsed results with a
highest-confidence-wins merge.

That merge was wrong in a way that would not have been visible in the output.
A filing summary and the agreement it summarises frequently state the same
term differently -- rounded, simplified, or genuinely inconsistent -- and the
assignment requires those differences be surfaced and classified, not
resolved. Keeping one value and discarding the other destroys the very
evidence Workstream 3 exists to report, and produces a confident single answer
where the honest output is "these two sources disagree".

Within a single layer, extraction may still be split across chunks when the
layer exceeds the API's page limit. Those results *are* reconciled, because
they are readings of one document -- but conflicting readings are recorded as
conflicts rather than silently resolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..ingestion.classifier import DocumentLayer
from ..ingestion.locators import compute_anchor, verify_evidence
from ..ingestion.pipeline import IngestionResult
from . import models
from .client import (
    CHARS_PER_TOKEN,
    MAX_PAGES_PER_PDF_REQUEST,
    MODEL_ID,
    ExtractionResponse,
    ModelProfile,
    build_text_content,
    estimate_tokens,
    extract_structured,
    max_input_tokens,
    profile_for,
    slice_pdf,
)
from .models import ExtractedField
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_output_schema, build_user_prompt
from .registry import FINANCING, FieldSpec, fields_for, inapplicable_fields

# Layers worth extracting from, in the order they are reported. Constitutional
# documents, press releases and investor presentations are not sources of deal
# terms.
#
# `credit-agreement` was added after the Uber / Delivery Hero filing: its
# bridge facility is attached as Exhibit 10.1, 84 pages that state the bridge
# amount, maturity, interest basis and fees ten of our fields ask for. The
# original two-layer list was drawn around the development filing, whose only
# other exhibit was a two-page charter, and generalised "these two exhibit
# kinds are not sources" into "nothing else is a source". A financing
# agreement is a second operative contract, not supporting material.
EXTRACTABLE_LAYERS = ("filing-summary", "agreement", "credit-agreement")

# What each layer is a source *for*. A credit agreement has its own material
# adverse effect clause, its own conditions precedent and its own termination
# provisions -- all about the loan, not the merger. Extracting the full field
# set from it would return confident answers to the wrong questions, so it is
# scoped to the category it actually speaks to. `None` means every applicable
# field.
LAYER_FIELD_CATEGORIES: dict[str, tuple[str, ...] | None] = {
    "filing-summary": None,
    "agreement": None,
    "credit-agreement": (FINANCING,),
}


def _specs_for_layer(
    layer_id: str, specs: tuple[FieldSpec, ...]
) -> tuple[FieldSpec, ...]:
    """Narrow the field set to what a given layer is a source for."""
    categories = LAYER_FIELD_CATEGORIES.get(layer_id)
    if categories is None:
        return specs
    return tuple(spec for spec in specs if spec.category in categories)

# Rough output cost per request: 48 fields with evidence quotes, plus adaptive
# thinking at high effort. Used only for the pre-flight estimate.
OUTPUT_TOKENS_PER_REQUEST_LOW = 20_000
OUTPUT_TOKENS_PER_REQUEST_HIGH = 45_000


@dataclass
class LayerExtraction:
    """Fields extracted from one document layer."""

    layer_id: str
    layer_label: str
    fields: list[ExtractedField] = field(default_factory=list)
    chunk_count: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def found_count(self) -> int:
        return sum(1 for f in self.fields if f.status == models.FOUND)


@dataclass
class LayerEstimate:
    """
    Pre-flight size and cost estimate for one layer.

    Priced against the model that will actually run it. A cost gate that
    prices every model at the most expensive one's rates would block runs
    that are affordable, which is a failure in the other direction.
    """

    layer_id: str
    pages: int
    input_tokens: int
    requests: int
    profile: ModelProfile = field(default_factory=profile_for)

    def _cost(self, output_tokens_per_request: int) -> float:
        return (
            self.input_tokens * self.profile.input_usd_per_mtok
            + self.requests * output_tokens_per_request * self.profile.output_usd_per_mtok
        ) / 1_000_000

    @property
    def cost_low(self) -> float:
        return self._cost(OUTPUT_TOKENS_PER_REQUEST_LOW)

    @property
    def cost_high(self) -> float:
        return self._cost(OUTPUT_TOKENS_PER_REQUEST_HIGH)


@dataclass
class RunEstimate:
    """What a run is expected to cost, before any of it is spent."""

    layers: list[LayerEstimate] = field(default_factory=list)
    profile: ModelProfile = field(default_factory=profile_for)

    @property
    def input_tokens(self) -> int:
        return sum(l.input_tokens for l in self.layers)

    @property
    def requests(self) -> int:
        return sum(l.requests for l in self.layers)

    @property
    def cost_low(self) -> float:
        return sum(l.cost_low for l in self.layers)

    @property
    def cost_high(self) -> float:
        return sum(l.cost_high for l in self.layers)

    def describe(self) -> str:
        parts = [
            f"{l.layer_id}: {l.pages} pages, ~{l.input_tokens:,} tokens, "
            f"{l.requests} request(s)"
            for l in self.layers
        ]
        return (
            "; ".join(parts)
            + f" | total ~{self.input_tokens:,} input tokens across "
            f"{self.requests} request(s), ${self.cost_low:.2f}-${self.cost_high:.2f} "
            f"on {self.profile.model_id}"
        )


@dataclass
class ExtractionRun:
    """Complete extraction across every extractable layer of one document."""

    document_id: str
    run_id: str
    model_id: str
    prompt_version: str
    layers: list[LayerExtraction] = field(default_factory=list)
    fields: list[ExtractedField] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimate: "RunEstimate | None" = None
    error: str | None = None

    @property
    def total_input_tokens(self) -> int:
        return sum(l.input_tokens for l in self.layers)

    @property
    def total_output_tokens(self) -> int:
        return sum(l.output_tokens for l in self.layers)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(l.cache_read_tokens for l in self.layers)

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.fields:
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts


def _chunk_pages(
    ingestion: IngestionResult, pages: list[int], overhead_tokens: int
) -> tuple[list[list[int]], list[str]]:
    """
    Split a layer's pages into requests that fit the model's context.

    Chunking is by estimated token count, not page count. Page size varies by
    more than 20x within a single filing, so a page-count threshold
    corresponds to no fixed quantity of content: the same "400 pages" may be
    120,000 tokens or 600,000. In PDF mode a hard 600-page API limit also
    applies and is enforced alongside the token budget.

    `overhead_tokens` is what each request spends on the schema and the
    prompt before any document text: it comes off the budget because the
    pages have to fit alongside it, not instead of it.

    Fewer chunks is strictly better -- that overhead is re-sent with each one
    -- so pages are packed greedily up to the budget.
    """
    warnings: list[str] = []
    text_mode = ingestion.inventory.is_machine_readable
    budget = max_input_tokens(overhead_tokens)
    page_limit = len(pages) if text_mode else MAX_PAGES_PER_PDF_REQUEST

    chunks: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0

    for page in pages:
        page_tokens = estimate_tokens(ingestion.inventory.page(page).char_count)
        if page_tokens > budget:
            warnings.append(
                f"PDF page {page} alone is an estimated {page_tokens:,} tokens, "
                f"above the {budget:,}-token request budget. Sent as its own "
                "request; the response may be truncated."
            )
        over_budget = current and current_tokens + page_tokens > budget
        over_pages = current and len(current) >= page_limit
        if over_budget or over_pages:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(page)
        current_tokens += page_tokens

    if current:
        chunks.append(current)
    return (chunks or [[]]), warnings


def _payloads_by_field(
    data: dict, specs: tuple[FieldSpec, ...], warnings: list[str]
) -> dict[str, list[dict]]:
    """
    Index one response's records by the field each claims to answer.

    The schema guarantees the shape of a record and the spelling of its name,
    but it cannot require one record per field -- see the note in `prompts.py`.
    Completeness is therefore checked here: a field with no record is reported
    by the caller as unresolved, and a field with several is reconciled rather
    than having one reading silently win.
    """
    entries = data.get("fields")
    if not isinstance(entries, list):
        warnings.append(
            "Model response had no 'fields' list; every field in this request "
            "is reported unresolved."
        )
        return {}

    known = {spec.name for spec in specs}
    by_name: dict[str, list[dict]] = {}
    unknown: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("field_name")
        if name not in known:
            unknown.append(str(name))
            continue
        by_name.setdefault(name, []).append(entry)

    if unknown:
        # The enum should make this impossible. If it happens anyway, the
        # schema is not doing what we think it is, and that is worth saying.
        warnings.append(
            f"Model returned {len(unknown)} record(s) for field name(s) outside "
            f"the requested set ({', '.join(sorted(set(unknown))[:5])}); ignored."
        )
    return by_name


def _record_from_payload(
    field_name: str,
    payload: dict,
    ingestion: IngestionResult,
    layer: DocumentLayer,
    page_map: list[int],
    model_id: str = MODEL_ID,
) -> ExtractedField:
    """
    Turn one field's raw model output into an ExtractedField with provenance.

    The model reports a page counted within the excerpt it was given;
    `page_map` translates that back to a page of the source PDF. Getting this
    wrong would produce citations that look precise and point at the wrong
    page, so an out-of-range page is treated as no page at all rather than
    clamped to a plausible one.
    """
    record = ExtractedField(
        field_name=field_name,
        document_id=ingestion.document_id,
        run_id=ingestion.run_id,
        document_layer=layer.qualified_id,
        extraction_method="llm",
        model_id=model_id,
        prompt_version=PROMPT_VERSION,
    )

    if not payload.get("found"):
        record.status = models.NOT_FOUND
        record.confidence = 0.0
        return record

    record.raw_value = (payload.get("raw_value") or "").strip() or None
    record.evidence = (payload.get("evidence") or "").strip() or None
    record.section = (payload.get("section") or "").strip() or None
    try:
        record.confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        record.confidence = 0.0
        record.add_note("Model returned a non-numeric confidence; treated as 0.")
    record.confidence = min(max(record.confidence, 0.0), 1.0)

    excerpt_page = payload.get("page") or 0
    if isinstance(excerpt_page, int) and 1 <= excerpt_page <= len(page_map):
        record.pdf_page = page_map[excerpt_page - 1]
    else:
        record.add_note(
            f"Model reported page {excerpt_page!r}, which is outside this "
            f"excerpt of {len(page_map)} page(s). No page recorded."
        )

    if record.pdf_page is not None:
        locator = ingestion.locator_for(
            record.pdf_page, section=record.section, evidence=record.evidence
        )
        record.locator_uri = locator.to_uri()
        record.printed_page = locator.printed_page

    return record


def _finalise(record: ExtractedField, ingestion: IngestionResult) -> ExtractedField:
    """Apply normalization and the fail-closed controls, verifying evidence."""
    page_text = (
        ingestion.inventory.text_for(record.pdf_page)
        if record.pdf_page is not None
        else None
    )
    return models.finalise(
        record,
        verify_evidence_fn=verify_evidence if page_text is not None else None,
        page_text=page_text,
    )


def reconcile_within_layer(
    candidates: list[ExtractedField], field_name: str
) -> ExtractedField:
    """
    Combine several chunks' readings of the same field within one layer.

    Agreement on the normalized value is treated as corroboration and the
    best-evidenced reading is kept. Disagreement is recorded as a conflict:
    the value is withheld, both readings are preserved in the notes, and the
    field is routed to review. A critical field never auto-resolves.
    """
    answerable = [c for c in candidates if c.is_answerable]
    if not answerable:
        # Prefer an informative failure over a bare not_found.
        for status in (models.UNRESOLVED, models.NOT_FOUND):
            match = next((c for c in candidates if c.status == status), None)
            if match:
                return match
        return candidates[0]

    distinct = {}
    for candidate in answerable:
        distinct.setdefault(str(candidate.normalized_value), []).append(candidate)

    if len(distinct) == 1:
        best = max(answerable, key=lambda c: c.confidence)
        if len(answerable) > 1:
            best.add_note(
                f"Corroborated by {len(answerable)} independent readings within this layer."
            )
        return best

    best = max(answerable, key=lambda c: c.confidence)
    conflict = ExtractedField(
        field_name=field_name,
        document_id=best.document_id,
        run_id=best.run_id,
        document_layer=best.document_layer,
        raw_value=best.raw_value,
        evidence=best.evidence,
        section=best.section,
        pdf_page=best.pdf_page,
        printed_page=best.printed_page,
        locator_uri=best.locator_uri,
        extraction_method="llm",
        model_id=best.model_id,
        prompt_version=best.prompt_version,
        confidence=best.confidence,
        status=models.CONFLICT,
        review_status=models.EXCEPTION,
        normalized_value=None,
    )
    conflict.add_note(
        "Conflicting values found within the same layer; value withheld pending review."
    )
    for value, group in sorted(distinct.items()):
        pages = ", ".join(str(c.pdf_page) for c in group if c.pdf_page)
        conflict.add_note(f"Reading: {value!r} (PDF page {pages or 'unknown'})")
    return conflict


def estimate_run(
    ingestion: IngestionResult,
    specs: tuple[FieldSpec, ...] | None = None,
    layer_ids: tuple[str, ...] = EXTRACTABLE_LAYERS,
    model_id: str | None = None,
) -> RunEstimate:
    """
    Size and price a run without making any API call.

    Exists so spend is a decision rather than a surprise. Everything it needs
    -- page inventory, character counts, layer boundaries -- is already known
    from ingestion, so the estimate is free and can gate the run.

    The schema and prompt are counted once per request, not once per run: both
    are re-sent with every call, so a layer split across three chunks pays for
    them three times. That is also why chunking is something to avoid, not
    merely tolerate.

    The prompt is measured rather than guessed at, because the field catalogue
    inside it is the larger half of the per-request overhead.

    Pricing follows the chosen model, because the estimate feeds the spend
    ceiling.
    """
    specs = specs if specs is not None else fields_for(ingestion.structure.structure)
    schema_tokens = estimate_tokens(len(json.dumps(build_output_schema(specs))))
    profile = profile_for(model_id)

    estimate = RunEstimate(profile=profile)
    for layer in [l for l in ingestion.layers if l.layer_id in layer_ids]:
        pages = layer.body_pages()
        if not pages:
            continue
        prompt_tokens = estimate_tokens(
            len(SYSTEM_PROMPT)
            + len(
                build_user_prompt(
                    _specs_for_layer(layer.layer_id, specs),
                    layer_label=f"{layer.label} ({layer.qualified_id})",
                    structure=ingestion.structure.structure,
                    page_range=(pages[0], pages[-1]),
                )
            )
        )
        chunks, _ = _chunk_pages(ingestion, pages, schema_tokens + prompt_tokens)
        content_tokens = estimate_tokens(
            sum(ingestion.inventory.page(p).char_count for p in pages)
        )
        estimate.layers.append(
            LayerEstimate(
                layer_id=layer.qualified_id,
                pages=len(pages),
                input_tokens=content_tokens + len(chunks) * (schema_tokens + prompt_tokens),
                requests=len(chunks),
                profile=profile,
            )
        )
    return estimate


def extract_layer(
    client,
    ingestion: IngestionResult,
    layer: DocumentLayer,
    pdf_bytes: bytes,
    specs: tuple[FieldSpec, ...],
    profile: ModelProfile | None = None,
) -> LayerExtraction:
    """Extract every applicable field from one document layer."""
    profile = profile or profile_for()
    result = LayerExtraction(layer_id=layer.qualified_id, layer_label=layer.label)

    pages = layer.body_pages()
    if not pages:
        result.warnings.append(f"Layer {layer.qualified_id} has no body pages to extract from.")
        return result

    schema = build_output_schema(specs)
    overhead_tokens = estimate_tokens(
        len(json.dumps(schema))
        + len(SYSTEM_PROMPT)
        + len(
            build_user_prompt(
                specs,
                layer_label=f"{layer.label} ({layer.qualified_id})",
                structure=ingestion.structure.structure,
                page_range=(pages[0], pages[-1]),
            )
        )
    )
    chunks, chunk_warnings = _chunk_pages(ingestion, pages, overhead_tokens)
    result.chunk_count = len(chunks)
    result.warnings.extend(chunk_warnings)
    if len(chunks) > 1:
        layer_tokens = estimate_tokens(
            sum(ingestion.inventory.page(p).char_count for p in pages)
        )
        result.warnings.append(
            f"Layer {layer.qualified_id} is an estimated {layer_tokens:,} tokens "
            f"across {len(pages)} pages and was split into {len(chunks)} requests. "
            "Values appearing in more than one chunk are reconciled; conflicting "
            "readings are reported rather than resolved."
        )
    per_chunk: list[list[ExtractedField]] = []

    for index, page_map in enumerate(chunks):
        prompt = build_user_prompt(
            specs,
            layer_label=f"{layer.label} ({layer.qualified_id})",
            structure=ingestion.structure.structure,
            page_range=(page_map[0], page_map[-1]),
        )
        # Machine-readable documents go as text; the rest fall back to page
        # images. Ingestion already made this determination, so extraction
        # does not re-litigate it.
        if ingestion.inventory.is_machine_readable:
            source = {
                "document_text": build_text_content(
                    [(page, ingestion.inventory.text_for(page)) for page in page_map]
                )
            }
        else:
            source = {"pdf_bytes": slice_pdf(pdf_bytes, page_map)}

        response: ExtractionResponse = extract_structured(
            client,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            output_schema=schema,
            profile=profile,
            **source,
        )
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        result.cache_read_tokens += response.cache_read_tokens
        result.warnings.extend(response.warnings)

        payloads = _payloads_by_field(response.data, specs, result.warnings)
        chunk_fields = []
        for spec in specs:
            entries = payloads.get(spec.name, [])
            if not entries:
                record = ExtractedField(
                    field_name=spec.name,
                    document_id=ingestion.document_id,
                    run_id=ingestion.run_id,
                    document_layer=layer.qualified_id,
                    status=models.UNRESOLVED,
                    review_status=models.EXCEPTION,
                    model_id=profile.model_id,
                    prompt_version=PROMPT_VERSION,
                )
                record.add_note("Model omitted this field from its response.")
                chunk_fields.append(record)
                continue

            readings = [
                _finalise(
                    _record_from_payload(
                        spec.name, entry, ingestion, layer, page_map, profile.model_id
                    ),
                    ingestion,
                )
                for entry in entries
            ]
            if len(readings) == 1:
                chunk_fields.append(readings[0])
                continue
            # The model answered the same field more than once. Treat the
            # answers as competing readings rather than picking one: agreement
            # is corroboration, disagreement goes to review.
            merged = reconcile_within_layer(readings, spec.name)
            merged.add_note(
                f"Model returned {len(readings)} records for this field in one response."
            )
            chunk_fields.append(merged)
        per_chunk.append(chunk_fields)

    if len(per_chunk) == 1:
        result.fields = per_chunk[0]
    else:
        by_name: dict[str, list[ExtractedField]] = {}
        for chunk_fields in per_chunk:
            for record in chunk_fields:
                by_name.setdefault(record.field_name, []).append(record)
        result.fields = [
            reconcile_within_layer(candidates, name) for name, candidates in by_name.items()
        ]

    return result


def _qualify_absences(extraction: LayerExtraction, unreadable: list[int]) -> None:
    """
    Mark every absence from a layer we could not read in full.

    A field reported `not_found` normally means the layer does not state it.
    Where part of that layer is unreadable, the honest claim is weaker: we did
    not find it in the part we could read. Leaving the two indistinguishable
    is how a hole in the source becomes a finding about the agreement.
    """
    if not unreadable:
        return
    pages = ", ".join(str(p) for p in unreadable)
    extraction.warnings.append(
        f"Layer {extraction.layer_id} contains {len(unreadable)} unreadable "
        f"page(s) ({pages}); absences below are qualified."
    )
    for record in extraction.fields:
        if record.status == models.NOT_FOUND:
            record.add_note(
                f"Not found in the readable part of this layer. Page(s) {pages} "
                "could not be read and may state this field."
            )


def extract_document(
    client,
    ingestion: IngestionResult,
    pdf_bytes: bytes,
    layer_ids: tuple[str, ...] = EXTRACTABLE_LAYERS,
    max_cost_usd: float | None = None,
    model_id: str | None = None,
) -> ExtractionRun:
    """
    Extract structured fields from every extractable layer of one document.

    An unreadable page stops nothing on its own. What matters is whether it
    falls in a layer this run reads; where it does, the run proceeds and every
    absence from that layer is qualified rather than reported as silence.

    `max_cost_usd` is a spend ceiling checked before the first request. A
    filing several times larger than expected, or one whose layers were
    mis-segmented so the whole document landed in a single layer, would
    otherwise be discovered only on the invoice.

    `model_id` selects the model. It is recorded on the run and on every field
    the run produces, because Workstream 8 requires an output be traceable to
    the model that made it -- and a field extracted by Haiku that claims Opus
    produced it is worse than one with no attribution at all. An unrecognised
    id raises rather than falling back.
    """
    profile = profile_for(model_id)
    run = ExtractionRun(
        document_id=ingestion.document_id,
        run_id=ingestion.run_id,
        model_id=profile.model_id,
        prompt_version=PROMPT_VERSION,
    )

    # An unreadable page only bears on this run if it sits in a layer this run
    # reads. One in an investor presentation has no bearing on the agreement,
    # and refusing the document over it declines a contract because a chart is
    # a picture. Where a page IS in scope, extraction proceeds rather than
    # stopping -- a partial result a reader can trust the boundaries of beats
    # no result -- but every absence from the affected layer is qualified,
    # because "not found" and "not found, and we could not read everything"
    # are different claims.
    blocking = ingestion.unreadable_pages_in(layer_ids)
    out_of_scope = sorted(
        set(ingestion.integrity.unreadable_pages) - set(blocking)
    )
    if out_of_scope:
        run.warnings.append(
            f"{len(out_of_scope)} unreadable page(s) "
            f"({', '.join(str(p) for p in out_of_scope[:8])}) require OCR but "
            "fall outside the layers being extracted, so they do not affect "
            "this run."
        )
    if blocking:
        run.warnings.append(
            f"INCOMPLETE SOURCE: {len(blocking)} page(s) "
            f"({', '.join(str(p) for p in blocking[:8])}) inside the extracted "
            "layers could not be read and require OCR. Fields reported as not "
            "found may be stated on those pages."
        )

    structure = ingestion.structure.structure
    specs = fields_for(structure)

    targets = [l for l in ingestion.layers if l.layer_id in layer_ids]
    if not targets:
        run.error = "No extractable layer was identified in this document."
        return run

    run.estimate = estimate_run(ingestion, specs, layer_ids, profile.model_id)
    if max_cost_usd is not None and run.estimate.cost_high > max_cost_usd:
        run.error = (
            f"Extraction halted before any request: estimated cost "
            f"${run.estimate.cost_low:.2f}-${run.estimate.cost_high:.2f} exceeds "
            f"the ${max_cost_usd:.2f} ceiling. {run.estimate.describe()}"
        )
        return run

    unreadable_by_layer = {
        layer.qualified_id: sorted(set(blocking) & set(layer.body_pages()))
        for layer in targets
    }

    for layer in targets:
        try:
            extraction = extract_layer(
                client, ingestion, layer, pdf_bytes,
                _specs_for_layer(layer.layer_id, specs), profile,
            )
            _qualify_absences(extraction, unreadable_by_layer.get(layer.qualified_id, []))
        except Exception as exc:
            run.warnings.append(f"Layer {layer.qualified_id} failed: {exc}")
            continue
        run.layers.append(extraction)
        run.fields.extend(extraction.fields)
        run.warnings.extend(extraction.warnings)

    # Fields that do not exist in this kind of transaction are recorded
    # explicitly, once, so an export shows the complete field set and a reader
    # can distinguish "absent" from "does not apply here".
    for spec in inapplicable_fields(structure):
        run.fields.append(
            models.not_applicable_field(
                spec.name, ingestion.document_id, ingestion.run_id, structure
            )
        )

    if not run.layers:
        run.error = "Extraction produced no results for any layer."

    return run
