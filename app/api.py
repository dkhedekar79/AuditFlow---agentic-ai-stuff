"""Optional FastAPI surface for the AuditFlow workflow.

The dashboard is the primary interface, but the same workflow is exposed as a
small JSON API so it can be embedded in another finance tool:

    uvicorn app.api:app --reload --port 8000

Endpoints
    GET  /health                      liveness + runtime mode
    GET  /runtime                     extraction mode and model
    POST /audits                      run the agent and persist the audit
    GET  /audits                      list audits, filter by status
    GET  /audits/stats                counts per lifecycle state
    GET  /audits/{record_id}          fetch one audit with its trace
    POST /audits/{record_id}/decision record a human approve/reject decision
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import __version__
from app.agent import describe_runtime, run_audit
from app.database import (
    SessionFactory,
    apply_human_decision,
    count_by_status,
    get_audit_record,
    get_default_engine,
    get_session_factory,
    init_db,
    list_audit_records,
)
from app.models import (
    AuditFlowError,
    AuditOutcome,
    AuditRecord,
    AuditStatus,
    InvalidStateTransition,
    InvoiceExtractionError,
    ReviewDecision,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Ensure the SQLite schema exists before the first request."""

    init_db(get_default_engine())
    yield


app = FastAPI(
    title="AuditFlow API",
    version=__version__,
    description=(
        "Human-in-the-loop invoice auditing: PydanticAI extraction, deterministic "
        "policy guardrails and an audited human decision step."
    ),
    lifespan=lifespan,
)


class AuditRequest(BaseModel):
    """Payload for running a new audit."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(
        min_length=10,
        max_length=60_000,
        description="Receipt text. Upload PDFs through the dashboard and post the "
        "extracted text here.",
    )
    source: str = Field(
        default="api-upload",
        min_length=1,
        max_length=255,
        description="Label stored with the audit, usually the filename.",
    )
    extraction_mode: Literal["auto", "llm", "heuristic"] = Field(
        default="auto",
        description="'auto' uses the LLM when a key is configured, else demo mode.",
    )


def get_factory() -> SessionFactory:
    """FastAPI dependency returning the process-wide session factory.

    Tests override this with ``app.dependency_overrides`` to point at a temporary
    database, which keeps the API and the evaluation suite independent.
    """

    return get_session_factory()


@app.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Liveness probe that also reports the active extraction mode."""

    runtime = describe_runtime()
    return {"status": "ok", "version": __version__, **runtime}


@app.get("/runtime", tags=["meta"])
def runtime() -> dict[str, Any]:
    """Return the active extraction mode, model and safety flags."""

    return describe_runtime()


@app.post("/audits", response_model=AuditOutcome, status_code=201, tags=["audits"])
def create_audit(
    payload: AuditRequest, factory: SessionFactory = Depends(get_factory)
) -> AuditOutcome:
    """Run the extraction agent, policy guardrails and routing, then persist.

    Returns ``PENDING_REVIEW`` for anything that needs a human, or
    ``AUTO_APPROVED`` when every deterministic check passes.
    """

    try:
        return run_audit(
            payload.text,
            source=payload.source,
            session_factory=factory,
            extraction_mode=payload.extraction_mode,
        )
    except InvoiceExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AuditFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/audits", response_model=list[AuditRecord], tags=["audits"])
def list_audits(
    status: AuditStatus | None = Query(
        default=None, description="Filter by lifecycle status."
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    factory: SessionFactory = Depends(get_factory),
) -> list[AuditRecord]:
    """List audits, newest first."""

    return list_audit_records(factory, status=status, limit=limit, offset=offset)


@app.get("/audits/stats", tags=["audits"])
def audit_stats(factory: SessionFactory = Depends(get_factory)) -> dict[str, int]:
    """Return the size of every lifecycle bucket (including zeros)."""

    return {
        status.value: count
        for status, count in count_by_status(factory).items()
    }


@app.get("/audits/{record_id}", response_model=AuditRecord, tags=["audits"])
def read_audit(
    record_id: int, factory: SessionFactory = Depends(get_factory)
) -> AuditRecord:
    """Fetch a single audit with its full trace and policy results."""

    record = get_audit_record(factory, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Audit {record_id} not found.")
    return record


@app.post(
    "/audits/{record_id}/decision",
    response_model=AuditRecord,
    tags=["audits"],
)
def record_decision(
    record_id: int,
    payload: ReviewDecision,
    factory: SessionFactory = Depends(get_factory),
) -> AuditRecord:
    """Record a human approve/reject decision and resume the workflow.

    Terminal audits cannot be decided again; that returns ``409 Conflict``.
    """

    try:
        return apply_human_decision(factory, record_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
