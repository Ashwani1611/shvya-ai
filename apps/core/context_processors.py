from apps.ai_engagement.coins import credits_to_coins


AI_CREDIT_ALERT_THRESHOLD = 500


NAV_ITEMS = [
    {
        "label": "CRM",
        "icon": "ti-users",
        "url_name": "crm-dashboard",
        "path_exact": "/dashboard/",
    },
    {
        "label": "Co-Pilot",
        "icon": "ti-user-star",
        "url_name": "crm-copilot",
        "path_exact": "/dashboard/copilot/",
    },
    {
        "label": "Auto Follow-ups",
        "icon": "ti-camera-plus",
        "path_prefix": "/dashboard/auto-follow-ups/",
        "children": [
            {
                "label": "Sequences",
                "icon": "ti-repeat",
                "url_name": "crm-auto-follow-ups-sequences",
                "path_prefix": "/dashboard/auto-follow-ups/sequences/",
            },
            {
                "label": "Workflows",
                "icon": "ti-git-branch",
                "url_name": "crm-auto-follow-ups-workflows",
                "path_prefix": "/dashboard/auto-follow-ups/workflows/",
            },
        ],
    },
    {
        "label": "Knowledge Base",
        "icon": "ti-clipboard-list",
        "path_prefix": "/dashboard/knowledge-base/",
        "children": [
            {
                "label": "AI Setup",
                "icon": "ti-settings",
                "url_name": "crm-knowledge-base-ai-setup",
                "path_prefix": "/dashboard/knowledge-base/ai-setup/",
            },
            {
                "label": "FAQ",
                "icon": "ti-help-circle",
                "url_name": "crm-knowledge-base-faq",
                "path_prefix": "/dashboard/knowledge-base/faq/",
            },
        ],
    },
    {
        "label": "Smart Triggers",
        "icon": "ti-target-arrow",
        "url_name": "crm-smart-triggers",
        "path_exact": "/dashboard/smart-triggers/",
    },
    {
        "label": "Analytics",
        "icon": "ti-chart-line",
        "url_name": "crm-analytics",
        "path_exact": "/dashboard/analytics/",
    },
    {
        "label": "WhatsApp",
        "icon": "ti-brand-whatsapp",
        "path_prefix": "/dashboard/whatsapp/",
        "children": [
            {
                "label": "Connect API",
                "icon": "ti-plug-connected",
                "url_name": "whatsapp-connect-api",
                "path_prefix": "/dashboard/whatsapp/connect/api/",
                "hide_when_whatsapp_connected": True,
            },
            {
                "label": "Connected Numbers",
                "icon": "ti-device-mobile-check",
                "url_name": "whatsapp-accounts",
                "path_prefix": "/dashboard/whatsapp/accounts/",
                "requires_whatsapp_connection": True,
            },
            {
                "label": "Chats",
                "icon": "ti-message-circle",
                "url_name": "whatsapp-chats",
                "path_prefix": "/dashboard/whatsapp/chats/",
                "requires_whatsapp_connection": True,
            },
            {
                "label": "Templates",
                "icon": "ti-file-text",
                "url_name": "whatsapp-template-list",
                "path_prefix": "/dashboard/whatsapp/templates/",
                "requires_whatsapp_connection": True,
            },
            {
                "label": "Broadcasts",
                "icon": "ti-speakerphone",
                "url_name": "whatsapp-campaign-list",
                "path_prefix": "/dashboard/whatsapp/campaigns/",
                "requires_whatsapp_connection": True,
            },
            {
                "label": "Hosted Account",
                "icon": "ti-server",
                "url_name": "whatsapp-connect-hosted",
                "path_prefix": "/dashboard/whatsapp/connect/hosted/",
                "requires_hosted_account_enabled": True,
            },
        ],
    },
    {
        "label": "Instagram",
        "icon": "ti-brand-instagram",
        "path_prefix": "/dashboard/instagram/",
        "children": [
            {
                "label": "Connect Instagram",
                "icon": "ti-plug-connected",
                "url_name": "crm-instagram-connect",
                "path_prefix": "/dashboard/instagram/connect/",
            },
            {
                "label": "Chats",
                "icon": "ti-message-circle",
                "url_name": "crm-instagram-chats",
                "path_prefix": "/dashboard/instagram/chats/",
            },
        ],
    },
    {
        "label": "Connect Hub",
        "icon": "ti-plug-connected",
        "url_name": "crm-connect-hub",
        "path_prefix": "/dashboard/connect-hub/",
    },
    {
        "label": "Call Scheduler",
        "icon": "ti-phone-plus",
        "url_name": "crm-call-scheduler",
        "path_exact": "/dashboard/call-scheduler/",
    },
    {
        "label": "Call Tracker",
        "icon": "ti-phone-check",
        "url_name": "crm-call-tracker",
        "path_exact": "/dashboard/call-tracker/",
    },
    {
        "label": "Teams",
        "icon": "ti-users-group",
        "url_name": "crm-teams",
        "path_exact": "/dashboard/teams/",
    },
]


def _resolve_active(entry, request_path):
    """Return whether a navigation entry owns the current request path."""
    path_exact = entry.get("path_exact")
    path_prefix = entry.get("path_prefix")

    if path_exact:
        return request_path == path_exact

    return bool(path_prefix and request_path.startswith(path_prefix))


def _has_connected_whatsapp_account(request):
    """Return True only for a usable Meta WhatsApp API connection."""
    user = getattr(request, "crm_user", None)
    if not user or not getattr(user, "organization_id", None):
        return False

    from apps.channels.models import WhatsAppAccount

    return WhatsAppAccount.objects.filter(
        organization_id=user.organization_id,
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).exists()


def _has_hosted_account_access(request):
    """Return whether Superadmin enabled Hosted Account for this organization."""
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization:
        return False

    from apps.organizations.features import is_hosted_account_enabled

    return is_hosted_account_enabled(organization)


def _pending_reminder_count(request):
    """Expose the live pending-reminder badge on every authenticated dashboard page."""
    user = getattr(request, "crm_user", None)
    if not user or not getattr(user, "organization_id", None):
        return 0

    from apps.crm.models import LeadReminder

    return LeadReminder.objects.filter(
        lead__organization_id=user.organization_id,
        status="pending",
    ).count()


def _ai_credit_context(request):
    """Expose exact credits plus the 30:1 user-facing AI coin wallet context."""
    user = getattr(request, "crm_user", None)
    organization_id = getattr(user, "organization_id", None)
    coin_alert_threshold = credits_to_coins(AI_CREDIT_ALERT_THRESHOLD)

    if not organization_id:
        return {
            "ai_credit_balance": None,
            "ai_credit_is_low": False,
            "ai_credit_alert_threshold": AI_CREDIT_ALERT_THRESHOLD,
            "ai_coin_balance": None,
            "ai_coin_total": None,
            "ai_coin_is_low": False,
            "ai_coin_alert_threshold": coin_alert_threshold,
        }

    from apps.ai_engagement.models import AICreditWallet

    wallet = (
        AICreditWallet.objects
        .filter(organization_id=organization_id)
        .only(
            "balance",
            "reserved_credits",
            "lifetime_credits_added",
        )
        .first()
    )

    available = wallet.available_credits if wallet is not None else 0
    total_funded = int(wallet.lifetime_credits_added) if wallet is not None else 0
    is_low = available <= AI_CREDIT_ALERT_THRESHOLD

    return {
        # Raw credit aliases remain for any older templates/integrations.
        "ai_credit_balance": available,
        "ai_credit_is_low": is_low,
        "ai_credit_alert_threshold": AI_CREDIT_ALERT_THRESHOLD,
        # Dashboard UI uses coins.
        "ai_coin_balance": credits_to_coins(available),
        "ai_coin_total": credits_to_coins(total_funded),
        "ai_coin_is_low": is_low,
        "ai_coin_alert_threshold": coin_alert_threshold,
    }


def sidebar_nav(request):
    """Build shared sidebar navigation with connection-aware WhatsApp items."""
    nav_items = []
    has_whatsapp_connection = _has_connected_whatsapp_account(request)
    has_hosted_account_access = _has_hosted_account_access(request)

    for item in NAV_ITEMS:
        entry = dict(item)
        children = entry.get("children")

        if children:
            resolved_children = []

            for child in children:
                if (
                    child.get("requires_whatsapp_connection")
                    and not has_whatsapp_connection
                ):
                    continue

                if (
                    child.get("hide_when_whatsapp_connected")
                    and has_whatsapp_connection
                ):
                    continue

                if (
                    child.get("requires_hosted_account_enabled")
                    and not has_hosted_account_access
                ):
                    continue

                child_entry = dict(child)
                child_entry["is_active"] = _resolve_active(
                    child_entry,
                    request.path,
                )
                resolved_children.append(child_entry)

            entry["children"] = resolved_children
            entry["is_active"] = any(
                child["is_active"] for child in resolved_children
            ) or _resolve_active(entry, request.path)

        else:
            entry["is_active"] = _resolve_active(entry, request.path)

        nav_items.append(entry)

    context = {
        "nav_items": nav_items,
        "has_whatsapp_connection": has_whatsapp_connection,
        "pending_reminder_count": _pending_reminder_count(request),
    }
    context.update(_ai_credit_context(request))
    return context
