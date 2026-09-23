"""Collect tenant rows and record external cleanup in the same transaction."""

from django.db import models
from django.db.models.deletion import Collector
from .models import OrganizationDeletionCleanup


def delete_organization(organization):
    collector = Collector(using=organization._state.db)
    collector.collect([organization])
    files = []
    # Fast-delete querysets can contain FileFields too.
    collections = list(collector.data.items()) + [
        (query.model, query) for query in collector.fast_deletes
    ]
    for model, rows in collections:
        fields = [
            field for field in model._meta.fields if isinstance(field, models.FileField)
        ]
        if not fields:
            continue
        for row in rows:
            for field in fields:
                value = getattr(row, field.name)
                if value and value.name:
                    files.append(
                        {
                            "model": model._meta.label,
                            "field": field.name,
                            "name": value.name,
                        }
                    )
    sessions = []
    for account in organization.whatsapp_accounts.filter(connection_type="hosted"):
        from apps.channels.hosted_gateway_routing import gateway_shard_for_account

        sessions.append(
            {
                "id": str(account.pk),
                "shard": gateway_shard_for_account(account),
            }
        )
    if files or sessions:
        OrganizationDeletionCleanup.objects.create(
            organization_id=organization.pk,
            files=files,
            hosted_session_ids=sessions,
        )
    return collector.delete()
