from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_engagement", "0020_knowledgerepairrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="orginfo",
            name="qualification_model",
            field=models.CharField(
                blank=True,
                help_text="Optional OpenAI model override for New Lead qualification replies.",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="orginfo",
            name="sales_support_model",
            field=models.CharField(
                blank=True,
                help_text="Optional OpenAI model override for non-qualification sales/support replies.",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="orginfo",
            name="summary_model",
            field=models.CharField(
                blank=True,
                help_text="Optional OpenAI model override for post-turn conversation summaries.",
                max_length=100,
            ),
        ),
    ]
