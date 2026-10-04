"""Cadence and Hosted WhatsApp step tools for SHVYA Operations MCP."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy

from django.core.files.base import ContentFile
from django.db import transaction

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.followups.models import FollowupExecution, FollowupSequence, FollowupStep
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.integrations.operations.constants import MAX_MCP_CADENCE_ATTACHMENT_BYTES
from apps.integrations.operations.attachment_payloads import (
    AttachmentPayloadError,
    decode_email_attachments,
    redact_attachment_content,
)
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_CADENCE_CONFIG_WRITE, approval_required
from services.channels.hosted_automation_service import (
    HostedAutomationError,
    add_hosted_whatsapp_step as domain_add_hosted_whatsapp_step,
    update_hosted_whatsapp_step as domain_update_hosted_whatsapp_step,
)
from services.followup_service import (
    FollowupError,
    _recalculate_active_states,
    _validate_schedule,
    delete_step,
    replace_email_step_attachments,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsToolError,
    ToolExecution,
    _cadence_schedule,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _uuid,
    _write_gate,
)

def _cadence(organization, cadence_id, *, active=True):
    query = FollowupSequence.objects.filter(
        pk=_uuid(cadence_id, field="cadence_id"),
        organization=organization,
    )
    if active:
        query = query.filter(is_active=True)
    sequence = query.select_related("whatsapp_account").defer(
        "whatsapp_account__access_token"
    ).first()
    if sequence is None:
        raise OperationsToolError("Cadence not found in this organization.")
    return sequence


def _attachment_from_data(data):
    encoded = str(data.get("attachment_base64") or "").strip()
    if not encoded:
        return None
    filename = str(data.get("attachment_name") or "").strip()
    if not filename:
        raise OperationsToolError("attachment_name is required with attachment_base64.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationsToolError("attachment_base64 is invalid.") from exc
    if not raw or len(raw) > MAX_MCP_CADENCE_ATTACHMENT_BYTES:
        raise OperationsToolError("MCP attachment must be between 1 byte and 50 MiB.")
    upload = ContentFile(raw, name=filename)
    upload.content_type = str(data.get("attachment_mime_type") or "")
    return upload


def add_hosted_whatsapp_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="add_hosted_whatsapp_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    if not sequence.whatsapp_account_id or sequence.whatsapp_account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        raise OperationsToolError("The selected Cadence is not a Hosted WhatsApp Cadence.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Hosted WhatsApp step object.")
    _reject_secret_like_content(
        {key: value for key, value in data.items() if key != "attachment_base64"},
        field="hosted_cadence_step",
    )
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        raise OperationsToolError("Hosted WhatsApp step title and body are required.")
    schedule = _cadence_schedule(data)
    attachment_name = str(data.get("attachment_name") or "").strip()
    attachment_size = 0
    if data.get("attachment_base64"):
        try:
            attachment_size = len(base64.b64decode(str(data["attachment_base64"]), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise OperationsToolError("attachment_base64 is invalid.") from exc
    proposal = {
        "cadence_id": str(sequence.id),
        "existing_step_count": sequence.steps.count(),
        "next_position": sequence.steps.count() + 1,
        "title": title,
        "body": body,
        "schedule": schedule,
        "attachment_name": attachment_name,
        "attachment_size": attachment_size,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "next_position": proposal["next_position"],
                "schedule_type": schedule["schedule_type"],
                "attachment_size": attachment_size,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "add_hosted_whatsapp_step", "proposal_digest": _proposal_digest(proposal)},
        )

    with transaction.atomic():
        sequence = (
            FollowupSequence.objects.select_for_update()
            .select_related("whatsapp_account")
            .filter(pk=sequence.pk, organization=organization, is_active=True)
            .first()
        )
        if sequence is None:
            raise OperationsApprovalRequired("The Cadence changed after review. Run a fresh dry-run.")
        locked_proposal = {
            **proposal,
            "existing_step_count": sequence.steps.count(),
            "next_position": sequence.steps.count() + 1,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        attachment = _attachment_from_data(data)
        try:
            step = domain_add_hosted_whatsapp_step(
                sequence=sequence,
                title=title,
                body=body,
                attachment=attachment,
                **schedule,
            )
        except (HostedAutomationError, FollowupError) as exc:
            raise OperationsToolError(str(exc)) from exc
        step.refresh_from_db()
        hosted = HostedFollowupStepConfig.objects.get(step=step)
        if hosted.body != body or step.position != locked_proposal["next_position"]:
            raise OperationsToolError("Hosted Cadence step verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence_id": str(sequence.id),
            "step": {
                "id": str(step.id),
                "type": step.step_type,
                "position": step.position,
                "title": step.title,
                "body": hosted.body,
                "has_attachment": bool(hosted.attachment),
                "schedule_type": step.schedule_type,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "add_hosted_whatsapp_step", "cadence_id": str(sequence.id), "verification": "passed"},
    )


def _step_snapshot(step):
    hosted = None
    if (
        step.step_type == FollowupStep.StepType.WHATSAPP
        and step.sequence.whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.coexisted
    ):
        hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
    return {
        "id": str(step.id),
        "type": step.step_type,
        "position": step.position,
        "title": step.title,
        "template_id": str(step.whatsapp_template_id) if step.whatsapp_template_id else None,
        "email_subject": step.email_subject,
        "email_body": step.email_body,
        "email_attachments": [
            {
                "name": item.original_name,
                "mime_type": item.mime_type,
                "size": item.size,
            }
            for item in step.attachments.order_by("position", "created_at")
        ] if step.step_type == FollowupStep.StepType.EMAIL else [],
        "reminder_text": step.reminder_text,
        "hosted_body": hosted.body if hosted else None,
        "hosted_attachment_name": hosted.attachment_original_name if hosted else "",
        "schedule": {
            "type": step.schedule_type,
            "delay_value": step.delay_value,
            "delay_unit": step.delay_unit,
            "time": step.specific_time.isoformat() if step.specific_time else None,
            "weekday": step.specific_weekday,
            "recurring_every": step.recurring_every,
            "recurring_unit": step.recurring_unit,
            "weekdays": list(step.recurring_weekdays or []),
        },
        "is_active": step.is_active,
    }


def update_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="update_cadence_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step = sequence.steps.filter(
        pk=_uuid((arguments or {}).get("step_id"), field="step_id")
    ).select_related("sequence__whatsapp_account", "whatsapp_template").first()
    if step is None:
        raise OperationsToolError("Cadence step not found.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence step object.")
    _reject_secret_like_content(
        redact_attachment_content(data),
        field="cadence_step",
    )
    schedule = _cadence_schedule(data)
    before = _step_snapshot(step)
    after = deepcopy(before)
    after["schedule"] = {
        "type": schedule["schedule_type"],
        "delay_value": schedule["delay_value"],
        "delay_unit": schedule["delay_unit"],
        "time": schedule["specific_time"].isoformat() if schedule["specific_time"] else None,
        "weekday": schedule["specific_weekday"],
        "recurring_every": schedule["recurring_every"],
        "recurring_unit": schedule["recurring_unit"],
        "weekdays": schedule["recurring_weekdays"],
    }
    after["is_active"] = data.get("is_active", step.is_active)
    if not isinstance(after["is_active"], bool):
        raise OperationsToolError("is_active must be a boolean.")

    email_attachments = None
    email_attachment_descriptors = []
    email_attachments_supplied = "attachments" in data
    remove_email_attachments = bool(data.get("remove_attachments", False))
    if email_attachments_supplied and remove_email_attachments:
        raise OperationsToolError(
            "Use either attachments or remove_attachments, not both."
        )

    hosted = (
        step.step_type == FollowupStep.StepType.WHATSAPP
        and sequence.whatsapp_account.connection_type == WhatsAppAccount.ConnectionType.coexisted
    )
    template = None
    if hosted:
        after["title"] = str(data.get("title", step.title) or "").strip()
        current_hosted = HostedFollowupStepConfig.objects.filter(step=step).first()
        after["hosted_body"] = str(
            data.get("body", current_hosted.body if current_hosted else "") or ""
        ).strip()
        if not after["title"] or not after["hosted_body"]:
            raise OperationsToolError("Hosted WhatsApp title and body are required.")
    elif step.step_type == FollowupStep.StepType.WHATSAPP:
        template_id = data.get("template_id", step.whatsapp_template_id)
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(template_id, field="template_id"),
            organization=organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).first()
        if template is None:
            raise OperationsToolError("Approved WhatsApp template not found for this Cadence account.")
        after["template_id"] = str(template.id)
        after["title"] = template.name
    elif step.step_type == FollowupStep.StepType.EMAIL:
        after["title"] = str(data.get("title", step.title) or "").strip()
        after["email_subject"] = str(data.get("subject", step.email_subject) or "").strip()
        after["email_body"] = str(data.get("body", step.email_body) or "").strip()
        if not after["email_subject"] or not after["email_body"]:
            raise OperationsToolError("Email Cadence step subject and body are required.")
        if email_attachments_supplied:
            try:
                email_attachments, email_attachment_descriptors = decode_email_attachments(data)
            except AttachmentPayloadError as exc:
                raise OperationsToolError(str(exc)) from exc
            after["email_attachments"] = [
                {
                    "name": item["name"],
                    "mime_type": item["mime_type"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                }
                for item in email_attachment_descriptors
            ]
        elif remove_email_attachments:
            after["email_attachments"] = []
    else:
        after["reminder_text"] = str(
            data.get("text", step.reminder_text) or ""
        ).strip()
        if not after["reminder_text"]:
            raise OperationsToolError("Reminder Cadence step text is required.")

    proposal = {"cadence_id": str(sequence.id), "step_id": str(step.id), "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_id": str(step.id),
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence_step",
            target_id=str(step.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "update_cadence_step", "proposal_digest": _proposal_digest(proposal)},
        )

    with transaction.atomic():
        locked_step = (
            FollowupStep.objects.select_for_update(of=("self",))
            .select_related("sequence__whatsapp_account", "whatsapp_template")
            .filter(
                pk=step.pk,
                sequence__organization=organization,
                sequence_id=sequence.id,
            )
            .first()
        )
        if locked_step is None:
            raise OperationsApprovalRequired("The Cadence step changed after review. Run a fresh dry-run.")
        locked_before = _step_snapshot(locked_step)
        locked_proposal = {
            "cadence_id": str(sequence.id),
            "step_id": str(locked_step.id),
            "before": locked_before,
            "after": after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)

        if hosted:
            attachment = _attachment_from_data(data)
            try:
                domain_update_hosted_whatsapp_step(
                    step=locked_step,
                    title=after["title"],
                    body=after["hosted_body"],
                    attachment=attachment,
                    remove_attachment=bool(data.get("remove_attachment", False)),
                )
            except HostedAutomationError as exc:
                raise OperationsToolError(str(exc)) from exc
        elif locked_step.step_type == FollowupStep.StepType.WHATSAPP:
            locked_step.whatsapp_template = template
            locked_step.title = template.name
        elif locked_step.step_type == FollowupStep.StepType.EMAIL:
            locked_step.title = after["title"]
            locked_step.email_subject = after["email_subject"]
            locked_step.email_body = after["email_body"]
            if email_attachments_supplied:
                try:
                    replace_email_step_attachments(
                        step=locked_step,
                        attachments=email_attachments,
                    )
                except FollowupError as exc:
                    raise OperationsToolError(str(exc)) from exc
            elif remove_email_attachments:
                replace_email_step_attachments(
                    step=locked_step,
                    attachments=[],
                )
        else:
            locked_step.reminder_text = after["reminder_text"]

        for field, value in {
            "schedule_type": schedule["schedule_type"],
            "delay_value": schedule["delay_value"],
            "delay_unit": schedule["delay_unit"],
            "specific_time": schedule["specific_time"],
            "specific_weekday": schedule["specific_weekday"],
            "recurring_every": schedule["recurring_every"],
            "recurring_unit": schedule["recurring_unit"],
            "recurring_weekdays": list(schedule["recurring_weekdays"] or []),
            "is_active": after["is_active"],
        }.items():
            setattr(locked_step, field, value)
        try:
            _validate_schedule(
                schedule_type=locked_step.schedule_type,
                delay_value=locked_step.delay_value,
                delay_unit=locked_step.delay_unit,
                specific_time=locked_step.specific_time,
                specific_weekday=locked_step.specific_weekday,
                recurring_every=locked_step.recurring_every,
                recurring_unit=locked_step.recurring_unit,
                recurring_weekdays=locked_step.recurring_weekdays,
            )
        except FollowupError as exc:
            raise OperationsToolError(str(exc)) from exc
        locked_step.save()
        _recalculate_active_states(locked_step.sequence)
        locked_step.refresh_from_db()
        verification = _step_snapshot(locked_step)

    return ToolExecution(
        data={"status": "FIXED", "step": verification, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "update_cadence_step", "verification": "passed"},
    )


def delete_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="delete_cadence_step",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step = sequence.steps.filter(
        pk=_uuid((arguments or {}).get("step_id"), field="step_id")
    ).first()
    if step is None:
        raise OperationsToolError("Cadence step not found.")
    execution_count = FollowupExecution.objects.filter(step=step).count()
    if execution_count:
        raise OperationsToolError(
            "Cadence step has delivery history and cannot be permanently deleted. Archive the Cadence or deactivate/edit the step instead."
        )
    before = _step_snapshot(
        FollowupStep.objects.select_related("sequence__whatsapp_account", "whatsapp_template").get(pk=step.pk)
    )
    proposal = {"cadence_id": str(sequence.id), "step_id": str(step.id), "before": before, "after": None}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_id": str(step.id),
                "affected_execution_count": 0,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": False,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence_step",
            target_id=str(step.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "delete_cadence_step", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = (
            FollowupStep.objects.select_for_update(of=("self",))
            .select_related("sequence__whatsapp_account", "whatsapp_template")
            .filter(pk=step.pk, sequence=sequence)
            .first()
        )
        if locked is None or FollowupExecution.objects.filter(step=locked).exists():
            raise OperationsApprovalRequired("Cadence step dependencies changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "cadence_id": str(sequence.id),
            "step_id": str(locked.id),
            "before": _step_snapshot(locked),
            "after": None,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        delete_step(step=locked)
    return ToolExecution(
        data={"status": "FIXED", "step_id": str(step.id), "deleted": True, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={"operation": "delete_cadence_step", "verification": "passed"},
    )


def reorder_cadence_steps(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="reorder_cadence_steps",
        arguments=arguments,
    )
    sequence = _cadence(organization, (arguments or {}).get("cadence_id"))
    step_ids = (arguments or {}).get("step_ids")
    if not isinstance(step_ids, list) or not all(isinstance(item, str) for item in step_ids):
        raise OperationsToolError("step_ids must be an ordered list of UUID strings.")
    steps = list(sequence.steps.order_by("position", "created_at"))
    existing_ids = [str(step.id) for step in steps]
    if len(step_ids) != len(steps) or set(step_ids) != set(existing_ids):
        raise OperationsToolError("step_ids must contain every current Cadence step exactly once.")
    proposal = {
        "cadence_id": str(sequence.id),
        "before": existing_ids,
        "after": step_ids,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "before": existing_ids,
                "after": step_ids,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "reorder_cadence_steps", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked_steps = list(
            FollowupStep.objects.select_for_update()
            .filter(sequence=sequence)
            .order_by("position", "created_at")
        )
        locked_ids = [str(step.id) for step in locked_steps]
        locked_proposal = {"cadence_id": str(sequence.id), "before": locked_ids, "after": step_ids}
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        if len(step_ids) != len(locked_steps) or set(step_ids) != set(locked_ids):
            raise OperationsApprovalRequired("Cadence steps changed after review. Run a fresh dry-run.")
        by_id = {str(step.id): step for step in locked_steps}
        offset = len(locked_steps) + 1000
        for index, step in enumerate(locked_steps, start=1):
            step.position = offset + index
        FollowupStep.objects.bulk_update(locked_steps, ["position"])
        ordered = []
        for index, step_id in enumerate(step_ids, start=1):
            step = by_id[step_id]
            step.position = index
            ordered.append(step)
        FollowupStep.objects.bulk_update(ordered, ["position"])
        _recalculate_active_states(sequence)
    verified = list(
        sequence.steps.order_by("position", "created_at").values_list("id", flat=True)
    )
    if [str(item) for item in verified] != step_ids:
        raise OperationsToolError("Cadence reorder verification failed.")
    return ToolExecution(
        data={"status": "FIXED", "step_ids": step_ids, "verification": "passed"},
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(sequence.id),
        reason=reason,
        audit_summary={"operation": "reorder_cadence_steps", "verification": "passed"},
    )
