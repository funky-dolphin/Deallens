"""Structured transaction extraction (Workstream 2)."""

from .client import MODEL_ID, slice_pdf
from .extractor import ExtractionRun, LayerExtraction, extract_document, extract_layer
from .models import ExtractedField, finalise, not_applicable_field, validate
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_output_schema, build_user_prompt
from .registry import BY_NAME, CRITICAL_FIELDS, FIELDS, FieldSpec, by_category, fields_for

__all__ = [
    "BY_NAME",
    "CRITICAL_FIELDS",
    "FIELDS",
    "MODEL_ID",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "ExtractedField",
    "ExtractionRun",
    "FieldSpec",
    "LayerExtraction",
    "build_output_schema",
    "build_user_prompt",
    "by_category",
    "extract_document",
    "extract_layer",
    "fields_for",
    "finalise",
    "not_applicable_field",
    "slice_pdf",
    "validate",
]
