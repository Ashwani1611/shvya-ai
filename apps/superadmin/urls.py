from .platform_email import platform_email_view
from django.urls import path
from .bac_views import bac_list

from .views import (
    admin_global_search,
    ai_credit_overview_view,
    org_list_view,
    operations_mcp_access_key_generate_view,
    operations_mcp_workspace_view,
    operations_mcp_superadmin_session_revoke_view,
    operations_mcp_support_end_view,
    rag_monitor_view,
    organization_ai_credit_view,
    organization_create_view,
    organization_detail_view,
    organization_generate_login_link_view,
    organization_hosted_account_toggle_view,
    organization_hosted_ignore_download_view,
    organization_hosted_ignore_list_view,
    organization_hosted_ignore_reset_view,
    organization_hosted_ignore_sync_view,
    organization_notes_update_view,
    organization_operations_policy_update_view,
    organization_operations_session_revoke_view,
    organization_operations_support_end_view,
    organization_payment_create_view,
    organization_payment_delete_view,
    organization_payment_update_view,
    organization_pipeline_create_view,
    organization_pipeline_delete_view,
    organization_pipeline_update_view,
    organization_tags_update_view,
    organization_update_view,
    organization_user_create_view,
    organization_user_reset_password_view,
    organization_user_toggle_active_view,
    organization_user_update_view,
    superadmin_login_view,
    superadmin_logout_view,
)

from .plan_controls import organization_modules_view, organization_delete_view, organization_tag_manage_view

urlpatterns = [
    path("email/", platform_email_view, name="superadmin-platform-email"),
    path("tags/", organization_tag_manage_view, name="superadmin-tags"),
    path("organization/<uuid:organization_id>/modules/", organization_modules_view, name="superadmin-organization-modules"),
    path("organization/<uuid:organization_id>/delete/", organization_delete_view, name="superadmin-organization-delete"),
    path("bac/", bac_list, name="superadmin-bac"),
    # =========================================================
    # SUPER ADMIN — LOGIN
    # =========================================================

    path(
        "login/",
        superadmin_login_view,
        name="superadmin-login",
    ),
    path(
        "logout/",
        superadmin_logout_view,
        name="superadmin-logout",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION CONSOLE
    # =========================================================

    path(
        "",
        org_list_view,
        name="superadmin-org-list",
    ),

    path(
        "rag-monitor/",
        rag_monitor_view,
        name="superadmin-rag-monitor",
    ),
    path(
        "mcp/",
        operations_mcp_workspace_view,
        name="superadmin-operations-mcp",
    ),
    path(
        "mcp/keys/generate/",
        operations_mcp_access_key_generate_view,
        name="superadmin-operations-mcp-key-generate",
    ),
    path(
        "mcp/sessions/<uuid:token_id>/revoke/",
        operations_mcp_superadmin_session_revoke_view,
        name="superadmin-operations-mcp-session-revoke",
    ),
    path(
        "mcp/support/<uuid:session_id>/end/",
        operations_mcp_support_end_view,
        name="superadmin-operations-mcp-support-end",
    ),

    # =========================================================
    # SUPER ADMIN — AI CREDITS
    # =========================================================

    path(
        "ai-credits/",
        ai_credit_overview_view,
        name="superadmin-ai-credit-overview",
    ),
    path(
        "organization/<uuid:organization_id>/ai-credits/",
        organization_ai_credit_view,
        name="superadmin-organization-ai-credits",
    ),

    # =========================================================
    # SUPER ADMIN — CREATE ORGANIZATION
    # =========================================================

    path(
        "organization/create/",
        organization_create_view,
        name="superadmin-organization-create",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION DETAIL
    # =========================================================

    path(
        "organization/<uuid:organization_id>/",
        organization_detail_view,
        name="superadmin-organization-detail",
    ),

    # Existing Hosted WhatsApp chat protection lives at the organization
    # level, deliberately outside the Edit Organization form.
    path(
        "organization/<uuid:organization_id>/hosted-ignore/",
        organization_hosted_ignore_list_view,
        name="superadmin-organization-hosted-ignore-list",
    ),
    path(
        "organization/<uuid:organization_id>/hosted-ignore/sync/",
        organization_hosted_ignore_sync_view,
        name="superadmin-organization-hosted-ignore-sync",
    ),
    path(
        "organization/<uuid:organization_id>/hosted-ignore/download/",
        organization_hosted_ignore_download_view,
        name="superadmin-organization-hosted-ignore-download",
    ),
    path(
        "organization/<uuid:organization_id>/hosted-ignore/reset/",
        organization_hosted_ignore_reset_view,
        name="superadmin-organization-hosted-ignore-reset",
    ),

    # =========================================================
    # SUPER ADMIN — GENERATE ONE-TIME LOGIN LINK
    # =========================================================

    path(
        "organization/<uuid:organization_id>/generate-login-link/",
        organization_generate_login_link_view,
        name="superadmin-organization-generate-login-link",
    ),

    # =========================================================
    # SUPER ADMIN — PIPELINES
    # =========================================================

    path(
        "organization/<uuid:organization_id>/pipelines/add/",
        organization_pipeline_create_view,
        name="superadmin-organization-pipeline-add",
    ),
    path(
        "organization/<uuid:organization_id>/pipelines/<uuid:pipeline_id>/edit/",
        organization_pipeline_update_view,
        name="superadmin-organization-pipeline-edit",
    ),
    path(
        "organization/<uuid:organization_id>/pipelines/<uuid:pipeline_id>/delete/",
        organization_pipeline_delete_view,
        name="superadmin-organization-pipeline-delete",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION INFORMATION
    # =========================================================

    path(
        "organization/<uuid:organization_id>/update/",
        organization_update_view,
        name="superadmin-organization-update",
    ),
    path(
        "organization/<uuid:organization_id>/notes/",
        organization_notes_update_view,
        name="superadmin-organization-notes-update",
    ),
    path(
        "organization/<uuid:organization_id>/tags/",
        organization_tags_update_view,
        name="superadmin-organization-tags-update",
    ),
    path(
        "organization/<uuid:organization_id>/hosted-account/toggle/",
        organization_hosted_account_toggle_view,
        name="superadmin-organization-hosted-account-toggle",
    ),

    path(
        "organization/<uuid:organization_id>/operations-mcp-policy/",
        organization_operations_policy_update_view,
        name="superadmin-organization-operations-mcp-policy",
    ),

    path(
        "organization/<uuid:organization_id>/operations-sessions/<uuid:token_id>/revoke/",
        organization_operations_session_revoke_view,
        name="superadmin-organization-operations-session-revoke",
    ),
    path(
        "organization/<uuid:organization_id>/operations-support/<uuid:session_id>/end/",
        organization_operations_support_end_view,
        name="superadmin-organization-operations-support-end",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION USERS
    # =========================================================

    path(
        "organization/<uuid:organization_id>/users/add/",
        organization_user_create_view,
        name="superadmin-organization-user-add",
    ),
    path(
        "organization/<uuid:organization_id>/users/<uuid:user_id>/edit/",
        organization_user_update_view,
        name="superadmin-organization-user-edit",
    ),
    path(
        "organization/<uuid:organization_id>/users/<uuid:user_id>/toggle-active/",
        organization_user_toggle_active_view,
        name="superadmin-organization-user-toggle-active",
    ),
    path(
        "organization/<uuid:organization_id>/users/reset-password/",
        organization_user_reset_password_view,
        name="superadmin-organization-user-reset-password",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION PAYMENTS
    # =========================================================

    path(
        "organization/<uuid:organization_id>/payments/add/",
        organization_payment_create_view,
        name="superadmin-organization-payment-add",
    ),
    path(
        "organization/<uuid:organization_id>/payments/<int:payment_id>/edit/",
        organization_payment_update_view,
        name="superadmin-organization-payment-edit",
    ),
    path(
        "organization/<uuid:organization_id>/payments/<int:payment_id>/delete/",
        organization_payment_delete_view,
        name="superadmin-organization-payment-delete",
    ),

    # =========================================================
    # SHVYA ADMIN — GLOBAL SEARCH
    # =========================================================

    path(
        "search/",
        admin_global_search,
        name="superadmin-global-search",
    ),
]
