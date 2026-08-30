from django.urls import path

from apps.teams.views.team_views import (
    TeamDetailAPIView, TeamListCreateAPIView, TeamMemberDetailAPIView, TeamMemberListAPIView,
)

urlpatterns = [
    path("", TeamListCreateAPIView.as_view(), name="team-list-create"),
    path("<uuid:team_id>/", TeamDetailAPIView.as_view(), name="team-detail"),
    path("<uuid:team_id>/members/", TeamMemberListAPIView.as_view(), name="team-member-list"),
    path("<uuid:team_id>/members/<uuid:user_id>/", TeamMemberDetailAPIView.as_view(), name="team-member-detail"),
]