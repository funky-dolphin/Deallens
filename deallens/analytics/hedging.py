"""
hedging.py
Workstream 5 - Hedging and Financing Analytics
All inputs are synthetic assumptions unless extracted from the document.

Timing comes from Workstream 4. The delay scenarios take their dates from the
timeline's hedge horizon rather than deriving a close date here: the timeline
already computes it from the agreement's extension clause and shows the
arithmetic, and two modules deriving the same date from the same agreement is
two chances to disagree about it.
"""

from datetime import date


# Default synthetic assumptions for Bio-Techne (from assignment)
BIO_TECHNE_ASSUMPTIONS = {
    "financing": {
        "expected_debt_issuance_usd": 4_000_000_000,
        "expected_tenor_years": 7,
        "expected_issue_date": "2027-03-15",
        "debt_fixed_rate": True
    },
    "market": {
        "treasury_rate": 0.0425,
        "swap_rate": 0.0440,
        "issuer_credit_spread": 0.0100,
        "benchmark_dv01_per_100mm": 65_000
    },
    "transaction": {
        "base_case_close_probability": 0.85,
        "delayed_close_probability": 0.10,
        "failure_probability": 0.05
    }
}


def compute_dv01(notional_usd, benchmark_dv01_per_100mm):
    """
    Dollar Value of a Basis Point for a given notional.
    DV01 = (notional / 100mm) * benchmark_dv01
    """
    return (notional_usd / 100_000_000) * benchmark_dv01_per_100mm


def compute_rate_pnl(dv01, rate_shift_bps):
    """
    P&L impact of a rate move.
    Positive shift = rates up = bond price down = negative P&L for issuer hedging.
    rate_shift_bps: basis points (e.g. 25 for +25bps)
    """
    return -dv01 * rate_shift_bps


def compute_carry_cost(notional_usd, swap_rate, treasury_rate, delay_days):
    """
    Cost of carrying a forward-starting hedge across a closing delay.

    A hedge struck for an expected issuance date has to be rolled when closing
    slips. The carry is approximated as the swap spread -- the swap rate over
    the benchmark -- applied to the notional for the length of the delay:

        notional x (swap_rate - treasury_rate) x delay_days / 365

    This is the first use the swap spread has been put to; it was previously
    read from the assumptions and never referenced. The figure is an
    approximation and is labelled as a calculation, not an extracted fact.
    """
    return notional_usd * (swap_rate - treasury_rate) * (delay_days / 365.0)


def _delay_scenarios(horizon):
    """
    The two delay scenarios the assignment requires, dated from the timeline.

    Returns (label, delay_days, dated_to) triples. The dates come from
    Workstream 4's horizon rather than being derived again here: two modules
    computing a close date from the same agreement is two chances to disagree
    about it, and the timeline is the one that shows its arithmetic.

    With no horizon, or one without calculated extension dates, no delay is
    assumed. An invented delay would put a fabricated timing cost in front of
    someone sizing a hedge.
    """
    if horizon is None or not horizon.outside_date or not horizon.extension_dates:
        return []

    outside = date.fromisoformat(horizon.outside_date)
    dates = horizon.extension_dates
    chosen = [("first", dates[0])]
    if len(dates) > 1:
        chosen.append(("final", dates[-1]))

    return [
        (
            f"Closing delayed to {which} extension date",
            (date.fromisoformat(when) - outside).days,
            when,
        )
        for which, when in chosen
    ]


def run_scenarios(assumptions=None, horizon=None):
    """
    Run all required hedging scenarios.

    `horizon` is Workstream 4's HedgeHorizon. When supplied, the delay
    scenarios use its extension dates and say how many days each represents;
    without it they are reported as unavailable rather than assumed.

    Returns a list of scenario result dicts.
    """
    if assumptions is None:
        assumptions = BIO_TECHNE_ASSUMPTIONS

    notional = assumptions["financing"]["expected_debt_issuance_usd"]
    dv01_per_100mm = assumptions["market"]["benchmark_dv01_per_100mm"]
    treasury_rate = assumptions["market"]["treasury_rate"]
    swap_rate = assumptions["market"]["swap_rate"]
    credit_spread = assumptions["market"]["issuer_credit_spread"]
    close_prob = assumptions["transaction"]["base_case_close_probability"]
    delay_prob = assumptions["transaction"]["delayed_close_probability"]
    fail_prob = assumptions["transaction"]["failure_probability"]

    dv01 = compute_dv01(notional, dv01_per_100mm)

    scenarios = []

    # Rate scenarios
    rate_shifts = [
        ("Rates +25bps", 25),
        ("Rates -25bps", -25),
        ("Rates +50bps", 50),
    ]

    for name, shift in rate_shifts:
        for strategy in ["Unhedged", "Forward-Starting IRS Hedge", "Deal-Contingent Hedge"]:
            pnl = compute_rate_pnl(dv01, shift)
            if strategy == "Forward-Starting IRS Hedge":
                # Hedge offsets rate move, leaves swap spread and credit spread exposed
                hedge_pnl = -pnl  # hedge gains offset losses
                net_pnl = pnl + hedge_pnl
            elif strategy == "Deal-Contingent Hedge":
                # Only pays out if deal closes, weighted by probability
                hedge_pnl = -pnl * close_prob
                net_pnl = pnl + hedge_pnl
            else:
                net_pnl = pnl

            scenarios.append({
                "scenario": name,
                "strategy": strategy,
                "rate_shift_bps": shift,
                "credit_spread_shift_bps": 0,
                "dv01": dv01,
                "gross_pnl": pnl,
                "net_pnl": net_pnl,
                "note": "Synthetic assumptions — not extracted from agreement"
            })

    # Parallel rate + credit spread widening
    for strategy in ["Unhedged", "Forward-Starting IRS Hedge", "Deal-Contingent Hedge"]:
        rate_shift = 25
        spread_shift = 20
        rate_pnl = compute_rate_pnl(dv01, rate_shift)
        spread_pnl = compute_rate_pnl(dv01, spread_shift)
        gross = rate_pnl + spread_pnl

        if strategy == "Forward-Starting IRS Hedge":
            net_pnl = spread_pnl  # rate hedged, credit spread still exposed
        elif strategy == "Deal-Contingent Hedge":
            net_pnl = gross * (1 - close_prob)
        else:
            net_pnl = gross

        scenarios.append({
            "scenario": "Rates +25bps + Credit Spread +20bps",
            "strategy": strategy,
            "rate_shift_bps": rate_shift,
            "credit_spread_shift_bps": spread_shift,
            "dv01": dv01,
            "gross_pnl": gross,
            "net_pnl": net_pnl,
            "note": "Synthetic assumptions — not extracted from agreement"
        })

    # Transaction outcome scenarios
    for outcome, prob, label in [
        ("base_case_close", close_prob, "Base Case Close"),
        ("transaction_failure", fail_prob, "Transaction Failure"),
    ]:
        for strategy in ["Unhedged", "Forward-Starting IRS Hedge", "Deal-Contingent Hedge"]:
            if outcome == "transaction_failure":
                if strategy == "Forward-Starting IRS Hedge":
                    # Unwind cost — assume 10bps breakage
                    net_pnl = compute_rate_pnl(dv01, 10)
                elif strategy == "Deal-Contingent Hedge":
                    net_pnl = 0  # deal contingent hedge cancels on failure
                else:
                    net_pnl = 0
            else:
                net_pnl = 0

            scenarios.append({
                "scenario": label,
                "strategy": strategy,
                "rate_shift_bps": 0,
                "credit_spread_shift_bps": 0,
                "dv01": dv01,
                "gross_pnl": net_pnl,
                "net_pnl": net_pnl,
                "probability": prob,
                "note": "Synthetic assumptions — not extracted from agreement"
            })

    # Timing scenarios, dated from the Workstream 4 timeline.
    delays = _delay_scenarios(horizon)
    if not delays:
        for strategy in ["Unhedged", "Forward-Starting IRS Hedge", "Deal-Contingent Hedge"]:
            scenarios.append({
                "scenario": "Closing delayed (no extension dates available)",
                "strategy": strategy,
                "rate_shift_bps": 0,
                "credit_spread_shift_bps": 0,
                "dv01": dv01,
                "gross_pnl": None,
                "net_pnl": None,
                "delay_days": None,
                "probability": delay_prob,
                "note": "No extension dates on the timeline; no delay assumed.",
            })
        return scenarios

    for label, delay_days, dated_to in delays:
        carry = compute_carry_cost(notional, swap_rate, treasury_rate, delay_days)
        for strategy in ["Unhedged", "Forward-Starting IRS Hedge", "Deal-Contingent Hedge"]:
            if strategy == "Forward-Starting IRS Hedge":
                # The hedge has to be rolled to the later issuance date.
                net_pnl = -carry
            elif strategy == "Deal-Contingent Hedge":
                # Extension is embedded; the premium already prices timing.
                net_pnl = 0.0
            else:
                # Nothing to carry, but the issuance is exposed for longer.
                net_pnl = 0.0

            scenarios.append({
                "scenario": label,
                "strategy": strategy,
                "rate_shift_bps": 0,
                "credit_spread_shift_bps": 0,
                "dv01": dv01,
                "gross_pnl": net_pnl,
                "net_pnl": net_pnl,
                "delay_days": delay_days,
                "delayed_to": dated_to,
                "probability": delay_prob,
                "note": (
                    f"Delay of {delay_days} days to {dated_to}, from the extension "
                    "clause via the Workstream 4 timeline (a calculated date). "
                    "Carry priced on the synthetic swap spread."
                ),
            })

    return scenarios
