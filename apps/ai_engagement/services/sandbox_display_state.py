"""Customer identity and displayed-question tracking for in-memory Sandbox leads."""
import re

from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION, NEW_LEAD_STAGE, QUALIFICATION_STATE_KEY, normalize_stage_name, state_for_lead,
)


def sandbox_customer_name(attributes):
    """Use only a captured personal-name requirement, never the preview label."""
    if not isinstance(attributes, dict):
        return ""
    state = attributes.get(QUALIFICATION_STATE_KEY) or {}
    if not isinstance(state, dict):
        return ""
    names = []
    for requirement in state.get("flow_snapshot") or []:
        if not isinstance(requirement, dict):
            continue
        label = " ".join(str(requirement.get("label") or "").casefold().split()).rstrip(".?")
        key = str(requirement.get("attribute_key") or "").casefold()
        personal_name = (
            key in {"name", "full_name"}
            or label in {"name", "full name", "your name", "your full name"}
            or re.fullmatch(r"please (?:provide|share) your (?:full )?name", label)
        )
        answer = (state.get("requirement_states") or {}).get(str(requirement.get("id") or ""), {})
        if personal_name and isinstance(answer, dict) and answer.get("status") == "answered":
            value = answer.get("value")
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    return names[0] if len(names) == 1 else ""


def displayed_requirement_id(*, lead, requirements, decision):
    """Recover an omitted ID only from an exact authored question prefix.

    A business answer, quote, negation or acknowledgement cannot mark the next
    question as asked. This records presentation only; it never captures an
    answer, qualifies a lead or authorizes a stage/action.
    """
    if not decision.should_engage:
        return None
    declared = getattr(decision, "next_requirement_id", None)
    if declared:
        return declared
    if normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) != NEW_LEAD_STAGE:
        return None
    state = state_for_lead(lead, requirements=requirements)
    if state.get("engagement_mode") != MODE_QUALIFICATION or state.get("qualification_completed"):
        return None
    pending = str(state.get("next_requirement_id") or "")
    requirement = next((item for item in requirements if str(item.get("id") or "") == pending), None)
    if not requirement or not requirement.get("can_direct_ask"):
        return None
    question = str(requirement.get("question") or "").splitlines()[0].strip()
    def normalize(text):
        return re.sub(r"[^\w]+", " ", text.casefold()).strip()
    authored = normalize(question)
    if not authored:
        return None
    for clause in re.split(r"(?<=[.!?।])\s+|\n+|,\s*", str(decision.message or "")):
        if re.search(r'["“”‘’]|\b(?:not|never|already|previously)\b', clause, re.I):
            continue
        visible = normalize(clause)
        if visible == authored or visible.startswith(authored + " "):
            return pending
    return None
