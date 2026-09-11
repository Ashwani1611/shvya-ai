from __future__ import annotations

import re
from copy import deepcopy

from django.utils import timezone


QUALIFICATION_STATE_KEY = "_shvya_ai_qualification"
STATE_VERSION = 2
MAX_HISTORY_EVENTS = 200
MAX_PROCESSED_MESSAGE_IDS = 100

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

RESULT_QUALIFIED = "qualified"
RESULT_NOT_QUALIFIED = "not_qualified"

MODE_QUALIFICATION = "qualification"
MODE_CONVERSATION = "conversation"

REQUIREMENT_UNKNOWN = "unknown"
REQUIREMENT_ASKED = "asked"
REQUIREMENT_ANSWERED = "answered"
REQUIREMENT_UNCLEAR = "unclear"
REQUIREMENT_SKIPPED = "skipped"
REQUIREMENT_NOT_APPLICABLE = "not_applicable"
REQUIREMENT_STATUSES = {
    REQUIREMENT_UNKNOWN,
    REQUIREMENT_ASKED,
    REQUIREMENT_ANSWERED,
    REQUIREMENT_UNCLEAR,
    REQUIREMENT_SKIPPED,
    REQUIREMENT_NOT_APPLICABLE,
}

NEW_LEAD_STAGE = "new lead"
QUALIFIED_STAGE = "qualified"

_ACK_ONLY = {
    "ok", "okay", "thanks", "thank you", "great", "fine", "sure", "done",
    "correct", "right", "got it", "understood",
}
_UNCLEAR_ONLY = {
    "maybe", "not sure", "unsure", "i don't know", "i dont know",
    "don't know", "dont know",
}
_YES = {"yes", "y", "yeah", "yep", "yes please", "correct"}
_NO = {"no", "n", "nope", "not yet"}
_BOOLEAN_QUESTION_PREFIXES = (
    "is ", "are ", "do ", "does ", "did ", "have ", "has ", "can ",
    "could ", "would ", "will ", "was ", "were ",
)
_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+(?P<value>.+?)\s*$"
)
_QUESTION_START_RE = re.compile(
    r"^(?:what|which|where|who|how|select|choose|share|tell)\b", re.IGNORECASE
)
_FREEFORM_TERMS = (
    "city", "location", "occupation", "profession", "course", "product",
    "service", "requirement", "name", "company", "destination", "country",
    "state", "industry", "role", "designation", "plan", "package", "model",
    "size", "type", "purpose", "challenge", "problem", "issue", "goal",
)
_TYPED_TERMS = (
    "budget", "price range", "amount", "how much", "age", "quantity",
    "how many", "count", "timeline", "when", "date", "time frame",
    "timeframe", "how soon", "purchase by", "start by", "email", "e-mail",
    "phone", "mobile", "contact number", "whatsapp number",
)
_INTERRUPT_TERMS = (
    "booked a call", "book a call", "schedule a call", "call me", "call back",
    "speak with", "talk to", "human", "agent", "support", "demo booked",
)
_CORRECTION_TERMS = (
    "actually", "correction", "correct that", "change my", "change it",
    "instead", "i meant", "update my", "replace my",
)


def normalize_stage_name(value) -> str:
    name = str(value or "").strip().casefold()
    return NEW_LEAD_STAGE if name == "new leads" else name


def _qualified_stage(lead):
    pipeline = getattr(lead, "pipeline", None)
    if pipeline is None:
        return None
    for stage in pipeline.stages.filter(is_active=True).order_by("display_order", "name"):
        if normalize_stage_name(stage.name) == QUALIFIED_STAGE:
            return stage
    return None


def _raw_state(lead) -> dict:
    raw_attributes = getattr(lead, "attributes", {})
    attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    state = attributes.get(QUALIFICATION_STATE_KEY)
    return deepcopy(state) if isinstance(state, dict) else {}


def _status(value) -> str:
    value = str(value or "").strip().casefold()
    if value in {STATUS_NOT_STARTED, STATUS_IN_PROGRESS, STATUS_COMPLETED}:
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
        "raw_answer": raw.get("raw_answer"),
        "confidence": str(raw.get("confidence") or "").strip(),
        "source_message_id": str(raw.get("source_message_id")) if raw.get("source_message_id") is not None else None,
        "asked_at": raw.get("asked_at"),
        "updated_at": raw.get("updated_at"),
    }


def _flow_version(requirements) -> str:
    for item in requirements or []:
        value = str(item.get("flow_version") or "").strip()
        if value:
            return value
    return ""


def _snapshot(requirements) -> list[dict]:
    return deepcopy([item for item in requirements or [] if isinstance(item, dict)])


def requirements_for_lead(lead, current_requirements=None) -> list[dict]:
    """Pin a live questionnaire to the version active when qualification started."""
    state = _raw_state(lead)
    snapshot = state.get("flow_snapshot")
    if (
        _status(state.get("qualification_status")) in {STATUS_IN_PROGRESS, STATUS_COMPLETED}
        and isinstance(snapshot, list)
        and snapshot
    ):
        return _snapshot(snapshot)
    return _snapshot(current_requirements)


def _aliases(requirement: dict) -> set[str]:
    values = {
        str(requirement.get("id") or "").strip(),
        str(requirement.get("stable_id") or "").strip(),
    }
    for value in requirement.get("legacy_ids") or []:
        values.add(str(value or "").strip())
    values.discard("")
    return values


def _canonical_requirement_id(requirement_id, requirements) -> str:
    supplied = str(requirement_id or "").strip()
    if not supplied:
        return ""
    for requirement in requirements or []:
        if supplied in _aliases(requirement):
            return str(requirement.get("id") or supplied).strip()
    return supplied


def _normalized_requirement_states(state: dict, requirements=None) -> dict:
    raw_states = state.get("requirement_states")
    raw_states = raw_states if isinstance(raw_states, dict) else {}
    normalized: dict[str, dict] = {}

    for requirement in requirements or []:
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        source = None
        for alias in _aliases(requirement):
            if alias in raw_states:
                source = raw_states[alias]
                break
        normalized[requirement_id] = _requirement_state(source or {})

    for requirement_id, value in raw_states.items():
        key = str(requirement_id or "").strip()
        if key and key not in normalized:
            normalized[key] = _requirement_state(value)
    return normalized


def _required_ids(requirements) -> list[str]:
    return [
        str(requirement.get("id") or "").strip()
        for requirement in requirements or []
        if requirement.get("required", True) and str(requirement.get("id") or "").strip()
    ]


def _missing_ids(requirements, requirement_states) -> list[str]:
    missing: list[str] = []
    for requirement_id in _required_ids(requirements):
        status = requirement_states.get(requirement_id, {}).get("status", REQUIREMENT_UNKNOWN)
        if status in {REQUIREMENT_UNKNOWN, REQUIREMENT_ASKED, REQUIREMENT_UNCLEAR}:
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
            REQUIREMENT_UNKNOWN, REQUIREMENT_ASKED, REQUIREMENT_UNCLEAR,
        }:
            return deepcopy(requirement)
    return None


def _answered_ids(requirements, requirement_states) -> list[str]:
    return [
        str(item.get("id"))
        for item in requirements or []
        if requirement_states.get(str(item.get("id")), {}).get("status") == REQUIREMENT_ANSWERED
    ]


def _answers(requirements, requirement_states) -> dict:
    return {
        str(item.get("id")): requirement_states[str(item.get("id"))].get("value")
        for item in requirements or []
        if requirement_states.get(str(item.get("id")), {}).get("status") == REQUIREMENT_ANSWERED
    }


def _history(state: dict) -> list[dict]:
    value = state.get("history")
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value[-MAX_HISTORY_EVENTS:] if isinstance(item, dict)]


def _processed_message_ids(state: dict) -> list[str]:
    value = state.get("processed_message_ids")
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[-MAX_PROCESSED_MESSAGE_IDS:]:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _append_history(state: dict, *, event: str, requirement_id: str | None = None, message_id: str | None = None, value=None) -> None:
    events = _history(state)
    item = {"event": event, "at": timezone.now().isoformat()}
    if requirement_id:
        item["requirement_id"] = str(requirement_id)
    if message_id:
        item["message_id"] = str(message_id)
    if value is not None:
        item["value"] = value
    events.append(item)
    state["history"] = events[-MAX_HISTORY_EVENTS:]


def _normalize_runtime_state(state: dict, requirements, *, lead=None) -> dict:
    status = _status(state.get("qualification_status"))
    result = _result(state.get("qualification_result"))
    requirement_states = _normalized_requirement_states(state, requirements)
    missing = _missing_ids(requirements, requirement_states)
    next_item = next_requirement(requirements, requirement_states)

    current_id = _canonical_requirement_id(
        state.get("current_requirement_id") or state.get("last_asked_requirement_id"),
        requirements,
    )
    if current_id:
        current_status = requirement_states.get(current_id, {}).get("status")
        if current_status in {REQUIREMENT_ANSWERED, REQUIREMENT_SKIPPED, REQUIREMENT_NOT_APPLICABLE}:
            current_id = ""
    if not current_id and next_item is not None:
        current_id = str(next_item.get("id") or "")

    if status == STATUS_COMPLETED:
        current_id = ""
        next_item = None
        missing = []

    qualified_stage = _qualified_stage(lead) if lead is not None else None
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) if lead is not None else ""
    engagement_mode = (
        MODE_QUALIFICATION
        if lead is not None and stage_name == NEW_LEAD_STAGE and status != STATUS_COMPLETED
        else MODE_CONVERSATION
    )

    flow_version = str(state.get("flow_version") or _flow_version(requirements) or "").strip()
    snapshot = state.get("flow_snapshot")
    if not isinstance(snapshot, list):
        snapshot = []

    normalized = {
        "state_version": STATE_VERSION,
        "qualification_status": status,
        "qualification_result": result,
        "qualification_completed_at": state.get("qualification_completed_at"),
        "qualification_completed": status == STATUS_COMPLETED,
        "engagement_mode": engagement_mode,
        "flow_version": flow_version,
        "flow_snapshot": _snapshot(snapshot),
        "current_requirement_id": current_id or None,
        "requirement_states": requirement_states,
        "missing_requirement_ids": missing,
        "answered_requirement_ids": _answered_ids(requirements, requirement_states),
        "qualification_answers": _answers(requirements, requirement_states),
        "all_requirements_answered": bool(requirements) and not _missing_ids(requirements, requirement_states),
        "next_requirement_id": str(next_item.get("id")) if next_item is not None else None,
        "last_asked_requirement_id": _canonical_requirement_id(state.get("last_asked_requirement_id"), requirements) or None,
        "processed_message_ids": _processed_message_ids(state),
        "history": _history(state),
        "qualified_stage_id": str(qualified_stage.id) if qualified_stage is not None else None,
        "qualified_stage_name": qualified_stage.name if qualified_stage is not None else None,
    }
    return normalized


def state_for_lead(lead, requirements=None) -> dict:
    active_requirements = requirements_for_lead(lead, requirements)
    return _normalize_runtime_state(_raw_state(lead), active_requirements, lead=lead)


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
    active_requirements = requirements_for_lead(lead, requirements)
    state = _normalize_runtime_state(_raw_state(lead), active_requirements, lead=lead)
    if requirements and not state.get("flow_snapshot") and state["qualification_status"] != STATUS_COMPLETED:
        state["flow_snapshot"] = _snapshot(requirements)
        state["flow_version"] = _flow_version(requirements)
    attributes = attributes_with_state(lead, state)
    current_attributes = getattr(lead, "attributes", {}) if isinstance(getattr(lead, "attributes", {}), dict) else {}
    if attributes != current_attributes:
        lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
        lead.attributes = attributes
    return state


def state_after_stage_change(*, lead, old_stage_name: str, new_stage_name: str) -> dict:
    state = state_for_lead(lead)
    old_name = normalize_stage_name(old_stage_name)
    new_name = normalize_stage_name(new_stage_name)
    if state["qualification_status"] != STATUS_COMPLETED:
        if new_name == QUALIFIED_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_result"] = RESULT_QUALIFIED
            state["qualification_completed_at"] = timezone.now().isoformat()
            state["current_requirement_id"] = None
            _append_history(state, event="qualification_completed")
        elif old_name == NEW_LEAD_STAGE and new_name != NEW_LEAD_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_completed_at"] = timezone.now().isoformat()
            state["current_requirement_id"] = None
            _append_history(state, event="qualification_closed_by_stage_change")
    state["qualification_completed"] = state["qualification_status"] == STATUS_COMPLETED
    state["engagement_mode"] = (
        MODE_QUALIFICATION
        if new_name == NEW_LEAD_STAGE and state["qualification_status"] != STATUS_COMPLETED
        else MODE_CONVERSATION
    )
    return state


def mark_in_progress(lead) -> dict:
    state = state_for_lead(lead)
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", ""))
    if stage_name == NEW_LEAD_STAGE and state["qualification_status"] == STATUS_NOT_STARTED:
        state["qualification_status"] = STATUS_IN_PROGRESS
        state["engagement_mode"] = MODE_QUALIFICATION
        _append_history(state, event="qualification_started")
        _persist_state(lead, state)
    return state


def record_last_asked_requirement(lead, requirement_id: str | None, *, requirements=None) -> dict:
    active_requirements = requirements_for_lead(lead, requirements)
    if not active_requirements and requirements:
        active_requirements = _snapshot(requirements)
    requirement_id = _canonical_requirement_id(requirement_id, active_requirements)
    state = _normalize_runtime_state(_raw_state(lead), active_requirements, lead=lead)
    if not requirement_id:
        return state

    allowed = {str(item.get("id")) for item in active_requirements}
    if requirement_id not in allowed:
        return state

    expected = next_requirement(active_requirements, state["requirement_states"])
    expected_id = str(expected.get("id")) if expected is not None else ""
    if expected_id and requirement_id != expected_id:
        requirement_id = expected_id

    current = state["requirement_states"].get(requirement_id, _requirement_state({}))
    if current.get("status") in {REQUIREMENT_ANSWERED, REQUIREMENT_SKIPPED, REQUIREMENT_NOT_APPLICABLE}:
        return state

    now = timezone.now().isoformat()
    current["status"] = REQUIREMENT_ASKED
    current["asked_at"] = current.get("asked_at") or now
    current["updated_at"] = now
    state["requirement_states"][requirement_id] = current
    state["current_requirement_id"] = requirement_id
    state["last_asked_requirement_id"] = requirement_id
    state["flow_snapshot"] = state.get("flow_snapshot") or _snapshot(active_requirements)
    state["flow_version"] = state.get("flow_version") or _flow_version(active_requirements)
    if state["qualification_status"] == STATUS_NOT_STARTED:
        state["qualification_status"] = STATUS_IN_PROGRESS
    _append_history(state, event="asked", requirement_id=requirement_id)
    return _persist_state(lead, _normalize_runtime_state(state, active_requirements, lead=lead))


def _is_boolean_question(question: str) -> bool:
    first_line = str(question or "").splitlines()[0]
    normalized = " ".join(first_line.strip().casefold().split())
    if normalized.startswith(_BOOLEAN_QUESTION_PREFIXES) or " whether " in f" {normalized} ":
        return True
    options = _question_options(question)
    values = {str(item.get("value") or "").strip().casefold() for item in options}
    return bool(options) and values.issubset(_YES | _NO | {"yes", "no"})


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
    return bool(re.search(
        r"\b(?:today|tomorrow|tonight|this week|next week|this month|next month|immediately|asap|soon|within\s+\d+\s+(?:day|days|week|weeks|month|months)|\d+\s+(?:day|days|week|weeks|month|months))\b",
        normalized,
    ))


def _question_options(question: str) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for line in str(question or "").splitlines()[1:]:
        match = _OPTION_LINE_RE.match(line.strip())
        if match:
            key = match.group("key")
            options.append({
                "key": key.upper() if key.isalpha() else key,
                "value": re.sub(r"\s+", " ", match.group("value")).strip(),
            })
    return options


def _match_option_answer(text: str, options: list[dict[str, str]]) -> str | None:
    normalized = " ".join(str(text or "").strip().casefold().split()).strip(" .,:;-)('")
    if not normalized:
        return None
    stripped = re.sub(r"^option\s+", "", normalized)
    for index, option in enumerate(options, start=1):
        key = str(option.get("key") or "").strip().casefold()
        value = str(option.get("value") or "").strip()
        aliases = {key, str(index), value.casefold(), f"option {key}", f"option {index}"}
        if index <= 26:
            aliases.add(chr(96 + index))
        if normalized in aliases or stripped in aliases:
            return value
    return None


def _looks_like_interrupt(text: str, question: str) -> bool:
    normalized = " ".join(str(text or "").strip().casefold().split())
    if "?" in text:
        return True
    if any(term in normalized for term in _INTERRUPT_TERMS):
        question_normalized = " ".join(str(question or "").strip().casefold().split())
        return not any(term in question_normalized for term in ("call", "demo", "appointment", "human", "agent"))
    return False


def _classify_direct_reply(*, text: str, question: str) -> tuple[str, object, str] | None:
    """Classify only an answer to the active, already-asked requirement."""
    raw_text = str(text or "").strip()
    normalized = " ".join(raw_text.casefold().split())
    if not normalized or len(normalized) > 240 or "\n" in raw_text:
        return None
    if normalized in _ACK_ONLY:
        return None
    if normalized in _UNCLEAR_ONLY:
        return (REQUIREMENT_UNCLEAR, raw_text, "high")
    if _looks_like_interrupt(raw_text, question):
        return None

    options = _question_options(question)
    if options:
        matched = _match_option_answer(raw_text, options)
        if matched is not None:
            return (REQUIREMENT_ANSWERED, matched, "high")
        return None

    if normalized in _YES | _NO:
        if not _is_boolean_question(question):
            return None
        return (REQUIREMENT_ANSWERED, normalized in _YES, "high")

    if _question_has_any(question, ("budget", "price range", "amount", "how much", "age", "quantity", "how many", "count")) and _looks_like_numeric_value(raw_text):
        return (REQUIREMENT_ANSWERED, raw_text, "high")
    if _question_has_any(question, ("timeline", "when", "date", "time frame", "timeframe", "how soon", "purchase by", "start by")) and _looks_like_timeline_value(raw_text):
        return (REQUIREMENT_ANSWERED, raw_text, "high")
    if _question_has_any(question, ("email", "e-mail")) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", raw_text):
        return (REQUIREMENT_ANSWERED, raw_text, "high")
    if _question_has_any(question, ("phone", "mobile", "contact number", "whatsapp number")):
        digits = re.sub(r"\D", "", raw_text)
        if 7 <= len(digits) <= 15:
            return (REQUIREMENT_ANSWERED, raw_text, "high")

    question_stem = " ".join(str(question or "").splitlines()[0].split())
    lower_question = question_stem.casefold()
    if any(term in lower_question for term in _TYPED_TERMS) or _is_boolean_question(question_stem):
        return None
    if _QUESTION_START_RE.match(question_stem) or any(term in lower_question for term in _FREEFORM_TERMS):
        return (REQUIREMENT_ANSWERED, raw_text, "high")
    return None


def _is_explicit_correction(source_text: str) -> bool:
    normalized = " ".join(str(source_text or "").strip().casefold().split())
    return any(term in normalized for term in _CORRECTION_TERMS)


def apply_unambiguous_reply(*, lead, requirements, text: str, source_message_id: str | None) -> dict:
    active_requirements = requirements_for_lead(lead, requirements)
    state = state_for_lead(lead, requirements=active_requirements)
    source_id = str(source_message_id or "").strip()
    if source_id and source_id in state.get("processed_message_ids", []):
        return {"changed": False, "state": state, "answer_status": None, "duplicate": True}

    active_id = str(state.get("current_requirement_id") or state.get("last_asked_requirement_id") or "").strip()
    if not active_id:
        return {"changed": False, "state": state, "answer_status": None}
    requirement = next((item for item in active_requirements if str(item.get("id")) == active_id), None)
    if requirement is None:
        return {"changed": False, "state": state, "answer_status": None}

    current = state["requirement_states"].get(active_id, _requirement_state({}))
    if current.get("status") not in {REQUIREMENT_ASKED, REQUIREMENT_UNCLEAR}:
        return {"changed": False, "state": state, "answer_status": current.get("status")}

    classified = _classify_direct_reply(
        text=text,
        question=str(requirement.get("question") or requirement.get("label") or ""),
    )
    if classified is None:
        return {"changed": False, "state": state, "answer_status": None}

    answer_status, value, confidence = classified
    now = timezone.now().isoformat()
    state["requirement_states"][active_id] = {
        **current,
        "status": answer_status,
        "value": value,
        "raw_answer": str(text or "").strip(),
        "confidence": confidence,
        "source_message_id": source_id or None,
        "updated_at": now,
    }
    if source_id:
        processed = state.get("processed_message_ids", []) + [source_id]
        state["processed_message_ids"] = processed[-MAX_PROCESSED_MESSAGE_IDS:]
    if state["qualification_status"] == STATUS_NOT_STARTED:
        state["qualification_status"] = STATUS_IN_PROGRESS
    _append_history(state, event="answered" if answer_status == REQUIREMENT_ANSWERED else "clarification_required", requirement_id=active_id, message_id=source_id or None, value=value)

    state = _normalize_runtime_state(state, active_requirements, lead=lead)
    if state["all_requirements_answered"] and active_requirements:
        state["qualification_status"] = STATUS_COMPLETED
        state["qualification_completed"] = True
        state["qualification_completed_at"] = state.get("qualification_completed_at") or now
        state["current_requirement_id"] = None
        state["next_requirement_id"] = None
        state["engagement_mode"] = MODE_CONVERSATION
        _append_history(state, event="qualification_answers_complete")
    _persist_state(lead, state)
    next_item = next_requirement(active_requirements, state["requirement_states"]) if state["qualification_status"] != STATUS_COMPLETED else None
    return {"changed": True, "state": state, "answer_status": answer_status, "next_requirement": next_item}


def reset_state(lead) -> dict:
    state = state_for_lead(lead)
    state.update({
        "state_version": STATE_VERSION,
        "qualification_status": STATUS_NOT_STARTED,
        "qualification_result": "",
        "qualification_completed_at": None,
        "qualification_completed": False,
        "flow_version": "",
        "flow_snapshot": [],
        "current_requirement_id": None,
        "requirement_states": {},
        "missing_requirement_ids": [],
        "answered_requirement_ids": [],
        "qualification_answers": {},
        "all_requirements_answered": False,
        "next_requirement_id": None,
        "last_asked_requirement_id": None,
        "processed_message_ids": [],
        "history": [],
    })
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", ""))
    state["engagement_mode"] = MODE_QUALIFICATION if stage_name == NEW_LEAD_STAGE else MODE_CONVERSATION
    return _persist_state(lead, state)


def project_answer_updates(*, state, requirements, updates, messages):
    """Validate evidence and project updates without allowing state to move backwards."""
    result = deepcopy(state)
    allowed = {str(item["id"]) for item in requirements}
    sources = {
        str(m.get("id")): str(m.get("body") or "")
        for m in messages
        if m.get("direction") == "inbound"
    }
    if not isinstance(updates, list) or len(updates) > len(allowed):
        raise ValueError("Invalid qualification updates.")

    seen: set[str] = set()
    processed = set(_processed_message_ids(result))
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"requirement_id", "value", "source_message_id", "evidence"}:
            raise ValueError("Invalid qualification answer schema.")
        requirement_id = _canonical_requirement_id(update["requirement_id"], requirements)
        source_id = str(update["source_message_id"] or "")
        evidence = update["evidence"]
        value = update["value"]
        source_text = sources.get(source_id, "")
        if (
            requirement_id not in allowed
            or requirement_id in seen
            or not source_id
            or not isinstance(evidence, str)
            or not evidence.strip()
            or evidence not in source_text
            or not isinstance(value, (str, int, float, bool))
            or (isinstance(value, str) and not value.strip())
        ):
            raise ValueError("Qualification answer lacks valid inbound evidence.")
        seen.add(requirement_id)

        current = _requirement_state((result.get("requirement_states") or {}).get(requirement_id, {}))
        if current.get("status") == REQUIREMENT_ANSWERED and not _is_explicit_correction(source_text):
            continue
        if source_id in processed and current.get("status") == REQUIREMENT_ANSWERED:
            continue

        result.setdefault("requirement_states", {})[requirement_id] = {
            **current,
            "status": REQUIREMENT_ANSWERED,
            "value": value,
            "raw_answer": evidence,
            "source_message_id": source_id,
            "confidence": "supported",
            "updated_at": timezone.now().isoformat(),
        }
        processed.add(source_id)
        _append_history(result, event="answer_corrected" if current.get("status") == REQUIREMENT_ANSWERED else "answered", requirement_id=requirement_id, message_id=source_id, value=value)

    result["processed_message_ids"] = list(processed)[-MAX_PROCESSED_MESSAGE_IDS:]
    result["missing_requirement_ids"] = _missing_ids(requirements, result.get("requirement_states", {}))
    result["answered_requirement_ids"] = _answered_ids(requirements, result.get("requirement_states", {}))
    result["qualification_answers"] = _answers(requirements, result.get("requirement_states", {}))
    result["all_requirements_answered"] = bool(requirements) and not result["missing_requirement_ids"]
    next_item = next_requirement(requirements, result.get("requirement_states", {}))
    result["current_requirement_id"] = str(next_item.get("id")) if next_item is not None else None
    result["next_requirement_id"] = result["current_requirement_id"]
    if result["all_requirements_answered"]:
        result["qualification_status"] = STATUS_COMPLETED
        result["qualification_completed"] = True
        result["qualification_completed_at"] = result.get("qualification_completed_at") or timezone.now().isoformat()
        result["current_requirement_id"] = None
        result["next_requirement_id"] = None
        result["engagement_mode"] = MODE_CONVERSATION
    return result


def persist_answer_updates(*, lead, updates):
    """Persist evidence-backed answers under the caller's final lead lock."""
    if not updates:
        return state_for_lead(lead)
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements

    org_info = OrgInfo.objects.filter(organization_id=lead.organization_id).first()
    current_requirements = compile_qualification_requirements(
        org_info.qualification_requirements if org_info else ""
    )["requirements"]
    requirements = requirements_for_lead(lead, current_requirements)
    messages = list(
        lead.whatsapp_messages.filter(
            organization_id=lead.organization_id,
            id__in=[item["source_message_id"] for item in updates if isinstance(item, dict) and item.get("source_message_id")],
            direction="inbound",
        ).values("id", "body", "direction")
    )
    base = state_for_lead(lead, requirements=requirements)
    if not base.get("flow_snapshot") and requirements:
        base["flow_snapshot"] = _snapshot(requirements)
        base["flow_version"] = _flow_version(requirements)
    state = project_answer_updates(
        state=base,
        requirements=requirements,
        updates=updates,
        messages=messages,
    )
    if state.get("qualification_status") == STATUS_COMPLETED:
        _append_history(state, event="qualification_answers_complete")
    return _persist_state(lead, state)
