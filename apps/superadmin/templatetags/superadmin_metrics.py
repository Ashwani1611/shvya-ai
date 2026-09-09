from __future__ import annotations

from django import template
from django.db.models import Count, Q

from apps.ai_engagement.coins import credits_to_coins
from apps.ai_engagement.models import (
    AICreditTransaction,
    AICreditWallet,
    KnowledgeSource,
)
from apps.followups.models import FollowupExecution, FollowupSequence


register = template.Library()


DEFAULT_METRICS = {
    "ai_messages": 0,
    "ai_qualifications": 0,
    "ai_bumpups": 0,
    "afs_sent": 0,
    # Raw credit aliases are kept for backwards compatibility. Superadmin UI
    # uses the coin fields below (30 credits = 1 coin).
    "credits_used": 0,
    "credits_remaining": 0,
    "credits_total": 0,
    "coins_used": credits_to_coins(0),
    "coins_remaining": credits_to_coins(0),
    "coins_total": credits_to_coins(0),
    "kb_setup": False,
    "sequences": 0,
}


def build_organization_metrics(organizations):
    """Return real Superadmin usage metrics keyed by organization id.

    Provider accounting stays in exact AI credits. Superadmin summary metrics are
    also exposed as AI coins so operators see the billing-facing 30:1 unit.
    """

    organizations = list(organizations)
    organization_ids = [organization.pk for organization in organizations]
    metrics = {
        str(organization_id): DEFAULT_METRICS.copy()
        for organization_id in organization_ids
    }

    if not organization_ids:
        return metrics

    for row in AICreditWallet.objects.filter(
        organization_id__in=organization_ids,
    ).values(
        "organization_id",
        "balance",
        "reserved_credits",
        "lifetime_credits_added",
        "lifetime_credits_used",
    ):
        item = metrics[str(row["organization_id"])]
        credits_used = int(row["lifetime_credits_used"] or 0)
        credits_remaining = max(
            int(row["balance"] or 0) - int(row["reserved_credits"] or 0),
            0,
        )
        credits_total = int(row["lifetime_credits_added"] or 0)

        item["credits_used"] = credits_used
        item["credits_remaining"] = credits_remaining
        item["credits_total"] = credits_total
        item["coins_used"] = credits_to_coins(credits_used)
        item["coins_remaining"] = credits_to_coins(credits_remaining)
        item["coins_total"] = credits_to_coins(credits_total)

    usage_rows = (
        AICreditTransaction.objects.filter(
            organization_id__in=organization_ids,
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
        )
        .values("organization_id")
        .annotate(
            engagement_calls=Count(
                "id",
                filter=Q(feature="engagement"),
            ),
            qualification_calls=Count(
                "id",
                filter=Q(feature="qualification"),
            ),
            bumpup_calls=Count(
                "id",
                filter=Q(metadata__task="bump_up"),
            ),
        )
    )
    for row in usage_rows:
        item = metrics[str(row["organization_id"])]
        bumpups = int(row["bumpup_calls"] or 0)
        item["ai_bumpups"] = bumpups
        item["ai_messages"] = max(
            int(row["engagement_calls"] or 0) - bumpups,
            0,
        )
        item["ai_qualifications"] = int(row["qualification_calls"] or 0)

    afs_rows = (
        FollowupExecution.objects.filter(
            organization_id__in=organization_ids,
            status=FollowupExecution.Status.SENT,
        )
        .values("organization_id")
        .annotate(total=Count("id"))
    )
    for row in afs_rows:
        metrics[str(row["organization_id"])]["afs_sent"] = int(
            row["total"] or 0
        )

    sequence_rows = (
        FollowupSequence.objects.filter(
            organization_id__in=organization_ids,
        )
        .values("organization_id")
        .annotate(total=Count("id"))
    )
    for row in sequence_rows:
        metrics[str(row["organization_id"])]["sequences"] = int(
            row["total"] or 0
        )

    kb_organization_ids = set(
        KnowledgeSource.objects.filter(
            organization_id__in=organization_ids,
            is_active=True,
        ).values_list("organization_id", flat=True)
    )
    for organization_id in kb_organization_ids:
        metrics[str(organization_id)]["kb_setup"] = True

    return metrics


@register.simple_tag
def organization_metrics(organizations):
    return build_organization_metrics(organizations)


@register.simple_tag
def organization_metric(organization):
    return build_organization_metrics([organization]).get(
        str(organization.pk),
        DEFAULT_METRICS.copy(),
    )


@register.filter
def dict_get(mapping, key):
    if not mapping:
        return DEFAULT_METRICS.copy()
    return mapping.get(str(key), DEFAULT_METRICS.copy())
