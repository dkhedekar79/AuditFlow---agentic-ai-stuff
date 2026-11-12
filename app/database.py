"""SQLite persistence for audits, traces and human decisions.

The ledger is the single source of truth for the review queue, duplicate
detection and reporting. Each row stores the extracted invoice as a validated
JSON document plus a fully materialised reasoning trace, so an audit remains
explainable even after the process that produced it is gone.

SQLite is configured with WAL mode and a busy timeout so the Streamlit
dashboard, the FastAPI app and the test suite can share a file safely.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date as DateType
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.types import JSON, DateTime, Float, String, Text

from app.models import (
    AuditOutcome,
    AuditRecord,
    AuditStatus,
    ExtractionMode,
    HumanDecision,
    InvoiceData,
    PolicyResult,
    TraceStep,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_FILENAME = "auditflow.db"

#: Defaults mirroring ``data/policies.json``; callers pass policy values when available.
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_AMOUNT_TOLERANCE = 0.01

#: SQLAlchemy session factory type used across the application.
SessionFactory = sessionmaker[Session]


def _utcnow() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to SQLite's naive datetimes and normalise aware ones."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all AuditFlow tables."""


class AuditRecordRow(Base):
    """One audited invoice, its policy results, reasoning trace and decision."""

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    extraction_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    vendor: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    invoice_date: Mapped[DateType] = mapped_column(nullable=False, index=True)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    audit_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_results: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list
    )
    trace: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    invoice_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    human_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AuditRecordRow(id={self.id!r}, vendor={self.vendor!r}, "
            f"status={self.status!r}, total_amount={self.total_amount!r})"
        )


def resolve_database_url(database_url: str | None = None) -> str:
    """Resolve a SQLAlchemy URL from an argument, environment or default file.

    ``AUDITFLOW_DB_PATH`` may be absolute or relative to the project root. The
    parent directory is created on demand so a fresh clone works immediately.
    """

    if database_url:
        return database_url

    configured = Path(os.getenv("AUDITFLOW_DB_PATH", DEFAULT_DATABASE_FILENAME))
    path = configured.expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def build_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine with SQLite-appropriate pragmas.

    Args:
        database_url: Optional explicit URL (tests pass a temporary file).
        echo: When true, log emitted SQL for debugging.

    Returns:
        A configured engine. The schema is *not* created here.
    """

    url = resolve_database_url(database_url)
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 15} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            """Make concurrent dashboard/API access reliable."""

            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create tables if they do not exist. Safe to call on every startup."""

    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> SessionFactory:
    """Return a session factory bound to ``engine``."""

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@lru_cache(maxsize=1)
def get_default_engine() -> Engine:
    """Lazily build and cache the process-wide engine used by the UI and API."""

    engine = build_engine()
    init_db(engine)
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> SessionFactory:
    """Return the process-wide session factory, creating the schema once."""

    return session_factory(get_default_engine())


def reset_default_engine() -> None:
    """Dispose the cached engine so tests or env changes start clean."""

    if get_default_engine.cache_info().currsize:
        get_default_engine().dispose()
    get_default_engine.cache_clear()
    get_session_factory.cache_clear()


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """Run a unit of work, committing on success and rolling back on error."""

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _row_to_record(row: AuditRecordRow) -> AuditRecord:
    """Convert an ORM row back into the public Pydantic record."""

    invoice = InvoiceData.model_validate(row.invoice_payload)
    return AuditRecord(
        id=row.id,
        source=row.source,
        extraction_mode=ExtractionMode(row.extraction_mode),
        model_name=row.model_name,
        invoice=invoice,
        policy_results=[
            PolicyResult.model_validate(payload) for payload in row.policy_results
        ],
        audit_flags=list(row.audit_flags or []),
        status=AuditStatus(row.status),
        trace=[TraceStep.model_validate(step) for step in row.trace or []],
        human_decision=(
            HumanDecision(row.human_decision) if row.human_decision else None
        ),
        reviewer=row.reviewer,
        reviewer_note=row.reviewer_note,
        reviewed_at=_as_utc(row.reviewed_at),
        created_at=_as_utc(row.created_at) or _utcnow(),
        updated_at=_as_utc(row.updated_at) or _utcnow(),
    )


def create_audit_record(factory: SessionFactory, outcome: AuditOutcome) -> AuditRecord:
    """Persist an agent outcome and return the stored record.

    Args:
        factory: Session factory for the target database.
        outcome: Result produced by :func:`app.agent.run_audit`.

    Returns:
        The persisted :class:`AuditRecord` with its assigned id.
    """

    row = AuditRecordRow(
        source=outcome.source,
        extraction_mode=outcome.extraction_mode.value,
        model_name=outcome.model_name,
        vendor=outcome.invoice.vendor,
        invoice_date=outcome.invoice.date,
        total_amount=outcome.invoice.total_amount,
        currency=outcome.invoice.currency,
        category=outcome.invoice.category,
        confidence_score=outcome.invoice.confidence_score,
        status=outcome.status.value,
        audit_flags=list(outcome.audit_flags),
        policy_results=[
            result.model_dump(mode="json") for result in outcome.policy_results
        ],
        trace=[step.model_dump(mode="json") for step in outcome.trace],
        invoice_payload=outcome.invoice.model_dump(mode="json"),
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )

    with session_scope(factory) as session:
        session.add(row)
        session.flush()
        record_id = row.id

    record = get_audit_record(factory, record_id)
    if record is None:  # pragma: no cover - defensive, would indicate a driver bug
        raise RuntimeError(f"audit record {record_id} vanished after insertion")
    return record


def get_audit_record(
    factory: SessionFactory, record_id: int
) -> AuditRecord | None:
    """Fetch a single audit by id, or ``None`` when it does not exist."""

    with session_scope(factory) as session:
        row = session.get(AuditRecordRow, record_id)
        return _row_to_record(row) if row is not None else None


def list_audit_records(
    factory: SessionFactory,
    *,
    status: AuditStatus | str | None = None,
    limit: int = 100,
    offset: int = 0,
    newest_first: bool = True,
) -> list[AuditRecord]:
    """List audits, optionally filtered by status.

    Args:
        factory: Session factory for the target database.
        status: Optional status filter (accepts the enum or its value).
        limit: Maximum rows to return, capped at 500.
        offset: Pagination offset.
        newest_first: Order by creation time descending.

    Returns:
        Matching records, converted to the public model.
    """

    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must be non-negative")

    stmt = select(AuditRecordRow)
    if status is not None:
        stmt = stmt.where(AuditRecordRow.status == AuditStatus(status).value)

    ordering = (
        AuditRecordRow.created_at.desc() if newest_first else AuditRecordRow.created_at
    )
    stmt = stmt.order_by(ordering, AuditRecordRow.id.desc()).limit(limit).offset(offset)

    with session_scope(factory) as session:
        rows = session.scalars(stmt).all()
        return [_row_to_record(row) for row in rows]


def count_by_status(factory: SessionFactory) -> dict[AuditStatus, int]:
    """Return how many audits exist in each lifecycle state (zero-filled)."""

    stmt = select(AuditRecordRow.status, func.count()).group_by(AuditRecordRow.status)
    counts = {status: 0 for status in AuditStatus}
    with session_scope(factory) as session:
        for raw_status, count in session.execute(stmt):
            counts[AuditStatus(raw_status)] = int(count)
    return counts
