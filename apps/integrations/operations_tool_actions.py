# ruff: noqa: F401
"""Compatibility facade for Operations MCP action tools.

Implementation is split by responsibility under apps.integrations.operations.tools.
Historical imports and MCP dispatch contracts remain unchanged.
"""

from apps.integrations.operations.tools.ai_actions import (
    update_ai_configuration as update_ai_configuration,
)
from apps.integrations.operations.tools.audit_actions import (
    get_operations_audit as get_operations_audit,
)
from apps.integrations.operations.tools.conversion_actions import (
    get_conversion_analysis as get_conversion_analysis,
)
from apps.integrations.operations.tools.lead_actions import (
    _validate_operations_stage_move as _validate_operations_stage_move,
    diagnose_lead_qualification as diagnose_lead_qualification,
    move_lead_stage as move_lead_stage,
    repair_qualification_stage as repair_qualification_stage,
    update_lead_attributes as update_lead_attributes,
)
from apps.integrations.operations.tools.messaging_actions import (
    update_messaging_automation_settings as update_messaging_automation_settings,
)
