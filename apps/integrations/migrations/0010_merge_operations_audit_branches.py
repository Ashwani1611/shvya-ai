from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        (
            "integrations",
            "0008_operations_audit_protect",
        ),
        (
            "integrations",
            "0009_operations_oauth_capability_grants",
        ),
    ]

    operations = []
