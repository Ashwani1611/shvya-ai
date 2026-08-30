"""
TeamService — business logic for Team creation and membership management
lives here, never in views or serializers (CLAUDE.md rule 2).
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from apps.teams.models import Team, TeamMembership


class DuplicateTeamError(Exception):
    """Raised when a team with this (organization, name) already exists."""


class CrossOrganizationMembershipError(Exception):
    """Raised when trying to add a user to a team outside their organization."""


class DuplicateMembershipError(Exception):
    """Raised when the user is already a member of this team."""


def create_team(*, organization, name, description=""):
    team = Team(organization=organization, name=name, description=description)
    try:
        team.full_clean()
        team.save()
    except (DjangoValidationError, IntegrityError) as exc:
        raise DuplicateTeamError(
            f"A team named '{name}' already exists for this organization."
        ) from exc
    return team


def update_team(*, team, name=None, description=None, is_active=None):
    if name is not None:
        team.name = name
    if description is not None:
        team.description = description
    if is_active is not None:
        team.is_active = is_active
    try:
        team.full_clean()
        team.save()
    except (DjangoValidationError, IntegrityError) as exc:
        raise DuplicateTeamError(
            f"A team named '{team.name}' already exists for this organization."
        ) from exc
    return team


def add_member(*, team, user, role=TeamMembership.Role.MEMBER):
    """Enforces that the user belongs to the same organization as the team."""
    if user.organization_id != team.organization_id:
        raise CrossOrganizationMembershipError(
            "User does not belong to this team's organization."
        )
    try:
        with transaction.atomic():
            membership = TeamMembership.objects.create(team=team, user=user, role=role)
    except IntegrityError as exc:
        raise DuplicateMembershipError(f"{user} is already a member of {team}.") from exc
    return membership


def remove_member(*, team, user):
    """No-op if they aren't a member."""
    TeamMembership.objects.filter(team=team, user=user).delete()


def set_member_role(*, team, user, role):
    membership = TeamMembership.objects.get(team=team, user=user)
    membership.role = role
    membership.save(update_fields=["role"])
    return membership