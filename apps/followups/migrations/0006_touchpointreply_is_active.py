from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("followups", "0005_touchpointcategory_touchpointreply_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="touchpointreply",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
