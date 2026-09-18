from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.authentication import crm_login_required
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
from services.teams.whatsapp_connections import member_whatsapp_connections


def _can_manage(user):
    return user.is_superuser or user.role in (
        User.Role.SUPERADMIN,
        User.Role.ADMIN,
    )


def _whatsapp_settings_url(account, member):
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return f"{reverse('whatsapp-connect-hosted')}?{urlencode({'settings': account.id})}"
    # API and Business App Coexistence intentionally share the Cloud API gear.
    params = urlencode({"owner": member.id, "settings": account.id})
    return f"{reverse('whatsapp-accounts')}?{params}"


@crm_login_required
def team_list_view(request):
    """List organization members with one verified, pipeline-owned connection."""
    user = request.crm_user
    members = list(User.objects.filter(organization=user.organization).order_by("name"))
    connections = member_whatsapp_connections(
        organization=user.organization,
        members=members,
    )
    for member in members:
        member.whatsapp_connection = connections[member.pk]
    response = render(
        request,
        "teams/team_list.html",
        {"members": members, "can_manage": _can_manage(user)},
    )
    response["Cache-Control"] = "no-store"
    return response


@crm_login_required
def team_member_automation_settings_view(request, user_id):
    """Open the exact connection shown in Teams, after revalidating ownership."""
    admin = request.crm_user
    if not _can_manage(admin):
        messages.error(request, "Only organization admins can manage lead automation.")
        return redirect("crm-teams")

    # An admin may manage a still-connected number owned by an inactive member.
    # The member's activity and the WhatsApp connection are independent states.
    member = get_object_or_404(User, id=user_id, organization=admin.organization)
    connection = member_whatsapp_connections(
        organization=admin.organization,
        members=[member],
    )[member.pk]
    account = connection["account"]
    requested_account = request.GET.get("account", "").strip()
    if requested_account and (account is None or str(account.id) != requested_account):
        messages.error(
            request,
            "This WhatsApp connection has changed or is no longer linked to this member. Refresh Teams and try again.",
        )
        return redirect("crm-teams")

    if account is not None:
        return redirect(_whatsapp_settings_url(account, member))

    # Preserve the existing selection screen for old/bookmarked, unpinned URLs.
    # New Teams gears always include the exact selected account ID above.
    if not requested_account and len(connection["candidates"]) > 1:
        return render(
            request,
            "teams/member_automation_accounts.html",
            {
                "member": member,
                "account_rows": [
                    {
                        "account": candidate,
                        "is_hosted": candidate.connection_type == WhatsAppAccount.ConnectionType.coexisted,
                        "connection_label": candidate.team_connection_label,
                        "settings_url": _whatsapp_settings_url(candidate, member),
                    }
                    for candidate in connection["candidates"]
                ],
            },
        )

    messages.error(request, connection["reason"])
    return redirect("crm-teams")


@crm_login_required
def team_create_view(request):
    user = request.crm_user

    if not _can_manage(user):
        messages.error(request, "Only org admins can create teams.")
        return redirect("crm-teams")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if not name:
            messages.error(request, "Team name is required.")
            return render(request, "teams/team_form.html", {"mode": "create", "name": name, "description": description})

        try:
            team = create_team(organization=user.organization, name=name, description=description)
        except DuplicateTeamError as exc:
            messages.error(request, str(exc))
            return render(request, "teams/team_form.html", {"mode": "create", "name": name, "description": description})

        messages.success(request, f"Team '{team.name}' created.")
        return redirect("crm-team-detail", team_id=team.id)

    return render(request, "teams/team_form.html", {"mode": "create"})


@crm_login_required
def team_edit_view(request, team_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    if not _can_manage(user):
        messages.error(request, "Only org admins can edit teams.")
        return redirect("crm-team-detail", team_id=team.id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        is_active = request.POST.get("is_active") == "on"

        if not name:
            messages.error(request, "Team name is required.")
            return render(request, "teams/team_form.html", {"mode": "edit", "team": team, "name": name, "description": description})

        try:
            update_team(team=team, name=name, description=description, is_active=is_active)
        except DuplicateTeamError as exc:
            messages.error(request, str(exc))
            return render(request, "teams/team_form.html", {"mode": "edit", "team": team, "name": name, "description": description})

        messages.success(request, f"Team '{team.name}' updated.")
        return redirect("crm-team-detail", team_id=team.id)

    return render(request, "teams/team_form.html", {"mode": "edit", "team": team})


@crm_login_required
def team_delete_view(request, team_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    if not _can_manage(user):
        messages.error(request, "Only org admins can delete teams.")
        return redirect("crm-team-detail", team_id=team.id)

    if request.method == "POST":
        team_name = team.name
        team.delete()
        messages.success(request, f"Team '{team_name}' deleted.")
        return redirect("crm-teams")

    return redirect("crm-team-detail", team_id=team.id)


@crm_login_required
def team_detail_view(request, team_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    memberships = team.memberships.select_related("user").all()
    member_user_ids = memberships.values_list("user_id", flat=True)

    available_users = User.objects.filter(
        organization=user.organization,
    ).exclude(
        id__in=member_user_ids,
    ).order_by("name")

    return render(
        request,
        "teams/team_detail.html",
        {
            "team": team,
            "memberships": memberships,
            "available_users": available_users,
            "can_manage": _can_manage(user),
            "role_choices": TeamMembership.Role.choices,
        },
    )


@crm_login_required
def team_member_add_view(request, team_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    if not _can_manage(user):
        messages.error(request, "Only org admins can add team members.")
        return redirect("crm-team-detail", team_id=team.id)

    if request.method == "POST":
        target_user_id = request.POST.get("user_id")
        role = request.POST.get("role", TeamMembership.Role.MEMBER)
        target_user = get_object_or_404(User, id=target_user_id)

        try:
            add_member(team=team, user=target_user, role=role)
        except (CrossOrganizationMembershipError, DuplicateMembershipError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Added {target_user.name} to '{team.name}'.")

    return redirect("crm-team-detail", team_id=team.id)


@crm_login_required
def team_member_remove_view(request, team_id, user_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    if not _can_manage(user):
        messages.error(request, "Only org admins can remove team members.")
        return redirect("crm-team-detail", team_id=team.id)

    if request.method == "POST":
        target_user = get_object_or_404(User, id=user_id)
        remove_member(team=team, user=target_user)
        messages.success(request, f"Removed {target_user.name} from '{team.name}'.")

    return redirect("crm-team-detail", team_id=team.id)


@crm_login_required
def team_member_role_view(request, team_id, user_id):
    user = request.crm_user
    team = get_object_or_404(Team, id=team_id, organization=user.organization)

    if not _can_manage(user):
        messages.error(request, "Only org admins can change member roles.")
        return redirect("crm-team-detail", team_id=team.id)

    if request.method == "POST":
        target_user = get_object_or_404(User, id=user_id)
        role = request.POST.get("role", TeamMembership.Role.MEMBER)

        try:
            set_member_role(team=team, user=target_user, role=role)
        except TeamMembership.DoesNotExist:
            messages.error(request, f"{target_user.name} is not a member of this team.")
        else:
            messages.success(request, f"Updated {target_user.name}'s role.")

    return redirect("crm-team-detail", team_id=team.id)
