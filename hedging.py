"""
hedging.py
Workstream 5 - Hedging and Financing Analytics
All inputs are synthetic assumptions unless extracted from the document.
"""


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


def run_scenarios(assumptions=None):
    """
    Run all required hedging scenarios.
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
        ("delayed_close", delay_prob, "Delayed Close"),
        ("transaction_failure", fail_prob, "Transaction Failure")
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
                net_pnl = 0  # simplified — timing cost omitted

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

    return scenarios
