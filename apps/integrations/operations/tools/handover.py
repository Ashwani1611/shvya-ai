"""Tenant-authorized preparation of account handover artifacts."""
from apps.integrations.operations.handover_rendering import (
    HandoverValidationError,
    render_handover,
)
from apps.integrations.operations.tools.setup_authoring import _execution
from apps.integrations.operations_policy import CAP_SETUP_ARTIFACTS_PREPARE
from apps.integrations.operations_tools import (
    OperationsToolError,
    _organization_for,
    _require_operations_capability,
)


def render_account_handover(identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_SETUP_ARTIFACTS_PREPARE,
    )
    try:
        data = render_handover(
            arguments["document"],
            organization_id=str(organization.id),
            organization_name=organization.name,
            format=arguments.get("format", "html"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 20000),
        )
    except HandoverValidationError as exc:
        raise OperationsToolError(str(exc)) from exc
    return _execution(data, organization, CAP_SETUP_ARTIFACTS_PREPARE, {
        "format": data["format"],
        "offset": data["offset"],
        "returned_chars": data["returned_chars"],
        "total_chars": data["total_chars"],
        "script_only": True,
        "persisted": False,
    })
