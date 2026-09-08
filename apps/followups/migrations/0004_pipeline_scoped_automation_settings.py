from copy import deepcopy
from datetime import time

from django.db import migrations


def migrate_global_settings_to_linked_accounts(apps, schema_editor):
    AutoFollowupSettings = apps.get_model("followups", "AutoFollowupSettings")
    WhatsAppAccount = apps.get_model("channels", "WhatsAppAccount")

    for config in AutoFollowupSettings.objects.select_related("organization").iterator():
        organization = config.organization
        org_settings = deepcopy(organization.settings or {})
        hosted_root = org_settings.setdefault("hosted_whatsapp", {})
        sessions = hosted_root.setdefault("sessions", {})

        accounts = WhatsAppAccount.objects.filter(
            organization_id=organization.id,
            is_active=True,
        )
        for account in accounts.iterator():
            key = str(account.id)
            current = deepcopy(sessions.get(key, {}))

            # Preserve the actual pre-migration follow-up state. Previously an
            # organization-level OFF switch blocked every account even when its
            # account switch was ON. After this migration, that effective state
            # belongs to each linked number independently.
            current["auto_follow_up"] = bool(
                current.get("auto_follow_up", True) and config.enabled
            )

            # Meta API sequences historically got their scheduling window and
            # conversation delay from the organization-level row. Copy those
            # values into each API number so removing Global Settings does not
            # change existing live schedules.
            if account.connection_type == "api":
                current.setdefault(
                    "business_hours_start",
                    config.business_hours_start.strftime("%H:%M"),
                )
                current.setdefault(
                    "business_hours_end",
                    config.business_hours_end.strftime("%H:%M"),
                )
                current.setdefault(
                    "active_conversation_delay_value",
                    max(1, config.conversation_delay_value),
                )
                current.setdefault(
                    "active_conversation_delay_unit",
                    config.conversation_delay_unit,
                )

            sessions[key] = current

        organization.settings = org_settings
        organization.save(update_fields=["settings"])

        # Legacy dispatcher paths still join this row. Make it permanently
        # neutral so only the linked account/pipeline controls execution.
        AutoFollowupSettings.objects.filter(pk=config.pk).update(
            enabled=True,
            business_hours_start=time(0, 0),
            business_hours_end=time(23, 59, 59),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("followups", "0003_leadsequencestate_assigned_by"),
    ]

    operations = [
        migrations.RunPython(
            migrate_global_settings_to_linked_accounts,
            migrations.RunPython.noop,
        ),
    ]
