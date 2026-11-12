"""Evaluation suite for AuditFlow's agentic workflow.

The four bundled receipts are the project's contract: each one must always
produce the documented routing outcome, with or without an LLM. These tests run
entirely offline in demo mode against a temporary SQLite ledger.

Run with::

    pytest -q
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app import tools
from app.agent import (
    TraceCollector,
    decide_status,
    extract_invoice_heuristically,
    run_audit,
)
from app.database import (
    SessionFactory,
    apply_human_decision,
    build_engine,
    count_by_status,
    find_duplicate_invoice,
    init_db,
    list_audit_records,
    reset_ledger,
    session_factory,
)
from app.models import (
    AuditFlowError,
    AuditStatus,
    HumanDecision,
    InvalidStateTransition,
    InvoiceData,
    InvoiceExtractionError,
    PolicyResult,
    ReviewDecision,
    TraceKind,
    TraceStep,
)
from app.parsing import read_receipt_file

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "sample_receipts"
CLEAN = SAMPLES / "01_clean_software_receipt.txt"
OVER_LIMIT = SAMPLES / "02_over_limit_travel.json"
DUPLICATE = SAMPLES / "03_duplicate_software_receipt.txt"
UNAPPROVED = SAMPLES / "04_unapproved_vendor_receipt.json"

ALL_SAMPLES = [CLEAN, OVER_LIMIT, DUPLICATE, UNAPPROVED]

EVAL_CASES = [
    pytest.param(CLEAN, AuditStatus.AUTO_APPROVED, set(), id="clean-auto-approve"),
    pytest.param(
        OVER_LIMIT,
        AuditStatus.PENDING_REVIEW,
        {"BUDGET_LIMIT_EXCEEDED", "OVER_AUTO_APPROVAL_THRESHOLD"},
        id="over-limit-human-review",
    ),
    pytest.param(
        DUPLICATE,
        AuditStatus.PENDING_REVIEW,
        {"DUPLICATE_INVOICE"},
        id="duplicate-human-review",
    ),
    pytest.param(
        UNAPPROVED,
        AuditStatus.PENDING_REVIEW,
        {"UNAPPROVED_VENDOR"},
        id="unapproved-vendor-human-review",
    ),
]


@pytest.fixture()
def factory(tmp_path: Path) -> SessionFactory:
    """Provide an isolated SQLite ledger for each test."""

    engine = build_engine(f"sqlite:///{tmp_path / 'auditflow-test.db'}")
    init_db(engine)
    return session_factory(engine)


@pytest.fixture(autouse=True)
def demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force deterministic extraction so evaluations never call an LLM."""

    monkeypatch.setenv("AUDITFLOW_DEMO_MODE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AUDITFLOW_TRACE_LOG", raising=False)


def audit_sample(
    factory: SessionFactory, path: Path, *, persist: bool = True
):
    """Run the workflow for a sample receipt."""

    parsed = read_receipt_file(path)
    return run_audit(
        parsed.text,
        source=path.name,
        session_factory=factory,
        persist=persist,
    )


# ── Routing evaluation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("sample, expected_status, expected_flags", EVAL_CASES)
def test_sample_routes_to_documented_outcome(
    factory: SessionFactory,
    sample: Path,
    expected_status: AuditStatus,
    expected_flags: set[str],
) -> None:
    """The four samples are the acceptance criteria for the whole workflow."""

    if sample is DUPLICATE:
        # A duplicate is only detectable once the original exists in the ledger.
        audit_sample(factory, CLEAN)

    outcome = audit_sample(factory, sample)

    assert outcome.status is expected_status
    assert set(outcome.audit_flags) == expected_flags
    assert outcome.record_id is not None
    assert len(outcome.policy_results) == 4
    assert outcome.trace[-1].kind is TraceKind.ROUTING
    assert outcome.trace[-1].outputs["status"] == expected_status.value


def test_clean_sample_is_extracted_accurately() -> None:
    invoice = extract_invoice_heuristically(read_receipt_file(CLEAN).text)
    assert invoice.vendor == "Northwind Cloud Services"
    assert invoice.total_amount == pytest.approx(89.0)
    assert invoice.currency == "USD"
    assert invoice.category == "software"
    assert len(invoice.line_items) == 2
    assert invoice.line_items_total == pytest.approx(invoice.total_amount)
    assert 0 < invoice.confidence_score <= 1


@pytest.mark.parametrize("sample", ALL_SAMPLES)
def test_extraction_produces_complete_typed_invoice(sample: Path) -> None:
    invoice = extract_invoice_heuristically(read_receipt_file(sample).text)
    assert isinstance(invoice, InvoiceData)
    assert invoice.vendor
    assert invoice.date <= date.today()
    assert invoice.total_amount > 0
    assert invoice.currency.isalpha() and len(invoice.currency) == 3
    assert invoice.category
    assert 0 <= invoice.confidence_score <= 1


@pytest.mark.parametrize(
    "receipt",
    ["", "just some random text", "Vendor: Acme\nDate: someday\nTotal: 10"],
)
def test_malformed_receipts_fail_with_actionable_error(receipt: str) -> None:
    with pytest.raises(InvoiceExtractionError):
        extract_invoice_heuristically(receipt)


def test_llm_mode_without_key_explains_the_problem(
    factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDITFLOW_DEMO_MODE", "0")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    with pytest.raises(InvoiceExtractionError, match="no provider API key"):
        run_audit(
            read_receipt_file(CLEAN).text,
            source="clean.txt",
            session_factory=factory,
            extraction_mode="llm",
        )


def test_unknown_extraction_mode_is_rejected(factory: SessionFactory) -> None:
    with pytest.raises(ValueError, match="Unknown extraction mode"):
        run_audit(
            read_receipt_file(CLEAN).text,
            session_factory=factory,
            extraction_mode="magic",
        )


def test_persist_false_leaves_the_ledger_untouched(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, OVER_LIMIT, persist=False)
    assert outcome.record_id is None
    assert outcome.status is AuditStatus.PENDING_REVIEW
    assert list_audit_records(factory) == []


def test_routing_is_derived_from_failed_checks() -> None:
    failing = PolicyResult(
        check="budget_limit",
        title="Category budget",
        passed=False,
        severity="critical",
        message="Over budget.",
    )
    status, flags = decide_status([failing])
    assert status is AuditStatus.PENDING_REVIEW
    assert flags == ["BUDGET_LIMIT_EXCEEDED"]

    status, flags = decide_status([failing.model_copy(update={"passed": True})])
    assert status is AuditStatus.AUTO_APPROVED
    assert flags == []


# ── Guardrail tools ─────────────────────────────────────────────────────────


def test_budget_tool_reports_headroom_and_overage() -> None:
    within = tools.check_budget_limit("software", 89.0)
    assert within["passed"] is True
    assert within["limit"] == pytest.approx(2500.0)
    assert within["remaining"] == pytest.approx(2411.0)

    over = tools.check_budget_limit("travel", 1249.0)
    assert over["passed"] is False
    assert over["overage"] == pytest.approx(449.0)


def test_budget_tool_flags_unmapped_categories() -> None:
    result = tools.check_budget_limit("gadgets", 100.0)
    assert result["passed"] is False
    assert result["unknown_category"] is True
    assert "known_categories" in result


def test_vendor_tool_matches_aliases_and_blocked_list() -> None:
    approved = tools.verify_approved_vendor("Northwind Cloud Svcs")
    assert approved["approved"] is True
    assert approved["matched_vendor"] == "Northwind Cloud Services"

    blocked = tools.verify_approved_vendor("QuickCash Gadgets Ltd")
    assert blocked["approved"] is False
    assert blocked["blocked"] is True

    unknown = tools.verify_approved_vendor("Totally New Vendor LLC")
    assert unknown["approved"] is False
    assert unknown["blocked"] is False


def test_duplicate_tool_finds_original_record(factory: SessionFactory) -> None:
    original = audit_sample(factory, CLEAN)
    assert original.record_id is not None

    found = tools.duplicate_check(
        "Northwind Cloud Services",
        "2026-10-14",
        89.0,
        session_factory=factory,
    )
    assert found["duplicate_found"] is True
    assert found["matched_record_id"] == original.record_id
    assert found["passed"] is False


def test_duplicate_lookup_ignores_other_amounts(factory: SessionFactory) -> None:
    audit_sample(factory, CLEAN)
    assert (
        find_duplicate_invoice(
            factory,
            vendor="Northwind Cloud Services",
            invoice_date="2026-10-14",
            total_amount=90.5,
        )
        is None
    )


# ── Human-in-the-loop state machine ─────────────────────────────────────────


def test_human_approval_resumes_and_audits_the_workflow(
    factory: SessionFactory,
) -> None:
    outcome = audit_sample(factory, OVER_LIMIT)
    assert outcome.record_id is not None

    updated = apply_human_decision(
        factory,
        outcome.record_id,
        ReviewDecision(
            decision=HumanDecision.APPROVED,
            note="CFO approved the summit travel.",
            reviewer="daiwik",
        ),
    )

    assert updated.status is AuditStatus.APPROVED
    assert updated.human_decision is HumanDecision.APPROVED
    assert updated.reviewer == "daiwik"
    assert updated.reviewer_note == "CFO approved the summit travel."
    assert updated.reviewed_at is not None
    assert updated.trace[-1].kind is TraceKind.HUMAN
    assert updated.trace[-1].inputs["note"] == "CFO approved the summit travel."
    assert updated.trace[-1].outputs["status"] == "APPROVED"


def test_rejection_records_reason(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, UNAPPROVED)
    assert outcome.record_id is not None

    updated = apply_human_decision(
        factory,
        outcome.record_id,
        ReviewDecision(
            decision=HumanDecision.REJECTED,
            note="Blocked vendor; escalate to procurement.",
        ),
    )

    assert updated.status is AuditStatus.REJECTED
    assert updated.reviewer_note == "Blocked vendor; escalate to procurement."


def test_terminal_audits_cannot_be_decided_twice(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, OVER_LIMIT)
    assert outcome.record_id is not None

    apply_human_decision(
        factory,
        outcome.record_id,
        ReviewDecision(decision=HumanDecision.APPROVED),
    )

    with pytest.raises(InvalidStateTransition, match="Allowed transitions: none"):
        apply_human_decision(
            factory,
            outcome.record_id,
            ReviewDecision(decision=HumanDecision.REJECTED),
        )


def test_unknown_audit_id_raises_lookup_error(factory: SessionFactory) -> None:
    with pytest.raises(LookupError):
        apply_human_decision(
            factory, 999_999, ReviewDecision(decision=HumanDecision.APPROVED)
        )


def test_rejected_invoice_does_not_block_a_legitimate_rebill(
    factory: SessionFactory,
) -> None:
    original = audit_sample(factory, CLEAN)
    assert original.record_id is not None
    apply_human_decision(
        factory,
        original.record_id,
        ReviewDecision(decision=HumanDecision.REJECTED, note="Misfiled by AP."),
    )

    resubmission = audit_sample(factory, DUPLICATE)
    assert resubmission.status is AuditStatus.AUTO_APPROVED
    assert "DUPLICATE_INVOICE" not in resubmission.audit_flags


def test_duplicate_flag_points_at_the_original(factory: SessionFactory) -> None:
    original = audit_sample(factory, CLEAN)
    duplicate = audit_sample(factory, DUPLICATE)
    check = next(
        result
        for result in duplicate.policy_results
        if result.check == "duplicate_invoice"
    )
    assert check.passed is False
    assert check.details["matched_record_id"] == original.record_id


def test_status_counts_and_reset(factory: SessionFactory) -> None:
    audit_sample(factory, CLEAN)
    audit_sample(factory, OVER_LIMIT)
    counts = count_by_status(factory)
    assert counts[AuditStatus.AUTO_APPROVED] == 1
    assert counts[AuditStatus.PENDING_REVIEW] == 1
    assert counts[AuditStatus.APPROVED] == 0

    assert reset_ledger(factory) == 2
    assert sum(count_by_status(factory).values()) == 0


# ── Observability ───────────────────────────────────────────────────────────


def test_trace_records_every_guardrail(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, OVER_LIMIT)
    names = [step.name for step in outcome.trace]

    assert names[0] == "audit_started"
    assert "policy.check_budget_limit" in names
    assert "policy.verify_approved_vendor" in names
    assert "policy.check_duplicate_invoice" in names
    assert "policy.check_auto_approve_threshold" in names
    assert "guardrail_evaluation" in names
    assert "routing_decision" in names

    tool_steps = [step for step in outcome.trace if step.kind is TraceKind.TOOL]
    assert len(tool_steps) == 4
    assert all(step.outputs for step in tool_steps)
    assert all(step.duration_ms is not None for step in tool_steps)


def test_trace_collector_writes_json_lines(tmp_path: Path) -> None:
    sink = tmp_path / "traces" / "run.jsonl"
    collector = TraceCollector(sink_path=sink)
    collector.add(
        kind=TraceKind.SYSTEM,
        name="audit_started",
        summary="Unit-test trace",
        inputs={"source": "unit.txt"},
    )
    collector.add(
        kind=TraceKind.ROUTING,
        name="routing_decision",
        summary="Routed to AUTO_APPROVED",
        outputs={"status": "AUTO_APPROVED"},
        duration_ms=1.23456,
    )

    assert [step.index for step in collector.steps] == [0, 1]
    assert collector.steps[-1].duration_ms == pytest.approx(1.23)
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"name": "routing_decision"' in lines[1]


def test_trace_steps_are_serialisable(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, CLEAN)
    payload = [step.model_dump(mode="json") for step in outcome.trace]
    assert all("created_at" in step for step in payload)
    stored = list_audit_records(factory)[0]
    assert isinstance(stored.trace[0], TraceStep)


def test_demo_mode_is_explicit_in_the_outcome(factory: SessionFactory) -> None:
    outcome = audit_sample(factory, CLEAN)
    assert outcome.extraction_mode.value == "heuristic"
    assert outcome.model_name is None
    assert outcome.source == CLEAN.name


def test_policy_file_errors_are_explained(tmp_path: Path) -> None:
    broken = tmp_path / "policies.json"
    broken.write_text("{}", encoding="utf-8")
    with pytest.raises(AuditFlowError, match="categories"):
        tools.load_policies(broken, force_reload=True)
