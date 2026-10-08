from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("followups", "0009_followupsequence_instagram_account_and_more")]

    operations = [
        migrations.RemoveConstraint(model_name="followupsequence", name="fu_seq_exactly_one_channel"),
        migrations.AddConstraint(
            model_name="followupsequence",
            constraint=models.CheckConstraint(
                condition=(
                    Q(whatsapp_account__isnull=False, instagram_account__isnull=True)
                    | Q(whatsapp_account__isnull=True, instagram_account__isnull=False)
                    | Q(whatsapp_account__isnull=True, instagram_account__isnull=True, is_active=False)
                ),
                name="fu_seq_exactly_one_channel",
            ),
        ),
    ]
