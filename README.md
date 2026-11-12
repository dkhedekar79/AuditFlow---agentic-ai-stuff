<div align="center">

# 🧾 AuditFlow

### Human-in-the-loop expense & invoice auditor

**PydanticAI extracts. Deterministic policy tools decide. A human signs off.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![PydanticAI](https://img.shields.io/badge/pydantic--ai-2.x-e92063)](https://ai.pydantic.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.162-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.67-ff4b4b?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-0f766e)](LICENSE)

</div>

AuditFlow is a portfolio-grade reference implementation of an **agentic workflow
that is allowed to pause**. An LLM reads a messy receipt, deterministic Python
tools enforce company policy, and anything unusual stops in a review queue until
a human approves or rejects it — with every tool call, policy check and decision
captured in an inspectable reasoning trace.

It runs **without an API key** thanks to a deterministic demo extractor, so you
can clone the repo and exercise the complete workflow in under a minute.

---

## Why this exists

Most agent demos hand the final decision to the model. That is the wrong
abstraction for finance: spend controls need to be reproducible, auditable and
owned by people.

AuditFlow separates the three jobs:

| Job | Owner | Why |
| --- | --- | --- |
| Turn an unstructured receipt into typed data | LLM (PydanticAI) | Language understanding is the model's strength |
| Enforce budgets, vendor rules and duplicate checks | Deterministic Python | Same input must always produce the same policy result |
| Approve money movement | Human reviewer | Accountability cannot be delegated to a prompt |

The result is an agent whose **output is data, not authority**.

## What it does

- **Typed extraction** — receipts become a strict `InvoiceData` model
  (`vendor`, `date`, `total_amount`, `currency`, `line_items`, `category`,
  `confidence_score`); malformed model output is rejected by Pydantic and retried.
- **Three deterministic guardrails** — category budget limits, an approved-vendor
  allow-list (with blocked suppliers) and duplicate-invoice detection against the
  SQLite ledger.
- **Routing with a real state machine** — clean invoices at or below **$500** are
  `AUTO_APPROVED`; failed checks, duplicates or larger totals become
  `PENDING_REVIEW` with explicit audit flags.
- **Human-in-the-loop pause/resume** — reviewers approve (with an optional
  override note) or reject from the dashboard; the decision is written to the
  ledger and appended to the audit trace. Terminal states cannot be re-decided.
- **Complete observability** — every LLM request, tool call, policy result and
  routing decision is stored as a `TraceStep`, streamed to an optional JSON-lines
  sink, and rendered in a trace inspector.
- **Three ways in** — a Streamlit dashboard, a FastAPI surface, and a pytest
  evaluation suite that asserts the routing of all four demo receipts.

## Stack

| Layer | Choice |
| --- | --- |
| Agent framework | [PydanticAI](https://ai.pydantic.dev/) 2.x — typed outputs, dependency injection, function tools |
| Contracts | Pydantic 2 |
| Persistence | SQLite via SQLAlchemy 2 (JSON columns + WAL mode) |
| Backend | FastAPI + Uvicorn |
| Dashboard | Streamlit |
| Document parsing | pypdf + pdfplumber, plus JSON and plain text |
| Observability | Local JSON trace steps, optional Logfire/OpenTelemetry |
| Evaluation | pytest |

## Demo modes

AuditFlow selects its extraction mode at runtime:

- **LLM mode** — set `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) in `.env`. The
  agent extracts fields and may call the policy tools while doing so.
- **Demo mode** — leave the keys blank. A deterministic extractor parses the
  bundled receipts, policies still run, and the same review queue appears. No
  network calls, no key, no cost.

The deterministic policy gate runs in **both** modes, so routing is identical.
