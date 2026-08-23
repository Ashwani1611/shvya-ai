from django.urls import path

from .views import (
    admin_global_search,
    organization_create_view,
    organization_detail_view,
    organization_generate_login_link_view,
    superadmin_login_view,
    organization_payment_create_view,
    organization_payment_delete_view,
    organization_payment_update_view,
    organization_pipeline_create_view,
    organization_pipeline_delete_view,
    organization_pipeline_update_view,
    organization_update_view,
    organization_user_create_view,
    organization_user_reset_password_view,
    organization_user_toggle_active_view,
    organization_user_update_view,
    org_list_view,
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

    # Create Pipeline
    path(
        "organization/<uuid:organization_id>/pipelines/add/",
        organization_pipeline_create_view,
        name="superadmin-organization-pipeline-add",
    ),

    # Edit Pipeline
    path(
        "organization/<uuid:organization_id>/pipelines/<uuid:pipeline_id>/edit/",
        organization_pipeline_update_view,
        name="superadmin-organization-pipeline-edit",
    ),

    # Delete Pipeline
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

    # =========================================================
    # SUPER ADMIN — ORGANIZATION USERS
    # =========================================================

    # Create User
    path(
        "organization/<uuid:organization_id>/users/add/",
        organization_user_create_view,
        name="superadmin-organization-user-add",
    ),

    # Edit User
    path(
        "organization/<uuid:organization_id>/users/<uuid:user_id>/edit/",
        organization_user_update_view,
        name="superadmin-organization-user-edit",
    ),

    # Enable / Disable User
    path(
        "organization/<uuid:organization_id>/users/<uuid:user_id>/toggle-active/",
        organization_user_toggle_active_view,
        name="superadmin-organization-user-toggle-active",
    ),

    # Reset User Password
    path(
        "organization/<uuid:organization_id>/users/reset-password/",
        organization_user_reset_password_view,
        name="superadmin-organization-user-reset-password",
    ),

    # =========================================================
    # SUPER ADMIN — ORGANIZATION PAYMENTS
    # =========================================================

    # Add Payment
    path(
        "organization/<uuid:organization_id>/payments/add/",
        organization_payment_create_view,
        name="superadmin-organization-payment-add",
    ),

    # Edit Payment
    path(
        "organization/<uuid:organization_id>/payments/<int:payment_id>/edit/",
        organization_payment_update_view,
        name="superadmin-organization-payment-edit",
    ),

    # Delete Payment
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