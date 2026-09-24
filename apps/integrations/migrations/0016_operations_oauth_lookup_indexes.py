from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("integrations", "0015_operationsintakeentry"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="operationsoauthauthorizationcode",
            index=models.Index(
                fields=["client", "used_at", "expires_at"],
                name="ops_code_client_exp_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="operationsoauthauthorizationcode",
            index=models.Index(
                fields=["actor", "used_at", "expires_at"],
                name="ops_code_actor_exp_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="operationsoauthtoken",
            index=models.Index(
                fields=["role", "revoked_at", "refresh_expires_at"],
                name="ops_token_role_grant_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="operationsoauthtoken",
            index=models.Index(
                fields=["organization", "role", "revoked_at"],
                name="ops_token_org_role_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="operationsoauthtoken",
            index=models.Index(
                fields=["client", "revoked_at", "refresh_expires_at"],
                name="ops_token_client_exp_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="operationssupportsession",
            index=models.Index(
                fields=["token", "ended_at", "-last_seen_at"],
                name="ops_support_token_idx",
            ),
        ),
    ]
