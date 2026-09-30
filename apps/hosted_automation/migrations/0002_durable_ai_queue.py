from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("hosted_automation", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="hostedautomationjob", name="source_message",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="hosted_automation_job", to="channels.whatsappmessage"),
        ),
        migrations.AlterField(
            model_name="hostedautomationjob", name="kind",
            field=models.CharField(choices=[("welcome", "Welcome"), ("ai_engagement", "AI Engagement")], default="ai_engagement", max_length=24),
        ),
        migrations.AddField(model_name="hostedautomationjob", name="lease_expires_at", field=models.DateTimeField(blank=True, null=True, db_index=True)),
        migrations.AddField(model_name="hostedautomationjob", name="claim_token", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="hostedautomationjob", name="attempts", field=models.PositiveIntegerField(default=0)),
        migrations.AddConstraint(model_name="hostedautomationjob", constraint=models.UniqueConstraint(fields=("account", "lead"), condition=models.Q(kind="welcome"), name="hosted_one_welcome_per_lead")),
    ]
