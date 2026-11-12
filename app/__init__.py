"""AuditFlow — human-in-the-loop expense and invoice auditing.

The package is intentionally small and layered:

* :mod:`app.models` — typed contracts shared by every layer
* :mod:`app.database` — SQLite ledger and audited state transitions
* :mod:`app.tools` — deterministic policy, vendor and duplicate checks
* :mod:`app.parsing` — PDF/JSON/text receipt ingestion
* :mod:`app.agent` — PydanticAI extraction, guardrails and routing
* :mod:`app.ui` — Streamlit review dashboard
* :mod:`app.api` — optional FastAPI surface
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
