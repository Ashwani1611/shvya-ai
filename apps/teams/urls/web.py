from django.urls import path

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
    path("create/", team_create_view, name="crm-team-create"),
    path("<uuid:team_id>/", team_detail_view, name="crm-team-detail"),
    path("<uuid:team_id>/edit/", team_edit_view, name="crm-team-edit"),
    path("<uuid:team_id>/delete/", team_delete_view, name="crm-team-delete"),
    path("<uuid:team_id>/members/add/", team_member_add_view, name="crm-team-member-add"),
    path("<uuid:team_id>/members/<uuid:user_id>/remove/", team_member_remove_view, name="crm-team-member-remove"),
    path("<uuid:team_id>/members/<uuid:user_id>/role/", team_member_role_view, name="crm-team-member-role"),

]