import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.pagination import StandardResultsPagination
from apps.core.permissions import IsOrgMember
from apps.teams.models import Team, TeamMembership
from apps.teams.serializers import (
    TeamCreateSerializer, TeamMemberAddSerializer, TeamMemberRoleUpdateSerializer,
    TeamMemberSerializer, TeamSerializer, TeamUpdateSerializer,
)
from services.teams.team_service import (
    CrossOrganizationMembershipError, DuplicateMembershipError, DuplicateTeamError,
    add_member, create_team, remove_member, set_member_role, update_team,
)

logger = logging.getLogger(__name__)


def _is_org_admin(user):
    return user.is_superuser or user.role in (User.Role.SUPERADMIN, User.Role.ADMIN)


class TeamListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]
    pagination_class = StandardResultsPagination

    def get(self, request, *args, **kwargs):
        queryset = Team.objects.filter(organization=request.user.organization).prefetch_related("memberships__user")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        serializer = TeamSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can create teams."}, status=status.HTTP_403_FORBIDDEN)
        serializer = TeamCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            team = create_team(
                organization=request.user.organization,
                name=serializer.validated_data["name"],
                description=serializer.validated_data.get("description", ""),
            )
        except DuplicateTeamError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TeamSerializer(team).data, status=status.HTTP_201_CREATED)


class TeamDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def _get_team(self, request, team_id):
        return get_object_or_404(Team, id=team_id, organization=request.user.organization)

    def get(self, request, team_id, *args, **kwargs):
        return Response(TeamSerializer(self._get_team(request, team_id)).data)

    def patch(self, request, team_id, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can update teams."}, status=status.HTTP_403_FORBIDDEN)
        team = self._get_team(request, team_id)
        serializer = TeamUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            team = update_team(team=team, **serializer.validated_data)
        except DuplicateTeamError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TeamSerializer(team).data)

    def delete(self, request, team_id, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can delete teams."}, status=status.HTTP_403_FORBIDDEN)
        self._get_team(request, team_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TeamMemberListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def _get_team(self, request, team_id):
        return get_object_or_404(Team, id=team_id, organization=request.user.organization)

    def get(self, request, team_id, *args, **kwargs):
        team = self._get_team(request, team_id)
        memberships = team.memberships.select_related("user")
        serializer = TeamMemberSerializer(memberships, many=True)
        return Response(serializer.data)

    def post(self, request, team_id, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can add team members."}, status=status.HTTP_403_FORBIDDEN)
        team = self._get_team(request, team_id)
        serializer = TeamMemberAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_object_or_404(User, id=serializer.validated_data["user_id"])
        try:
            add_member(team=team, user=user, role=serializer.validated_data["role"])
        except (CrossOrganizationMembershipError, DuplicateMembershipError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TeamSerializer(team).data, status=status.HTTP_201_CREATED)


class TeamMemberDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def _get_team(self, request, team_id):
        return get_object_or_404(Team, id=team_id, organization=request.user.organization)

    def patch(self, request, team_id, user_id, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can change member roles."}, status=status.HTTP_403_FORBIDDEN)
        team = self._get_team(request, team_id)
        user = get_object_or_404(User, id=user_id)
        serializer = TeamMemberRoleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            set_member_role(team=team, user=user, role=serializer.validated_data["role"])
        except TeamMembership.DoesNotExist:
            return Response({"message": f"{user} is not a member of this team."}, status=status.HTTP_404_NOT_FOUND)
        return Response(TeamSerializer(team).data)

    def delete(self, request, team_id, user_id, *args, **kwargs):
        if not _is_org_admin(request.user):
            return Response({"message": "Only org admins can remove team members."}, status=status.HTTP_403_FORBIDDEN)
        team = self._get_team(request, team_id)
        user = get_object_or_404(User, id=user_id)
        remove_member(team=team, user=user)
        return Response(status=status.HTTP_204_NO_CONTENT)