"""
Deterministic value normalization.

Normalization is done here, in code, rather than asked of the model. Three
reasons: it is identical across every document and every run; it is unit
testable against the awkward cases legal drafting actually contains; and when
it cannot produce a confident answer it says so, which is what the fail-closed
requirement demands.

Every function returns a `Normalized` carrying a status. `ambiguous` is a
first-class outcome, not an error: "the first anniversary of the date hereof"
is a perfectly valid contractual date that is not a calendar date, and
silently resolving it to one would be a fabrication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Outcome statuses.
OK = "ok"
AMBIGUOUS = "ambiguous"
UNPARSEABLE = "unparseable"
EMPTY = "empty"


@dataclass(frozen=True)
class Normalized:
    """Result of normalizing one raw value."""

    value: object | None
    status: str
    reason: str | None = None
    # Set when the source qualifies the figure, e.g. "approximately $5.2 billion".
    qualifier: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK


_MULTIPLIERS = {
    "thousand": 1_000,
    "k": 1_000,
    "million": 1_000_000,
    "mm": 1_000_000,
    "m": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "chf": "CHF",
    "c$": "CAD",
    "cad": "CAD",
    "a$": "AUD",
    "aud": "AUD",
    "sek": "SEK",
    "dkk": "DKK",
    "nok": "NOK",
}

# Language that makes a figure a bound or estimate rather than the figure.
_APPROXIMATION_RE = re.compile(
    r"\b(approximately|approx\.?|about|around|up\s+to|not\s+(?:to\s+)?exceed(?:ing)?"
    r"|no\s+more\s+than|at\s+least|in\s+excess\s+of|estimated)\b",
    re.I,
)

# A relative or conditional date: valid contractually, not a calendar date.
_RELATIVE_DATE_RE = re.compile(
    r"\b(anniversary|business\s+days?\s+(?:after|following|from)|days?\s+(?:after|following|from)"
    r"|months?\s+(?:after|following|from)|promptly|as\s+soon\s+as|upon\s+(?:the\s+)?"
    r"|following\s+the|after\s+the\s+date\s+hereof|date\s+hereof)\b",
    re.I,
)

_WORD_FRACTIONS = {
    "a majority": 0.5,
    "majority": 0.5,
    "two-thirds": 2 / 3,
    "two thirds": 2 / 3,
    "three-quarters": 0.75,
    "three quarters": 0.75,
    "nine-tenths": 0.9,
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %b %Y",
    "%m/%d/%Y",
)


def normalize_money(raw: str | None) -> Normalized:
    """
    Parse a monetary amount into a float, ignoring currency (handled separately).

    Handles symbol prefixes, thousands separators, and scale words. Returns
    `ambiguous` where the document states a bound or estimate rather than a
    figure -- "up to $250 million" is a cap, and recording it as the amount
    would overstate what the document says.
    """
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")

    text = " ".join(str(raw).split())
    qualifier_match = _APPROXIMATION_RE.search(text)
    qualifier = qualifier_match.group(0).lower() if qualifier_match else None

    match = re.search(
        r"(-?\d[\d,]*\.?\d*)\s*(thousand|million|billion|trillion|mm|bn|[kmb])?\b",
        text,
        re.I,
    )
    if not match:
        return Normalized(None, UNPARSEABLE, f"no numeric amount found in {text!r}")

    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return Normalized(None, UNPARSEABLE, f"could not parse number from {text!r}")

    scale = match.group(2)
    if scale:
        amount *= _MULTIPLIERS[scale.lower()]

    # A bound is not the value. Report the figure but refuse to call it settled.
    if qualifier and re.search(r"\b(up\s+to|not\s+(?:to\s+)?exceed|no\s+more\s+than|at\s+least|in\s+excess\s+of)\b", qualifier, re.I):
        return Normalized(
            amount, AMBIGUOUS,
            f"value is stated as a bound ({qualifier!r}), not a definite amount",
            qualifier=qualifier,
        )

    return Normalized(amount, OK, qualifier=qualifier)


def normalize_currency(raw: str | None) -> Normalized:
    """Resolve a currency symbol or code to an ISO 4217 code."""
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")

    text = str(raw).strip().lower()
    if re.fullmatch(r"[a-z]{3}", text) and text.upper() in set(_CURRENCY_SYMBOLS.values()):
        return Normalized(text.upper(), OK)
    for token, code in _CURRENCY_SYMBOLS.items():
        if token in text:
            return Normalized(code, OK)
    return Normalized(None, UNPARSEABLE, f"unrecognised currency {raw!r}")


def normalize_date(raw: str | None) -> Normalized:
    """
    Parse a calendar date into ISO form.

    Relative and conditional dates return `ambiguous` with the original text
    preserved. This distinction is not cosmetic: Workstream 4 requires fixed
    calendar dates to be told apart from relative, conditional and
    election-dependent ones, and the separation starts here.
    """
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")

    text = " ".join(str(raw).split())

    if _RELATIVE_DATE_RE.search(text):
        return Normalized(
            None, AMBIGUOUS,
            f"date is expressed relative to another event, not as a calendar date: {text!r}",
        )

    cleaned = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    cleaned = cleaned.replace(",", ", ").strip()
    cleaned = " ".join(cleaned.split())

    for fmt in _DATE_FORMATS:
        for candidate in (cleaned, cleaned.replace(", ", " ")):
            try:
                return Normalized(datetime.strptime(candidate, fmt).date().isoformat(), OK)
            except ValueError:
                continue

    # Fall back to locating a date inside a longer phrase.
    embedded = re.search(r"([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", text)
    if embedded:
        nested = normalize_date(embedded.group(1))
        if nested.ok:
            return Normalized(nested.value, OK, f"date extracted from surrounding text {text!r}")

    return Normalized(None, UNPARSEABLE, f"could not parse a date from {text!r}")


def normalize_percent(raw: str | None) -> Normalized:
    """
    Parse a threshold into a fraction between 0 and 1.

    Approval thresholds are frequently worded rather than numeric -- "a
    majority of the outstanding shares" -- so common fractions are recognised.
    Anything else is ambiguous rather than guessed at, because the difference
    between a simple majority and a supermajority changes the deal.
    """
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")

    text = " ".join(str(raw).split()).lower()

    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s+cent)", text)
    if match:
        return Normalized(float(match.group(1)) / 100.0, OK)

    for phrase, value in _WORD_FRACTIONS.items():
        if phrase in text:
            return Normalized(
                value, OK,
                f"threshold stated in words as {phrase!r}",
            )

    return Normalized(None, AMBIGUOUS, f"no numeric threshold found in {text!r}")


def normalize_enum(raw: str | None, allowed: tuple[str, ...]) -> Normalized:
    """Match a value against a closed vocabulary, case- and separator-insensitive."""
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")

    text = re.sub(r"[\s_-]+", "_", str(raw).strip().lower())
    for option in allowed:
        if text == re.sub(r"[\s_-]+", "_", option.lower()):
            return Normalized(option, OK)
    return Normalized(None, AMBIGUOUS, f"{raw!r} is not one of {list(allowed)}")


def normalize_boolean(raw: str | None) -> Normalized:
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")
    text = str(raw).strip().lower()
    if text in {"yes", "true", "y"}:
        return Normalized(True, OK)
    if text in {"no", "false", "n", "none"}:
        return Normalized(False, OK)
    return Normalized(None, AMBIGUOUS, f"{raw!r} is not a yes/no value")


def normalize_text(raw: str | None) -> Normalized:
    """
    Collapse whitespace while preserving the source's own terminology.

    The assignment requires original terminology be preserved, so this
    deliberately does not paraphrase, truncate or re-case.
    """
    if raw is None or not str(raw).strip():
        return Normalized(None, EMPTY, "no value supplied")
    return Normalized(" ".join(str(raw).split()), OK)


_DISPATCH = {
    "money": lambda raw, spec: normalize_money(raw),
    "date": lambda raw, spec: normalize_date(raw),
    "percent": lambda raw, spec: normalize_percent(raw),
    "boolean": lambda raw, spec: normalize_boolean(raw),
    "enum": lambda raw, spec: normalize_enum(raw, spec.enum_values),
    "text": lambda raw, spec: normalize_text(raw),
}


def normalize_for(spec, raw: str | None) -> Normalized:
    """Normalize a raw value according to its field's declared type."""
    handler = _DISPATCH.get(spec.value_type)
    if handler is None:
        return Normalized(None, UNPARSEABLE, f"no normalizer for type {spec.value_type!r}")
    # Currency fields are text by declaration but need ISO resolution.
    if spec.name.endswith("_currency"):
        return normalize_currency(raw)
    return handler(raw, spec)
