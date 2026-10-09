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
    submit_whatsapp_templates,
    validate_whatsapp_routing,
)
from apps.integrations.operations.tools.workflows import (
    get_workflow_schema,
    list_workflow_actions,
    list_workflow_triggers,
    validate_workflow_configuration,
)
from apps.integrations.operations.tools.calendar import (
    create_calendar_page,
    get_calendar_configuration,
    get_calendar_available_slots,
    get_calendar_setup_readiness,
    inspect_calendar_public_link,
    probe_calendar_public_https,
    upload_calendar_logo,
    validate_calendar_booking_acceptance,
    get_calendar_delivery_evidence,
    reschedule_booking_operation,
    update_booking_status,
    upsert_calendar_configuration,
    upsert_calendar_reminder,
    validate_calendar_configuration,
    verify_booking,
)
from apps.integrations.operations.tools.commitments import list_commitments, upsert_commitment
from apps.integrations.operations.tools.enhancements import (
    disconnect_integration,
    get_integration_lifecycle,
    run_acceptance_suite,
    validate_cadence_batch,
)
from apps.integrations.operations.tools.onboarding import (
    list_industry_playbooks,
    prepare_account_onboarding,
)
from apps.integrations.operations.tools.traces import get_production_trace
from apps.integrations.operations.tools.sales_branding import list_sales_templates, upsert_sales_template_branding, upload_sales_template_asset
from apps.integrations.operations.tools.team_settings import get_team_settings, upsert_team_settings


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
    "submit_whatsapp_templates": submit_whatsapp_templates,
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
    "get_calendar_configuration": get_calendar_configuration,
    "get_calendar_available_slots": get_calendar_available_slots,
    "get_calendar_setup_readiness": get_calendar_setup_readiness,
    "inspect_calendar_public_link": inspect_calendar_public_link,
    "probe_calendar_public_https": probe_calendar_public_https,
    "upload_calendar_logo": upload_calendar_logo,
    "validate_calendar_booking_acceptance": validate_calendar_booking_acceptance,
    "get_calendar_delivery_evidence": get_calendar_delivery_evidence,
    "create_calendar_page": create_calendar_page,
    "validate_calendar_configuration": validate_calendar_configuration,
    "upsert_calendar_configuration": upsert_calendar_configuration,
    "upsert_calendar_reminder": upsert_calendar_reminder,
    "verify_booking": verify_booking,
    "update_booking_status": update_booking_status,
    "reschedule_booking": reschedule_booking_operation,
    "get_production_trace": get_production_trace,
    "prepare_account_onboarding": prepare_account_onboarding,
    "list_industry_playbooks": list_industry_playbooks,
    "get_integration_lifecycle": get_integration_lifecycle,
    "disconnect_integration": disconnect_integration,
    "validate_cadence_batch": validate_cadence_batch,
    "run_acceptance_suite": run_acceptance_suite,
    "list_commitments": list_commitments,
    "upsert_commitment": upsert_commitment,
    "list_sales_templates": list_sales_templates,
    "upsert_sales_template_branding": upsert_sales_template_branding,
    "upload_sales_template_asset": upload_sales_template_asset,
    "get_team_settings": get_team_settings,
    "upsert_team_settings": upsert_team_settings,
}


from apps.integrations.operations.tools.dashboard_crm import DASHBOARD_CRM_HANDLERS  # noqa: E402
EXTENDED_HANDLERS.update(DASHBOARD_CRM_HANDLERS)


from apps.integrations.operations.tools.vault import VAULT_TOOL_HANDLERS  # noqa: E402
EXTENDED_HANDLERS.update(VAULT_TOOL_HANDLERS)


from apps.integrations.operations.tools.ai_knowledge_dashboard import AI_KNOWLEDGE_TOOL_HANDLERS  # noqa: E402
EXTENDED_HANDLERS.update(AI_KNOWLEDGE_TOOL_HANDLERS)


from apps.integrations.operations.tools.channel_dashboard import CHANNEL_DASHBOARD_HANDLERS  # noqa: E402
EXTENDED_HANDLERS.update(CHANNEL_DASHBOARD_HANDLERS)


from apps.integrations.operations.tools import flow_testing  # noqa: E402
EXTENDED_HANDLERS.update({name: getattr(flow_testing, name) for name in (
    "create_ai_flow_test_run", "run_ai_flow_test_turn", "get_ai_flow_test_run", "cleanup_ai_flow_test_run",
)})
