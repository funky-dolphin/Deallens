"""
Ingestion pipeline (Workstream 1).

Orchestrates the controlled path from raw bytes to a classified, integrity-
checked document ready for extraction:

    load_pdf -> check_integrity -> segment_layers -> classify_structure

The pipeline returns a result rather than raising, because a document that
fails an integrity control is still something the analyst needs to see and
reason about. The one thing it will not do is hand a blocked document to the
extractor: `IngestionResult.may_extract` gates that, and it is false whenever
pages could not be read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import (
    DocumentLayer,
    LAYER_AGREEMENT,
    LAYER_FILING_SUMMARY,
    StructureClassification,
    classify_structure,
    layer_for_page,
    segment_layers,
)
from .integrity import IntegrityReport, check_integrity
from .loader import DocumentInventory, load_pdf
from .locators import SourceLocator, compute_anchor

# Version stamped onto every ingestion run. Workstream 8 requires that an
# output can be tied to the logic that produced it; bump this whenever
# classification or integrity behaviour changes in a way that would alter
# results for an unchanged input.
INGESTION_VERSION = "1.0.0"


@dataclass
class IngestionResult:
    """Everything ingestion knows about one document."""

    inventory: DocumentInventory
    integrity: IntegrityReport
    layers: list[DocumentLayer]
    structure: StructureClassification
    run_id: str
    ingestion_version: str = INGESTION_VERSION
    warnings: list[str] = field(default_factory=list)

    @property
    def document_id(self) -> str:
        return self.inventory.document_id

    @property
    def may_extract(self) -> bool:
        """
        Whether the whole document read cleanly.

        Kept for reporting: it is the honest answer to "did anything in this
        file fail to read?". It is deliberately NOT the extraction gate --
        see `unreadable_pages_in`, which asks the narrower question that
        actually governs a run.
        """
        return self.integrity.ingestion_status != "blocked"

    def unreadable_pages_in(self, layer_ids: tuple[str, ...]) -> list[int]:
        """
        Unreadable pages that fall inside the layers about to be extracted.

        The distinction matters more than it looks. A filing can carry an
        image-only page in an investor presentation while its operative
        agreement reads perfectly, and refusing the whole document over the
        first would decline a contract because a chart in a slide deck is a
        picture. What governs a run is whether the pages *that run will read*
        are readable.

        A page outside those layers is still reported as an integrity issue;
        it just does not block work it has no bearing on.
        """
        if not self.integrity.unreadable_pages:
            return []
        in_scope: set[int] = set()
        for layer in self.layers:
            if layer.layer_id in layer_ids:
                in_scope.update(layer.body_pages())
        return sorted(set(self.integrity.unreadable_pages) & in_scope)

    def locator_for(
        self,
        pdf_page: int,
        section: str | None = None,
        evidence: str | None = None,
    ) -> SourceLocator:
        """
        Build a fully-qualified source locator for a page.

        The printed page is taken from the reconciled set, not from the raw
        extraction, so a folio that failed sequence validation degrades the
        citation to PDF-page-only rather than asserting a number we do not
        trust.
        """
        layer = layer_for_page(self.layers, pdf_page)
        return SourceLocator(
            document_id=self.document_id,
            layer_id=layer.qualified_id if layer else "unknown",
            pdf_page=pdf_page,
            printed_page=self.integrity.reconciled_labels.get(pdf_page),
            section=section,
            anchor=compute_anchor(evidence) if evidence else None,
        )

    def layer(self, layer_id: str) -> DocumentLayer | None:
        for candidate in self.layers:
            if candidate.layer_id == layer_id:
                return candidate
        return None

    @property
    def summary_layer(self) -> DocumentLayer | None:
        """The registrant's own summary, compared against the agreement in WS3."""
        return self.layer(LAYER_FILING_SUMMARY)

    @property
    def agreement_layer(self) -> DocumentLayer | None:
        """The operative agreement exhibit."""
        return self.layer(LAYER_AGREEMENT)

    def document_record(self) -> dict:
        """Flat record for the `documents` table."""
        return {
            "document_id": self.document_id,
            "filename": self.inventory.filename,
            "source_url": self.inventory.source_url,
            "checksum": self.inventory.checksum,
            "byte_size": self.inventory.byte_size,
            "filing_date": self.inventory.filing_date,
            "ingestion_timestamp": self.inventory.ingestion_timestamp,
            "page_count": self.inventory.page_count,
            "is_machine_readable": self.inventory.is_machine_readable,
            "requires_ocr": self.inventory.requires_ocr,
            "ingestion_status": self.integrity.ingestion_status,
            "transaction_structure": self.structure.structure,
            "structure_confidence": self.structure.confidence,
            "structure_review_status": self.structure.review_status,
            "ingestion_version": self.ingestion_version,
            "run_id": self.run_id,
        }


def ingest(
    pdf_bytes: bytes,
    filename: str,
    run_id: str,
    source_url: str | None = None,
) -> IngestionResult:
    """
    Run the full ingestion pipeline over one PDF.

    `run_id` is supplied by the caller rather than generated here so that a
    single analytical run spanning several documents shares one identifier,
    which is what makes an audit record reproducible.
    """
    inventory = load_pdf(pdf_bytes, filename=filename, source_url=source_url)
    integrity = check_integrity(inventory)
    layers = segment_layers(inventory)
    structure = classify_structure(inventory, layers)

    warnings: list[str] = []
    if not any(l.layer_id == LAYER_AGREEMENT for l in layers):
        warnings.append(
            "No operative agreement layer was identified. Filing-summary vs "
            "agreement comparison (WS3) cannot run for this document."
        )
    if not any(l.layer_id == LAYER_FILING_SUMMARY for l in layers):
        warnings.append(
            "No filing-summary layer was identified. This document appears to "
            "be a standalone agreement rather than a composite filing."
        )
    if structure.structure == "unknown":
        warnings.append(
            f"Transaction structure could not be determined: {structure.note}"
        )

    return IngestionResult(
        inventory=inventory,
        integrity=integrity,
        layers=layers,
        structure=structure,
        run_id=run_id,
        warnings=warnings,
    )
