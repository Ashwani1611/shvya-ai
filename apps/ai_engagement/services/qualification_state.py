from __future__ import annotations

import re
from copy import deepcopy

from django.utils import timezone


QUALIFICATION_STATE_KEY = "_shvya_ai_qualification"

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

RESULT_QUALIFIED = "qualified"
RESULT_NOT_QUALIFIED = "not_qualified"

MODE_QUALIFICATION = "qualification"
MODE_CONVERSATION = "conversation"

REQUIREMENT_UNKNOWN = "unknown"
REQUIREMENT_ANSWERED = "answered"
REQUIREMENT_UNCLEAR = "unclear"
REQUIREMENT_NOT_APPLICABLE = "not_applicable"
REQUIREMENT_STATUSES = {
    REQUIREMENT_UNKNOWN,
    REQUIREMENT_ANSWERED,
    REQUIREMENT_UNCLEAR,
    REQUIREMENT_NOT_APPLICABLE,
}

NEW_LEAD_STAGE = "new lead"
QUALIFIED_STAGE = "qualified"


_ACK_ONLY = {
    "ok",
    "okay",
    "thanks",
    "thank you",
    "great",
    "fine",
    "sure",
    "done",
    "correct",
    "right",
}
_UNCLEAR_ONLY = {
    "maybe",
    "not sure",
    "unsure",
    "i don't know",
    "i dont know",
    "don't know",
    "dont know",
}
_YES = {"yes", "y", "yeah", "yep", "yes please", "correct"}
_NO = {"no", "n", "nope", "not yet"}
_BOOLEAN_QUESTION_PREFIXES = (
    "is ",
    "are ",
    "do ",
    "does ",
    "did ",
    "have ",
    "has ",
    "can ",
    "could ",
    "would ",
    "will ",
    "was ",
    "were ",
)


def normalize_stage_name(value) -> str:
    name = str(value or "").strip().casefold()
    return NEW_LEAD_STAGE if name == "new leads" else name


def _qualified_stage(lead):
    pipeline = getattr(lead, "pipeline", None)
    if pipeline is None:
        return None

    for stage in pipeline.stages.filter(is_active=True).order_by(
        "display_order",
        "name",
    ):
        if normalize_stage_name(stage.name) == QUALIFIED_STAGE:
            return stage
    return None


def _raw_state(lead) -> dict:
    raw_attributes = getattr(lead, "attributes", {})
    attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    state = attributes.get(QUALIFICATION_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
    return deepcopy(state)


def _status(value) -> str:
    value = str(value or "").strip().casefold()
    if value in {
        STATUS_NOT_STARTED,
        STATUS_IN_PROGRESS,
        STATUS_COMPLETED,
    }:
        return value
    return STATUS_NOT_STARTED


def _result(value) -> str:
    value = str(value or "").strip().casefold()
    if value in {RESULT_QUALIFIED, RESULT_NOT_QUALIFIED}:
        return value
    return ""


def _requirement_state(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    status = str(raw.get("status") or REQUIREMENT_UNKNOWN).strip().casefold()
    if status not in REQUIREMENT_STATUSES:
        status = REQUIREMENT_UNKNOWN
    return {
        "status": status,
        "value": raw.get("value"),
        "confidence": str(raw.get("confidence") or "").strip(),
        "source_message_id": (
            str(raw.get("source_message_id"))
            if raw.get("source_message_id") is not None
            else None
        ),
        "updated_at": raw.get("updated_at"),
    }


def _normalized_requirement_states(state: dict, requirements=None) -> dict:
    raw_states = state.get("requirement_states")
    if not isinstance(raw_states, dict):
        raw_states = {}

    normalized = {
        str(requirement_id): _requirement_state(value)
        for requirement_id, value in raw_states.items()
        if str(requirement_id).strip()
    }

    for requirement in requirements or []:
        requirement_id = str(requirement.get("id") or "").strip()
        if requirement_id and requirement_id not in normalized:
            normalized[requirement_id] = _requirement_state({})
    return normalized


def _required_ids(requirements) -> list[str]:
    return [
        str(requirement.get("id") or "").strip()
        for requirement in requirements or []
        if requirement.get("required", True)
        and str(requirement.get("id") or "").strip()
    ]


def _missing_ids(requirements, requirement_states) -> list[str]:
    missing: list[str] = []
    for requirement_id in _required_ids(requirements):
        status = requirement_states.get(requirement_id, {}).get(
            "status", REQUIREMENT_UNKNOWN
        )
        if status in {REQUIREMENT_UNKNOWN, REQUIREMENT_UNCLEAR}:
            missing.append(requirement_id)
    return missing


def next_requirement(requirements, requirement_states) -> dict | None:
    ordered = sorted(
        [r for r in requirements or [] if str(r.get("id") or "").strip()],
        key=lambda item: (int(item.get("priority") or 999999), str(item.get("id"))),
    )
    for requirement in ordered:
        if not requirement.get("required", True):
            continue
        state = requirement_states.get(str(requirement.get("id")), {})
        if state.get("status", REQUIREMENT_UNKNOWN) in {
            REQUIREMENT_UNKNOWN,
            REQUIREMENT_UNCLEAR,
        }:
            return deepcopy(requirement)
    return None


def state_for_lead(lead, requirements=None) -> dict:
    """Return normalized application-controlled qualification state.

    The state remains embedded in Lead.attributes for backwards compatibility,
    but now tracks each organization-defined requirement independently so the
    application can choose the next requirement without another model call.
    """
    state = _raw_state(lead)
    status = _status(state.get("qualification_status"))
    result = _result(state.get("qualification_result"))
    stage_name = normalize_stage_name(
        getattr(getattr(lead, "stage", None), "name", "")
    )
    qualified_stage = _qualified_stage(lead)
    requirement_states = _normalized_requirement_states(state, requirements)
    missing = _missing_ids(requirements, requirement_states)
    next_item = next_requirement(requirements, requirement_states)

    engagement_mode = (
        MODE_QUALIFICATION
        if stage_name == NEW_LEAD_STAGE and status != STATUS_COMPLETED
        else MODE_CONVERSATION
    )

    return {
        "qualification_status": status,
        "qualification_result": result,
        "qualification_completed_at": state.get("qualification_completed_at"),
        "engagement_mode": engagement_mode,
        "requirement_states": requirement_states,
        "missing_requirement_ids": missing,
        "all_requirements_answered": bool(requirements) and not missing,
        "next_requirement_id": (
            str(next_item.get("id")) if next_item is not None else None
        ),
        "last_asked_requirement_id": (
            str(state.get("last_asked_requirement_id"))
            if state.get("last_asked_requirement_id")
            else None
        ),
        "qualified_stage_id": (
            str(qualified_stage.id)
            if qualified_stage is not None
            else None
        ),
        "qualified_stage_name": (
            qualified_stage.name
            if qualified_stage is not None
            else None
        ),
    }


def attributes_with_state(lead, state: dict) -> dict:
    raw_attributes = getattr(lead, "attributes", {})
    attributes = deepcopy(raw_attributes) if isinstance(raw_attributes, dict) else {}
    attributes[QUALIFICATION_STATE_KEY] = deepcopy(state)
    return attributes


def _persist_state(lead, state: dict) -> dict:
    attributes = attributes_with_state(lead, state)
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
    lead.attributes = attributes
    return state


def ensure_state(lead, requirements=None) -> dict:
    """Persist normalized state when it is missing or stale."""
    state = state_for_lead(lead, requirements=requirements)
    attributes = attributes_with_state(lead, state)
    raw_attributes = getattr(lead, "attributes", {})
    current_attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    if attributes != current_attributes:
        lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
        lead.attributes = attributes
    return state


def state_after_stage_change(*, lead, old_stage_name: str, new_stage_name: str) -> dict:
    """Return durable qualification state after a CRM stage transition."""
    state = state_for_lead(lead)
    old_name = normalize_stage_name(old_stage_name)
    new_name = normalize_stage_name(new_stage_name)

    if state["qualification_status"] != STATUS_COMPLETED:
        if new_name == QUALIFIED_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_result"] = RESULT_QUALIFIED
            state["qualification_completed_at"] = timezone.now().isoformat()
        elif old_name == NEW_LEAD_STAGE and new_name != NEW_LEAD_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_completed_at"] = timezone.now().isoformat()

    state["engagement_mode"] = (
        MODE_QUALIFICATION
        if new_name == NEW_LEAD_STAGE
        and state["qualification_status"] != STATUS_COMPLETED
        else MODE_CONVERSATION
    )

    return state


def mark_in_progress(lead) -> dict:
    """Mark the first customer-facing qualification exchange as started."""
    state = state_for_lead(lead)
    stage_name = normalize_stage_name(
        getattr(getattr(lead, "stage", None), "name", "")
    )

    if (
        stage_name == NEW_LEAD_STAGE
        and state["qualification_status"] == STATUS_NOT_STARTED
    ):
        state["qualification_status"] = STATUS_IN_PROGRESS
        state["engagement_mode"] = MODE_QUALIFICATION
        _persist_state(lead, state)

    return state


def record_last_asked_requirement(
    lead,
    requirement_id: str | None,
    *,
    requirements=None,
) -> dict:
    """Record which qualification requirement the queued SHVYA reply asked."""
    requirement_id = str(requirement_id or "").strip()
    if not requirement_id:
        return state_for_lead(lead, requirements=requirements)
    state = state_for_lead(lead, requirements=requirements)
    state["last_asked_requirement_id"] = requirement_id
    if state["qualification_status"] == STATUS_NOT_STARTED:
        state["qualification_status"] = STATUS_IN_PROGRESS
    return _persist_state(lead, state)


def _is_boolean_question(question: str) -> bool:
    normalized = " ".join(str(question or "").strip().casefold().split())
    return normalized.startswith(_BOOLEAN_QUESTION_PREFIXES) or " whether " in (
        f" {normalized} "
    )


def _question_has_any(question: str, terms: tuple[str, ...]) -> bool:
    normalized = " ".join(str(question or "").strip().casefold().split())
    return any(term in normalized for term in terms)


def _looks_like_numeric_value(text: str) -> bool:
    normalized = str(text or "").strip().casefold()
    return bool(
        re.search(r"\d", normalized)
        and re.fullmatch(
            r"[\s₹$€£+\-.,:/a-z0-9]*(?:crore|cr|lakh|lac|k|m|million|thousand|years?|months?|days?|weeks?)?[\s₹$€£+\-.,:/a-z0-9]*",
            normalized,
        )
    )


def _looks_like_timeline_value(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().casefold().split())
    if re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", normalized):
        return True
    return bool(
        re.search(
            r"\b(?:today|tomorrow|tonight|this week|next week|this month|next month|immediately|asap|soon|within\s+\d+\s+(?:day|days|week|weeks|month|months)|\d+\s+(?:day|days|week|weeks|month|months))\b",
            normalized,
        )
    )


def _classify_direct_reply(*, text: str, question: str) -> tuple[str, object, str] | None:
    """Classify only values whose type is unambiguous from the asked question."""
    raw_text = str(text or "").strip()
    normalized = " ".join(raw_text.casefold().split())
    if not normalized or len(normalized) > 80 or "?" in normalized:
        return None
    if normalized in _ACK_ONLY:
        return None
    if normalized in _UNCLEAR_ONLY:
        return (REQUIREMENT_UNCLEAR, raw_text, "high")

    if normalized in _YES | _NO:
        if not _is_boolean_question(question):
            return None
        return (
            REQUIREMENT_ANSWERED,
            normalized in _YES,
            "high",
        )

    if _question_has_any(
        question,
        (
            "budget",
            "price range",
            "amount",
            "how much",
            "age",
            "quantity",
            "how many",
            "count",
        ),
    ) and _looks_like_numeric_value(raw_text):
        return (REQUIREMENT_ANSWERED, raw_text, "high")

    if _question_has_any(
        question,
        (
            "timeline",
            "when",
            "date",
            "time frame",
            "timeframe",
            "how soon",
            "purchase by",
            "start by",
        ),
    ) and _looks_like_timeline_value(raw_text):
        return (REQUIREMENT_ANSWERED, raw_text, "high")

    if _question_has_any(question, ("email", "e-mail")) and re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+", raw_text
    ):
        return (REQUIREMENT_ANSWERED, raw_text, "high")

    if _question_has_any(
        question,
        ("phone", "mobile", "contact number", "whatsapp number"),
    ):
        digits = re.sub(r"\D", "", raw_text)
        if 7 <= len(digits) <= 15:
            return (REQUIREMENT_ANSWERED, raw_text, "high")

    # Free-form values such as a city, product choice or occupation remain on
    # the LLM path because matching them safely requires semantic understanding.
    return None


def apply_unambiguous_reply(
    *,
    lead,
    requirements,
    text: str,
    source_message_id: str | None,
) -> dict:
    """Persist an obvious answer to the last asked qualification requirement.

    This zero-LLM path is intentionally conservative. It never guesses which
    requirement a reply belongs to and never handles multi-intent/question
    messages. Ambiguous turns stay on the normal engagement model path.
    """
    state = state_for_lead(lead, requirements=requirements)
    last_id = str(state.get("last_asked_requirement_id") or "").strip()
    if not last_id:
        return {"changed": False, "state": state, "answer_status": None}

    requirement = next(
        (
            item
            for item in requirements or []
            if str(item.get("id") or "") == last_id
        ),
        None,
    )
    if requirement is None:
        return {"changed": False, "state": state, "answer_status": None}

    current = state["requirement_states"].get(last_id, _requirement_state({}))
    if current.get("status") in {
        REQUIREMENT_ANSWERED,
        REQUIREMENT_NOT_APPLICABLE,
    }:
        return {
            "changed": False,
            "state": state,
            "answer_status": current.get("status"),
        }

    classified = _classify_direct_reply(
        text=text,
        question=str(requirement.get("question") or requirement.get("label") or ""),
    )
    if classified is None:
        return {"changed": False, "state": state, "answer_status": None}

    answer_status, value, confidence = classified
    state["requirement_states"][last_id] = {
        "status": answer_status,
        "value": value,
        "confidence": confidence,
        "source_message_id": str(source_message_id) if source_message_id else None,
        "updated_at": timezone.now().isoformat(),
    }
    if state["qualification_status"] == STATUS_NOT_STARTED:
        state["qualification_status"] = STATUS_IN_PROGRESS

    missing = _missing_ids(requirements, state["requirement_states"])
    next_item = next_requirement(requirements, state["requirement_states"])
    state["missing_requirement_ids"] = missing
    state["all_requirements_answered"] = bool(requirements) and not missing
    state["next_requirement_id"] = (
        str(next_item.get("id")) if next_item is not None else None
    )
    _persist_state(lead, state)
    return {
        "changed": True,
        "state": state,
        "answer_status": answer_status,
        "next_requirement": next_item,
    }


def reset_state(lead) -> dict:
    """Explicit reset hook. Normal stage changes and reconnects never call it."""
    state = state_for_lead(lead)
    state.update(
        {
            "qualification_status": STATUS_NOT_STARTED,
            "qualification_result": "",
            "qualification_completed_at": None,
            "requirement_states": {},
            "missing_requirement_ids": [],
            "all_requirements_answered": False,
            "next_requirement_id": None,
            "last_asked_requirement_id": None,
        }
    )
    stage_name = normalize_stage_name(
        getattr(getattr(lead, "stage", None), "name", "")
    )
    state["engagement_mode"] = (
        MODE_QUALIFICATION
        if stage_name == NEW_LEAD_STAGE
        else MODE_CONVERSATION
    )
    return _persist_state(lead, state)


def project_answer_updates(*, state, requirements, updates, messages):
    """Validate semantic answers against supplied inbound evidence, without writes."""
    result = deepcopy(state)
    allowed = {str(item["id"]) for item in requirements}
    sources = {str(m.get("id")): str(m.get("body") or "")
               for m in messages if m.get("direction") == "inbound"}
    if not isinstance(updates, list) or len(updates) > len(allowed):
        raise ValueError("Invalid qualification updates.")
    seen = set()
    for update in updates:
        if not isinstance(update, dict) or set(update) != {
            "requirement_id", "value", "source_message_id", "evidence",
        }:
            raise ValueError("Invalid qualification answer schema.")
        requirement_id = update["requirement_id"]
        source_id = update["source_message_id"]
        evidence = update["evidence"]
        value = update["value"]
        if (not isinstance(requirement_id, str) or requirement_id not in allowed
                or requirement_id in seen or not isinstance(source_id, str)
                or not isinstance(evidence, str) or not evidence.strip()
                or evidence not in sources.get(source_id, "")
                or not isinstance(value, (str, int, float, bool))
                or (isinstance(value, str) and not value.strip())):
            raise ValueError("Qualification answer lacks valid inbound evidence.")
        seen.add(requirement_id)
        result["requirement_states"][requirement_id] = {
            "status": REQUIREMENT_ANSWERED, "value": value,
            "source_message_id": source_id, "confidence": "supported",
            "updated_at": timezone.now().isoformat(),
        }
    result["missing_requirement_ids"] = _missing_ids(requirements, result["requirement_states"])
    result["all_requirements_answered"] = bool(requirements) and not result["missing_requirement_ids"]
    return result


def persist_answer_updates(*, lead, updates):
    """Called under the finalization lead lock, after conversation freshness checks."""
    if not updates:
        return
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements

    org_info = OrgInfo.objects.filter(organization_id=lead.organization_id).first()
    requirements = compile_qualification_requirements(
        org_info.qualification_requirements if org_info else "",
    )["requirements"]
    messages = list(lead.whatsapp_messages.filter(
        organization_id=lead.organization_id,
        id__in=[item["source_message_id"] for item in updates],
        direction="inbound",
    ).values("id", "body", "direction"))
    state = project_answer_updates(
        state=state_for_lead(lead, requirements=requirements),
        requirements=requirements, updates=updates, messages=messages,
    )
    _persist_state(lead, state)
