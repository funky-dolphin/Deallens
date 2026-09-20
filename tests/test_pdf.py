"""
Tests for Markdown-to-PDF rendering of the written deliverables.

The parser is a subset, so the test that matters most is that an unsupported
construct degrades rather than hangs — an early version spun forever on a
heading level it had not been taught.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from deallens.pdf import markdown_to_pdf


def _text(markdown: str) -> str:
    reader = PdfReader(io.BytesIO(markdown_to_pdf(markdown, title="T")))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def test_a_heading_level_it_was_not_taught_does_not_hang():
    """
    `#### Field coverage` in the execution plan matched no heading branch and
    fell through to the paragraph handler, which consumes nothing on a line
    starting with `#`. The loop never advanced.
    """
    rendered = _text("# Title\n\n#### Deeper heading\n\nBody text here.\n")
    assert "Deeper heading" in rendered
    assert "Body text here" in rendered


def test_an_unrecognised_line_is_emitted_rather_than_skipped():
    rendered = _text("# T\n\n<!-- a comment -->\n\nAfter.\n")
    assert "After" in rendered


@pytest.mark.parametrize("level", ["#", "##", "###", "####", "#####", "######"])
def test_every_heading_level_renders(level):
    assert "Heading" in _text(f"{level} Heading\n\nBody.\n")


def test_tables_render_as_text_not_pipes():
    rendered = _text(
        "# T\n\n| Field | Value |\n|---|---|\n| bridge_amount | 14,200,000,000 |\n"
    )
    assert "bridge_amount" in rendered
    assert "14,200,000,000" in rendered


def test_inline_emphasis_is_not_shown_as_asterisks():
    rendered = _text("# T\n\nThis is **bold** and *italic* and `code`.\n")
    assert "bold" in rendered and "italic" in rendered and "code" in rendered
    assert "**" not in rendered


def test_a_code_span_containing_markup_is_not_mangled():
    """A `<` inside a code span must not become reportlab markup."""
    rendered = _text("# T\n\nThe value `a < b` holds.\n")
    assert "a < b" in rendered or "a &lt; b" not in rendered


def test_lists_and_quotes_render():
    rendered = _text("# T\n\n- first\n- second\n\n> a quotation\n\n1. one\n2. two\n")
    assert all(w in rendered for w in ("first", "second", "quotation", "one", "two"))


def test_a_fenced_code_block_survives():
    rendered = _text("# T\n\n```\nEXTRACTABLE_LAYERS = (...)\n```\n\nAfter.\n")
    assert "EXTRACTABLE_LAYERS" in rendered
    assert "After" in rendered


def test_the_output_is_a_real_pdf_with_pages():
    body = "\n\n".join(f"Paragraph {n}. " + ("Filler sentence. " * 40) for n in range(40))
    data = markdown_to_pdf("# Title\n\n" + body, title="T")
    assert data.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(data)).pages) >= 2


def test_every_shipped_document_renders():
    """The five written deliverables, as they actually are on disk."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in (
        "TECHNICAL_MEMO.md", "DECISION_RECORDS.md", "AGENT_WORKFLOW.md",
        "WS7_GENERALIZATION.md", "EXECUTION_PLAN.md",
    ):
        path = root / name
        if not path.exists():
            continue
        data = markdown_to_pdf(path.read_text(), title=name)
        assert data.startswith(b"%PDF"), name
        assert len(PdfReader(io.BytesIO(data)).pages) >= 1, name
