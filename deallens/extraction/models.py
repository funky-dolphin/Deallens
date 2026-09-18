"""
The extracted-field record and its validation.

`ExtractedField` carries every key the assignment's required output specifies,
plus the provenance this pipeline adds: both page numbers, a resolvable
locator, the normalization outcome, and whether the evidence quote was
verified against the page it cites.

The fail-closed rules live in `finalise()`. A value is only presented as an
answer when it was found, normalized unambiguously, evidenced by a quote that
actually appears on the cited page, and asserted above the confidence
threshold. Failing any of those does not discard the extraction -- the raw
value and evidence are kept so a human can adjudicate -- it withholds the
normalized value and routes the field to review.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, asdict

from ..ingestion.locators import SourceLocator
from . import normalize as norm
from .registry import BY_NAME, FieldSpec

# Field-level status, per the assignment's fail-closed requirement.
FOUND = "found"
NOT_FOUND = "not_found"
NOT_APPLICABLE = "not_applicable"
CONFLICT = "conflict"
UNRESOLVED = "unresolved"

# Review status, per the required output schema.
UNREVIEWED = "unreviewed"
VERIFIED = "verified"
EXCEPTION = "exception"

# Confidence below which a value is not asserted. Critical fields are held to
# a higher bar because the assignment forbids silently inferring them and the
# cost of a wrong outside date or termination fee is not symmetric with the
# cost of sending it to review.
CONFIDENCE_THRESHOLD = 0.60
CRITICAL_CONFIDENCE_THRESHOLD = 0.75


@dataclass
class ExtractedField:
    """One extracted field, with provenance and control outcomes."""

    field_name: str
    document_id: str
    run_id: str

    # Value
    normalized_value: object | None = None
    currency: str | None = None
    raw_value: str | None = None

    # Provenance
    document_layer: str | None = None
    pdf_page: int | None = None
    printed_page: str | None = None
    section: str | None = None
    evidence: str | None = None
    locator_uri: str | None = None

    # Controls
    extraction_method: str = "llm"
    confidence: float = 0.0
    status: str = NOT_FOUND
    review_status: str = UNREVIEWED
    normalization_status: str | None = None
    evidence_verified: bool | None = None
    notes: list[str] = dataclass_field(default_factory=list)

    # Versioning (WS8: an output must be tied to the logic that produced it)
    model_id: str | None = None
    prompt_version: str | None = None

    @property
    def spec(self) -> FieldSpec | None:
        return BY_NAME.get(self.field_name)

    @property
    def is_critical(self) -> bool:
        spec = self.spec
        return bool(spec and spec.critical)

    @property
    def threshold(self) -> float:
        return CRITICAL_CONFIDENCE_THRESHOLD if self.is_critical else CONFIDENCE_THRESHOLD

    @property
    def category(self) -> str | None:
        spec = self.spec
        return spec.category if spec else None

    @property
    def is_answerable(self) -> bool:
        """Whether this field may be used as an answer downstream."""
        return self.status == FOUND and self.normalized_value is not None

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["category"] = self.category
        payload["critical"] = self.is_critical
        return payload

    def to_required_schema(self) -> dict:
        """
        The exact field-level output shape the assignment specifies.

        `page` is the printed page where one was reconciled, falling back to
        the PDF page, because that is the number a reader would cite. The
        unambiguous pair is always available in the full record and in the
        locator URI.
        """
        return {
            "field_name": self.field_name,
            "normalized_value": self.normalized_value,
            "currency": self.currency,
            "raw_value": self.raw_value,
            "document_id": self.document_id,
            "document_layer": self.document_layer,
            "page": self.printed_page or self.pdf_page,
            "section": self.section,
            "evidence": self.evidence,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "review_status": self.review_status,
            "run_id": self.run_id,
        }


def not_applicable_field(field_name: str, document_id: str, run_id: str, structure: str) -> ExtractedField:
    """
    Record a field that does not exist in this kind of transaction.

    Distinct from `not_found`: a German takeover offer has no shareholder vote,
    and reporting that as a failed extraction would misrepresent both the
    document and the pipeline's accuracy.
    """
    record = ExtractedField(
        field_name=field_name,
        document_id=document_id,
        run_id=run_id,
        status=NOT_APPLICABLE,
        extraction_method="deterministic",
        confidence=1.0,
    )
    record.add_note(f"Field does not apply to a transaction of type '{structure}'.")
    return record


def finalise(record: ExtractedField, verify_evidence_fn=None, page_text: str | None = None) -> ExtractedField:
    """
    Apply normalization and the fail-closed controls to a raw extraction.

    Order matters. Normalization runs first because an ambiguous normalization
    is itself grounds to withhold the value. Evidence verification runs next,
    because a quote that does not appear on its cited page is the signature of
    a fabricated or misattributed citation and no confidence score should
    rescue it. The confidence threshold is applied last.
    """
    spec = record.spec
    if spec is None:
        record.status = UNRESOLVED
        record.review_status = EXCEPTION
        record.add_note(f"Unknown field '{record.field_name}' is not in the registry.")
        return record

    if record.status == NOT_APPLICABLE:
        return record

    if record.raw_value is None or not str(record.raw_value).strip():
        record.status = NOT_FOUND
        record.normalized_value = None
        record.confidence = 0.0
        return record

    # Provisionally found. Each control below can demote this, and none can
    # promote it -- so a control that fails to run leaves the field withheld
    # rather than asserted.
    record.status = FOUND

    # -- Normalization -------------------------------------------------------
    result = norm.normalize_for(spec, record.raw_value)
    record.normalization_status = result.status
    if result.qualifier:
        record.add_note(f"Source qualifies this figure as '{result.qualifier}'.")

    if result.status == norm.OK:
        record.normalized_value = result.value
    else:
        record.normalized_value = None
        record.status = UNRESOLVED
        record.review_status = EXCEPTION
        record.add_note(f"Normalization {result.status}: {result.reason}")

    # -- Evidence verification ----------------------------------------------
    # Three outcomes, all of which must fail closed except a clean pass: the
    # quote is missing, the quote cannot be checked because no page resolved,
    # or the check ran and failed.
    if record.evidence is None:
        record.evidence_verified = False
        record.normalized_value = None
        record.status = UNRESOLVED
        record.review_status = EXCEPTION
        record.add_note("No supporting evidence quote was returned for this value.")
    elif verify_evidence_fn is not None and page_text is not None and record.locator_uri:
        check = verify_evidence_fn(
            SourceLocator.parse(record.locator_uri), record.evidence, page_text
        )
        record.evidence_verified = check.ok
        if not check.ok:
            record.normalized_value = None
            record.status = UNRESOLVED
            record.review_status = EXCEPTION
            record.add_note(f"Evidence check failed: {check.reason}")
    elif record.pdf_page is None:
        record.evidence_verified = False
        record.normalized_value = None
        record.status = UNRESOLVED
        record.review_status = EXCEPTION
        record.add_note(
            "Evidence could not be verified: no source page was resolved for this value."
        )

    # -- Confidence threshold -----------------------------------------------
    if record.status not in (UNRESOLVED, CONFLICT) and record.confidence < record.threshold:
        record.normalized_value = None
        record.status = UNRESOLVED
        record.review_status = EXCEPTION
        record.add_note(
            f"Confidence {record.confidence:.2f} is below the "
            f"{'critical ' if record.is_critical else ''}threshold of {record.threshold:.2f}."
        )

    return record


def validate(record: ExtractedField) -> list[str]:
    """
    Schema validation for an extracted field.

    Returns a list of violations; an empty list means the record is
    structurally sound. Called before persistence so that a malformed record
    is reported rather than written.
    """
    problems: list[str] = []

    if record.field_name not in BY_NAME:
        problems.append(f"unknown field_name {record.field_name!r}")
    if not record.document_id:
        problems.append("document_id is required")
    if not record.run_id:
        problems.append("run_id is required")
    if record.status not in {FOUND, NOT_FOUND, NOT_APPLICABLE, CONFLICT, UNRESOLVED}:
        problems.append(f"invalid status {record.status!r}")
    if record.review_status not in {UNREVIEWED, VERIFIED, EXCEPTION}:
        problems.append(f"invalid review_status {record.review_status!r}")
    if not 0.0 <= record.confidence <= 1.0:
        problems.append(f"confidence {record.confidence} outside [0, 1]")
    if record.extraction_method not in {"deterministic", "llm", "manual", "hybrid"}:
        problems.append(f"invalid extraction_method {record.extraction_method!r}")

    # A presented answer must be fully evidenced. This is the invariant the
    # whole fail-closed design exists to protect.
    if record.status == FOUND:
        if record.normalized_value is None:
            problems.append("status is 'found' but normalized_value is null")
        if not record.evidence:
            problems.append("status is 'found' but no evidence quote is recorded")
        if record.pdf_page is None:
            problems.append("status is 'found' but no source page is recorded")

    return problems
