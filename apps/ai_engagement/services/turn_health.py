from __future__ import annotations

from typing import Any


TURN_HEALTH_VERSION = "v1"


def _has_rejected_actions(data: dict[str, Any]) -> bool:
    crm = data.get("crm_actions")
    if not isinstance(crm, dict):
        return False
    attempts = crm.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        isinstance(item, dict) and bool(item.get("rejected"))
        for item in attempts
    )


def evaluate_turn_health(data: dict[str, Any], *, total_ms: int | None = None) -> dict[str, Any]:
    """Compute a deterministic health score from facts already observed in a turn.

    This is deliberately not an LLM judging another LLM. It measures operational
    correctness signals that SHVYA can verify: grounding outcome, action
    acceptance, provider recovery, delivery state, runtime errors and latency.
    """

    data = data if isinstance(data, dict) else {}
    score = 100
    flags: list[str] = []

    status = str(data.get("status") or "").casefold()
    if status == "failed":
        score -= 55
        flags.append("runtime_failed")
    elif status == "blocked":
        score -= 35
        flags.append("runtime_blocked")
    elif status == "stale":
        score -= 10
        flags.append("superseded_turn")
    elif status == "duplicate":
        # Duplicate suppression is healthy idempotency, not a quality failure.
        flags.append("duplicate_suppressed")

    grounding = data.get("grounding")
    if isinstance(grounding, dict):
        if grounding.get("approved") is False:
            score -= 20
            flags.append("grounding_rejected")
        if grounding.get("repair_attempted"):
            score -= 3
            flags.append("grounding_repair_used")

    provider = data.get("provider")
    if isinstance(provider, dict):
        if provider.get("fallback_used"):
            score -= 4
            flags.append("provider_fallback_used")
        if provider.get("circuit_breaker_bypassed_primary"):
            score -= 2
            flags.append("primary_circuit_open")

    if _has_rejected_actions(data):
        score -= 12
        flags.append("crm_action_rejected")

    qualification = data.get("qualification")
    if isinstance(qualification, dict) and qualification.get("updates_rejected"):
        score -= 10
        flags.append("qualification_update_rejected")

    delivery = data.get("delivery")
    if not isinstance(delivery, dict):
        details = data.get("finalization")
        delivery = details.get("delivery") if isinstance(details, dict) else None
    if isinstance(delivery, dict) and str(delivery.get("status") or "").casefold() == "failed":
        score -= 25
        flags.append("delivery_failed")

    if isinstance(data.get("error"), dict) and data["error"]:
        score -= 20
        flags.append("runtime_error_observed")

    try:
        elapsed = max(0, int(total_ms or 0))
    except (TypeError, ValueError):
        elapsed = 0
    if elapsed >= 15000:
        score -= 12
        flags.append("very_slow_turn")
    elif elapsed >= 8000:
        score -= 7
        flags.append("slow_turn")
    elif elapsed >= 5000:
        score -= 3
        flags.append("moderate_latency")

    score = min(max(score, 0), 100)
    band = "excellent" if score >= 95 else "healthy" if score >= 85 else "degraded" if score >= 65 else "poor"

    return {
        "version": TURN_HEALTH_VERSION,
        "kind": "deterministic_turn_health",
        "score": score,
        "band": band,
        "flags": list(dict.fromkeys(flags))[:20],
        "total_ms": elapsed,
    }
