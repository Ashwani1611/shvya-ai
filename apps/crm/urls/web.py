from django.urls import path

from .coming_soon import coming_soon_urlpatterns
from apps.accounts.views import crm_signup_view
from apps.crm.authentication import (
    crm_login_view,
    crm_profile_view,
)
from apps.crm.views.dashboard import (
    dashboard_view,
    lead_create_modal,
    lead_create_save,
    lead_call_modal,
    lead_call_save,
    lead_conversation_summary_modal,
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
    lead_stage_create,
    lead_stage_delete,
    lead_stage_ai_toggle,
    lead_stage_move,
    lead_stage_rename,
    lead_table_partial,
    attribute_manage_modal,
    attribute_edit_modal,
    attribute_update_save,
    attribute_delete,
    attribute_create_modal,
    attribute_create_save,
    lead_attribute_values_modal,
    lead_attribute_values_save,
    lead_import_start_modal,
    lead_import_start,
    lead_import_upload_modal,
    lead_import_upload,
    lead_import_sample_file,
    lead_import_mapping_modal,
    lead_import_mapping_save,
    lead_import_destination_modal,
    lead_import_destination_save,
    lead_import_review_modal,
    lead_import_execute,
    global_reminders_modal,
    global_reminder_complete,
    global_reminder_delete,
    global_reminder_edit_save,
    global_reminder_snooze,
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
    # NEW LEAD
    # ========================================================

    path(
        "leads/create/",
        lead_create_modal,
        name="crm-lead-create-modal",
    ),

    path(
        "leads/create/save/",
        lead_create_save,
        name="crm-lead-create-save",
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
    # ATTRIBUTE MANAGEMENT
    # ========================================================

    path(
        "attributes/create/",
        attribute_create_modal,
        name="crm-attribute-create-modal",
    ),

    path(
        "attributes/create/save/",
        attribute_create_save,
        name="crm-attribute-create-save",
    ),

    path(
        "attributes/manage/",
        attribute_manage_modal,
        name="crm-attribute-manage-modal",
    ),

    path(
        "attributes/<uuid:attribute_id>/edit/",
        attribute_edit_modal,
        name="crm-attribute-edit-modal",
    ),

    path(
        "attributes/<uuid:attribute_id>/edit/save/",
        attribute_update_save,
        name="crm-attribute-update-save",
    ),

    path(
        "attributes/<uuid:attribute_id>/delete/",
        attribute_delete,
        name="crm-attribute-delete",
    ),

    path(
        "leads/<uuid:lead_id>/attributes/edit/",
        lead_attribute_values_modal,
        name="crm-lead-attribute-values-modal",
    ),

    path(
        "leads/<uuid:lead_id>/attributes/edit/save/",
        lead_attribute_values_save,
        name="crm-lead-attribute-values-save",
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


    # ========================================================
    # STAGE MANAGEMENT
    # ========================================================

    path(
        "stages/create/",
        lead_stage_create,
        name="crm-stage-create",
    ),

    path(
        "stages/<uuid:stage_id>/rename/",
        lead_stage_rename,
        name="crm-stage-rename",
    ),

    path(
    "stages/<uuid:stage_id>/ai-toggle/",
    lead_stage_ai_toggle,
    name="crm-stage-ai-toggle",
    ),

    path(
        "stages/<uuid:stage_id>/delete/",
        lead_stage_delete,
        name="crm-stage-delete",
    ),

    path(
        "leads/<uuid:lead_id>/stage/",
        lead_stage_move,
        name="crm-lead-stage-move",
    ),

    # ========================================================
    # IMPORT LEADS
    # ========================================================

    path(
        "leads/import/",
        lead_import_start_modal,
        name="crm-lead-import-start-modal",
    ),

    path(
        "leads/import/start/",
        lead_import_start,
        name="crm-lead-import-start",
    ),

    # ========================================================
    # IMPORT LEADS — FILE UPLOAD
    # ========================================================

    path(
        "leads/import/upload/",
        lead_import_upload_modal,
        name="crm-lead-import-upload-modal",
    ),

    path(
        "leads/import/upload/save/",
        lead_import_upload,
        name="crm-lead-import-upload",
    ),

    path(
        "leads/import/sample/",
        lead_import_sample_file,
        name="crm-lead-import-sample",
    ),

    # ========================================================
    # IMPORT LEADS — FIELD MAPPING
    # ========================================================

    path(
        "leads/import/mapping/",
        lead_import_mapping_modal,
        name="crm-lead-import-mapping-modal",
    ),

    path(
        "leads/import/mapping/save/",
        lead_import_mapping_save,
        name="crm-lead-import-mapping-save",
    ),

    # ========================================================
    # IMPORT LEADS — DESTINATION / ASSIGNMENT
    # ========================================================

    path(
        "leads/import/destination/",
        lead_import_destination_modal,
        name="crm-lead-import-destination-modal",
    ),

    path(
        "leads/import/destination/save/",
        lead_import_destination_save,
        name="crm-lead-import-destination-save",
    ),

    # ========================================================
    # IMPORT LEADS — REVIEW / EXECUTE
    # ========================================================

    path(
        "leads/import/review/",
        lead_import_review_modal,
        name="crm-lead-import-review-modal",
    ),

    path(
        "leads/import/execute/",
        lead_import_execute,
        name="crm-lead-import-execute",
    ),

# ========================================================
# GLOBAL REMINDERS
# ========================================================

path(
    "reminders/",
    global_reminders_modal,
    name="crm-global-reminders-modal",
),

path(
    "reminders/<uuid:reminder_id>/complete/",
    global_reminder_complete,
    name="crm-global-reminder-complete",
),

path(
    "reminders/<uuid:reminder_id>/snooze/",
    global_reminder_snooze,
    name="crm-global-reminder-snooze",
),

path(
    "reminders/<uuid:reminder_id>/delete/",
    global_reminder_delete,
    name="crm-global-reminder-delete",
),

path(
    "reminders/<uuid:reminder_id>/edit/save/",
    global_reminder_edit_save,
    name="crm-global-reminder-edit-save",
),

# ========================================================
# CONVERSATION SUMMARY
# ========================================================

path(
    "leads/<uuid:lead_id>/conversation-summary/",
    lead_conversation_summary_modal,
    name="crm-lead-conversation-summary-modal",
),
]

urlpatterns += coming_soon_urlpatterns