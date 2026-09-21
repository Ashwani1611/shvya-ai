from django.db import migrations


def backfill_instagram_webhook_routing(apps, schema_editor):
    InstagramAccount = apps.get_model("channels", "InstagramAccount")
    InstagramWebhookDelivery = apps.get_model(
        "channels",
        "InstagramWebhookDelivery",
    )

    for delivery in InstagramWebhookDelivery.objects.all().iterator(chunk_size=200):
        payload = delivery.raw_payload if isinstance(delivery.raw_payload, dict) else {}
        own_ids = sorted({
            str(entry.get("id") or "").strip()
            for entry in (payload.get("entry", []) or [])
            if isinstance(entry, dict) and str(entry.get("id") or "").strip()
        })
        if own_ids:
            rows = InstagramAccount.objects.filter(
                ig_user_id__in=own_ids,
            ).values_list("id", "organization_id")
            account_ids = sorted({str(account_id) for account_id, _ in rows})
            organization_ids = sorted({str(org_id) for _, org_id in rows})
        else:
            account_ids = []
            organization_ids = []

        if (
            list(delivery.account_ids or []) != account_ids
            or list(delivery.organization_ids or []) != organization_ids
        ):
            InstagramWebhookDelivery.objects.filter(pk=delivery.pk).update(
                account_ids=account_ids,
                organization_ids=organization_ids,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0016_instagramwebhookdelivery_routing_metadata"),
    ]

    operations = [
        migrations.RunPython(
            backfill_instagram_webhook_routing,
            migrations.RunPython.noop,
        ),
    ]
