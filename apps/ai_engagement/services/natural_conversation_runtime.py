from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from functools import wraps


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

    def grounded_conversation_reply(*, about: str, inbound: str, organization_name: str, bot_languages=""):
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
            bot_languages=bot_languages,
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
            from apps.ai_engagement.services.reminder_time_runtime import (
                parse_grounded_due_at,
            )

            due_at = parse_grounded_due_at(latest_text)
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


# Customer-chat regression hardening belongs to the natural conversation
# runtime; keep its separate installer only to preserve bootstrap ordering.
_CUSTOMER_CHAT_INSTALLED = False
_INTERNAL_QUESTION_LABEL_RE = re.compile(
    r"^\s*Q\s*\d{1,3}\s*[\)\].:\-]\s*",
    flags=re.IGNORECASE,
)
_INTERNAL_QUESTION_LABEL_IN_MESSAGE_RE = re.compile(
    r"(^|[.!?]\s+)Q\s*\d{1,3}\s*[\)\].:\-]\s*",
    flags=re.IGNORECASE,
)
_INFORMATION_NOUN_TERMS = (
    "functionality",
    "functionalities",
    "function",
    "functions",
    "feature",
    "features",
    "capability",
    "capabilities",
    "benefit",
    "benefits",
    "integration",
    "integrations",
    "pricing",
    "price",
    "cost",
    "plan",
    "plans",
    "service",
    "services",
    "product",
    "products",
    "use case",
    "use cases",
    "details",
    "information",
)
_YES_VARIANTS = {
    "yes",
    "y",
    "yeah",
    "yep",
    "ye",
    "yea",
    "ya",
    "yup",
    "yes please",
    "correct",
}
_NO_VARIANTS = {"no", "n", "nope", "nah", "not yet"}


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_internal_question_label(value: str) -> str:
    """Remove authored internal labels such as Q1./Q2) from customer text."""
    return _INTERNAL_QUESTION_LABEL_RE.sub("", str(value or ""), count=1).strip()


def _strip_internal_labels_from_message(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _INTERNAL_QUESTION_LABEL_IN_MESSAGE_RE.sub(
        lambda match: match.group(1),
        text,
    ).strip()


def _strip_outer_customer_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 2:
        return text
    pairs = {
        '"': '"',
        "'": "'",
        "“": "”",
        "‘": "’",
    }
    closing = pairs.get(text[0])
    if closing and text.endswith(closing):
        inner = text[1:-1].strip()
        if inner:
            return inner
    return text


def sanitize_customer_message(value: str, *, qualification_context: bool = False) -> str:
    """Normalize formatting leakage without changing authored answer content."""
    text = _strip_outer_customer_quotes(value)
    if qualification_context:
        text = _strip_internal_labels_from_message(text)
    return text.strip()


def _information_noun_phrase(value: str) -> bool:
    normalized = _clean(value).casefold().strip(" .!?;:\"'()[]{}")
    if not normalized or len(normalized) > 180:
        return False
    words = normalized.split()
    if len(words) > 16:
        return False
    return any(term in normalized for term in _INFORMATION_NOUN_TERMS)


def _sanitize_requirement(value):
    if not isinstance(value, dict):
        return value
    item = deepcopy(value)
    if item.get("question") is not None:
        item["question"] = strip_internal_question_label(item.get("question"))
    if item.get("label") is not None:
        item["label"] = strip_internal_question_label(item.get("label"))
    return item


def _patch_requirement_compiler() -> None:
    from apps.ai_engagement.services import organization_profile

    if getattr(organization_profile, "_shvya_customer_label_patch", False):
        return

    original_clean = organization_profile._clean_requirement_line

    def clean_requirement_line(value: str) -> str:
        return strip_internal_question_label(original_clean(value))

    organization_profile._clean_requirement_line = clean_requirement_line
    organization_profile._shvya_customer_label_patch = True


def _patch_active_answer_normalization() -> None:
    from apps.ai_engagement.services import qualification_state

    if getattr(qualification_state, "_shvya_short_boolean_patch", False):
        return

    original_classifier = qualification_state._classify_direct_reply

    def classify(*, text: str, question: str):
        result = original_classifier(text=text, question=question)
        if result is not None:
            return result

        options = qualification_state._question_options(question)
        if not options:
            return None
        by_value = {
            _clean(item.get("value")).casefold(): _clean(item.get("value"))
            for item in options
            if _clean(item.get("value"))
        }
        if set(by_value) != {"yes", "no"}:
            return None

        normalized = _clean(text).casefold().strip(" .,:;!?()[]{}\"'")
        if normalized in _YES_VARIANTS:
            return (
                qualification_state.REQUIREMENT_ANSWERED,
                by_value["yes"],
                "high",
            )
        if normalized in _NO_VARIANTS:
            return (
                qualification_state.REQUIREMENT_ANSWERED,
                by_value["no"],
                "high",
            )
        return None

    qualification_state._classify_direct_reply = classify
    qualification_state._shvya_short_boolean_patch = True


def _patch_intent_priority() -> None:
    from apps.ai_engagement.services import conversation_priority_runtime

    if getattr(conversation_priority_runtime, "_shvya_noun_intent_patch", False):
        return

    original_intent_kind = conversation_priority_runtime._intent_kind

    def intent_kind(text: str) -> str:
        kind = original_intent_kind(text)
        if kind != "none":
            return kind
        if _information_noun_phrase(text):
            return "question"
        return "none"

    conversation_priority_runtime._intent_kind = intent_kind
    conversation_priority_runtime._shvya_noun_intent_patch = True


def _patch_engagement_runtime() -> None:
    from apps.ai_engagement.services.engagement import EngagementService

    if getattr(EngagementService, "_shvya_customer_chat_regression_patch", False):
        return

    original_should_retrieve = EngagementService._should_retrieve_knowledge
    original_build_input = EngagementService._build_input
    original_engage = EngagementService.engage

    def should_retrieve_knowledge(self, *, context):
        latest = self._latest_inbound_text(context=context)
        if _information_noun_phrase(latest):
            return True
        return original_should_retrieve(self, context=context)

    def build_input(self, *, context, **kwargs):
        raw = original_build_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw

        turn = payload.get("qualification_turn")
        if isinstance(turn, dict):
            for key in (
                "current_requirement",
                "next_requirement_if_current_answered",
            ):
                turn[key] = _sanitize_requirement(turn.get(key))
            payload["qualification_turn"] = turn

        if isinstance(payload.get("next_requirement"), dict):
            payload["next_requirement"] = _sanitize_requirement(
                payload["next_requirement"]
            )

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @wraps(original_engage)
    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        decision = original_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        message = str(getattr(decision, "message", "") or "")
        qualification_context = bool(
            str(getattr(decision, "next_requirement_id", "") or "").strip()
            or str(getattr(decision, "reason_code", "") or "").strip().upper()
            in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}
        )
        sanitized = sanitize_customer_message(
            message,
            qualification_context=qualification_context,
        )
        if sanitized != message.strip():
            return replace(decision, message=sanitized)
        return decision

    EngagementService._should_retrieve_knowledge = should_retrieve_knowledge
    EngagementService._build_input = build_input
    EngagementService.engage = engage
    EngagementService._shvya_customer_chat_regression_patch = True


def _patch_failsoft_capability_intent() -> None:
    from apps.ai_engagement.services import engagement_failsoft

    capability_terms = tuple(engagement_failsoft._CAPABILITY_TERMS)
    for term in ("function", "functions", "functionality", "functionalities"):
        if term not in capability_terms:
            capability_terms += (term,)
    engagement_failsoft._CAPABILITY_TERMS = capability_terms

    if getattr(engagement_failsoft, "_shvya_customer_chat_regression_patch", False):
        return

    original_builder = engagement_failsoft.build_deterministic_fallback_decision

    @wraps(original_builder)
    def build_deterministic_fallback_decision(*args, **kwargs):
        decision = original_builder(*args, **kwargs)
        message = str(getattr(decision, "message", "") or "")
        qualification_context = bool(
            str(getattr(decision, "next_requirement_id", "") or "").strip()
            or str(getattr(decision, "reason_code", "") or "").strip().upper()
            in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}
        )
        sanitized = sanitize_customer_message(
            message,
            qualification_context=qualification_context,
        )
        if sanitized != message.strip():
            return replace(decision, message=sanitized)
        return decision

    engagement_failsoft.build_deterministic_fallback_decision = (
        build_deterministic_fallback_decision
    )
    engagement_failsoft._shvya_customer_chat_regression_patch = True


def install_customer_chat_regressions() -> None:
    """Install narrow fixes for observed natural qualification chat failures."""
    global _CUSTOMER_CHAT_INSTALLED
    if _CUSTOMER_CHAT_INSTALLED:
        return

    _patch_requirement_compiler()
    _patch_active_answer_normalization()
    _patch_intent_priority()
    _patch_engagement_runtime()
    _patch_failsoft_capability_intent()
    _CUSTOMER_CHAT_INSTALLED = True
