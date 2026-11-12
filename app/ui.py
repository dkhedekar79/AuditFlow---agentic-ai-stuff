"""Streamlit dashboard for the AuditFlow human-in-the-loop auditor.

Run from the repository root::

    streamlit run app/ui.py

The dashboard has three jobs:

1. accept a receipt (PDF, JSON, pasted text or bundled sample),
2. show what the agent extracted and which deterministic checks failed,
3. hand flagged invoices to a reviewer with one-click approve/reject.

It calls the same :func:`app.agent.run_audit` entry point as the API, so the UI
adds no business logic of its own.
"""

from __future__ import annotations

import html
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# ``streamlit run app/ui.py`` puts the script directory on sys.path, not the
# repository root. Make ``import app`` work from a fresh clone.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

from app.agent import describe_runtime, run_audit  # noqa: E402
from app.database import (  # noqa: E402
    SessionFactory,
    apply_human_decision,
    count_by_status,
    get_session_factory,
    list_audit_records,
    reset_ledger,
)
from app.models import (  # noqa: E402
    AuditFlowError,
    AuditOutcome,
    AuditRecord,
    AuditStatus,
    HumanDecision,
    InvalidStateTransition,
    InvoiceExtractionError,
    PolicyResult,
    ReviewDecision,
    TraceStep,
)
from app.parsing import (  # noqa: E402
    ParsedReceipt,
    ReceiptParseError,
    list_sample_receipts,
    parse_receipt,
    read_receipt_file,
)
from app.tools import get_policy_summary  # noqa: E402

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="AuditFlow · HITL Invoice Auditor",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)

_STATUS_META: dict[AuditStatus, tuple[str, str]] = {
    AuditStatus.PENDING_REVIEW: ("pending", "⏳ Pending review"),
    AuditStatus.AUTO_APPROVED: ("auto", "✅ Auto-approved"),
    AuditStatus.APPROVED: ("approved", "✅ Approved"),
    AuditStatus.REJECTED: ("rejected", "⛔ Rejected"),
}

_SAMPLE_LABELS: dict[str, str] = {
    "01_clean_software_receipt.txt": "Clean software invoice · expect auto-approval",
    "02_over_limit_travel.json": "Over-budget travel · expect human review",
    "03_duplicate_software_receipt.txt": "Duplicate resubmission · run the clean sample first",
    "04_unapproved_vendor_receipt.json": "Blocked vendor · expect human review",
}

_TRACE_ICONS: dict[str, str] = {
    "system": "⚙️",
    "extraction": "📄",
    "llm": "🤖",
    "tool": "🛠️",
    "policy": "🛡️",
    "routing": "🚦",
    "human": "🧑‍⚖️",
}

_CSS = """
<style>
.block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1280px; }
.af-hero {
  border: 1px solid #e2e8f0; border-radius: 20px; padding: 1.4rem 1.6rem;
  background: linear-gradient(120deg, #f0fdfa 0%, #f8fafc 55%, #ecfeff 100%);
  margin-bottom: 1rem;
}
.af-kicker {
  font-size: 0.72rem; letter-spacing: 0.14em; text-transform: uppercase;
  color: #0f766e; font-weight: 700;
}
.af-title { font-size: 2rem; font-weight: 800; color: #0f172a; margin: 0.1rem 0; }
.af-subtitle { color: #475569; font-size: 1rem; margin-bottom: 0.7rem; }
.af-chip {
  display: inline-block; background: #0f766e; color: #ffffff; border-radius: 999px;
  padding: 0.18rem 0.7rem; font-size: 0.75rem; font-weight: 600; margin-right: 0.35rem;
}
.af-chip.muted { background: #e2e8f0; color: #334155; font-weight: 500; }
.af-badge {
  display: inline-block; border-radius: 999px; padding: 0.22rem 0.75rem;
  font-size: 0.8rem; font-weight: 700; letter-spacing: 0.01em;
}
.af-badge.pending { background: #fef3c7; color: #92400e; }
.af-badge.auto { background: #dcfce7; color: #166534; }
.af-badge.approved { background: #dbeafe; color: #1e40af; }
.af-badge.rejected { background: #fee2e2; color: #991b1b; }
.af-empty {
  border: 1px dashed #cbd5e1; border-radius: 16px; padding: 2rem 1.4rem;
  background: #f8fafc; color: #64748b; text-align: center;
}
.af-empty h4 { color: #334155; margin: 0 0 0.4rem 0; }
.af-meta { color: #64748b; font-size: 0.85rem; }
div[data-testid="stMetricValue"] { font-size: 1.35rem; }
</style>
"""


@st.cache_resource(show_spinner=False)
def _ledger() -> SessionFactory:
    """Open (and initialise) the shared SQLite ledger once per session."""

    return get_session_factory()


def _inject_styles() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _status_badge(status: AuditStatus) -> str:
    """Return an HTML badge for a status, using fixed labels only."""

    css_class, label = _STATUS_META[status]
    return f'<span class="af-badge {css_class}">{label}</span>'


def _render_header(runtime: dict[str, object]) -> None:
    """Render the hero banner with the active extraction mode."""

    if runtime["mode"] == "llm":
        mode_label = "LLM mode"
        mode_detail = str(runtime["model_name"])
    else:
        mode_label = "Demo mode"
        mode_detail = "deterministic extractor · no API key needed"

    st.markdown(
        f"""
        <div class="af-hero">
          <div class="af-kicker">Human-in-the-loop · expense audit</div>
          <div class="af-title">🧾 AuditFlow</div>
          <div class="af-subtitle">
            PydanticAI extracts. Deterministic tools decide. You sign off.
          </div>
          <span class="af-chip">{html.escape(mode_label)}</span>
          <span class="af-chip muted">{html.escape(mode_detail)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar(factory: SessionFactory) -> None:
    """Render policy, runtime and ledger controls."""

    runtime = describe_runtime()
    counts = count_by_status(factory)
    total = sum(counts.values())

    with st.sidebar:
        st.markdown("### ⚙️ Runtime")
        if runtime["mode"] == "llm":
            st.success(f"LLM extraction · `{runtime['model_name']}`")
        else:
            st.info("Demo mode · deterministic extractor")
        st.caption(
            "Add `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` to `.env` to switch to "
            "LLM extraction. Routing stays deterministic either way."
        )

        st.divider()
        st.markdown("### 📋 Policy")
        try:
            policy = get_policy_summary()
        except AuditFlowError as exc:
            st.error(str(exc))
            st.stop()

        st.caption(f"{policy['company']} · policy v{policy['version']}")
        st.metric(
            "Auto-approval ceiling",
            f"${policy['auto_approve_threshold']:,.0f}",
            help="Invoices above this total always need a human decision.",
        )
        with st.expander("Category budgets"):
            rows = [
                {
                    "Category": name.replace("_", " ").title(),
                    "Limit": f"${config['budget_limit']:,.0f}",
                }
                for name, config in sorted(policy["categories"].items())
            ]
            st.dataframe(rows, hide_index=True)

        st.divider()
        st.markdown("### 🗂️ Ledger")
        st.metric("Audits stored", total)
        for status in (
            AuditStatus.PENDING_REVIEW,
            AuditStatus.AUTO_APPROVED,
            AuditStatus.APPROVED,
            AuditStatus.REJECTED,
        ):
            st.caption(f"{_STATUS_META[status][1]} · **{counts[status]}**")

        with st.expander("Reset demo ledger"):
            st.caption(
                "Deletes every audit so the four samples can be replayed from a "
                "clean slate. Policy files and receipts are untouched."
            )
            confirmed = st.checkbox(
                "I understand this clears the audit history", key="reset_confirm"
            )
            if st.button(
                "Reset ledger",
                disabled=not confirmed,
                key="reset_ledger_button",
                type="secondary",
            ):
                removed = reset_ledger(factory)
                for key in ("last_outcome", "suite_results", "queue_selection", "trace_selection"):
                    st.session_state.pop(key, None)
                st.toast(f"Removed {removed} audit(s) from the ledger.")
                st.rerun()


def _sample_label(filename: str) -> str:
    """Return a descriptive label for a bundled sample receipt."""

    return _SAMPLE_LABELS.get(filename, filename)


def _resolve_input(
    uploaded: object, pasted: str, sample_path: Path | None
) -> tuple[ParsedReceipt | None, str | None]:
    """Pick the most specific input and parse it, returning an error message."""

    try:
        if pasted and pasted.strip():
            return parse_receipt(pasted, filename="pasted-text.txt"), None
        if uploaded is not None and hasattr(uploaded, "getvalue"):
            return parse_receipt(uploaded.getvalue(), filename=uploaded.name), None
        if sample_path is not None:
            return read_receipt_file(sample_path), None
    except ReceiptParseError as exc:
        return None, str(exc)
    return None, "Add a receipt to audit."


def _render_policy_checks(results: list[PolicyResult]) -> None:
    """Render each deterministic guardrail with its raw payload."""

    for result in results:
        icon = "✅" if result.passed else "❌"
        with st.expander(
            f"{icon} {result.title} · {result.status_label}",
            expanded=not result.passed,
        ):
            st.write(result.message)
            if result.policy_reference:
                st.caption(f"Policy reference · `{result.policy_reference}`")
            st.json(result.details, expanded=False)


def _render_trace(steps: list[TraceStep], *, empty_message: str) -> None:
    """Render the reasoning trace as expandable, inspectable steps."""

    if not steps:
        st.caption(empty_message)
        return
    for step in steps:
        icon = _TRACE_ICONS.get(step.kind.value, "•")
        expanded = step.kind.value in {"routing", "human"}
        with st.expander(
            f"{icon} {step.index + 1}. {step.name} · {step.kind.value}",
            expanded=expanded,
        ):
            st.write(step.summary)
            meta: list[str] = []
            if step.duration_ms is not None:
                meta.append(f"{step.duration_ms:.2f} ms")
            meta.append(step.created_at.strftime("%H:%M:%S UTC"))
            st.caption(" · ".join(meta))

            input_col, output_col = st.columns(2)
            if step.inputs:
                with input_col:
                    st.caption("Inputs")
                    st.json(step.inputs, expanded=False)
            if step.outputs:
                with output_col:
                    st.caption("Outputs")
                    st.json(step.outputs, expanded=False)


def _render_last_outcome(outcome: AuditOutcome | None) -> None:
    """Render the most recent audit result in the upload tab."""

    if outcome is None:
        st.markdown(
            """
            <div class="af-empty">
              <h4>Your audit result appears here</h4>
              <p>Pick a bundled sample (or upload your own receipt) and press
              <strong>Run audit</strong>.</p>
              <p>Clean invoices auto-approve. Flagged ones move to the review
              queue with every policy result attached.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    invoice = outcome.invoice
    st.markdown(_status_badge(outcome.status), unsafe_allow_html=True)
    st.markdown(f"#### {html.escape(invoice.vendor)}")
    st.markdown(
        f"**{invoice.currency} {invoice.total_amount:,.2f}** "
        f"· {invoice.date.isoformat()} "
        f"· {invoice.category.replace('_', ' ').title()}"
    )
    source = html.escape(outcome.source)
    audit_id = f"#{outcome.record_id}" if outcome.record_id else "not persisted"
    st.markdown(
        f'<span class="af-meta">Audit {audit_id} · {source} · '
        f"{outcome.extraction_mode.value} extraction · "
        f"{invoice.confidence_score:.0%} confidence</span>",
        unsafe_allow_html=True,
    )

    st.write("")
    if outcome.audit_flags:
        st.warning(
            "**Human review required.** Flags: "
            + ", ".join(f"`{flag}`" for flag in outcome.audit_flags),
            icon="⚠️",
        )
    else:
        st.success(
            "All deterministic checks passed and the total is inside the "
            "auto-approval ceiling.",
            icon="✅",
        )

    st.markdown("##### Deterministic checks")
    _render_policy_checks(outcome.policy_results)

    with st.expander("Extracted invoice (JSON)"):
        st.json(invoice.model_dump(mode="json"), expanded=False)


def _render_upload_tab(factory: SessionFactory) -> None:
    """Render receipt input and the latest audit summary."""

    st.subheader("Upload, paste or test a sample")
    st.caption(
        "Priority: pasted text → uploaded file → bundled sample. "
        "The duplicate sample is detected only after the clean sample exists in "
        "the ledger."
    )

    samples = list_sample_receipts()
    samples_by_name = {path.name: path for path in samples}
    sample_names = sorted(samples_by_name)
    selected_name = st.session_state.get("sample_choice", sample_names[0] if sample_names else "")
    if selected_name not in samples_by_name and sample_names:
        selected_name = sample_names[0]

    left, right = st.columns([1.05, 1.0], gap="large")

    with left:
        if sample_names:
            choice = st.selectbox(
                "Bundled sample receipt",
                options=sample_names,
                index=sample_names.index(selected_name),
                format_func=lambda name: f"{_sample_label(name)} — {name}",
                key="sample_choice",
            )
            st.caption(f"`data/sample_receipts/{choice}`")

        uploaded = st.file_uploader(
            "Upload a receipt",
            type=["pdf", "txt", "json", "md"],
            help="PDFs are read with pypdf/pdfplumber. Text and JSON work offline.",
        )
        pasted = st.text_area(
            "…or paste receipt text",
            height=170,
            placeholder="Vendor: Northwind Cloud Services\nDate: 2026-10-14\nTotal: 89.00",
            key="paste_text",
        )
        run_clicked = st.button(
            "🚀 Run audit", type="primary", key="run_audit_button"
        )

    if run_clicked:
        parsed, error = _resolve_input(
            uploaded, pasted, samples_by_name.get(choice) if sample_names else None
        )
        if error is not None:
            st.error(error)
        elif parsed is not None:
            try:
                with st.spinner("Extracting, checking policy and routing…"):
                    outcome = run_audit(
                        parsed.text,
                        source=parsed.source,
                        session_factory=factory,
                    )
            except InvoiceExtractionError as exc:
                st.error(f"Extraction failed: {exc}")
            except AuditFlowError as exc:
                st.error(str(exc))
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                logger.exception("Unhandled audit failure")
                st.error(f"Unexpected error while auditing this receipt: {exc}")
            else:
                st.session_state["last_outcome"] = outcome
                st.toast(f"Routed to {outcome.status.value}")

    with right:
        _render_last_outcome(st.session_state.get("last_outcome"))

    _render_demo_suite(factory, samples)


def _render_demo_suite(factory: SessionFactory, samples: list[Path]) -> None:
    """Run all four receipts in the documented order for a repeatable demo."""

    st.write("")
    with st.expander("▶️ Run the four-receipt evaluation in order"):
        st.caption(
            "Runs clean → over-limit → duplicate → unapproved vendor. Reset the "
            "ledger first if you want the canonical four outcomes; otherwise the "
            "duplicate detector may flag repeats of earlier receipts."
        )
        if st.button("Run all samples", key="run_all_samples"):
            progress = st.progress(0.0, text="Starting evaluation…")
            results: list[AuditOutcome] = []
            try:
                for index, path in enumerate(samples, start=1):
                    parsed = read_receipt_file(path)
                    results.append(
                        run_audit(
                            parsed.text,
                            source=path.name,
                            session_factory=factory,
                        )
                    )
                    progress.progress(
                        index / max(len(samples), 1), text=f"Audited {path.name}"
                    )
            except (AuditFlowError, ReceiptParseError) as exc:
                st.error(str(exc))
            else:
                st.session_state["suite_results"] = results
                st.session_state["last_outcome"] = results[-1] if results else None
                st.toast("Evaluation complete.")
            finally:
                progress.empty()

        suite: list[AuditOutcome] = st.session_state.get("suite_results", [])
        if suite:
            st.dataframe(
                [
                    {
                        "Receipt": outcome.source,
                        "Status": outcome.status.value,
                        "Flags": ", ".join(outcome.audit_flags) or "—",
                        "Amount": f"{outcome.invoice.currency} "
                        f"{outcome.invoice.total_amount:,.2f}",
                        "Audit": f"#{outcome.record_id}",
                    }
                    for outcome in suite
                ],
                hide_index=True,
            )


def _format_timestamp(value: datetime | None) -> str:
    """Format an audit timestamp for display."""

    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _render_audit_identity(record: AuditRecord) -> None:
    """Render the vendor, amount, status and provenance of an audit."""

    invoice = record.invoice
    st.markdown(_status_badge(record.status), unsafe_allow_html=True)
    st.markdown(f"### {html.escape(invoice.vendor)}")
    st.markdown(
        f"**{invoice.currency} {invoice.total_amount:,.2f}** · "
        f"{invoice.date.isoformat()} · "
        f"{invoice.category.replace('_', ' ').title()}"
    )
    st.markdown(
        f'<span class="af-meta">Audit #{record.id} · '
        f"{html.escape(record.source)} · {record.extraction_mode.value} extraction · "
        f"{_format_timestamp(record.created_at)}</span>",
        unsafe_allow_html=True,
    )
    if record.audit_flags:
        st.warning(
            "Flags: " + ", ".join(f"`{flag}`" for flag in record.audit_flags),
            icon="⚠️",
        )
    if record.human_decision:
        note = (
            f" — \u201c{record.reviewer_note}\u201d" if record.reviewer_note else ""
        )
        st.info(
            f"**{record.human_decision.value.title()}** by "
            f"{record.reviewer or 'reviewer'} on "
            f"{_format_timestamp(record.reviewed_at)}{note}",
            icon="🧑‍⚖️",
        )


def _render_review_queue(factory: SessionFactory) -> None:
    """Render the human-in-the-loop approval queue."""

    st.subheader("Human review queue")
    pending = list_audit_records(
        factory, status=AuditStatus.PENDING_REVIEW, limit=200
    )

    if not pending:
        st.success(
            "No invoices are waiting for review. Run the over-limit, duplicate "
            "or blocked-vendor sample to populate the queue.",
            icon="🎉",
        )
        return

    st.caption(
        f"{len(pending)} invoice(s) are paused mid-workflow. Extraction and policy "
        "results are frozen until you approve or reject them."
    )

    labels = {
        f"#{record.id} · {record.invoice.vendor} · "
        f"{record.invoice.currency} {record.invoice.total_amount:,.2f}": record
        for record in pending
    }
    selected = st.selectbox("Flagged invoice", options=list(labels))
    record = labels[selected]

    invoice_col, decision_col = st.columns([1.0, 1.05], gap="large")

    with invoice_col:
        _render_audit_identity(record)
        st.markdown("##### Extracted invoice")
        st.json(record.invoice.model_dump(mode="json"), expanded=False)
        st.markdown("##### Deterministic checks")
        _render_policy_checks(record.policy_results)

    with decision_col:
        st.markdown("##### Reasoning trace")
        _render_trace(record.trace, empty_message="No trace was recorded.")

        st.divider()
        st.markdown("##### Record your decision")
        st.caption(
            "Approving resumes the workflow; rejecting closes it. Either way the "
            "decision is appended to the audit trace and cannot be changed again."
        )
        note = st.text_area(
            "Reviewer note (optional)",
            key=f"decision_note_{record.id}",
            height=90,
            placeholder="e.g. Manager pre-approval confirmed for the Q4 summit.",
        )
        reviewer = st.text_input(
            "Reviewer",
            value="human-reviewer",
            key=f"reviewer_{record.id}",
            max_chars=120,
        )

        approve_col, reject_col = st.columns(2)
        approve = approve_col.button(
            "✅ Approve",
            type="primary",
            key=f"approve_{record.id}",
        )
        reject = reject_col.button("⛔ Reject", key=f"reject_{record.id}")

        if approve or reject:
            clean_reviewer = reviewer.strip() or "human-reviewer"
            if len(clean_reviewer) < 2:
                st.error("Reviewer must be at least two characters.")
            else:
                decision = ReviewDecision(
                    decision=(
                        HumanDecision.APPROVED if approve else HumanDecision.REJECTED
                    ),
                    note=note.strip() or None,
                    reviewer=clean_reviewer,
                )
                try:
                    updated = apply_human_decision(factory, record.id, decision)
                except LookupError as exc:
                    st.error(str(exc))
                except InvalidStateTransition as exc:
                    st.error(str(exc))
                else:
                    st.toast(
                        f"Audit #{record.id} moved to {updated.status.value}.",
                        icon="✅" if approve else "⛔",
                    )
                    st.rerun()


def _render_trace_inspector(factory: SessionFactory) -> None:
    """Render the audit trace inspector for any stored record."""

    st.subheader("Audit trace inspector")
    records = list_audit_records(factory, limit=200)
    if not records:
        st.info(
            "No audits yet. Run a receipt from the Upload tab and its full "
            "reasoning trace will appear here.",
            icon="🔍",
        )
        return

    labels = {
        f"#{record.id} · {record.status.value} · {record.invoice.vendor} · "
        f"{record.invoice.currency} {record.invoice.total_amount:,.2f}": record
        for record in records
    }
    selected = st.selectbox("Audit record", options=list(labels))
    record = labels[selected]

    _render_audit_identity(record)

    failed = [result for result in record.policy_results if not result.passed]
    metric_cols = st.columns(4)
    metric_cols[0].metric(
        "Total", f"{record.invoice.currency} {record.invoice.total_amount:,.2f}"
    )
    metric_cols[1].metric(
        "Extraction confidence", f"{record.invoice.confidence_score:.0%}"
    )
    metric_cols[2].metric("Failed checks", len(failed))
    metric_cols[3].metric("Trace steps", len(record.trace))

    st.download_button(
        "⬇️ Download audit JSON",
        data=json.dumps(record.model_dump(mode="json"), indent=2),
        file_name=f"auditflow-audit-{record.id}.json",
        mime="application/json",
        key=f"download_audit_{record.id}",
    )

    trace_tab, checks_tab, invoice_tab, raw_tab = st.tabs(
        ["🧠 Trace timeline", "🛡️ Policy results", "📄 Extracted invoice", "🧾 Raw record"]
    )
    with trace_tab:
        st.caption(
            "Every step the workflow executed, in order. Tool outputs are the "
            "exact deterministic payloads used for routing."
        )
        _render_trace(record.trace, empty_message="No trace was recorded.")
    with checks_tab:
        _render_policy_checks(record.policy_results)
    with invoice_tab:
        st.json(record.invoice.model_dump(mode="json"), expanded=False)
    with raw_tab:
        st.json(record.model_dump(mode="json"), expanded=False)


def main() -> None:
    """Compose the dashboard."""

    _inject_styles()
    try:
        factory = _ledger()
    except Exception as exc:  # pragma: no cover - startup boundary
        st.error(f"Could not open the AuditFlow ledger: {exc}")
        st.stop()

    counts = count_by_status(factory)
    _render_sidebar(factory)
    _render_header(describe_runtime())

    upload_tab, queue_tab, trace_tab = st.tabs(
        [
            "📥 Upload / Test Sample",
            f"🧑‍⚖️ Human Review Queue ({counts[AuditStatus.PENDING_REVIEW]})",
            "🔍 Audit Trace Inspector",
        ]
    )
    with upload_tab:
        _render_upload_tab(factory)
    with queue_tab:
        _render_review_queue(factory)
    with trace_tab:
        _render_trace_inspector(factory)


if __name__ == "__main__":
    main()
