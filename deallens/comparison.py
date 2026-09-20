"""
Filing summary vs. agreement comparison (Workstream 3).

Extraction deliberately keeps each document layer separate and never merges
them, which leaves every field with up to two readings: one from the 8-K
filing summary, one from the operative agreement it summarises. This module is
what turns those two readings into a finding.

The two readings disagree more often than one might expect, and rarely because
either is wrong. A filing summary is the registrant's description of the deal,
written for investors: it rounds ("approximately $5.7 billion" against
"$73.00 per share"), simplifies ("expected to close in the second half of
2026" against a hard outside date), and occasionally states something the
agreement contradicts. Surfacing that gap is the deliverable. Resolving it
silently would destroy the evidence the workstream exists to report.

Classification vocabulary
-------------------------
The seven classes are the assignment's, verbatim, and this module does not
invent an eighth. Where one class covers materially different situations --
`unresolved` spans "neither document mentions this field" and "both mention it
but the value was withheld by a control" -- the distinction is carried in the
`reason` string rather than by widening the vocabulary.

Source hierarchy
----------------
On a conflict the operative agreement governs: it is the executed contract,
whereas the filing summary is a description of it prepared for a different
audience and subject to no one's signature. `preferred_layer` records that
choice on every conflict, and both readings are kept in full alongside it --
the hierarchy nominates a value, it does not discard one.

Comparisons are computed on demand from the stored fields rather than
persisted. The review queue lets an analyst change a field's status, and a
stored comparison would be stale the moment they did.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field as dataclass_field

from .extraction import models
from .extraction.registry import CATEGORY_ORDER, BY_NAME
from .ingestion.classifier import LAYER_AGREEMENT, LAYER_FILING_SUMMARY

# Comparison classes, exactly as Workstream 3 enumerates them.
MATCH = "match"
NORMALIZED_MATCH = "normalized_match"
SUMMARY_ONLY = "summary_only"
AGREEMENT_ONLY = "agreement_only"
CONFLICT = "conflict"
NOT_APPLICABLE = "not_applicable"
UNRESOLVED = "unresolved"

# Triage order: the classes that need a human come first.
CLASS_ORDER = (
    CONFLICT,
    AGREEMENT_ONLY,
    SUMMARY_ONLY,
    UNRESOLVED,
    NORMALIZED_MATCH,
    MATCH,
    NOT_APPLICABLE,
)

# Floating-point readings of the same decimal text should compare equal; a
# genuine difference in a deal term is never this small.
_REL_TOL = 1e-9


@dataclass
class LayerReading:
    """One layer's reading of one field, as far as comparison cares."""

    layer: str | None = None
    status: str = models.NOT_FOUND
    normalized_value: object | None = None
    raw_value: str | None = None
    currency: str | None = None
    page: object | None = None
    section: str | None = None
    evidence: str | None = None
    locator_uri: str | None = None
    confidence: float = 0.0
    review_status: str = models.UNREVIEWED

    @property
    def is_answerable(self) -> bool:
        return self.status == models.FOUND and self.normalized_value is not None

    @property
    def was_attempted(self) -> bool:
        """Whether the model found something here, even if a control withheld it."""
        return self.status in {models.FOUND, models.CONFLICT, models.UNRESOLVED} or bool(
            self.raw_value
        )


@dataclass
class FieldComparison:
    """The comparison of one field across the two substantive layers."""

    field_name: str
    category: str | None
    is_critical: bool
    classification: str
    reason: str
    summary: LayerReading = dataclass_field(default_factory=LayerReading)
    agreement: LayerReading = dataclass_field(default_factory=LayerReading)
    preferred_layer: str | None = None
    preferred_value: object | None = None
    # The reading the hierarchy nominated, kept whole so the governing value
    # carries its own page and locator. A value the pipeline puts forward as
    # the answer without a citation is the one thing this project is built not
    # to produce.
    preferred_reading: LayerReading | None = None

    @property
    def needs_review(self) -> bool:
        """
        Whether a person has something to do about this field.

        A conflict always does. An `unresolved` only does when a value was
        actually read and then withheld -- when neither layer mentions the
        field there is nothing to adjudicate, and putting it in front of a
        reviewer is how a queue stops being read.
        """
        if self.classification == CONFLICT:
            return True
        if self.classification == UNRESOLVED:
            return self.summary.was_attempted or self.agreement.was_attempted
        return False

    @property
    def preferred_page(self) -> object | None:
        return self.preferred_reading.page if self.preferred_reading else None

    @property
    def preferred_locator(self) -> str | None:
        return self.preferred_reading.locator_uri if self.preferred_reading else None

    def to_dict(self) -> dict:
        """Flat, JSON-safe shape for export and for the UI table."""
        return {
            "field_name": self.field_name,
            "category": self.category,
            "critical": self.is_critical,
            "classification": self.classification,
            "reason": self.reason,
            "preferred_layer": self.preferred_layer,
            "preferred_value": self.preferred_value,
            "preferred_page": self.preferred_page,
            "preferred_section": (
                self.preferred_reading.section if self.preferred_reading else None
            ),
            "preferred_locator": self.preferred_locator,
            "summary_value": self.summary.normalized_value,
            "summary_raw": self.summary.raw_value,
            "summary_page": self.summary.page,
            "summary_section": self.summary.section,
            "summary_evidence": self.summary.evidence,
            "summary_locator": self.summary.locator_uri,
            "summary_status": self.summary.status,
            "agreement_value": self.agreement.normalized_value,
            "agreement_raw": self.agreement.raw_value,
            "agreement_page": self.agreement.page,
            "agreement_section": self.agreement.section,
            "agreement_evidence": self.agreement.evidence,
            "agreement_locator": self.agreement.locator_uri,
            "agreement_status": self.agreement.status,
        }


def _reading_from_row(row: dict) -> LayerReading:
    return LayerReading(
        layer=row.get("document_layer"),
        status=row.get("status") or models.NOT_FOUND,
        normalized_value=row.get("normalized_value"),
        raw_value=row.get("raw_value"),
        currency=row.get("currency"),
        page=row.get("printed_page") or row.get("pdf_page"),
        section=row.get("section"),
        evidence=row.get("evidence"),
        locator_uri=row.get("locator_uri"),
        confidence=row.get("confidence") or 0.0,
        review_status=row.get("review_status") or models.UNREVIEWED,
    )


_LAYER_QUALIFIER_RE = re.compile(r"-(?:ex[\d.]+|\d+)$")


def _base_layer(layer: str | None) -> str | None:
    """
    The layer kind, with any exhibit or ordinal qualifier removed.

    Layer ids carry the exhibit they came from -- `agreement-ex2.1` -- because
    a filing can contain more than one instrument. Comparison cares only that
    it is the agreement.
    """
    if not layer:
        return None
    return _LAYER_QUALIFIER_RE.sub("", layer)


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _values_identical(left: object, right: object) -> bool:
    """Same value, same way of writing it."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=_REL_TOL)
    return _collapse(left) == _collapse(right)


def _raw_identical(left: str | None, right: str | None) -> bool:
    """
    Whether both layers used the same words for the value.

    Whitespace is collapsed because a line break inside a PDF is not a
    difference in the document's terminology. Case is not folded: this is the
    check that separates `match` from `normalized_match`, and the assignment
    requires the source's own terminology be preserved, so a difference in how
    the value is written is a finding rather than noise.
    """
    if left is None or right is None:
        return left == right
    return _collapse(left) == _collapse(right)


# Monetary amounts written inside narrative text: "$250,000,000",
# "EUR 14,200,000,000", "€11.5 billion". Deliberately not every number -- a
# clause reference, a section number and a count of business days are all
# digits, and none of them disagreeing means the two layers disagree.
_EMBEDDED_MONEY_RE = re.compile(
    r"(?:[$€£¥]|\b(?:USD|EUR|GBP|JPY|CHF)\b)\s*"
    r"(\d[\d,.]*)\s*(billion|million|bn|mm|m|b)?",
    re.I,
)
_MONEY_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6}


def _embedded_amounts(text: object) -> set[float]:
    """Monetary amounts stated inside a narrative value."""
    amounts: set[float] = set()
    for digits, scale in _EMBEDDED_MONEY_RE.findall(str(text or "")):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        amounts.add(value * _MONEY_SCALE.get((scale or "").lower(), 1.0))
    return amounts


# Above this length a text value is prose describing a provision; below it,
# it is an identifier -- a party name, a threshold, a short period. The two
# need different treatment: prose is expected to differ between a summary and
# the contract it summarises, whereas two different party names are two
# different parties. The boundary is a heuristic, and deliberately generous
# to the strict side.
_NARRATIVE_LENGTH = 80


def _is_narrative(summary: LayerReading, agreement: LayerReading) -> bool:
    return max(
        len(str(summary.normalized_value or "")),
        len(str(agreement.normalized_value or "")),
    ) > _NARRATIVE_LENGTH


def _one_contains_the_other(left: object, right: object) -> bool:
    """
    Whether one short value is a condensation of the other.

    "Bio-Techne" inside "Bio-Techne Corporation" is a summary using the short
    form of a name, not a disagreement about which company is being bought.
    "Acme Corp" against "Bio-Techne Corporation" contains nothing, and stays a
    conflict.
    """
    a, b = _collapse(left).casefold(), _collapse(right).casefold()
    if not a or not b:
        return False
    return a in b or b in a


def _compare_text(
    summary: LayerReading, agreement: LayerReading
) -> tuple[str, str]:
    """
    Classify two narrative readings, in order of how much each test tells us.

    Amounts first, because a figure stated in both is the strongest evidence
    either way and does not depend on how long the surrounding prose is. Then
    containment, which catches a summary using the short form of a name. Then
    length, which decides what an unexplained difference means: in prose it
    means the summary summarised, and in a short value it means the two
    layers name different things.
    """
    left = _embedded_amounts(summary.normalized_value)
    right = _embedded_amounts(agreement.normalized_value)

    if left and right:
        if left & right:
            return NORMALIZED_MATCH, (
                "Both layers describe this provision in different words but "
                f"state the same amount(s): {sorted(left & right)}."
            )
        return CONFLICT, (
            "Both layers describe this provision, and the monetary amounts "
            f"they state disagree: {sorted(left)} against {sorted(right)}."
        )

    if _one_contains_the_other(summary.normalized_value, agreement.normalized_value):
        return NORMALIZED_MATCH, (
            "The filing summary states a shorter form of the same value "
            f"({summary.normalized_value!r} within {agreement.normalized_value!r})."
        )

    if _is_narrative(summary, agreement):
        return NORMALIZED_MATCH, (
            "Both layers describe this provision in different words, which is "
            "what a summary does. Narrative fields are not compared verbatim "
            "and no conflicting amounts were found; both readings are shown "
            "for a reviewer to judge."
        )

    return CONFLICT, (
        f"The layers state different values: {summary.normalized_value!r} "
        f"against {agreement.normalized_value!r}."
    )


def _values_equivalent(left: object, right: object) -> bool:
    """
    Same value, allowing for presentation differences.

    Only case and whitespace are forgiven. Nothing here paraphrases: "Bio-Techne
    Corporation" and "BIO-TECHNE CORPORATION" are the same party, while
    "Bio-Techne Corporation" and "Bio-Techne" are not, and deciding otherwise
    would be exactly the silent reconciliation this module exists to avoid.
    """
    if isinstance(left, str) and isinstance(right, str):
        return _collapse(left).casefold() == _collapse(right).casefold()
    return _values_identical(left, right)


def _one_sided_reason(present: str, absent: str, absent_reading: LayerReading) -> str:
    """
    Why only one of the two layers produced a value.

    A layer that is silent on a term and a layer whose reading a control
    withheld are different findings. The first needs nothing: most of these
    fields are contract mechanics an 8-K summary never mentions. The second
    has a value sitting in the review queue and a page to check it against.
    Reporting both as "only the other layer supports a value" hides the one
    that has work attached to it.
    """
    if not absent_reading.was_attempted:
        return f"Only the {present} supports a value; the {absent} does not mention it."
    if absent_reading.raw_value:
        return (
            f"Only the {present} supports a value. The {absent} reported "
            f"{absent_reading.raw_value!r}, which a control withheld — it is in "
            "the review queue."
        )
    return (
        f"Only the {present} supports a value. The {absent} yielded a reading "
        f"that a control withheld ({absent_reading.status}) — it is in the "
        "review queue."
    )


def compare_field(
    field_name: str, summary: LayerReading, agreement: LayerReading
) -> FieldComparison:
    """
    Classify one field's two readings.

    The order of the checks is the order of precedence: a field that does not
    exist in this transaction structure is never a conflict, and a value
    withheld by a control is never a match.
    """
    spec = BY_NAME.get(field_name)
    comparison = FieldComparison(
        field_name=field_name,
        category=spec.category if spec else None,
        is_critical=bool(spec and spec.critical),
        classification=UNRESOLVED,
        reason="",
        summary=summary,
        agreement=agreement,
    )

    if models.NOT_APPLICABLE in {summary.status, agreement.status}:
        comparison.classification = NOT_APPLICABLE
        comparison.reason = "This field does not apply to the detected transaction structure."
        return comparison

    summary_ok = summary.is_answerable
    agreement_ok = agreement.is_answerable

    if summary_ok and agreement_ok:
        # A matching amount in a different currency is not a match.
        currencies = {summary.currency, agreement.currency} - {None}
        if len(currencies) > 1:
            comparison.classification = CONFLICT
            comparison.reason = (
                f"Both layers report a value, in different currencies "
                f"({summary.currency} and {agreement.currency})."
            )
        elif _values_identical(summary.normalized_value, agreement.normalized_value):
            # The normalized values agree. Whether that is a match or a
            # normalized match turns on the source text, not on the normalized
            # value: "$250 million" and "$250,000,000" both become 250000000.0,
            # and reporting them as a plain match would hide that the two
            # documents state the term differently.
            if _raw_identical(summary.raw_value, agreement.raw_value):
                comparison.classification = MATCH
                comparison.reason = "Both layers report the same value, worded identically."
            else:
                comparison.classification = NORMALIZED_MATCH
                comparison.reason = (
                    "Both layers report the same value, written differently "
                    f"({summary.raw_value!r} and {agreement.raw_value!r})."
                )
        elif _values_equivalent(summary.normalized_value, agreement.normalized_value):
            comparison.classification = NORMALIZED_MATCH
            comparison.reason = (
                "Both layers report the same value, differing only in case or "
                f"spacing ({summary.raw_value!r} and {agreement.raw_value!r})."
            )
        elif spec is not None and spec.value_type == "text":
            outcome = _compare_text(summary, agreement)
            comparison.classification, comparison.reason = outcome
        else:
            comparison.classification = CONFLICT
            comparison.reason = (
                f"The layers disagree: the filing summary reports "
                f"{summary.normalized_value!r} and the agreement reports "
                f"{agreement.normalized_value!r}."
            )
        # The agreement governs. Recorded on every two-sided outcome, not only
        # conflicts, so the hierarchy is visible rather than implied.
        comparison.preferred_layer = agreement.layer or LAYER_AGREEMENT
        comparison.preferred_value = agreement.normalized_value
        comparison.preferred_reading = agreement
        return comparison

    if summary_ok:
        comparison.classification = SUMMARY_ONLY
        comparison.reason = _one_sided_reason("filing summary", "agreement", agreement)
        comparison.preferred_layer = summary.layer or LAYER_FILING_SUMMARY
        comparison.preferred_value = summary.normalized_value
        comparison.preferred_reading = summary
        return comparison

    if agreement_ok:
        comparison.classification = AGREEMENT_ONLY
        comparison.reason = _one_sided_reason("agreement", "filing summary", summary)
        comparison.preferred_layer = agreement.layer or LAYER_AGREEMENT
        comparison.preferred_value = agreement.normalized_value
        comparison.preferred_reading = agreement
        return comparison

    # Neither side yielded a usable value. Whether that is an absence or a
    # withheld reading is the difference between "the documents are silent"
    # and "we do not trust what we read", so it is spelled out.
    withheld = [
        name
        for name, reading in (("filing summary", summary), ("agreement", agreement))
        if reading.status in {models.CONFLICT, models.UNRESOLVED} or reading.raw_value
    ]
    comparison.classification = UNRESOLVED
    if withheld:
        comparison.reason = (
            f"A value was read from the {' and '.join(withheld)} but withheld by a "
            "control; no comparison is possible until it is reviewed."
        )
    else:
        comparison.reason = "Neither layer reports this field."
    return comparison


def compare_layers(
    rows: list[dict],
    summary_layer: str = LAYER_FILING_SUMMARY,
    agreement_layer: str = LAYER_AGREEMENT,
) -> list[FieldComparison]:
    """
    Compare every field across the filing summary and the agreement.

    `rows` is the output of `get_extracted_fields` for one document. Rows from
    other layers -- a press release, a charter exhibit -- are ignored rather
    than folded in: they are not the agreement, and treating them as such is
    how a summary/agreement comparison silently becomes something else.

    Where a run produced several readings of the same field within one layer,
    the extractor has already reconciled them into one record, so at most one
    row per (field, layer) is expected. If more arrive -- rows from two runs of
    the same document, say -- the most confident is used and the rest ignored,
    because comparing an arbitrary one would be worse than comparing the best.
    """
    per_field: dict[str, dict[str, dict]] = {}
    not_applicable: dict[str, dict] = {}

    for row in rows:
        name = row.get("field_name")
        if not name:
            continue
        if row.get("status") == models.NOT_APPLICABLE:
            not_applicable[name] = row
            continue

        base = _base_layer(row.get("document_layer"))
        if base not in {summary_layer, agreement_layer}:
            continue
        slot = per_field.setdefault(name, {})
        incumbent = slot.get(base)
        if incumbent is None or _is_better(row, incumbent):
            slot[base] = row

    comparisons: list[FieldComparison] = []
    for name in sorted(set(per_field) | set(not_applicable)):
        # A field recorded as not applicable to this structure is that,
        # whatever else turned up for it.
        if name in not_applicable:
            comparisons.append(
                compare_field(
                    name,
                    LayerReading(layer=summary_layer, status=models.NOT_APPLICABLE),
                    LayerReading(layer=agreement_layer, status=models.NOT_APPLICABLE),
                )
            )
            continue
        slot = per_field.get(name, {})
        summary = (
            _reading_from_row(slot[summary_layer])
            if summary_layer in slot
            else LayerReading(layer=summary_layer)
        )
        agreement = (
            _reading_from_row(slot[agreement_layer])
            if agreement_layer in slot
            else LayerReading(layer=agreement_layer)
        )
        comparisons.append(compare_field(name, summary, agreement))

    return sort_comparisons(comparisons)


def _is_better(candidate: dict, incumbent: dict) -> bool:
    """Prefer an answerable reading, then the more confident one."""
    def rank(row: dict) -> tuple[int, float]:
        answerable = row.get("status") == models.FOUND and row.get("normalized_value") is not None
        return (1 if answerable else 0, row.get("confidence") or 0.0)

    return rank(candidate) > rank(incumbent)


def sort_comparisons(comparisons: list[FieldComparison]) -> list[FieldComparison]:
    """Category order, then triage order within a category, then field name."""
    category_rank = {category: index for index, category in enumerate(CATEGORY_ORDER)}
    class_rank = {name: index for index, name in enumerate(CLASS_ORDER)}
    return sorted(
        comparisons,
        key=lambda c: (
            category_rank.get(c.category, len(category_rank)),
            class_rank.get(c.classification, len(class_rank)),
            c.field_name,
        ),
    )


def classification_counts(comparisons: list[FieldComparison]) -> dict[str, int]:
    """Counts by class, in triage order, omitting classes with no members."""
    counts = {name: 0 for name in CLASS_ORDER}
    for comparison in comparisons:
        counts[comparison.classification] = counts.get(comparison.classification, 0) + 1
    return {name: count for name, count in counts.items() if count}
