from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace


_INSTALLED = False
_QUALIFICATION_STAGE_NAMES = {"new lead", "new leads", "in conversation"}
_SIMPLE_ACKS = {
    "ok", "okay", "sure", "cool", "great", "fine", "thanks", "thank you",
    "got it", "understood", "alright", "all right", "it's alright", "its alright",
    "no worries", "all good", "perfect", "sounds good",
}
_MISSED_CALL_TERMS = (
    "no one called", "nobody called", "didn't call", "did not call", "missed my call",
)
_CALL_REQUEST_RE = re.compile(
    r"\b(?:call\s+me|call\s+back|callback|contact\s+me|connect\s+me|"
    r"speak\s+with|talk\s+to|book\s+(?:a\s+)?(?:call|demo|meeting)|"
    r"schedule\s+(?:a\s+)?(?:call|demo|meeting)|demo|meeting)\b",
    flags=re.IGNORECASE,
)
_UNCONFIRMED_FUTURE_PROMISE_RE = re.compile(
    r"\b(?:i|we)\s+(?:will|'ll)\s+(?:confirm|check|ask|inform|tell|contact|call|"
    r"follow\s*up|get\s+back)\b",
    flags=re.IGNORECASE,
)

_NATURAL_CONVERSATION_INSTRUCTIONS = r"""
NATURAL CONVERSATION AND QUALIFICATION CONTINUITY
- This section supersedes any earlier sentence that says qualification is New Lead-only.
- Qualification may remain active in both New Lead/New leads and In Conversation until
  backend qualification_status becomes completed.
- Moving New Lead -> In Conversation on the lead's first genuine inbound response is a
  normal CRM transition and MUST NOT pause, reset, or restart qualification.
- Qualification requirements are data requirements, not a customer-facing script.
  Respond to the lead's actual intent first, then naturally continue with the one
  backend-authorized unanswered qualification question when appropriate.
- Never repeat the qualification completion acknowledgement after the backend marks
  qualification_completion_ack_sent=true.
- Treat short social acknowledgements such as okay, sure, cool, thanks, alright,
  perfect, and "it's alright" as normal conversation. Never answer them with an
  unknown-information fallback.
- After qualification is complete, continue as a normal sales assistant. A greeting,
  acknowledgement, pricing question, call request, complaint, or later message must
  never restart Q1-Q5.
- For explicit call/demo/human-contact requests, use configured CRM actions when the
  matching organization stage/reminder is available. Do not claim a booked slot unless
  the system confirms it; a requested/preferred time is not a confirmed appointment.
- Never promise "I/we will confirm/check/get back/call/follow up" unless this turn also
  creates a validated backend action that can actually carry out or surface that work.
- If the lead reports that a previously booked call was missed, treat that as a human
  follow-up/escalation request rather than a generic unknown-information question.
""".strip()


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized(value) -> str:
    return _clean(value).replace("’", "'").replace("‘", "'").casefold().strip(" .!?;:")


def _stage_name_from_lead(lead) -> str:
    return _normalized(getattr(getattr(lead, "stage", None), "name", ""))


def _latest_inbound_text(context) -> str:
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("direction") != "inbound":
            continue
        body = str(message.get("body") or "").strip()
        if body:
            return body
    return ""


def _patch_qualification_state() -> None:
    from apps.ai_engagement.services import qualification_state as qstate

    if getattr(qstate, "_shvya_natural_conversation_patch", False):
        return

    original_normalize = qstate._normalize_runtime_state
    original_mark_in_progress = qstate.mark_in_progress
    original_reset_state = qstate.reset_state

    def normalize_runtime_state(state, requirements, *, lead=None):
        normalized = original_normalize(state, requirements, lead=lead)
        if lead is None:
            return normalized
        stage_name = _stage_name_from_lead(lead)
        if (
            stage_name in _QUALIFICATION_STAGE_NAMES
            and normalized.get("qualification_status") != qstate.STATUS_COMPLETED
        ):
            normalized["engagement_mode"] = qstate.MODE_QUALIFICATION
            if not normalized.get("current_requirement_id"):
                item = qstate.next_requirement(
                    requirements,
                    normalized.get("requirement_states", {}),
                )
                if item is not None:
                    requirement_id = str(item.get("id") or "").strip() or None
                    normalized["current_requirement_id"] = requirement_id
                    normalized["next_requirement_id"] = requirement_id
        return normalized

    def mark_in_progress(lead):
        stage_name = _stage_name_from_lead(lead)
        if stage_name not in _QUALIFICATION_STAGE_NAMES:
            return original_mark_in_progress(lead)
        state = qstate.state_for_lead(lead)
        if state.get("qualification_status") == qstate.STATUS_NOT_STARTED:
            state["qualification_status"] = qstate.STATUS_IN_PROGRESS
            state["engagement_mode"] = qstate.MODE_QUALIFICATION
            qstate._append_history(state, event="qualification_started")
            qstate._persist_state(lead, state)
        return qstate.state_for_lead(lead)

    def reset_state(lead):
        state = original_reset_state(lead)
        if (
            _stage_name_from_lead(lead) in _QUALIFICATION_STAGE_NAMES
            and state.get("qualification_status") != qstate.STATUS_COMPLETED
            and state.get("engagement_mode") != qstate.MODE_QUALIFICATION
        ):
            state["engagement_mode"] = qstate.MODE_QUALIFICATION
            qstate._persist_state(lead, state)
        return qstate.state_for_lead(lead)

    qstate._normalize_runtime_state = normalize_runtime_state
    qstate.mark_in_progress = mark_in_progress
    qstate.reset_state = reset_state
    qstate._shvya_natural_conversation_patch = True

    # signals.py imports these functions before AppConfig.ready installs runtime
    # patches, so update its bound aliases too.
    from apps.ai_engagement import signals as signal_module

    signal_module.mark_in_progress = mark_in_progress


def _patch_qualification_stage_guards() -> None:
    from apps.ai_engagement.services import conversation_priority_runtime
    from apps.ai_engagement.services import qualification_crm_action_runtime

    conversation_priority_runtime._NEW_LEAD_STAGE_NAMES.add("in conversation")
    qualification_crm_action_runtime._NEW_LEAD_STAGE_NAMES.add("in conversation")


def _patch_policy_actions() -> None:
    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    from apps.ai_engagement.services import engagement_instruction_runtime as authored_runtime
    from apps.ai_engagement.services.qualification_state import project_answer_updates

    if getattr(policy_actions_module, "_shvya_natural_conversation_patch", False):
        return

    current_builder = policy_actions_module.build_controlled_actions

    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        controlled = [dict(item) for item in controlled]
        stage = getattr(context, "stage", None)
        stage_name = _normalized(stage.get("name") if isinstance(stage, dict) else "")
        latest_message_id, _latest_text = authored_runtime._latest_inbound(context)

        # The authored attribute mapping wrapper historically skipped In Conversation.
        # Re-project the current verified answer so mappings such as Q1 -> BIGGEST
        # PROBLEM continue after New Lead -> In Conversation.
        if stage_name == "in conversation":
            projected = project_answer_updates(
                state=qualification_state,
                requirements=requirements,
                updates=getattr(decision, "qualification_updates", []) or [],
                messages=(getattr(context, "conversation", None) or {}).get("messages", []),
            )
            authored_runtime._merge_authored_attribute_updates(
                controlled,
                context=context,
                projected_state=projected,
                requirements=requirements,
                runtime_policy=runtime_policy,
                latest_message_id=latest_message_id,
            )
            result = {**result, "projected_qualification_state": projected}

        # First genuine inbound moves New Lead -> In Conversation, but only when
        # another stronger transition (Qualified, Call Requested, handoff, etc.)
        # has not already been selected for this turn.
        if (
            stage_name in {"new lead", "new leads"}
            and latest_message_id
            and not any(item.get("type") == "pipeline_transition" for item in controlled)
            and qualification_state.get("qualification_status") != "completed"
        ):
            pipeline = getattr(context, "pipeline", None)
            destinations = pipeline.get("available_stages", []) if isinstance(pipeline, dict) else []
            in_conversation = next(
                (
                    item
                    for item in destinations
                    if isinstance(item, dict)
                    and _normalized(item.get("name")) == "in conversation"
                    and item.get("id") is not None
                ),
                None,
            )
            if in_conversation is not None:
                stage_id = str(in_conversation.get("id"))
                controlled.append({
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": stage_id},
                })
                result = {
                    **result,
                    "stage_transition": {
                        "stage_id": stage_id,
                        "source": "first_genuine_inbound",
                    },
                }

        return controlled, result

    policy_actions_module.build_controlled_actions = build
    policy_actions_module._shvya_natural_conversation_patch = True


def _patch_completion_ack_tracking() -> None:
    from apps.ai_engagement.services import runtime_state

    if getattr(runtime_state, "_shvya_completion_ack_patch", False):
        return

    original_finalize = runtime_state.finalize_runtime

    def finalize_runtime(*, lead, decision, qualification, requirements, message_id):
        attributes_before = deepcopy(lead.attributes or {})
        saved_before = attributes_before.get(runtime_state.STATE_KEY)
        saved_before = saved_before if isinstance(saved_before, dict) else {}
        was_complete = saved_before.get("qualification_status") == "completed"

        state = original_finalize(
            lead=lead,
            decision=decision,
            qualification=qualification,
            requirements=requirements,
            message_id=message_id,
        )

        is_complete = qualification.get("qualification_status") == "completed"
        if is_complete and not was_complete and getattr(decision, "should_engage", False):
            state["qualification_completion_ack_sent"] = True
            state["qualification_completion_ack_message_id"] = str(message_id)
            state["qualification_completion_ack_hash"] = runtime_state.response_hash(
                getattr(decision, "message", "")
            )
            attributes = deepcopy(lead.attributes or {})
            attributes[runtime_state.STATE_KEY] = state
            lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
            lead.attributes = attributes
        return state

    runtime_state.finalize_runtime = finalize_runtime
    runtime_state._shvya_completion_ack_patch = True


def _patch_engagement_validation_and_prompt() -> None:
    from apps.ai_engagement.services import engagement as engagement_module
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.runtime_state import STATE_KEY, response_hash

    if getattr(EngagementService, "_shvya_natural_conversation_patch", False):
        return

    # Remove the contradictory authored-policy sentence already appended by an
    # earlier runtime installer, then add the final authoritative behavior.
    EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS.replace(
            "- New Lead/New leads is the only qualification stage. In every other stage,\n"
            "  continue normal conversation only; never emit qualification_updates or restart\n"
            "  the questionnaire. CRM stage movement and reminders may still be proposed.\n",
            "- New Lead/New leads and In Conversation may carry an incomplete qualification flow.\n"
            "  A move to In Conversation does not pause or restart qualification.\n",
        )
        + "\n\n"
        + _NATURAL_CONVERSATION_INSTRUCTIONS
    )

    current_validator = EngagementService._validate_qualification_decision

    def validate(self, *, decision, context, requirements, qualification_state):
        current_validator(
            self,
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=qualification_state,
        )

        runtime = ((getattr(context, "lead", {}) or {}).get("attributes") or {}).get(STATE_KEY) or {}
        latest_text = _latest_inbound_text(context)
        normalized = _normalized(latest_text)
        reason_code = str(getattr(decision, "reason_code", "") or "").strip().upper()
        message = str(getattr(decision, "message", "") or "").strip()

        if normalized in _SIMPLE_ACKS and reason_code == "UNKNOWN_INFORMATION":
            raise engagement_module.EngagementError(
                "A simple conversational acknowledgement must receive a natural acknowledgement, not an unknown-information fallback."
            )

        if (
            message
            and _UNCONFIRMED_FUTURE_PROMISE_RE.search(message)
            and not (getattr(decision, "crm_actions", None) or [])
        ):
            raise engagement_module.EngagementError(
                "Do not promise a future confirmation, callback, follow-up, or get-back unless this turn includes a validated backend action."
            )

        if (
            qualification_state.get("qualification_status") == "completed"
            and runtime.get("qualification_completion_ack_sent")
        ):
            previous_hash = str(runtime.get("qualification_completion_ack_hash") or "")
            if previous_hash and message and response_hash(message) == previous_hash:
                raise engagement_module.EngagementError(
                    "The qualification completion acknowledgement was already sent. Continue normal conversation without repeating it."
                )
            if str(getattr(decision, "next_requirement_id", "") or "").strip():
                raise engagement_module.EngagementError(
                    "Qualification is already complete; do not restart it."
                )

    EngagementService._validate_qualification_decision = validate
    EngagementService._shvya_natural_conversation_patch = True


def _find_stage_id(*, organization, names: tuple[str, ...]) -> str | None:
    wanted = {_normalized(name) for name in names}
    pipelines = organization.pipelines.filter(is_active=True).prefetch_related("stages")
    for pipeline in pipelines:
        for stage in pipeline.stages.all():
            if stage.is_active and _normalized(stage.name) in wanted:
                return str(stage.id)
    return None


def _patch_failsoft() -> None:
    from apps.ai_engagement.services import engagement_failsoft as failsoft

    if getattr(failsoft, "_shvya_natural_conversation_patch", False):
        return

    failsoft._SIMPLE_ACKS.update(_SIMPLE_ACKS)
    original_grounded = failsoft._grounded_conversation_reply
    original_builder = failsoft.build_deterministic_fallback_decision

    def grounded_conversation_reply(*, about: str, inbound: str, organization_name: str):
        normalized = _normalized(inbound)
        if normalized in _SIMPLE_ACKS:
            if normalized in {"thanks", "thank you"}:
                return "You’re welcome.", "NORMAL_CONVERSATION"
            if normalized in {"cool", "great", "perfect", "sounds good"}:
                return "Sounds good.", "NORMAL_CONVERSATION"
            if normalized in {"it's alright", "its alright", "no worries", "all good"}:
                return "Thanks for understanding.", "NORMAL_CONVERSATION"
            return "Got it.", "NORMAL_CONVERSATION"
        return original_grounded(
            about=about,
            inbound=inbound,
            organization_name=organization_name,
        )

    failsoft._grounded_conversation_reply = grounded_conversation_reply

    def build_deterministic_fallback_decision(*, organization, lead, latest_inbound=None):
        decision = original_builder(
            organization=organization,
            lead=lead,
            latest_inbound=latest_inbound,
        )
        if latest_inbound is None:
            latest_inbound = failsoft._latest_inbound_for_lead(
                organization=organization,
                lead=lead,
            )
        latest_text = str(getattr(latest_inbound, "body", "") or "").strip()
        normalized = _normalized(latest_text)

        if normalized in _SIMPLE_ACKS:
            message, reason = grounded_conversation_reply(
                about="",
                inbound=latest_text,
                organization_name=str(getattr(organization, "name", "") or ""),
            )
            return replace(
                decision,
                message=message,
                reason=reason,
                reason_code=reason,
                crm_actions=[],
            )

        missed_call = any(term in normalized for term in _MISSED_CALL_TERMS)
        call_request = bool(_CALL_REQUEST_RE.search(latest_text))
        if not missed_call and not call_request:
            return decision

        actions = list(decision.crm_actions or [])
        target_stage_id = None
        if missed_call:
            target_stage_id = _find_stage_id(
                organization=organization,
                names=("Human Intervention Needed", "Human Intervention", "Needs Human"),
            )
            actions.append({
                "type": "add_note",
                "note": "Lead reports that a previously booked call was missed and requests team follow-up.",
            })
        else:
            target_stage_id = _find_stage_id(
                organization=organization,
                names=("Call Requested", "Demo Requested"),
            )

        if target_stage_id:
            actions.append({
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": target_stage_id},
            })

        if call_request:
            from apps.ai_engagement.services.qualification_crm_action_runtime import (
                _parse_grounded_due_at,
            )

            due_at = _parse_grounded_due_at(latest_text)
            if due_at:
                actions.append({
                    "type": "create_reminder",
                    "title": "Follow up with lead",
                    "description": "Lead requested a call, demo, meeting, or follow-up.",
                    "due_at": due_at,
                })

        if missed_call and actions:
            message = "Sorry about that. I’ve flagged the missed booked call for the team to follow up with you."
            reason = "HUMAN_HANDOFF"
        elif call_request and actions:
            if any(item.get("type") == "create_reminder" for item in actions):
                message = "Sure — I’ve noted your preferred callback time. The team will still need to confirm the call."
            else:
                message = "Sure — I’ve noted that you’d like to speak with the team. They’ll still need to confirm the call."
            reason = "HUMAN_HANDOFF"
        else:
            return decision

        return replace(
            decision,
            message=message,
            reason=reason,
            reason_code=reason,
            crm_actions=actions,
        )

    failsoft.build_deterministic_fallback_decision = build_deterministic_fallback_decision
    failsoft._shvya_natural_conversation_patch = True


def install_natural_conversation_runtime() -> None:
    """Make Hosted + Meta WhatsApp engagement stateful without weakening backend control."""

    global _INSTALLED
    if _INSTALLED:
        return

    _patch_qualification_state()
    _patch_qualification_stage_guards()
    _patch_policy_actions()
    _patch_completion_ack_tracking()
    _patch_engagement_validation_and_prompt()
    _patch_failsoft()

    _INSTALLED = True
