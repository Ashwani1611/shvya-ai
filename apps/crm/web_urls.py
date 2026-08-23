from django.urls import path

from .views import (
    crm_login_view,
    crm_profile_view,
    dashboard_view,
    lead_table_partial,
    lead_edit_modal,
    lead_edit_stages,
    lead_edit_save,
    lead_filters_modal,
    lead_filters_values,
)

from apps.accounts.views import (
    crm_signup_view,
)


urlpatterns = [
    # =========================================================
    # CRM LOGIN
    # =========================================================

    path(
        "login/",
        crm_login_view,
        name="crm-login",
    ),

    # =========================================================
    # CRM SIGNUP
    #
    # Signup/account creation logic lives in:
    #
    # apps.accounts.views.crm_signup_view
    #
    # It is imported above so CRM web URLs can expose:
    #
    # /dashboard/signup/
    # =========================================================

    path(
        "signup/",
        crm_signup_view,
        name="crm-signup",
    ),

    # =========================================================
    # CRM PROFILE
    # =========================================================

    path(
        "profile/",
        crm_profile_view,
        name="crm-profile",
    ),

    # =========================================================
    # CRM DASHBOARD
    # =========================================================

    path(
        "",
        dashboard_view,
        name="crm-dashboard",
    ),

    # =========================================================
    # LEAD TABLE
    # =========================================================

    path(
        "leads/table/",
        lead_table_partial,
        name="crm-lead-table-partial",
    ),

    # =========================================================
    # EDIT LEAD MODAL
    # =========================================================

    path(
        "leads/<uuid:lead_id>/edit/",
        lead_edit_modal,
        name="crm-lead-edit-modal",
    ),

    # =========================================================
    # LOAD LEAD EDIT STAGES
    # =========================================================

    path(
    "leads/edit/stages/",
    lead_edit_stages,
    name="crm-lead-edit-stages",
    ),

    # =========================================================
    # SAVE LEAD
    # =========================================================

    path(
        "leads/<uuid:lead_id>/edit/save/",
        lead_edit_save,
        name="crm-lead-edit-save",
    ),

    # =========================================================
    # FILTER MODAL
    # =========================================================

    path(
        "leads/filters/",
        lead_filters_modal,
        name="crm-lead-filters-modal",
    ),

    # =========================================================
    # FILTER VALUES
    # =========================================================

    path(
        "leads/filters/values/",
        lead_filters_values,
        name="crm-lead-filters-values",
    ),
]