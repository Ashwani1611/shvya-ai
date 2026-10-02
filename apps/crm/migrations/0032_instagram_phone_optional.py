from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0031_attributedefinition_is_active"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="phone",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name="lead",
            name="uniq_org_phone",
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=~models.Q(phone=""),
                fields=("organization", "phone"),
                name="uniq_org_phone",
            ),
        ),
    ]
