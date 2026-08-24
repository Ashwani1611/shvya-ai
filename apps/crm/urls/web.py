from django.urls import path

from apps.accounts.views import crm_signup_view
from apps.crm.authentication import crm_login_view, crm_profile_view
from apps.crm.views.dashboard import (
    dashboard_view,
    lead_edit_modal,
    lead_edit_save,
    lead_edit_stages,
    lead_filters_modal,
    lead_filters_values,
    lead_table_partial,
)

urlpatterns = [
    path("login/", crm_login_view, name="crm-login"),
    path("signup/", crm_signup_view, name="crm-signup"),
    path("profile/", crm_profile_view, name="crm-profile"),
    path("", dashboard_view, name="crm-dashboard"),
    path("leads/table/", lead_table_partial, name="crm-lead-table-partial"),
    path("leads/<uuid:lead_id>/edit/", lead_edit_modal, name="crm-lead-edit-modal"),
    path("leads/edit/stages/", lead_edit_stages, name="crm-lead-edit-stages"),
    path("leads/<uuid:lead_id>/edit/save/", lead_edit_save, name="crm-lead-edit-save"),
    path("leads/filters/", lead_filters_modal, name="crm-lead-filters-modal"),
    path("leads/filters/values/", lead_filters_values, name="crm-lead-filters-values"),
]
