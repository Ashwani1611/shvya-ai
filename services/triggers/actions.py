"""Execute CRM actions and queue messages through existing SHVYA services."""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, LeadReminder, Stage
from apps.followups.models import FollowupSequence, LeadSequenceState
from apps.integrations.services.email import (
    EmailConfigurationError,
    send_organization_email,
)
from apps.triggers.models import TriggerRun
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
    move_lead_to_stage,
)
from services.followup_service import (
    FollowupError,
    _render_text,
    assign_sequence,
    clear_sequence,
    set_lead_followup_enabled,
)
from services.triggers.evaluator import causal_rules, delta, timer_valid

logger = logging.getLogger(__name__)


def scheduled_at(action, lead, reference):
    mode = action["schedule"]
    if mode == "relative":
        return reference + delta(action)
    zone = ZoneInfo(lead.organization.timezone or "UTC")
    if mode == "fixed":
        local = reference.astimezone(zone)
        due = datetime.combine(
            local.date(), time.fromisoformat(action["time"]), tzinfo=zone
        )
        return due if due > reference else due + timedelta(days=1)
    value = (lead.attributes or {}).get(action["date_attribute"])
    if not value:
        raise ValueError("The lead has no value for the selected date-time attribute.")
    due = datetime.fromisoformat(str(value))
    if timezone.is_naive(due):
        due = due.replace(tzinfo=zone)
    if due <= reference:
        raise ValueError("The selected lead date-time is in the past.")
    return due


def run_block_reason(run, lead):
    """Recheck cancellation and tenant boundaries at the execution boundary."""
    if (run.rule.organization_id != lead.organization_id
            or run.event.organization_id != lead.organization_id):
        return "Workflow data does not belong to the lead organization."
    if not lead.organization.is_active:
        return "Organization is inactive."
    if not run.rule.enabled:
        return "Workflow is disabled."
    if not timer_valid(run.event, lead):
        return "Timer cancelled by newer lead activity."
    return ""


def workflow_message_block_reason(message):
    """No provider call here; used by every retry of a tagged workflow send."""
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    metadata = payload.get("shvya_workflow")
    if not isinstance(metadata, dict):
        return "Invalid workflow delivery metadata."
    try:
        run = TriggerRun.objects.select_related(
            "rule", "event", "lead__organization", "lead__pipeline", "lead__stage"
        ).filter(id=metadata.get("run_id"), message_id=message.pk).first()
    except (ValueError, ValidationError):
        return "Invalid workflow delivery reference."
    if run is None:
        return "Workflow was deleted or its delivery reference is unavailable."
    if (run.lead_id != message.lead_id
            or run.rule.organization_id != message.organization_id
            or message.account.organization_id != message.organization_id
            or str(message.account_id) != str(run.action.get("account"))):
        return "Workflow sender or lead does not match the queued message."
    reason = run_block_reason(run, run.lead)
    if reason:
        return reason
    if run.status not in ("queued", "dispatching"):
        return "Workflow delivery is no longer pending."
    if (not message.account.is_active or message.account.status != "connected"
            or message.account.connection_type not in (WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted)):
        return "The selected WhatsApp account is no longer connected."
    if message.account.connection_type == WhatsAppAccount.ConnectionType.API and not WhatsAppMessage.objects.filter(
        organization_id=message.organization_id, lead_id=message.lead_id,
        account_id=message.account_id, direction="inbound",
        created_at__gte=timezone.now() - timedelta(hours=24),
    ).exists():
        return "WhatsApp reply window expired. Use an approved-template Cadence sequence."
    return ""


def defer_workflow_message_for_health(message):
    """Keep the same queued send until the existing Hosted health pause ends."""
    if message.account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        return False
    from services.channels.hosted_health_guard import hosted_health_pause_until

    until = hosted_health_pause_until(account=message.account)
    if not until:
        return False
    metadata = (message.raw_payload or {}).get("shvya_workflow") or {}
    return bool(TriggerRun.objects.filter(
        id=metadata.get("run_id"), message_id=message.pk,
        lead_id=message.lead_id, rule__organization_id=message.organization_id,
        rule__enabled=True, rule__organization__is_active=True,
        status__in=["queued", "dispatching"],
    ).update(
        status="queued", due_at=until, finished_at=None,
        detail="Waiting for the selected Hosted account's Account Health pause to end.",
    ))


@transaction.atomic
def execute(run_id):
    initial = TriggerRun.objects.get(id=run_id)
    lead = (
        Lead.objects.select_for_update(of=("self",))
        .select_related("organization", "pipeline", "stage")
        .get(id=initial.lead_id, organization_id=initial.rule.organization_id)
    )
    run = (
        TriggerRun.objects.select_for_update(of=("self",))
        .select_related("rule", "rule__created_by", "event")
        .get(id=run_id)
    )
    if run.status not in ("pending", "scheduled") or run.due_at > timezone.now():
        return
    reason = run_block_reason(run, lead)
    if reason:
        run.status, run.detail = "skipped", reason
    else:
        try:
            # Roll back partial CRM work if an action fails.
            with transaction.atomic():
                token = causal_rules.set(
                    (*run.event.payload.get("causal_rules", []), str(run.rule_id))
                )
                try:
                    _apply(run, lead)
                finally:
                    causal_rules.reset(token)
        except (
            ValueError,
            ValidationError,
            LeadTransitionError,
            FollowupError,
            ObjectDoesNotExist,
        ) as exc:
            run.status = "failed"
            run.detail = str(exc)[:1000]
        except Exception:
            logger.exception("Smart Trigger action failed: %s", run.id)
            run.status, run.detail = (
                "failed",
                "The action could not complete. Contact an administrator with this run ID.",
            )
    run.finished_at = (
        None if run.status in ("pending", "scheduled", "queued", "email_ready")
        else timezone.now()
    )
    run.save()


def _apply(run, lead):
    a, kind = run.action, run.action_type
    actor = run.rule.created_by
    org = lead.organization
    if actor and (actor.organization_id != lead.organization_id or not actor.is_active):
        actor = None
    if kind == "start_sequence":
        if (
            not a["replace"]
            and LeadSequenceState.objects.filter(
                lead=lead, organization=org, status__in=["active", "paused"]
            ).exists()
        ):
            run.status, run.detail = (
                "skipped",
                "The lead already has an assigned sequence.",
            )
            return
        sequence = FollowupSequence.objects.get(
            id=a["sequence"], organization=org, is_active=True
        )
        assign_sequence(lead=lead, sequence=sequence, actor=actor)
    elif kind == "stop_sequence":
        clear_sequence(lead=lead)
    elif kind == "followup":
        set_lead_followup_enabled(lead=lead, enabled=a["enabled"])
    elif kind == "ai":
        lead.ai_enabled = a["enabled"]
        lead.save(update_fields=["ai_enabled", "updated_at"])
    elif kind == "move_stage":
        stage = Stage.objects.select_related("pipeline").get(
            id=a["stage"],
            pipeline_id=a["pipeline"],
            pipeline__organization=org,
            pipeline__is_active=True,
            is_active=True,
        )
        if stage.id == lead.stage_id:
            run.status, run.detail = "skipped", "Lead is already in this stage."
            return

        if lead.pipeline_id == stage.pipeline_id:
            move_lead_to_stage(
                lead=lead,
                stage=stage,
                actor=actor,
            )
        else:
            move_lead_to_pipeline_stage(
                lead=lead,
                pipeline=stage.pipeline,
                stage=stage,
                actor=actor,
            )
    elif kind == "attribute":
        from apps.crm.models import AttributeDefinition
        from services.triggers.rules import attribute_value

        definition = AttributeDefinition.objects.get(organization=org, key=a["key"])
        value = attribute_value(definition, a["value"])
        lead.attributes = {**(lead.attributes or {}), a["key"]: value}
        lead.save(update_fields=["attributes", "updated_at"])
    elif kind == "reminder":
        existing = LeadReminder.objects.filter(lead=lead, status="pending")
        if existing.exists() and not a["overwrite"]:
            run.status, run.detail = (
                "skipped",
                "The lead already has a pending reminder.",
            )
            return
        if a["overwrite"]:
            existing.update(status="cancelled")
        LeadReminder.objects.create(
            lead=lead,
            assigned_to=actor,
            title=run.rule.name[:200],
            description=_render_text(a["note"], lead, actor),
            due_at=run.event.created_at + delta(a),
        )
    elif kind == "message":
        if run.status == "pending":
            run.due_at = scheduled_at(a, lead, run.event.created_at)
            run.status = "scheduled"
            return
        account = WhatsAppAccount.objects.get(
            id=a["account"], organization=org, is_active=True, status="connected",
            connection_type__in=[WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted],
        )
        # SHVYA's API transport supports free text only in the active 24h window.
        if account.connection_type == WhatsAppAccount.ConnectionType.API and not WhatsAppMessage.objects.filter(
            organization=org,
            lead=lead,
            account=account,
            direction="inbound",
            created_at__gte=timezone.now() - timedelta(hours=24),
        ).exists():
            run.status, run.detail = (
                "blocked",
                "No active WhatsApp reply window. Use an approved-template follow-up sequence.",
            )
            return
        from services.channels.whatsapp_service import queue_outbound_message

        run.message = queue_outbound_message(
            organization=org,
            account=account,
            lead=lead,
            to_number=lead.phone,
            body=_render_text(a["body"], lead, actor),
        )
        run.message.raw_payload = {
            **(run.message.raw_payload or {}),
            "shvya_workflow": {"run_id": str(run.id)},
        }
        run.message.save(update_fields=["raw_payload", "updated_at"])
        run.status, run.detail = "queued", "Queued through the selected WhatsApp account."
        return
    elif kind == "email":
        if not lead.email:
            run.status, run.detail = "skipped", "Lead has no email address."
            return
        # Email is dispatched separately after a durable sending claim is committed.
        # The delivery worker uses the organization's connected Connect Hub mailbox.
        run.status = "email_ready"
        return
    else:
        raise ValueError("Unsupported action.")
    run.status = "completed"


def deliver_email(run_id):
    # At-most-once automatic delivery: uncertain SMTP outcomes require review,
    # never an automatic duplicate. Message-ID is stable for provider tracing.
    if not TriggerRun.objects.filter(id=run_id, status="email_ready").update(
        status="sending", due_at=timezone.now() + timedelta(minutes=10)
    ):
        return
    try:
        run = TriggerRun.objects.select_related(
            "lead__organization", "lead__pipeline", "lead__stage", "rule__created_by", "event"
        ).get(id=run_id)
    except TriggerRun.DoesNotExist:
        return
    try:
        reason = run_block_reason(run, run.lead)
        if reason:
            run.status, run.detail = "skipped", reason
        elif not run.lead.email:
            run.status, run.detail = "skipped", "Lead has no email address."
        else:
            subject = (
                _render_text(run.action["subject"], run.lead, run.rule.created_by)
                .replace("\r", " ")
                .replace("\n", " ")
            )
            body = _render_text(run.action["body"], run.lead, run.rule.created_by)
            sent = send_organization_email(
                organization=run.lead.organization,
                to=run.lead.email,
                subject=subject,
                text_body=body,
                headers={"Message-ID": f"<smart-trigger-{run.id}@shvya-ai.com>"},
            )
            if sent == 0:
                raise EmailConfigurationError("The connected mailbox did not send the email.")
            run.status = "completed"
    except EmailConfigurationError as exc:
        run.status, run.detail = "failed", str(exc)[:1000]
    except Exception:
        logger.exception("Smart Trigger email outcome is uncertain: %s", run.id)
        run.status, run.detail = (
            "needs_review",
            "Email delivery could not be confirmed; check provider logs before retrying.",
        )
    # A deleted workflow must not be recreated by a late provider response.
    TriggerRun.objects.filter(pk=run.pk, status="sending").update(
        status=run.status, detail=run.detail, finished_at=timezone.now()
    )
