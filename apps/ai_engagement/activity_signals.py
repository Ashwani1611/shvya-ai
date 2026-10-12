"""Observe settled AI usage without duplicating the accounting ledger."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.api_activity.services import record_activity

from .models import AICreditTransaction


@receiver(post_save, sender=AICreditTransaction, dispatch_uid="api-activity-ai-usage")
def ai_usage_settled(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.transaction_type != AICreditTransaction.TransactionType.AI_USAGE:
        return
    record_activity(
        integration="ai", operation="ai.usage.settled", kind="usage", direction="internal",
        organization_id=instance.organization_id,
        entity_type="ai_credit_transaction", entity_id=str(instance.pk),
        status="completed", summary="AI usage settled in the existing credit ledger.",
        metadata={
            "model": instance.model, "feature": instance.feature,
            "input_tokens": instance.input_tokens, "output_tokens": instance.output_tokens,
            "credits_consumed": max(0, -instance.amount), "cost_unit": "ai_credits",
            "reservation_id": str(instance.reservation_id or ""),
        },
    )
