"""
PDF loading and page inventory.

Produces the immutable, evidence-bearing record of what was actually ingested.
Everything downstream -- integrity checks, layer classification, extraction,
citation -- reads this inventory rather than re-parsing the PDF, so that a
single parse is the one source of truth about the document's physical form.

This module deliberately performs no interpretation of *meaning*. It records
what is on each page; deciding what those pages are is `classifier.py`'s job.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO

from pypdf import PdfReader

from .locators import normalize_text

# A page carrying fewer than this many characters of extractable text is
# unlikely to be a genuine text page. Legal filings routinely run 2,000-9,000
# characters per page; a signature or divider page can legitimately be short,
# so this threshold flags pages for review rather than rejecting them.
SPARSE_TEXT_THRESHOLD = 100

# Below this, there is effectively no text layer at all on the page.
EMPTY_TEXT_THRESHOLD = 10

_ROMAN_RE = re.compile(r"^[ivxlcdm]{1,7}$", re.IGNORECASE)

# Folio grammars, most specific first. Annex and schedule pages are routinely
# numbered "A-1" or "I-12" rather than continuing the body's sequence; reading
# only bare numerals would leave every annex page uncitable by its own number.
_PAGE_LABEL_PATTERNS = (
    re.compile(r"^[\s\[\(]*([A-Z]{1,3})[\-\u2013](\d{1,4})[\s\]\)]*$"),
    re.compile(r"^[\-\u2013\u2014\s\[\(]*(\d{1,4})[\-\u2013\u2014\s\]\)]*$"),
    re.compile(r"^[\-\u2013\u2014\s\[\(]*([ivxlcdmIVXLCDM]{1,7})[\-\u2013\u2014\s\]\)]*$"),
)


@dataclass
class PageRecord:
    """One physical page of the source PDF."""

    pdf_page: int  # 1-based position in the downloaded file
    text: str
    char_count: int
    content_hash: str  # hash of normalized text, for duplicate detection
    printed_page: str | None = None  # label printed on the page, verbatim
    has_images: bool = False
    rotation: int = 0

    @property
    def is_empty(self) -> bool:
        return self.char_count < EMPTY_TEXT_THRESHOLD

    @property
    def is_sparse(self) -> bool:
        return EMPTY_TEXT_THRESHOLD <= self.char_count < SPARSE_TEXT_THRESHOLD

    @property
    def text_layer_status(self) -> str:
        """
        Per-page machine-readability verdict.

        `image_only` is the case that requires OCR: the page carries no text
        layer but does carry raster content, so there is something to read and
        we simply cannot read it.
        """
        if self.is_empty:
            return "image_only" if self.has_images else "blank"
        if self.is_sparse:
            return "sparse"
        return "machine_readable"


@dataclass
class DocumentInventory:
    """The complete physical record of one ingested PDF."""

    document_id: str
    filename: str
    checksum: str
    byte_size: int
    page_count: int
    pages: list[PageRecord]
    source_url: str | None = None
    filing_date: str | None = None  # from PDF metadata; superseded by extraction
    pdf_metadata: dict = field(default_factory=dict)
    is_encrypted: bool = False
    ingestion_timestamp: str = ""

    def page(self, pdf_page: int) -> PageRecord:
        """Look up a page by its 1-based PDF position."""
        if not 1 <= pdf_page <= self.page_count:
            raise IndexError(f"PDF page {pdf_page} out of range (1-{self.page_count})")
        return self.pages[pdf_page - 1]

    def text_for(self, pdf_page: int) -> str:
        return self.page(pdf_page).text

    @property
    def total_chars(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def requires_ocr(self) -> bool:
        """True when any page carries raster content but no readable text."""
        return any(p.text_layer_status == "image_only" for p in self.pages)

    @property
    def has_degraded_text(self) -> bool:
        """
        Pages carrying images alongside very little text.

        The signature of a partial or failed text layer: something was
        rendered on the page that the extractor did not capture. Distinct from
        a genuinely short page, which is simply short.
        """
        return any(p.text_layer_status == "sparse" and p.has_images for p in self.pages)

    @property
    def is_machine_readable(self) -> bool:
        """
        Whether this document's extracted text can be trusted as complete.

        True when no page needs OCR, no page shows signs of a degraded text
        layer, and the document carries text at all.

        Note what is deliberately *not* required: that most pages be long. An
        earlier version demanded 95% of pages exceed 100 characters, which
        conflated a short cover or signature page -- where nothing is missing
        -- with a page whose content we failed to read. Ordinary filings have
        several short pages, so that rule pushed perfectly readable documents
        onto the page-image path at roughly twice the token cost for no gain.
        """
        if self.page_count == 0 or self.total_chars == 0:
            return False
        return not self.requires_ocr and not self.has_degraded_text


def compute_checksum(pdf_bytes: bytes) -> str:
    """SHA-256 over the raw bytes, as filed."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def derive_document_id(checksum: str) -> str:
    """
    Content-addressed document identity.

    Deriving the id from the checksum rather than the filename means the same
    filing ingested twice under different names is recognised as the same
    document, which is what makes duplicate detection meaningful.
    """
    return f"doc_{checksum[:12]}"


def extract_printed_page_label(text: str) -> str | None:
    """
    Recover the page number printed on the page by the drafter.

    Legal PDFs place the folio on its own line at the foot of the page, and
    occasionally at the head. We inspect only the outermost few lines: a bare
    numeral in the middle of a page is far more likely to be a section
    cross-reference or a dollar figure than a folio.

    Returns the label verbatim (so roman numerals stay roman) or None when the
    page carries no recognisable folio -- which is itself informative, and is
    what `integrity.detect_missing_pages` keys off.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    # Candidates are read liberally here and filtered downstream by
    # `integrity.reconcile_page_labels`, which only trusts a label that
    # participates in a consistent numbering run. An earlier version tried to
    # reject contents-page references at this level by assuming such pages are
    # numbered in roman -- true of the development filing, false in general,
    # and it discarded genuine arabic folios. Sequence agreement is the
    # document-independent test, so it is the only one applied.
    candidates = lines[-3:][::-1] + lines[:2]
    for line in candidates:
        for pattern in _PAGE_LABEL_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            label = "-".join(match.groups())
            # A bare 4-digit number at a page edge is far more likely to be a
            # year or a dollar amount than a folio.
            if label.isdigit() and len(label) == 4:
                break
            return label
    return None


def parse_page_label(label: str | None) -> tuple[str, int] | None:
    """
    Split a printed label into its numbering series and ordinal.

    Returns (series, ordinal), where series distinguishes independent
    numbering sequences that must never be reconciled against one another:
    arabic body folios (""), roman front matter ("roman"), and annex series
    ("A", "I", ...). Without this, roman "i" and arabic "1" both parse to 1
    and can form a spurious run across a front-matter boundary.
    """
    if not label:
        return None
    if label.isdigit():
        return ("", int(label))
    if _ROMAN_RE.match(label):
        values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        total = 0
        previous = 0
        for char in reversed(label.lower()):
            current = values[char]
            total += current if current >= previous else -current
            previous = max(previous, current)
        return ("roman", total)
    parts = label.split("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and parts[0].isalpha():
        return (parts[0].upper(), int(parts[1]))
    return None


def page_label_to_int(label: str | None) -> int | None:
    """Ordinal component of a printed label; None when it is not ordinal."""
    parsed = parse_page_label(label)
    return parsed[1] if parsed else None


def _page_has_images(page) -> bool:
    """
    Detect raster content in a page's resource dictionary.

    Used to distinguish a genuinely blank page from a scanned one: both yield
    no text, but only the latter needs OCR.
    """
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return False
        for ref in xobjects.get_object().values():
            if ref.get_object().get("/Subtype") == "/Image":
                return True
    except Exception:
        # A malformed resource tree is itself a signal worth not crashing over;
        # integrity reporting will flag the page through its text status.
        return False
    return False


def load_pdf(
    pdf_bytes: bytes,
    filename: str,
    source_url: str | None = None,
) -> DocumentInventory:
    """
    Parse a PDF into a complete page inventory.

    Raises ValueError for a file that cannot be opened as a PDF at all, since
    there is no partial result worth returning in that case. Page-level
    problems are recorded rather than raised -- a filing with three unreadable
    pages is still worth ingesting, provided the damage is reported.
    """
    checksum = compute_checksum(pdf_bytes)
    document_id = derive_document_id(checksum)

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as exc:
        raise ValueError(f"Could not parse {filename!r} as a PDF: {exc}") from exc

    metadata = {}
    filing_date = None
    try:
        if reader.metadata:
            metadata = {str(k): str(v) for k, v in reader.metadata.items()}
            filing_date = _parse_pdf_date(metadata.get("/CreationDate"))
    except Exception:
        metadata = {}

    pages: list[PageRecord] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            # A page whose content stream will not decode is recorded as empty
            # so that integrity reporting surfaces it, rather than aborting the
            # whole ingestion over one bad page.
            text = ""
        normalized = normalize_text(text)
        pages.append(
            PageRecord(
                pdf_page=index,
                text=text,
                char_count=len(text.strip()),
                content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
                printed_page=extract_printed_page_label(text),
                has_images=_page_has_images(page),
                rotation=int(page.get("/Rotate") or 0),
            )
        )

    return DocumentInventory(
        document_id=document_id,
        filename=filename,
        checksum=checksum,
        byte_size=len(pdf_bytes),
        page_count=len(pages),
        pages=pages,
        source_url=source_url,
        filing_date=filing_date,
        pdf_metadata=metadata,
        is_encrypted=bool(reader.is_encrypted),
        ingestion_timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _parse_pdf_date(raw: str | None) -> str | None:
    """
    Convert a PDF date string (D:YYYYMMDDHHmmSS+hh'mm') to an ISO date.

    This is the document's *creation* date, which for an SEC filing PDF is a
    good proxy for the filing date but is not authoritative -- the filing date
    stated on the cover page governs, and extraction overwrites this value when
    it finds one.
    """
    if not raw:
        return None
    match = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if not match:
        return None
    year, month, day = match.groups()
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return None
