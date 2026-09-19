"""
Tests for structured extraction (Workstream 2).

The model is replaced by a fake that returns whatever payload a test
specifies. That keeps these tests free, deterministic, and focused on what we
actually own: page mapping, normalization, the fail-closed controls, and the
rule that layers are never merged.
"""

from __future__ import annotations

import pytest

from deallens.extraction import models, validate
from deallens.extraction.extractor import (
    extract_document,
    extract_layer,
    reconcile_within_layer,
)
from deallens.extraction.models import ExtractedField
from deallens.extraction.registry import BY_NAME, fields_for
from deallens.ingestion import ingest

from .pdf_factory import agreement_pages, exhibit_cover, make_pdf, sec_cover_page


class FakeClient:
    """
    Stands in for anthropic.Anthropic.

    `payloads` maps a field name to the dict the model would have returned for
    it. Fields absent from the map are reported as not found. `per_call` lets
    a test vary the response between calls, which is how chunk conflicts and
    layer differences are simulated.
    """

    def __init__(self, payloads=None, per_call=None):
        self.payloads = payloads or {}
        self.per_call = list(per_call) if per_call else None
        self.calls = 0
        self.prompts = []

        outer = self

        class _Messages:
            def stream(self, **kwargs):
                return _Stream(kwargs)

        class _Stream:
            def __init__(self, kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                outer.calls += 1
                outer.prompts.append(self.kwargs)
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                import json

                schema = self.kwargs["output_config"]["format"]["schema"]
                source = (
                    outer.per_call[min(outer.calls - 1, len(outer.per_call) - 1)]
                    if outer.per_call
                    else outer.payloads
                )
                data = {}
                for name in schema["required"]:
                    data[name] = source.get(
                        name,
                        {
                            "found": False,
                            "raw_value": "",
                            "page": 0,
                            "section": "",
                            "evidence": "",
                            "confidence": 0.0,
                        },
                    )
                return _Message(json.dumps(data))

        class _Message:
            def __init__(self, text):
                self.content = [type("Block", (), {"type": "text", "text": text})()]
                self.stop_reason = "end_turn"
                self.usage = type(
                    "Usage", (), {
                        "input_tokens": 100, "output_tokens": 50,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                    },
                )()
                self._request_id = "req_test"

        self.messages = _Messages()


EVIDENCE = "the Company Termination Fee shall be $250,000,000"


def _found(raw, page, evidence, confidence=0.95, section="Section 7.02"):
    return {
        "found": True,
        "raw_value": raw,
        "page": page,
        "section": section,
        "evidence": evidence,
        "confidence": confidence,
    }


@pytest.fixture
def composite_pdf():
    """An 8-K wrapping a merger agreement, with a known quote on a known page."""
    body = agreement_pages(body_pages=5)
    body[1] = f"ARTICLE 1 COVENANTS\n{EVIDENCE}. The parties agree.\n2"
    pages = (
        [sec_cover_page(), "Item 1.01 Entry into a Material Definitive Agreement."]
        + [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER")]
        + body
    )
    return make_pdf(pages)


@pytest.fixture
def ingested(composite_pdf):
    return ingest(composite_pdf, "synthetic.pdf", run_id="ws2-test")


# ---------------------------------------------------------------------------
# Page mapping
# ---------------------------------------------------------------------------

def test_excerpt_page_maps_back_to_source_pdf_page(ingested, composite_pdf):
    """
    The model counts pages within the excerpt it was given. A citation that
    is not translated back to the source page looks precise and points at the
    wrong page.
    """
    agreement = ingested.agreement_layer
    # The quote sits on the second page of the agreement layer's body.
    excerpt_page = agreement.body_pages().index(5) + 1
    client = FakeClient({"company_termination_fee": _found("$250,000,000", excerpt_page, EVIDENCE)})

    result = extract_layer(client, ingested, agreement, composite_pdf, fields_for("merger"))
    fee = next(f for f in result.fields if f.field_name == "company_termination_fee")

    assert fee.pdf_page == 5
    assert fee.status == models.FOUND
    assert fee.normalized_value == 250_000_000.0


def test_out_of_range_page_records_no_page_rather_than_guessing(ingested, composite_pdf):
    agreement = ingested.agreement_layer
    client = FakeClient({"company_termination_fee": _found("$250,000,000", 999, EVIDENCE)})
    result = extract_layer(client, ingested, agreement, composite_pdf, fields_for("merger"))
    fee = next(f for f in result.fields if f.field_name == "company_termination_fee")

    assert fee.pdf_page is None
    assert fee.status == models.UNRESOLVED
    assert any("outside this excerpt" in n for n in fee.notes)


# ---------------------------------------------------------------------------
# Fail-closed controls
# ---------------------------------------------------------------------------

def test_evidence_not_on_cited_page_withholds_the_value(ingested, composite_pdf):
    """
    A quote that does not appear on the page it cites is the signature of a
    fabricated or misattributed citation. No confidence score rescues it.
    """
    agreement = ingested.agreement_layer
    client = FakeClient({
        "company_termination_fee": _found(
            "$250,000,000", 1, "a quote that appears nowhere in this document", 0.99
        )
    })
    result = extract_layer(client, ingested, agreement, composite_pdf, fields_for("merger"))
    fee = next(f for f in result.fields if f.field_name == "company_termination_fee")

    assert fee.status == models.UNRESOLVED
    assert fee.normalized_value is None
    assert fee.evidence_verified is False
    assert fee.review_status == models.EXCEPTION


def test_low_confidence_withholds_the_value(ingested, composite_pdf):
    agreement = ingested.agreement_layer
    excerpt_page = agreement.body_pages().index(5) + 1
    client = FakeClient({
        "company_termination_fee": _found("$250,000,000", excerpt_page, EVIDENCE, confidence=0.4)
    })
    result = extract_layer(client, ingested, agreement, composite_pdf, fields_for("merger"))
    fee = next(f for f in result.fields if f.field_name == "company_termination_fee")

    assert fee.status == models.UNRESOLVED
    assert fee.normalized_value is None
    assert fee.raw_value == "$250,000,000", "raw value must be kept for human review"


def test_critical_fields_are_held_to_a_higher_confidence_bar():
    """A 0.7 reading passes for a narrative field and fails for a critical one."""
    assert BY_NAME["company_termination_fee"].critical
    assert not BY_NAME["matching_rights"].critical

    def build(name):
        record = ExtractedField(
            field_name=name, document_id="d", run_id="r",
            raw_value="$1,000,000" if name == "company_termination_fee" else "Four business days",
            evidence="quote", confidence=0.70, pdf_page=1,
        )
        return models.finalise(record)

    assert build("matching_rights").status == models.FOUND
    assert build("company_termination_fee").status == models.UNRESOLVED


def test_ambiguous_normalization_withholds_the_value():
    """'up to $250 million' is a cap, not the fee."""
    record = ExtractedField(
        field_name="company_termination_fee", document_id="d", run_id="r",
        raw_value="up to $250 million", evidence="quote", confidence=0.95, pdf_page=1,
    )
    finalised = models.finalise(record)
    assert finalised.status == models.UNRESOLVED
    assert finalised.normalization_status == "ambiguous"


def test_relative_date_is_not_resolved_to_a_calendar_date():
    record = ExtractedField(
        field_name="outside_date", document_id="d", run_id="r",
        raw_value="the first anniversary of the date hereof",
        evidence="quote", confidence=0.95, pdf_page=1,
    )
    finalised = models.finalise(record)
    assert finalised.status == models.UNRESOLVED
    assert finalised.normalized_value is None


def test_missing_evidence_withholds_the_value():
    record = ExtractedField(
        field_name="target", document_id="d", run_id="r",
        raw_value="Bio-Techne Corporation", evidence=None, confidence=0.99, pdf_page=1,
    )
    assert models.finalise(record).status == models.UNRESOLVED


# ---------------------------------------------------------------------------
# Structure applicability
# ---------------------------------------------------------------------------

def test_inapplicable_fields_are_not_reported_as_missing(ingested, composite_pdf):
    """
    A merger has no tender acceptance threshold. Recording that as not_found
    would misrepresent both the document and the pipeline's accuracy.
    """
    run = extract_document(FakeClient(), ingested, composite_pdf)
    threshold = next(f for f in run.fields if f.field_name == "tender_acceptance_threshold")
    assert threshold.status == models.NOT_APPLICABLE
    assert threshold.extraction_method == "deterministic"


def test_every_registry_field_is_accounted_for(ingested, composite_pdf):
    """No field may simply be absent from the output."""
    run = extract_document(FakeClient(), ingested, composite_pdf)
    reported = {f.field_name for f in run.fields}
    assert reported == set(BY_NAME)


# ---------------------------------------------------------------------------
# Layers are never merged -- the central WS3 enabler
# ---------------------------------------------------------------------------

def test_layers_are_extracted_separately_and_both_values_survive(ingested, composite_pdf):
    """
    The filing summary and the agreement are queried independently, and a
    differing value in each is preserved. The original scaffold collapsed
    these by confidence, destroying the comparison WS3 depends on.
    """
    summary_payload = {"consideration_per_share": _found("$73.00", 1, "the summary text", 0.9)}
    agreement_payload = {"consideration_per_share": _found("$71.50", 1, "the agreement text", 0.95)}
    client = FakeClient(per_call=[summary_payload, agreement_payload])

    run = extract_document(client, ingested, composite_pdf)
    per_share = [f for f in run.fields if f.field_name == "consideration_per_share"]

    assert len(per_share) == 2, "one reading per layer must survive"
    assert {f.document_layer for f in per_share} == {"filing-summary", "agreement-ex2.1"}
    assert {f.raw_value for f in per_share} == {"$73.00", "$71.50"}


def test_each_layer_is_queried_once(ingested, composite_pdf):
    client = FakeClient()
    extract_document(client, ingested, composite_pdf)
    assert client.calls == 2


# ---------------------------------------------------------------------------
# Within-layer reconciliation
# ---------------------------------------------------------------------------

def _candidate(value, page, confidence=0.9):
    record = ExtractedField(
        field_name="company_termination_fee", document_id="d", run_id="r",
        document_layer="agreement", raw_value=value, evidence="quote",
        confidence=confidence, pdf_page=page,
    )
    return models.finalise(record)


def test_agreeing_chunks_corroborate():
    merged = reconcile_within_layer(
        [_candidate("$250,000,000", 10, 0.8), _candidate("$250,000,000", 62, 0.95)],
        "company_termination_fee",
    )
    assert merged.status == models.FOUND
    assert merged.normalized_value == 250_000_000.0
    assert any("Corroborated" in n for n in merged.notes)


def test_disagreeing_chunks_produce_a_conflict_not_a_winner():
    """
    The original implementation kept the higher-confidence value and silently
    dropped the other. Both readings must survive, and no value is asserted.
    """
    merged = reconcile_within_layer(
        [_candidate("$250,000,000", 10, 0.80), _candidate("$300,000,000", 62, 0.95)],
        "company_termination_fee",
    )
    assert merged.status == models.CONFLICT
    assert merged.normalized_value is None
    assert merged.review_status == models.EXCEPTION
    notes = " ".join(merged.notes)
    assert "250000000" in notes and "300000000" in notes


# ---------------------------------------------------------------------------
# Blocked documents and schema validation
# ---------------------------------------------------------------------------

def test_extraction_refuses_to_run_on_a_blocked_document():
    pages = agreement_pages(body_pages=3) + [""]
    blocked = ingest(make_pdf(pages, with_image_on={4}), "scanned.pdf", run_id="r")
    assert not blocked.may_extract

    run = extract_document(FakeClient(), blocked, b"")
    assert run.error and "blocked" in run.error
    assert run.fields == []


def test_validation_rejects_a_found_field_with_no_evidence():
    record = ExtractedField(
        field_name="target", document_id="d", run_id="r",
        status=models.FOUND, normalized_value="Bio-Techne", evidence=None, pdf_page=1,
    )
    problems = validate(record)
    assert any("no evidence" in p for p in problems)


def test_validation_accepts_a_well_formed_record():
    record = ExtractedField(
        field_name="target", document_id="d", run_id="r",
        status=models.FOUND, normalized_value="Bio-Techne Corporation",
        raw_value="Bio-Techne Corporation", evidence="quote", pdf_page=10, confidence=0.98,
    )
    assert validate(record) == []


def test_required_output_schema_matches_the_assignment():
    record = ExtractedField(
        field_name="consideration_per_share", document_id="bio_techne", run_id="run-1",
        normalized_value=73.0, currency="USD", raw_value="$73.00",
        document_layer="filing-summary", pdf_page=12, printed_page="3",
        section="Merger Consideration", evidence="excerpt", confidence=0.99,
        status=models.FOUND,
    )
    payload = record.to_required_schema()
    assert set(payload) == {
        "field_name", "normalized_value", "currency", "raw_value", "document_id",
        "document_layer", "page", "section", "evidence", "extraction_method",
        "confidence", "review_status", "run_id",
    }
    assert payload["normalized_value"] == 73.0
    assert payload["page"] == "3", "printed page is the citable number when reconciled"


# ---------------------------------------------------------------------------
# Cost control: source selection
# ---------------------------------------------------------------------------

def test_machine_readable_documents_are_sent_as_text(ingested, composite_pdf):
    """
    A PDF block is billed for a rendered image of every page. Where ingestion
    already extracted and verified the text, those images buy nothing -- on
    the development filing they were ~2,700 tokens per page against ~1,200.
    """
    assert ingested.inventory.is_machine_readable
    client = FakeClient()
    extract_document(client, ingested, composite_pdf)

    content = client.prompts[0]["messages"][0]["content"]
    assert content[0]["type"] == "text", "machine-readable text must not be sent as page images"
    assert "[PAGE 1]" in content[0]["text"], "page markers carry the excerpt numbering"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_unreadable_documents_fall_back_to_page_images():
    """
    Where the text layer is incomplete, the rendered page is the only way to
    read the document, so the PDF block remains the fallback path.
    """
    from deallens.extraction.client import extract_structured

    pages = [exhibit_cover("2.1", "AGREEMENT AND PLAN OF MERGER")] + agreement_pages(body_pages=3)
    ing = ingest(make_pdf(pages), "x.pdf", run_id="r")
    # A page carrying an image and only a scrap of text: the signature of a
    # partial text layer. Not blocked -- there is text -- but not trustworthy
    # enough to read without the rendered page.
    ing.inventory.pages[2].char_count = 40
    ing.inventory.pages[2].has_images = True
    assert ing.inventory.has_degraded_text
    assert not ing.inventory.is_machine_readable
    assert ing.may_extract

    client = FakeClient()
    extract_document(client, ing, make_pdf(pages))
    content = client.prompts[0]["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"


def test_extract_structured_requires_a_source():
    from deallens.extraction.client import extract_structured

    with pytest.raises(ValueError, match="document_text or pdf_bytes"):
        extract_structured(FakeClient(), "sys", "user", {"required": []})


def test_page_markers_number_within_the_excerpt_not_the_source():
    """
    The model is asked for a page within the excerpt, and page_map translates
    it back. Marking pages with their source number would double-translate.
    """
    from deallens.extraction.client import build_text_content

    rendered = build_text_content([(50, "first"), (51, "second"), (52, "third")])
    assert "[PAGE 1]\nfirst" in rendered
    assert "[PAGE 3]\nthird" in rendered
    assert "[PAGE 50]" not in rendered
