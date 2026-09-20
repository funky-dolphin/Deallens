"""
Markdown to PDF, for the written deliverables.

The project's documents are Markdown because that is what version control
reads well, but a reviewer opening `AGENT_WORKFLOW.md` in a text editor sees
pipe-delimited tables and literal asterisks. This renders them as documents.

Deliberately a subset, not a Markdown implementation: headings, paragraphs,
bullet and numbered lists, fenced code, block quotes, horizontal rules, tables,
and inline bold / italic / code. That is everything these files use. Anything
outside the subset degrades to plain text rather than raising, because a
slightly plain PDF is a better failure than no PDF.

`reportlab` was chosen over an HTML-to-PDF converter because it is pure Python.
The alternatives need a browser engine or system libraries that are not
available on a Streamlit Cloud deployment.
"""

from __future__ import annotations

import io
import re
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# The house palette, matching the application.
NAVY = colors.HexColor("#1A3668")
RED = colors.HexColor("#C8102E")
INK = colors.HexColor("#10192B")
MUTED = colors.HexColor("#5B6577")
RULE = colors.HexColor("#D8DEE8")
PANEL = colors.HexColor("#F4F6F9")

_BODY_FONT = "Times-Roman"
_BOLD_FONT = "Times-Bold"
_MONO_FONT = "Courier"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontName=_BOLD_FONT, fontSize=18,
            textColor=INK, spaceAfter=2, alignment=TA_LEFT,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=_BOLD_FONT, fontSize=13,
            textColor=NAVY, spaceBefore=16, spaceAfter=6, leading=16,
        ),
        "h3": ParagraphStyle(
            "h3", parent=base["Heading3"], fontName=_BOLD_FONT, fontSize=11,
            textColor=INK, spaceBefore=10, spaceAfter=4, leading=14,
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontName=_BODY_FONT, fontSize=9.5,
            textColor=INK, leading=13.5, spaceAfter=7,
        ),
        "quote": ParagraphStyle(
            "quote", parent=base["BodyText"], fontName=_BODY_FONT, fontSize=9.5,
            textColor=MUTED, leading=13.5, leftIndent=14, spaceAfter=7,
            borderPadding=(0, 0, 0, 6),
        ),
        "code": ParagraphStyle(
            "code", parent=base["Code"], fontName=_MONO_FONT, fontSize=7.6,
            textColor=INK, leading=9.6, backColor=PANEL, borderPadding=6,
            spaceAfter=8,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["BodyText"], fontName=_BODY_FONT, fontSize=8,
            textColor=INK, leading=10.5, spaceAfter=0,
        ),
        "cellhead": ParagraphStyle(
            "cellhead", parent=base["BodyText"], fontName=_BOLD_FONT, fontSize=8,
            textColor=colors.white, leading=10.5, spaceAfter=0,
        ),
    }


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """
    Markdown emphasis to reportlab's inline markup.

    Code spans are lifted out before escaping and restored afterwards, so a
    `<` inside one is not mangled and an asterisk inside one is not read as
    emphasis.
    """
    spans: list[str] = []

    def _stash(match: re.Match) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_RE.sub(_stash, text)
    text = _LINK_RE.sub(r"\1", text)
    text = escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)

    for index, span in enumerate(spans):
        text = text.replace(
            f"\x00{index}\x00",
            f'<font face="{_MONO_FONT}" size="8.5">{escape(span)}</font>',
        )
    return text


def _table(rows: list[list[str]], styles) -> Table:
    """A Markdown table as a ruled table, sized to the page."""
    header, *body = rows
    data = [[Paragraph(_inline(c), styles["cellhead"]) for c in header]]
    data += [[Paragraph(_inline(c), styles["cell"]) for c in row] for row in body]

    available = LETTER[0] - 2 * inch
    table = Table(data, colWidths=[available / len(header)] * len(header), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_divider(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:|-]+\|[\s:|-]*", line.strip()))


def markdown_to_pdf(text: str, title: str | None = None) -> bytes:
    """Render a Markdown document to PDF bytes."""
    styles = _styles()
    story: list = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # Fenced code.
        if stripped.startswith("```"):
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            body = escape("\n".join(block)).replace(" ", "&nbsp;").replace("\n", "<br/>")
            story.append(Paragraph(body, styles["code"]))
            continue

        # Tables: a header row followed by a divider.
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and _is_divider(lines[index + 1])
        ):
            rows = [_split_row(stripped)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_row(lines[index]))
                index += 1
            story.append(Spacer(1, 4))
            story.append(_table(rows, styles))
            story.append(Spacer(1, 10))
            continue

        # Any heading level. Matching them together rather than one branch per
        # level is what stops an unanticipated depth -- "#### " -- falling
        # through to the paragraph handler, which consumes nothing.
        heading = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading:
            depth, content = len(heading.group(1)), heading.group(2)
            if depth == 1:
                story.append(Paragraph(_inline(content), styles["title"]))
                story.append(
                    HRFlowable(width="100%", thickness=2, color=RED, spaceAfter=12)
                )
            else:
                story.append(
                    Paragraph(_inline(content), styles["h2" if depth == 2 else "h3"])
                )
            index += 1
            continue

        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.5, color=RULE))
            story.append(Spacer(1, 6))
            index += 1
            continue

        if stripped.startswith("> "):
            quoted = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            story.append(Paragraph(_inline(" ".join(quoted)), styles["quote"]))
            continue

        # Lists, bullet or numbered; continuation lines are folded in.
        bullet = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)", line)
        if bullet:
            items, ordered = [], bool(re.match(r"\d+\.", bullet.group(1)))
            while index < len(lines):
                match = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)", lines[index])
                if not match:
                    if lines[index].startswith(("  ", "\t")) and lines[index].strip() and items:
                        items[-1] += " " + lines[index].strip()
                        index += 1
                        continue
                    break
                items.append(match.group(2))
                index += 1
            story.append(
                ListFlowable(
                    [
                        ListItem(Paragraph(_inline(item), styles["body"]), leftIndent=16)
                        for item in items
                    ],
                    bulletType="1" if ordered else "bullet",
                    bulletFontName=_BODY_FONT,
                    bulletFontSize=9,
                    leftIndent=14,
                )
            )
            story.append(Spacer(1, 5))
            continue

        # Paragraph: consume until a blank line or a block starts.
        paragraph = []
        while index < len(lines):
            current = lines[index].strip()
            if not current or current.startswith(("#", "|", "```", ">")):
                break
            if re.match(r"^\s*([-*+]|\d+\.)\s+", lines[index]):
                break
            paragraph.append(current)
            index += 1
        if paragraph:
            story.append(Paragraph(_inline(" ".join(paragraph)), styles["body"]))
        else:
            # Nothing recognised this line. Emit it plainly and move on: a
            # slightly plain PDF is a better failure than a parser that spins
            # on a construct it was not taught.
            story.append(Paragraph(_inline(stripped), styles["body"]))
            index += 1

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=inch, rightMargin=inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=title or "DealLens",
        author="DealLens",
    )

    def _furniture(canvas, doc):
        canvas.saveState()
        canvas.setFont(_BODY_FONT, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(inch, 0.55 * inch, title or "DealLens")
        canvas.drawRightString(LETTER[0] - inch, 0.55 * inch, f"{doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.line(inch, 0.72 * inch, LETTER[0] - inch, 0.72 * inch)
        canvas.restoreState()

    document.build(story, onFirstPage=_furniture, onLaterPages=_furniture)
    return buffer.getvalue()
