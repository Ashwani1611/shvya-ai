"""Backend-owned conversation contract; model output cannot write this state."""
from copy import deepcopy
import hashlib
import json
import re


STATE_KEY = "_shvya_ai_runtime"
TERMINAL_REQUIREMENTS = {"answered", "not_applicable", "skipped"}


def _semantic_state(value):
    # Audit bookkeeping is allowed to change while a response is generated.
    # Answers, IDs, status, flow snapshots and operational facts are not.
    if isinstance(value, dict):
        return {key: _semantic_state(item) for key, item in value.items()
                if key not in {"history", "asked_at", "updated_at", "created_at"}}
    if isinstance(value, list):
        return [_semantic_state(item) for item in value]
    return value


def state_revision(lead):
    attributes = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    return response_hash(json.dumps(_semantic_state({
        "qualification": attributes.get("_shvya_ai_qualification"),
        "runtime": attributes.get(STATE_KEY),
        "stage": str(getattr(lead, "stage_id", "")),
    }), sort_keys=True, default=str))


def response_hash(message):
    return hashlib.sha256(str(message).strip().encode("utf-8")).hexdigest()


def observe_message(saved, text):
    """Conservative explicit intents; plain yes/no never reset a workflow."""
    result = deepcopy(saved or {})
    normalized = " ".join(str(text).casefold().split()).rstrip(".! ")
    if re.search(r"\bi (?:have )?already (?:booked|scheduled)\b", normalized):
        if result.get("booking_status") != "confirmed":
            result["booking_status"] = "user_reported"
        if result.get("active_interaction") == "booking":
            result["active_interaction_status"] = "completed"
    if normalized in {"not now", "later", "i'm busy", "i am busy"}:
        if result.get("conversation_mode") != "paused":
            result["resume_mode"] = result.get("conversation_mode", "qualification")
        result["conversation_mode"] = "paused"
    elif normalized in {"continue", "resume", "let's continue"} and result.get("conversation_mode") == "paused":
        result["conversation_mode"] = result.pop("resume_mode", "conversation")
    if normalized in {"stop", "unsubscribe", "do not contact me", "please do not contact me"}:
        result["conversation_mode"] = "opt_out"
        result["active_interaction_status"] = "declined"
    if normalized in {"no", "no thanks"} and result.get("active_interaction") == "booking":
        result["booking_status"] = "declined"
        result["active_interaction_status"] = "declined"
    return result


def contract(*, qualification, requirements, saved=None, organization_id=""):
    saved = deepcopy(saved) if isinstance(saved, dict) else {}
    from apps.ai_engagement.services.qualification_state import next_requirement
    states = qualification.get("requirement_states", {})
    complete = qualification.get("qualification_status") == "completed"
    next_item = next_requirement(requirements, states)
    mode = qualification.get("conversation_mode") or qualification.get("engagement_mode", "conversation")
    if saved.get("conversation_mode") in {"paused", "opt_out"}:
        mode = saved["conversation_mode"]
    current = str(next_item["id"]) if next_item and not complete and mode in {"qualification", "qualifying"} else None
    booking_status = saved.get("booking_status", "unknown")
    confirmation = saved.get("booking_confirmation")
    if booking_status == "confirmed" and not confirmation:
        booking_status = "unverified"
    return {
        **saved,
        "current_requirement_id": current,
        "answered_requirements": {key: value.get("value") for key, value in states.items() if value.get("status") == "answered"},
        "not_applicable_requirements": [key for key, value in states.items() if value.get("status") == "not_applicable"],
        "skipped_requirements": [key for key, value in states.items() if value.get("status") == "skipped"],
        "qualification_status": "completed" if complete else qualification.get("qualification_status", "not_started"),
        "conversation_mode": "qualified" if complete and mode in {"qualification", "qualifying"} else mode,
        "active_interaction": saved.get("active_interaction"),
        "active_interaction_status": saved.get("active_interaction_status", "none"),
        "booking_status": booking_status,
        "booking_confirmation": confirmation,
        "scheduled_time": saved.get("scheduled_time"),
        "flow_id": saved.get("flow_id") or f"qualification:{organization_id}",
        "flow_version": hashlib.sha256(json.dumps(requirements, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "stable_requirement_id": (next_item.get("stable_id") or current) if current else None,
    }


def validate_response(*, decision, runtime, requirements):
    """Hard structural checks supplement independent semantic validation."""
    selected = getattr(decision, "next_requirement_id", None)
    if selected and selected != runtime.get("current_requirement_id"):
        raise ValueError("Response asks a requirement outside the backend's next valid action.")
    if selected:
        item = next(item for item in requirements if str(item["id"]) == selected)
        message = str(decision.message)
        positions = [message.find(str(option.get("value", "")) if isinstance(option, dict) else str(option)) for option in item.get("options", [])]
        if any(position < 0 for position in positions) or positions != sorted(positions):
            raise ValueError("Response must preserve every configured option in order.")
    if not getattr(decision, "should_engage", False) and selected:
        raise ValueError("A silent decision cannot open a customer interaction.")


def finalize_runtime(*, lead, decision, qualification, requirements, message_id):
    """Call under the lead row lock in the same transaction as outbound creation."""
    attributes = deepcopy(lead.attributes or {})
    state = contract(qualification=qualification, requirements=requirements,
                     saved=attributes.get(STATE_KEY), organization_id=lead.organization_id)
    selected = getattr(decision, "next_requirement_id", None)
    active = state.get("active_interaction")
    if selected:
        state.update(active_interaction=selected, active_interaction_status="pending")
    elif active in state["answered_requirements"] or active in state["not_applicable_requirements"] or active in state["skipped_requirements"]:
        state.update(active_interaction=None, active_interaction_status="completed")
    state.update(message_id=str(message_id), processed=True,
                 response_hash=response_hash(decision.message))
    attributes[STATE_KEY] = state
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
    lead.attributes = attributes
    return state
