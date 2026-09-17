from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("channels", "0012_instagram_backend")]

    operations = [
        migrations.CreateModel(
            name="HostedChatReadState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_key", models.CharField(max_length=128)),
                ("read_through_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="channels.whatsappaccount")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("account", "chat_key"), name="hosted_read_account_key_uniq")]},
        ),
        migrations.AddIndex(
            model_name="whatsappmessage",
            index=models.Index(fields=["account", "created_at", "id"], name="wa_msg_account_created_idx"),
        ),
    ]
