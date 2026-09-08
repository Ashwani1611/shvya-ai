from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.models import User
from apps.crm.authentication import crm_login_required
from apps.teams.models import Team, TeamMembership
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Pipeline
from services.channels.hosted_whatsapp_service import (
    normalize_whatsapp_number,
    pipeline_whatsapp_number,
)
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


def _can_manage(user):
    return user.is_superuser or user.role in (
        User.Role.SUPERADMIN,
        User.Role.ADMIN,
    )


@crm_login_required
def team_list_view(request):
    """
    "Teams" page -- currently a flat list of every user in the org
    (matches the org-members-with-per-agent-settings pattern the
    product wants), not the Team/TeamMembership grouping model.
    """
    user = request.crm_user

    members = User.objects.filter(
        organization=user.organization,
    ).order_by("name")

    return render(
        request,
        "teams/team_list.html",
        {
            "members": members,
            "can_manage": _can_manage(user),
        },
    )


@crm_login_required
def team_member_automation_settings_view(request, user_id):
    """Open automation controls for the member's verified pipeline number.

    A Team member can own more than one pipeline, but this action intentionally
    opens a settings panel only when exactly one active pipeline has a connected
    WhatsApp number.  That prevents an administrator from changing automation
    on an unrelated number.
    """
    admin = request.crm_user
    if not _can_manage(admin):
        messages.error(request, "Only organization admins can manage lead automation.")
        return redirect("crm-teams")

    member = get_object_or_404(
        User,
        id=user_id,
        organization=admin.organization,
        is_active=True,
    )
    pipelines = Pipeline.objects.filter(
        organization=admin.organization,
        owner=member,
        is_active=True,
    )
    pipeline_numbers = {
        pipeline_whatsapp_number(pipeline)
        for pipeline in pipelines
        if pipeline_whatsapp_number(pipeline)
    }
    matching_accounts = []
    for account in WhatsAppAccount.objects.filter(
        organization=admin.organization,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).order_by("-updated_at"):
        number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        if number and number in pipeline_numbers:
            matching_accounts.append(account)

    if not matching_accounts:
        messages.error(
            request,
            f"{member.name or member.email} has no active pipeline with a connected WhatsApp number.",
        )
        return redirect("crm-teams")

    if len(matching_accounts) > 1:
        account_rows = []
        for account in matching_accounts:
            is_hosted = account.connection_type == WhatsAppAccount.ConnectionType.coexisted
            account_rows.append(
                {
                    "account": account,
                    "is_hosted": is_hosted,
                    "settings_url": (
                        f"{reverse('whatsapp-connect-hosted')}?settings={account.id}"
                        if is_hosted
                        else f"{reverse('whatsapp-accounts')}?owner={member.id}&settings={account.id}"
                    ),
                }
            )
        return render(
            request,
            "teams/member_automation_accounts.html",
            {"member": member, "account_rows": account_rows},
        )

    account = matching_accounts[0]
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return redirect(
            f"{reverse('whatsapp-connect-hosted')}?settings={account.id}"
        )
    return redirect(
        f"{reverse('whatsapp-accounts')}?owner={member.id}&settings={account.id}"
    )


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

