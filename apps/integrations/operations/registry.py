"""Stable registry for domain-oriented Operations MCP tools."""

from apps.integrations.operations.tools.cadence import (
    add_hosted_whatsapp_step,
    delete_cadence_step,
    reorder_cadence_steps,
    update_cadence_step,
)
from apps.integrations.operations.tools.faqs import archive_faq, list_faqs, upsert_faq
from apps.integrations.operations.tools.knowledge import (
    archive_knowledge_document,
    create_knowledge_source,
    publish_knowledge_document,
    upload_knowledge_document,
)
from apps.integrations.operations.tools.qualification import (
    get_qualification_configuration,
    upsert_qualification_configuration,
    validate_qualification_configuration,
)
from apps.integrations.operations.tools.setup_authoring import (
    analyze_setup_group_export,
    get_setup_library_resource,
    get_setup_variable_schema,
    list_setup_library,
    render_setup_template,
)
from apps.integrations.operations.tools.setup_intake import (
    archive_setup_intake_entry,
    get_setup_intake,
    upsert_setup_intake_entry,
)
from apps.integrations.operations.tools.simulations import (
    simulate_ai_conversation,
    simulate_cadence,
    simulate_workflow,
)
from apps.integrations.operations.tools.touchpoints import (
    archive_touchpoint,
    list_touchpoints,
    upsert_touchpoint,
)
from apps.integrations.operations.tools.whatsapp import (
    begin_whatsapp_connection,
    bind_whatsapp_account_to_pipeline,
    create_whatsapp_template,
    get_whatsapp_template_status,
    list_whatsapp_accounts,
    list_whatsapp_templates,
    submit_whatsapp_template,
    validate_whatsapp_routing,
)
from apps.integrations.operations.tools.workflows import (
    get_workflow_schema,
    list_workflow_actions,
    list_workflow_triggers,
    validate_workflow_configuration,
)


EXTENDED_HANDLERS = {
    "list_setup_library": list_setup_library,
    "get_setup_library_resource": get_setup_library_resource,
    "get_setup_variable_schema": get_setup_variable_schema,
    "render_setup_template": render_setup_template,
    "analyze_setup_group_export": analyze_setup_group_export,
    "get_setup_intake": get_setup_intake,
    "upsert_setup_intake_entry": upsert_setup_intake_entry,
    "archive_setup_intake_entry": archive_setup_intake_entry,
    "get_qualification_configuration": get_qualification_configuration,
    "validate_qualification_configuration": validate_qualification_configuration,
    "upsert_qualification_configuration": upsert_qualification_configuration,
    "list_whatsapp_accounts": list_whatsapp_accounts,
    "list_whatsapp_templates": list_whatsapp_templates,
    "get_whatsapp_template_status": get_whatsapp_template_status,
    "create_whatsapp_template": create_whatsapp_template,
    "submit_whatsapp_template": submit_whatsapp_template,
    "begin_whatsapp_connection": begin_whatsapp_connection,
    "bind_whatsapp_account_to_pipeline": bind_whatsapp_account_to_pipeline,
    "validate_whatsapp_routing": validate_whatsapp_routing,
    "list_workflow_triggers": list_workflow_triggers,
    "list_workflow_actions": list_workflow_actions,
    "get_workflow_schema": get_workflow_schema,
    "validate_workflow_configuration": validate_workflow_configuration,
    "list_touchpoints": list_touchpoints,
    "upsert_touchpoint": upsert_touchpoint,
    "archive_touchpoint": archive_touchpoint,
    "list_faqs": list_faqs,
    "upsert_faq": upsert_faq,
    "archive_faq": archive_faq,
    "create_knowledge_source": create_knowledge_source,
    "upload_knowledge_document": upload_knowledge_document,
    "publish_knowledge_document": publish_knowledge_document,
    "archive_knowledge_document": archive_knowledge_document,
    "add_hosted_whatsapp_step": add_hosted_whatsapp_step,
    "update_cadence_step": update_cadence_step,
    "delete_cadence_step": delete_cadence_step,
    "reorder_cadence_steps": reorder_cadence_steps,
    "simulate_ai_conversation": simulate_ai_conversation,
    "simulate_workflow": simulate_workflow,
    "simulate_cadence": simulate_cadence,
}
