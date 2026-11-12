"""Contract tests for AuditFlow's optional FastAPI surface.

These tests use FastAPI's TestClient against a temporary ledger, so they exercise
the same routes, validation and state machine as a real deployment without
starting a server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.database import (
    SessionFactory,
    build_engine,
    init_db,
    reset_default_engine,
    session_factory,
)
from app.parsing import read_receipt_file

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "sample_receipts"
CLEAN = SAMPLES / "01_clean_software_receipt.txt"
OVER_LIMIT = SAMPLES / "02_over_limit_travel.json"
UNAPPROVED = SAMPLES / "04_unapproved_vendor_receipt.json"


@pytest.fixture()
def factory(tmp_path: Path) -> SessionFactory:
    """Isolated ledger used by API handlers under test."""

    engine = build_engine(f"sqlite:///{tmp_path / 'api-test.db'}")
    init_db(engine)
    return session_factory(engine)


@pytest.fixture()
def client(
    factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """FastAPI test client wired to the fixture ledger and demo mode."""

    monkeypatch.setenv("AUDITFLOW_DEMO_MODE", "1")
    monkeypatch.setenv("AUDITFLOW_DB_PATH", str(tmp_path / "default.db"))
    for variable in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AUDITFLOW_TRACE_LOG",
    ):
        monkeypatch.delenv(variable, raising=False)

    # The API owns a process-wide default engine for its lifespan hook; reset it
    # so startup initialises the temporary database instead of the repo root.
    reset_default_engine()
    from app.api import app, get_factory

    app.dependency_overrides[get_factory] = lambda: factory
    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    reset_default_engine()


def _receipt(path: Path) -> str:
    return read_receipt_file(path).text


def _create_audit(client: TestClient, path: Path, source: str | None = None):
    return client.post(
        "/audits",
        json={"text": _receipt(path), "source": source or path.name},
    )


def test_health_reports_demo_mode(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["mode"] == "demo"
    assert payload["model_name"] is None
    assert payload["version"]


def test_runtime_endpoint_matches_agent_settings(client: TestClient) -> None:
    payload = client.get("/runtime").json()
    assert payload["mode"] == "demo"
    assert payload["demo_forced"] is True
    assert payload["strict_llm"] is False


def test_clean_invoice_is_auto_approved(client: TestClient) -> None:
    response = _create_audit(client, CLEAN)
    assert response.status_code == 201

    payload = response.json()
    assert payload["status"] == "AUTO_APPROVED"
    assert payload["audit_flags"] == []
    assert payload["record_id"] is not None
    assert payload["invoice"]["vendor"] == "Northwind Cloud Services"
    assert payload["invoice"]["total_amount"] == pytest.approx(89.0)
    assert len(payload["trace"]) >= 5

    stored = client.get(f"/audits/{payload['record_id']}")
    assert stored.status_code == 200
    assert stored.json()["invoice"]["category"] == "software"


def test_out_of_policy_invoice_pauses_for_review(client: TestClient) -> None:
    response = _create_audit(client, OVER_LIMIT)
    assert response.status_code == 201

    payload = response.json()
    assert payload["status"] == "PENDING_REVIEW"
    assert set(payload["audit_flags"]) == {
        "BUDGET_LIMIT_EXCEEDED",
        "OVER_AUTO_APPROVAL_THRESHOLD",
    }
    failed = [result for result in payload["policy_results"] if not result["passed"]]
    assert len(failed) == 2


def test_list_supports_status_filter_and_pagination(client: TestClient) -> None:
    _create_audit(client, CLEAN)
    _create_audit(client, OVER_LIMIT)

    all_audits = client.get("/audits").json()
    assert len(all_audits) == 2
    assert all_audits[0]["id"] > all_audits[1]["id"]  # newest first

    pending = client.get("/audits?status=PENDING_REVIEW").json()
    assert len(pending) == 1
    assert pending[0]["invoice"]["vendor"] == "Contoso Travel Group"

    limited = client.get("/audits?limit=1").json()
    assert len(limited) == 1

    assert client.get("/audits?limit=0").status_code == 422
    assert client.get("/audits?status=BOGUS").status_code == 422


def test_stats_returns_every_lifecycle_bucket(client: TestClient) -> None:
    _create_audit(client, CLEAN)
    _create_audit(client, UNAPPROVED)

    stats = client.get("/audits/stats").json()
    assert stats == {
        "PENDING_REVIEW": 1,
        "APPROVED": 0,
        "REJECTED": 0,
        "AUTO_APPROVED": 1,
    }


def test_decision_resumes_workflow_and_blocks_second_decision(
    client: TestClient,
) -> None:
    created = _create_audit(client, OVER_LIMIT).json()
    record_id = created["record_id"]

    approved = client.post(
        f"/audits/{record_id}/decision",
        json={
            "decision": "APPROVED",
            "note": "CFO approved the summit travel.",
            "reviewer": "daiwik",
        },
    )
    assert approved.status_code == 200
    payload = approved.json()
    assert payload["status"] == "APPROVED"
    assert payload["human_decision"] == "APPROVED"
    assert payload["reviewer"] == "daiwik"
    assert payload["trace"][-1]["kind"] == "human"

    conflict = client.post(
        f"/audits/{record_id}/decision", json={"decision": "REJECTED"}
    )
    assert conflict.status_code == 409
    assert "Allowed transitions: none" in conflict.json()["detail"]


def test_decision_for_missing_audit_returns_404(client: TestClient) -> None:
    response = client.post(
        "/audits/424242/decision", json={"decision": "REJECTED"}
    )
    assert response.status_code == 404
    assert client.get("/audits/424242").status_code == 404


def test_malformed_receipt_returns_422(client: TestClient) -> None:
    response = client.post(
        "/audits", json={"text": "this is not a receipt", "source": "bad.txt"}
    )
    assert response.status_code == 422
    assert "Could not find vendor" in response.json()["detail"]


def test_invalid_payloads_are_rejected(client: TestClient) -> None:
    missing_text = client.post("/audits", json={"source": "no-text.txt"})
    assert missing_text.status_code == 422

    created = _create_audit(client, CLEAN).json()
    bad_decision = client.post(
        f"/audits/{created['record_id']}/decision",
        json={"decision": "MAYBE"},
    )
    assert bad_decision.status_code == 422
