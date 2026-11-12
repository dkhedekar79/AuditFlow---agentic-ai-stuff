"""Streamlit dashboard for the AuditFlow human-in-the-loop auditor.

Run from the repository root::

    streamlit run app/ui.py

The interface is deliberately quiet: one product statement, one receipt, one
decision. All business logic lives in :func:`app.agent.run_audit`; this module is
presentation only.
"""

from __future__ import annotations

import html
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from textwrap import dedent

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

GITHUB_URL = "https://github.com/dkhedekar79/AuditFlow---agentic-ai-stuff"

st.set_page_config(
    page_title="AuditFlow",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_STATUS_META: dict[AuditStatus, tuple[str, str]] = {
    AuditStatus.PENDING_REVIEW: ("pending", "Needs review"),
    AuditStatus.AUTO_APPROVED: ("auto", "Auto-approved"),
    AuditStatus.APPROVED: ("approved", "Approved"),
    AuditStatus.REJECTED: ("rejected", "Rejected"),
}

_SAMPLE_LABELS: dict[str, str] = {
    "01_clean_software_receipt.txt": "Clean software invoice",
    "02_over_limit_travel.json": "Travel over budget",
    "03_duplicate_software_receipt.txt": "Duplicate submission",
    "04_unapproved_vendor_receipt.json": "Blocked vendor",
}

_CSS = """
<style>
:root {
  --af-ink: #1d1d1f;
  --af-muted: #6e6e73;
  --af-faint: #86868b;
  --af-surface: #f5f5f7;
  --af-line: #d2d2d7;
  --af-blue: #0071e3;
  --af-green: #248a3d;
  --af-orange: #b25000;
  --af-red: #d70015;
}
html, body, .stApp, [class*="st-"] {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
    "Helvetica Neue", Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.stApp { background: #ffffff; color: var(--af-ink); }
header[data-testid="stHeader"], #MainMenu, footer,
[data-testid="stToolbar"], [data-testid="stDecoration"] {
  display: none !important;
}
.block-container { max-width: 1040px; padding: 0 28px 88px; }
@media (max-width: 734px) { .block-container { padding: 0 20px 64px; } }

/* ── Navigation ─────────────────────────────────────────── */
.af-nav {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 0 16px; border-bottom: 1px solid var(--af-line);
  font-size: 12px;
}
.af-wordmark { font-size: 17px; font-weight: 600; letter-spacing: -0.02em; }
.af-nav-right { display: flex; align-items: center; gap: 22px; color: var(--af-faint); }
.af-nav a { color: var(--af-ink); text-decoration: none; }
.af-nav a:hover { color: var(--af-blue); }
.af-nav-mode { display: inline-flex; align-items: center; gap: 7px; color: var(--af-faint); }
.af-nav-mode .af-dot { background: var(--af-faint); }

/* ── Hero ───────────────────────────────────────────────── */
.af-hero { padding: 76px 0 44px; max-width: 840px; }
.af-eyebrow {
  font-size: 11px; font-weight: 600; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--af-faint); margin: 0 0 20px;
}
.af-hero h1 {
  font-size: clamp(44px, 6.2vw, 76px); line-height: 1.04;
  letter-spacing: -0.035em; font-weight: 600; margin: 0 0 24px;
}
.af-lede {
  font-size: clamp(18px, 2.1vw, 23px); line-height: 1.42;
  letter-spacing: -0.01em; color: var(--af-muted); max-width: 700px;
  margin: 0 0 26px;
}
.af-actions { display: flex; gap: 26px; align-items: center; font-size: 16px; }
.af-link { color: var(--af-blue); text-decoration: none; font-weight: 500; }
.af-link:hover { text-decoration: underline; }
.af-link.secondary { color: var(--af-muted); font-weight: 400; }

/* ── Product stage ─────────────────────────────────────── */
.af-stage {
  background: var(--af-surface); border-radius: 28px; padding: 44px 28px;
  display: flex; justify-content: center; margin: 0 0 52px;
}
.af-receipt {
  background: #ffffff; width: 410px; max-width: 100%; border-radius: 18px;
  padding: 24px 26px; box-shadow: 0 30px 60px rgba(0, 0, 0, 0.08);
}
.af-receipt-top {
  display: flex; justify-content: space-between; font-size: 10px;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--af-faint);
}
.af-receipt-vendor { font-size: 21px; font-weight: 600; letter-spacing: -0.02em; margin: 20px 0 2px; }
.af-receipt-amount { font-size: 33px; font-weight: 600; letter-spacing: -0.03em; margin: 0 0 18px; }
.af-receipt-rule { height: 1px; background: var(--af-line); margin-bottom: 10px; }
.af-receipt-row {
  display: flex; justify-content: space-between; font-size: 13px;
  color: var(--af-muted); padding: 6px 0;
}
.af-receipt-row strong { color: var(--af-ink); font-weight: 500; }
.af-receipt-row .over { color: var(--af-orange); }
.af-receipt-status {
  display: flex; align-items: center; gap: 8px; font-size: 13px;
  font-weight: 500; color: var(--af-orange); margin-top: 16px;
}

/* ── Segmented tabs ────────────────────────────────────── */
div[data-baseweb="tab-list"] {
  background: var(--af-surface); border-radius: 999px; padding: 4px;
  gap: 0; width: max-content; max-width: 100%; margin: 0 0 44px;
}
button[data-baseweb="tab"] {
  border-radius: 999px; padding: 8px 20px; font-size: 13px;
  font-weight: 500; color: var(--af-muted); background: transparent;
}
button[data-baseweb="tab"][aria-selected="true"] {
  background: #ffffff; color: var(--af-ink);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}
div[data-baseweb="tab-highlight"], div[data-baseweb="tab-border"] { display: none; }

/* ── Sections and type ─────────────────────────────────── */
.af-section-title { font-size: clamp(27px, 3.2vw, 38px); font-weight: 600; letter-spacing: -0.025em; margin: 0 0 10px; }
.af-section-copy { font-size: 15px; line-height: 1.5; color: var(--af-muted); max-width: 620px; margin: 0 0 28px; }
.af-label { font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--af-faint); margin: 0 0 10px; }
.af-meta { font-size: 12px; color: var(--af-faint); }
[data-testid="stCaptionContainer"] p { font-size: 12px; color: var(--af-faint); }
label p, [data-testid="stWidgetLabel"] p { font-size: 13px; font-weight: 500; color: var(--af-ink); }
h2, h3, h4 { letter-spacing: -0.02em; }

/* ── Inputs ────────────────────────────────────────────── */
[data-baseweb="select"] > div {
  border-radius: 12px !important; border-color: var(--af-line) !important;
  background: #ffffff !important; min-height: 44px;
}
[data-testid="stFileUploaderDropzone"] {
  border: 1px dashed var(--af-line); border-radius: 18px;
  background: var(--af-surface); padding: 20px;
}
[data-testid="stFileUploaderDropzone"] button {
  border-radius: 999px; border: 1px solid var(--af-line);
  background: #ffffff; color: var(--af-ink); font-size: 13px;
}
textarea, input[type="text"] {
  border-radius: 14px !important; border-color: var(--af-line) !important;
  background: #ffffff !important; font-size: 14px !important;
}
.stButton > button, .stDownloadButton > button {
  border-radius: 999px; border: 1px solid var(--af-line); background: #ffffff;
  color: var(--af-ink); font-size: 14px; font-weight: 500; padding: 9px 20px;
}
.stButton > button:hover, .stDownloadButton > button:hover {
  border-color: var(--af-faint); background: #ffffff; color: var(--af-ink);
}
.stButton > button[kind="primary"], [data-testid="baseButton-primary"] {
  background: var(--af-blue); border-color: var(--af-blue); color: #ffffff;
}
.stButton > button[kind="primary"]:hover {
  background: #0077ed; border-color: #0077ed; color: #ffffff;
}
[data-testid="stExpander"] {
  border: 1px solid var(--af-line); border-radius: 14px;
  background: #ffffff; margin-bottom: 8px;
}
[data-testid="stExpander"] summary { font-size: 14px; font-weight: 500; padding: 13px 16px; }
[data-testid="stExpander"] summary:hover { color: var(--af-blue); }
[data-testid="stAlert"] {
  border-radius: 14px; border: 1px solid var(--af-line);
  background: var(--af-surface); color: var(--af-ink);
}
div[data-testid="stMetricValue"] { font-size: 27px; font-weight: 600; letter-spacing: -0.02em; }
label[data-testid="stMetricLabel"] p {
  font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--af-faint);
}
[data-testid="stDataFrame"] { border: 1px solid var(--af-line); border-radius: 12px; overflow: hidden; }

/* ── Status and checks ─────────────────────────────────── */
.af-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.af-status {
  display: inline-flex; align-items: center; gap: 8px; font-size: 11px;
  font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
}
.af-status.pending { color: var(--af-orange); }
.af-status.auto { color: var(--af-green); }
.af-status.approved { color: var(--af-blue); }
.af-status.rejected { color: var(--af-red); }
.af-checks { border-top: 1px solid var(--af-line); margin-top: 6px; }
.af-check { display: flex; gap: 13px; align-items: flex-start; padding: 15px 0; border-bottom: 1px solid var(--af-line); }
.af-check .af-dot { margin-top: 7px; }
.af-check .af-dot.pass { color: var(--af-green); }
.af-check .af-dot.fail { color: var(--af-red); }
.af-check-copy { flex: 1; }
.af-check-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 3px; }
.af-check-title { font-size: 14px; font-weight: 600; margin: 0; }
.af-check-state { font-size: 10px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--af-faint); }
.af-check-state.fail { color: var(--af-red); }
.af-check-message { font-size: 13px; line-height: 1.45; color: var(--af-muted); margin: 0; }

/* ── Result panel ──────────────────────────────────────── */
.af-result-vendor { font-size: 25px; font-weight: 600; letter-spacing: -0.02em; margin: 14px 0 2px; }
.af-result-amount { font-size: 31px; font-weight: 600; letter-spacing: -0.03em; margin: 0 0 6px; }
.af-note {
  border-left: 2px solid var(--af-line); padding: 2px 0 2px 14px;
  font-size: 14px; line-height: 1.5; color: var(--af-muted); margin: 18px 0;
}
.af-note.warn { border-color: #ff9f0a; color: var(--af-ink); }
.af-empty {
  border: 1px solid var(--af-line); border-radius: 20px;
  padding: 58px 28px; text-align: center; color: var(--af-muted);
}
.af-empty h3 { font-size: 21px; font-weight: 600; color: var(--af-ink); margin: 0 0 8px; }
.af-empty p { font-size: 14px; line-height: 1.5; margin: 0; }

/* ── Footer ────────────────────────────────────────────── */
.af-footer {
  border-top: 1px solid var(--af-line); margin-top: 64px; padding-top: 20px;
  display: flex; justify-content: space-between; gap: 18px;
  font-size: 12px; color: var(--af-faint);
}
.af-footer a { color: var(--af-muted); text-decoration: none; }
.af-footer a:hover { color: var(--af-blue); }
@media (max-width: 734px) {
  .af-nav-right { gap: 14px; }
  .af-nav-mode { display: none; }
  .af-footer { flex-direction: column; }
  .af-stage { padding: 28px 16px; }
}
</style>
"""


@st.cache_resource(show_spinner=False)
def _ledger() -> SessionFactory:
    """Open (and initialise) the shared SQLite ledger once per session."""

    return get_session_factory()


def _inject_styles() -> None:
    """Load the design system once per rerun."""

    st.markdown(_CSS, unsafe_allow_html=True)


def _html(markup: str) -> None:
    """Render trusted, dedented HTML without Markdown's code-block rules."""

    st.markdown(dedent(markup).strip(), unsafe_allow_html=True)


def _status_badge(status: AuditStatus) -> str:
    """Return a typographic status indicator (no icon fonts, no emoji)."""

    css_class, label = _STATUS_META[status]
    return f'<span class="af-status {css_class}"><span class="af-dot"></span>{label}</span>'


def _render_nav(runtime: dict[str, object]) -> None:
    """Render the thin product navigation bar."""

    mode = (
        "Demo mode"
        if runtime["mode"] == "demo"
        else html.escape(str(runtime["model_name"]))
    )
    _html(
        f"""
        <nav class="af-nav">
          <span class="af-wordmark">AuditFlow</span>
          <span class="af-nav-right">
            <span class="af-nav-mode"><span class="af-dot"></span>{mode}</span>
            <a href="{GITHUB_URL}" target="_blank" rel="noreferrer">GitHub</a>
          </span>
        </nav>
        """
    )


def _render_hero() -> None:
    """Render the product statement."""

    _html(
        f"""
        <section class="af-hero">
          <p class="af-eyebrow">Human-in-the-loop expense audit</p>
          <h1>Expense audits,<br/>with a human in the loop.</h1>
          <p class="af-lede">
            AuditFlow reads a receipt, enforces company policy in Python,
            and stops for review when the numbers need a person.
          </p>
          <div class="af-actions">
            <a class="af-link" href="{GITHUB_URL}" target="_blank" rel="noreferrer">View the source</a>
            <a class="af-link secondary" href="{GITHUB_URL}#readme" target="_blank" rel="noreferrer">Read the documentation</a>
          </div>
        </section>
        """
    )


def _render_stage() -> None:
    """Render a quiet product shot of a paused audit."""

    _html(
        """
        <section class="af-stage" aria-label="Example audited invoice">
          <div class="af-receipt">
            <div class="af-receipt-top"><span>Invoice</span><span>CTG-88213</span></div>
            <p class="af-receipt-vendor">Contoso Travel Group</p>
            <p class="af-receipt-amount">$1,249.00</p>
            <div class="af-receipt-rule"></div>
            <div class="af-receipt-row"><span>Category</span><strong>Travel</strong></div>
            <div class="af-receipt-row"><span>Category budget</span><strong class="over">$449 over</strong></div>
            <div class="af-receipt-row"><span>Approved vendor</span><strong>Yes</strong></div>
            <div class="af-receipt-status"><span class="af-dot"></span>Needs review</div>
          </div>
        </section>
        """
    )


def _section(title: str, copy: str) -> None:
    """Render a section heading in the page's voice."""

    _html(
        f"""
        <p class="af-section-title">{html.escape(title)}</p>
        <p class="af-section-copy">{html.escape(copy)}</p>
        """
    )


def _sample_label(filename: str) -> str:
    """Return a short, non-promotional label for a bundled sample."""

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
    """Render the deterministic checks as a plain, scannable list."""

    rows: list[str] = []
    for result in results:
        state = "pass" if result.passed else "fail"
        label = "Passed" if result.passed else "Failed"
        rows.append(
            '<div class="af-check">'
            f'<span class="af-dot {state}"></span>'
            '<div class="af-check-copy">'
            '<div class="af-check-head">'
            f'<p class="af-check-title">{html.escape(result.title)}</p>'
            f'<span class="af-check-state {state}">{label}</span>'
            "</div>"
            f'<p class="af-check-message">{html.escape(result.message)}</p>'
            "</div></div>"
        )
    _html(f'<div class="af-checks">{"".join(rows)}</div>')


def _render_trace(steps: list[TraceStep], *, empty_message: str) -> None:
    """Render the execution trace as numbered, inspectable rows."""

    if not steps:
        st.caption(empty_message)
        return
    for step in steps:
        expanded = step.kind.value in {"routing", "human"}
        with st.expander(
            f"{step.index + 1:02d}   {step.name}",
            expanded=expanded,
        ):
            meta = [
                step.kind.value.upper(),
                step.created_at.strftime("%H:%M:%S UTC"),
            ]
            if step.duration_ms is not None:
                meta.insert(1, f"{step.duration_ms:.1f} ms")
            st.markdown(
                f'<span class="af-label">{" · ".join(meta)}</span>',
                unsafe_allow_html=True,
            )
            st.write(step.summary)
            input_col, output_col = st.columns(2)
            if step.inputs:
                with input_col:
                    st.caption("Input")
                    st.json(step.inputs, expanded=False)
            if step.outputs:
                with output_col:
                    st.caption("Output")
                    st.json(step.outputs, expanded=False)


def _render_last_outcome(outcome: AuditOutcome | None) -> None:
    """Render the latest audit result."""

    if outcome is None:
        _html(
            """
            <div class="af-empty">
              <h3>No audit selected</h3>
              <p>Choose a sample, upload a receipt or paste text, then run the audit.<br/>
              Clean invoices settle automatically. Everything else stops here for a decision.</p>
            </div>
            """
        )
        return

    invoice = outcome.invoice
    audit_id = f"#{outcome.record_id}" if outcome.record_id else "not stored"
    _html(
        f"""
        <div>
          <p class="af-label">Latest audit</p>
          {_status_badge(outcome.status)}
          <p class="af-result-vendor">{html.escape(invoice.vendor)}</p>
          <p class="af-result-amount">{invoice.currency} {invoice.total_amount:,.2f}</p>
          <p class="af-meta">
            {invoice.date.isoformat()} · {invoice.category.replace('_', ' ').title()}
            · {invoice.confidence_score:.0%} confidence
            · audit {audit_id} · {html.escape(outcome.source)}
          </p>
        </div>
        """
    )

    if outcome.audit_flags:
        flags = ", ".join(html.escape(flag.replace("_", " ").lower()) for flag in outcome.audit_flags)
        _html(
            f'<p class="af-note warn"><strong>Needs a human decision.</strong> {flags}.</p>'
        )
    else:
        _html(
            '<p class="af-note">All four checks passed and the total is inside '
            "the auto-approval ceiling.</p>"
        )

    _html('<p class="af-label">Checks</p>')
    _render_policy_checks(outcome.policy_results)
    with st.expander("Invoice data"):
        st.json(invoice.model_dump(mode="json"), expanded=False)


def _render_upload_tab(factory: SessionFactory) -> None:
    """Render receipt input and the latest audit summary."""

    _section(
        "Audit a receipt",
        "Paste text, upload a PDF, or start with a bundled sample. "
        "Pasted text takes priority over an upload, which takes priority over the sample.",
    )

    samples = list_sample_receipts()
    samples_by_name = {path.name: path for path in samples}
    sample_names = sorted(samples_by_name)
    selected_name = st.session_state.get(
        "sample_choice", sample_names[0] if sample_names else ""
    )
    if selected_name not in samples_by_name and sample_names:
        selected_name = sample_names[0]

    input_col, result_col = st.columns([1.05, 1.0], gap="large")

    with input_col:
        if sample_names:
            choice = st.selectbox(
                "Sample",
                options=sample_names,
                index=sample_names.index(selected_name),
                format_func=lambda name: f"{_sample_label(name)} — {name}",
                key="sample_choice",
            )
            st.caption(f"data/sample_receipts/{choice}")

        uploaded = st.file_uploader(
            "Receipt file",
            type=["pdf", "txt", "json", "md"],
            help="Text-based PDFs, plain text and JSON. Scanned images need OCR first.",
        )
        pasted = st.text_area(
            "Or paste receipt text",
            height=150,
            placeholder="Vendor: Northwind Cloud Services\nDate: 2026-10-14\nTotal: 89.00",
            key="paste_text",
        )
        run_clicked = st.button("Run audit", type="primary", key="run_audit_button")

    if run_clicked:
        parsed, error = _resolve_input(
            uploaded, pasted, samples_by_name.get(choice) if sample_names else None
        )
        if error is not None:
            st.error(error)
        elif parsed is not None:
            try:
                with st.spinner("Extracting and checking policy…"):
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

    with result_col:
        _render_last_outcome(st.session_state.get("last_outcome"))

    _render_demo_suite(factory, samples)


def _render_demo_suite(factory: SessionFactory, samples: list[Path]) -> None:
    """Run all four receipts in their documented order."""

    with st.expander("Run all four samples"):
        st.caption(
            "Clean, over-limit, duplicate, blocked vendor. Reset the ledger first for "
            "the canonical outcomes; otherwise repeat receipts are flagged as duplicates."
        )
        if st.button("Run sample set", key="run_all_samples"):
            progress = st.progress(0.0, text="Starting…")
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
    """Render the identity and provenance of a stored audit."""

    invoice = record.invoice
    _html(
        f"""
        <div>
          {_status_badge(record.status)}
          <p class="af-result-vendor">{html.escape(invoice.vendor)}</p>
          <p class="af-result-amount">{invoice.currency} {invoice.total_amount:,.2f}</p>
          <p class="af-meta">
            {invoice.date.isoformat()} · {invoice.category.replace('_', ' ').title()}
            · {record.extraction_mode.value} extraction
            · audit #{record.id} · {html.escape(record.source)}
            · {_format_timestamp(record.created_at)}
          </p>
        </div>
        """
    )
    if record.audit_flags:
        flags = ", ".join(html.escape(flag.replace("_", " ").lower()) for flag in record.audit_flags)
        _html(f'<p class="af-note warn"><strong>Flags.</strong> {flags}.</p>')
    if record.human_decision:
        note = f" — “{html.escape(record.reviewer_note)}”" if record.reviewer_note else ""
        _html(
            f'<p class="af-note"><strong>{record.human_decision.value.title()}</strong> '
            f"by {html.escape(record.reviewer or 'reviewer')} on "
            f"{_format_timestamp(record.reviewed_at)}{note}</p>"
        )


def _render_review_queue(factory: SessionFactory) -> None:
    """Render the human review queue."""

    _section(
        "Review queue",
        "Invoices that failed a check or exceeded the approval ceiling. "
        "The agent is paused; your decision resumes or closes the workflow.",
    )

    pending = list_audit_records(factory, status=AuditStatus.PENDING_REVIEW, limit=200)
    if not pending:
        _html(
            """
            <div class="af-empty">
              <h3>Nothing to review</h3>
              <p>Every invoice in the ledger is settled.<br/>
              Run the over-limit, duplicate or blocked-vendor sample to create a review.</p>
            </div>
            """
        )
        return

    labels = {
        f"#{record.id} · {record.invoice.vendor} · "
        f"{record.invoice.currency} {record.invoice.total_amount:,.2f}": record
        for record in pending
    }
    selected = st.selectbox("Invoice", options=list(labels))
    record = labels[selected]

    detail_col, decision_col = st.columns([1.0, 1.05], gap="large")

    with detail_col:
        _render_audit_identity(record)
        _html('<p class="af-label" style="margin-top:26px">Checks</p>')
        _render_policy_checks(record.policy_results)
        with st.expander("Invoice data"):
            st.json(record.invoice.model_dump(mode="json"), expanded=False)

    with decision_col:
        _html('<p class="af-label">Trace</p>')
        _render_trace(record.trace, empty_message="No trace was recorded.")

        _html('<p class="af-label" style="margin-top:26px">Decision</p>')
        st.caption(
            "Approving resumes the workflow. Rejecting closes it. "
            "Either way the decision is appended to the audit trace and cannot be changed again."
        )
        note = st.text_area(
            "Note (optional)",
            key=f"decision_note_{record.id}",
            height=84,
            placeholder="Manager pre-approval confirmed.",
        )
        reviewer = st.text_input(
            "Reviewer",
            value="human-reviewer",
            key=f"reviewer_{record.id}",
            max_chars=120,
        )

        approve_col, reject_col = st.columns(2)
        approve = approve_col.button(
            "Approve", type="primary", key=f"approve_{record.id}"
        )
        reject = reject_col.button("Reject", key=f"reject_{record.id}")

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
                    st.toast(f"Audit #{record.id} moved to {updated.status.value.lower()}.")
                    st.rerun()


def _render_trace_inspector(factory: SessionFactory) -> None:
    """Render the audit trace inspector."""

    _section(
        "Trace",
        "Every extraction, tool call and decision, stored with the audit. "
        "Tool payloads are the exact values used for routing.",
    )

    records = list_audit_records(factory, limit=200)
    if not records:
        _html(
            """
            <div class="af-empty">
              <h3>No audits yet</h3>
              <p>Run a receipt from the audit tab and its trace will appear here.</p>
            </div>
            """
        )
        return

    labels = {
        f"#{record.id} · {record.status.value} · {record.invoice.vendor} · "
        f"{record.invoice.currency} {record.invoice.total_amount:,.2f}": record
        for record in records
    }
    selected = st.selectbox("Audit", options=list(labels))
    record = labels[selected]

    _render_audit_identity(record)

    failed = [result for result in record.policy_results if not result.passed]
    metric_cols = st.columns(4)
    metric_cols[0].metric(
        "Total", f"{record.invoice.currency} {record.invoice.total_amount:,.2f}"
    )
    metric_cols[1].metric(
        "Confidence", f"{record.invoice.confidence_score:.0%}"
    )
    metric_cols[2].metric("Failed checks", len(failed))
    metric_cols[3].metric("Trace steps", len(record.trace))

    st.download_button(
        "Download audit JSON",
        data=json.dumps(record.model_dump(mode="json"), indent=2),
        file_name=f"auditflow-audit-{record.id}.json",
        mime="application/json",
        key=f"download_audit_{record.id}",
    )

    timeline, checks, invoice_tab, raw = st.tabs(
        ["Timeline", "Policy", "Invoice", "Record"]
    )
    with timeline:
        _render_trace(record.trace, empty_message="No trace was recorded.")
    with checks:
        _render_policy_checks(record.policy_results)
    with invoice_tab:
        st.json(record.invoice.model_dump(mode="json"), expanded=False)
    with raw:
        st.json(record.model_dump(mode="json"), expanded=False)


def _render_system_section(
    factory: SessionFactory, policy: dict[str, object], counts: dict[AuditStatus, int]
) -> None:
    """Render policy, ledger counts and the demo reset control."""

    with st.expander("Policy, ledger and reset"):
        policy_col, ledger_col = st.columns([1.15, 1.0], gap="large")

        with policy_col:
            _html(
                f"""
                <p class="af-label">Policy</p>
                <p class="af-meta">{html.escape(str(policy['company']))} · version {html.escape(str(policy['version']))}
                · ceiling ${float(policy['auto_approve_threshold']):,.0f}</p>
                """
            )
            categories = policy["categories"]
            st.dataframe(
                [
                    {
                        "Category": name.replace("_", " ").title(),
                        "Limit": f"${float(config['budget_limit']):,.0f}",
                    }
                    for name, config in sorted(categories.items())
                ],
                hide_index=True,
            )

        with ledger_col:
            _html('<p class="af-label">Ledger</p>')
            metric_cols = st.columns(2)
            metric_cols[0].metric("Needs review", counts[AuditStatus.PENDING_REVIEW])
            metric_cols[1].metric("Auto-approved", counts[AuditStatus.AUTO_APPROVED])
            metric_cols = st.columns(2)
            metric_cols[0].metric("Approved", counts[AuditStatus.APPROVED])
            metric_cols[1].metric("Rejected", counts[AuditStatus.REJECTED])

            confirmed = st.checkbox(
                "Clear the audit history", key="reset_confirm"
            )
            if st.button(
                "Reset ledger",
                disabled=not confirmed,
                key="reset_ledger_button",
            ):
                removed = reset_ledger(factory)
                for key in (
                    "last_outcome",
                    "suite_results",
                    "queue_selection",
                    "trace_selection",
                ):
                    st.session_state.pop(key, None)
                st.toast(f"Removed {removed} audits.")
                st.rerun()


def _render_footer(policy: dict[str, object]) -> None:
    """Render the page footer."""

    _html(
        f"""
        <footer class="af-footer">
          <span>AuditFlow · MIT License · {html.escape(str(policy['company']))}</span>
          <span>
            PydanticAI, FastAPI and Streamlit ·
            <a href="{GITHUB_URL}" target="_blank" rel="noreferrer">GitHub</a>
          </span>
        </footer>
        """
    )


def main() -> None:
    """Compose the dashboard."""

    _inject_styles()
    try:
        factory = _ledger()
    except Exception as exc:  # pragma: no cover - startup boundary
        st.error(f"Could not open the AuditFlow ledger: {exc}")
        st.stop()

    try:
        policy = get_policy_summary()
    except AuditFlowError as exc:
        st.error(str(exc))
        st.stop()

    runtime = describe_runtime()
    counts = count_by_status(factory)

    _render_nav(runtime)
    _render_hero()
    _render_stage()

    audit_tab, review_tab, trace_tab = st.tabs(
        ["Audit", f"Review ({counts[AuditStatus.PENDING_REVIEW]})", "Trace"]
    )
    with audit_tab:
        _render_upload_tab(factory)
    with review_tab:
        _render_review_queue(factory)
    with trace_tab:
        _render_trace_inspector(factory)

    _render_system_section(factory, policy, counts)
    _render_footer(policy)


if __name__ == "__main__":
    main()
