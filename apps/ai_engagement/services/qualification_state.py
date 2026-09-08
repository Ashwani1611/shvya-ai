from __future__ import annotations

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

NEW_LEAD_STAGE = "new lead"
QUALIFIED_STAGE = "qualified"


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
    attributes = lead.attributes if isinstance(lead.attributes, dict) else {}
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


def state_for_lead(lead) -> dict:
    """Return the normalized, application-controlled qualification state."""
    state = _raw_state(lead)
    status = _status(state.get("qualification_status"))
    result = _result(state.get("qualification_result"))
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", ""))
    qualified_stage = _qualified_stage(lead)

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
    attributes = deepcopy(lead.attributes) if isinstance(lead.attributes, dict) else {}
    attributes[QUALIFICATION_STATE_KEY] = deepcopy(state)
    return attributes


def ensure_state(lead) -> dict:
    """Persist the normalized state when it is missing or stale."""
    state = state_for_lead(lead)
    attributes = attributes_with_state(lead, state)
    if attributes != (lead.attributes or {}):
        lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
        lead.attributes = attributes
    return state


def state_after_stage_change(*, lead, old_stage_name: str, new_stage_name: str) -> dict:
    """
    Return the durable qualification state after a CRM stage transition.

    Leaving New Lead completes that qualification journey. Moving to Qualified
    records a qualified result. A completed qualification is never restarted by
    moving the lead between stages; only ``reset_state`` can reset it.
    """
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
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", ""))

    if (
        stage_name == NEW_LEAD_STAGE
        and state["qualification_status"] == STATUS_NOT_STARTED
    ):
        state["qualification_status"] = STATUS_IN_PROGRESS
        state["engagement_mode"] = MODE_QUALIFICATION
        attributes = attributes_with_state(lead, state)
        lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
        lead.attributes = attributes

    return state


def reset_state(lead) -> dict:
    """Explicit reset hook. Normal stage changes and reconnects never call it."""
    state = state_for_lead(lead)
    state.update(
        {
            "qualification_status": STATUS_NOT_STARTED,
            "qualification_result": "",
            "qualification_completed_at": None,
        }
    )
    stage_name = normalize_stage_name(getattr(getattr(lead, "stage", None), "name", ""))
    state["engagement_mode"] = (
        MODE_QUALIFICATION
        if stage_name == NEW_LEAD_STAGE
        else MODE_CONVERSATION
    )
    attributes = attributes_with_state(lead, state)
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
    lead.attributes = attributes
    return state
