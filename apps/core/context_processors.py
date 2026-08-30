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
                "label": "Chats",
                "icon": "ti-message-circle",
                "url_name": "whatsapp-chats",
                "path_prefix": "/dashboard/whatsapp/chats/",
            },
            {
                "label": "Templates",
                "icon": "ti-file-text",
                "url_name": "whatsapp-template-list",
                "path_prefix": "/dashboard/whatsapp/templates/",
            },
            {
                "label": "Broadcasts",
                "icon": "ti-speakerphone",
                "url_name": "whatsapp-campaign-list",
                "path_prefix": "/dashboard/whatsapp/campaigns/",
            },
            {
                # TODO: confirm the real url_name + path for Hosted
                # Accounts (templates/channels/whatsapp_connect_hosted.html
                # already exists, so this view likely already exists too —
                # run: grep -n "hosted" apps/channels/urls.py
                "label": "Hosted Accounts",
                "icon": "ti-server",
                "url_name": "whatsapp-connect-hosted",
                "path_prefix": "/dashboard/whatsapp/hosted/",
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
    """
    True when this entry's own configured path matches the current
    request -- "path_exact" for a single exact URL (CRM's dashboard
    root, which would otherwise also match every other module since
    everything lives under /dashboard/), "path_prefix" for anything
    that owns a whole sub-tree of pages (e.g. WhatsApp's chats/
    templates/campaigns all under /dashboard/whatsapp/...).
    """
    path_exact = entry.get("path_exact")
    path_prefix = entry.get("path_prefix")

    if path_exact:
        return request_path == path_exact

    return bool(path_prefix and request_path.startswith(path_prefix))


def sidebar_nav(request):
    """
    Shared sidebar nav items. Items with a "url_name" render as a
    real clickable link (base.html); items without one, and without
    "children" either, stay disabled "Coming soon" placeholders.

    Items with "children" render as an expandable/collapsible group
    (e.g. WhatsApp) instead of a direct link -- "is_active" on the
    group itself means "a child route is currently open", which
    base.html uses to auto-expand the group on load so the active
    page's parent section isn't shown collapsed.
    """
    nav_items = []

    for item in NAV_ITEMS:
        entry = dict(item)

        children = entry.get("children")

        if children:
            resolved_children = []

            for child in children:
                child_entry = dict(child)
                child_entry["is_active"] = _resolve_active(
                    child_entry, request.path
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
    }
