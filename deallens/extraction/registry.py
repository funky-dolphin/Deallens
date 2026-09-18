"""
Field registry (Workstream 2).

A data-driven catalogue of every field the model extracts. Adding a field, or
making one apply to a new transaction structure, is an entry here rather than
a change to extraction logic -- which is what lets the same pipeline run
against a US merger, a tender offer and a German takeover offer without
transaction-specific branching.

Three properties carry real weight:

  `applies_to`   Which transaction structures a field is meaningful for. A
                 German takeover offer has no shareholder vote, so a missing
                 `shareholder_approval_threshold` there is `not_applicable`,
                 not `not_found`. Conflating the two would make every
                 non-merger look like a failed extraction.

  `critical`     Fields the spec forbids silently inferring or overwriting.
                 Critical fields get a higher confidence bar and are routed to
                 review on any conflict rather than auto-resolved.

  `value_type`   Drives deterministic normalization and validation. The model
                 supplies the raw text; normalization is ours, so it is
                 testable and identical across documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

# Transaction structures, matching ingestion's classifier vocabulary.
MERGER = "merger"
TENDER_OFFER = "tender_offer"
TAKEOVER_OFFER = "takeover_offer"
SCHEME = "scheme_of_arrangement"
ALL_STRUCTURES = (MERGER, TENDER_OFFER, TAKEOVER_OFFER, SCHEME)

# Categories, in the order the assignment lists them.
IDENTITY = "Transaction identity"
TIMING = "Timing"
CONDITIONS = "Conditions"
TERMINATION = "Termination and fiduciary provisions"
FINANCING = "Financing"
EQUITY = "Equity treatment"

CATEGORY_ORDER = (IDENTITY, TIMING, CONDITIONS, TERMINATION, FINANCING, EQUITY)


@dataclass(frozen=True)
class FieldSpec:
    """Definition of one extractable field."""

    name: str
    category: str
    value_type: str  # text | money | date | percent | enum | boolean
    description: str
    critical: bool = False
    applies_to: tuple[str, ...] = ALL_STRUCTURES
    enum_values: tuple[str, ...] = ()
    # Free-text guidance appended to the prompt for this field only. Used
    # sparingly, for fields where the wrong reading is a known trap.
    guidance: str | None = None

    def applicable(self, structure: str | None) -> bool:
        """
        Whether this field is meaningful for a given transaction structure.

        An unknown structure is treated as applicable-to-everything: when we
        do not know what kind of deal this is, declining to look for a field
        would hide evidence rather than protect against a wrong reading.
        """
        if structure in (None, "unknown"):
            return True
        return structure in self.applies_to


FIELDS: tuple[FieldSpec, ...] = (
    # -- Transaction identity ------------------------------------------------
    FieldSpec("target", IDENTITY, "text", "Legal name of the target or company being acquired.", critical=True),
    FieldSpec("acquirer", IDENTITY, "text", "Legal name of the parent, bidder or acquirer.", critical=True),
    FieldSpec(
        "merger_subsidiary", IDENTITY, "text",
        "Legal name of the merger subsidiary or acquisition vehicle.",
        applies_to=(MERGER, TENDER_OFFER, SCHEME),
    ),
    FieldSpec("guarantors", IDENTITY, "text", "Guarantors or other covered parties to the agreement."),
    FieldSpec("agreement_date", IDENTITY, "date", "Date the agreement was entered into, as stated in its preamble.", critical=True),
    FieldSpec(
        "transaction_type", IDENTITY, "enum",
        "Structure of the transaction as described in the document.",
        critical=True,
        enum_values=("merger", "tender_offer", "takeover_offer", "scheme_of_arrangement", "other"),
    ),
    FieldSpec(
        "consideration_type", IDENTITY, "enum",
        "Form of consideration offered to holders.",
        enum_values=("cash", "stock", "mixed", "other"),
    ),
    FieldSpec(
        "consideration_per_share", IDENTITY, "money",
        "Amount payable per share, exclusive of currency symbol.",
        critical=True,
        guidance=(
            "Report the headline per-share consideration paid to common holders. "
            "Do not report a price applicable only to a specific award type."
        ),
    ),
    FieldSpec("consideration_currency", IDENTITY, "text", "ISO currency code of the consideration, e.g. USD or EUR.", critical=True),
    FieldSpec("total_transaction_value", IDENTITY, "money", "Aggregate equity or enterprise value, if stated."),

    # -- Timing --------------------------------------------------------------
    FieldSpec("expected_closing_timing", TIMING, "text", "Expected closing timing as stated, which may be a period rather than a date."),
    FieldSpec(
        "outside_date", TIMING, "date",
        "Initial outside date, long-stop date, or end date after which either party may terminate.",
        critical=True,
        guidance=(
            "Report the INITIAL outside date only. Dates reachable through "
            "extension belong in extension_conditions, not here."
        ),
    ),
    FieldSpec("extension_conditions", TIMING, "text", "How the outside date may be extended, by whom, how many times, and on what conditions."),
    FieldSpec(
        "offer_or_acceptance_period", TIMING, "text",
        "Duration of the offer or acceptance period, and any extension mechanics.",
        applies_to=(TENDER_OFFER, TAKEOVER_OFFER),
    ),
    FieldSpec("cure_periods", TIMING, "text", "Cure periods applicable to breaches before termination rights arise."),

    # -- Conditions ----------------------------------------------------------
    FieldSpec(
        "shareholder_approval_threshold", CONDITIONS, "text",
        "Shareholder or stockholder vote required to approve the transaction.",
        critical=True, applies_to=(MERGER, SCHEME),
    ),
    FieldSpec(
        "tender_acceptance_threshold", CONDITIONS, "text",
        "Minimum tender or acceptance threshold that must be satisfied.",
        critical=True, applies_to=(TENDER_OFFER, TAKEOVER_OFFER),
    ),
    FieldSpec("antitrust_approvals", CONDITIONS, "text", "Antitrust and competition clearances required as conditions."),
    FieldSpec("foreign_investment_approvals", CONDITIONS, "text", "Foreign investment, national security or investment screening approvals required."),
    FieldSpec("other_regulatory_approvals", CONDITIONS, "text", "Any further regulatory approvals conditioning closing."),
    FieldSpec("no_injunction_condition", CONDITIONS, "text", "The no-injunction or no-legal-restraint condition."),
    FieldSpec("material_adverse_effect_condition", CONDITIONS, "text", "The material adverse effect condition and its principal carve-outs."),
    FieldSpec(
        "burdensome_condition_limitation", CONDITIONS, "text",
        "Any burdensome-condition, substantial-detriment or comparable limit on "
        "remedies the acquirer must accept to obtain regulatory clearance.",
        guidance=(
            "This is the limit on what divestitures or behavioural remedies the "
            "buyer is obliged to accept. Often phrased as 'Burdensome Condition' "
            "or a materiality qualifier on efforts covenants."
        ),
    ),
    FieldSpec(
        "financing_condition", CONDITIONS, "enum",
        "Whether closing is conditioned on the acquirer obtaining financing.",
        critical=True, enum_values=("yes", "no"),
        guidance=(
            "Most US strategic mergers expressly state there is NO financing "
            "condition. Answer 'no' only where the document says so; do not "
            "infer absence from silence."
        ),
    ),

    # -- Termination and fiduciary provisions --------------------------------
    FieldSpec("company_termination_fee", TERMINATION, "money", "Termination fee payable by the target or company.", critical=True),
    FieldSpec("parent_termination_fee", TERMINATION, "money", "Reverse termination fee payable by the parent, bidder or acquirer.", critical=True),
    FieldSpec("fee_triggers", TERMINATION, "text", "Circumstances triggering each termination fee."),
    FieldSpec("fee_tail", TERMINATION, "text", "Tail period during which a fee remains payable after termination."),
    FieldSpec("superior_proposal_provisions", TERMINATION, "text", "Definition and treatment of a superior or competing proposal."),
    FieldSpec("change_in_board_recommendation", TERMINATION, "text", "Circumstances permitting a change in the board's recommendation."),
    FieldSpec("matching_rights", TERMINATION, "text", "The acquirer's matching rights and notice periods on a competing proposal."),
    FieldSpec("termination_rights", TERMINATION, "text", "Each party's rights to terminate the agreement."),

    # -- Financing -----------------------------------------------------------
    FieldSpec("funding_sources", FINANCING, "text", "Sources of funds identified for the transaction."),
    FieldSpec("committed_financing", FINANCING, "text", "Committed financing arrangements and the commitment parties."),
    FieldSpec("bridge_amount", FINANCING, "money", "Principal amount of any bridge facility."),
    FieldSpec("bridge_currency", FINANCING, "text", "ISO currency code of the bridge facility."),
    FieldSpec("bridge_maturity", FINANCING, "text", "Maturity or tenor of the bridge facility."),
    FieldSpec("interest_basis", FINANCING, "text", "Interest rate basis and margin, e.g. SOFR or EURIBOR plus a spread."),
    FieldSpec("financing_fees", FINANCING, "text", "Arrangement, commitment, ticking or underwriting fees disclosed."),
    FieldSpec("duration_fees_and_step_ups", FINANCING, "text", "Duration fees and margin step-ups over the life of the facility."),
    FieldSpec("refinancing_requirements", FINANCING, "text", "Requirements to refinance, repay or take out existing indebtedness."),
    FieldSpec("financing_conditions", FINANCING, "text", "Conditions precedent attaching to the financing itself."),

    # -- Equity treatment ----------------------------------------------------
    FieldSpec("common_shares_treatment", EQUITY, "text", "Treatment of outstanding common shares at the effective time."),
    FieldSpec("vested_options_treatment", EQUITY, "text", "Treatment of vested stock options."),
    FieldSpec("unvested_options_treatment", EQUITY, "text", "Treatment of unvested stock options."),
    FieldSpec("rsu_treatment", EQUITY, "text", "Treatment of restricted stock units, vested and unvested."),
    FieldSpec("psu_treatment", EQUITY, "text", "Treatment of performance stock units, including how performance is deemed achieved."),
    FieldSpec("restricted_stock_treatment", EQUITY, "text", "Treatment of restricted stock awards."),
    FieldSpec("espp_treatment", EQUITY, "text", "Treatment of the employee stock purchase plan and any offering periods."),
    FieldSpec(
        "equity_grant_date_distinctions", EQUITY, "text",
        "Differences in award treatment based on grant date, vesting status or performance status.",
        guidance=(
            "Some agreements treat awards granted before and after the agreement "
            "date differently. Capture any such distinction explicitly, including "
            "the cut-off date used."
        ),
    ),
)

BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELDS}
CRITICAL_FIELDS: frozenset[str] = frozenset(spec.name for spec in FIELDS if spec.critical)


def fields_for(structure: str | None) -> tuple[FieldSpec, ...]:
    """Fields meaningful for a given transaction structure."""
    return tuple(spec for spec in FIELDS if spec.applicable(structure))


def inapplicable_fields(structure: str | None) -> tuple[FieldSpec, ...]:
    """
    Fields that do not apply to this structure.

    Recorded explicitly as `not_applicable` so an export shows the full field
    set for every deal, and a reader can tell a field we could not find from
    one that does not exist in this kind of transaction.
    """
    return tuple(spec for spec in FIELDS if not spec.applicable(structure))


def by_category(specs: tuple[FieldSpec, ...] | None = None) -> dict[str, list[FieldSpec]]:
    """Group field specs by category, preserving the assignment's ordering."""
    specs = specs if specs is not None else FIELDS
    grouped: dict[str, list[FieldSpec]] = {category: [] for category in CATEGORY_ORDER}
    for spec in specs:
        grouped.setdefault(spec.category, []).append(spec)
    return {category: items for category, items in grouped.items() if items}
