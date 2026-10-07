import json
import secrets
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations.diagnostic_auth import (
    request_fingerprint,
    sanitize_text,
)
from apps.integrations.operations_auth import (
    CLAUDE_BROWSER_CLIENT_ID,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    end_support_context_record,
    operations_grant_status,
    revoke_token_record,
    token_hash,
)
from apps.integrations.operations_endpoints import (
    PRODUCTION_OPERATIONS_ORIGIN,
    operations_authorization_url,
    operations_issuer,
    operations_registration_url,
    operations_resource,
    operations_resource_metadata_url,
    operations_revocation_url,
    operations_token_url,
)
from apps.integrations.operations_models import (
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthClient,
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
    SUPERADMIN_ONLY_CAPABILITIES,
    ALWAYS_APPROVAL_CAPABILITIES,
    WRITE_CAPABILITIES,
    capabilities_for_grant,
)
from apps.integrations.operations_self_test import run_operations_self_test
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from apps.superadmin.views_flat import superuser_required


@superuser_required
def operations_mcp_workspace_view(request):
    """Global Superadmin workspace for the single SHVYA Operations MCP."""

    now = timezone.now()
    operations_mcp_url = operations_resource()
    oauth_issuer_url = operations_issuer()
    oauth_authorize_url = operations_authorization_url()
    oauth_token_url = operations_token_url()
    oauth_registration_url = operations_registration_url()
    oauth_revocation_url = operations_revocation_url()
    resource_metadata_url = operations_resource_metadata_url()
    server_metadata_url = (
        operations_issuer()
        + "/.well-known/oauth-authorization-server"
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
        token.is_direct_key = str(token.client.client_id).startswith(
            "shvya_key_"
        )
    direct_key_tokens = [
        token for token in superadmin_tokens if token.is_direct_key
    ]
    oauth_grant_tokens = [
        token for token in superadmin_tokens if not token.is_direct_key
    ]

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
    cursor_configuration = json.dumps(
        {
            "mcpServers": {
                "shvya-superadmin": {
                    "url": operations_mcp_url,
                }
            }
        },
        indent=2,
    )
    gemini_configuration = json.dumps(
        {
            "mcpServers": {
                "shvya-superadmin": {
                    "httpUrl": operations_mcp_url,
                }
            }
        },
        indent=2,
    )
    codex_configuration = (
        "[mcp_servers.shvya-superadmin]\n"
        f'url = "{operations_mcp_url}"'
    )
    claude_code_command = (
        "claude mcp add --transport http "
        f"shvya-superadmin {operations_mcp_url}"
    )

    self_test = run_operations_self_test()
    client_compatibility = [
        {
            "name": "ChatGPT",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth 2.1 · CIMD/DCR",
            "callback": "https://chatgpt.com/connector_platform_oauth_redirect",
            "configuration": "Universal production MCP URL",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Claude",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth 2.1 · CIMD",
            "callback": "https://claude.ai/api/mcp/auth_callback",
            "configuration": "Production MCP URL + public OAuth",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Codex",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth or direct key",
            "callback": "Client-managed OAuth callback",
            "configuration": "config.toml example below",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "VS Code",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth or direct key",
            "callback": "HTTPS or RFC 8252 loopback",
            "configuration": "mcp.json example below",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Cursor",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth or direct key",
            "callback": "HTTPS or RFC 8252 loopback",
            "configuration": "mcp.json example below",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Claude Code",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth or direct key",
            "callback": "Client-managed OAuth callback",
            "configuration": "CLI example below",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Gemini CLI",
            "protocol": "Streamable HTTP MCP",
            "authentication": "Direct key",
            "callback": "Not required for direct key",
            "configuration": "settings.json example below",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
        {
            "name": "Windsurf",
            "protocol": "Streamable HTTP MCP",
            "authentication": "OAuth or direct key",
            "callback": "HTTPS or RFC 8252 loopback",
            "configuration": "Remote MCP + Bearer header",
            "verification": "CODE VERIFIED",
            "live_verification": "NOT LIVE VERIFIED",
        },
    ]

    return render(
        request,
        "superadmin/mcp_workspace.html",
        {
            "operations_mcp_url": operations_mcp_url,
            "operations_environment": (
                "Production"
                if operations_mcp_url.startswith(
                    PRODUCTION_OPERATIONS_ORIGIN
                )
                else "Local test"
            ),
            "oauth_issuer_url": oauth_issuer_url,
            "claude_browser_client_id": CLAUDE_BROWSER_CLIENT_ID,
            "oauth_authorize_url": oauth_authorize_url,
            "oauth_token_url": oauth_token_url,
            "oauth_registration_url": oauth_registration_url,
            "oauth_revocation_url": oauth_revocation_url,
            "resource_metadata_url": resource_metadata_url,
            "server_metadata_url": server_metadata_url,
            "supported_protocol_versions": [
                "2026-07-28",
                "2025-11-25",
            ],
            "supported_scopes": [
                "operations.read",
                "operations.write",
                "offline_access",
            ],
            "self_test": self_test,
            "client_compatibility": client_compatibility,
            "vscode_configuration": vscode_configuration,
            "cursor_configuration": cursor_configuration,
            "gemini_configuration": gemini_configuration,
            "codex_configuration": codex_configuration,
            "claude_code_command": claude_code_command,
            "superadmin_tokens": superadmin_tokens,
            "direct_key_tokens": direct_key_tokens,
            "oauth_grant_tokens": oauth_grant_tokens,
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
def operations_mcp_access_key_generate_view(request):
    """Issue a revocable Superadmin MCP bearer key without an OAuth redirect."""

    now = timezone.now()
    active_direct_keys = OperationsOAuthToken.objects.filter(
        actor=request.user,
        role=ROLE_SUPERADMIN,
        client__client_id__startswith="shvya_key_",
        revoked_at__isnull=True,
        refresh_expires_at__gt=now,
    ).count()
    if active_direct_keys >= 10:
        response = JsonResponse(
            {
                "ok": False,
                "error": (
                    "Maximum active direct MCP keys reached. "
                    "Revoke an unused key before generating another."
                ),
            },
            status=409,
        )
        response["Cache-Control"] = "no-store"
        return response

    label = sanitize_text(
        request.POST.get("label") or "Superadmin MCP Key",
        limit=80,
    ).strip()
    if not label:
        label = "Superadmin MCP Key"

    access_mode = str(
        request.POST.get("access_mode") or "read_write"
    ).strip()
    if access_mode not in {"read_only", "read_write"}:
        return JsonResponse(
            {"ok": False, "error": "Unsupported MCP key access mode."},
            status=400,
        )

    try:
        ttl_days = int(request.POST.get("ttl_days") or "30")
    except (TypeError, ValueError):
        ttl_days = 0
    if ttl_days not in {7, 30, 90}:
        return JsonResponse(
            {
                "ok": False,
                "error": "MCP key lifetime must be 7, 30, or 90 days.",
            },
            status=400,
        )

    allow_writes = access_mode == "read_write"
    scopes = [OPERATIONS_READ_SCOPE]
    if allow_writes:
        scopes.append(OPERATIONS_WRITE_SCOPE)

    raw_key = "shvya_mcp_" + secrets.token_urlsafe(48)
    disabled_refresh_secret = (
        "direct-key-refresh-disabled:" + secrets.token_urlsafe(48)
    )
    expires_at = now + timedelta(days=ttl_days)
    operations_mcp_url = operations_resource()

    with transaction.atomic():
        client = OperationsOAuthClient.objects.create(
            client_id="shvya_key_" + secrets.token_urlsafe(24),
            client_name=("Direct key · " + label)[:200],
            application_type="native",
            redirect_uris=[],
            grant_types=[],
            response_types=[],
        )
        token = OperationsOAuthToken.objects.create(
            client=client,
            actor=request.user,
            organization=None,
            active_organization=None,
            role=ROLE_SUPERADMIN,
            access_token_hash=token_hash(raw_key),
            refresh_token_hash=token_hash(disabled_refresh_secret),
            scope=" ".join(scopes),
            granted_capabilities=sorted(
                capabilities_for_grant(
                    role=ROLE_SUPERADMIN,
                    organization=None,
                    allow_writes=allow_writes,
                )
            ),
            resource=operations_mcp_url,
            expires_at=expires_at,
            refresh_expires_at=expires_at,
        )
        OperationsAuditEvent.objects.create(
            actor=request.user,
            role=ROLE_SUPERADMIN,
            organization=None,
            support_session=None,
            tool_name="mcp_key_generate",
            capability="",
            target_type="mcp_access_key",
            target_id=str(token.id),
            reason=(
                "SHVYA Superadmin generated a direct MCP access key "
                "from the Superadmin workspace."
            ),
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint=request_fingerprint(
                {
                    "event": "mcp_key_generate",
                    "token_id": str(token.id),
                    "access_mode": access_mode,
                    "ttl_days": ttl_days,
                }
            ),
            change_summary={
                "label": label,
                "access": (
                    "read_write" if allow_writes else "read_only"
                ),
                "expires_at": expires_at.isoformat(),
            },
            duration_ms=0,
            error_code="",
        )

    bearer_header = "Bearer " + raw_key
    configurations = {
        "vscode": json.dumps(
            {
                "servers": {
                    "shvya-superadmin": {
                        "type": "http",
                        "url": operations_mcp_url,
                        "headers": {
                            "Authorization": bearer_header,
                        },
                    }
                }
            },
            indent=2,
        ),
        "cursor": json.dumps(
            {
                "mcpServers": {
                    "shvya-superadmin": {
                        "url": operations_mcp_url,
                        "headers": {
                            "Authorization": bearer_header,
                        },
                    }
                }
            },
            indent=2,
        ),
        "gemini": json.dumps(
            {
                "mcpServers": {
                    "shvya-superadmin": {
                        "httpUrl": operations_mcp_url,
                        "headers": {
                            "Authorization": bearer_header,
                        },
                    }
                }
            },
            indent=2,
        ),
        "claude_code": (
            "claude mcp add --transport http "
            f"shvya-superadmin {operations_mcp_url} "
            f'--header "Authorization: {bearer_header}"'
        ),
        "codex": (
            "[mcp_servers.shvya-superadmin]\n"
            f'url = "{operations_mcp_url}"\n'
            "http_headers = { Authorization = "
            f'"{bearer_header}" }}'
        ),
    }

    response = JsonResponse(
        {
            "ok": True,
            "key": raw_key,
            "token_id": str(token.id),
            "label": label,
            "access_mode": access_mode,
            "expires_at": expires_at.isoformat(),
            "endpoint": operations_mcp_url,
            "authorization_header": bearer_header,
            "configurations": configurations,
        }
    )
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    return response


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
        if capability in requested_allowed and capability not in SUPERADMIN_ONLY_CAPABILITIES
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
            and (capability in requested_approval or capability in ALWAYS_APPROVAL_CAPABILITIES)
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
