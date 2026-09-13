from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import is_dataclass, replace


_INSTALLED = False
_NEW_LEAD_STAGE_NAMES = {"new lead", "new leads"}

_FINAL_SCOPE_INSTRUCTIONS = """
============================================================
QUALIFICATION STAGE SCOPE - FINAL AUTHORITY
============================================================
- Qualification runs only while the lead is in New Lead/New leads.
- Do not move a lead to In Conversation merely because the lead replied.
- Keep an incomplete qualifying lead in New Lead so the backend can continue the
  next unanswered requirement without pausing or restarting the flow.
- When deterministic backend qualification is complete, move the lead directly
  to Qualified. Do not route through In Conversation first.
- In Conversation and every other non-New-Lead stage are normal conversation
  stages: qualification_updates must be empty and next_requirement_id must be null.
- Completed qualification stays completed and must never restart on a later
  greeting, acknowledgement, pricing question, call request, or other message.
""".strip()


def _clean(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _lead_stage_name(lead) -> str:
    return _clean(getattr(getattr(lead, "stage", None), "name", ""))


def _context_stage_name(context) -> str:
    stage = getattr(context, "stage", None)
    return _clean(stage.get("name") if isinstance(stage, dict) else "")


def _patch_qualification_state() -> None:
    from apps.ai_engagement.services import qualification_state as qstate

    if getattr(qstate, "_shvya_new_lead_scope_patch", False):
        return

    current_normalize = qstate._normalize_runtime_state
    current_mark_in_progress = qstate.mark_in_progress
    current_reset_state = qstate.reset_state

    def normalize_runtime_state(state, requirements, *, lead=None):
        normalized = current_normalize(state, requirements, lead=lead)
        if lead is None or normalized.get("qualification_status") == qstate.STATUS_COMPLETED:
            return normalized

        stage_name = _lead_stage_name(lead)
        normalized["engagement_mode"] = (
            qstate.MODE_QUALIFICATION
            if stage_name in _NEW_LEAD_STAGE_NAMES
            else qstate.MODE_CONVERSATION
        )
        return normalized

    def mark_in_progress(lead):
        if _lead_stage_name(lead) not in _NEW_LEAD_STAGE_NAMES:
            return qstate.state_for_lead(lead)
        return current_mark_in_progress(lead)

    def reset_state(lead):
        state = current_reset_state(lead)
        if (
            _lead_stage_name(lead) not in _NEW_LEAD_STAGE_NAMES
            and state.get("qualification_status") != qstate.STATUS_COMPLETED
        ):
            state["engagement_mode"] = qstate.MODE_CONVERSATION
            qstate._persist_state(lead, state)
        return qstate.state_for_lead(lead)

    qstate._normalize_runtime_state = normalize_runtime_state
    qstate.mark_in_progress = mark_in_progress
    qstate.reset_state = reset_state
    qstate._shvya_new_lead_scope_patch = True

    # signals.py binds mark_in_progress during module import, before AppConfig.ready
    # installs runtime wrappers. Keep the signal on the final authoritative function.
    from apps.ai_engagement import signals as signal_module

    signal_module.mark_in_progress = mark_in_progress


def _restore_new_lead_stage_guards() -> None:
    from apps.ai_engagement.services import conversation_priority_runtime
    from apps.ai_engagement.services import qualification_crm_action_runtime

    conversation_priority_runtime._NEW_LEAD_STAGE_NAMES.discard("in conversation")
    qualification_crm_action_runtime._NEW_LEAD_STAGE_NAMES.discard("in conversation")


def _without_qualification_proposals(decision):
    """Keep normal CRM proposals but remove qualification output outside New Lead."""

    if is_dataclass(decision):
        try:
            return replace(
                decision,
                qualification_updates=[],
                next_requirement_id=None,
            )
        except TypeError:
            pass

    cloned = copy(decision)
    if hasattr(cloned, "qualification_updates"):
        setattr(cloned, "qualification_updates", [])
    if hasattr(cloned, "next_requirement_id"):
        setattr(cloned, "next_requirement_id", None)
    return cloned


def _enforce_policy_action_scope() -> None:
    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    if getattr(policy_actions_module, "_shvya_new_lead_scope_patch", False):
        return

    current_builder = policy_actions_module.build_controlled_actions

    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        stage_name = _context_stage_name(context)
        effective_decision = (
            decision
            if stage_name in _NEW_LEAD_STAGE_NAMES
            else _without_qualification_proposals(decision)
        )

        controlled, result = current_builder(
            decision=effective_decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        controlled = [deepcopy(item) for item in controlled]
        result = deepcopy(result) if isinstance(result, dict) else {}

        # #167 added an automatic New Lead -> In Conversation transition on the
        # first genuine inbound. That conflicts with the qualification contract:
        # the lead must remain in New Lead until deterministic completion.
        transition = result.get("stage_transition")
        if (
            isinstance(transition, dict)
            and transition.get("source") == "first_genuine_inbound"
        ):
            automatic_stage_id = str(transition.get("stage_id") or "").strip()
            controlled = [
                item
                for item in controlled
                if not (
                    item.get("type") == "pipeline_transition"
                    and str((item.get("stage_shift") or {}).get("stage_id") or "").strip()
                    == automatic_stage_id
                )
            ]
            result.pop("stage_transition", None)
            result["new_lead_qualification_scope_enforced"] = True

        return controlled, result

    policy_actions_module.build_controlled_actions = build
    policy_actions_module._shvya_new_lead_scope_patch = True


def _restore_prompt_scope() -> None:
    from apps.ai_engagement.services.base_instructions import SHVYABaseInstructions
    from apps.ai_engagement.services.engagement import EngagementService

    base = SHVYABaseInstructions.SYSTEM_INSTRUCTIONS
    base = base.replace(
        "9. Qualification mode is active only when the backend supplies engagement_mode\n"
        "   as qualification. In the standard sales flow this may continue through both\n"
        "   New Lead/New leads and In Conversation while qualification is incomplete.\n"
        "   Moving New Lead to In Conversation must not reset, pause, or restart the\n"
        "   questionnaire. In a stage where backend engagement_mode is conversation,\n"
        "   qualification_updates MUST be [] and next_requirement_id MUST be null.\n",
        "9. Qualification mode is active only while the lead is currently in New Lead/New leads.\n"
        "   In every other stage, qualification_updates MUST be [] and next_requirement_id\n"
        "   MUST be null. Never ask a qualification question there.\n",
    )
    SHVYABaseInstructions.SYSTEM_INSTRUCTIONS = base

    instructions = EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS
    if _FINAL_SCOPE_INSTRUCTIONS not in instructions:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{instructions}\n\n{_FINAL_SCOPE_INSTRUCTIONS}"
        )


def install_qualification_scope_runtime() -> None:
    """Make New Lead the single qualification stage across all runtime layers."""

    global _INSTALLED
    if _INSTALLED:
        return

    _patch_qualification_state()
    _restore_new_lead_stage_guards()
    _enforce_policy_action_scope()
    _restore_prompt_scope()

    _INSTALLED = True
