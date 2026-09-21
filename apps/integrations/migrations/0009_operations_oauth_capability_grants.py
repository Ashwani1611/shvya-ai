from django.db import migrations, models


READ_CAPABILITIES = {
    "organization.read",
    "diagnostics.read",
    "audit.read",
}

WRITE_CAPABILITIES = {
    "lead.stage.write",
    "lead.attributes.write",
    "ai.config.write",
    "crm.pipeline.config.write",
    "crm.stage.config.write",
    "crm.attribute.config.write",
    "automation.workflow.config.write",
    "automation.cadence.config.write",
    "automation.messaging.config.write",
}

LEGACY_EXPANSIONS = {
    "crm.config.write": {
        "crm.pipeline.config.write",
        "crm.stage.config.write",
        "crm.attribute.config.write",
    },
    "automation.config.write": {
        "automation.workflow.config.write",
        "automation.cadence.config.write",
        "automation.messaging.config.write",
    },
}


def _expanded(values):
    raw = {str(item) for item in (values or [])}
    expanded = {
        item
        for item in raw
        if item in READ_CAPABILITIES or item in WRITE_CAPABILITIES
    }
    for legacy, replacements in LEGACY_EXPANSIONS.items():
        if legacy in raw:
            expanded.update(replacements)
    return expanded


def _grant_for(*, role, organization_id, scope, policies):
    scopes = set(str(scope or "").split())
    allow_writes = "operations.write" in scopes

    if role == "SHVYA_SUPERADMIN":
        capabilities = set(READ_CAPABILITIES)
        if allow_writes:
            capabilities.update(WRITE_CAPABILITIES)
        return sorted(capabilities)

    if role != "ORGANIZATION_ADMIN" or not organization_id:
        return []

    policy = policies.get(organization_id)
    if policy is None or not policy.organization_admin_enabled:
        return []

    capabilities = _expanded(policy.allowed_capabilities)
    if not allow_writes:
        capabilities.difference_update(WRITE_CAPABILITIES)
    return sorted(capabilities)


def backfill_granted_capabilities(apps, schema_editor):
    Policy = apps.get_model("integrations", "OperationsPolicy")
    AuthorizationCode = apps.get_model(
        "integrations",
        "OperationsOAuthAuthorizationCode",
    )
    Token = apps.get_model(
        "integrations",
        "OperationsOAuthToken",
    )

    policies = {
        policy.organization_id: policy
        for policy in Policy.objects.all().iterator(chunk_size=200)
    }

    for model in (AuthorizationCode, Token):
        for row in model.objects.all().iterator(chunk_size=200):
            grant = _grant_for(
                role=row.role,
                organization_id=row.organization_id,
                scope=row.scope,
                policies=policies,
            )
            model.objects.filter(pk=row.pk).update(
                granted_capabilities=grant,
            )


class Migration(migrations.Migration):
    dependencies = [
        (
            "integrations",
            "0008_operationsaudit_protected_references",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="operationsoauthauthorizationcode",
            name="granted_capabilities",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Consent-time Operations capability snapshot.",
            ),
        ),
        migrations.AddField(
            model_name="operationsoauthtoken",
            name="granted_capabilities",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Consent-time Operations capability snapshot.",
            ),
        ),
        migrations.RunPython(
            backfill_granted_capabilities,
            migrations.RunPython.noop,
        ),
    ]
