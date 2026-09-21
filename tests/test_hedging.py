"""
Tests for the hedging and financing analytics (Workstream 5).

These pin the economics, not just the plumbing. The sign convention, what each
hedge does and does not cover, and the completeness of the required scenario
and risk grids are all things that can be wrong while the code still runs and
produces a table.
"""

from __future__ import annotations

import pytest

from deallens.analytics.hedging import (
    ADDITIONAL_ASSUMPTIONS,
    BENCHMARK_RATE,
    BIO_TECHNE_ASSUMPTIONS,
    COMPLETION,
    CREDIT_SPREAD,
    DEAL_CONTINGENT,
    FORWARD_STARTING,
    RISK_FACTORS,
    STRATEGIES,
    SWAP_SPREAD,
    TIMING,
    UNHEDGED,
    UNWIND,
    DealCharacteristics,
    compute_carry_cost,
    compute_dv01,
    compute_rate_pnl,
    deal_from_rows,
    probability_weighted,
    risk_exposures,
    run_scenarios,
)
from deallens.extraction import models
from deallens.timeline import build_timeline

DV01 = 2_600_000.0  # $4bn at $65,000 per $100mm


def _results(horizon=None, deal=None):
    return run_scenarios(horizon=horizon, deal=deal)


def _find(results, scenario_id, strategy):
    return next(
        r for r in results if r.scenario_id == scenario_id and r.strategy == strategy
    )


def _row(field_name, value, layer="agreement-ex2.1"):
    return {
        "field_name": field_name, "document_layer": layer, "normalized_value": value,
        "raw_value": str(value), "status": models.FOUND, "currency": None,
        "printed_page": "A-1", "pdf_page": 1, "section": None, "evidence": "q",
        "locator_uri": "deallens://d/x", "confidence": 0.95,
        "review_status": models.UNREVIEWED,
    }


def _horizon():
    rows = [
        _row("agreement_date", "2026-06-25"),
        _row("outside_date", "2027-06-25"),
        _row("extension_conditions", "two successive periods of ninety (90) days"),
    ]
    return build_timeline(rows).horizon


# ---------------------------------------------------------------------------
# The required grid
# ---------------------------------------------------------------------------

def test_every_required_scenario_is_present():
    """The assignment names seven. Missing one is a missing deliverable."""
    ids = {r.scenario_id for r in _results(horizon=_horizon())}
    assert ids == {
        "rates_up_25",
        "rates_down_25",
        "rates_up_50",
        "rates_and_credit",
        "delay_first_extension",
        "delay_final_extension",
        "transaction_failure",
    }


def test_every_scenario_is_priced_for_every_strategy():
    results = _results(horizon=_horizon())
    by_scenario: dict[str, set[str]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario_id, set()).add(result.strategy)
    assert all(strategies == set(STRATEGIES) for strategies in by_scenario.values())


def test_every_result_reports_all_seven_risks():
    """
    "This strategy is not exposed to that risk" and "we did not consider that
    risk" are different statements, so a zero is reported rather than omitted.
    """
    for result in _results(horizon=_horizon()):
        assert set(result.attribution) == set(RISK_FACTORS)


def test_results_carry_a_scenario_id_and_assumptions_version():
    """Workstream 8: versioned assumptions and scenario identifiers."""
    for result in _results(horizon=_horizon()):
        assert result.scenario_id
        assert result.assumptions_version


# ---------------------------------------------------------------------------
# Sign convention and the core arithmetic
# ---------------------------------------------------------------------------

def test_dv01_matches_the_assignment_inputs():
    assert compute_dv01(4_000_000_000, 65_000) == DV01


def test_rising_rates_cost_the_pre_issuance_issuer():
    """The coupon is not yet fixed, so higher rates are a loss, not a gain."""
    assert compute_rate_pnl(DV01, 25) < 0
    assert compute_rate_pnl(DV01, -25) > 0
    assert compute_rate_pnl(DV01, 25) == -65_000_000


def test_the_unhedged_issuer_bears_the_whole_rate_move():
    up = _find(_results(), "rates_up_25", UNHEDGED)
    down = _find(_results(), "rates_down_25", UNHEDGED)

    assert up.attribution[BENCHMARK_RATE] == -65_000_000
    assert down.attribution[BENCHMARK_RATE] == 65_000_000


def test_the_hedge_removes_benchmark_exposure_and_the_upside_with_it():
    """A hedge is not free money: it gives up the gain as well as the loss."""
    for scenario_id in ("rates_up_25", "rates_down_25", "rates_up_50"):
        for strategy in (FORWARD_STARTING, DEAL_CONTINGENT):
            result = _find(_results(), scenario_id, strategy)
            assert result.attribution[BENCHMARK_RATE] == 0.0


def test_a_bigger_rate_move_costs_proportionally_more():
    twenty_five = _find(_results(), "rates_up_25", UNHEDGED).net_pnl
    fifty = _find(_results(), "rates_up_50", UNHEDGED).net_pnl
    assert fifty == pytest.approx(2 * twenty_five)


# ---------------------------------------------------------------------------
# What the hedges do not cover
# ---------------------------------------------------------------------------

def test_no_rate_hedge_covers_the_issuers_own_credit_spread():
    """
    The most important thing the risk separation shows. A forward-starting
    swap references the swap rate; the bond prices off the benchmark plus the
    issuer's own spread, and nothing here hedges that.
    """
    results = _results()
    costs = {
        strategy: _find(results, "rates_and_credit", strategy).attribution[CREDIT_SPREAD]
        for strategy in STRATEGIES
    }
    assert costs[UNHEDGED] == -52_000_000  # 2.6mm x 20bp
    assert costs[FORWARD_STARTING] == costs[UNHEDGED]
    assert costs[DEAL_CONTINGENT] == costs[UNHEDGED]


def test_the_credit_scenario_hurts_a_hedged_issuer_too():
    results = _results()
    hedged = _find(results, "rates_and_credit", FORWARD_STARTING)
    assert hedged.net_pnl < 0
    assert any("credit spread" in note for note in hedged.notes)


def test_only_a_hedger_carries_swap_spread_basis():
    """The unhedged issuer holds no instrument priced off the swap rate."""
    exposures = {row["strategy"]: row for row in risk_exposures()}
    assert exposures[UNHEDGED][SWAP_SPREAD] == 0.0
    assert exposures[FORWARD_STARTING][SWAP_SPREAD] < 0
    assert exposures[DEAL_CONTINGENT][SWAP_SPREAD] < 0


def test_the_unhedged_issuer_has_no_benchmark_hedge_but_full_benchmark_exposure():
    exposures = {row["strategy"]: row for row in risk_exposures()}
    assert exposures[UNHEDGED][BENCHMARK_RATE] == -DV01
    assert exposures[FORWARD_STARTING][BENCHMARK_RATE] == 0.0


# ---------------------------------------------------------------------------
# Completion, unwind and timing
# ---------------------------------------------------------------------------

def test_the_deal_contingent_premium_is_paid_even_when_it_buys_nothing():
    """It is the price of the contingency, not a charge for using it."""
    results = _results(horizon=_horizon())
    for scenario_id in ("rates_up_25", "transaction_failure", "delay_first_extension"):
        result = _find(results, scenario_id, DEAL_CONTINGENT)
        assert result.attribution[COMPLETION] == -DV01 * 15.0

    assert _find(results, "rates_up_25", UNHEDGED).attribution[COMPLETION] == 0.0
    assert _find(results, "rates_up_25", FORWARD_STARTING).attribution[COMPLETION] == 0.0


def test_on_failure_the_conventional_swap_is_left_to_unwind():
    """
    The deal-contingent structure exists to avoid exactly this, so the two
    must not report the same thing.
    """
    results = _results(horizon=_horizon())
    conventional = _find(results, "transaction_failure", FORWARD_STARTING)
    contingent = _find(results, "transaction_failure", DEAL_CONTINGENT)
    unhedged = _find(results, "transaction_failure", UNHEDGED)

    assert conventional.attribution[UNWIND] == -DV01 * 2.0
    assert contingent.attribution[UNWIND] == 0.0
    assert unhedged.attribution[UNWIND] == 0.0


def test_a_failed_transaction_has_no_rate_exposure_for_anyone():
    """No issuance means no coupon to be exposed on."""
    for strategy in STRATEGIES:
        result = _find(_results(horizon=_horizon()), "transaction_failure", strategy)
        assert result.attribution[BENCHMARK_RATE] == 0.0
        assert result.attribution[CREDIT_SPREAD] == 0.0


def test_a_delay_costs_the_conventional_hedge_its_carry():
    results = _results(horizon=_horizon())
    first = _find(results, "delay_first_extension", FORWARD_STARTING)
    final = _find(results, "delay_final_extension", FORWARD_STARTING)

    assert first.delay_days == 90 and first.delayed_to == "2027-09-23"
    assert final.delay_days == 180 and final.delayed_to == "2027-12-22"
    assert first.attribution[TIMING] < 0
    assert final.attribution[TIMING] < first.attribution[TIMING]


def test_without_a_timeline_no_delay_is_assumed():
    results = _results(horizon=None)
    delays = [r for r in results if r.scenario_id.startswith("delay_")]

    assert delays, "the scenarios are still reported"
    assert all(r.delay_days is None for r in delays)
    assert all(r.net_pnl == 0.0 for r in delays)
    assert all("no delay assumed" in " ".join(r.notes).lower() for r in delays)


def test_the_carry_cost_uses_the_swap_spread():
    """treasury_rate and swap_rate were previously read and never used."""
    cost = compute_carry_cost(4_000_000_000, 0.0440, 0.0425, 180)
    assert cost == pytest.approx(4_000_000_000 * 0.0015 * 180 / 365)
    assert compute_carry_cost(4_000_000_000, 0.0440, 0.0425, 0) == 0


# ---------------------------------------------------------------------------
# FX comes from the deal, not from an assumption
# ---------------------------------------------------------------------------

def test_a_dollar_deal_has_no_fx_exposure():
    deal = DealCharacteristics(consideration_currency="USD")
    assert not deal.has_fx_exposure
    assert risk_exposures(deal=deal)[0]["fx"] == "none"


def test_a_euro_deal_reports_fx_exposure():
    """The Uber / Delivery Hero case. Hard-coding zero would hide it."""
    deal = DealCharacteristics(consideration_currency="EUR", bridge_currency="EUR")
    assert deal.foreign_currencies == ["EUR"]
    assert "EUR" in risk_exposures(deal=deal)[0]["fx"]

    result = _find(_results(deal=deal), "rates_up_25", UNHEDGED)
    assert any("EUR" in note for note in result.notes)


def test_fx_exposure_is_read_from_the_extraction():
    deal = deal_from_rows([
        _row("consideration_currency", "EUR"),
        _row("bridge_currency", "EUR"),
    ])
    assert deal.has_fx_exposure
    assert deal.foreign_currencies == ["EUR"]


def test_fx_is_reported_as_an_exposure_rather_than_priced():
    """No FX rate or volatility is supplied, so a number would be invented."""
    deal = DealCharacteristics(consideration_currency="EUR")
    result = _find(_results(deal=deal), "rates_up_25", UNHEDGED)
    assert result.attribution["fx"] == 0.0
    assert any("rather than priced" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Assumptions and weighting
# ---------------------------------------------------------------------------

def test_the_assignments_inputs_are_unchanged():
    """These are given, and must not drift."""
    assert BIO_TECHNE_ASSUMPTIONS["financing"]["expected_debt_issuance_usd"] == 4_000_000_000
    assert BIO_TECHNE_ASSUMPTIONS["market"]["benchmark_dv01_per_100mm"] == 65_000
    assert BIO_TECHNE_ASSUMPTIONS["transaction"]["failure_probability"] == 0.05


def test_every_added_assumption_explains_why_it_is_required():
    for name, (value, reason) in ADDITIONAL_ASSUMPTIONS.items():
        assert isinstance(value, float), name
        assert len(reason) > 40, f"{name} needs a reason, not a label"


def test_the_outcome_probabilities_sum_to_one():
    """
    The assignment gives one delayed-close probability and asks for two delay
    scenarios. Giving each the full 0.10 would put the outcomes at 1.10, so it
    is split: the two are alternative ways one outcome plays out.
    """
    results = _results(horizon=_horizon())
    per_scenario = {
        r.scenario_id: r.probability
        for r in results
        if r.strategy == UNHEDGED and r.probability is not None
    }
    base = BIO_TECHNE_ASSUMPTIONS["transaction"]["base_case_close_probability"]

    assert sum(per_scenario.values()) + base == pytest.approx(1.0)
    assert per_scenario["delay_first_extension"] == pytest.approx(0.05)
    assert per_scenario["delay_final_extension"] == pytest.approx(0.05)
    assert per_scenario["transaction_failure"] == pytest.approx(0.05)


def test_only_outcome_scenarios_are_probability_weighted():
    """
    Rate shocks are sensitivities, not outcomes with a likelihood. Averaging
    them in would mix two different questions.
    """
    results = _results(horizon=_horizon())
    weighted = probability_weighted(results)

    assert set(weighted) == set(STRATEGIES)
    rate_shocks = [r for r in results if r.scenario_id.startswith("rates_")]
    assert all(r.probability is None for r in rate_shocks)


def test_the_export_shape_carries_every_risk_column():
    payload = _results(horizon=_horizon())[0].to_dict()
    for factor in RISK_FACTORS:
        assert factor in payload
    assert payload["net_pnl"] == pytest.approx(
        sum(payload[factor] for factor in RISK_FACTORS)
    )


# ---------------------------------------------------------------------------
# Adapting the analytics to the extracted deal (WS5 + WS7)
# ---------------------------------------------------------------------------

def _fin_row(field_name, value):
    return {
        "field_name": field_name, "document_layer": "credit-agreement-ex10.1",
        "normalized_value": value, "raw_value": str(value), "status": models.FOUND,
        "currency": None, "printed_page": "1", "pdf_page": 44, "section": None,
        "evidence": "q", "locator_uri": "d://x", "confidence": 0.95,
        "review_status": models.UNREVIEWED,
    }


def test_the_notional_comes_from_the_filing_when_it_states_one():
    """
    The analysis previously priced a USD 4bn seven-year issuance for every
    deal, including one whose disclosed facility is EUR 14.2bn over 364 days —
    with all of those facts already extracted.
    """
    from deallens.analytics.hedging import EXTRACTED, resolve_financing

    resolved = resolve_financing(
        BIO_TECHNE_ASSUMPTIONS,
        [_fin_row("bridge_amount", 11_500_000_000.0),
         _fin_row("bridge_currency", "EUR"),
         _fin_row("bridge_maturity", "364 days after the Closing Date")],
    )
    assert resolved.notional.value == 11_500_000_000.0
    assert resolved.notional.source == EXTRACTED
    assert resolved.currency.value == "EUR"
    assert set(resolved.extracted) >= {"notional", "currency", "tenor"}


def test_assumptions_are_used_where_the_filing_is_silent():
    from deallens.analytics.hedging import ASSUMED, resolve_financing

    resolved = resolve_financing(BIO_TECHNE_ASSUMPTIONS, [])
    assert resolved.notional.value == 4_000_000_000
    assert resolved.notional.source == ASSUMED
    assert resolved.tenor_years.value == 7
    assert resolved.extracted == []


def test_dv01_follows_the_stated_tenor():
    """
    A 364-day facility carries roughly a seventh of the rate duration of a
    seven-year issuance. Ignoring tenor overstated its sensitivity sevenfold.
    """
    from deallens.analytics.hedging import DERIVED, resolve_financing

    seven_year = resolve_financing(BIO_TECHNE_ASSUMPTIONS, [])
    one_year = resolve_financing(
        BIO_TECHNE_ASSUMPTIONS,
        [_fin_row("bridge_amount", 4_000_000_000.0),
         _fin_row("bridge_maturity", "364 days after the Closing Date")],
    )
    assert seven_year.dv01.value == pytest.approx(DV01)
    assert float(one_year.dv01.value) == pytest.approx(DV01 * 364 / 365 / 7, rel=1e-3)
    assert one_year.dv01.source == DERIVED


@pytest.mark.parametrize(
    "text,years",
    [
        ("364 days after the Closing Date", 364 / 365),
        ("5 years from Closing", 5.0),
        ("18 months", 1.5),
        ("a 364-day facility", 364 / 365),
        ("no stated maturity", None),
        (None, None),
    ],
)
def test_tenor_parsing(text, years):
    from deallens.analytics.hedging import parse_tenor_years

    parsed = parse_tenor_years(text)
    if years is None:
        assert parsed is None
    else:
        assert parsed == pytest.approx(years, rel=1e-3)


def test_results_report_the_deals_own_currency():
    """No FX rate is supplied, so a EUR facility is reported in EUR rather
    than converted at an invented one."""
    rows = [_fin_row("bridge_amount", 11_500_000_000.0),
            _fin_row("bridge_currency", "EUR")]
    results = run_scenarios(rows=rows)

    assert all(r.currency == "EUR" for r in results)
    assert all("notional" in r.extracted_inputs for r in results)


def test_two_different_deals_no_longer_price_identically():
    """The defect this closes: every filing produced the same grid."""
    usd = run_scenarios(rows=[])
    eur = run_scenarios(rows=[_fin_row("bridge_amount", 11_500_000_000.0),
                              _fin_row("bridge_maturity", "364 days")])

    a = _find(usd, "rates_up_25", UNHEDGED).net_pnl
    b = _find(eur, "rates_up_25", UNHEDGED).net_pnl
    assert a != b
    assert abs(b) < abs(a), "a 364-day facility is less rate-sensitive"
