import pytest

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.teams.models import Team, TeamMembership
from services.teams.team_service import (
    CrossOrganizationMembershipError,
    DuplicateMembershipError,
    DuplicateTeamError,
    add_member,
    create_team,
    remove_member,
    set_member_role,
    update_team,
)


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


@pytest.fixture
def outside_user(other_organization):
    return User.objects.create_user(
        email="agent@globex.test",
        organization=other_organization,
        name="Outside Agent",
        role=User.Role.AGENT,
    )


@pytest.mark.django_db
class TestCreateTeam:

    def test_create_team(self, organization):
        team = create_team(organization=organization, name="Inbound Sales")
        assert team.pk is not None
        assert team.name == "Inbound Sales"

    def test_duplicate_name_raises(self, organization):
        create_team(organization=organization, name="Inbound Sales")

        with pytest.raises(DuplicateTeamError):
            create_team(organization=organization, name="Inbound Sales")


@pytest.mark.django_db
class TestUpdateTeam:

    def test_partial_update(self, organization):
        team = create_team(organization=organization, name="Inbound Sales")
        updated = update_team(team=team, description="Handles new leads")

        assert updated.description == "Handles new leads"
        assert updated.name == "Inbound Sales"


@pytest.mark.django_db
class TestMembership:

    def test_add_member(self, organization, user):
        team = create_team(organization=organization, name="Support")
        membership = add_member(team=team, user=user)

        assert membership.role == TeamMembership.Role.MEMBER
        assert TeamMembership.objects.filter(team=team, user=user).exists()

    def test_add_member_from_other_org_raises(self, organization, outside_user):
        team = create_team(organization=organization, name="Support")

        with pytest.raises(CrossOrganizationMembershipError):
            add_member(team=team, user=outside_user)

    def test_add_duplicate_member_raises(self, organization, user):
        team = create_team(organization=organization, name="Support")
        add_member(team=team, user=user)

        with pytest.raises(DuplicateMembershipError):
            add_member(team=team, user=user)

    def test_remove_member(self, organization, user):
        team = create_team(organization=organization, name="Support")
        add_member(team=team, user=user)

        remove_member(team=team, user=user)

        assert not TeamMembership.objects.filter(team=team, user=user).exists()

    def test_remove_member_is_a_noop_if_not_a_member(self, organization, user):
        team = create_team(organization=organization, name="Support")
        # Should not raise even though `user` was never added.
        remove_member(team=team, user=user)

    def test_set_member_role(self, organization, user):
        team = create_team(organization=organization, name="Support")
        add_member(team=team, user=user)

        membership = set_member_role(
            team=team, user=user, role=TeamMembership.Role.LEAD
        )

        assert membership.role == TeamMembership.Role.LEAD