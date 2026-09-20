"""
Tests for the filing summary vs. agreement comparison (Workstream 3).

The classification vocabulary is the assignment's, and the rule that matters
most is negative: a disagreement between the two layers is never resolved into
a single value. These tests pin that down case by case, including the ones
where resolving would be tempting -- a rounded figure, a different currency, a
value one layer states and the other does not.
"""

from __future__ import annotations

from deallens.comparison import (
    AGREEMENT_ONLY,
    CONFLICT,
    MATCH,
    NORMALIZED_MATCH,
    NOT_APPLICABLE,
    SUMMARY_ONLY,
    UNRESOLVED,
    LayerReading,
    classification_counts,
    compare_field,
    compare_layers,
)
from deallens.extraction import models


def _reading(value, raw=None, layer="agreement", currency=None, status=models.FOUND, **kw):
    return LayerReading(
        layer=layer,
        status=status,
        normalized_value=value,
        raw_value=raw if raw is not None else (str(value) if value is not None else None),
        currency=currency,
        **kw,
    )


def _row(field_name, layer, value=None, raw=None, status=models.FOUND, **kw):
    row = {
        "field_name": field_name,
        "document_layer": layer,
        "normalized_value": value,
        "raw_value": raw,
        "status": status,
        "currency": None,
        "printed_page": None,
        "pdf_page": None,
        "section": None,
        "evidence": None,
        "locator_uri": None,
        "confidence": 0.9,
        "review_status": models.UNREVIEWED,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_identical_values_are_a_match():
    result = compare_field(
        "consideration_per_share",
        _reading(73.0, "$73.00", layer="filing-summary"),
        _reading(73.0, "$73.00"),
    )
    assert result.classification == MATCH


def test_same_value_written_differently_is_a_normalized_match():
    """
    "$250,000,000" and "$250 million" are the same term. Calling this a plain
    match would hide that the two documents phrase it differently; calling it
    a conflict would cry wolf.
    """
    result = compare_field(
        "company_termination_fee",
        _reading(250_000_000.0, "$250 million", layer="filing-summary"),
        _reading(250_000_000.0, "$250,000,000"),
    )
    assert result.classification == NORMALIZED_MATCH
    assert "$250 million" in result.reason


def test_case_and_whitespace_differences_are_a_normalized_match():
    result = compare_field(
        "target",
        _reading("BIO-TECHNE  CORPORATION", layer="filing-summary"),
        _reading("Bio-Techne Corporation"),
    )
    assert result.classification == NORMALIZED_MATCH


def test_a_shortened_party_name_is_not_a_conflict():
    """
    Revised after the Uber run. This was asserted as a conflict on the
    reasoning that normalization forgives presentation and never substance --
    but a summary naming "Bio-Techne" where the contract says "Bio-Techne
    Corporation" is the short form of one name, not two companies. A
    genuinely different name still conflicts; see the short-value test below.
    """
    result = compare_field(
        "target",
        _reading("Bio-Techne", layer="filing-summary"),
        _reading("Bio-Techne Corporation"),
    )
    assert result.classification == NORMALIZED_MATCH


def test_differing_values_are_a_conflict_and_neither_is_discarded():
    result = compare_field(
        "company_termination_fee",
        _reading(250_000_000.0, "$250,000,000", layer="filing-summary"),
        _reading(255_000_000.0, "$255,000,000"),
    )
    assert result.classification == CONFLICT
    assert result.summary.normalized_value == 250_000_000.0
    assert result.agreement.normalized_value == 255_000_000.0
    assert result.needs_review


def test_same_amount_in_different_currencies_is_a_conflict():
    """A number is not a value until you know its unit."""
    result = compare_field(
        "consideration_per_share",
        _reading(73.0, "$73.00", layer="filing-summary", currency="USD"),
        _reading(73.0, "€73.00", currency="EUR"),
    )
    assert result.classification == CONFLICT
    assert "currencies" in result.reason


def test_value_in_only_one_layer_is_reported_as_one_sided():
    summary_only = compare_field(
        "total_transaction_value",
        _reading(5_700_000_000.0, layer="filing-summary"),
        LayerReading(layer="agreement", status=models.NOT_FOUND),
    )
    assert summary_only.classification == SUMMARY_ONLY

    agreement_only = compare_field(
        "matching_rights",
        LayerReading(layer="filing-summary", status=models.NOT_FOUND),
        _reading("four business days"),
    )
    assert agreement_only.classification == AGREEMENT_ONLY


def test_a_silent_layer_and_a_withheld_layer_read_differently():
    """
    Both come out `agreement only`, but they are not the same finding. Most
    contract mechanics never appear in an 8-K summary and need nothing from
    anyone; a reading a control withheld has a value in the review queue and a
    page to check it against. One reason string for both hid the second.
    """
    silent = compare_field(
        "matching_rights",
        LayerReading(layer="filing-summary", status=models.NOT_FOUND),
        _reading("four business days", page="A-52"),
    )
    withheld = compare_field(
        "outside_date",
        LayerReading(
            layer="filing-summary",
            status=models.UNRESOLVED,
            raw_value="the second half of 2026",
            page="3",
        ),
        _reading("2027-06-25", page="A-9"),
    )

    assert silent.classification == withheld.classification == AGREEMENT_ONLY
    assert "does not mention it" in silent.reason
    assert "withheld" in withheld.reason
    assert "the second half of 2026" in withheld.reason
    assert "review queue" in withheld.reason


def test_the_one_sided_reason_is_symmetric_between_layers():
    """The summary-only path said this; the agreement-only path did not."""
    summary_side = compare_field(
        "total_transaction_value",
        _reading(5_700_000_000.0, layer="filing-summary"),
        LayerReading(layer="agreement", status=models.UNRESOLVED, raw_value="approximately $5.7 billion"),
    )
    assert summary_side.classification == SUMMARY_ONLY
    assert "withheld" in summary_side.reason
    assert "review queue" in summary_side.reason


def test_a_withheld_reading_keeps_its_page_for_the_reviewer():
    """
    The page is what makes the withheld reading checkable, so withholding the
    value must not discard it.
    """
    result = compare_field(
        "outside_date",
        LayerReading(
            layer="filing-summary",
            status=models.UNRESOLVED,
            raw_value="the second half of 2026",
            page="3",
        ),
        _reading("2027-06-25", page="A-9"),
    )
    assert result.summary.normalized_value is None
    assert result.summary.page == "3"
    assert result.summary.raw_value == "the second half of 2026"


def test_a_field_neither_layer_reports_is_unresolved_and_says_so():
    result = compare_field(
        "bridge_amount",
        LayerReading(layer="filing-summary", status=models.NOT_FOUND),
        LayerReading(layer="agreement", status=models.NOT_FOUND),
    )
    assert result.classification == UNRESOLVED
    assert "Neither layer reports" in result.reason


def test_a_withheld_value_is_unresolved_not_absent():
    """
    A value the model read but a control withheld is a different situation
    from a document that is silent, and the reason has to distinguish them --
    one needs a reviewer, the other needs nothing.
    """
    result = compare_field(
        "outside_date",
        LayerReading(
            layer="filing-summary",
            status=models.UNRESOLVED,
            raw_value="the first anniversary of the date hereof",
        ),
        LayerReading(layer="agreement", status=models.NOT_FOUND),
    )
    assert result.classification == UNRESOLVED
    assert "withheld by a control" in result.reason
    assert "filing summary" in result.reason


def test_a_withheld_value_never_counts_as_a_match():
    """Two layers that both failed their controls agree on nothing."""
    result = compare_field(
        "company_termination_fee",
        LayerReading(layer="filing-summary", status=models.UNRESOLVED, raw_value="$250,000,000"),
        LayerReading(layer="agreement", status=models.UNRESOLVED, raw_value="$250,000,000"),
    )
    assert result.classification == UNRESOLVED


def test_not_applicable_beats_every_other_classification():
    result = compare_field(
        "shareholder_approval_threshold",
        LayerReading(layer="filing-summary", status=models.NOT_APPLICABLE),
        _reading("a majority of the outstanding shares"),
    )
    assert result.classification == NOT_APPLICABLE


# ---------------------------------------------------------------------------
# Source hierarchy
# ---------------------------------------------------------------------------

def test_the_agreement_governs_a_conflict_without_discarding_the_summary():
    """
    The assignment requires a documented source hierarchy AND that no
    conflicting value is silently discarded. Both, not either.
    """
    result = compare_field(
        "outside_date",
        _reading("2027-01-01", layer="filing-summary"),
        _reading("2027-06-25", layer="agreement-ex2.1"),
    )
    assert result.classification == CONFLICT
    assert result.preferred_layer == "agreement-ex2.1"
    assert result.preferred_value == "2027-06-25"
    assert result.summary.normalized_value == "2027-01-01", "the summary reading survives"


def test_the_governing_value_carries_its_own_page_and_locator():
    """
    A value the pipeline puts forward as the answer must be citable. Reporting
    a governing value with no page is the one output shape this project exists
    to avoid, and it is easy to reintroduce by reading `preferred_value` alone.
    """
    result = compare_field(
        "company_termination_fee",
        _reading(250_000_000.0, "$250 million", layer="filing-summary",
                 page="3", section="Item 1.01", locator_uri="deallens://doc/p3"),
        _reading(255_000_000.0, "$255,000,000", layer="agreement-ex2.1",
                 page="A-47", section="Section 7.02", locator_uri="deallens://doc/p47"),
    )
    assert result.preferred_value == 255_000_000.0
    assert result.preferred_page == "A-47"
    assert result.preferred_locator == "deallens://doc/p47"

    payload = result.to_dict()
    assert payload["preferred_page"] == "A-47"
    assert payload["preferred_section"] == "Section 7.02"
    assert payload["preferred_locator"] == "deallens://doc/p47"


def test_a_comparison_with_no_governing_value_has_no_page():
    """The page must come from a reading, never from a default."""
    result = compare_field(
        "bridge_amount",
        LayerReading(layer="filing-summary", status=models.NOT_FOUND),
        LayerReading(layer="agreement", status=models.NOT_FOUND),
    )
    assert result.preferred_value is None
    assert result.preferred_page is None
    assert result.preferred_locator is None


def test_a_one_sided_field_prefers_the_layer_that_has_it():
    result = compare_field(
        "total_transaction_value",
        _reading(5_700_000_000.0, layer="filing-summary"),
        LayerReading(layer="agreement", status=models.NOT_FOUND),
    )
    assert result.preferred_layer == "filing-summary"
    assert result.preferred_value == 5_700_000_000.0


# ---------------------------------------------------------------------------
# Assembling a whole document
# ---------------------------------------------------------------------------

def test_exhibit_qualified_agreement_layers_are_recognised():
    """
    Layer ids carry their exhibit (`agreement-ex2.1`) because a filing may
    hold more than one instrument. The comparison must still see it as the
    agreement, or every field would come back summary-only.
    """
    rows = [
        _row("target", "filing-summary", "Bio-Techne Corporation"),
        _row("target", "agreement-ex2.1", "Bio-Techne Corporation"),
    ]
    results = compare_layers(rows)
    assert len(results) == 1
    assert results[0].classification == MATCH


def test_rows_from_other_layers_are_not_treated_as_the_agreement():
    """
    A press release is not the contract. Folding it in would make a
    summary-vs-agreement comparison quietly into something else.
    """
    rows = [
        _row("target", "filing-summary", "Bio-Techne Corporation"),
        _row("target", "press-release", "Bio-Techne"),
    ]
    results = compare_layers(rows)
    assert results[0].classification == SUMMARY_ONLY


def test_one_row_per_field_per_layer_collapses_to_one_comparison():
    """
    This is the symptom that motivated the view: 48 fields across two layers
    are 96 stored rows and must read as 48 findings, not 96 near-duplicates.
    """
    rows = []
    for name in ("target", "acquirer", "outside_date"):
        rows.append(_row(name, "filing-summary", f"{name}-value"))
        rows.append(_row(name, "agreement-ex2.1", f"{name}-value"))
    results = compare_layers(rows)

    assert len(results) == 3, "six rows, three fields, three findings"
    assert {r.classification for r in results} == {MATCH}


def test_duplicate_rows_for_one_layer_prefer_the_answerable_reading():
    """
    Two runs of the same document leave two rows for one (field, layer). The
    comparison uses the answerable one rather than whichever the query
    happened to return first.
    """
    rows = [
        _row("target", "filing-summary", None, status=models.UNRESOLVED, confidence=0.95),
        _row("target", "filing-summary", "Bio-Techne Corporation", confidence=0.80),
        _row("target", "agreement-ex2.1", "Bio-Techne Corporation"),
    ]
    results = compare_layers(rows)
    assert len(results) == 1
    assert results[0].classification == MATCH


def test_counts_cover_every_field_exactly_once():
    rows = [
        _row("target", "filing-summary", "A"),
        _row("target", "agreement-ex2.1", "A"),
        _row("outside_date", "filing-summary", "2027-01-01"),
        _row("outside_date", "agreement-ex2.1", "2027-06-25"),
        _row("bridge_amount", "filing-summary", None, status=models.NOT_FOUND),
        _row("bridge_amount", "agreement-ex2.1", None, status=models.NOT_FOUND),
        _row("offer_or_acceptance_period", None, None, status=models.NOT_APPLICABLE),
    ]
    results = compare_layers(rows)
    counts = classification_counts(results)

    assert sum(counts.values()) == len(results) == 4
    assert counts[MATCH] == 1
    assert counts[CONFLICT] == 1
    assert counts[UNRESOLVED] == 1
    assert counts[NOT_APPLICABLE] == 1


def test_conflicts_sort_ahead_of_matches_within_a_category():
    """Triage order: the fields a human has to look at come first."""
    rows = [
        _row("target", "filing-summary", "A"),
        _row("target", "agreement-ex2.1", "A"),
        _row("acquirer", "filing-summary", "Acme Corporation"),
        _row("acquirer", "agreement-ex2.1", "Zenith Holdings"),
    ]
    results = compare_layers(rows)
    assert [r.classification for r in results] == [CONFLICT, MATCH]


def test_export_shape_carries_both_readings_and_both_locations():
    """
    "Preserve both values and both source locations" is the requirement; the
    export is where that is checked.
    """
    rows = [
        _row(
            "company_termination_fee", "filing-summary", 250_000_000.0,
            raw="$250 million", printed_page="3", section="Item 1.01",
            evidence="a termination fee of $250 million",
            locator_uri="deallens://doc/p3",
        ),
        _row(
            "company_termination_fee", "agreement-ex2.1", 255_000_000.0,
            raw="$255,000,000", printed_page="A-47", section="Section 7.02",
            evidence="the Company Termination Fee shall be $255,000,000",
            locator_uri="deallens://doc/p47",
        ),
    ]
    payload = compare_layers(rows)[0].to_dict()

    assert payload["classification"] == CONFLICT
    assert payload["summary_value"] == 250_000_000.0
    assert payload["agreement_value"] == 255_000_000.0
    assert payload["summary_page"] == "3"
    assert payload["agreement_page"] == "A-47"
    assert payload["summary_locator"] and payload["agreement_locator"]
    assert payload["summary_evidence"] and payload["agreement_evidence"]
    assert payload["preferred_layer"] == "agreement-ex2.1"
    assert payload["critical"] is True


# ---------------------------------------------------------------------------
# Narrative fields: a summary summarises
# ---------------------------------------------------------------------------

def _text(summary_value, agreement_value, field_name="other_regulatory_approvals"):
    return compare_field(
        field_name,
        _reading(summary_value, layer="filing-summary"),
        _reading(agreement_value, layer="agreement-ex2.1"),
    )


def test_the_same_provision_worded_differently_is_not_a_conflict():
    """
    Taken verbatim from the Uber / Delivery Hero run. These say the same
    thing; calling them a conflict buried the one that mattered.
    """
    same_meaning = _text(
        "at least 50% of the number of Delivery Hero Shares as of the expiration "
        "of the acceptance period for the Offer plus one share, excluding treasury shares",
        "at least 50% plus one (1) share (in words: fifty per cent plus one share) "
        "of the Delivery Hero Shares, other than the Treasury Shares",
        field_name="tender_acceptance_threshold",
    )
    assert same_meaning.classification == NORMALIZED_MATCH
    assert "what a summary does" in same_meaning.reason


def test_a_summary_and_the_detail_behind_it_are_not_a_conflict():
    result = _text(
        "the receipt of specified financial services regulatory approvals",
        "Monetary Authority of Singapore approval under Art. 28 of the Payment "
        "Services Act 2019; Bank of Greece; Central Bank of Turkey approvals",
    )
    assert result.classification == NORMALIZED_MATCH


def test_clause_numbers_and_day_counts_are_not_disagreements():
    """
    Two layers citing different clauses is not two layers disagreeing, so only
    currency-prefixed amounts are compared inside prose.
    """
    result = _text(
        "termination by either party because a competing offer is announced",
        "termination pursuant to Clause 13.1(a)(ii) or 13.1(a)(i), within seven (7) "
        "Business Days",
        field_name="fee_triggers",
    )
    assert result.classification == NORMALIZED_MATCH


def test_disagreeing_amounts_inside_prose_are_still_a_conflict():
    """
    The real finding from the Uber run: the 8-K describes a EUR 14.2bn bridge
    facility and the agreement says EUR 11.5bn. That must survive the
    loosening, and it is caught inside narrative text.
    """
    result = _text(
        "Bridge Credit Agreement providing senior unsecured bridge loan commitments "
        "in an aggregate amount of €14,200,000,000, among the Company as borrower",
        "bridge facility arrangement providing committed financing of €11,500,000,000 "
        "with multiple global financial institutions",
        field_name="committed_financing",
    )
    assert result.classification == CONFLICT
    assert "14200000000" in result.reason.replace(",", "")
    assert "11500000000" in result.reason.replace(",", "")


def test_matching_amounts_inside_prose_are_not_a_conflict():
    result = _text(
        "a bridge facility of €11,500,000,000 committed by the lenders",
        "committed financing of EUR 11,500,000,000 under the Bridge Credit Agreement",
        field_name="committed_financing",
    )
    assert result.classification == NORMALIZED_MATCH


def test_typed_fields_are_still_compared_strictly():
    """
    The loosening is for narrative text only. Money, dates, percentages and
    enums normalize to canonical values, where a difference IS the
    disagreement.
    """
    money = compare_field(
        "company_termination_fee",
        _reading(250_000_000.0, "$250,000,000", layer="filing-summary"),
        _reading(255_000_000.0, "$255,000,000", layer="agreement-ex2.1"),
    )
    date = compare_field(
        "outside_date",
        _reading("2027-01-01", layer="filing-summary"),
        _reading("2027-06-25", layer="agreement-ex2.1"),
    )
    assert money.classification == CONFLICT
    assert date.classification == CONFLICT


def test_a_genuinely_different_short_value_is_still_a_conflict():
    """
    The narrative loosening must not reach party names. A shortened name is a
    summary using a short form; a different name is a different company.
    """
    shortened = compare_field(
        "target",
        _reading("Bio-Techne", layer="filing-summary"),
        _reading("Bio-Techne Corporation", layer="agreement-ex2.1"),
    )
    different = compare_field(
        "target",
        _reading("Acme Corporation", layer="filing-summary"),
        _reading("Bio-Techne Corporation", layer="agreement-ex2.1"),
    )
    assert shortened.classification == NORMALIZED_MATCH
    assert "shorter form" in shortened.reason
    assert different.classification == CONFLICT
