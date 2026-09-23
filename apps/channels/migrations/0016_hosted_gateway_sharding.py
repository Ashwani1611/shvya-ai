from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0015_instagramconversation_lead"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappaccount",
            name="hosted_gateway_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="whatsappaccount",
            name="hosted_gateway_shard",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="whatsappaccount",
            name="hosted_lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="whatsappaccount",
            name="hosted_lease_owner",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="whatsappaccount",
            name="hosted_session_state",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddIndex(
            model_name="whatsappaccount",
            index=models.Index(
                fields=["connection_type", "hosted_gateway_shard", "status"],
                name="wa_acct_hosted_shard_idx",
            ),
        ),
    ]
