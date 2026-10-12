"""Persist safe canonical channel state evidence, including verified callbacks.

These are observations of saved state, not a reconstruction of past transitions.
No provider/customer payload, body, attachment, credential or QR is copied.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.api_activity.services import record_activity
from .models import WhatsAppAccount, WhatsAppMessage
from .instagram_models import InstagramMessage, InstagramWebhookDelivery
from .template_models import WhatsAppTemplateOperation


def _message_state(instance, integration, *, created, update_fields):
    if not created and update_fields is not None and not set(update_fields) & {"status", "external_id", "error"}:
        return
    state = instance.status
    status = {"queued": "queued", "sent": "accepted", "delivered": "completed", "read": "completed",
              "received": "completed", "failed": "failed"}.get(state, "unknown")
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    errors = payload.get("errors")
    error = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    code = error.get("code")
    error_code = str(code) if type(code) in (str, int) and str(code).isascii() and str(code).isdigit() and len(str(code)) <= 12 else ""
    record_activity(
        integration=integration, operation="message.created" if created else "message.state_observed",
        kind="message", direction=instance.direction, organization_id=instance.organization_id,
        connection_id=str(instance.account_id), entity_type="message", entity_id=str(instance.pk),
        provider_reference=instance.external_id or "", status=status,
        error_code=error_code if state == "failed" else "",
        error_category="provider" if state == "failed" and error_code else "",
        summary=f"Message {'created' if created else 'saved'} with {state} status.",
        metadata={"message_status": state, "message_id": str(instance.pk),
                  "lead_id": str(getattr(instance, "lead_id", "") or ""),
                  "message_type": instance.message_type},
    )


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="api-activity-whatsapp-state")
def whatsapp_message_state(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw or (not created and update_fields is not None and not set(update_fields) & {"status", "external_id", "error"}):
        return
    if getattr(instance.account, "is_operations_test", False):
        return
    integration = "whatsapp_hosted" if instance.account.connection_type == "hosted" else "whatsapp_api"
    _message_state(instance, integration, created=created, update_fields=update_fields)


@receiver(post_save, sender=InstagramMessage, dispatch_uid="api-activity-instagram-state")
def instagram_message_state(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if not raw:
        _message_state(instance, "instagram", created=created, update_fields=update_fields)


@receiver(post_save, sender=WhatsAppAccount, dispatch_uid="api-activity-hosted-session")
def hosted_session_state(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw or instance.connection_type != "hosted" or instance.is_operations_test:
        return
    if not created and update_fields is not None and "status" not in update_fields:
        return
    record_activity(integration="whatsapp_hosted", operation="session.state_observed", kind="connection",
                    organization_id=instance.organization_id, connection_id=str(instance.pk),
                    entity_type="connection", entity_id=str(instance.pk),
                    status={"connected": "completed", "failed": "failed", "disconnected": "blocked"}.get(instance.status, "processing"),
                    summary=f"Hosted session saved with {instance.status} status.", metadata={"session_status": instance.status})


@receiver(post_save, sender=InstagramWebhookDelivery, dispatch_uid="api-activity-instagram-webhook")
def instagram_webhook_state(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    record_activity(integration="instagram", operation="webhook.state_observed", kind="webhook", direction="inbound",
                    entity_type="webhook", entity_id=str(instance.pk),
                    status={"pending": "queued", "processing": "processing", "processed": "completed",
                            "ignored": "skipped", "failed": "failed"}.get(instance.status, "unknown"),
                    summary=f"Instagram webhook envelope saved with {instance.status} status.",
                    metadata={"webhook_delivery_id": str(instance.pk)})


@receiver(post_save, sender=WhatsAppTemplateOperation, dispatch_uid="api-activity-meta-template")
def template_operation(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    record_activity(integration="meta_templates", operation=f"template.{instance.operation}", kind="template",
                    organization_id=instance.organization_id, connection_id=str(instance.account_id or ""),
                    entity_type="template", entity_id=str(instance.template_id or ""),
                    status="completed" if instance.success else "failed", http_status=instance.http_status,
                    error_code=instance.meta_error_code, error_category="provider" if not instance.success else "",
                    summary=f"Template {instance.operation} {'completed' if instance.success else 'failed'}.",
                    metadata={"provider_error_subcode": instance.meta_error_subcode,
                              "operation_id": str(instance.pk)})
