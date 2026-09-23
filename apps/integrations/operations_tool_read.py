# ruff: noqa: F401
"""Compatibility facade for Operations MCP read tools.

Implementation is split by read responsibility under
apps.integrations.operations.tools. Historical imports, helper patch points,
and MCP dispatch contracts remain compatible.
"""

from apps.integrations.operations.tools import read_validation as _validation
from apps.integrations.operations.tools import read_configuration as _configuration
from apps.integrations.operations.tools import read_messaging as _messaging

_reject_secret_like_content = _validation._reject_secret_like_content
_attribute_value_compatible = _validation._attribute_value_compatible
_normalized_lead_attribute_values = _validation._normalized_lead_attribute_values
_validated_lead_attribute_values = _validation._validated_lead_attribute_values
_attribute_schema_snapshot = _validation._attribute_schema_snapshot
_incompatible_existing_attribute_value_count = _validation._incompatible_existing_attribute_value_count
_sensitive_attribute_keys = _validation._sensitive_attribute_keys
_safe_workflow_config = _validation._safe_workflow_config
_assert_workflow_safe_attribute_references = _validation._assert_workflow_safe_attribute_references
_workflow_reference_index = _validation._workflow_reference_index
_assert_workflow_tenant_references = _validation._assert_workflow_tenant_references
_safe_url_host = _configuration._safe_url_host
_safe_knowledge_name = _configuration._safe_knowledge_name
_knowledge_health = _configuration._knowledge_health
_safe_full_config_text = _configuration._safe_full_config_text
_qualification_snapshot = _messaging._qualification_snapshot
_qualification_contract_snapshot = _messaging._qualification_contract_snapshot
_messaging_account = _messaging._messaging_account
_public_messaging_settings = _messaging._public_messaging_settings
_safe_messaging_settings_row = _messaging._safe_messaging_settings_row

_IMPLEMENTATIONS = (_validation, _configuration, _messaging)
_ENTRYPOINTS = frozenset({
    "get_ai_configuration",
    "get_knowledge_health",
    "get_organization_configuration",
    "get_automation_configuration",
    "get_messaging_automation_settings",
})


def _sync_facade_overrides():
    """Propagate historical facade patch points into focused implementations."""
    facade = globals()
    for module in _IMPLEMENTATIONS:
        for name in tuple(module.__dict__):
            if name in _ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def get_ai_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _configuration.get_ai_configuration(identity=identity, arguments=arguments)


def get_knowledge_health(*, identity, arguments):
    _sync_facade_overrides()
    return _configuration.get_knowledge_health(identity=identity, arguments=arguments)


def get_organization_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _configuration.get_organization_configuration(
        identity=identity,
        arguments=arguments,
    )


def get_automation_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _configuration.get_automation_configuration(
        identity=identity,
        arguments=arguments,
    )


def get_messaging_automation_settings(*, identity, arguments):
    _sync_facade_overrides()
    return _messaging.get_messaging_automation_settings(
        identity=identity,
        arguments=arguments,
    )
