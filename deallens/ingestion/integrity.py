"""
Page-integrity controls.

Workstream 1 requires detection of duplicate, missing, and unreadable pages;
Workstream 8 requires that those findings become part of the audit record
rather than warnings on a console. This module produces a structured
`IntegrityReport` that the ingestion pipeline persists alongside the document.

The governing principle is that we report what we observed and how confident
we are, and never silently repair. A missing page in a merger agreement may be
the page carrying the termination fee; quietly renumbering around it would
produce a document that looks whole and is not.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict

from .loader import DocumentInventory, page_label_to_int

# A folio run must be at least this long before we trust it to reconcile
# labels. Two consecutive agreeing pages can happen by coincidence in a
# document full of numerals; four in a row is a real numbering sequence.
MIN_OFFSET_RUN = 4


@dataclass
class IntegrityIssue:
    """A single defect found in the physical document."""

    kind: str  # duplicate_page | missing_page | unreadable_page | blank_page | label_anomaly
    severity: str  # error | warning | info
    pdf_pages: list[int]
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IntegrityReport:
    """Complete integrity assessment for one ingested document."""

    document_id: str
    page_count: int
    issues: list[IntegrityIssue] = field(default_factory=list)
    duplicate_groups: list[list[int]] = field(default_factory=list)
    unreadable_pages: list[int] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)
    sparse_pages: list[int] = field(default_factory=list)
    # pdf_page -> reconciled printed label, for labels confirmed by a run
    reconciled_labels: dict[int, str] = field(default_factory=dict)
    rejected_labels: dict[int, str] = field(default_factory=dict)
    requires_ocr: bool = False
    is_machine_readable: bool = True

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def ingestion_status(self) -> str:
        """
        Whether this document is safe to extract from.

        `blocked` is reserved for defects that make extraction unsound: pages we
        cannot read at all. Duplicates and label anomalies degrade citation
        quality but do not invalidate the text we did read, so they warn.
        """
        if self.requires_ocr:
            return "blocked"
        if self.has_errors:
            return "review_required"
        if self.issues:
            return "ingested_with_warnings"
        return "ingested"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["ingestion_status"] = self.ingestion_status
        return payload


def detect_duplicate_pages(inventory: DocumentInventory) -> tuple[list[list[int]], list[IntegrityIssue]]:
    """
    Find pages whose normalized text is byte-identical.

    Repeated boilerplate is expected in filings -- a blank divider or a
    repeated exhibit cover is not a defect -- so genuinely empty pages are
    excluded and short pages are reported at lower severity. What we are
    hunting for is the real failure mode: a page duplicated by a broken
    download or assembly step, which silently shifts every page after it.
    """
    by_hash: dict[str, list[int]] = defaultdict(list)
    for page in inventory.pages:
        if page.is_empty:
            continue
        by_hash[page.content_hash].append(page.pdf_page)

    groups = sorted([pages for pages in by_hash.values() if len(pages) > 1])
    issues: list[IntegrityIssue] = []
    for pages in groups:
        substantial = inventory.page(pages[0]).char_count >= 500
        issues.append(
            IntegrityIssue(
                kind="duplicate_page",
                severity="error" if substantial else "info",
                pdf_pages=pages,
                detail=(
                    f"PDF pages {pages} have identical text "
                    f"({inventory.page(pages[0]).char_count} chars). "
                    + (
                        "Substantial duplicate content may indicate a corrupted "
                        "download or a page repeated during assembly."
                        if substantial
                        else "Short repeated page, most likely a divider or cover."
                    )
                ),
            )
        )
    return groups, issues


def assess_readability(inventory: DocumentInventory) -> tuple[list[int], list[int], list[int], list[IntegrityIssue]]:
    """
    Classify every page's text layer and flag what cannot be read.

    Returns (unreadable, blank, sparse, issues). `unreadable` means the page
    carries raster content with no text layer: OCR is required and we do not
    perform it, so the honest outcome is to block extraction rather than
    extract from a document with holes in it.
    """
    unreadable, blank, sparse = [], [], []
    for page in inventory.pages:
        status = page.text_layer_status
        if status == "image_only":
            unreadable.append(page.pdf_page)
        elif status == "blank":
            blank.append(page.pdf_page)
        elif status == "sparse":
            sparse.append(page.pdf_page)

    issues: list[IntegrityIssue] = []
    if unreadable:
        issues.append(
            IntegrityIssue(
                kind="unreadable_page",
                severity="error",
                pdf_pages=unreadable,
                detail=(
                    f"{len(unreadable)} page(s) carry images but no text layer and "
                    "require OCR, which this pipeline does not perform. Extraction "
                    "is blocked because content on these pages cannot be evidenced."
                ),
            )
        )
    if blank:
        issues.append(
            IntegrityIssue(
                kind="blank_page",
                severity="info",
                pdf_pages=blank,
                detail=f"{len(blank)} page(s) contain neither text nor images.",
            )
        )
    if sparse:
        issues.append(
            IntegrityIssue(
                kind="blank_page",
                severity="info",
                pdf_pages=sparse,
                detail=(
                    f"{len(sparse)} page(s) carry very little text; expected for "
                    "cover, divider and signature pages."
                ),
            )
        )
    return unreadable, blank, sparse, issues


def reconcile_page_labels(
    inventory: DocumentInventory,
) -> tuple[dict[int, str], dict[int, str], list[IntegrityIssue]]:
    """
    Confirm or reject each extracted folio by checking it against its neighbours.

    A folio is trustworthy when it participates in a run of consecutive pages
    sharing a constant offset between PDF position and printed number. In the
    Bio-Techne filing the agreement body runs at a constant offset of 9, so a
    page claiming a printed number wildly inconsistent with that run -- a table
    of contents entry misread as a folio, say -- is rejected.

    This is also how missing pages are found: a break in an otherwise steady
    run means either a page was dropped from the PDF, or the drafter's own
    numbering jumps. We report the observation and let a human judge which.
    """
    offsets: dict[int, int] = {}
    for page in inventory.pages:
        printed = page_label_to_int(page.printed_page)
        if printed is not None:
            offsets[page.pdf_page] = page.pdf_page - printed

    # Group pages into maximal runs sharing one offset.
    run_membership: dict[int, int] = {}  # pdf_page -> offset of the run it belongs to
    offset_counts = Counter(offsets.values())
    for pdf_page, offset in offsets.items():
        run_length = 1
        for direction in (-1, 1):
            probe = pdf_page + direction
            while offsets.get(probe) == offset:
                run_length += 1
                probe += direction
        # Accept a label either because it sits in a long local run, or because
        # its offset dominates the document overall.
        if run_length >= MIN_OFFSET_RUN or offset_counts[offset] >= MIN_OFFSET_RUN:
            run_membership[pdf_page] = offset

    reconciled: dict[int, str] = {}
    rejected: dict[int, str] = {}
    for page in inventory.pages:
        if page.printed_page is None:
            continue
        if page.pdf_page in run_membership:
            reconciled[page.pdf_page] = page.printed_page
        else:
            rejected[page.pdf_page] = page.printed_page

    issues: list[IntegrityIssue] = []
    if rejected:
        issues.append(
            IntegrityIssue(
                kind="label_anomaly",
                severity="warning",
                pdf_pages=sorted(rejected),
                detail=(
                    "Printed page labels on these pages do not fit any consistent "
                    "numbering run and were not trusted: "
                    + ", ".join(f"PDF {p} claimed '{rejected[p]}'" for p in sorted(rejected))
                    + ". Citations for these pages fall back to PDF page only."
                ),
            )
        )

    issues.extend(_detect_sequence_gaps(reconciled, run_membership))
    return reconciled, rejected, issues


def _detect_sequence_gaps(
    reconciled: dict[int, str], run_membership: dict[int, int]
) -> list[IntegrityIssue]:
    """
    Find breaks in an otherwise continuous printed-page sequence.

    Only checked within a single offset run, because numbering legitimately
    restarts between document layers -- an annex beginning again at page 1 is
    not a missing page.
    """
    by_offset: dict[int, list[int]] = defaultdict(list)
    for pdf_page, offset in run_membership.items():
        by_offset[offset].append(pdf_page)

    issues: list[IntegrityIssue] = []
    for offset, pdf_pages in sorted(by_offset.items()):
        ordered = sorted(pdf_pages)
        for previous, current in zip(ordered, ordered[1:]):
            gap = current - previous
            if gap <= 1:
                continue
            # A gap of exactly one PDF page is usually an unnumbered page --
            # a part divider or a signature page -- not a missing one.
            missing = list(range(previous + 1, current))
            severity = "info" if gap == 2 else "warning"
            issues.append(
                IntegrityIssue(
                    kind="missing_page",
                    severity=severity,
                    pdf_pages=missing,
                    detail=(
                        f"Printed numbering runs {reconciled.get(previous)} -> "
                        f"{reconciled.get(current)} across PDF pages {previous} -> "
                        f"{current}, leaving PDF page(s) {missing} unnumbered. "
                        + (
                            "Most likely an unnumbered divider or signature page."
                            if gap == 2
                            else "Verify no pages were dropped from this download."
                        )
                    ),
                )
            )
    return issues


def check_integrity(inventory: DocumentInventory) -> IntegrityReport:
    """Run all page-integrity controls and return the consolidated report."""
    duplicate_groups, duplicate_issues = detect_duplicate_pages(inventory)
    unreadable, blank, sparse, readability_issues = assess_readability(inventory)
    reconciled, rejected, label_issues = reconcile_page_labels(inventory)

    return IntegrityReport(
        document_id=inventory.document_id,
        page_count=inventory.page_count,
        issues=duplicate_issues + readability_issues + label_issues,
        duplicate_groups=duplicate_groups,
        unreadable_pages=unreadable,
        blank_pages=blank,
        sparse_pages=sparse,
        reconciled_labels=reconciled,
        rejected_labels=rejected,
        requires_ocr=inventory.requires_ocr,
        is_machine_readable=inventory.is_machine_readable,
    )
