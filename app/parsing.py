"""Receipt ingestion for PDF, JSON and plain-text documents.

The parser converts an uploaded document into normalised text that the
extraction agent (or the deterministic demo extractor) can read. It deliberately
does not extract invoice fields: that is the agent's job and is traced
separately, so this module stays a pure, testable I/O boundary.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pdfplumber
from pypdf import PdfReader

from app.models import AuditFlowError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = PROJECT_ROOT / "data" / "sample_receipts"

#: Hard limit for a single upload; keeps a bad file from exhausting memory.
MAX_RECEIPT_BYTES = 5 * 1024 * 1024

#: Extensions the dashboard offers in its file picker.
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".pdf", ".txt", ".md", ".json")

ReceiptFormat = Literal["pdf", "json", "text"]

_BLANK_RUNS = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)


class ReceiptParseError(AuditFlowError):
    """Raised when an uploaded document cannot be read as a receipt."""


@dataclass(frozen=True, slots=True)
class ParsedReceipt:
    """Normalised receipt content ready for extraction.

    Attributes:
        text: Text passed to the extraction agent.
        format: Detected input format.
        source: Filename (or ``"pasted-text"``) shown in the audit ledger.
        page_count: Number of PDF pages, when applicable.
    """

    text: str
    format: ReceiptFormat
    source: str
    page_count: int | None = None

    @property
    def character_count(self) -> int:
        """Number of characters available to the extractor."""

        return len(self.text)


def parse_receipt(data: bytes | str, *, filename: str | None = None) -> ParsedReceipt:
    """Normalise a receipt upload into text plus format metadata.

    Args:
        data: Raw file bytes or already-decoded text.
        filename: Original filename, used for format hints and the ledger.

    Returns:
        A :class:`ParsedReceipt` with normalised text.

    Raises:
        ReceiptParseError: If the document is empty, too large, unreadable or
            an unsupported structured format.
    """

    source = filename or ("pasted-text" if isinstance(data, str) else "upload")
    suffix = Path(filename or "").suffix.lower()

    if isinstance(data, str):
        raw = data.encode("utf-8")
        text = data
    else:
        raw = data
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ReceiptParseError(
                f"Receipt is {len(raw) / 1_048_576:.1f} MiB; "
                f"the limit is {MAX_RECEIPT_BYTES // 1_048_576} MiB."
            )
        text = ""

    if not raw.strip():
        raise ReceiptParseError("The receipt is empty. Upload a file or paste text.")

    if suffix == ".pdf" or raw.lstrip().startswith(b"%PDF"):
        if isinstance(data, str):
            raise ReceiptParseError("PDF receipts must be uploaded as file bytes.")
        text, page_count = _extract_pdf_text(data, source)
        return ParsedReceipt(
            text=_normalise_text(text),
            format="pdf",
            source=source,
            page_count=page_count,
        )

    if not text:
        text = _decode(raw, source)

    stripped = text.lstrip()
    if suffix == ".json" or stripped.startswith("{"):
        _validate_json_object(text, source)
        return ParsedReceipt(text=_normalise_text(text), format="json", source=source)

    return ParsedReceipt(text=_normalise_text(text), format="text", source=source)


def read_receipt_file(path: Path) -> ParsedReceipt:
    """Read and parse a receipt from disk.

    Args:
        path: Path to a supported receipt file.

    Returns:
        The parsed receipt.

    Raises:
        ReceiptParseError: If the file is missing, empty or unreadable.
    """

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReceiptParseError(f"Could not read '{path.name}': {exc}") from exc
    return parse_receipt(data, filename=path.name)


def list_sample_receipts(directory: Path | None = None) -> list[Path]:
    """Return the bundled demo receipts in a stable, documented order."""

    sample_dir = directory or SAMPLE_DIR
    if not sample_dir.is_dir():
        return []
    return sorted(
        path
        for path in sample_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _decode(raw: bytes, source: str) -> str:
    """Decode bytes as UTF-8, falling back to a friendly error."""

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReceiptParseError(
            f"'{source}' is not UTF-8 text. Export the receipt as text/JSON or "
            "upload the original PDF."
        ) from exc


def _validate_json_object(text: str, source: str) -> None:
    """Ensure JSON receipts are a single object with recognisable content."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReceiptParseError(
            f"'{source}' looks like JSON but could not be parsed: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc
    if not isinstance(payload, dict):
        raise ReceiptParseError(
            f"'{source}' must contain a JSON object, not {type(payload).__name__}."
        )
    if not payload:
        raise ReceiptParseError(f"'{source}' contains an empty JSON object.")


def _extract_pdf_text(data: bytes, source: str) -> tuple[str, int]:
    """Extract selectable text from a PDF using pypdf, then pdfplumber.

    pypdf is fast and handles most digital invoices; pdfplumber recovers text
    from trickier layouts. Scans without an OCR layer are rejected with an
    actionable message rather than silently producing an empty audit.
    """

    text = ""
    page_count = 0

    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # pragma: no cover - depends on malformed PDFs
        logger.debug("pypdf could not read %s: %s", source, exc)

    if not text.strip():
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                page_count = page_count or len(pdf.pages)
                text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        except Exception as exc:  # pragma: no cover - depends on malformed PDFs
            logger.debug("pdfplumber could not read %s: %s", source, exc)

    if not text.strip():
        raise ReceiptParseError(
            f"No selectable text found in '{source}'. It may be a scanned image; "
            "run OCR first or paste the receipt text manually."
        )

    return text, page_count


def _normalise_text(text: str) -> str:
    """Normalise whitespace without destroying the receipt's structure."""

    cleaned = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("\u2019", "'")
    )
    cleaned = _TRAILING_SPACE.sub("", cleaned)
    cleaned = _BLANK_RUNS.sub("\n\n", cleaned)
    return cleaned.strip()
