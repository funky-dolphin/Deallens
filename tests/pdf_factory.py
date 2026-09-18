"""
Minimal PDF construction for tests.

Builds valid, text-extractable PDFs from plain strings with no third-party
dependency beyond what the project already uses. This exists so that
generalization can be tested against document shapes the development filing
does not exhibit -- a standalone agreement with no SEC wrapper, an
arabic-numbered table of contents, annex folios of the form "A-1", a scanned
page, a duplicated page -- without opening the out-of-sample validation
filings, which must stay sealed until Workstream 7.

The generated files are deliberately plain: one Helvetica text block per page,
no compression. Anything fancier would test the factory rather than the
pipeline.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    """Lay out one page of text, top-down, in 12pt Helvetica."""
    parts = ["BT", "/F1 10 Tf", "12 TL", "1 0 0 1 56 736 Tm"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", errors="replace")


def make_pdf(pages: list[str], with_image_on: set[int] | None = None) -> bytes:
    """
    Build a PDF whose pages carry the given text.

    `pages` is one string per page; embedded newlines become separate lines.
    `with_image_on` lists 1-based page numbers that should additionally carry a
    raster image, which is how a scanned page is simulated: no text layer, but
    image content present, so the pipeline must classify it `image_only` and
    require OCR rather than calling it blank.
    """
    with_image_on = with_image_on or set()
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # 1-based object number

    font_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # A 1x1 greyscale image, reused by every page that needs one.
    image_num = None
    if with_image_on:
        pixel = b"\xff"
        image_num = add(
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\n"
            b"stream\n" + pixel + b"\nendstream"
        )

    pages_num_placeholder = add(b"")  # reserved; filled once kids are known

    page_nums: list[int] = []
    for index, page_text in enumerate(pages, start=1):
        stream = _content_stream(page_text.split("\n")) if page_text else b""
        if index in with_image_on:
            stream += b"\nq 1 0 0 1 56 600 cm /Im1 Do Q"
        content_num = add(
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream
            + b"\nendstream"
        )
        resources = f"/Font << /F1 {font_num} 0 R >>"
        if index in with_image_on and image_num:
            resources += f" /XObject << /Im1 {image_num} 0 R >>"
        page_nums.append(
            add(
                f"<< /Type /Page /Parent {pages_num_placeholder} 0 R "
                f"/MediaBox [0 0 612 792] /Resources << {resources} >> "
                f"/Contents {content_num} 0 R >>".encode("latin-1")
            )
        )

    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objects[pages_num_placeholder - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>".encode("latin-1")
    )
    catalog_num = add(f"<< /Type /Catalog /Pages {pages_num_placeholder} 0 R >>".encode("latin-1"))

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_num} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


# ---------------------------------------------------------------------------
# Document shapes
# ---------------------------------------------------------------------------

def sec_cover_page(form: str = "8-K", registrant: str = "EXAMPLE CORPORATION") -> str:
    return (
        "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nWashington, DC 20549\n"
        f"FORM {form}\nCURRENT REPORT\n"
        "Pursuant to Section 13 or 15(d) of the Securities Exchange Act of 1934\n"
        f"{registrant}\n(Exact Name of Registrant as Specified in its Charter)"
    )


def contents_pages(count: int = 2, numbering: str = "roman", start: int = 1) -> list[str]:
    """
    A table of contents, optionally numbered in arabic rather than roman.

    The arabic variant is the case the loader's original reference-table guard
    could not handle: it suppressed arabic folios on contents pages outright.
    """
    roman = ["i", "ii", "iii", "iv", "v", "vi"]
    out = []
    for page_index in range(count):
        lines = ["TABLE OF CONTENTS"]
        for entry in range(12):
            section = page_index * 12 + entry
            lines.append(f"Section {section // 10 + 1}.{section % 10:02d}")
            lines.append("Some Heading Of The Agreement")
            lines.append(str(10 + section))
        folio = roman[page_index] if numbering == "roman" else str(start + page_index)
        lines.append(folio)
        out.append("\n".join(lines))
    return out


def agreement_pages(
    title: str = "AGREEMENT AND PLAN OF MERGER",
    body_pages: int = 8,
    first_folio: int = 1,
    folio_style: str = "arabic",
    vocabulary: str | None = None,
) -> list[str]:
    """Operative agreement body with consistent folios."""
    vocabulary = vocabulary or (
        "This Agreement is made by and among Parent, Merger Sub, a wholly-owned "
        "Subsidiary of Parent, and the Company. At the Effective Time the "
        "Surviving Corporation shall continue. The Company Shareholder Approval "
        "is required."
    )
    out = []
    for offset in range(body_pages):
        folio_number = first_folio + offset
        folio = f"A-{folio_number}" if folio_style == "annex" else str(folio_number)
        head = f"{title}\n" if offset == 0 else f"ARTICLE {offset} COVENANTS\n"
        out.append(
            head
            + vocabulary
            + f"\nSection {offset + 1}.01 provides for the foregoing.\n{folio}"
        )
    return out


def exhibit_cover(number: str, title: str, header: str = "EXAMPLE CORPORATION 8-K") -> str:
    return f"{header}\nExhibit {number}\nExecution Version\n{title}\namong the parties\nDated as of June 25, 2026"
