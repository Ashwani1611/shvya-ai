from zoneinfo import ZoneInfo

from django.db import migrations


def seed_booked_at(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Attribute = apps.get_model("crm", "AttributeDefinition")
    for org_id in Organization.objects.values_list("pk", flat=True).iterator():
        attribute = Attribute.objects.filter(organization_id=org_id, key="booked_at").first()
        if attribute:
            Attribute.objects.filter(pk=attribute.pk).update(field_type="datetime", is_active=True)
        else:
            # Reuse an existing label without violating the organization/name constraint.
            attribute = Attribute.objects.filter(organization_id=org_id, name="Booked at").first()
            label = "Booked at" if attribute is None else "Booked at (Calendar)"
            Attribute.objects.create(organization_id=org_id, key="booked_at", name=label, field_type="datetime")

    Booking = apps.get_model("shvya_calendar", "CalendarBooking")
    Lead = apps.get_model("crm", "Lead")
    for booking in Booking.objects.filter(status__in=["scheduled", "rescheduled"]).order_by("updated_at").iterator():
        lead = Lead.objects.filter(pk=booking.lead_id, organization_id=booking.organization_id).first()
        if lead is not None:
            values = dict(lead.attributes or {})
            values["booked_at"] = booking.start_at.astimezone(ZoneInfo(booking.timezone)).strftime("%Y-%m-%dT%H:%M")
            Lead.objects.filter(pk=lead.pk).update(attributes=values)


class Migration(migrations.Migration):
    dependencies = [
        ("shvya_calendar", "0003_alter_calendarpage_pipeline_alter_calendarpage_stage_and_more"),
        ("crm", "0031_attributedefinition_is_active"),
    ]
    operations = [migrations.RunPython(seed_booked_at, migrations.RunPython.noop)]
