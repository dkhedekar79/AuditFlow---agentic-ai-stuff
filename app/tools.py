"""Deterministic policy tools used by the agent and the guardrail gate.

These functions are the authoritative business rules of AuditFlow. They never
call an LLM, are cheap to run, and are safe to expose to a model as tools: an
invoice is only auto-approved when every one of them passes.

All limits come from :file:`data/policies.json`, so changing company policy does
not require touching code.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import date as DateType
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from app.database import SessionFactory, find_duplicate_invoice, get_session_factory
from app.models import AuditFlowError, normalise_category, normalise_vendor_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICIES_PATH = PROJECT_ROOT / "data" / "policies.json"

_lock = threading.Lock()
_cache_path: Path | None = None
_cache_mtime: float | None = None
_cache_payload: dict[str, Any] | None = None


class PolicyConfigurationError(AuditFlowError):
    """Raised when ``policies.json`` is missing, malformed or incomplete."""


def load_policies(
    path: Path | None = None, *, force_reload: bool = False
) -> dict[str, Any]:
    """Load and validate the expense policy, caching by file path and mtime.

    Args:
        path: Optional alternate policy file (tests use a temporary copy).
        force_reload: Bypass the cache; useful after editing the policy file.

    Returns:
        The validated policy document.

    Raises:
        PolicyConfigurationError: If the file cannot be read, parsed or fails
            structural validation.
    """

    global _cache_path, _cache_mtime, _cache_payload

    policy_path = (path or DEFAULT_POLICIES_PATH).resolve()
    try:
        mtime = policy_path.stat().st_mtime
    except OSError as exc:
        raise PolicyConfigurationError(
            f"Policy file not found at {policy_path}. Expected data/policies.json "
            "at the project root."
        ) from exc

    with _lock:
        if (
            not force_reload
            and _cache_payload is not None
            and _cache_path == policy_path
            and _cache_mtime == mtime
        ):
            return _cache_payload

        try:
            raw = policy_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except JSONDecodeError as exc:
            raise PolicyConfigurationError(
                f"{policy_path.name} is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})."
            ) from exc
        except OSError as exc:
            raise PolicyConfigurationError(
                f"Could not read {policy_path.name}: {exc}"
            ) from exc

        _validate_policy_document(payload, policy_path)
        _cache_path, _cache_mtime, _cache_payload = policy_path, mtime, payload
        return payload


def get_policy_summary(policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the non-sensitive policy facts the dashboard displays."""

    document = policies or load_policies()
    categories = document["categories"]
    return {
        "version": document.get("version", "unknown"),
        "company": document.get("company", "Unknown company"),
        "base_currency": document.get("base_currency", "USD"),
        "auto_approve_threshold": float(document["auto_approve_threshold"]),
        "categories": {
            name: {
                "budget_limit": float(config["budget_limit"]),
                "description": config.get("description", ""),
            }
            for name, config in categories.items()
        },
        "approved_vendor_count": len(document.get("approved_vendors", [])),
    }


def get_auto_approve_threshold(policies: dict[str, Any] | None = None) -> float:
    """Return the maximum total that may be auto-approved without a human."""

    document = policies or load_policies()
    return float(document["auto_approve_threshold"])


def get_base_currency(policies: dict[str, Any] | None = None) -> str:
    """Return the reporting currency used in reviewer-facing messages."""

    document = policies or load_policies()
    return str(document.get("base_currency", "USD")).upper()


def get_duplicate_settings(
    policies: dict[str, Any] | None = None,
) -> tuple[int, float]:
    """Return ``(lookback_days, amount_tolerance)`` for duplicate detection."""

    document = policies or load_policies()
    lookback = int(document.get("duplicate_lookback_days", 180))
    tolerance = float(document.get("duplicate_amount_tolerance", 0.01))
    return lookback, tolerance


def list_policy_categories(policies: dict[str, Any] | None = None) -> list[str]:
    """Return every expense category the policy understands, sorted."""

    document = policies or load_policies()
    return sorted(document["categories"])


def check_budget_limit(
    category: str, amount: float, *, policies: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Check a spend category against its configured budget.

    Args:
        category: Expense category as written on the receipt; normalised before
            lookup so ``"Office Supplies"`` maps to ``office_supplies``.
        amount: Invoice total in the policy's base currency.
        policies: Optional pre-loaded policy document.

    Returns:
        A JSON-serialisable payload with ``passed``/``within_budget`` plus the
        limit, remaining headroom and overage used to explain the decision.

    Raises:
        ValueError: If ``amount`` is not a positive, finite number.
    """

    document = policies or load_policies()
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be a positive, finite number")

    key = normalise_category(category)
    categories: dict[str, Any] = document["categories"]
    currency = get_base_currency(document)
    amount = round(float(amount), 2)

    if key not in categories:
        return {
            "tool": "check_budget_limit",
            "category": key,
            "amount": amount,
            "currency": currency,
            "passed": False,
            "within_budget": False,
            "limit": None,
            "remaining": 0.0,
            "overage": 0.0,
            "unknown_category": True,
            "known_categories": sorted(categories),
            "message": (
                f"No budget policy is defined for '{key}'. Finance must map this "
                "spend to a supported category before it can be approved."
            ),
            "policy_reference": None,
        }

    config = categories[key]
    limit = float(config["budget_limit"])
    within_budget = amount <= limit + 1e-9
    overage = round(max(0.0, amount - limit), 2)
    remaining = round(max(0.0, limit - amount), 2)
    label = key.replace("_", " ").capitalize()
    money = _money(currency)

    if within_budget:
        message = (
            f"{label} spend of {money(amount)} is within the {money(limit)} "
            f"category limit ({money(remaining)} remaining)."
        )
    else:
        message = (
            f"{label} spend of {money(amount)} exceeds the {money(limit)} "
            f"category limit by {money(overage)}."
        )

    return {
        "tool": "check_budget_limit",
        "category": key,
        "amount": amount,
        "currency": currency,
        "passed": within_budget,
        "within_budget": within_budget,
        "limit": limit,
        "remaining": remaining,
        "overage": overage,
        "unknown_category": False,
        "requires_manager_approval_above": config.get("requires_manager_approval_above"),
        "message": message,
        "policy_reference": f"categories.{key}.budget_limit",
    }


def check_auto_approve_threshold(
    amount: float, *, policies: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Check the invoice total against the global auto-approval threshold.

    This is the second guardrail behind the $500 rule: category budgets may pass
    while a large total still requires a human signature.
    """

    document = policies or load_policies()
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be a positive, finite number")

    currency = get_base_currency(document)
    threshold = get_auto_approve_threshold(document)
    amount = round(float(amount), 2)
    within_threshold = amount <= threshold + 1e-9
    money = _money(currency)

    if within_threshold:
        message = (
            f"Total of {money(amount)} is at or below the {money(threshold)} "
            "auto-approval threshold."
        )
    else:
        message = (
            f"Total of {money(amount)} exceeds the {money(threshold)} "
            "auto-approval threshold and needs a human decision."
        )

    return {
        "tool": "check_auto_approve_threshold",
        "amount": amount,
        "currency": currency,
        "threshold": threshold,
        "passed": within_threshold,
        "requires_human_review": not within_threshold,
        "message": message,
        "policy_reference": "auto_approve_threshold",
    }


def list_approved_vendors(
    policies: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a compact view of the approved-vendor allow-list."""

    document = policies or load_policies()
    return sorted(
        (
            {
                "name": str(vendor.get("name", "")),
                "category": vendor.get("category"),
                "risk_level": vendor.get("risk_level"),
                "payment_terms": vendor.get("payment_terms"),
            }
            for vendor in document.get("approved_vendors", [])
            if vendor.get("name")
        ),
        key=lambda vendor: vendor["name"].lower(),
    )


def verify_approved_vendor(
    vendor: str, *, policies: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Verify a vendor against the approved allow-list and blocked list.

    Matching is normalised (case, punctuation and legal suffixes are ignored)
    and checks aliases, so ``"Northwind Cloud Svcs"`` resolves to the approved
    ``"Northwind Cloud Services"``.

    Args:
        vendor: Vendor name as extracted from the receipt.
        policies: Optional pre-loaded policy document.

    Returns:
        A payload with ``passed``/``approved`` plus the matched vendor's risk
        level and payment terms. Blocked vendors are reported separately.

    Raises:
        ValueError: If the vendor name is too short to match safely.
    """

    document = policies or load_policies()
    key = normalise_vendor_name(vendor)
    if len(key) < 2:
        raise ValueError("vendor must contain at least two alphanumeric characters")

    base: dict[str, Any] = {
        "tool": "verify_approved_vendor",
        "vendor": vendor,
        "normalised_vendor": key,
        "approved": False,
        "blocked": False,
        "matched_vendor": None,
        "risk_level": None,
        "payment_terms": None,
        "policy_reference": "approved_vendors",
    }

    for blocked in document.get("blocked_vendors", []):
        if normalise_vendor_name(str(blocked.get("name", ""))) == key:
            return {
                **base,
                "passed": False,
                "blocked": True,
                "matched_vendor": blocked.get("name"),
                "message": (
                    f"{blocked.get('name', vendor)} is on the blocked-vendor list. "
                    f"{(blocked.get('reason') or 'Procurement must review this supplier.').strip()}"
                ),
                "policy_reference": "blocked_vendors",
            }

    for approved in document.get("approved_vendors", []):
        names = [str(approved.get("name", ""))] + [
            str(alias) for alias in approved.get("aliases", [])
        ]
        if key in {normalise_vendor_name(name) for name in names if name}:
            terms = approved.get("payment_terms") or "not specified"
            return {
                **base,
                "passed": True,
                "approved": True,
                "matched_vendor": approved.get("name"),
                "risk_level": approved.get("risk_level"),
                "payment_terms": approved.get("payment_terms"),
                "onboarded": approved.get("onboarded"),
                "message": (
                    f"{approved.get('name')} is an approved vendor "
                    f"(risk: {approved.get('risk_level', 'unknown')}, terms: {terms})."
                ),
            }

    return {
        **base,
        "passed": False,
        "message": (
            f"{vendor} is not on the approved-vendor list. Procurement must onboard "
            "the supplier before payment can be released."
        ),
    }


def duplicate_check(
    vendor: str,
    date: str | DateType,
    amount: float,
    *,
    policies: dict[str, Any] | None = None,
    session_factory: SessionFactory | None = None,
    exclude_id: int | None = None,
) -> dict[str, Any]:
    """Look for an earlier audit with the same vendor, date and amount.

    Args:
        vendor: Vendor name on the receipt.
        date: Invoice date as an ISO string or ``date``.
        amount: Invoice total in the policy base currency.
        policies: Optional pre-loaded policy document.
        session_factory: Optional database factory (defaults to the app ledger).
        exclude_id: Audit id to ignore when re-checking an existing record.

    Returns:
        A payload with ``duplicate_found`` and the matching record's id when found.

    Raises:
        ValueError: If the date or amount is invalid.
    """

    document = policies or load_policies()
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be a positive, finite number")

    parsed_date = _parse_invoice_date(date)
    lookback_days, tolerance = get_duplicate_settings(document)
    factory = session_factory or get_session_factory()
    currency = get_base_currency(document)
    money = _money(currency)

    match = find_duplicate_invoice(
        factory,
        vendor=vendor,
        invoice_date=parsed_date,
        total_amount=amount,
        exclude_id=exclude_id,
        lookback_days=lookback_days,
        amount_tolerance=tolerance,
    )

    base: dict[str, Any] = {
        "tool": "check_duplicate_invoice",
        "vendor": vendor,
        "date": parsed_date.isoformat(),
        "amount": round(float(amount), 2),
        "currency": currency,
        "lookback_days": lookback_days,
        "passed": match is None,
        "duplicate_found": match is not None,
        "matched_record_id": match.id if match else None,
        "matched_status": match.status.value if match else None,
        "policy_reference": "duplicate_lookback_days",
    }

    if match is None:
        return {
            **base,
            "message": (
                "No earlier audit matches this vendor, date and amount within the "
                f"{lookback_days}-day lookback window."
            ),
        }

    return {
        **base,
        "matched_vendor": match.invoice.vendor,
        "matched_total": match.invoice.total_amount,
        "matched_created_at": match.created_at.isoformat(),
        "message": (
            f"Possible duplicate: audit #{match.id} already covers "
            f"{match.invoice.vendor} on {parsed_date.isoformat()} for "
            f"{money(match.invoice.total_amount)} (status {match.status.value})."
        ),
    }


def check_duplicate_invoice(vendor: str, date: str, amount: float) -> bool:
    """Return ``True`` when this invoice duplicates an earlier audit.

    This is the concise tool signature exposed to the agent. Callers that need
    the matching record use :func:`duplicate_check` instead.
    """

    return bool(duplicate_check(vendor, date, amount)["duplicate_found"])


def _parse_invoice_date(value: str | DateType) -> DateType:
    """Parse an ISO date string, passing through ``date`` instances."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, DateType):
        return value
    try:
        return DateType.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(
            f"Invalid invoice date {value!r}; expected ISO format YYYY-MM-DD."
        ) from exc


def _money(currency: str):
    """Return a formatter for amounts in ``currency`` (USD gets a ``$`` sign)."""

    code = currency.upper()

    def format_amount(value: float) -> str:
        if code == "USD":
            return f"${value:,.2f}"
        return f"{value:,.2f} {code}"

    return format_amount


def _validate_policy_document(payload: Any, path: Path) -> None:
    """Fail fast with an actionable error when policy structure is wrong."""

    if not isinstance(payload, dict):
        raise PolicyConfigurationError(
            f"{path.name} must contain a JSON object at the top level."
        )

    categories = payload.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise PolicyConfigurationError(
            f"{path.name} must define a non-empty 'categories' object."
        )

    for name, config in categories.items():
        if not isinstance(config, dict):
            raise PolicyConfigurationError(
                f"Category '{name}' must be a JSON object."
            )
        limit = config.get("budget_limit")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
            raise PolicyConfigurationError(
                f"Category '{name}' needs a positive numeric 'budget_limit'."
            )

    threshold = payload.get("auto_approve_threshold")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or threshold <= 0
    ):
        raise PolicyConfigurationError(
            f"{path.name} needs a positive numeric 'auto_approve_threshold'."
        )

    if not isinstance(payload.get("approved_vendors"), list):
        raise PolicyConfigurationError(
            f"{path.name} must contain an 'approved_vendors' array."
        )
