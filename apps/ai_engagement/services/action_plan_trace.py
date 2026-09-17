from __future__ import annotations


def trace_action_plan(plan) -> None:
    """Record Phase 7 proposal details in the existing AI Trace buffer.

    Tracing is observational and fail-soft. A trace failure must never change
    whether an action can be validated or executed.
    """
    try:
        from apps.ai_engagement.services.trace_service import append, record

        accepted = [item.as_dict() for item in plan.accepted_actions]
        rejected = [item.as_dict() for item in plan.rejected_actions]
        append(
            "crm_actions",
            "action_plans",
            {
                "plan_version": plan.plan_version,
                "organization_id": plan.organization_id,
                "lead_id": plan.lead_id,
                "source_message_id": plan.source_message_id,
                "policy_outcome": plan.policy_outcome,
                "plan_reason": plan.plan_reason,
                "proposed": [item.as_dict() for item in plan.actions],
                "accepted": accepted,
                "rejected": rejected,
                "rejection_reasons": [
                    item.reason_code for item in plan.rejected_actions if item.reason_code
                ],
            },
        )
        record(
            "performance",
            {"action_planning_ms": round(float(plan.planning_latency_ms), 3)},
        )
    except Exception:
        # AI Trace is explicitly fail-soft. Never make action processing depend
        # on audit/observability availability.
        return
