"""Campaign configuration and durable preparation, built on SHVYA CRM services."""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.channels.campaign_models import CampaignDelivery, CampaignPlan
from apps.channels.models import BulkMessageCampaign, BulkMessageRecipient, WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import Lead, Stage
from services.crm.lead_service import DuplicateLeadError, create_lead, upsert_lead

from .campaign_audience import (
    is_suppressed, owned_upload, require_manage, rights, source_catalog, user_pipelines, uuid_value,
)
from .campaign_policy import (
    CampaignInputError, fingerprint, integer, render_message, retry_decision, scheduled_time, template_fields,
)

logger = logging.getLogger(__name__)


def account_ready(account):
    return (account.connection_type == WhatsAppAccount.ConnectionType.API and account.is_active
            and account.status == WhatsAppAccount.Status.CONNECTED and bool(account.phone_number_id)
            and bool(account.access_token))


def get_template(*, user, template_id):
    template = WhatsAppTemplate.objects.filter(
        pk=uuid_value(template_id, "Template"), organization_id=user.organization_id,
        account__organization_id=user.organization_id, account__connection_type=WhatsAppAccount.ConnectionType.API,
    ).select_related("account", "meta_state").first()
    if template is None:
        raise PermissionDenied
    if template.status != WhatsAppTemplate.Status.APPROVED or not template.meta_template_id:
        raise CampaignInputError("Only Meta-approved, synchronized templates can be sent.")
    if not account_ready(template.account):
        raise CampaignInputError("The selected WhatsApp API or Coexistence account is not connected.")
    return template


def template_snapshot(template):
    state = getattr(template, "meta_state", None)
    if state is None or state.local_status in {"deleted", "remote_deleted"}:
        raise CampaignInputError("Sync this template from Meta before using it in a campaign.")
    spec = {
        "id": str(template.pk), "account_id": str(template.account_id), "meta_id": template.meta_template_id,
        "name": template.name, "category": template.category, "language": state.language,
        "components": state.components, "placeholder_mapping": state.placeholder_mapping,
    }
    template_fields(spec)
    return spec


def default_bindings(spec, sources):
    keys = {item["key"] for item in sources}
    result = {}
    for field in template_fields(spec):
        source = field["source"]
        source = source if source in keys else f"attr:{source}" if f"attr:{source}" in keys else ""
        result[field["key"]] = {"source": source, "default": ""}
    return result


def preview_campaign(*, user, upload, template_id, bindings):
    if not upload.review_digest or not upload.reviewed_rows:
        raise CampaignInputError("Review a non-empty audience before selecting a template.")
    template = get_template(user=user, template_id=template_id)
    spec = template_snapshot(template)
    sources = source_catalog(user)
    allowed = {item["key"] for item in sources}
    previews, failures = [], []
    for row in upload.reviewed_rows:
        try:
            rendered = render_message(spec, bindings, row["values"], allowed)
            if len(previews) < 5:
                previews.append({"row": row["row"], "name": row["name"], "body": rendered["body"]})
        except CampaignInputError as exc:
            failures.append({"row": row["row"], "reason": str(exc)})
    return {
        "ready": not failures, "recipient_count": len(upload.reviewed_rows), "missing_count": len(failures),
        "errors": failures[:25], "previews": previews,
        "digest": fingerprint({"audience": upload.review_digest, "template": spec, "bindings": bindings}),
        "spec": spec,
    }


def _wake(campaign_id):
    from apps.channels.campaign_tasks import prepare_campaign_task

    try:
        prepare_campaign_task.delay(str(campaign_id))
    except Exception:
        # The database is the durable queue. Beat will recover this publication.
        logger.exception("Campaign preparation wake-up failed for %s; retained for recovery", campaign_id)


@transaction.atomic
def confirm_campaign(*, user, data):
    upload = owned_upload(user=user, token=data.get("upload_id"), lock=True)
    existing = CampaignPlan.objects.filter(upload=upload).select_related("campaign").first()
    if existing:
        require_manage(user, existing.campaign)
        return existing.campaign, False
    if data.get("confirmed") is not True or data.get("consent_confirmed") is not True:
        raise CampaignInputError("Confirm the campaign and that these recipients have permitted WhatsApp contact.")
    if str(data.get("audience_digest")) != upload.review_digest:
        raise CampaignInputError("The audience changed. Review it again before confirming.")
    excluded = upload.review_stats.get("rows", 0) - upload.review_stats.get("eligible", 0)
    if excluded and data.get("accept_exclusions") is not True:
        raise CampaignInputError("Acknowledge the excluded rows before continuing.")
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 150:
        raise CampaignInputError("Enter a campaign name of 1–150 characters.")
    primary = user_pipelines(user).filter(pk=uuid_value(upload.review_config.get("pipeline_id"))).first()
    if primary is None or not rights(user, primary)["can_edit_leads"]:
        raise PermissionDenied
    preview = preview_campaign(user=user, upload=upload, template_id=data.get("template_id"), bindings=data.get("bindings"))
    if not preview["ready"]:
        raise CampaignInputError("Some recipients have missing template parameters. Complete the mapping or fallback values.")
    if preview["digest"] != data.get("preview_digest"):
        raise CampaignInputError("The template or its values changed. Preview it again before confirming.")
    zone = str(data.get("timezone") or "UTC")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise CampaignInputError("Choose a valid IANA time zone.") from exc
    due = scheduled_time(data.get("scheduled_local"), zone, now=timezone.now()) if data.get("schedule_enabled") is True else timezone.now()
    attempts = integer(data.get("retry_attempts", 3), minimum=0, maximum=3, label="Retry attempts")
    delay = integer(data.get("retry_delay_hours", 24), minimum=1, maximum=168, label="Retry delay in hours")
    auto_retry = data.get("auto_retry") is True
    if auto_retry and attempts == 0:
        raise CampaignInputError("Choose at least one retry attempt or turn automatic retries off.")
    template = get_template(user=user, template_id=data["template_id"])
    campaign = BulkMessageCampaign.objects.create(
        organization_id=user.organization_id, account=template.account, created_by=user, name=name,
        pipeline=primary, body=preview["previews"][0]["body"], template_name=template.name,
        status=BulkMessageCampaign.Status.QUEUED,
    )
    CampaignPlan.objects.create(
        campaign=campaign, upload=upload, template=template, template_snapshot=preview["spec"], bindings=data["bindings"],
        scheduled_for=due, timezone=zone, auto_retry=auto_retry, retry_attempts=attempts, retry_delay_hours=delay,
        consent_at=timezone.now(), consent_by=user,
        stats={**{key: value for key, value in upload.review_stats.items() if key != "errors"},
               "new_leads_created": 0, "existing_leads_updated": 0, "preparation_skipped": 0, "preparation_errors": []},
    )
    transaction.on_commit(lambda: _wake(campaign.pk))
    return campaign, True


def validate_plan(plan):
    user = plan.consent_by
    if user is None or not user.is_active or user.organization_id != plan.campaign.organization_id:
        raise CampaignInputError("The campaign owner is no longer an active member of this organization.")
    require_manage(user, plan.campaign)
    if plan.template_id is None:
        raise CampaignInputError("The campaign template has been deleted.")
    template = get_template(user=user, template_id=plan.template_id)
    if template.account_id != plan.campaign.account_id or template_snapshot(template) != plan.template_snapshot:
        raise CampaignInputError("The template changed after review. Cancel this campaign and create a new one with the current definition.")
    return user


def _materialize_lead(*, user, row, config):
    target = user_pipelines(user).filter(pk=uuid_value(row["pipeline_id"])).first()
    stage = Stage.objects.filter(pk=uuid_value(row["stage_id"]), pipeline=target, is_active=True).first() if target else None
    if target is None or stage is None:
        raise CampaignInputError("The reviewed pipeline or stage is no longer available.")
    permitted = rights(user, target)
    if not permitted["can_edit_leads"]:
        raise PermissionDenied
    lead = Lead.objects.select_for_update().filter(organization_id=user.organization_id, phone=row["phone"]).first()
    if row["existing_id"]:
        if lead is None or str(lead.pk) != row["existing_id"]:
            raise CampaignInputError("The reviewed lead was deleted or its phone changed; it was not recreated.")
        current = user_pipelines(user).filter(pk=lead.pipeline_id).first()
        if current is None or not rights(user, current)["can_edit_leads"]:
            raise PermissionDenied
        if config["update_existing"]:
            if row.get("existing_updated_at") and lead.updated_at.isoformat() != row["existing_updated_at"]:
                raise CampaignInputError("This lead was edited after review. It was not overwritten.")
            if config["move_existing"] and (not permitted["can_move_leads"] or not rights(user, current)["can_move_leads"]):
                raise PermissionDenied
            lead, _ = upsert_lead(
                organization=user.organization, pipeline=target if config["move_existing"] else None,
                stage=stage if config["move_existing"] else None, name=row["name"], phone=row["phone"], email=row["email"],
                attributes=row["attribute_changes"], lead_source="csv_import", send_welcome=False,
            )
            return lead, "existing_leads_updated"
        return lead, None
    if lead is not None:
        raise CampaignInputError("A lead with this phone appeared after review. The row was skipped rather than changing an unreviewed lead.")
    if not permitted["can_create_leads"]:
        raise PermissionDenied
    return create_lead(
        organization=user.organization, pipeline=target, stage=stage, name=row["name"], phone=row["phone"],
        email=row["email"], attributes=row["attribute_changes"], lead_source="csv_import", send_welcome=False,
    ), "new_leads_created"


@transaction.atomic
def prepare_campaign_chunk(campaign_id, limit=100):
    plan = CampaignPlan.objects.select_for_update(of=("self",)).select_related(
        "campaign", "campaign__organization", "consent_by__organization", "upload", "template__account",
    ).filter(pk=campaign_id).first()
    if plan is None or plan.prepared_at or plan.cancelled_at or plan.prepare_error:
        return False
    try:
        user = validate_plan(plan)
        if plan.upload_id is None:
            raise CampaignInputError("The reviewed upload is no longer available.")
    except (CampaignInputError, PermissionDenied) as exc:
        plan.prepare_error = str(exc) or "The campaign owner no longer has the required pipeline access."
        plan.save(update_fields=["prepare_error", "updated_at"])
        plan.campaign.status = BulkMessageCampaign.Status.FAILED
        plan.campaign.completed_at = timezone.now()
        plan.campaign.save(update_fields=["status", "completed_at"])
        return False
    upload = plan.upload
    rows = upload.reviewed_rows[plan.prepare_cursor:plan.prepare_cursor + limit]
    allowed = {item["key"] for item in source_catalog(user)}
    stats = dict(plan.stats)
    for row in rows:
        lead, recipient, reason, changed = None, None, "", None
        rendered = {"body": "", "components": []}
        try:
            with transaction.atomic():
                rendered = render_message(plan.template_snapshot, plan.bindings, row["values"], allowed)
                if is_suppressed(organization_id=user.organization_id, phone=row["phone"]):
                    raise CampaignInputError("Recipient opted out after audience review.")
                lead, changed = _materialize_lead(user=user, row=row, config=upload.review_config)
                if is_suppressed(organization_id=user.organization_id, phone=row["phone"], lead=lead):
                    raise CampaignInputError("Recipient opted out of campaign contact.")
                recipient, _ = BulkMessageRecipient.objects.get_or_create(campaign=plan.campaign, lead=lead)
        except (CampaignInputError, ValidationError, DuplicateLeadError, PermissionDenied) as exc:
            reason = str(exc) if not isinstance(exc, PermissionDenied) else "Required CRM permissions are no longer available."
            reason = reason[:1000]
            lead, recipient, changed = None, None, None
        delivery, created = CampaignDelivery.objects.get_or_create(
            campaign=plan.campaign, phone=row["phone"], defaults={
                "recipient": recipient, "lead": lead, "name": row["name"],
                "pipeline_key": lead.pipeline_id if lead else row["pipeline_id"],
                "stage_key": lead.stage_id if lead else row["stage_id"],
                "pipeline_label": lead.pipeline.name if lead else row["pipeline_label"],
                "stage_label": lead.stage.name if lead else row["stage_label"],
                "values": row["values"], "body": rendered["body"], "components": rendered["components"],
                "state": "skipped" if reason else "pending", "error_message": reason,
                "due_at": None if reason else max(timezone.now(), plan.scheduled_for),
            },
        )
        if created and reason:
            stats["preparation_skipped"] = stats.get("preparation_skipped", 0) + 1
            stats.setdefault("preparation_errors", []).append({"row": row["row"], "reason": reason})
        if created and changed:
            stats[changed] = stats.get(changed, 0) + 1
        plan.prepare_cursor += 1
    plan.stats = stats
    done = plan.prepare_cursor >= len(upload.reviewed_rows)
    if done:
        plan.prepared_at = timezone.now()
        upload.rows = []
        upload.reviewed_rows = []
        upload.save(update_fields=["rows", "reviewed_rows"])
    plan.save(update_fields=["prepare_cursor", "prepared_at", "stats", "updated_at"])
    return not done


def retry_eligibility(delivery, plan, *, now):
    if plan.cancelled_at:
        return False, None, "This campaign was cancelled."
    if delivery.delivered_at or delivery.read_at or delivery.replied_at:
        return False, None, "A delivered, read or replied-to recipient cannot be retried."
    if delivery.state != "failed" or not delivery.failed_at:
        return False, None, "Only definitively failed recipients are retry candidates."
    return retry_decision(
        code=delivery.error_code, http_status=delivery.http_status, uncertain=delivery.uncertain,
        attempts=delivery.attempt_count, maximum_retries=plan.retry_attempts,
        failed_at=delivery.failed_at, delay_hours=plan.retry_delay_hours, now=now,
    )


@transaction.atomic
def retry_recipients(*, user, campaign, selection):
    plan = CampaignPlan.objects.select_for_update().get(campaign=campaign)
    require_manage(user, campaign)
    validate_plan(plan)
    now, queued, ignored = timezone.now(), 0, 0
    details = []
    for delivery in selection.select_for_update(of=("self",)).order_by("pk"):
        if delivery.campaign_id != campaign.pk:
            raise PermissionDenied
        permitted, due, reason = retry_eligibility(delivery, plan, now=now)
        if permitted and is_suppressed(organization_id=user.organization_id, phone=delivery.phone, lead=delivery.lead):
            permitted, reason = False, "Recipient has opted out."
        if permitted:
            delivery.state, delivery.due_at, delivery.published_at = "pending", due, None
            delivery.save(update_fields=["state", "due_at", "published_at", "updated_at"])
            if delivery.recipient_id:
                BulkMessageRecipient.objects.filter(pk=delivery.recipient_id).update(status="pending")
            queued += 1
        else:
            ignored += 1
            if len(details) < 20:
                details.append({"id": str(delivery.pk), "reason": reason})
    if queued:
        campaign.status, campaign.completed_at = BulkMessageCampaign.Status.QUEUED, None
        campaign.save(update_fields=["status", "completed_at"])
    return {"queued": queued, "not_queued": ignored, "reasons": details}


@transaction.atomic
def cancel_campaign(*, user, campaign):
    plan = CampaignPlan.objects.select_for_update().get(campaign=campaign)
    require_manage(user, campaign)
    if not plan.cancelled_at:
        if plan.prepared_at and not campaign.campaign_delivery_rows.filter(state__in=["pending", "sending"]).exists():
            raise CampaignInputError("This campaign has already finished processing.")
        plan.cancelled_at = timezone.now()
        plan.save(update_fields=["cancelled_at", "updated_at"])
        pending = campaign.campaign_delivery_rows.filter(state="pending")
        BulkMessageRecipient.objects.filter(delivery_state__in=pending).update(status="skipped", skip_reason="Campaign cancelled before sending.")
        count = pending.update(state="skipped", due_at=None, error_message="Campaign cancelled before sending.")
        campaign.status, campaign.completed_at = BulkMessageCampaign.Status.COMPLETED, timezone.now()
        campaign.save(update_fields=["status", "completed_at"])
        return count
    return 0


def finish_campaign(campaign_id):
    """Completion means queue processing ended, not that every message delivered."""
    with transaction.atomic():
        plan = CampaignPlan.objects.select_for_update().select_related("campaign").filter(pk=campaign_id).first()
        if not plan or not plan.prepared_at or plan.cancelled_at or plan.prepare_error:
            return
        rows = plan.campaign.campaign_delivery_rows
        if rows.filter(state__in=["pending", "sending"]).exists():
            return
        campaign = plan.campaign
        all_failed = rows.exists() and not rows.exclude(state__in=["failed", "review"]).exists()
        campaign.status = BulkMessageCampaign.Status.FAILED if all_failed else BulkMessageCampaign.Status.COMPLETED
        if campaign.completed_at is None:
            campaign.completed_at = timezone.now()
        campaign.save(update_fields=["status", "completed_at"])
