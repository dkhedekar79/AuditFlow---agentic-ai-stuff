"""Typed contracts shared by the agent, API, UI and persistence layers.

Everything that crosses a boundary in AuditFlow is a Pydantic model:

* the LLM must return an :class:`InvoiceData` (or the run is rejected/retried),
* deterministic tools report :class:`PolicyResult` objects,
* every step of the workflow is captured as a :class:`TraceStep`,
* the SQLite ledger stores :class:`AuditRecord` rows.

Models are deliberately strict (``extra="forbid"``) so a malformed LLM response
fails validation instead of silently leaking into the ledger.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuditFlowError(Exception):
    """Base class for errors AuditFlow can explain to a human reviewer."""


class InvoiceExtractionError(AuditFlowError):
    """Raised when a receipt cannot be converted into a valid ``InvoiceData``."""


class InvalidStateTransition(AuditFlowError):
    """Raised when a decision is not allowed from an audit's current status."""


class AuditStatus(str, Enum):
    """Lifecycle of an audited invoice.

    ``PENDING_REVIEW`` and ``AUTO_APPROVED`` are the two routing outcomes of the
    agent. ``APPROVED`` and ``REJECTED`` are terminal human decisions.
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTO_APPROVED = "AUTO_APPROVED"


class HumanDecision(str, Enum):
    """Decisions a reviewer can record for a flagged invoice."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExtractionMode(str, Enum):
    """How invoice fields were extracted from the source document."""

    LLM = "llm"
    HEURISTIC = "heuristic"


class TraceKind(str, Enum):
    """Category of a reasoning-trace entry, used for colour coding in the UI."""

    SYSTEM = "system"
    EXTRACTION = "extraction"
    LLM = "llm"
    TOOL = "tool"
    POLICY = "policy"
    ROUTING = "routing"
    HUMAN = "human"


#: Statuses that no further decision may change.
TERMINAL_STATUSES: frozenset[AuditStatus] = frozenset(
    {AuditStatus.APPROVED, AuditStatus.REJECTED}
)

#: Human-readable category aliases mapped onto the keys in ``data/policies.json``.
CATEGORY_ALIASES: dict[str, str] = {
    "software_subscription": "software",
    "software_subscriptions": "software",
    "saas": "software",
    "office_supplies": "office_supplies",
    "office_supply": "office_supplies",
    "supplies": "office_supplies",
    "equipment": "hardware",
    "it_hardware": "hardware",
    "meals_and_entertainment": "meals",
    "food": "meals",
    "professional_service": "professional_services",
    "consulting": "professional_services",
    "travel_expense": "travel",
    "lodging": "travel",
    "education": "training",
    "learning": "training",
}

_WHITESPACE = re.compile(r"\s+")
_VENDOR_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|pty|sarl)\b\.?", re.IGNORECASE
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_category(value: str) -> str:
    """Return the canonical policy key for a free-text expense category.

    Args:
        value: Category as written on the receipt or returned by the LLM.

    Returns:
        Lowercase, underscore-separated category key (unknown values are
        slugified rather than dropped so policy checks can flag them).
    """

    slug = _NON_ALNUM.sub("_", value.strip().lower()).strip("_")
    return CATEGORY_ALIASES.get(slug, slug)


def normalise_vendor_name(value: str) -> str:
    """Normalise a vendor name for fuzzy duplicate and allow-list matching.

    Strips legal suffixes and punctuation so ``"Acme Corp."`` and
    ``"ACME"`` compare equal.
    """

    lowered = _VENDOR_SUFFIXES.sub(" ", value.strip().lower())
    return _NON_ALNUM.sub("", lowered)


class LineItem(BaseModel):
    """A single billed line on an invoice."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(
        min_length=1,
        max_length=240,
        description="What was purchased, as printed on the receipt.",
    )
    quantity: float = Field(
        default=1.0, gt=0, le=10_000, description="Units purchased (defaults to 1)."
    )
    unit_price: float = Field(
        default=0.0, ge=0, le=1_000_000, description="Price per unit."
    )
    amount: float = Field(
        ge=0, le=1_000_000, description="Extended amount for this line."
    )

    @property
    def line_total(self) -> float:
        """Return the line amount rounded to cents."""

        return round(self.amount, 2)


class InvoiceData(BaseModel):
    """Strict, validated representation of one supplier invoice.

    This is the structured output contract for the extraction agent. The field
    set intentionally mirrors what a finance reviewer needs to make a decision;
    optional context such as payment terms is not trusted to the model.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str = Field(
        min_length=2,
        max_length=120,
        description="Legal or trading name of the supplier.",
    )
    date: dt.date = Field(description="Invoice issue date (ISO ``YYYY-MM-DD``).")
    total_amount: float = Field(
        gt=0,
        le=1_000_000,
        description="Grand total charged, including tax. Always positive.",
    )
    currency: str = Field(
        default="USD",
        description="ISO-4217 three-letter currency code, e.g. ``USD``.",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        max_length=100,
        description="Itemised charges; empty when the receipt has no breakdown.",
    )
    category: str = Field(
        min_length=2,
        max_length=60,
        description="Spend category, e.g. ``software``, ``travel``, ``meals``.",
    )
    confidence_score: float = Field(
        ge=0,
        le=1,
        description="Extractor confidence from 0 (guess) to 1 (verbatim fields).",
    )

    @field_validator("vendor")
    @classmethod
    def _clean_vendor(cls, value: str) -> str:
        """Collapse whitespace and strip stray punctuation from the vendor."""

        cleaned = _WHITESPACE.sub(" ", value).strip(" -:,.")
        if len(cleaned) < 2:
            raise ValueError("vendor must contain at least two characters")
        return cleaned

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str) -> str:
        """Normalise currency codes to uppercase ISO-4217 form."""

        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be a three-letter code such as USD")
        return code

    @field_validator("category")
    @classmethod
    def _normalise_category(cls, value: str) -> str:
        """Map free-text categories onto policy keys."""

        return normalise_category(value)

    @field_validator("date")
    @classmethod
    def _sane_invoice_date(cls, value: dt.date) -> dt.date:
        """Reject dates that cannot belong to a real expense claim."""

        today = dt.date.today()
        if value.year < 2000:
            raise ValueError("invoice date looks implausible (before 2000)")
        if value > today + dt.timedelta(days=365):
            raise ValueError("invoice date is more than a year in the future")
        return value

    @property
    def line_items_total(self) -> float:
        """Sum of all line item amounts, rounded to cents."""

        return round(sum(item.line_total for item in self.line_items), 2)


class PolicyResult(BaseModel):
    """Outcome of one deterministic policy check.

    ``details`` carries the raw tool payload so reviewers (and the trace
    inspector) can see exactly which limit or record drove the decision.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    check: str = Field(
        min_length=2,
        max_length=60,
        description="Stable identifier, e.g. ``budget_limit``.",
    )
    title: str = Field(
        min_length=2, max_length=80, description="Human-readable check name."
    )
    passed: bool = Field(description="True when the invoice satisfies this policy.")
    severity: Literal["info", "warning", "critical"] = Field(
        default="info", description="Criticality when the check fails."
    )
    message: str = Field(
        min_length=1,
        max_length=500,
        description="Reviewer-facing explanation of the result.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict, description="Structured payload returned by the tool."
    )
    policy_reference: str | None = Field(
        default=None,
        max_length=200,
        description="Dotted path into ``data/policies.json``.",
    )

    @property
    def status_label(self) -> str:
        """Short human label for tables and badges."""

        return "Passed" if self.passed else "Failed"


class TraceStep(BaseModel):
    """One observable step in the agentic audit loop.

    Trace steps are persisted alongside the audit record, so a reviewer can
    reconstruct every LLM request, tool call, policy check and routing decision
    after the fact without reading server logs.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    index: int = Field(ge=0, description="Zero-based position in the trace.")
    kind: TraceKind = Field(description="Which stage of the workflow produced this.")
    name: str = Field(
        min_length=2, max_length=80, description="Stable step identifier."
    )
    summary: str = Field(
        min_length=1, max_length=1000, description="One-line description for the UI."
    )
    inputs: dict[str, Any] | None = Field(
        default=None, description="Sanitised arguments supplied to the step."
    )
    outputs: dict[str, Any] | None = Field(
        default=None, description="Sanitised result produced by the step."
    )
    duration_ms: float | None = Field(
        default=None, ge=0, description="Wall-clock duration, when measured."
    )
    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc),
        description="UTC timestamp for the step.",
    )


class AuditBase(BaseModel):
    """Fields shared by an in-flight audit outcome and its persisted record."""

    invoice: InvoiceData
    policy_results: list[PolicyResult] = Field(default_factory=list)
    audit_flags: list[str] = Field(
        default_factory=list,
        description="Machine-readable reasons the invoice needs attention.",
    )
    status: AuditStatus
    trace: list[TraceStep] = Field(default_factory=list)
    extraction_mode: ExtractionMode
    model_name: str | None = Field(
        default=None, description="PydanticAI model identifier, if an LLM was used."
    )
    source: str = Field(
        min_length=1, max_length=255, description="Receipt filename or upload origin."
    )

    @property
    def failed_checks(self) -> list[PolicyResult]:
        """Policy results that did not pass, in evaluation order."""

        return [result for result in self.policy_results if not result.passed]

    @property
    def requires_human_review(self) -> bool:
        """True while the audit is paused waiting for a reviewer."""

        return self.status is AuditStatus.PENDING_REVIEW


class AuditRecord(AuditBase):
    """A persisted audit, including its reasoning trace and human decision."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(gt=0)
    human_decision: HumanDecision | None = None
    reviewer: str | None = Field(default=None, max_length=120)
    reviewer_note: str | None = Field(default=None, max_length=1000)
    reviewed_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def is_terminal(self) -> bool:
        """True once a human decision has been recorded."""

        return self.status in TERMINAL_STATUSES


class AuditOutcome(AuditBase):
    """Result of one agent run, before or after it is written to the ledger."""

    record_id: int | None = Field(
        default=None, description="Ledger id assigned when the audit was persisted."
    )


class ReviewDecision(BaseModel):
    """A human review action submitted from the dashboard or API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: HumanDecision = Field(description="Approve or reject the invoice.")
    note: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional override or rejection rationale.",
    )
    reviewer: str = Field(
        default="human-reviewer",
        min_length=2,
        max_length=120,
        description="Name or email of the person recording the decision.",
    )
