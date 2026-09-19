"""
Tests for the transaction timeline and risk map (Workstream 4).

Two things carry the weight here. First, that entries with no calendar
position are never given one -- the assignment requires relative, conditional
and election-dependent dates be told apart from fixed ones, and a timeline
that quietly places them is the failure this module exists to prevent.
Second, that calculated dates are labelled as calculated and show their
arithmetic, and that unreadable extension language produces no date at all.
"""

from __future__ import annotations

from deallens.extraction import models
from deallens.timeline import (
    AUTO_EXTENDED,
    CALCULATED,
    CONDITIONAL,
    ELECTION,
    ESTIMATE,
    EXTRACTED,
    FIXED,
    RELATIVE,
    build_timeline,
    parse_extension,
)


def _row(field_name, value, raw=None, layer="agreement-ex2.1", page="A-9", **kw):
    row = {
        "field_name": field_name,
        "document_layer": layer,
        "normalized_value": value,
        "raw_value": raw if raw is not None else (str(value) if value is not None else None),
        "status": models.FOUND if value is not None else models.NOT_FOUND,
        "currency": None,
        "printed_page": page,
        "pdf_page": 9,
        "section": "Section 8.01",
        "evidence": f"quote for {field_name}",
        "locator_uri": f"deallens://doc/{field_name}",
        "confidence": 0.95,
        "review_status": models.UNREVIEWED,
    }
    row.update(kw)
    return row


def _base_rows(**overrides):
    rows = [
        _row("agreement_date", "2026-06-25"),
        _row("outside_date", "2027-06-25"),
    ]
    for field_name, value in overrides.items():
        rows.append(_row(field_name, value))
    return rows


# ---------------------------------------------------------------------------
# The extension parser
# ---------------------------------------------------------------------------

def test_parses_the_drafting_convention_of_word_then_digits():
    """Legal drafting spells the number then repeats it; trust the digits."""
    assert parse_extension(
        "extended for two successive periods of ninety (90) days"
    ) == (2, 90, "day")


def test_parses_a_single_unqualified_extension():
    assert parse_extension("may be extended by three (3) months") == (1, 3, "month")


def test_parses_twice():
    assert parse_extension("may be extended twice by 90 days") == (2, 90, "day")


def test_parses_a_bare_hyphenated_duration():
    assert parse_extension("two 90-day extensions") == (2, 90, "day")


def test_unreadable_extension_language_parses_to_nothing():
    """
    The safe outcome, and the common one. A wrong worst-case close date in
    front of someone sizing a hedge is worse than no worst-case date.
    """
    assert parse_extension("may be extended as the parties agree in writing") is None
    assert parse_extension("subject to such extensions as may be necessary") is None
    assert parse_extension(None) is None
    assert parse_extension("") is None


# ---------------------------------------------------------------------------
# Calculated dates
# ---------------------------------------------------------------------------

def test_extension_dates_are_calculated_and_labelled_as_calculated():
    rows = _base_rows(
        extension_conditions="The Outside Date may be extended by either party "
        "for two successive periods of ninety (90) days if antitrust clearance "
        "remains outstanding."
    )
    timeline = build_timeline(rows)
    derived = timeline.calculated

    assert [e.resolved_date for e in derived] == ["2027-09-23", "2027-12-22"]
    assert all(e.basis == CALCULATED for e in derived)
    assert derived[0].derivation == "2027-06-25 + 1 x 90 days"
    assert derived[1].derivation == "2027-06-25 + 2 x 90 days"
    # Every calculated entry still cites the clause it was derived from.
    assert all(e.locator_uri for e in derived)
    assert all(e.evidence for e in derived)


def test_extracted_dates_are_not_labelled_calculated():
    timeline = build_timeline(_base_rows())
    assert all(e.basis == EXTRACTED for e in timeline.entries)
    assert timeline.calculated == []


def test_no_dates_are_calculated_when_the_clause_cannot_be_read():
    rows = _base_rows(
        extension_conditions="The Outside Date may be extended as the parties agree."
    )
    timeline = build_timeline(rows)

    assert timeline.calculated == []
    extension = next(e for e in timeline.entries if e.field_name == "extension_conditions")
    assert "could not be read" in (extension.trigger or "")


def test_nothing_is_calculated_without_a_fixed_outside_date():
    rows = [
        _row("agreement_date", "2026-06-25"),
        _row("outside_date", None, raw="the first anniversary of the date hereof",
             status=models.UNRESOLVED),
        _row("extension_conditions", "extended for two periods of ninety (90) days"),
    ]
    assert build_timeline(rows).calculated == []


def test_months_are_counted_as_thirty_days_and_say_so():
    rows = _base_rows(extension_conditions="may be extended by three (3) months")
    derived = build_timeline(rows).calculated
    assert derived[0].resolved_date == "2027-09-23"
    assert "months counted as 30 days" in derived[0].derivation


# ---------------------------------------------------------------------------
# Anchored vs unanchored
# ---------------------------------------------------------------------------

def test_undatable_entries_are_never_given_a_calendar_position():
    """
    "30 days after written notice" has no place on a calendar until the
    notice exists. Putting it at one would invent a date.
    """
    rows = _base_rows(
        cure_periods="30 days after written notice of breach",
        expected_closing_timing="the second half of 2026",
    )
    timeline = build_timeline(rows)

    unanchored = {e.field_name for e in timeline.unanchored}
    assert {"cure_periods", "expected_closing_timing"} <= unanchored
    assert all(e.resolved_date is None for e in timeline.unanchored)


def test_anchored_entries_come_back_in_calendar_order():
    rows = _base_rows(
        extension_conditions="extended for two successive periods of ninety (90) days"
    )
    dates = [e.resolved_date for e in build_timeline(rows).anchored]
    assert dates == sorted(dates)
    assert dates[0] == "2026-06-25"


def test_expected_closing_is_a_non_binding_estimate_not_a_date():
    rows = _base_rows(expected_closing_timing="the second half of 2026")
    entry = next(
        e for e in build_timeline(rows).entries if e.field_name == "expected_closing_timing"
    )
    assert entry.kind == ESTIMATE
    assert entry.resolved_date is None


def test_a_date_field_that_would_not_normalize_is_relative_not_fixed():
    rows = [
        _row("agreement_date", "2026-06-25"),
        _row("outside_date", None, raw="the first anniversary of the date hereof",
             status=models.UNRESOLVED),
    ]
    entry = next(e for e in build_timeline(rows).entries if e.field_name == "outside_date")
    assert entry.kind == RELATIVE
    assert entry.resolved_date is None
    # The withheld reading still has to show what the document said, and where.
    assert entry.stated_as == "the first anniversary of the date hereof"
    assert entry.page == "A-9"
    assert entry.locator_uri


def test_automatic_and_elective_extensions_are_told_apart():
    """An automatic extension moves the date on its own; an elective one only
    moves it if someone acts. Different exposure, different kind."""
    automatic = build_timeline(
        _base_rows(
            extension_conditions="the Outside Date shall be extended automatically "
            "by ninety (90) days"
        )
    )
    elective = build_timeline(
        _base_rows(
            extension_conditions="either party may extend the Outside Date by "
            "ninety (90) days"
        )
    )

    assert next(
        e for e in automatic.entries if e.field_name == "extension_conditions"
    ).kind == AUTO_EXTENDED
    assert next(
        e for e in elective.entries if e.field_name == "extension_conditions"
    ).kind == ELECTION


def test_regulatory_approvals_are_conditional():
    rows = _base_rows(antitrust_approvals="HSR clearance and EU merger clearance")
    entry = next(
        e for e in build_timeline(rows).entries if e.field_name == "antitrust_approvals"
    )
    assert entry.kind == CONDITIONAL
    assert entry.resolved_date is None


# ---------------------------------------------------------------------------
# The hedge horizon, which WS5 consumes
# ---------------------------------------------------------------------------

def test_the_horizon_measures_the_window_a_hedge_must_cover():
    rows = _base_rows(
        extension_conditions="extended for two successive periods of ninety (90) days",
        antitrust_approvals="HSR clearance",
        financing_conditions="customary conditions",
    )
    horizon = build_timeline(rows).horizon

    assert horizon.signing_date == "2026-06-25"
    assert horizon.outside_date == "2027-06-25"
    assert horizon.base_days == 365
    assert horizon.extension_days == 180
    assert horizon.total_days == 545
    assert horizon.final_outside_date == "2027-12-22"
    assert horizon.outstanding_conditions == 2
    assert horizon.is_complete


def test_the_horizon_says_when_it_cannot_be_built():
    rows = [_row("agreement_date", "2026-06-25")]
    horizon = build_timeline(rows).horizon

    assert not horizon.is_complete
    assert horizon.total_days is None
    assert any("no fixed signing and outside date" in n for n in horizon.notes)


def test_an_unquantified_extension_leaves_the_window_unextended_and_says_so():
    rows = _base_rows(
        extension_conditions="may be extended as the parties agree in writing"
    )
    horizon = build_timeline(rows).horizon

    assert horizon.base_days == 365
    assert horizon.extension_days is None
    assert horizon.total_days == 365
    assert any("not quantified" in n for n in horizon.notes)


def test_the_horizon_flags_that_its_extended_date_is_calculated():
    rows = _base_rows(
        extension_conditions="extended for two successive periods of ninety (90) days"
    )
    horizon = build_timeline(rows).horizon
    assert any("calculated" in n for n in horizon.notes)


def test_the_export_carries_both_sections_and_the_horizon():
    rows = _base_rows(
        cure_periods="30 days after written notice",
        extension_conditions="extended for two successive periods of ninety (90) days",
    )
    payload = build_timeline(rows).to_dict()

    assert payload["horizon"]["total_days"] == 545
    assert any(e["basis"] == CALCULATED for e in payload["anchored"])
    assert any(e["field_name"] == "cure_periods" for e in payload["unanchored"])
    assert all("locator_uri" in e for e in payload["anchored"])
