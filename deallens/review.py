"""
Human review and manual correction (Workstream 8).

The pipeline withholds a value whenever a control fails: normalization was
ambiguous, the evidence quote was not found on the page it cited, confidence
was below the bar. That is the right default, and it leaves a queue of fields
where the honest answer is "a person needs to look at this".

Until now a reviewer could only annotate that queue. Marking a field verified
set its review status and left `normalized_value` null, so the field still did
not reach Q&A, the timeline or the hedging horizon -- the reviewer's judgement
was recorded and then ignored. This module closes that: a reviewer reading the
filing can supply the value, and it flows downstream like any other.

What a correction keeps
-----------------------
A manual value is normalized by the same code as a model value, so downstream
consumers get the same types and the same currency handling. Where the
reviewer gives an evidence quote and a page, the quote is checked against the
stored page text by the same verifier -- a human is more authoritative than
our matcher, so a failed check does not block the correction, but it is
recorded rather than hidden.

What it records
---------------
`extraction_method` becomes `manual` where the model found nothing and a
person supplied the value, and `hybrid` where a person corrected something the
model read. The model's original reading is written into the notes verbatim,
because the assignment requires material manual corrections be disclosed and
an audit record that quietly overwrites what the model said cannot do that.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field as dataclass_field

from .db.repository import get_extracted_fields, get_page_text, get_review_queue
from .extraction import models
from .extraction import normalize as norm
from .extraction.registry import BY_NAME
from .ingestion.locators import SourceLocator, compute_anchor, verify_evidence

MANUAL = "manual"
HYBRID = "hybrid"

# Why a field is in front of a reviewer.
CONTROL_EXCEPTION = "control_exception"
LAYER_CONFLICT = "layer_conflict"


def review_items(conn: sqlite3.Connection, document_id: str) -> list[dict]:
    """
    Everything a reviewer has to adjudicate, from both sources.

    A field reaches review two ways, and querying only one of them leaves the
    other invisible:

      `control_exception`  A control withheld the value -- ambiguous
                           normalization, an evidence quote absent from the
                           cited page, confidence below the bar. This is
                           recorded on the row as `review_status='exception'`.

      `layer_conflict`     The filing summary and the agreement each produced
                           a clean value and the values disagree. Both rows
                           passed every control on their own, so neither is an
                           exception; the problem exists only between them.
                           Workstream 3 computes this on demand and does not
                           write it back, so a query on `review_status` alone
                           never sees it -- and the assignment requires a
                           conflict be routed to review.

    Each returned row carries `review_reason` and, for a conflict, the other
    layer's value in `conflict_with`.
    """
    from .comparison import CONFLICT, compare_layers

    rows = get_extracted_fields(conn, document_id)
    by_id = {row["id"]: row for row in rows}

    items: dict[int, dict] = {}
    for row in get_review_queue(conn, document_id):
        item = dict(by_id.get(row["id"], row))
        item["review_reason"] = CONTROL_EXCEPTION
        item["conflict_with"] = None
        items[row["id"]] = item

    for comparison in compare_layers(rows):
        if comparison.classification != CONFLICT:
            continue
        for reading, other in (
            (comparison.summary, comparison.agreement),
            (comparison.agreement, comparison.summary),
        ):
            match = next(
                (
                    row
                    for row in rows
                    if row["field_name"] == comparison.field_name
                    and row["document_layer"] == reading.layer
                ),
                None,
            )
            if match is None or match["review_status"] == models.VERIFIED:
                continue
            item = items.setdefault(match["id"], dict(match))
            # A row can be both: withheld by a control *and* in conflict. The
            # conflict is the more actionable of the two, so it wins the label.
            item["review_reason"] = LAYER_CONFLICT
            item["conflict_with"] = {
                "layer": other.layer,
                "value": other.normalized_value,
                "page": other.page,
                "evidence": other.evidence,
            }

    return sorted(
        items.values(),
        key=lambda row: (
            not row["is_critical"],
            0 if row["review_reason"] == LAYER_CONFLICT else 1,
            row["field_name"],
            row["document_layer"] or "",
        ),
    )


@dataclass
class CorrectionResult:
    """What applying a correction did, and what it could not do."""

    accepted: bool
    field_name: str = ""
    normalized_value: object | None = None
    extraction_method: str = MANUAL
    evidence_verified: bool | None = None
    reason: str | None = None
    notes: list[str] = dataclass_field(default_factory=list)


def apply_correction(
    conn: sqlite3.Connection,
    field_row_id: int,
    raw_value: str,
    evidence: str | None = None,
    printed_page: str | None = None,
    pdf_page: int | None = None,
    section: str | None = None,
    reviewer_note: str | None = None,
) -> CorrectionResult:
    """
    Replace a field's value with one a reviewer supplied.

    Refuses rather than guesses when the supplied value will not normalize:
    a reviewer who types "next June" into a date field should be told the
    field could not be read, not have it silently stored as text. The
    correction is rejected, nothing is written, and they can try again.
    """
    row = conn.execute(
        "SELECT * FROM extracted_fields WHERE id = ?", (field_row_id,)
    ).fetchone()
    if row is None:
        return CorrectionResult(False, reason=f"No field row with id {field_row_id}.")

    record = dict(row)
    spec = BY_NAME.get(record["field_name"])
    if spec is None:
        return CorrectionResult(
            False,
            field_name=record["field_name"],
            reason=f"{record['field_name']!r} is not in the field registry.",
        )

    raw_value = (raw_value or "").strip()
    if not raw_value:
        return CorrectionResult(
            False, field_name=spec.name, reason="A value is required."
        )

    normalized = norm.normalize_for(spec, raw_value)
    if not normalized.ok:
        return CorrectionResult(
            False,
            field_name=spec.name,
            reason=(
                f"{raw_value!r} could not be read as a {spec.value_type} value: "
                f"{normalized.reason or normalized.status}."
            ),
        )

    currency = record["currency"]
    if spec.value_type == "money":
        detected = norm.normalize_currency(raw_value)
        if detected.ok:
            currency = detected.value

    # A person correcting a reading the model produced is a different record
    # from a person supplying one it never found.
    previously_read = bool((record["raw_value"] or "").strip())
    method = HYBRID if previously_read else MANUAL

    page = pdf_page if pdf_page is not None else record["pdf_page"]
    printed = printed_page if printed_page is not None else record["printed_page"]
    quote = evidence if evidence is not None else record["evidence"]
    where = section if section is not None else record["section"]

    locator_uri = record["locator_uri"]
    if page is not None:
        locator_uri = SourceLocator(
            document_id=record["document_id"],
            layer_id=record["document_layer"] or "unknown",
            pdf_page=int(page),
            printed_page=printed,
            section=where,
            anchor=compute_anchor(quote) if quote else None,
        ).to_uri()

    notes = json.loads(record["notes"]) if record["notes"] else []
    if previously_read:
        notes.append(
            f"Manual correction: replaced the model's reading "
            f"{record['raw_value']!r} (confidence {record['confidence']:.2f}, "
            f"status {record['status']}) with {raw_value!r}."
        )
    else:
        notes.append(
            f"Manual entry: the model reported no value; a reviewer supplied "
            f"{raw_value!r}."
        )

    # Verify the reviewer's quote the same way a model's is verified. A human
    # reading the filing outranks our matcher, so a failure is recorded rather
    # than treated as grounds to refuse.
    evidence_verified: bool | None = None
    if quote and page is not None and locator_uri:
        page_text = get_page_text(conn, record["document_id"], int(page))
        if page_text is not None:
            check = verify_evidence(SourceLocator.parse(locator_uri), quote, page_text)
            evidence_verified = check.ok
            if not check.ok:
                notes.append(
                    f"Manual evidence did not match the cited page: {check.reason}. "
                    "Kept on the reviewer's authority."
                )
        else:
            notes.append(
                "Manual evidence could not be checked: no stored text for the "
                "cited page."
            )
    if reviewer_note:
        notes.append(f"Review: {reviewer_note}")

    with conn:
        conn.execute(
            """
            UPDATE extracted_fields SET
                raw_value = ?, normalized_value = ?, value_json = ?, currency = ?,
                pdf_page = ?, printed_page = ?, section = ?, evidence = ?,
                locator_uri = ?, extraction_method = ?, confidence = ?,
                status = ?, review_status = ?, normalization_status = ?,
                evidence_verified = ?, notes = ?
            WHERE id = ?
            """,
            (
                raw_value,
                str(normalized.value),
                json.dumps(normalized.value),
                currency,
                page,
                printed,
                where,
                quote,
                locator_uri,
                method,
                # A reviewer who has read the filing is not expressing a
                # probability. The model's confidence score does not carry
                # over to a value it did not produce.
                1.0,
                models.FOUND,
                models.VERIFIED,
                normalized.status,
                None if evidence_verified is None else int(evidence_verified),
                json.dumps(notes),
                field_row_id,
            ),
        )

    return CorrectionResult(
        accepted=True,
        field_name=spec.name,
        normalized_value=normalized.value,
        extraction_method=method,
        evidence_verified=evidence_verified,
        notes=notes,
    )
