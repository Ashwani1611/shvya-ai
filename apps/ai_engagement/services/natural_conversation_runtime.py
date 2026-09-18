from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace


_INSTALLED = False
_SIMPLE_ACKS = {
    "ok", "okay", "sure", "cool", "great", "fine", "thanks", "thank you",
    "got it", "understood", "alright", "all right", "it's alright", "its alright",
    "no worries", "all good", "perfect", "sounds good",
}
_BOOKING_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:already\s+)?(?:booked|scheduled)\b",
    flags=re.IGNORECASE,
)

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
NATURAL CONVERSATION SAFEGUARDS
- Qualification stage scope, sequence, completion, and stage movement are owned by
  backend state. Do not broaden qualification into another CRM stage.
- Qualification requirements are data requirements, not a customer-facing script.
  Respond to the lead's actual intent first, then continue only with the one
  backend-authorized unanswered qualification question when qualification is active.
- Never repeat the qualification completion acknowledgement after the backend marks
  qualification_completion_ack_sent=true.
- Treat short social acknowledgements such as okay, sure, cool, thanks, alright,
  perfect, and "it's alright" as normal conversation. Never answer them with an
  unknown-information fallback.
- After qualification is complete, continue as a normal sales assistant. A greeting,
  acknowledgement, pricing question, call request, complaint, or later message must
  never restart qualification.
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

    if _NATURAL_CONVERSATION_INSTRUCTIONS not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n"
            f"{_NATURAL_CONVERSATION_INSTRUCTIONS}"
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

        booking_claim = bool(_BOOKING_CLAIM_RE.search(latest_text))
        missed_call = any(term in normalized for term in _MISSED_CALL_TERMS)
        if booking_claim and not missed_call:
            return replace(
                decision,
                message=(
                    "Got it. I’ll treat that as a booking you’ve reported, but I can’t "
                    "confirm the appointment unless the booking system confirms it."
                ),
                reason="NORMAL_CONVERSATION",
                reason_code="NORMAL_CONVERSATION",
                crm_actions=[],
            )

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
    """Install natural WhatsApp safeguards without changing qualification scope."""

    global _INSTALLED
    if _INSTALLED:
        return

    _patch_completion_ack_tracking()
    _patch_engagement_validation_and_prompt()
    _patch_failsoft()

    _INSTALLED = True
