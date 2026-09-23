# ruff: noqa: F401
"""Compatibility facade for Operations MCP configuration management.

Implementation is split under apps.integrations.operations.configuration while
this historical module preserves imports, helper patch points, handler names,
and MCP contracts.
"""

from apps.integrations.operations.configuration import export as _export
from apps.integrations.operations.configuration import validation as _validation
from apps.integrations.operations.configuration import plans as _plans
from apps.integrations.operations.configuration import importing as _importing
from apps.integrations.operations.configuration.common import (
    ALLOWED_PLAN_TOOLS,
    EXPORT_SCHEMA_VERSION,
    INTERNAL_PLAN_TOOLS,
    PLAN_MAX_OPERATIONS,
    PLAN_MAX_TTL_MINUTES,
    PLAN_TOOL_CAPABILITIES,
    PLAN_TTL_MINUTES,
    PUBLIC_PLAN_TOOLS,
)

_json_hash = _export._json_hash
_account_ref = _export._account_ref
_step_export = _export._step_export
_portable_workflow = _export._portable_workflow
_portable_configuration = _export._portable_configuration
configuration_etag = _export.configuration_etag
_object_etag = _export._object_etag
_dependency_graph = _validation._dependency_graph
_workflow_cycles = _validation._workflow_cycles
_organization_validation = _validation._organization_validation
_contains_ref = _plans._contains_ref
_lookup_path = _plans._lookup_path
_resolve_refs = _plans._resolve_refs
_normalize_plan_operations = _plans._normalize_plan_operations
_operation_is_reversible = _plans._operation_is_reversible
_execute_plan_member = _plans._execute_plan_member
_dry_run_plan_member = _plans._dry_run_plan_member
_consume_plan_approval = _plans._consume_plan_approval
_snapshot_step_for_inverse = _plans._snapshot_step_for_inverse
_capture_inverse = _plans._capture_inverse
_find_account_by_ref = _importing._find_account_by_ref
_import_operations = _importing._import_operations

_IMPLEMENTATIONS = (_export, _validation, _plans, _importing)
_ENTRYPOINTS = frozenset([
    "get_configuration_dependency_graph",
    "validate_organization_configuration",
    "reorder_stages",
    "create_configuration_plan",
    "apply_configuration_plan",
    "rollback_configuration_plan",
    "export_organization_configuration",
    "import_organization_configuration"
])


def _sync_facade_overrides():
    """Propagate historical facade patch points into focused implementations."""
    facade = globals()
    for module in _IMPLEMENTATIONS:
        for name in tuple(module.__dict__):
            if name in _ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def export_organization_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _export.export_organization_configuration(identity=identity, arguments=arguments)


def get_configuration_dependency_graph(*, identity, arguments):
    _sync_facade_overrides()
    return _validation.get_configuration_dependency_graph(identity=identity, arguments=arguments)


def validate_organization_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _validation.validate_organization_configuration(identity=identity, arguments=arguments)


def reorder_stages(*, identity, arguments):
    _sync_facade_overrides()
    return _validation.reorder_stages(identity=identity, arguments=arguments)


def create_configuration_plan(*, identity, arguments):
    _sync_facade_overrides()
    return _plans.create_configuration_plan(identity=identity, arguments=arguments)


def apply_configuration_plan(*, identity, arguments):
    _sync_facade_overrides()
    return _plans.apply_configuration_plan(identity=identity, arguments=arguments)


def rollback_configuration_plan(*, identity, arguments):
    _sync_facade_overrides()
    return _plans.rollback_configuration_plan(identity=identity, arguments=arguments)


def import_organization_configuration(*, identity, arguments):
    _sync_facade_overrides()
    return _importing.import_organization_configuration(identity=identity, arguments=arguments)


CONFIGURATION_MANAGEMENT_HANDLERS = {
    "get_configuration_dependency_graph": get_configuration_dependency_graph,
    "validate_organization_configuration": validate_organization_configuration,
    "reorder_stages": reorder_stages,
    "create_configuration_plan": create_configuration_plan,
    "apply_configuration_plan": apply_configuration_plan,
    "rollback_configuration_plan": rollback_configuration_plan,
    "export_organization_configuration": export_organization_configuration,
    "import_organization_configuration": import_organization_configuration,
}
