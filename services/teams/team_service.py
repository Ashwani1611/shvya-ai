# ruff: noqa: F401
"""Compatibility facade for the canonical Teams domain service.

New application code should import from apps.teams.services.team_service.
"""

from apps.teams.services.team_service import (
    CrossOrganizationMembershipError as CrossOrganizationMembershipError,
    DuplicateMembershipError as DuplicateMembershipError,
    DuplicateTeamError as DuplicateTeamError,
    add_member as add_member,
    create_team as create_team,
    remove_member as remove_member,
    set_member_role as set_member_role,
    update_team as update_team,
)
