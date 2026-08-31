from django.urls import path

from apps.core.coming_soon import coming_soon
from apps.teams.views.web import (
    team_create_view,
    team_delete_view,
    team_detail_view,
    team_edit_view,
    team_list_view,
    team_member_add_view,
    team_member_remove_view,
    team_member_role_view,
)

urlpatterns = [

    path("", team_list_view, name="crm-teams"),

    # ---------------------------------------------------------
    # Per-agent automation settings (gear icon on each row).
    # Not built yet -- placeholder until the settings model and
    # the automation engine behind it exist.
    # ---------------------------------------------------------

    path("settings/", coming_soon, {"feature": "team-member-settings"}, name="crm-team-settings"),

    # ---------------------------------------------------------
    # Team-grouping CRUD (Team / TeamMembership). Kept working
    # but currently unlinked from the sidebar -- the Teams page
    # now shows a flat org member list instead. Revisit if
    # grouping comes back into scope.
    # ---------------------------------------------------------

    path("groups/create/", team_create_view, name="crm-team-create"),
    path("groups/<uuid:team_id>/", team_detail_view, name="crm-team-detail"),
    path("groups/<uuid:team_id>/edit/", team_edit_view, name="crm-team-edit"),
    path("groups/<uuid:team_id>/delete/", team_delete_view, name="crm-team-delete"),
    path("groups/<uuid:team_id>/members/add/", team_member_add_view, name="crm-team-member-add"),
    path("groups/<uuid:team_id>/members/<uuid:user_id>/remove/", team_member_remove_view, name="crm-team-member-remove"),
    path("groups/<uuid:team_id>/members/<uuid:user_id>/role/", team_member_role_view, name="crm-team-member-role"),

]