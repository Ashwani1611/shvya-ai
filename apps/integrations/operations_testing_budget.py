"""Hard preflight limits on every metered provider call in a test context."""
from apps.integrations.operations_testing_scope import current_test_scope


def conservative_test_input_tokens(text, ordinary_estimate):
    if current_test_scope() is None:
        return ordinary_estimate
    # UTF-8 bytes conservatively bound token count; add request framing allowance.
    # Normal customer billing estimates are unchanged.
    return max(ordinary_estimate, len(str(text or "").encode("utf-8")) + 4096)


def enforce_provider_tenant(metadata):
    scope = current_test_scope()
    if scope is not None and str((metadata or {}).get("organization_id") or "") != scope.organization_id:
        from apps.ai_engagement.services.ai_provider import AIProviderPermanentError
        raise AIProviderPermanentError("Isolated test provider calls require the manifest organization.")


def budget_usage(run):
    entries = list(run.usage_reservations.select_related("reservation").all())
    settled = 0
    pending = 0
    for entry in entries:
        item = entry.reservation
        if item.status == "settled":
            settled += int(item.actual_credits)
        elif item.status == "active":
            pending += max(int(item.reserved_credits), int(item.actual_credits or 0))
    return {"unit": "ai_credits", "settled_credits": settled, "reserved_credits": pending,
            "provider_calls": run.provider_calls, "remaining_credits": max(run.max_credits - settled - pending, 0),
            "max_credits": run.max_credits, "max_provider_calls": run.max_provider_calls,
            "over_budget": settled + pending > run.max_credits}


def claim_test_reservation(*, organization_id, credits):
    """Called inside the wallet reservation transaction; lock serializes all workers."""
    scope = current_test_scope()
    if scope is None:
        return None
    from apps.ai_engagement.services.credits import AICreditUnavailableError
    from apps.integrations.operations_testing_models import OperationsAIFlowRun
    if str(organization_id) != scope.organization_id:
        raise AICreditUnavailableError("Test billing organization does not match the owned run.")
    run = OperationsAIFlowRun.objects.select_for_update().get(pk=scope.run_id, organization_id=scope.organization_id)
    if run.status != "ready" or not run.active_turn_id:
        raise AICreditUnavailableError("Test run has no active authorized turn.")
    usage = budget_usage(run)
    if run.provider_calls >= run.max_provider_calls:
        raise AICreditUnavailableError("Test provider-call budget exhausted.")
    if int(credits) > usage["remaining_credits"]:
        raise AICreditUnavailableError("Test credit budget cannot cover the next conservative reservation.")
    run.provider_calls += 1
    run.save(update_fields=["provider_calls", "updated_at"])
    return run


def attach_test_reservation(run, reservation):
    if run is not None:
        from apps.integrations.operations_testing_models import OperationsAIFlowReservation
        OperationsAIFlowReservation.objects.create(run=run, reservation=reservation)
