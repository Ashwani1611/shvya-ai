from __future__ import annotations

import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.crm.models import LeadNote, LeadReminder, Stage
from apps.followups.models import FollowupSequence
from services.crm.stage_service import move_lead
from services.triggers.runtime import suppress_trigger_signals


class TriggerActionError(Exception):
    pass


ACTION_TYPES = {
    "start_sequence",
    "clear_sequence",
    "move_stage",
    "set_ai_enabled",
    "add_note",
    "create_reminder",
}

DELAY_UNITS = {"minutes", "hours", "days"}

_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*}}")


def _lead_values(lead):
    values = {
        "lead_name": lead.name or "",
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone or "",
        "email": lead.email or "",
        "lead_source": lead.lead_source or "",
        "org_name": lead.organization.name,
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }
    for key, value in (lead.attributes or {}).items():
        values.setdefault(str(key), value)
    return values


def render_action_text(text, lead):
    values = _lead_values(lead)

    def replace(match):
        value = values.get(match.group(1), "")
        return str(value if value is not None else "")

    return _PLACEHOLDER_RE.sub(replace, text or "")


def validate_actions(actions, *, organization=None):
    if not isinstance(actions, list) or not actions:
        raise TriggerActionError("Add at least one action.")
    if len(actions) > 10:
        raise TriggerActionError("A Smart Trigger can have at most 10 actions.")

    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise TriggerActionError(f"Action {index} is invalid.")
        action_type = action.get("type")
        if action_type not in ACTION_TYPES:
            raise TriggerActionError(f"Action {index} has an unsupported type.")

        if action_type == "start_sequence":
            sequence_id = action.get("sequence_id")
            if not sequence_id:
                raise TriggerActionError(f"Action {index}: choose a sequence.")
            if organization and not FollowupSequence.objects.filter(
                id=sequence_id,
                organization=organization,
                is_active=True,
            ).exists():
                raise TriggerActionError(
                    f"Action {index}: the selected sequence is unavailable."
                )

        elif action_type == "move_stage":
            stage_id = action.get("stage_id")
            if not stage_id:
                raise TriggerActionError(f"Action {index}: choose a stage.")
            if organization and not Stage.objects.filter(
                id=stage_id,
                pipeline__organization=organization,
                is_active=True,
            ).exists():
                raise TriggerActionError(
                    f"Action {index}: the selected stage is unavailable."
                )

        elif action_type == "set_ai_enabled":
            if action.get("enabled") not in {True, False}:
                raise TriggerActionError(
                    f"Action {index}: choose whether AI should be enabled."
                )

        elif action_type == "add_note":
            if not str(action.get("text") or "").strip():
                raise TriggerActionError(f"Action {index}: note text is required.")

        elif action_type == "create_reminder":
            if not str(action.get("title") or "").strip():
                raise TriggerActionError(
                    f"Action {index}: reminder title is required."
                )
            try:
                delay_value = int(action.get("delay_value") or 0)
            except (TypeError, ValueError) as exc:
                raise TriggerActionError(
                    f"Action {index}: reminder delay must be a whole number."
                ) from exc
            if delay_value < 0:
                raise TriggerActionError(
                    f"Action {index}: reminder delay cannot be negative."
                )
            if action.get("delay_unit", "minutes") not in DELAY_UNITS:
                raise TriggerActionError(
                    f"Action {index}: choose minutes, hours, or days."
                )

    return actions


def _delay(value, unit):
    value = int(value or 0)
    if unit == "days":
        return timedelta(days=value)
    if unit == "hours":
        return timedelta(hours=value)
    return timedelta(minutes=value)


def execute_actions(*, trigger, lead, actions):
    """Execute database-backed actions atomically and return audit-safe results."""
    validate_actions(actions, organization=trigger.organization)
    results = []

    with transaction.atomic(), suppress_trigger_signals():
        for index, action in enumerate(actions, start=1):
            action_type = action["type"]
            try:
                if action_type == "start_sequence":
                    from services.followup_service import assign_sequence

                    sequence = FollowupSequence.objects.get(
                        id=action["sequence_id"],
                        organization=trigger.organization,
                        is_active=True,
                    )
                    state = assign_sequence(
                        lead=lead,
                        sequence=sequence,
                        actor=trigger.created_by,
                    )
                    results.append(
                        {
                            "type": action_type,
                            "sequence_id": str(sequence.id),
                            "state_id": str(state.id),
                        }
                    )

                elif action_type == "clear_sequence":
                    from services.followup_service import clear_sequence

                    clear_sequence(lead=lead)
                    results.append({"type": action_type})

                elif action_type == "move_stage":
                    stage = Stage.objects.select_related("pipeline").get(
                        id=action["stage_id"],
                        pipeline__organization=trigger.organization,
                        is_active=True,
                    )
                    if stage.pipeline_id != lead.pipeline_id:
                        raise TriggerActionError(
                            "The target stage must belong to the lead's current pipeline."
                        )
                    move_lead(lead=lead, new_stage=stage)
                    results.append(
                        {
                            "type": action_type,
                            "stage_id": str(stage.id),
                            "stage_name": stage.name,
                        }
                    )

                elif action_type == "set_ai_enabled":
                    enabled = bool(action["enabled"])
                    lead.ai_enabled = enabled
                    lead.save(update_fields=["ai_enabled", "updated_at"])
                    results.append({"type": action_type, "enabled": enabled})

                elif action_type == "add_note":
                    note = LeadNote.objects.create(
                        lead=lead,
                        created_by=trigger.created_by,
                        note=render_action_text(action.get("text", ""), lead),
                        note_type="system",
                    )
                    results.append({"type": action_type, "note_id": str(note.id)})

                elif action_type == "create_reminder":
                    due_at = timezone.now() + _delay(
                        action.get("delay_value", 0),
                        action.get("delay_unit", "minutes"),
                    )
                    reminder = LeadReminder.objects.create(
                        lead=lead,
                        assigned_to=trigger.created_by,
                        title=render_action_text(action.get("title", ""), lead),
                        description=render_action_text(
                            action.get("description", ""),
                            lead,
                        ),
                        due_at=due_at,
                    )
                    results.append(
                        {
                            "type": action_type,
                            "reminder_id": str(reminder.id),
                            "due_at": reminder.due_at.isoformat(),
                        }
                    )

            except TriggerActionError:
                raise
            except Exception as exc:
                raise TriggerActionError(
                    f"Action {index} ({action_type}) failed: {exc}"
                ) from exc

    return results
