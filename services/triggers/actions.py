"""Execute CRM actions and queue messages through existing SHVYA services."""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, LeadReminder, Stage
from apps.followups.models import FollowupSequence, LeadSequenceState
from apps.triggers.models import TriggerRun
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
        .select_related("rule", "rule__created_by")
        .get(id=run_id)
    )
    if run.status not in ("pending", "scheduled") or run.due_at > timezone.now():
        return
    if not run.rule.enabled or not lead.organization.is_active:
        run.status, run.detail = "skipped", "Rule is disabled."
    elif not timer_valid(run.event, lead):
        run.status, run.detail = "skipped", "Timer cancelled by newer lead activity."
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
        except (ValueError, ValidationError, FollowupError, ObjectDoesNotExist) as exc:
            run.status = "failed"
            run.detail = str(exc)[:1000]
        except Exception:
            logger.exception("Smart Trigger action failed: %s", run.id)
            run.status, run.detail = (
                "failed",
                "The action could not complete. Contact an administrator with this run ID.",
            )
    if run.status not in ("pending", "scheduled"):
        run.finished_at = timezone.now()
    run.save()


def _apply(run, lead):
    a, kind = run.action, run.action_type
    actor = run.rule.created_by
    org = lead.organization
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
        from services.crm_activity_service import (
            record_pipeline_changed,
            record_stage_changed,
        )

        old, old_pipeline = lead.stage, lead.pipeline
        lead.pipeline, lead.stage, lead.stage_entered_at = (
            stage.pipeline,
            stage,
            timezone.now(),
        )
        lead.full_clean()
        lead.save(update_fields=["pipeline", "stage", "stage_entered_at", "updated_at"])
        if old_pipeline.id == stage.pipeline_id:
            record_stage_changed(
                lead=lead,
                actor=actor,
                pipeline=stage.pipeline,
                old_stage=old,
                new_stage=stage,
            )
        else:
            record_pipeline_changed(
                lead=lead,
                actor=actor,
                old_pipeline=old_pipeline,
                new_pipeline=stage.pipeline,
                old_stage=old,
                new_stage=stage,
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
            due_at=run.created_at + delta(a),
        )
    elif kind == "message":
        if run.status == "pending":
            run.due_at = scheduled_at(a, lead, run.created_at)
            run.status = "scheduled"
            return
        account = WhatsAppAccount.objects.get(
            id=a["account"], organization=org, is_active=True, status="connected"
        )
        # SHVYA's API transport supports free text only in the active 24h window.
        if not WhatsAppMessage.objects.filter(
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
        run.status, run.detail = "queued", "Queued through the WhatsApp API."
        return
    elif kind == "email":
        if not lead.email:
            run.status, run.detail = "skipped", "Lead has no email address."
            return
        if not getattr(settings, "FOLLOWUP_EMAIL_DELIVERY_ENABLED", False):
            run.status, run.detail = (
                "blocked",
                "Email sender configuration is not enabled.",
            )
            return
        # Email is dispatched separately after a durable sending claim is committed.
        run.status = "email_ready"
        return
    else:
        raise ValueError("Unsupported action.")
    run.status = "completed"


def deliver_email(run_id):
    # At-most-once automatic delivery: uncertain SMTP outcomes require review,
    # never an automatic duplicate. Message-ID is stable for provider tracing.
    if not TriggerRun.objects.filter(id=run_id, status="email_ready").update(
        status="sending"
    ):
        return
    run = TriggerRun.objects.select_related(
        "lead__organization", "lead__pipeline", "lead__stage", "rule__created_by"
    ).get(id=run_id)
    try:
        if not run.rule.enabled:
            run.status, run.detail = "skipped", "Rule is disabled."
        else:
            subject = (
                _render_text(run.action["subject"], run.lead, run.rule.created_by)
                .replace("\r", " ")
                .replace("\n", " ")
            )
            body = _render_text(run.action["body"], run.lead, run.rule.created_by)
            EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[run.lead.email],
                headers={"Message-ID": f"<smart-trigger-{run.id}@shvya-ai.com>"},
            ).send(fail_silently=False)
            run.status = "completed"
    except Exception:
        logger.exception("Smart Trigger email outcome is uncertain: %s", run.id)
        run.status, run.detail = (
            "needs_review",
            "Email delivery could not be confirmed; check provider logs before retrying.",
        )
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "detail", "finished_at"])
