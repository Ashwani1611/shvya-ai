from django.db import migrations


def standard_callback(apps, schema_editor):
    Disposition = apps.get_model("telephony", "CallDisposition")
    Disposition.objects.filter(
        code="call_back_later", name="Call Back Later", category="not_connected"
    ).update(name="Callback Requested", category="connected")


class Migration(migrations.Migration):
    dependencies = [("telephony", "0001_initial")]
    operations = [migrations.RunPython(standard_callback, migrations.RunPython.noop)]
