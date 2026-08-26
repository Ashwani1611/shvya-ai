from django.urls import path

from apps.accounts.views import crm_signup_view
from apps.crm.authentication import (
    crm_login_view,
    crm_profile_view,
)
from apps.crm.views.dashboard import (
    dashboard_view,
    lead_call_modal,
    lead_call_save,
    lead_card_partial,
    lead_detail,
    lead_edit_modal,
    lead_edit_save,
    lead_edit_stages,
    lead_filters_modal,
    lead_filters_values,
    lead_note_modal,
    lead_note_save,
    lead_reminder_modal,
    lead_reminder_save,
    lead_table_partial,
)


urlpatterns = [

    # ========================================================
    # AUTHENTICATION
    # ========================================================

    path(
        "login/",
        crm_login_view,
        name="crm-login",
    ),

    path(
        "signup/",
        crm_signup_view,
        name="crm-signup",
    ),

    path(
        "profile/",
        crm_profile_view,
        name="crm-profile",
    ),


    # ========================================================
    # DASHBOARD
    # ========================================================

    path(
        "",
        dashboard_view,
        name="crm-dashboard",
    ),


    # ========================================================
    # LEAD TABLE
    # ========================================================

    path(
        "leads/table/",
        lead_table_partial,
        name="crm-lead-table-partial",
    ),


    # ========================================================
    # LEAD DETAIL
    # ========================================================

    path(
        "leads/<uuid:lead_id>/",
        lead_detail,
        name="crm-lead-detail",
    ),


    # ========================================================
    # LEAD CARD
    # ========================================================

    path(
        "leads/<uuid:lead_id>/card/",
        lead_card_partial,
        name="crm-lead-card-partial",
    ),


    # ========================================================
    # EDIT LEAD
    # ========================================================

    path(
        "leads/<uuid:lead_id>/edit/",
        lead_edit_modal,
        name="crm-lead-edit-modal",
    ),

    path(
        "leads/edit/stages/",
        lead_edit_stages,
        name="crm-lead-edit-stages",
    ),

    path(
        "leads/<uuid:lead_id>/edit/save/",
        lead_edit_save,
        name="crm-lead-edit-save",
    ),


    # ========================================================
    # CALL
    # ========================================================

    path(
        "leads/<uuid:lead_id>/call/",
        lead_call_modal,
        name="crm-lead-call-modal",
    ),

    path(
        "leads/<uuid:lead_id>/call/save/",
        lead_call_save,
        name="crm-lead-call-save",
    ),


    # ========================================================
    # REMINDER
    # ========================================================

    path(
        "leads/<uuid:lead_id>/reminder/",
        lead_reminder_modal,
        name="crm-lead-reminder-modal",
    ),

    path(
        "leads/<uuid:lead_id>/reminder/save/",
        lead_reminder_save,
        name="crm-lead-reminder-save",
    ),


    # ========================================================
    # NOTE
    # ========================================================

    path(
        "leads/<uuid:lead_id>/note/",
        lead_note_modal,
        name="crm-lead-note-modal",
    ),

    path(
        "leads/<uuid:lead_id>/note/save/",
        lead_note_save,
        name="crm-lead-note-save",
    ),


    # ========================================================
    # FILTERS
    # ========================================================

    path(
        "leads/filters/",
        lead_filters_modal,
        name="crm-lead-filters-modal",
    ),

    path(
        "leads/filters/values/",
        lead_filters_values,
        name="crm-lead-filters-values",
    ),

]