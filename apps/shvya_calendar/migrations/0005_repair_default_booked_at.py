from django.db import migrations


def repair_booked_at(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Attribute = apps.get_model("crm", "AttributeDefinition")
    alias = schema_editor.connection.alias
    for org_id in Organization.objects.using(alias).values_list("pk", flat=True).iterator():
        attributes = Attribute.objects.using(alias).filter(organization_id=org_id)
        existing = attributes.filter(key="booked_at")
        if existing.exists():
            existing.update(field_type="datetime", is_active=True)
            continue
        labels = set(attributes.values_list("name", flat=True))
        label, suffix = "Booked at", 1
        while label in labels:
            label = "Booked at (Calendar)" if suffix == 1 else f"Booked at (Calendar {suffix})"
            suffix += 1
        attributes.create(
            organization_id=org_id, key="booked_at", name=label,
            field_type="datetime", is_active=True,
            description="Appointment time in the booking calendar timezone.",
        )


class Migration(migrations.Migration):
    dependencies = [("shvya_calendar", "0004_booked_at_attribute")]
    operations = [migrations.RunPython(repair_booked_at, migrations.RunPython.noop)]
