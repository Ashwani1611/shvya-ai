from django.db import migrations, models
import django.db.models.deletion


def populate_sequence_provider(apps, schema_editor):
    FollowupSequence = apps.get_model("followups", "FollowupSequence")
    WhatsAppAccount = apps.get_model("channels", "WhatsAppAccount")

    hosted_ids = WhatsAppAccount.objects.filter(
        connection_type="hosted",
    ).values_list("id", flat=True)
    FollowupSequence.objects.filter(
        whatsapp_account_id__in=hosted_ids,
    ).update(provider="hosted")


class Migration(migrations.Migration):

    dependencies = [
        ("followups", "0008_followupstepattachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="followupsequence",
            name="provider",
            field=models.CharField(
                choices=[("api", "WhatsApp API"), ("hosted", "Hosted/Coexistence")],
                default="api",
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="followupsequence",
            name="whatsapp_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="followup_sequences",
                to="channels.whatsappaccount",
            ),
        ),
        migrations.RunPython(
            populate_sequence_provider,
            migrations.RunPython.noop,
        ),
    ]
