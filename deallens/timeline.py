"""
Transaction timeline and risk map (Workstream 4).

A merger agreement's schedule is mostly not a schedule. Of the nine areas this
workstream has to cover, only two -- the signing date and the initial outside
date -- are calendar dates. The rest are anchored to events that have not
happened ("30 days after written notice"), conditioned on outcomes nobody
knows yet ("if antitrust clearance remains outstanding"), dependent on a
party choosing to act, or frankly non-binding ("expected to close in the
second half of 2026").

That is why this is not one sorted table. Placing "30 days after notice of
breach" at a position on a calendar would invent a date the document does not
contain, which is the failure mode the rest of this pipeline exists to avoid.
Entries that resolve to a date are ordered; entries that do not are reported
against whatever triggers them, with the trigger named.

Two axes, kept separate
-----------------------
`kind`   is what sort of date it is, drawn from the six the assignment names:
         fixed, relative, conditional, automatically extended, party election,
         non-binding estimate.

`basis`  is where it came from: `extracted` if the document states it,
         `calculated` if we derived it. These are different questions -- a
         calculated extension date is a fixed calendar date whose basis is
         arithmetic -- and collapsing them into one label would lose the
         distinction the assignment draws between source facts and
         calculations.

Calculated dates
----------------
An outside date of 25 June 2027 extendable by "two successive periods of
ninety (90) days" implies 23 September 2027 and 22 December 2027. Those are
arithmetic on two stated facts, not guesses, and a reader pricing a hedge
needs the worst-case close date. They are emitted with `basis=calculated` and
a `derivation` string showing the arithmetic, so no reader can mistake them
for something the agreement says in those words. Where the extension language
cannot be parsed with confidence, nothing is calculated and the entry says so
-- a wrong worst-case date is worse than no worst-case date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date, timedelta

from .comparison import FieldComparison, compare_layers
from .extraction import models

# The six kinds of date the assignment requires be told apart.
FIXED = "fixed"
RELATIVE = "relative"
CONDITIONAL = "conditional"
AUTO_EXTENDED = "automatically_extended"
ELECTION = "party_election"
ESTIMATE = "non_binding_estimate"

# Where a date came from. Section 2 of the assignment requires source facts,
# calculations and analytical conclusions be distinguishable.
EXTRACTED = "extracted"
CALCULATED = "calculated"

# Timeline sections, in the order a deal runs.
SIGNING = "Signing"
APPROVALS = "Approvals and conditions"
CLOSING = "Closing"
TERMINATION = "Termination"
FINANCING = "Financing"
SECTION_ORDER = (SIGNING, APPROVALS, CLOSING, TERMINATION, FINANCING)


@dataclass(frozen=True)
class MilestoneSpec:
    """One extracted field's place on the timeline."""

    field_name: str
    event: str
    default_kind: str
    section: str


# The nine areas Workstream 4 lists, mapped onto fields the registry already
# extracts. Adding a milestone is an entry here, not a change to the builder.
MILESTONES: tuple[MilestoneSpec, ...] = (
    MilestoneSpec("agreement_date", "Agreement signed", FIXED, SIGNING),
    MilestoneSpec("shareholder_approval_threshold", "Shareholder approval", CONDITIONAL, APPROVALS),
    MilestoneSpec("tender_acceptance_threshold", "Tender / acceptance threshold", CONDITIONAL, APPROVALS),
    MilestoneSpec("offer_or_acceptance_period", "Offer / acceptance period", RELATIVE, APPROVALS),
    MilestoneSpec("antitrust_approvals", "Antitrust clearance", CONDITIONAL, APPROVALS),
    MilestoneSpec("foreign_investment_approvals", "Foreign investment approval", CONDITIONAL, APPROVALS),
    MilestoneSpec("other_regulatory_approvals", "Other regulatory approvals", CONDITIONAL, APPROVALS),
    MilestoneSpec("no_injunction_condition", "No-injunction condition", CONDITIONAL, APPROVALS),
    MilestoneSpec("expected_closing_timing", "Expected closing", ESTIMATE, CLOSING),
    MilestoneSpec("outside_date", "Initial outside date", FIXED, CLOSING),
    MilestoneSpec("extension_conditions", "Outside date extension", ELECTION, CLOSING),
    MilestoneSpec("cure_periods", "Cure period", RELATIVE, TERMINATION),
    MilestoneSpec("termination_rights", "Termination rights", CONDITIONAL, TERMINATION),
    MilestoneSpec("fee_tail", "Termination fee tail", RELATIVE, TERMINATION),
    MilestoneSpec("committed_financing", "Committed financing", CONDITIONAL, FINANCING),
    MilestoneSpec("bridge_maturity", "Bridge facility maturity", RELATIVE, FINANCING),
    MilestoneSpec("financing_conditions", "Financing conditions", CONDITIONAL, FINANCING),
)

MILESTONES_BY_FIELD = {spec.field_name: spec for spec in MILESTONES}

# Language that marks an extension as automatic rather than elective. An
# automatic extension moves the date on its own; an elective one only moves it
# if someone chooses to act, which is a materially different exposure.
_AUTOMATIC_RE = re.compile(r"\b(automatic(?:ally)?|shall\s+be\s+extended|without\s+further\s+action)\b", re.I)
_ELECTION_RE = re.compile(r"\b(may\s+(?:be\s+)?(?:elect|extend)|at\s+(?:the\s+)?(?:option|election)|either\s+party\s+may)\b", re.I)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# "ninety (90) days", "90-day", "three (3) months". Legal drafting spells the
# number then repeats it in digits; the digits are what we trust.
_DURATION_RE = re.compile(
    r"(?:\((\d+)\)|\b(\d+))[-\s]*(day|business\s+day|week|month)s?\b", re.I
)
# "two successive periods", "two 90-day extensions", "twice". The count and
# the noun it counts are often separated by the duration itself, so one
# duration phrase is allowed between them.
_COUNT_RE = re.compile(
    r"\b(?:(one|two|three|four|\d+)\s+"
    r"(?:(?:successive|additional|separate|further|consecutive)\s+)?"
    r"(?:\d+[-\s]*(?:business\s+)?(?:day|week|month)s?\s+)?"
    r"(?:periods?|extensions?|occasions?|times?)|(twice))\b",
    re.I,
)
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4}

_DAYS_PER = {"day": 1, "business day": 1, "week": 7, "month": 30}


@dataclass
class TimelineEntry:
    """One dated or datable item, with the provenance to check it."""

    event: str
    field_name: str
    section: str
    kind: str
    basis: str = EXTRACTED
    resolved_date: str | None = None
    stated_as: str | None = None
    trigger: str | None = None
    derivation: str | None = None
    layer: str | None = None
    page: object | None = None
    section_ref: str | None = None
    evidence: str | None = None
    locator_uri: str | None = None
    status: str = models.NOT_FOUND
    review_status: str = models.UNREVIEWED

    @property
    def is_anchored(self) -> bool:
        """Whether this sits at a real point on a calendar."""
        return self.resolved_date is not None

    @property
    def needs_review(self) -> bool:
        return self.review_status == models.EXCEPTION

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "field_name": self.field_name,
            "section": self.section,
            "kind": self.kind,
            "basis": self.basis,
            "resolved_date": self.resolved_date,
            "stated_as": self.stated_as,
            "trigger": self.trigger,
            "derivation": self.derivation,
            "document_layer": self.layer,
            "page": self.page,
            "document_section": self.section_ref,
            "evidence": self.evidence,
            "locator_uri": self.locator_uri,
            "status": self.status,
            "review_status": self.review_status,
        }


@dataclass
class HedgeHorizon:
    """
    The exposure window a hedge has to cover, and what can move it.

    Computed once, here, from the timeline's own dates. Workstream 5 reads
    these rather than deriving its own: two modules computing the same window
    from the same agreement is two chances to disagree about it.
    """

    signing_date: str | None = None
    outside_date: str | None = None
    final_outside_date: str | None = None
    # Each successive extension date, calculated, in order. Workstream 5 needs
    # the first and the last of these by name: they are the two delay
    # scenarios the assignment requires.
    extension_dates: list[str] = dataclass_field(default_factory=list)
    base_days: int | None = None
    extension_days: int | None = None
    total_days: int | None = None
    extension_kind: str | None = None
    extension_stated_as: str | None = None
    outstanding_conditions: int = 0
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Whether there is enough here to size a hedge at all."""
        return self.signing_date is not None and self.outside_date is not None

    def to_dict(self) -> dict:
        return {
            "signing_date": self.signing_date,
            "outside_date": self.outside_date,
            "final_outside_date": self.final_outside_date,
            "extension_dates": list(self.extension_dates),
            "base_days": self.base_days,
            "extension_days": self.extension_days,
            "total_days": self.total_days,
            "extension_kind": self.extension_kind,
            "extension_stated_as": self.extension_stated_as,
            "outstanding_conditions": self.outstanding_conditions,
            "notes": list(self.notes),
        }


@dataclass
class Timeline:
    """Everything Workstream 4 produces for one document."""

    entries: list[TimelineEntry] = dataclass_field(default_factory=list)
    horizon: HedgeHorizon = dataclass_field(default_factory=HedgeHorizon)

    @property
    def anchored(self) -> list[TimelineEntry]:
        """Entries that resolve to a date, in calendar order."""
        return sorted(
            (e for e in self.entries if e.is_anchored), key=lambda e: e.resolved_date
        )

    @property
    def unanchored(self) -> list[TimelineEntry]:
        """Entries with no calendar position, in deal order."""
        order = {name: index for index, name in enumerate(SECTION_ORDER)}
        return sorted(
            (e for e in self.entries if not e.is_anchored),
            key=lambda e: (order.get(e.section, len(order)), e.event),
        )

    @property
    def calculated(self) -> list[TimelineEntry]:
        return [e for e in self.entries if e.basis == CALCULATED]

    def to_dict(self) -> dict:
        return {
            "horizon": self.horizon.to_dict(),
            "anchored": [e.to_dict() for e in self.anchored],
            "unanchored": [e.to_dict() for e in self.unanchored],
        }


def parse_extension(text: str | None) -> tuple[int, int, str] | None:
    """
    Read "two successive periods of ninety (90) days" as (2, 90, "day").

    Returns None when the language cannot be read with confidence, which is
    the common case and the safe one: an unparsed extension produces no
    calculated date, and the entry says the extension could not be quantified.
    Guessing here would put a wrong worst-case close date in front of someone
    sizing a hedge.

    Months are treated as 30 days and business days as calendar days. Both are
    approximations, and both are stated on the entry, because a hedge horizon
    quoted to the day from "three months" would imply a precision the source
    does not have.
    """
    if not text:
        return None

    duration = _DURATION_RE.search(text)
    if not duration:
        return None
    amount = int(duration.group(1) or duration.group(2))
    unit = " ".join(duration.group(3).lower().split())
    if amount <= 0 or unit not in _DAYS_PER:
        return None

    count = 1
    match = _COUNT_RE.search(text)
    if match:
        if match.group(2):  # "twice"
            count = 2
        else:
            raw = match.group(1).lower()
            count = _WORD_NUMBERS.get(raw, int(raw) if raw.isdigit() else 1)
    return (max(count, 1), amount, unit)


def _extension_kind(text: str | None, default: str = ELECTION) -> str:
    """Automatic extensions move the date on their own; elective ones do not."""
    if not text:
        return default
    if _AUTOMATIC_RE.search(text) and not _ELECTION_RE.search(text):
        return AUTO_EXTENDED
    if _ELECTION_RE.search(text):
        return ELECTION
    return default


def _kind_for(spec: MilestoneSpec, comparison: FieldComparison) -> str:
    """
    Which of the six kinds this entry is.

    The registry says what a field usually is; the extracted value says what
    it turned out to be. A date field that normalized cleanly is fixed; one
    that did not is relative, because `normalize_date` returns `ambiguous`
    precisely for dates expressed against another event.
    """
    value = comparison.preferred_value
    if spec.default_kind == FIXED:
        if isinstance(value, str) and _ISO_DATE_RE.match(value):
            return FIXED
        # A date field with text that would not normalize is anchored to some
        # other event -- "the first anniversary of the date hereof" is a
        # perfectly good contractual date and not a calendar one.
        return RELATIVE
    if spec.field_name == "extension_conditions":
        return _extension_kind(str(value) if value else None)
    return spec.default_kind


def _entry_from(spec: MilestoneSpec, comparison: FieldComparison) -> TimelineEntry:
    # A milestone whose value every control withheld has no governing
    # reading, but it still has what the document said and the page it said
    # it on. Dropping that would leave the timeline silent about a term a
    # reviewer has to adjudicate -- so fall back to whichever layer was read,
    # agreement first, matching the hierarchy used everywhere else.
    reading = comparison.preferred_reading
    if reading is None:
        for candidate in (comparison.agreement, comparison.summary):
            if candidate.was_attempted:
                reading = candidate
                break

    value = comparison.preferred_value
    resolved = value if isinstance(value, str) and _ISO_DATE_RE.match(value) else None

    return TimelineEntry(
        event=spec.event,
        field_name=spec.field_name,
        section=spec.section,
        kind=_kind_for(spec, comparison),
        basis=EXTRACTED,
        resolved_date=resolved,
        stated_as=(reading.raw_value if reading else None) or (
            str(value) if value is not None else None
        ),
        trigger=None,
        layer=comparison.preferred_layer or (reading.layer if reading else None),
        page=comparison.preferred_page or (reading.page if reading else None),
        section_ref=reading.section if reading else None,
        evidence=reading.evidence if reading else None,
        locator_uri=comparison.preferred_locator or (
            reading.locator_uri if reading else None
        ),
        status=reading.status if reading else models.NOT_FOUND,
        review_status=reading.review_status if reading else models.UNREVIEWED,
    )


def _calculated_extensions(
    outside: TimelineEntry, extension: TimelineEntry | None
) -> tuple[list[TimelineEntry], str | None, int | None]:
    """
    Derive the dates an extension clause implies.

    Returns the entries, the final date, and the total extension in days.
    Nothing is produced unless there is a fixed outside date to extend and
    extension language that parses.
    """
    if extension is None or outside.resolved_date is None:
        return [], None, None

    parsed = parse_extension(extension.stated_as)
    if parsed is None:
        extension.trigger = (
            "Extension length could not be read from the clause; no dates calculated."
        )
        return [], None, None

    count, amount, unit = parsed
    step = amount * _DAYS_PER[unit]
    start = date.fromisoformat(outside.resolved_date)

    entries: list[TimelineEntry] = []
    current = start
    for index in range(1, count + 1):
        current = current + timedelta(days=step)
        entries.append(
            TimelineEntry(
                event=f"Outside date, extension {index} of {count}",
                field_name="extension_conditions",
                section=CLOSING,
                kind=extension.kind,
                basis=CALCULATED,
                resolved_date=current.isoformat(),
                stated_as=extension.stated_as,
                trigger=extension.trigger,
                derivation=(
                    f"{outside.resolved_date} + {index} x {amount} {unit}"
                    f"{'s' if amount != 1 else ''}"
                    + (" (months counted as 30 days)" if unit == "month" else "")
                ),
                layer=extension.layer,
                page=extension.page,
                section_ref=extension.section_ref,
                evidence=extension.evidence,
                locator_uri=extension.locator_uri,
                status=extension.status,
                review_status=extension.review_status,
            )
        )
    return entries, current.isoformat(), count * step


def build_timeline(rows: list[dict]) -> Timeline:
    """
    Assemble the timeline and risk map for one document.

    `rows` is the output of `get_extracted_fields`. Each milestone takes its
    governing value from the summary-vs-agreement comparison rather than
    picking a layer here, so the timeline inherits the documented source
    hierarchy and the page and locator that come with it.
    """
    comparisons = {c.field_name: c for c in compare_layers(rows)}
    timeline = Timeline()

    for spec in MILESTONES:
        comparison = comparisons.get(spec.field_name)
        if comparison is None or comparison.classification == "not_applicable":
            continue
        # Nothing was read for this milestone at all. Reporting an empty row
        # is noise; the field list on the extraction page already shows it.
        if comparison.preferred_reading is None and not comparison.summary.was_attempted \
                and not comparison.agreement.was_attempted:
            continue
        timeline.entries.append(_entry_from(spec, comparison))

    by_field = {e.field_name: e for e in timeline.entries}
    outside = by_field.get("outside_date")
    extension = by_field.get("extension_conditions")

    final_date = None
    extension_days = None
    if outside is not None:
        derived, final_date, extension_days = _calculated_extensions(outside, extension)
        timeline.entries.extend(derived)

    timeline.horizon = _build_horizon(
        timeline, by_field, final_date, extension_days, comparisons
    )
    return timeline


def _build_horizon(
    timeline: Timeline,
    by_field: dict[str, TimelineEntry],
    final_date: str | None,
    extension_days: int | None,
    comparisons: dict[str, FieldComparison],
) -> HedgeHorizon:
    """The exposure window, assembled from dates already on the timeline."""
    horizon = HedgeHorizon()
    signing = by_field.get("agreement_date")
    outside = by_field.get("outside_date")
    extension = by_field.get("extension_conditions")

    horizon.signing_date = signing.resolved_date if signing else None
    horizon.outside_date = outside.resolved_date if outside else None

    if horizon.signing_date and horizon.outside_date:
        horizon.base_days = (
            date.fromisoformat(horizon.outside_date)
            - date.fromisoformat(horizon.signing_date)
        ).days
    else:
        horizon.notes.append(
            "No hedge horizon: the timeline has no fixed signing and outside date pair."
        )

    horizon.extension_days = extension_days
    horizon.final_outside_date = final_date
    horizon.extension_dates = [
        e.resolved_date for e in timeline.entries
        if e.basis == CALCULATED and e.resolved_date
    ]
    if extension is not None:
        horizon.extension_kind = extension.kind
        horizon.extension_stated_as = extension.stated_as
        if extension_days is None:
            horizon.notes.append(
                "Outside date extension found but not quantified; the window "
                "below is the unextended one."
            )
    if horizon.base_days is not None:
        horizon.total_days = horizon.base_days + (extension_days or 0)

    # Conditions still outstanding: each is a way the deal does not close on
    # time, which is what a deal-contingent hedge is priced against.
    horizon.outstanding_conditions = sum(
        1
        for spec in MILESTONES
        if spec.default_kind == CONDITIONAL
        and (c := comparisons.get(spec.field_name)) is not None
        and c.preferred_value is not None
    )

    if any(e.basis == CALCULATED for e in timeline.entries):
        horizon.notes.append(
            "The extended outside date is calculated from the agreement's own "
            "extension clause, not stated in those words."
        )
    return horizon
