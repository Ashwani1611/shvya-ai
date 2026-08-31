import pgvector.django.vector
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        (
            "ai_engagement",
            "0005_vector_extension",
        ),
    ]

    operations = [
        migrations.RemoveField(
            model_name="chunk",
            name="embedding_id",
        ),
        migrations.AddField(
            model_name="chunk",
            name="embedding",
            field=pgvector.django.vector.VectorField(
                blank=True,
                dimensions=1536,
                null=True,
            ),
        ),
    ]