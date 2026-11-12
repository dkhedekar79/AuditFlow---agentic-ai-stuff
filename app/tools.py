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
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from app.models import AuditFlowError, normalise_category

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
