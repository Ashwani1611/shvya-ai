import json

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations.diagnostic_auth import (
    request_fingerprint,
    sanitize_text,
)
from apps.integrations.operations_auth import (
    end_support_context_record,
    operations_grant_status,
    revoke_token_record,
)
from apps.integrations.operations_models import (
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthToken,
    OperationsPolicy,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    ALL_CAPABILITIES,
    DEFAULT_APPROVAL_REQUIRED,
    DEFAULT_ORG_CAPABILITIES,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    WRITE_CAPABILITIES,
)
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from apps.superadmin.views_flat import superuser_required


@superuser_required
def operations_mcp_workspace_view(request):
    """Global Superadmin workspace for the single SHVYA Operations MCP."""

    now = timezone.now()
    operations_mcp_url = request.build_absolute_uri(
        reverse("shvya-operations-mcp")
    )
    oauth_authorize_url = request.build_absolute_uri(
        reverse("shvya-operations-oauth-authorize")
    )
    resource_metadata_url = request.build_absolute_uri(
        reverse("shvya-operations-oauth-resource-metadata")
    )
    server_metadata_url = request.build_absolute_uri(
        reverse("shvya-operations-oauth-server-metadata-rfc8414")
    )

    superadmin_tokens = list(
        OperationsOAuthToken.objects.filter(
            role=ROLE_SUPERADMIN,
            revoked_at__isnull=True,
            refresh_expires_at__gt=now,
        )
        .select_related(
            "actor",
            "client",
            "active_organization",
        )
        .order_by("-last_used_at", "-created_at")
    )
    for token in superadmin_tokens:
        (
            token.live_authority_valid,
            token.live_authority_reason_code,
            token.live_authority_message,
        ) = operations_grant_status(token)

    open_support_sessions = list(
        OperationsSupportSession.objects.filter(
            token__role=ROLE_SUPERADMIN,
            token__revoked_at__isnull=True,
            token__refresh_expires_at__gt=now,
            ended_at__isnull=True,
        )
        .select_related(
            "organization",
            "actor",
            "token__client",
        )
        .order_by("-last_seen_at", "-started_at")
    )
    for support_session in open_support_sessions:
        support_session.safe_reason = sanitize_text(
            support_session.reason,
            limit=500,
        )

    policies = list(
        OperationsPolicy.objects.select_related(
            "organization",
        ).order_by("organization__name")
    )
    enabled_policy_count = sum(
        1
        for policy in policies
        if policy.organization_admin_enabled
    )
    active_org_admin_grants = OperationsOAuthToken.objects.filter(
        role=ROLE_ORGANIZATION_ADMIN,
        revoked_at__isnull=True,
        refresh_expires_at__gt=now,
    ).count()

    platform_audit_events = list(
        OperationsAuditEvent.objects.filter(
            organization__isnull=True,
        )
        .select_related("actor")
        .order_by("-created_at")[:30]
    )

    vscode_configuration = json.dumps(
        {
            "servers": {
                "shvya-superadmin": {
                    "type": "http",
                    "url": operations_mcp_url,
                }
            }
        },
        indent=2,
    )

    return render(
        request,
        "superadmin/mcp_workspace.html",
        {
            "operations_mcp_url": operations_mcp_url,
            "oauth_authorize_url": oauth_authorize_url,
            "resource_metadata_url": resource_metadata_url,
            "server_metadata_url": server_metadata_url,
            "vscode_configuration": vscode_configuration,
            "superadmin_tokens": superadmin_tokens,
            "open_support_sessions": open_support_sessions,
            "operations_policies": policies,
            "enabled_policy_count": enabled_policy_count,
            "active_org_admin_grants": active_org_admin_grants,
            "platform_audit_events": platform_audit_events,
            "organization_count": Organization.objects.count(),
        },
    )


@superuser_required
@require_POST
def operations_mcp_superadmin_session_revoke_view(
    request,
    token_id,
):
    token = get_object_or_404(
        OperationsOAuthToken.objects.select_related(
            "actor",
            "client",
            "active_organization",
        ),
        pk=token_id,
        role=ROLE_SUPERADMIN,
        revoked_at__isnull=True,
    )
    organization = token.active_organization
    support_session = (
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        )
        .order_by("-started_at")
        .first()
    )
    revoked = revoke_token_record(token=token)
    if revoked is not None:
        OperationsAuditEvent.objects.create(
            actor=request.user,
            role=ROLE_SUPERADMIN,
            organization=organization,
            support_session=support_session,
            tool_name="oauth_revoke_superadmin_workspace",
            capability="",
            target_type="oauth_grant",
            target_id=str(token.id),
            reason="SHVYA Superadmin revoked a Superadmin External AI Operations session.",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint=request_fingerprint(
                {
                    "event": "oauth_revoke_superadmin_workspace",
                    "token_id": str(token.id),
                }
            ),
            change_summary={
                "client_name": sanitize_text(
                    token.client.client_name or "External AI",
                    limit=120,
                ),
                "session_owner": sanitize_text(
                    token.actor.name or token.actor.email,
                    limit=120,
                ),
                "access": "revoked",
                "support_context_closed": support_session is not None,
            },
            duration_ms=0,
            error_code="",
        )
        messages.success(
            request,
            "Superadmin External AI Operations session revoked.",
        )
    return redirect("superadmin-operations-mcp")


@superuser_required
@require_POST
def operations_mcp_support_end_view(
    request,
    session_id,
):
    support_session = get_object_or_404(
        OperationsSupportSession.objects.select_related(
            "organization",
            "actor",
            "token__client",
        ),
        pk=session_id,
        ended_at__isnull=True,
        token__role=ROLE_SUPERADMIN,
    )
    organization = support_session.organization
    ended_count = end_support_context_record(
        token=support_session.token,
        organization=organization,
    )
    if ended_count:
        support_session.refresh_from_db()
        _record_superadmin_operations_event(
            actor=request.user,
            organization=organization,
            tool_name="support_context_force_end",
            target_type="support_session",
            target_id=support_session.id,
            reason=(
                "SHVYA Superadmin force-ended an active External AI "
                "support context from the global MCP workspace."
            ),
            change_summary={
                "support_actor": sanitize_text(
                    support_session.actor.name or "SHVYA Support",
                    limit=120,
                ),
                "client_name": sanitize_text(
                    support_session.token.client.client_name or "External AI",
                    limit=120,
                ),
                "support_context": "ended",
            },
            support_session=support_session,
        )
        messages.success(
            request,
            "SHVYA Support Operations context ended.",
        )
    return redirect("superadmin-operations-mcp")


def _record_superadmin_operations_event(
    *,
    actor,
    organization,
    tool_name,
    target_type,
    target_id,
    reason,
    change_summary,
    support_session=None,
):
    return OperationsAuditEvent.objects.create(
        actor=actor,
        role=ROLE_SUPERADMIN,
        organization=organization,
        support_session=support_session,
        tool_name=tool_name[:100],
        capability="",
        target_type=target_type[:80],
        target_id=str(target_id)[:100],
        reason=sanitize_text(reason, limit=500),
        outcome=OperationsAuditEvent.Outcome.SUCCESS,
        request_fingerprint=request_fingerprint(
            {
                "event": tool_name,
                "actor_id": str(actor.id),
                "organization_id": str(organization.id),
                "target_type": target_type,
                "target_id": str(target_id),
            }
        ),
        change_summary=change_summary,
        duration_ms=0,
        error_code="",
    )


@superuser_required
@require_POST
def organization_operations_policy_update_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    policy, _ = OperationsPolicy.objects.get_or_create(
        organization=organization,
        defaults={
            "allowed_capabilities": DEFAULT_ORG_CAPABILITIES,
            "approval_required_capabilities": DEFAULT_APPROVAL_REQUIRED,
        },
    )

    enabled = request.POST.get("organization_admin_enabled") == "on"
    requested_allowed = set(
        request.POST.getlist("allowed_capabilities")
    )
    allowed = [
        capability
        for capability in ALL_CAPABILITIES
        if capability in requested_allowed
    ]
    requested_approval = set(
        request.POST.getlist(
            "approval_required_capabilities"
        )
    )
    approval = [
        capability
        for capability in WRITE_CAPABILITIES
        if (
            capability in allowed
            and capability in requested_approval
        )
    ]

    revoked_token_count = 0
    expired_code_count = 0
    with transaction.atomic():
        policy.organization_admin_enabled = enabled
        policy.allowed_capabilities = allowed
        policy.approval_required_capabilities = approval
        policy.updated_by = request.user
        policy.save(
            update_fields=[
                "organization_admin_enabled",
                "allowed_capabilities",
                "approval_required_capabilities",
                "updated_by",
                "updated_at",
            ]
        )

        if not enabled:
            now = timezone.now()
            revoked_token_count = OperationsOAuthToken.objects.filter(
                organization=organization,
                role=ROLE_ORGANIZATION_ADMIN,
                revoked_at__isnull=True,
            ).update(
                revoked_at=now,
                updated_at=now,
            )
            expired_code_count = (
                OperationsOAuthAuthorizationCode.objects.filter(
                    organization=organization,
                    role=ROLE_ORGANIZATION_ADMIN,
                    used_at__isnull=True,
                    expires_at__gt=now,
                ).update(
                    expires_at=now,
                )
            )

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        changed_fields=[
            "operations_mcp.organization_admin_enabled",
            "operations_mcp.allowed_capabilities",
            "operations_mcp.approval_required_capabilities",
        ],
        operations_mcp_enabled=enabled,
        operations_mcp_capability_count=len(allowed),
        operations_mcp_revoked_token_count=revoked_token_count,
        operations_mcp_expired_authorization_code_count=expired_code_count,
    )
    _record_superadmin_operations_event(
        actor=request.user,
        organization=organization,
        tool_name="operations_policy_update",
        target_type="organization",
        target_id=organization.id,
        reason="SHVYA Superadmin updated External AI Operations policy.",
        change_summary={
            "organization_admin_enabled": enabled,
            "allowed_capabilities": sorted(allowed),
            "approval_required_capabilities": sorted(approval),
            "revoked_token_count": revoked_token_count,
            "expired_authorization_code_count": expired_code_count,
        },
    )

    message = "External AI Operations policy updated for this organization."
    if revoked_token_count or expired_code_count:
        message += (
            f" Revoked {revoked_token_count} active Organization Admin "
            f"Operations session(s) and expired {expired_code_count} pending "
            "authorization code(s); fresh authorization is required to reconnect."
        )
    messages.success(request, message)
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )



@superuser_required
@require_POST
def organization_operations_session_revoke_view(
    request,
    organization_id,
    token_id,
):
    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )
    token = get_object_or_404(
        OperationsOAuthToken.objects.select_related(
            "actor",
            "client",
        ),
        pk=token_id,
        organization=organization,
        role=ROLE_ORGANIZATION_ADMIN,
        revoked_at__isnull=True,
    )
    revoked = revoke_token_record(token=token)
    if revoked is not None:
        _record_superadmin_operations_event(
            actor=request.user,
            organization=organization,
            tool_name="oauth_revoke_superadmin_dashboard",
            target_type="oauth_grant",
            target_id=token.id,
            reason=(
                "SHVYA Superadmin revoked an Organization Admin "
                "External AI Operations session."
            ),
            change_summary={
                "client_name": sanitize_text(
                    token.client.client_name or "External AI",
                    limit=120,
                ),
                "session_owner": sanitize_text(
                    token.actor.name or "Organization Admin",
                    limit=120,
                ),
                "scopes": sorted(set(str(token.scope or "").split())),
                "access": "revoked",
            },
        )
        messages.success(
            request,
            "External AI Operations session revoked for this organization.",
        )
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


@superuser_required
@require_POST
def organization_operations_support_end_view(
    request,
    organization_id,
    session_id,
):
    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )
    support_session = get_object_or_404(
        OperationsSupportSession.objects.select_related(
            "actor",
            "token__client",
        ),
        pk=session_id,
        organization=organization,
        ended_at__isnull=True,
        token__role=ROLE_SUPERADMIN,
    )
    ended_count = end_support_context_record(
        token=support_session.token,
        organization=organization,
    )
    if ended_count:
        support_session.refresh_from_db()
        _record_superadmin_operations_event(
            actor=request.user,
            organization=organization,
            tool_name="support_context_force_end",
            target_type="support_session",
            target_id=support_session.id,
            reason=(
                "SHVYA Superadmin force-ended an active External AI "
                "support context from the Superadmin dashboard."
            ),
            change_summary={
                "support_actor": sanitize_text(
                    support_session.actor.name or "SHVYA Support",
                    limit=120,
                ),
                "client_name": sanitize_text(
                    support_session.token.client.client_name or "External AI",
                    limit=120,
                ),
                "support_context": "ended",
            },
            support_session=support_session,
        )
        messages.success(
            request,
            "SHVYA Support Operations context ended for this organization.",
        )
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )
