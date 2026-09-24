from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_signupverificationdelivery"),
    ]

    operations = [
        migrations.CreateModel(
            name="SuperadminAccount",
            fields=[],
            options={
                "verbose_name": "Account Information",
                "verbose_name_plural": "Account Information",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("accounts.user",),
        ),
    ]
