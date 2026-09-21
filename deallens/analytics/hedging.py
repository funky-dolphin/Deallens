"""
Hedging and financing analytics (Workstream 5).

The issuer plans to raise USD 4bn of seven-year fixed-rate debt to fund a cash
merger. Between signing and issuance it is exposed to rates, to spreads, and
to the deal itself not closing. This module prices the seven required
scenarios against the three required strategies, and attributes every result
to one of the seven risks the assignment requires be kept apart.

Sign convention
---------------
Positive is a gain to the issuer, negative a cost. The issuer has not yet
issued, so a rise in rates raises the coupon it will pay and is a loss; a fall
is a gain. A forward-starting payer swap gains when rates rise, which is what
makes it a hedge. Every figure below is the change against the base case, not
an absolute funding cost.

What is a fact, what is an assumption, what is a calculation
------------------------------------------------------------
Nothing here is extracted from the agreement except the deal's currencies and
the closing dates, and both are marked as such where they are used. The market
and financing inputs are the assignment's standardized synthetic assumptions.
Where the assignment leaves something unspecified -- the size of the "parallel
rate move", what a deal-contingent hedge costs -- the gap is filled in
`ADDITIONAL_ASSUMPTIONS`, each with the reason it is needed. Everything else
is arithmetic on those two sets.

What the hedges do and do not cover
-----------------------------------
This is the point of separating the risks. A forward-starting swap references
the swap rate; the bond will price off the benchmark plus the issuer's own
credit spread. So the swap neutralises benchmark-rate risk, leaves swap-spread
basis behind, and does nothing at all about issuer credit spread -- which is
why scenario 4 hurts every strategy. A deal-contingent hedge adds termination
without breakage if the deal fails, and charges a premium for it in every
scenario, including the ones where the deal closes and the premium buys
nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Bumped whenever a number below changes, and recorded on every result, so a
# figure can be traced to the inputs that produced it (Workstream 8).
ASSUMPTIONS_VERSION = "2.0.0"

# The assignment's standardized Bio-Techne inputs, verbatim. Synthetic: these
# must never be represented as facts extracted from the agreement.
BIO_TECHNE_ASSUMPTIONS = {
    "financing": {
        "expected_debt_issuance_usd": 4_000_000_000,
        "expected_tenor_years": 7,
        "expected_issue_date": "2027-03-15",
        "debt_fixed_rate": True,
    },
    "market": {
        "treasury_rate": 0.0425,
        "swap_rate": 0.0440,
        "issuer_credit_spread": 0.0100,
        "benchmark_dv01_per_100mm": 65_000,
    },
    "transaction": {
        "base_case_close_probability": 0.85,
        "delayed_close_probability": 0.10,
        "failure_probability": 0.05,
    },
}

# Inputs the assignment does not supply but the required scenarios cannot be
# priced without. Each carries the reason it is required, and each is as
# synthetic as the block above.
ADDITIONAL_ASSUMPTIONS = {
    "parallel_rate_move_bps": (
        25.0,
        "Scenario 4 specifies a parallel rate move plus 20bp of credit widening "
        "but not the size of the rate move. 25bp is used, matching scenario 1, "
        "so the credit-spread effect is the only difference between them.",
    ),
    "deal_contingent_premium_bps": (
        15.0,
        "A deal-contingent hedge is paid for through a concession on the hedge "
        "rate. No premium is supplied, so 15bp of rate is assumed -- within the "
        "range such structures typically cost, and charged in every scenario "
        "because it is paid whether or not the contingency is used.",
    ),
    "unwind_bid_offer_bps": (
        2.0,
        "Unwinding a conventional swap early crosses the bid-offer. No breakage "
        "cost is supplied, so 2bp is assumed. This is the cost a deal-contingent "
        "structure exists to avoid.",
    ),
    "credit_spread_dv01_ratio": (
        1.0,
        "Only a benchmark DV01 is supplied. Spread duration on a seven-year bond "
        "is close to its rate duration, so the issuer's credit-spread DV01 is "
        "taken as equal to it. Re-measure before using these figures to trade.",
    ),
    "swap_spread_dv01_ratio": (
        1.0,
        "As above, for the basis between the swap rate the hedge references and "
        "the benchmark the bond prices off.",
    ),
}

# The seven risks the assignment requires be reported separately.
BENCHMARK_RATE = "benchmark_rate"
SWAP_SPREAD = "swap_spread"
CREDIT_SPREAD = "issuer_credit_spread"
FX = "fx"
TIMING = "timing"
COMPLETION = "transaction_completion"
UNWIND = "hedge_unwind"

RISK_FACTORS = (
    BENCHMARK_RATE,
    SWAP_SPREAD,
    CREDIT_SPREAD,
    FX,
    TIMING,
    COMPLETION,
    UNWIND,
)

RISK_LABELS = {
    BENCHMARK_RATE: "Benchmark rate",
    SWAP_SPREAD: "Swap spread",
    CREDIT_SPREAD: "Issuer credit spread",
    FX: "FX",
    TIMING: "Timing",
    COMPLETION: "Completion",
    UNWIND: "Unwind / breakage",
}

# The three required strategies.
UNHEDGED = "Unhedged"
FORWARD_STARTING = "Forward-starting IRS"
DEAL_CONTINGENT = "Deal-contingent hedge"
STRATEGIES = (UNHEDGED, FORWARD_STARTING, DEAL_CONTINGENT)


@dataclass(frozen=True)
class Scenario:
    """One required scenario, with a stable identifier."""

    scenario_id: str
    label: str
    rate_shift_bps: float = 0.0
    credit_spread_shift_bps: float = 0.0
    swap_spread_shift_bps: float = 0.0
    delay_days: int | None = None
    delayed_to: str | None = None
    transaction_fails: bool = False
    probability: float | None = None
    note: str = ""


@dataclass
class ScenarioResult:
    """One scenario under one strategy, attributed across the seven risks."""

    scenario_id: str
    scenario: str
    strategy: str
    attribution: dict[str, float] = field(default_factory=dict)
    probability: float | None = None
    delay_days: int | None = None
    delayed_to: str | None = None
    # Figures are in the deal's own currency. No FX rate is supplied, so a
    # EUR facility is reported in EUR rather than converted at an invented one.
    currency: str = "USD"
    extracted_inputs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    assumptions_version: str = ASSUMPTIONS_VERSION

    @property
    def net_pnl(self) -> float:
        return sum(self.attribution.values())

    def to_dict(self) -> dict:
        payload = {
            "scenario_id": self.scenario_id,
            "scenario": self.scenario,
            "strategy": self.strategy,
            "probability": self.probability,
            "delay_days": self.delay_days,
            "delayed_to": self.delayed_to,
            "currency": self.currency,
            "extracted_inputs": ", ".join(self.extracted_inputs),
            "net_pnl": self.net_pnl,
            "assumptions_version": self.assumptions_version,
            "notes": list(self.notes),
        }
        payload.update({factor: self.attribution.get(factor, 0.0) for factor in RISK_FACTORS})
        return payload


@dataclass(frozen=True)
class DealCharacteristics:
    """
    The extracted facts the analytics adapt to.

    These are the only inputs here that come from the agreement. FX exposure
    in particular is a property of the deal, not an assumption: a US cash
    merger settled in dollars has none, and a German takeover offer in euros
    has a great deal. Reading it from the extraction rather than hard-coding
    zero is what lets the same analytics run against all three transactions.
    """

    base_currency: str = "USD"
    consideration_currency: str | None = None
    bridge_currency: str | None = None
    bridge_amount: float | None = None

    @property
    def foreign_currencies(self) -> list[str]:
        found = [
            currency
            for currency in (self.consideration_currency, self.bridge_currency)
            if currency and currency.upper() != self.base_currency
        ]
        return sorted(set(found))

    @property
    def has_fx_exposure(self) -> bool:
        return bool(self.foreign_currencies)


def deal_from_rows(rows: list[dict]) -> DealCharacteristics:
    """
    Read the currency facts out of an extraction.

    The summary-vs-agreement comparison is consulted first, because it applies
    the documented source hierarchy. It only spans those two layers, though,
    and a bridge facility is attached as its own financing agreement -- so a
    field it does not cover falls back to any asserted reading. Without that,
    `bridge_currency` extracted from a credit agreement would never reach the
    FX exposure it exists to drive.
    """
    from ..comparison import compare_layers
    from ..extraction import models as field_models

    values = {
        c.field_name: c.preferred_value
        for c in compare_layers(rows)
        if c.preferred_value is not None
    }
    for row in rows:
        name = row.get("field_name")
        if (
            name not in values
            and row.get("status") == field_models.FOUND
            and row.get("normalized_value") is not None
        ):
            values[name] = row["normalized_value"]

    return DealCharacteristics(
        consideration_currency=values.get("consideration_currency"),
        bridge_currency=values.get("bridge_currency"),
        bridge_amount=values.get("bridge_amount"),
    )


# Where an input came from. The assignment requires source facts, synthetic
# assumptions and calculations be distinguishable, and for these analytics
# that distinction is per input rather than per figure: a run can price an
# extracted notional against an assumed rate curve.
EXTRACTED = "extracted"
ASSUMED = "assumed"
DERIVED = "derived"


@dataclass(frozen=True)
class Input:
    """One analytic input, and where it came from."""

    value: object
    source: str
    basis: str

    @property
    def is_extracted(self) -> bool:
        return self.source == EXTRACTED


@dataclass(frozen=True)
class FinancingInputs:
    """
    What the analysis is actually pricing.

    The assignment supplies a standardized financing block, and requires that
    for the validation transactions the analytics be adapted to the extracted
    transaction characteristics. So these are resolved per document: where the
    filing states a figure it is used and marked `extracted`, and where it
    does not the supplied assumption is used and marked `assumed`.

    Without this the analysis priced a USD 4bn seven-year fixed-rate issuance
    for every deal, including one whose disclosed facility is EUR 14.2bn over
    364 days -- with all four of those facts sitting extracted in the database.
    """

    notional: Input
    currency: Input
    tenor_years: Input
    rate_basis: Input
    dv01: Input

    @property
    def extracted(self) -> list[str]:
        return [
            name
            for name, field in (
                ("notional", self.notional),
                ("currency", self.currency),
                ("tenor", self.tenor_years),
                ("rate basis", self.rate_basis),
            )
            if field.is_extracted
        ]

    def to_rows(self) -> list[dict]:
        """Flat shape for display and export."""
        return [
            {"input": name, "value": field.value, "source": field.source,
             "basis": field.basis}
            for name, field in (
                ("Notional", self.notional),
                ("Currency", self.currency),
                ("Tenor (years)", self.tenor_years),
                ("Rate basis", self.rate_basis),
                ("DV01", self.dv01),
            )
        ]


_TENOR_RE = re.compile(
    r"(\d[\d,.]*)\s*[-\s]*(day|week|month|year)s?", re.I
)
_TENOR_YEARS = {"day": 1 / 365.0, "week": 7 / 365.0, "month": 1 / 12.0, "year": 1.0}


def parse_tenor_years(text: str | None) -> float | None:
    """
    Read a stated maturity as a number of years.

    "364 days after the Closing Date" is just under a year, and a facility of
    that length carries roughly a seventh of the interest-rate duration of the
    seven-year issuance the assumptions describe. Reading it matters more than
    it looks.
    """
    if not text:
        return None
    match = _TENOR_RE.search(str(text))
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount * _TENOR_YEARS[match.group(2).lower()]


def resolve_financing(
    assumptions: dict, rows: list[dict] | None = None
) -> FinancingInputs:
    """
    Decide what this run prices, preferring the filing over the assumption.

    DV01 is scaled by tenor against the supplied seven-year figure. That is a
    linear duration approximation, crude but the right order of magnitude and
    far closer than ignoring tenor altogether; it is labelled `derived` so no
    reader mistakes it for a quoted sensitivity.
    """
    financing = assumptions["financing"]
    market = assumptions["market"]

    default_tenor = float(financing["expected_tenor_years"])
    notional = Input(
        float(financing["expected_debt_issuance_usd"]), ASSUMED,
        "assignment-supplied expected debt issuance",
    )
    currency = Input("USD", ASSUMED, "assignment-supplied; the standardized block is USD")
    tenor = Input(default_tenor, ASSUMED, "assignment-supplied expected tenor")
    rate_basis = Input(
        "swap rate", ASSUMED, "assignment-supplied market block"
    )

    values = _found_values(rows or [])

    if isinstance(values.get("bridge_amount"), (int, float)):
        notional = Input(
            float(values["bridge_amount"]), EXTRACTED,
            "bridge_amount, extracted from the filing",
        )
    for field in ("bridge_currency", "consideration_currency"):
        if isinstance(values.get(field), str):
            currency = Input(values[field], EXTRACTED, f"{field}, extracted from the filing")
            break
    parsed = parse_tenor_years(values.get("bridge_maturity"))
    if parsed:
        tenor = Input(
            round(parsed, 3), EXTRACTED,
            f"bridge_maturity ({values['bridge_maturity']}), extracted from the filing",
        )
    if isinstance(values.get("interest_basis"), str):
        rate_basis = Input(
            values["interest_basis"], EXTRACTED,
            "interest_basis, extracted from the filing",
        )

    dv01 = compute_dv01(
        float(notional.value), market["benchmark_dv01_per_100mm"]
    ) * (float(tenor.value) / default_tenor)

    return FinancingInputs(
        notional=notional,
        currency=currency,
        tenor_years=tenor,
        rate_basis=rate_basis,
        dv01=Input(
            dv01, DERIVED,
            f"{notional.source} notional x supplied DV01 per 100mm, scaled "
            f"{tenor.value:g}/{default_tenor:g} years (linear duration approximation)",
        ),
    )


def _found_values(rows: list[dict]) -> dict:
    """Asserted values by field name, preferring the governing reading."""
    if not rows:
        return {}
    from ..comparison import compare_layers
    from ..extraction import models as field_models

    values = {
        c.field_name: c.preferred_value
        for c in compare_layers(rows)
        if c.preferred_value is not None
    }
    for row in rows:
        name = row.get("field_name")
        if (
            name not in values
            and row.get("status") == field_models.FOUND
            and row.get("normalized_value") is not None
        ):
            values[name] = row["normalized_value"]
    return values


def compute_dv01(notional_usd: float, benchmark_dv01_per_100mm: float) -> float:
    """
    Dollar value of a basis point for a given notional.

    DV01 = (notional / 100mm) x benchmark_dv01
    """
    return (notional_usd / 100_000_000) * benchmark_dv01_per_100mm


def compute_rate_pnl(dv01: float, rate_shift_bps: float) -> float:
    """
    P&L to the pre-issuance issuer of a move in rates.

    Negative for a rise: the issuer has not yet fixed its coupon, so higher
    rates mean a more expensive bond.
    """
    return -dv01 * rate_shift_bps


def compute_carry_cost(
    notional_usd: float, swap_rate: float, treasury_rate: float, delay_days: int
) -> float:
    """
    Cost of carrying a forward-starting hedge across a closing delay.

    A hedge struck for an expected issuance date must be rolled when closing
    slips. Carry is approximated as the swap spread applied to the notional
    for the length of the delay:

        notional x (swap_rate - treasury_rate) x delay_days / 365
    """
    return notional_usd * (swap_rate - treasury_rate) * (delay_days / 365.0)


def build_scenarios(assumptions: dict, horizon=None) -> list[Scenario]:
    """
    The seven required scenarios.

    The two delay scenarios take their dates from the Workstream 4 timeline
    rather than deriving a close date here. Without a timeline they are still
    reported, with no delay assumed -- an invented delay would put a
    fabricated timing cost in front of someone sizing a hedge.
    """
    transaction = assumptions["transaction"]
    parallel = ADDITIONAL_ASSUMPTIONS["parallel_rate_move_bps"][0]

    scenarios = [
        Scenario("rates_up_25", "Rates +25bp", rate_shift_bps=25.0),
        Scenario("rates_down_25", "Rates -25bp", rate_shift_bps=-25.0),
        Scenario("rates_up_50", "Rates +50bp", rate_shift_bps=50.0),
        Scenario(
            "rates_and_credit",
            f"Rates +{parallel:.0f}bp and credit +20bp",
            rate_shift_bps=parallel,
            credit_spread_shift_bps=20.0,
            note="Parallel rate move sized by assumption; see additional assumptions.",
        ),
    ]

    delays = _delay_scenarios(horizon)
    # The assignment supplies one delayed-close probability for what is asked
    # for as two scenarios. They are alternative ways the same delay outcome
    # plays out, not two independent outcomes, so the probability is split
    # between them -- giving each the full 0.10 would put the outcome
    # probabilities at 1.10.
    delay_probability = transaction["delayed_close_probability"] / len(delays)
    for scenario_id, label, delay_days, dated_to in delays:
        scenarios.append(
            Scenario(
                scenario_id,
                label,
                delay_days=delay_days,
                delayed_to=dated_to,
                probability=delay_probability,
                note=(
                    f"Delay of {delay_days} days to {dated_to}, from the agreement's "
                    "extension clause via the Workstream 4 timeline (a calculated date)."
                    if delay_days is not None
                    else "No extension dates on the timeline; no delay assumed."
                ),
            )
        )

    scenarios.append(
        Scenario(
            "transaction_failure",
            "Transaction failure",
            transaction_fails=True,
            probability=transaction["failure_probability"],
            note="No issuance occurs. Rates are assumed unchanged at unwind.",
        )
    )
    return scenarios


def _delay_scenarios(horizon) -> list[tuple[str, str, int | None, str | None]]:
    """The delay scenarios, dated from the timeline's hedge horizon."""
    if horizon is None or not horizon.outside_date or not horizon.extension_dates:
        return [
            ("delay_first_extension", "Closing delayed to first extension date", None, None),
            ("delay_final_extension", "Closing delayed to final extension date", None, None),
        ]

    outside = date.fromisoformat(horizon.outside_date)
    dates = horizon.extension_dates
    chosen = [("delay_first_extension", "first", dates[0])]
    if len(dates) > 1:
        chosen.append(("delay_final_extension", "final", dates[-1]))

    return [
        (
            scenario_id,
            f"Closing delayed to {which} extension date",
            (date.fromisoformat(when) - outside).days,
            when,
        )
        for scenario_id, which, when in chosen
    ]


def _attribute(
    scenario: Scenario,
    strategy: str,
    assumptions: dict,
    deal: DealCharacteristics,
    inputs: FinancingInputs,
) -> tuple[dict[str, float], list[str]]:
    """
    Split one scenario's P&L across the seven risks for one strategy.

    Every factor is present in the result even when it is zero, because "this
    strategy is not exposed to that risk" and "we did not consider that risk"
    are different statements and the table has to tell them apart.
    """
    market = assumptions["market"]
    notional = float(inputs.notional.value)
    dv01 = float(inputs.dv01.value)
    credit_dv01 = dv01 * ADDITIONAL_ASSUMPTIONS["credit_spread_dv01_ratio"][0]
    swap_spread_dv01 = dv01 * ADDITIONAL_ASSUMPTIONS["swap_spread_dv01_ratio"][0]
    premium_bps = ADDITIONAL_ASSUMPTIONS["deal_contingent_premium_bps"][0]
    bid_offer_bps = ADDITIONAL_ASSUMPTIONS["unwind_bid_offer_bps"][0]

    attribution = {factor: 0.0 for factor in RISK_FACTORS}
    notes: list[str] = []
    hedged = strategy in (FORWARD_STARTING, DEAL_CONTINGENT)

    if scenario.transaction_fails:
        # No issuance, so no rate or spread exposure on debt that never exists.
        # What remains is what each strategy is left holding.
        if strategy == FORWARD_STARTING:
            attribution[UNWIND] = -dv01 * bid_offer_bps
            notes.append(
                "The swap outlives the deal and must be unwound; at unchanged "
                "rates the cost is the bid-offer."
            )
        elif strategy == DEAL_CONTINGENT:
            notes.append("The hedge terminates with the transaction, at no breakage cost.")
        else:
            notes.append("Nothing was issued and nothing was hedged.")
    else:
        # Benchmark: borne in full when unhedged, neutralised when hedged.
        if strategy == UNHEDGED:
            attribution[BENCHMARK_RATE] = compute_rate_pnl(dv01, scenario.rate_shift_bps)
        elif scenario.rate_shift_bps:
            notes.append(
                "The hedge offsets the benchmark move; the gain on the swap and "
                "the higher coupon cancel."
            )

        # Swap spread: only a hedger has this basis, because only a hedger
        # holds an instrument priced off the swap rate.
        if hedged and scenario.swap_spread_shift_bps:
            attribution[SWAP_SPREAD] = -swap_spread_dv01 * scenario.swap_spread_shift_bps

        # Issuer credit spread: no rate hedge touches it. Every strategy bears
        # it in full, which is the point of reporting it separately.
        if scenario.credit_spread_shift_bps:
            attribution[CREDIT_SPREAD] = -credit_dv01 * scenario.credit_spread_shift_bps
            if hedged:
                notes.append(
                    "Neither hedge covers the issuer's own credit spread; this "
                    "cost is identical to the unhedged case."
                )

        # Timing: a delay means the hedge has to be carried further.
        if scenario.delay_days:
            if strategy == FORWARD_STARTING:
                attribution[TIMING] = -compute_carry_cost(
                    notional, market["swap_rate"], market["treasury_rate"],
                    scenario.delay_days,
                )
            elif strategy == DEAL_CONTINGENT:
                notes.append("Extension is embedded; the premium already prices timing.")
            else:
                notes.append("Nothing is being carried; the issuance simply moves.")

    # Completion: the deal-contingent premium is paid in every scenario,
    # including the ones where the contingency is never used.
    if strategy == DEAL_CONTINGENT:
        attribution[COMPLETION] = -dv01 * premium_bps

    # FX: a property of the transaction, not an assumption.
    if deal.has_fx_exposure:
        notes.append(
            f"Unhedged FX exposure in {', '.join(deal.foreign_currencies)}: no FX "
            "rate or volatility assumption is supplied, so it is reported as an "
            "exposure rather than priced."
        )
    return attribution, notes


def run_scenarios(
    assumptions=None, horizon=None, deal=None, rows=None
) -> list[ScenarioResult]:
    """
    Every required scenario against every required strategy.

    `horizon` is Workstream 4's HedgeHorizon, which dates the delay scenarios.
    `deal` carries the extracted currency facts that decide FX exposure.
    """
    assumptions = assumptions or BIO_TECHNE_ASSUMPTIONS
    deal = deal or (deal_from_rows(rows) if rows else DealCharacteristics())
    inputs = resolve_financing(assumptions, rows)

    results: list[ScenarioResult] = []
    for scenario in build_scenarios(assumptions, horizon):
        for strategy in STRATEGIES:
            unavailable = scenario.delay_days is None and scenario.scenario_id.startswith(
                "delay_"
            )
            attribution, notes = (
                ({factor: 0.0 for factor in RISK_FACTORS}, [])
                if unavailable
                else _attribute(scenario, strategy, assumptions, deal, inputs)
            )
            if scenario.note:
                notes.insert(0, scenario.note)
            results.append(
                ScenarioResult(
                    scenario_id=scenario.scenario_id,
                    scenario=scenario.label,
                    strategy=strategy,
                    attribution=attribution,
                    probability=scenario.probability,
                    delay_days=scenario.delay_days,
                    delayed_to=scenario.delayed_to,
                    currency=str(inputs.currency.value),
                    extracted_inputs=inputs.extracted,
                    notes=notes,
                )
            )
    return results


def risk_exposures(assumptions=None, deal=None, rows=None) -> list[dict]:
    """
    Per-basis-point sensitivity of each strategy to each risk.

    The scenario tables show what happens in the seven cases the assignment
    names; none of them moves the swap spread, and none prices FX. This is
    where those exposures are still visible -- a risk a strategy carries but
    no scenario happens to shock is still a risk it carries.
    """
    assumptions = assumptions or BIO_TECHNE_ASSUMPTIONS
    deal = deal or (deal_from_rows(rows) if rows else DealCharacteristics())
    inputs = resolve_financing(assumptions, rows)
    market = assumptions["market"]
    notional = assumptions["financing"]["expected_debt_issuance_usd"]
    dv01 = compute_dv01(notional, market["benchmark_dv01_per_100mm"])

    rows = []
    for strategy in STRATEGIES:
        hedged = strategy in (FORWARD_STARTING, DEAL_CONTINGENT)
        rows.append(
            {
                "strategy": strategy,
                BENCHMARK_RATE: 0.0 if hedged else -dv01,
                SWAP_SPREAD: (
                    -dv01 * ADDITIONAL_ASSUMPTIONS["swap_spread_dv01_ratio"][0]
                    if hedged
                    else 0.0
                ),
                CREDIT_SPREAD: -dv01 * ADDITIONAL_ASSUMPTIONS["credit_spread_dv01_ratio"][0],
                FX: (
                    f"exposed ({', '.join(deal.foreign_currencies)}), not priced"
                    if deal.has_fx_exposure
                    else "none"
                ),
                TIMING: "carry on delay" if strategy == FORWARD_STARTING else "none",
                COMPLETION: (
                    "premium paid in all cases"
                    if strategy == DEAL_CONTINGENT
                    else ("unwind exposure" if strategy == FORWARD_STARTING else "none")
                ),
                UNWIND: (
                    "bid-offer on early unwind"
                    if strategy == FORWARD_STARTING
                    else "none (terminates)"
                    if strategy == DEAL_CONTINGENT
                    else "none"
                ),
            }
        )
    return rows


def probability_weighted(results: list[ScenarioResult]) -> dict[str, float]:
    """
    Expected P&L per strategy across the outcome scenarios.

    Only the scenarios carrying a probability are weighted -- the rate shocks
    are sensitivities, not outcomes with a likelihood, and averaging them in
    would mix two different questions.
    """
    weighted: dict[str, float] = {strategy: 0.0 for strategy in STRATEGIES}
    for result in results:
        if result.probability is None:
            continue
        weighted[result.strategy] += result.net_pnl * result.probability
    return weighted
