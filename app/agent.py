"""AuditFlow's agentic workflow: extraction, guardrails and trace collection.

The module is intentionally split into three layers of trust:

1. **Extraction** — a PydanticAI agent turns unstructured receipt text into a
   validated :class:`~app.models.InvoiceData`. In demo mode a deterministic
   extractor does the same work so the workflow runs without an API key.
2. **Guardrails** — deterministic Python tools consult ``data/policies.json``
   and the SQLite ledger. Their results, not the model's opinion, decide the
   route. This keeps the business rules reproducible and testable.
3. **Trace** — every step, tool call and decision is recorded as a
   :class:`~app.models.TraceStep`, persisted with the audit and written to an
   optional JSON-lines sink.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from datetime import date as DateType
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models import (
    ExtractionMode,
    InvoiceData,
    InvoiceExtractionError,
    LineItem,
    TraceKind,
    TraceStep,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_OPENAI_MODEL = "openai:gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-3-5-sonnet-latest"

#: Receipt text shown in a trace step; keeps traces readable and avoids dumping
#: entire documents into the audit ledger.
TRACE_TEXT_PREVIEW = 600

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%B %d, %Y",
)

_LINE_ITEM_PATTERN = re.compile(
    r"^(?:[-*]\s*)?(?P<qty>\d+(?:\.\d+)?)\s*[x×]\s*(?P<desc>.+?)\s*@\s*"
    r"(?P<unit>[$€£]?\s*[\d,]+(?:\.\d{1,2})?)\s*=\s*"
    r"(?P<amount>[$€£]?\s*[\d,]+(?:\.\d{1,2})?)\s*$",
    re.IGNORECASE,
)

_TEXT_FLAGS = re.IGNORECASE | re.MULTILINE

_TEXT_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "vendor": re.compile(
        r"^(?:vendor|merchant|supplier)(?:\s+name)?\s*[:\-]\s*(?P<value>.+)$",
        _TEXT_FLAGS,
    ),
    "date": re.compile(
        r"^(?:invoice\s+date|date|issued)(?:\s+on)?\s*[:\-]\s*(?P<value>.+)$",
        _TEXT_FLAGS,
    ),
    "currency": re.compile(
        r"^(?:currency|ccy)(?:\s+code)?\s*[:\-]\s*(?P<value>[A-Za-z]{3})\b",
        _TEXT_FLAGS,
    ),
    "category": re.compile(
        r"^(?:expense\s+category|category)\s*[:\-]\s*(?P<value>.+)$",
        _TEXT_FLAGS,
    ),
    "total_amount": re.compile(
        r"^(?:grand\s+total|total\s+amount|invoice\s+total|total|amount)"
        r"\s*[:\-]\s*(?P<value>.+)$",
        _TEXT_FLAGS,
    ),
}

_JSON_VENDOR_KEYS = ("vendor", "merchant", "supplier", "vendor_name")
_JSON_DATE_KEYS = ("date", "invoice_date", "issued_at", "issued_on")
_JSON_TOTAL_KEYS = ("total_amount", "grand_total", "total", "amount")
_JSON_CURRENCY_KEYS = ("currency", "currency_code", "ccy")
_JSON_CATEGORY_KEYS = ("category", "expense_category")
_JSON_LINE_ITEM_KEYS = ("line_items", "items", "lines")
_JSON_DESCRIPTION_KEYS = ("description", "desc", "name", "item")
_JSON_QUANTITY_KEYS = ("quantity", "qty")
_JSON_UNIT_PRICE_KEYS = ("unit_price", "price", "rate")
_JSON_AMOUNT_KEYS = ("amount", "line_total", "total", "extended_price")


class TraceCollector:
    """Ordered, thread-safe record of everything an audit did.

    Args:
        sink_path: Optional JSON-lines file. Defaults to ``AUDITFLOW_TRACE_LOG``
            (relative paths resolve against the project root). A sink failure is
            logged but never breaks an audit.

    Example:
        >>> collector = TraceCollector()
        >>> step = collector.add(
        ...     kind=TraceKind.SYSTEM, name="audit_started", summary="Starting audit"
        ... )
        >>> step.index
        0
    """

    def __init__(self, sink_path: Path | None = None) -> None:
        self._steps: list[TraceStep] = []
        self._lock = threading.Lock()
        self._sink_path = sink_path if sink_path is not None else _default_trace_sink()
        self._sink_warned = False

    def add(
        self,
        *,
        kind: TraceKind,
        name: str,
        summary: str,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> TraceStep:
        """Append a step and mirror it to the JSON-lines sink when configured."""

        with self._lock:
            step = TraceStep(
                index=len(self._steps),
                kind=kind,
                name=name,
                summary=summary,
                inputs=_jsonable(inputs) if inputs is not None else None,
                outputs=_jsonable(outputs) if outputs is not None else None,
                duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            )
            self._steps.append(step)
            self._write_to_sink(step)
            return step

    @property
    def steps(self) -> list[TraceStep]:
        """Return a shallow copy so callers cannot mutate collector state."""

        with self._lock:
            return list(self._steps)

    def as_dicts(self) -> list[dict[str, Any]]:
        """Serialise every step for storage or display."""

        return [step.model_dump(mode="json") for step in self.steps]

    def __len__(self) -> int:
        return len(self._steps)

    def _write_to_sink(self, step: TraceStep) -> None:
        """Best-effort append to the JSON-lines trace sink."""

        if self._sink_path is None:
            return
        try:
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self._sink_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(step.model_dump(mode="json")) + "\n")
        except OSError as exc:
            if not self._sink_warned:
                logger.warning("Could not write trace sink %s: %s", self._sink_path, exc)
                self._sink_warned = True


def extract_invoice_heuristically(raw_text: str) -> InvoiceData:
    """Extract an invoice without an LLM (demo mode and offline evaluation).

    Supports the labelled plain-text format used by the bundled samples and JSON
    receipts with common field aliases.

    Args:
        raw_text: Normalised receipt text.

    Returns:
        A validated :class:`~app.models.InvoiceData`.

    Raises:
        InvoiceExtractionError: If required fields are missing or malformed.
    """

    text = raw_text.strip()
    if not text:
        raise InvoiceExtractionError("The receipt is empty; nothing to extract.")

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvoiceExtractionError(
                f"Receipt looks like JSON but could not be parsed: {exc.msg} "
                f"(line {exc.lineno})."
            ) from exc
        if not isinstance(payload, dict):
            raise InvoiceExtractionError("JSON receipts must contain an object.")
        return _invoice_from_json(payload)

    return _invoice_from_text(text)


def _invoice_from_text(text: str) -> InvoiceData:
    """Parse the labelled text format into an invoice."""

    fields: dict[str, str] = {}
    for name, pattern in _TEXT_FIELD_PATTERNS.items():
        match = pattern.search(text)
        if match:
            fields[name] = match.group("value").strip()

    missing = [
        label
        for label, key in (("vendor", "vendor"), ("date", "date"), ("total", "total_amount"))
        if key not in fields
    ]
    if missing:
        raise InvoiceExtractionError(
            "Could not find "
            + ", ".join(missing)
            + " in the receipt. Add labelled fields (Vendor:, Date:, Total:) or "
            "upload a JSON receipt, then try again."
        )

    line_items: list[LineItem] = []
    in_items = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^line\s+items?\s*:?\s*$", stripped, re.IGNORECASE):
            in_items = True
            continue
        if in_items and not stripped:
            continue
        match = _LINE_ITEM_PATTERN.match(stripped)
        if match:
            line_items.append(
                LineItem(
                    description=match.group("desc").strip(" -"),
                    quantity=_parse_amount_value(match.group("qty")),
                    unit_price=_parse_amount_value(match.group("unit")),
                    amount=_parse_amount_value(match.group("amount")),
                )
            )
        elif in_items and stripped and not match:
            # A new labelled section (Payment terms, Bill to, ...) ends the block.
            if re.match(r"^[A-Za-z][A-Za-z /_-]{2,40}\s*:", stripped):
                in_items = False

    confidence = 0.70
    if line_items:
        confidence += 0.15
    if "currency" in fields:
        confidence += 0.05
    if "category" in fields:
        confidence += 0.05
    if "." in fields["total_amount"]:
        confidence += 0.04

    return _build_invoice(
        vendor=fields["vendor"],
        date_value=fields["date"],
        total_value=fields["total_amount"],
        currency=fields.get("currency", "USD"),
        category=fields.get("category", "other"),
        line_items=line_items,
        confidence=round(min(confidence, 0.99), 2),
    )


def _invoice_from_json(payload: dict[str, Any]) -> InvoiceData:
    """Parse a JSON receipt with tolerant field aliases."""

    vendor = _first_present(payload, _JSON_VENDOR_KEYS)
    date_value = _first_present(payload, _JSON_DATE_KEYS)
    total_value = _first_present(payload, _JSON_TOTAL_KEYS)

    missing = [
        label
        for label, value in (
            ("vendor", vendor),
            ("date", date_value),
            ("total_amount", total_value),
        )
        if value is None
    ]
    if missing:
        raise InvoiceExtractionError(
            "JSON receipt is missing required field(s): "
            + ", ".join(missing)
            + ". Include vendor, date and total_amount."
        )

    raw_items = _first_present(payload, _JSON_LINE_ITEM_KEYS) or []
    if not isinstance(raw_items, list):
        raise InvoiceExtractionError("'line_items' must be a JSON array.")

    line_items: list[LineItem] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise InvoiceExtractionError(f"Line item {index} must be a JSON object.")
        description = _first_present(item, _JSON_DESCRIPTION_KEYS)
        quantity = _first_present(item, _JSON_QUANTITY_KEYS)
        unit_price = _first_present(item, _JSON_UNIT_PRICE_KEYS)
        amount = _first_present(item, _JSON_AMOUNT_KEYS)

        parsed_quantity = _parse_amount_value(quantity) if quantity is not None else 1.0
        parsed_unit = _parse_amount_value(unit_price) if unit_price is not None else 0.0
        if amount is None:
            parsed_amount = round(parsed_quantity * parsed_unit, 2)
        else:
            parsed_amount = _parse_amount_value(amount)

        line_items.append(
            LineItem(
                description=str(description or f"Line item {index}"),
                quantity=parsed_quantity,
                unit_price=parsed_unit,
                amount=parsed_amount,
            )
        )

    category = _first_present(payload, _JSON_CATEGORY_KEYS) or "other"
    currency = _first_present(payload, _JSON_CURRENCY_KEYS) or "USD"
    confidence = 0.90 + (0.05 if line_items else 0.0) + (0.03 if category else 0.0)

    return _build_invoice(
        vendor=vendor,
        date_value=date_value,
        total_value=total_value,
        currency=str(currency),
        category=str(category),
        line_items=line_items,
        confidence=round(min(confidence, 0.99), 2),
    )


def _build_invoice(
    *,
    vendor: Any,
    date_value: Any,
    total_value: Any,
    currency: str,
    category: str,
    line_items: list[LineItem],
    confidence: float,
) -> InvoiceData:
    """Construct and validate an invoice, converting Pydantic errors."""

    try:
        return InvoiceData(
            vendor=str(vendor),
            date=_parse_date_value(date_value),
            total_amount=_parse_amount_value(total_value),
            currency=currency,
            line_items=line_items,
            category=category,
            confidence_score=confidence,
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        raise InvoiceExtractionError(
            f"Extracted invoice failed validation: {problems}"
        ) from exc


def _parse_amount_value(value: Any) -> float:
    """Parse an amount from a number or a currency-formatted string."""

    if isinstance(value, bool):
        raise InvoiceExtractionError("Boolean values are not valid amounts.")
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        text = str(value).strip()
        cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
        if cleaned in {"", "-", ".", "-."}:
            raise InvoiceExtractionError(f"Could not read an amount from {value!r}.")
        try:
            amount = float(cleaned)
        except ValueError as exc:
            raise InvoiceExtractionError(
                f"Could not read an amount from {value!r}."
            ) from exc

    if not math.isfinite(amount):
        raise InvoiceExtractionError(f"Amount {value!r} is not finite.")
    if amount < 0:
        raise InvoiceExtractionError(f"Amount {value!r} must not be negative.")
    return round(amount, 2)


def _parse_date_value(value: Any) -> DateType:
    """Parse an invoice date from an ISO string or common receipt format."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, DateType):
        return value

    text = str(value).strip()
    if not text:
        raise InvoiceExtractionError("The invoice date is empty.")

    iso_candidate = text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}T", text) else text
    try:
        return DateType.fromisoformat(iso_candidate)
    except ValueError:
        pass

    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue

    raise InvoiceExtractionError(
        f"Unrecognised invoice date {value!r}. Use ISO format (YYYY-MM-DD) or a "
        "common format such as 14/10/2026."
    )


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty alias value from a JSON object."""

    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _jsonable(value: Any) -> Any:
    """Convert arbitrary step payloads into JSON-safe structures."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, DateType)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return repr(value)


def _preview(text: str, limit: int = TRACE_TEXT_PREVIEW) -> str:
    """Return a truncated, single-spaced preview for trace payloads."""

    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _default_trace_sink() -> Path | None:
    """Resolve the optional trace sink path from the environment."""

    configured = os.getenv("AUDITFLOW_TRACE_LOG", "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
