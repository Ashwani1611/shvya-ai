from django.urls import path

from apps.triggers.views.trigger_views import (
    trigger_create_page,
    trigger_create_save,
    trigger_delete,
    trigger_duplicate,
    trigger_edit_page,
    trigger_list,
    trigger_toggle,
    trigger_update_save,
)


urlpatterns = [
    path("", trigger_list, name="crm-smart-triggers"),
    path("new/", trigger_create_page, name="smart-trigger-create"),
    path("new/save/", trigger_create_save, name="smart-trigger-create-save"),
    path("<uuid:trigger_id>/", trigger_edit_page, name="smart-trigger-edit"),
    path(
        "<uuid:trigger_id>/save/",
        trigger_update_save,
        name="smart-trigger-update-save",
    ),
    path("<uuid:trigger_id>/toggle/", trigger_toggle, name="smart-trigger-toggle"),
    path(
        "<uuid:trigger_id>/duplicate/",
        trigger_duplicate,
        name="smart-trigger-duplicate",
    ),
    path("<uuid:trigger_id>/delete/", trigger_delete, name="smart-trigger-delete"),
]
