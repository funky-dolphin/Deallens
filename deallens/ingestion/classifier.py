"""
Document layer segmentation and transaction-structure classification.

Two distinct questions, both required by Workstream 1:

  1. *What are the parts of this file?* An SEC 8-K is a composite: a filing
     summary written by the registrant, followed by the operative agreement as
     an exhibit, followed by annexes. Workstream 3 compares the summary's
     account of a term against the agreement's, so the two must be separable
     before any extraction happens. Getting this wrong does not produce a
     visibly wrong answer -- it produces a comparison of a document with
     itself, which looks like perfect agreement.

  2. *What kind of transaction is this?* The spec is explicit that not every
     source is a US merger agreement, and the wrong structure classification
     cascades: a German takeover offer has an acceptance threshold and an
     offer period where a US merger has a shareholder vote and an outside date.

Both are deterministic, pattern-driven, and evidence-producing. No model call
is made here. That is a deliberate control choice: document structure is
cheaply and reliably detectable from its own boilerplate, and a deterministic
classifier can be unit-tested, versioned, and explained to a reviewer in a way
that a model's judgement cannot. Extraction of *meaning* is where the model
earns its place; segmentation is not.

Extending to a new document family is a matter of adding entries to
LAYER_MARKERS or STRUCTURE_SIGNATURES, not changing control flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .loader import DocumentInventory

# ---------------------------------------------------------------------------
# Layer segmentation
# ---------------------------------------------------------------------------
#
# Layers are hierarchical, not flat. A composite filing contains top-level
# layers -- the registrant's own summary, then each exhibit -- and each layer
# contains regions such as a table of contents, the operative body, and
# annexes. An earlier flat model treated a table of contents as a sibling of
# the agreement it indexes, which split the agreement into a one-page layer and
# swallowed its body into the contents. Both tiers are recorded because they
# answer different questions: Workstream 3 compares *layers*, while citation
# quality and extraction targeting depend on *regions*.

LAYER_FILING_SUMMARY = "filing-summary"
LAYER_AGREEMENT = "agreement"
LAYER_CREDIT_AGREEMENT = "credit-agreement"
LAYER_PRESS_RELEASE = "press-release"
LAYER_GOVERNING_DOCS = "governing-documents"
LAYER_EXHIBIT = "exhibit"
LAYER_UNKNOWN = "unknown"

REGION_TOC = "table-of-contents"
REGION_BODY = "body"
REGION_DEFINED_TERMS = "defined-terms"
REGION_TERM_INDEX = "index-of-defined-terms"
REGION_SIGNATURE = "signature"

# An exhibit reference only begins an exhibit when it sits at the very top of
# a page. The same string appears in the 8-K's exhibit index and in
# cross-references throughout the agreement; position is what separates a
# cover page from a mention.
_EXHIBIT_COVER_RE = re.compile(r"EXHIBIT\s+([0-9]+\.[0-9]+|[A-Z](?![A-Za-z]))", re.I)

# An exhibit label is found by line position rather than character offset.
# Registrants prefix exhibit covers with running headers of wildly varying
# length -- an offset tuned to one filing's header silently misses another's,
# merging the agreement into the filing summary. What is stable is that the
# label sits within the first few lines and stands alone as a short label,
# not buried in prose. A cross-reference to "Exhibit A" inside a sentence is
# on a long line; a cover label is not.
_EXHIBIT_COVER_MAX_LINES = 5
_EXHIBIT_LABEL_MAX_WORDS = 6

_SEC_COVER_RE = re.compile(
    r"UNITED\s+STATES\s+SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION"
    r"|\bFORM\s+(?:8-K|6-K|20-F|S-4)\b.{0,80}\bCURRENT\s+REPORT\b",
    re.I | re.S,
)

# What instrument does an exhibit contain? Read from its cover page title.
_INSTRUMENT_SIGNATURES: tuple[tuple[str, re.Pattern, str], ...] = (
    (LAYER_AGREEMENT, re.compile(
        r"AGREEMENT\s+AND\s+PLAN\s+OF\s+MERGER"
        r"|BUSINESS\s+COMBINATION\s+AGREEMENT"
        r"|TRANSACTION\s+AGREEMENT"
        r"|SCHEME\s+IMPLEMENTATION\s+(?:AGREEMENT|DEED)"
        r"|ARRANGEMENT\s+AGREEMENT"
        r"|OFFER\s+DOCUMENT", re.I), "Operative agreement"),
    # A financing agreement attached to the filing is a second operative
    # contract, not supporting material: it binds the acquirer to its lenders
    # and states the bridge amount, maturity, interest basis and fees the
    # financing fields ask for. Unmatched, it fell through to a generic
    # "exhibit" and was never read.
    (LAYER_CREDIT_AGREEMENT, re.compile(
        r"BRIDGE\s+CREDIT\s+AGREEMENT"
        r"|BRIDGE\s+(?:TERM\s+)?(?:LOAN\s+)?(?:FACILITY\s+)?AGREEMENT"
        r"|CREDIT\s+AGREEMENT"
        r"|(?:SENIOR\s+)?FACILIT(?:Y|IES)\s+AGREEMENT"
        r"|COMMITMENT\s+LETTER"
        r"|INTERIM\s+FACILIT(?:Y|IES)\s+AGREEMENT", re.I), "Financing agreement"),
    (LAYER_PRESS_RELEASE, re.compile(r"PRESS\s+RELEASE|FOR\s+IMMEDIATE\s+RELEASE", re.I), "Press release"),
    (LAYER_GOVERNING_DOCS, re.compile(
        r"(?:AMENDED\s+AND\s+RESTATED\s+)?(?:ARTICLES\s+OF\s+INCORPORATION"
        r"|CERTIFICATE\s+OF\s+INCORPORATION|BY-?LAWS)", re.I), "Constitutional documents"),
)

_REGION_SIGNATURES: tuple[tuple[str, re.Pattern, str], ...] = (
    (REGION_TERM_INDEX, re.compile(r"^(?:ANNEX\s+[IVX0-9]+\s+)?INDEX\s+OF\s+DEFINED\s+TERMS", re.I), "Index of defined terms"),
    (REGION_DEFINED_TERMS, re.compile(r"^ANNEX\s+[IVX0-9]+\s+DEFINED\s+TERMS", re.I), "Annex of defined terms"),
    (REGION_TOC, re.compile(r"^TABLE\s+OF\s+CONTENTS", re.I), "Table of contents"),
    (REGION_SIGNATURE, re.compile(r"^IN\s+WITNESS\s+WHEREOF", re.I), "Signature page"),
)


@dataclass
class DocumentRegion:
    """A sub-range of a layer, such as its contents, body or annexes."""

    region_id: str
    label: str
    start_page: int
    end_page: int

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def contains(self, pdf_page: int) -> bool:
        return self.start_page <= pdf_page <= self.end_page

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["page_count"] = self.page_count
        return payload


@dataclass
class DocumentLayer:
    """A top-level sub-document: the filing summary, or one exhibit."""

    layer_id: str
    label: str
    start_page: int  # inclusive, 1-based PDF page
    end_page: int  # inclusive
    exhibit_number: str | None = None
    detection_method: str = "deterministic"
    evidence: str | None = None
    ordinal: int = 0
    regions: list[DocumentRegion] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    @property
    def qualified_id(self) -> str:
        """Layer id unique within the document, used inside source locators."""
        if self.exhibit_number:
            return f"{self.layer_id}-ex{self.exhibit_number.lower()}"
        return self.layer_id if self.ordinal == 0 else f"{self.layer_id}-{self.ordinal}"

    def contains(self, pdf_page: int) -> bool:
        return self.start_page <= pdf_page <= self.end_page

    def region_for(self, pdf_page: int) -> DocumentRegion | None:
        for region in self.regions:
            if region.contains(pdf_page):
                return region
        return None

    def body_pages(self) -> list[int]:
        """
        Pages carrying operative text.

        Contents pages and term indexes are navigational: they repeat headings
        and page references, which inflates pattern matching and produces
        citations pointing at an index entry instead of the clause itself.
        """
        skip = {REGION_TOC, REGION_TERM_INDEX}
        excluded = {
            page
            for region in self.regions
            if region.region_id in skip
            for page in range(region.start_page, region.end_page + 1)
        }
        return [p for p in range(self.start_page, self.end_page + 1) if p not in excluded]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["qualified_id"] = self.qualified_id
        payload["page_count"] = self.page_count
        return payload


def _page_head(text: str, chars: int = 400) -> str:
    return " ".join(text.split())[:chars]


def _exhibit_label_on(text: str) -> str | None:
    """
    Return the exhibit number if this page opens an exhibit, else None.

    Requires the label to appear near the top of the page on a short,
    label-like line, which distinguishes a cover page from the many pages that
    merely cross-reference an exhibit in prose.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines[:_EXHIBIT_COVER_MAX_LINES]:
        if len(line.split()) > _EXHIBIT_LABEL_MAX_WORDS:
            continue
        match = _EXHIBIT_COVER_RE.search(line)
        if match:
            return match.group(1).upper()
    return None


def segment_layers(inventory: DocumentInventory) -> list[DocumentLayer]:
    """
    Partition the document into top-level layers, then into regions.

    Every page is assigned to exactly one layer, so no extracted value can come
    from a region we failed to classify.
    """
    boundaries: list[tuple[int, str, str | None, str]] = []
    for page in inventory.pages:
        head = _page_head(page.text)
        if not head:
            continue

        sec_match = _SEC_COVER_RE.search(head)
        if sec_match and not boundaries:
            boundaries.append((page.pdf_page, LAYER_FILING_SUMMARY, None, sec_match.group(0)[:120]))
            continue

        number = _exhibit_label_on(page.text)
        if number:
            layer_id, label = _instrument_for(head)
            boundaries.append((page.pdf_page, layer_id, number, f"Exhibit {number}: {label}"))

    if not boundaries:
        layer = DocumentLayer(
            layer_id=LAYER_UNKNOWN,
            label="Unclassified document",
            start_page=1,
            end_page=inventory.page_count,
            detection_method="fallback",
        )
        layer.regions = _segment_regions(inventory, layer)
        return [layer]

    layers: list[DocumentLayer] = []
    if boundaries[0][0] > 1:
        layers.append(
            DocumentLayer(
                layer_id=LAYER_UNKNOWN,
                label="Unclassified front matter",
                start_page=1,
                end_page=boundaries[0][0] - 1,
                detection_method="fallback",
            )
        )

    for index, (start_page, layer_id, number, evidence) in enumerate(boundaries):
        end_page = (
            boundaries[index + 1][0] - 1
            if index + 1 < len(boundaries)
            else inventory.page_count
        )
        layers.append(
            DocumentLayer(
                layer_id=layer_id,
                label=_LAYER_LABELS.get(layer_id, "Exhibit"),
                start_page=start_page,
                end_page=end_page,
                exhibit_number=number,
                evidence=evidence,
            )
        )

    layers = _assign_ordinals(layers)
    for layer in layers:
        layer.regions = _segment_regions(inventory, layer)
    return layers


_LAYER_LABELS = {
    LAYER_FILING_SUMMARY: "Filing summary",
    LAYER_AGREEMENT: "Operative agreement",
    LAYER_CREDIT_AGREEMENT: "Financing agreement",
    LAYER_PRESS_RELEASE: "Press release",
    LAYER_GOVERNING_DOCS: "Constitutional documents",
    LAYER_EXHIBIT: "Exhibit",
    LAYER_UNKNOWN: "Unclassified",
}


def _instrument_for(head: str) -> tuple[str, str]:
    """Identify what instrument an exhibit cover page announces."""
    for layer_id, pattern, label in _INSTRUMENT_SIGNATURES:
        if pattern.search(head):
            return layer_id, label
    return LAYER_EXHIBIT, "Unidentified exhibit"


def _segment_regions(inventory: DocumentInventory, layer: DocumentLayer) -> list[DocumentRegion]:
    """
    Divide one layer into contents / body / annex regions.

    Anything before the first recognised region marker is body text, which is
    the safe default: treating unrecognised pages as operative content means we
    may over-scan, never that we silently skip a clause.
    """
    marks: list[tuple[int, str, str]] = []
    for pdf_page in range(layer.start_page, layer.end_page + 1):
        head = _page_head(inventory.text_for(pdf_page), 200)
        for region_id, pattern, label in _REGION_SIGNATURES:
            if pattern.search(head):
                if marks and marks[-1][1] == region_id:
                    break  # running header, not a new region
                marks.append((pdf_page, region_id, label))
                break

    if not marks:
        return [DocumentRegion(REGION_BODY, "Body", layer.start_page, layer.end_page)]

    regions: list[DocumentRegion] = []
    if marks[0][0] > layer.start_page:
        regions.append(DocumentRegion(REGION_BODY, "Body", layer.start_page, marks[0][0] - 1))

    for index, (start_page, region_id, label) in enumerate(marks):
        end_page = marks[index + 1][0] - 1 if index + 1 < len(marks) else layer.end_page
        regions.append(DocumentRegion(region_id, label, start_page, end_page))

    # A contents region is followed by the body it indexes; the body has no
    # marker of its own, so it is inferred as the remainder of the contents run.
    expanded: list[DocumentRegion] = []
    for region in regions:
        if region.region_id == REGION_TOC and region.page_count > 6:
            toc_end = _last_contents_page(inventory, region)
            expanded.append(DocumentRegion(REGION_TOC, region.label, region.start_page, toc_end))
            if toc_end < region.end_page:
                expanded.append(DocumentRegion(REGION_BODY, "Body", toc_end + 1, region.end_page))
        else:
            expanded.append(region)
    return expanded


def _last_contents_page(inventory: DocumentInventory, region: DocumentRegion) -> int:
    """
    Find where a table of contents stops and the body begins.

    Contents pages are dense in stand-alone page references; the operative text
    that follows is not. The first page without that signature ends the run.
    """
    last = region.start_page
    for pdf_page in range(region.start_page, region.end_page + 1):
        lines = [l.strip() for l in inventory.text_for(pdf_page).split("\n") if l.strip()]
        numeric_lines = sum(1 for l in lines if l.isdigit())
        if numeric_lines >= 5:
            last = pdf_page
        else:
            break
    return last


def _assign_ordinals(layers: list[DocumentLayer]) -> list[DocumentLayer]:
    """Number repeated layers of the same kind so their locators stay unique."""
    seen: dict[str, int] = {}
    for layer in layers:
        count = seen.get(layer.layer_id, 0)
        layer.ordinal = count
        seen[layer.layer_id] = count + 1
    return layers


def layer_for_page(layers: list[DocumentLayer], pdf_page: int) -> DocumentLayer | None:
    for layer in layers:
        if layer.contains(pdf_page):
            return layer
    return None


# ---------------------------------------------------------------------------
# Transaction structure classification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StructureSignature:
    """Weighted evidence for one transaction structure."""

    structure: str
    pattern: re.Pattern
    weight: int
    note: str


# Weights reflect how decisive a phrase is, not how often it occurs. The title
# of the operative instrument is near-conclusive; incidental vocabulary is not.
STRUCTURE_SIGNATURES: tuple[StructureSignature, ...] = (
    # US one-step cash/stock merger
    StructureSignature("merger", re.compile(r"AGREEMENT\s+AND\s+PLAN\s+OF\s+MERGER", re.I), 10, "Operative instrument is a merger agreement"),
    StructureSignature("merger", re.compile(r"\bMerger\s+Sub\b", re.I), 4, "Merger subsidiary present"),
    StructureSignature("merger", re.compile(r"\bEffective\s+Time\b", re.I), 2, "Merger effective-time construct"),
    StructureSignature("merger", re.compile(r"\bSurviving\s+Corporation\b", re.I), 3, "Surviving corporation construct"),
    StructureSignature("merger", re.compile(r"\bCompany\s+(?:Shareholder|Stockholder)\s+(?:Approval|Meeting)\b", re.I), 3, "Shareholder vote mechanic"),
    # US two-step tender offer
    StructureSignature("tender_offer", re.compile(r"\bOffer\s+to\s+Purchase\b", re.I), 8, "Offer to purchase"),
    StructureSignature("tender_offer", re.compile(r"\bSchedule\s+TO\b", re.I), 8, "Schedule TO filing"),
    StructureSignature("tender_offer", re.compile(r"\bMinimum\s+(?:Tender\s+)?Condition\b", re.I), 6, "Minimum tender condition"),
    StructureSignature("tender_offer", re.compile(r"Section\s+251\(h\)", re.I), 7, "DGCL 251(h) back-end merger"),
    StructureSignature("tender_offer", re.compile(r"\bvalidly\s+tendered\b", re.I), 4, "Tender mechanics"),
    # German / EU voluntary public takeover offer
    StructureSignature("takeover_offer", re.compile(r"voluntary\s+public\s+(?:takeover\s+)?offer", re.I), 10, "Voluntary public takeover offer"),
    StructureSignature("takeover_offer", re.compile(r"\bWpÜG\b|Wertpapiererwerbs", re.I), 10, "German Takeover Act (WpÜG)"),
    StructureSignature("takeover_offer", re.compile(r"\bBusiness\s+Combination\s+Agreement\b", re.I), 7, "Business combination agreement"),
    StructureSignature("takeover_offer", re.compile(r"\bacceptance\s+period\b", re.I), 6, "Acceptance period"),
    StructureSignature("takeover_offer", re.compile(r"\bBaFin\b", re.I), 6, "German regulator BaFin"),
    StructureSignature("takeover_offer", re.compile(r"\bminimum\s+acceptance\s+(?:threshold|condition)\b", re.I), 6, "Minimum acceptance threshold"),
    # UK / Commonwealth scheme of arrangement
    StructureSignature("scheme_of_arrangement", re.compile(r"\bscheme\s+of\s+arrangement\b", re.I), 10, "Scheme of arrangement"),
    StructureSignature("scheme_of_arrangement", re.compile(r"\bCourt\s+Meeting\b", re.I), 6, "Court meeting"),
    StructureSignature("scheme_of_arrangement", re.compile(r"\bTakeover\s+Code\b|\bRule\s+2\.7\b", re.I), 7, "UK Takeover Code"),
)

# Below this score we decline to classify. Fail closed: `unknown` routes the
# document to human review, which is recoverable. A confident wrong structure
# silently mis-frames every downstream timing and conditionality question.
STRUCTURE_MIN_SCORE = 10
# A winning structure must lead the runner-up by this factor, otherwise the
# document is genuinely ambiguous -- a tender offer with a back-end merger
# legitimately scores on both, and that ambiguity should be surfaced.
STRUCTURE_MIN_RATIO = 1.5


@dataclass
class StructureClassification:
    """The transaction structure, with the evidence that produced it."""

    structure: str
    confidence: float
    # Structures with material support beyond the primary one. A two-step
    # tender offer with a back-end merger genuinely exhibits both, and that
    # is information to carry forward, not a classification failure.
    secondary_structures: list[str] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    detection_method: str = "deterministic"
    review_status: str = "unreviewed"
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def classify_structure(
    inventory: DocumentInventory,
    layers: list[DocumentLayer] | None = None,
) -> StructureClassification:
    """
    Determine the transaction structure from the operative agreement's language.

    Scored over the agreement layer where one was identified, falling back to
    the whole document otherwise. Restricting the scan matters: a filing
    summary discussing the possibility of a competing tender offer should not
    drag a plain merger toward `tender_offer`.
    """
    pages = _pages_to_scan(inventory, layers)
    scores: dict[str, int] = {}
    evidence: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for pdf_page, text in pages:
        for signature in STRUCTURE_SIGNATURES:
            match = signature.pattern.search(text)
            if not match:
                continue
            key = (signature.structure, signature.note)
            if key in seen:
                continue
            seen.add(key)
            scores[signature.structure] = scores.get(signature.structure, 0) + signature.weight
            evidence.append(
                {
                    "structure": signature.structure,
                    "note": signature.note,
                    "weight": signature.weight,
                    "pdf_page": pdf_page,
                    "matched_text": " ".join(match.group(0).split())[:120],
                }
            )

    if not scores:
        return StructureClassification(
            structure="unknown",
            confidence=0.0,
            scores={},
            evidence=[],
            review_status="exception",
            note="No recognised transaction-structure language found.",
        )

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_structure, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if top_score < STRUCTURE_MIN_SCORE:
        return StructureClassification(
            structure="unknown",
            confidence=round(top_score / STRUCTURE_MIN_SCORE, 2),
            scores=scores,
            evidence=evidence,
            review_status="exception",
            note=(
                f"Highest-scoring structure '{top_structure}' scored {top_score}, "
                f"below the threshold of {STRUCTURE_MIN_SCORE}."
            ),
        )

    total = sum(scores.values())

    if runner_up_score and top_score < runner_up_score * STRUCTURE_MIN_RATIO:
        # Two recognised structures both well-evidenced is a hybrid, not an
        # absence of signal. Collapsing it to `unknown` would discard a
        # correct reading; instead the primary is reported, the secondary
        # recorded, and the document routed to review so a human confirms
        # which mechanic governs timing and conditionality.
        secondary = [
            name
            for name, score in ranked[1:]
            if score >= STRUCTURE_MIN_SCORE or score >= top_score / STRUCTURE_MIN_RATIO
        ]
        return StructureClassification(
            structure=top_structure,
            confidence=round(top_score / total, 2) if total else 0.0,
            secondary_structures=secondary,
            scores=scores,
            evidence=[e for e in evidence if e["structure"] in {top_structure, *secondary}],
            review_status="exception",
            note=(
                f"Hybrid structure: '{top_structure}' ({top_score}) does not clearly "
                f"lead '{ranked[1][0]}' ({runner_up_score}). Commonly a tender offer "
                "with a back-end merger. Primary structure reported; confirm which "
                "mechanic governs timing and conditionality before relying on it."
            ),
        )

    return StructureClassification(
        structure=top_structure,
        confidence=round(top_score / total, 2) if total else 0.0,
        scores=scores,
        evidence=[e for e in evidence if e["structure"] == top_structure],
    )


def _pages_to_scan(
    inventory: DocumentInventory, layers: list[DocumentLayer] | None
) -> list[tuple[int, str]]:
    """Prefer the operative agreement; fall back to the entire document."""
    if layers:
        agreement_layers = [l for l in layers if l.layer_id == LAYER_AGREEMENT]
        if agreement_layers:
            return [
                (page, inventory.text_for(page))
                for layer in agreement_layers
                for page in layer.body_pages()
            ]
    return [(p.pdf_page, p.text) for p in inventory.pages]
