from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('channels', '0002_whatsapptemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='whatsappmessage',
            name='is_read',
            field=models.BooleanField(default=True),
        ),
    ]