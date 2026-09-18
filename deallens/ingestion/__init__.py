"""Controlled document ingestion and classification (Workstream 1)."""

from .classifier import (
    DocumentLayer,
    DocumentRegion,
    StructureClassification,
    classify_structure,
    layer_for_page,
    segment_layers,
)
from .integrity import IntegrityIssue, IntegrityReport, check_integrity
from .loader import DocumentInventory, PageRecord, compute_checksum, load_pdf
from .locators import EvidenceCheck, SourceLocator, compute_anchor, verify_evidence
from .pipeline import INGESTION_VERSION, IngestionResult, ingest

__all__ = [
    "DocumentInventory",
    "DocumentLayer",
    "DocumentRegion",
    "EvidenceCheck",
    "INGESTION_VERSION",
    "IngestionResult",
    "IntegrityIssue",
    "IntegrityReport",
    "PageRecord",
    "SourceLocator",
    "StructureClassification",
    "check_integrity",
    "classify_structure",
    "compute_anchor",
    "compute_checksum",
    "ingest",
    "layer_for_page",
    "load_pdf",
    "segment_layers",
    "verify_evidence",
]
