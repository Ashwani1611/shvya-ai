from django.contrib.postgres.indexes import GinIndex, OpClass
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models
from django.db.models.functions import Cast, Upper


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0017_lead_ai_enabled"),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name="lead",
            index=models.Index(
                fields=[
                    "organization",
                    "pipeline",
                    "stage",
                ],
                name="crm_lead_org_pipe_stage",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                OpClass(
                    Upper("name"),
                    name="gin_trgm_ops",
                ),
                name="crm_lead_name_trgm",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                OpClass(
                    Upper("phone"),
                    name="gin_trgm_ops",
                ),
                name="crm_lead_phone_trgm",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                OpClass(
                    Upper("email"),
                    name="gin_trgm_ops",
                ),
                name="crm_lead_email_trgm",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                OpClass(
                    Upper("notes"),
                    name="gin_trgm_ops",
                ),
                name="crm_lead_notes_trgm",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                fields=["attributes"],
                name="crm_lead_attr_gin",
            ),
        ),
        migrations.AddIndex(
            model_name="lead",
            index=GinIndex(
                OpClass(
                    Upper(
                        Cast(
                            "attributes",
                            output_field=models.TextField(),
                        )
                    ),
                    name="gin_trgm_ops",
                ),
                name="crm_lead_attr_trgm",
            ),
        ),
    ]
