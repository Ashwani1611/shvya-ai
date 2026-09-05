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
                "url_name": "whatsapp-accounts",
                "path_prefix": "/dashboard/whatsapp/accounts/",
            },
            {
                "label": "Coexisted Accounts",
                "icon": "ti-server",
                "url_name": "whatsapp-connect-hosted",
                "path_prefix": "/dashboard/whatsapp/connect/hosted/",
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
        "label": "Integrations Hub",
        "icon": "ti-plug-connected",
        "url_name": "crm-integrations-hub",
        "path_exact": "/dashboard/integrations-hub/",
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
    """Keep post-connection WhatsApp tools out of the sidebar until usable."""
    user = getattr(request, "crm_user", None)
    if not user or not getattr(user, "organization_id", None):
        return False

    from apps.channels.models import WhatsAppAccount

    return WhatsAppAccount.objects.filter(
        organization_id=user.organization_id,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).exists()


def sidebar_nav(request):
    """Build shared sidebar navigation with connection-aware WhatsApp items."""
    nav_items = []
    has_whatsapp_connection = _has_connected_whatsapp_account(request)

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

    return {
        "nav_items": nav_items,
        "has_whatsapp_connection": has_whatsapp_connection,
    }
