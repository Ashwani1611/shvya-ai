from __future__ import annotations

import logging
from dataclasses import replace


logger = logging.getLogger(__name__)
_INSTALLED = False


def install_transactional_decision_reuse() -> None:
    """Reuse the single provider decision after deterministic CRM resolution.

    The first engagement pass is the structured understanding/extraction pass. A
    state-changing turn then commits attributes, qualification state, stage and
    workflow actions before the canonical task continues. The canonical task must
    not call the provider a second time merely because that state revision
    changed; instead it reuses the already validated customer-response candidate
    with side effects removed and validates/finalizes it against the committed
    backend state before send.

    This preserves the required execution order without doubling model cost or
    creating inconsistent second-generation decisions for the same inbound turn.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.runtime_state import state_revision

    current_resolve = runtime._resolve_state_before_response

    def resolve_state_before_response(
        *,
        organization,
        lead,
        source_message_id,
        decision,
        account_id=None,
    ):
        result = current_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        if not isinstance(result, dict) or not result.get("applied"):
            return result

        try:
            lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
            committed_revision = state_revision(lead)
            final_candidate = replace(
                decision,
                # These mutations were already executed inside the lead row lock.
                # The canonical finalize/send path must never execute them twice.
                crm_actions=[],
                qualification_updates=[],
                backend_revision=committed_revision,
            )
            runtime._PRECOMPUTED_DECISION.set(
                {
                    "lead_id": str(lead.pk),
                    "revision": committed_revision,
                    "decision": final_candidate,
                }
            )
        except Exception:
            # Failing to populate this optimization must not hide a valid turn;
            # the canonical path can still regenerate as its existing fallback.
            logger.exception(
                "Unable to reuse pre-resolved engagement decision for lead %s",
                getattr(lead, "pk", ""),
            )
        return result

    runtime._resolve_state_before_response = resolve_state_before_response

    # ContextVars survive until explicitly reset. Always clear the one-turn
    # response candidate even when the canonical path exits early (for example a
    # permission/account freshness re-check) so a later inbound message on the
    # same worker can never consume a stale decision.
    from apps.ai_engagement import tasks as task_module

    current_task_execute = task_module._execute_ai_engagement_response_impl

    def execute_task(*, task, lead_id: str):
        try:
            return current_task_execute(task=task, lead_id=lead_id)
        finally:
            runtime._PRECOMPUTED_DECISION.set(None)

    task_module._execute_ai_engagement_response_impl = execute_task

    from apps.hosted_automation import execution as hosted_execution

    current_hosted_execute = hosted_execution.execute_hosted_ai_engagement

    def execute_hosted(*, task, job):
        try:
            return current_hosted_execute(task=task, job=job)
        finally:
            runtime._PRECOMPUTED_DECISION.set(None)

    hosted_execution.execute_hosted_ai_engagement = execute_hosted
    _INSTALLED = True
