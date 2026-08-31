from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    """
    Enable PostgreSQL pgvector before any ai_engagement migration
    creates a VectorField.
    """

    dependencies = [
        (
            "ai_engagement",
            "0005_document_source_url_alter_document_file",
        ),
    ]

    operations = [
        VectorExtension(),
    ]