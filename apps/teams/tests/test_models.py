import pytest
from django.db import IntegrityError

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.teams.models import Team, TeamMembership


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Acme Inc")


@pytest.fixture
def other_organization(db):
    return Organization.objects.create(name="Globex Corp")


@pytest.fixture
def user(organization):
    return User.objects.create_user(
        email="agent@acme.test",
        organization=organization,
        name="Agent Smith",
        role=User.Role.AGENT,
    )


@pytest.mark.django_db
class TestTeam:

    def test_create_team(self, organization):
        team = Team.objects.create(
            organization=organization,
            name="Inbound Sales",
        )
        assert team.name == "Inbound Sales"
        assert team.is_active is True

    def test_team_name_unique_per_organization(self, organization):
        Team.objects.create(organization=organization, name="Inbound Sales")

        with pytest.raises(IntegrityError):
            Team.objects.create(organization=organization, name="Inbound Sales")

    def test_same_team_name_allowed_across_organizations(
        self, organization, other_organization
    ):
        Team.objects.create(organization=organization, name="Inbound Sales")
        # Should not raise — uniqueness is scoped per-organization.
        Team.objects.create(organization=other_organization, name="Inbound Sales")


@pytest.mark.django_db
class TestTeamMembership:

    def test_add_membership(self, organization, user):
        team = Team.objects.create(organization=organization, name="Support")
        membership = TeamMembership.objects.create(team=team, user=user)

        assert membership.role == TeamMembership.Role.MEMBER
        assert user in team.members.all()

    def test_duplicate_membership_rejected(self, organization, user):
        team = Team.objects.create(organization=organization, name="Support")
        TeamMembership.objects.create(team=team, user=user)

        with pytest.raises(IntegrityError):
            TeamMembership.objects.create(team=team, user=user)