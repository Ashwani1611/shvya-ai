from django.urls import path

from apps.crm.views.stage_editor import (
    stage_editor_ai_toggle,
    stage_editor_create,
    stage_editor_delete,
    stage_editor_modal,
    stage_editor_update,
)


urlpatterns = [
    path(
        "",
        stage_editor_modal,
        name="crm-stage-editor-modal",
    ),
    path(
        "create/",
        stage_editor_create,
        name="crm-stage-editor-create",
    ),
    path(
        "<uuid:stage_id>/save/",
        stage_editor_update,
        name="crm-stage-editor-update",
    ),
    path(
        "<uuid:stage_id>/ai-toggle/",
        stage_editor_ai_toggle,
        name="crm-stage-editor-ai-toggle",
    ),
    path(
        "<uuid:stage_id>/delete/",
        stage_editor_delete,
        name="crm-stage-editor-delete",
    ),
]
