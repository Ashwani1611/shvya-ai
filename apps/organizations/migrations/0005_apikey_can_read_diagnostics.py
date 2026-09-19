from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.AddField(
            model_name="apikey",
            name="can_read_diagnostics",
            field=models.BooleanField(\n                default=False,\n                help_text="Allow this key to read organization-scoped diagnostic data.",\n            ),
        ),
    ]
