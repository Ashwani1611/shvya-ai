"""Attribute metered calls to one operator evaluation, never to wallet deltas.

The existing credit ledger remains authoritative. This optional ContextVar only
collects reservation identities created by this synchronous execution context;
concurrent customer traffic and other tenants are never included. No prompts,
answers, keys or reference text are captured, and billing behavior is unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


MAX_RESERVATIONS = 256
_CURRENT: ContextVar[tuple] = ContextVar("shvya_usage_observation", default=())


@dataclass
class UsageObservation:
    organization_id: str
    reservation_ids: set[str] = field(default_factory=set)
    truncated: bool = False

    def capture(self, reservation):
        if str(reservation.organization_id) != self.organization_id:
            return
        identity = str(reservation.pk)
        if identity in self.reservation_ids:
            return
        if len(self.reservation_ids) >= MAX_RESERVATIONS:
            self.truncated = True
            return
        self.reservation_ids.add(identity)


@contextmanager
def observe_usage(organization_id):
    observation = UsageObservation(str(organization_id))
    token = _CURRENT.set((*_CURRENT.get(), observation))
    try:
        yield observation
    finally:
        _CURRENT.reset(token)


def record_reservation(reservation):
    for observation in _CURRENT.get():
        observation.capture(reservation)


def usage_report(observation) -> dict:
    """Read only the captured tenant's ledger; unknown cost is never zero cost."""
    from django.db import DatabaseError, transaction
    from apps.ai_engagement.models import AICreditReservation, AICreditTransaction

    base = {"unit": "ai_credits", "currency_cost": None,
            "scope": "captured_reservations_only", "complete": False}
    try:
        with transaction.atomic():
            reservations = list(AICreditReservation.objects.filter(
                organization_id=observation.organization_id,
                pk__in=observation.reservation_ids,
            ).values("id", "status", "reserved_credits", "actual_credits"))
            entries = list(AICreditTransaction.objects.filter(
                organization_id=observation.organization_id,
                reservation_id__in=observation.reservation_ids,
                transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            ).values("reservation_id", "amount", "input_tokens", "output_tokens"))
    except DatabaseError:
        return {**base, "status": "unavailable", "settled_credits": None,
                "input_tokens": None, "output_tokens": None,
                "pending_reserved_credits": None}
    settled = {str(row["id"]): row for row in reservations if row["status"] == "settled"}
    pending = [row for row in reservations if row["status"] == "active"]
    ledger_ids = [str(row["reservation_id"]) for row in entries]
    consistent = (set(ledger_ids) == set(settled) and len(ledger_ids) == len(set(ledger_ids))
                  and all(-row["amount"] == settled[str(row["reservation_id"])]["actual_credits"]
                          for row in entries if str(row["reservation_id"]) in settled)
                  and all(row["amount"] <= 0 for row in entries))
    complete = (not observation.truncated and not pending and consistent
                and len(reservations) == len(observation.reservation_ids)
                and all(row["status"] in {"active", "settled", "released"} for row in reservations))
    return {**base, "status": "complete" if complete else "pending_or_incomplete",
            "complete": complete, "observed_reservations": len(observation.reservation_ids),
            "settled_credits": sum(-row["amount"] for row in entries) if consistent else None,
            "input_tokens": sum(row["input_tokens"] for row in entries) if consistent else None,
            "output_tokens": sum(row["output_tokens"] for row in entries) if consistent else None,
            "pending_reserved_credits": sum(row["reserved_credits"] for row in pending),
            "pending_reservations": len(pending),
            "released_reservations": sum(row["status"] == "released" for row in reservations),
            "truncated": observation.truncated}


def summarize_usage(cases, *, comparison_valid: bool) -> dict:
    totals = {}
    counts = {}
    for variant in ("baseline", "recovery"):
        rows = [turn.get("usage", {}) for case in cases for turn in case[variant]]
        known = all(type(row.get("settled_credits")) is int for row in rows)
        counts[variant] = len(rows)
        totals[variant] = {
            "complete": bool(rows) and all(row.get("complete") is True for row in rows),
            "settled_credits": sum(row["settled_credits"] for row in rows) if known else None,
            "pending_reserved_credits": sum(row.get("pending_reserved_credits") or 0 for row in rows) if known else None,
        }
    comparable = (comparison_valid and counts["baseline"] == counts["recovery"]
                  and all(value["complete"] for value in totals.values()))
    return {"unit": "ai_credits", "currency_cost": None, **totals,
            "incremental_credits": (totals["recovery"]["settled_credits"]
                                    - totals["baseline"]["settled_credits"]) if comparable else None,
            "incremental_complete": comparable}
