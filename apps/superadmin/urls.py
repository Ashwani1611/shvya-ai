from django.urls import path

from .views import (
    admin_global_search,
    ai_credit_overview_view,
    org_list_view,
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
)

urlpatterns = [
    # =========================================================
    # SUPER ADMIN — LOGIN
    # =========================================================

    path(
        "login/",
        superadmin_login_view,
        name="superadmin-login",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION CONSOLE
    # =========================================================

    path(
        "",
        org_list_view,
        name="superadmin-org-list",
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
