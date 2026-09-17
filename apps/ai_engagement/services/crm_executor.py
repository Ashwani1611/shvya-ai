from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.action_plan_trace import trace_action_plan
from apps.ai_engagement.services.action_planner import ActionPlanner
from apps.ai_engagement.services.crm_actions import (
    CRMActionSchemaError,
    validate_crm_actions,
)
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadContact,
    LeadNote,
    LeadReminder,
    Stage,
)
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
    move_lead_to_stage,
)
from services.crm.attribute_service import update_lead_attribute_values
from services.crm_activity_service import record_note_added, record_reminder_created


class CRMActionExecutionError(Exception):
    """Raised when a validated CRM action cannot be safely executed."""


class CRMActionExecutor:
    """Deterministic executor for AI-requested CRM actions.

    Phase 7 keeps planning and execution separate. ``execute`` first sends the
    incoming canonical action vocabulary through ``ActionPlanner``. The planner
    performs only read/validation work; this executor remains the mutation
    boundary and re-validates current state under a transaction/row lock.
    """

    def execute(
        self,
        *,
        organization,
        lead: Lead,
        actions: list[dict[str, Any]],
        actor=None,
        source_message=None,
    ) -> list[dict[str, Any]]:
        if organization is None:
            raise CRMActionExecutionError("Organization is required.")
        if lead is None:
            raise CRMActionExecutionError("Lead is required.")
        if lead.organization_id != organization.id:
            raise CRMActionExecutionError("Lead does not belong to this organization.")

        plan = ActionPlanner().plan(
            organization=organization,
            lead=lead,
            decision=SimpleNamespace(
                crm_actions=actions,
                file_document_id=None,
                reason_code="",
            ),
            source_message=source_message,
        )
        trace_action_plan(plan)
        if plan.rejected_actions:
            reason_codes = sorted(
                {proposal.reason_code or "ACTION_REJECTED" for proposal in plan.rejected_actions}
            )
            raise CRMActionExecutionError(
                "Action plan rejected one or more proposals: " + ", ".join(reason_codes)
            )

        try:
            normalized_actions = validate_crm_actions(plan.executor_actions)
        except CRMActionSchemaError as exc:
            raise CRMActionExecutionError(f"Invalid CRM actions: {exc}") from exc

        results = []
        with transaction.atomic():
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(id=lead.id, organization=organization)
            )

            if locked_lead.organization_id != organization.id:
                raise CRMActionExecutionError("Lead tenant changed before execution.")
            self._revalidate_plan_state(plan=plan, lead=locked_lead)

            for action in normalized_actions:
                action_type = action["type"]
                if action_type == "attribute_updates":
                    results.append(
                        self._execute_attribute_updates(
                            organization=organization,
                            lead=locked_lead,
                            action=action,
                        )
                    )
                elif action_type == "pipeline_transition":
                    results.append(
                        self._execute_pipeline_transition(
                            organization=organization,
                            lead=locked_lead,
                            action=action,
                            actor=actor,
                        )
                    )
                elif action_type == "add_note":
                    results.append(
                        self._execute_add_note(
                            lead=locked_lead,
                            action=action,
                            actor=actor,
                        )
                    )
                elif action_type == "create_reminder":
                    results.append(
                        self._execute_create_reminder(
                            lead=locked_lead,
                            action=action,
                            actor=actor,
                        )
                    )
                elif action_type == "contact_updates":
                    results.append(
                        self._execute_contact_updates(
                            organization=organization,
                            lead=locked_lead,
                            action=action,
                        )
                    )
                else:
                    raise CRMActionExecutionError(
                        f"Unsupported CRM action type: {action_type!r}."
                    )
        return results

    @staticmethod
    def _revalidate_plan_state(*, plan, lead: Lead) -> None:
        """Reject stale transition plans before any CRM side effect is executed."""
        for proposal in plan.accepted_actions:
            if proposal.executor_action_type != "pipeline_transition":
                continue
            snapshot = proposal.state_snapshot or {}
            planned_pipeline_id = snapshot.get("pipeline_id")
            planned_stage_id = snapshot.get("stage_id")
            current_pipeline_id = str(lead.pipeline_id) if lead.pipeline_id else None
            current_stage_id = str(lead.stage_id) if lead.stage_id else None
            if (
                planned_pipeline_id != current_pipeline_id
                or planned_stage_id != current_stage_id
            ):
                raise CRMActionExecutionError(
                    "Action plan is stale because the lead pipeline/stage changed before execution."
                )

    def _execute_attribute_updates(
        self,
        *,
        organization,
        lead: Lead,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        updates = action["updates"]
        requested_values = {item["key"]: item["value"] for item in updates}
        allowed_keys = set(
            AttributeDefinition.objects.filter(organization=organization).values_list(
                "key", flat=True
            )
        )
        invalid_keys = sorted(set(requested_values) - allowed_keys)
        if invalid_keys:
            raise CRMActionExecutionError(f"Unknown attribute keys: {invalid_keys}.")
        try:
            update_lead_attribute_values(
                organization=organization,
                lead=lead,
                values=requested_values,
            )
        except DjangoValidationError as exc:
            raise CRMActionExecutionError("Lead attribute update failed.") from exc
        return {
            "type": "attribute_updates",
            "status": "executed",
            "keys": list(requested_values.keys()),
        }

    def _execute_pipeline_transition(
        self,
        *,
        organization,
        lead: Lead,
        action: dict[str, Any],
        actor=None,
    ) -> dict[str, Any]:
        stage_id = action["stage_shift"]["stage_id"]
        stage = (
            Stage.objects.select_related("pipeline")
            .filter(
                id=stage_id,
                pipeline__organization=organization,
                pipeline__is_active=True,
                is_active=True,
            )
            .first()
        )
        if stage is None:
            raise CRMActionExecutionError(
                "Requested stage does not belong to an active pipeline in this organization."
            )

        old_pipeline_id = str(lead.pipeline_id) if lead.pipeline_id else None
        old_stage_id = str(lead.stage_id) if lead.stage_id else None
        try:
            if stage.pipeline_id == lead.pipeline_id:
                move_lead_to_stage(lead=lead, stage=stage, actor=actor)
            else:
                move_lead_to_pipeline_stage(
                    lead=lead,
                    pipeline=stage.pipeline,
                    stage=stage,
                    actor=actor,
                )
        except LeadTransitionError as exc:
            raise CRMActionExecutionError(str(exc)) from exc

        qualification_note_id = None
        if str(stage.name or "").strip().casefold() == "qualified":
            from apps.ai_engagement.services.qualification import QualificationService

            note = QualificationService().append_backend_completion_summary(
                organization=organization,
                lead=lead,
                created_by=actor,
            )
            qualification_note_id = str(note.id) if note is not None else None

        return {
            "type": "pipeline_transition",
            "status": (
                "no_op"
                if old_pipeline_id == str(stage.pipeline_id)
                and old_stage_id == str(stage.id)
                else "executed"
            ),
            "pipeline_id": str(stage.pipeline_id),
            "stage_id": str(stage.id),
            "qualification_note_id": qualification_note_id,
        }

    def _execute_add_note(
        self,
        *,
        lead: Lead,
        action: dict[str, Any],
        actor=None,
    ) -> dict[str, Any]:
        try:
            note = LeadNote.objects.create(
                lead=lead,
                created_by=actor,
                note=action["note"],
                note_type="system",
            )
            record_note_added(lead=lead, actor=actor, note=note)
        except Exception as exc:
            raise CRMActionExecutionError("CRM note creation failed.") from exc
        return {
            "type": "add_note",
            "status": "executed",
            "note_id": str(note.id),
        }

    def _execute_create_reminder(
        self,
        *,
        lead: Lead,
        action: dict[str, Any],
        actor=None,
    ) -> dict[str, Any]:
        due_at_raw = action["due_at"]
        try:
            due_at = datetime.fromisoformat(due_at_raw)
        except ValueError as exc:
            raise CRMActionExecutionError("Reminder due_at is invalid.") from exc
        if timezone.is_naive(due_at):
            due_at = timezone.make_aware(due_at, timezone.get_current_timezone())
        assigned_to = lead.pipeline.owner if lead.pipeline else None
        try:
            reminder = LeadReminder.objects.create(
                lead=lead,
                assigned_to=assigned_to,
                title=action["title"],
                description=action["description"],
                due_at=due_at,
                status="pending",
            )
            record_reminder_created(lead=lead, actor=actor, reminder=reminder)
        except Exception as exc:
            raise CRMActionExecutionError("CRM reminder creation failed.") from exc
        return {
            "type": "create_reminder",
            "status": "executed",
            "reminder_id": str(reminder.id),
        }

    def _execute_contact_updates(
        self,
        *,
        organization,
        lead: Lead,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        updated_contacts = []
        for update in action["updates"]:
            contact = (
                LeadContact.objects.filter(
                    id=update["contact_id"],
                    lead=lead,
                    lead__organization=organization,
                ).first()
            )
            if contact is None:
                raise CRMActionExecutionError(
                    "Requested contact does not belong to this lead."
                )
            contact.channel = update["channel"]
            contact.handle = update["handle"]
            contact.full_clean()
            contact.save(update_fields=["channel", "handle"])
            updated_contacts.append(str(contact.id))
        return {
            "type": "contact_updates",
            "status": "executed",
            "contact_ids": updated_contacts,
        }
