from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.action_plan_trace import trace_action_plan
from apps.ai_engagement.services.action_planner import ActionPlanner, EXECUTOR_ACTION_TYPES
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

        from apps.ai_engagement.models import AIActionReceipt
        from apps.ai_engagement.services.tenant_guard import TenantGuard
        from apps.channels.models import WhatsAppMessage

        source_id = plan.source_message_id
        results = []
        with transaction.atomic():
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(id=lead.id, organization=organization)
            )

            if locked_lead.organization_id != organization.id:
                raise CRMActionExecutionError("Lead tenant changed before execution.")
            guard = TenantGuard(organization)
            guard.validate_current_lead_context(locked_lead)
            if source_id:
                # Supplied/inferred source identity is never sufficient by itself.
                # Validate the real persisted message and its account before use.
                try:
                    source = WhatsAppMessage.objects.select_related("account", "lead").filter(
                        pk=source_id, organization=organization, lead=locked_lead,
                        direction=WhatsAppMessage.Direction.INBOUND,
                    ).first()
                except (ValueError, DjangoValidationError) as exc:
                    raise CRMActionExecutionError("Invalid action source message.") from exc
                if source is None:
                    raise CRMActionExecutionError("Action source message is outside this lead.")
                guard.validate_message(source, lead=locked_lead)

            proposals = [item for item in plan.accepted_actions
                         if item.executor_action_type in EXECUTOR_ACTION_TYPES]
            receipts = {
                item.idempotency_key: item for item in AIActionReceipt.objects.filter(
                    organization=organization, lead=locked_lead,
                    idempotency_key__in=[item.idempotency_key for item in proposals],
                )
            } if source_id else {}
            pending = [item for item in proposals if item.idempotency_key not in receipts]
            if pending:
                self._revalidate_plan_state(plan=replace(plan, actions=tuple(pending)), lead=locked_lead)

            for proposal, action in zip(proposals, normalized_actions, strict=True):
                receipt = receipts.get(proposal.idempotency_key)
                if receipt is not None:
                    results.append({**deepcopy(receipt.result), "idempotent_replay": True})
                    continue
                # Recheck target ownership under the lock, not just at planning.
                guard.validate_crm_action(lead=locked_lead, action=action)
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
                if source_id:
                    receipt = AIActionReceipt.objects.create(
                        organization=organization,
                        lead=locked_lead,
                        source_message_id=source_id,
                        idempotency_key=proposal.idempotency_key,
                        action_type=proposal.action_type,
                        result=deepcopy(results[-1]),
                    )
                    # Identical proposals in the same batch are also replays.
                    receipts[proposal.idempotency_key] = receipt
        return results

    @staticmethod
    def _revalidate_plan_state(*, plan, lead: Lead) -> None:
        """Reject stale transition plans before any CRM side effect is executed."""
        from apps.ai_engagement.services.organization_runtime_profile import configured_action_types

        if str(plan.organization_id) != str(lead.organization_id) or str(plan.lead_id) != str(lead.pk):
            raise CRMActionExecutionError("Action plan identity changed before execution.")
        allowed = configured_action_types(lead.organization.settings)
        for proposal in plan.accepted_actions:
            if proposal.executor_action_type in EXECUTOR_ACTION_TYPES and proposal.executor_action_type not in allowed:
                raise CRMActionExecutionError("Action permission changed before execution.")
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
        from apps.ai_engagement.services.confidentiality import (
            is_sensitive_attribute_definition,
        )

        existing = {
            item.key: item
            for item in AttributeDefinition.objects.filter(organization=organization)
        }
        requested_values = {}
        created_keys = []

        for item in updates:
            requested_key = str(item["key"]).strip().lower()
            definition = existing.get(requested_key)
            if definition is None and item.get("create_if_missing") is True:
                requested_name = str(item.get("name") or "").strip()
                # Reuse an equivalent explicitly named definition before creating
                # another field. This keeps conversational extraction from
                # producing duplicate CRM columns.
                definition = (
                    AttributeDefinition.objects.filter(
                        organization=organization,
                        name__iexact=requested_name,
                    ).first()
                )
                if definition is None:
                    from services.crm.attribute_service import create_attribute_definition

                    try:
                        definition = create_attribute_definition(
                            organization=organization,
                            name=requested_name,
                            field_type=str(item.get("field_type") or "text").strip().casefold(),
                            description=(
                                "Created from explicit lead information captured by SHVYA AI."
                            ),
                            options=[],
                        )
                    except DjangoValidationError:
                        # Dynamic capture is enrichment, not a reason to lose a
                        # valid customer reply. Capacity/name/type validation can
                        # safely skip this optional candidate.
                        continue
                    created_keys.append(definition.key)
                existing[definition.key] = definition

            if definition is None:
                raise CRMActionExecutionError(
                    f"Unknown attribute key: {requested_key}."
                )
            if is_sensitive_attribute_definition(
                {"key": definition.key, "name": definition.name}
            ):
                raise CRMActionExecutionError(
                    "Sensitive attributes cannot be written by AI."
                )
            requested_values[definition.key] = item["value"]

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
            "created_keys": created_keys,
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

        from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn
        from apps.ai_engagement.services.qualification_execution_contract import _config
        requirements = _requirements_for_turn(organization=organization, lead=lead)
        completion_config = _config(organization=organization, requirements=requirements)
        completion_target = completion_config.get("completion_stage")
        is_completion_target = (
            str(stage.name or "").strip().casefold() == "qualified"
            or (isinstance(completion_target, dict) and str(completion_target.get("id")) == str(stage.id))
            or str(stage.id) in completion_config.get("protected_completion_stage_ids", [])
        )
        if is_completion_target and stage.id != lead.stage_id:
            from apps.ai_engagement.services.qualification_state import normalize_stage_name
            if normalize_stage_name(getattr(lead.stage, "name", "")) != "new lead":
                raise CRMActionExecutionError("Automatic qualification is only available in New Leads.")
            from apps.ai_engagement.services.playbook import criteria_for_lead
            if not criteria_for_lead(lead=lead, requirements=requirements).get("qualified"):
                raise CRMActionExecutionError("AI Playbook qualification criteria are not satisfied.")

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
