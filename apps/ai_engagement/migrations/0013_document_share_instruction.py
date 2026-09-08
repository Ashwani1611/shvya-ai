from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0012_orginfo_engagement_instructions")]

    operations = [
        migrations.AddField(
            model_name="document",
            name="share_instruction",
            field=models.TextField(
                blank=True,
                help_text="Tell the AI when and why this file should be sent to a lead.",
            ),
        ),
    ]
