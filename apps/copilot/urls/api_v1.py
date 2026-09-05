from django.urls import path

from apps.copilot.views.api import (
    config_api,
    flags_api,
    move_stage_api,
    resolve_api,
    snooze_api,
)


urlpatterns = [
    path("flags/", flags_api, name="copilot-flags-api"),
    path("flags/<uuid:flag_id>/snooze/", snooze_api, name="copilot-snooze-api"),
    path("flags/<uuid:flag_id>/resolve/", resolve_api, name="copilot-resolve-api"),
    path("config/", config_api, name="copilot-config-api"),
    path(
        "leads/<uuid:lead_id>/move-stage/",
        move_stage_api,
        name="copilot-move-stage-api",
    ),
]
