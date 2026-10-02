from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0018_orginfo_ai_playbook")]
    operations = [
        migrations.AddField(
            model_name="document",
            name="file_sharing_ready",
            field=models.BooleanField(
                default=False,
                help_text="Uploaded bytes passed file validation; independent of knowledge indexing.",
            ),
        ),
    ]
