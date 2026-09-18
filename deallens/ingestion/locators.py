"""
Stable source locators.

Workstream 1 requires that every extracted value preserve a stable pointer back
to its origin in the source document. A bare page number is not sufficient:

  * The *PDF page* is the physical position in the downloaded file. It is what
    an analyst needs in order to open the file and look at the value, but it
    shifts whenever front matter changes and is meaningless to anyone holding a
    differently-assembled copy of the same agreement.

  * The *printed page* is the number printed on the page by the drafter. It is
    what the agreement's own table of contents and cross-references use, and it
    is what a lawyer means by "page 62". It is stable across re-downloads but
    is not unique within a composite filing -- an SEC 8-K restarts numbering at
    each exhibit.

Neither alone is a reliable citation, so a locator carries both, together with
the document layer that disambiguates the printed number, and a content anchor
that lets us detect when a citation has silently drifted off its evidence.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict

# Locator URI grammar:
#   deallens://<document_id>/<layer_id>/pdf:<n>[/doc:<label>][#<anchor>]
_URI_RE = re.compile(
    r"^deallens://"
    r"(?P<document_id>[^/]+)/"
    r"(?P<layer_id>[^/]+)/"
    r"pdf:(?P<pdf_page>\d+)"
    r"(?:/doc:(?P<printed_page>[^#]*))?"
    r"(?:#(?P<anchor>[0-9a-f]+))?$"
)

ANCHOR_LENGTH = 16


def normalize_text(text: str) -> str:
    """
    Canonical form used for both anchoring and page-duplicate detection.

    PDF text extraction is not stable at the whitespace level: the same page
    rendered by different tools, or the same clause reflowed across a page
    break, differs in spacing, line breaks, and quotation style while being the
    same text. Normalizing before hashing means an anchor survives those
    differences but still breaks if the words themselves change.
    """
    if not text:
        return ""
    # Fold typographic punctuation that varies between PDF producers.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(" ", " ")
    return " ".join(text.split()).strip().lower()


def compute_anchor(text: str) -> str | None:
    """
    Content hash of an evidence quote.

    Stored alongside the value so that `verify_anchor` can later confirm the
    cited page still contains the exact words the extraction claimed. This is
    the mechanism behind the Workstream 8 evidence-to-value consistency check.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:ANCHOR_LENGTH]


@dataclass(frozen=True)
class SourceLocator:
    """An addressable, verifiable pointer into a source document."""

    document_id: str
    layer_id: str
    pdf_page: int
    printed_page: str | None = None
    section: str | None = None
    anchor: str | None = None

    def to_uri(self) -> str:
        uri = f"deallens://{self.document_id}/{self.layer_id}/pdf:{self.pdf_page}"
        if self.printed_page:
            uri += f"/doc:{self.printed_page}"
        if self.anchor:
            uri += f"#{self.anchor}"
        return uri

    def to_dict(self) -> dict:
        return asdict(self)

    def citation(self) -> str:
        """
        Human-readable citation for display in the UI and exported audit record.

        Deliberately shows both numbers, because showing one invites the reader
        to assume it is the other.
        """
        parts = [self.layer_id.replace("-", " ")]
        if self.section:
            parts.append(self.section)
        if self.printed_page:
            parts.append(f"page {self.printed_page} (PDF page {self.pdf_page})")
        else:
            parts.append(f"PDF page {self.pdf_page}")
        return ", ".join(parts)

    @classmethod
    def parse(cls, uri: str) -> "SourceLocator":
        match = _URI_RE.match(uri.strip())
        if not match:
            raise ValueError(f"Malformed locator URI: {uri!r}")
        groups = match.groupdict()
        return cls(
            document_id=groups["document_id"],
            layer_id=groups["layer_id"],
            pdf_page=int(groups["pdf_page"]),
            printed_page=groups["printed_page"] or None,
            anchor=groups["anchor"],
        )


@dataclass(frozen=True)
class EvidenceCheck:
    """Result of verifying a stored citation against the source document."""

    anchor_intact: bool
    found_on_cited_page: bool

    @property
    def ok(self) -> bool:
        return self.anchor_intact and self.found_on_cited_page

    @property
    def reason(self) -> str | None:
        if self.ok:
            return None
        if not self.anchor_intact:
            return "evidence text does not match the recorded anchor"
        return "evidence quote does not appear on the cited page"


def verify_evidence(
    locator: SourceLocator,
    evidence_quote: str | None,
    page_text: str,
) -> EvidenceCheck:
    """
    Evidence-to-value consistency check (Workstream 8).

    Two independent failures are possible and are reported separately, because
    they mean different things:

      * the anchor no longer matches the stored quote, meaning the quote was
        edited or corrupted after extraction; and
      * the quote does not appear on the page it cites, meaning the model
        attributed real text to the wrong location, or invented it outright.

    A missing anchor or missing quote is treated as a failure rather than a
    pass. Fail-closed: an unverifiable citation is not a verified one.
    """
    if not evidence_quote:
        return EvidenceCheck(anchor_intact=False, found_on_cited_page=False)

    anchor_intact = (
        locator.anchor is not None and compute_anchor(evidence_quote) == locator.anchor
    )
    needle = normalize_text(evidence_quote)
    haystack = normalize_text(page_text)
    found = bool(needle) and bool(haystack) and needle in haystack
    return EvidenceCheck(anchor_intact=anchor_intact, found_on_cited_page=found)
