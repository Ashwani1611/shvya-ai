"""Exact, context-local attribution over the existing immutable credit ledger.

No rates, billing behavior, balances or provider adapters are changed. Tracking
is opt-in for an operator comparison; ordinary turns pay no extra database cost.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID


_SCOPES = ContextVar("shvya_usage_attribution", default=())
MAX_RESERVATIONS = 256


@dataclass
class UsageCapture:
    organization_id: str
    reservation_ids: set[str] = field(default_factory=set)
    truncated: bool = False


def capture_reservation(reservation):
    """An observability failure can never change a successful credit reservation."""
    for scope in _SCOPES.get():
        if str(reservation.organization_id) != scope.organization_id:
            continue
        if len(scope.reservation_ids) >= MAX_RESERVATIONS:
            scope.truncated = True
        else:
            scope.reservation_ids.add(str(reservation.pk))


@contextmanager
def capture_usage(organization_id):
    scope = UsageCapture(organization_id=str(UUID(str(organization_id))))
    token = _SCOPES.set((*_SCOPES.get(), scope))
    try:
        yield scope
    finally:
        _SCOPES.reset(token)


def summarize_rows(reservations, transactions, *, expected_count, truncated=False):
    """Report incomplete accounting explicitly, never as free provider usage."""
    by_reservation = {}
    for row in transactions:
        by_reservation.setdefault(str(row["reservation_id"]), []).append(row)
    settled_credits = input_tokens = output_tokens = reserved = pending_actual = 0
    counts = {"settled": 0, "released": 0, "pending": 0, "inconsistent": 0}
    for row in reservations:
        ledger = by_reservation.get(str(row["id"]), [])
        if row["status"] == "settled":
            if (len(ledger) != 1 or ledger[0]["amount"] != -row["actual_credits"]
                    or ledger[0]["input_tokens"] != row["actual_input_tokens"]
                    or ledger[0]["output_tokens"] != row["actual_output_tokens"]):
                counts["inconsistent"] += 1
                continue
            counts["settled"] += 1
            settled_credits += row["actual_credits"]
            input_tokens += row["actual_input_tokens"]
            output_tokens += row["actual_output_tokens"]
        elif row["status"] == "released" and not ledger:
            counts["released"] += 1
        elif row["status"] == "active":
            counts["pending"] += 1
            reserved += row["reserved_credits"]
            pending_actual += row["actual_credits"]
        else:
            counts["inconsistent"] += 1
    missing = max(0, expected_count - len(reservations))
    complete = not (truncated or missing or counts["pending"] or counts["inconsistent"])
    return {"unit": "internal_ai_credits", "accounting_complete": complete,
            "reservation_count": expected_count, **counts, "missing": missing,
            "truncated": bool(truncated), "confirmed_settled_credits": settled_credits,
            "total_credits": settled_credits if complete else None,
            "pending_reserved_credits": reserved, "pending_recorded_charge": pending_actual,
            "settled_input_tokens": input_tokens, "settled_output_tokens": output_tokens,
            "currency_cost": None, "currency_cost_reason": "no_currency_conversion_configured"}


def usage_report(scope):
    from django.db import transaction
    from apps.ai_engagement.models import AICreditReservation, AICreditTransaction

    try:
        # The savepoint protects a caller's transaction if optional reporting
        # storage is temporarily unavailable. A partial read is not a zero cost.
        with transaction.atomic():
            rows = list(AICreditReservation.objects.filter(
                organization_id=scope.organization_id, pk__in=scope.reservation_ids,
            ).values("id", "status", "reserved_credits", "actual_credits",
                     "actual_input_tokens", "actual_output_tokens"))
            ledger = list(AICreditTransaction.objects.filter(
                organization_id=scope.organization_id, reservation_id__in=scope.reservation_ids,
                transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            ).values("reservation_id", "amount", "input_tokens", "output_tokens")[:MAX_RESERVATIONS * 2])
        return summarize_rows(rows, ledger, expected_count=len(scope.reservation_ids), truncated=scope.truncated)
    except Exception:
        return {"unit": "internal_ai_credits", "accounting_complete": False,
                "total_credits": None, "reason": "usage_storage_unavailable"}


def merge_capture(target, captured):
    """Retain bounded attribution even across a many-turn comparison."""
    if target.organization_id != captured.organization_id:
        raise ValueError("Usage capture organization mismatch")
    ids = target.reservation_ids | captured.reservation_ids
    target.truncated |= captured.truncated or len(ids) > MAX_RESERVATIONS
    target.reservation_ids = set(sorted(ids)[:MAX_RESERVATIONS])


def comparison_cost(baseline, recovery):
    old, new = usage_report(baseline), usage_report(recovery)
    complete = old["accounting_complete"] and new["accounting_complete"]
    return {"baseline": old, "recovery": new, "accounting_complete": complete,
            "incremental_credits": new["total_credits"] - old["total_credits"] if complete else None,
            "scope": "exact_reservations_from_this_comparison_not_wallet_balance_delta"}
