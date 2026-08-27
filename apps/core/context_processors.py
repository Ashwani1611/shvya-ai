NAV_ITEMS = [
    {
        "label": "CRM",
        "icon": "ti-users",
        "url_name": "crm-dashboard",
        "path_exact": "/dashboard/",
    },
    {"label": "Co-Pilot", "icon": "ti-user-star"},
    {"label": "Auto Follow-ups", "icon": "ti-camera-plus"},
    {"label": "Knowledge Base", "icon": "ti-clipboard-list"},
    {"label": "Smart Triggers", "icon": "ti-target-arrow"},
    {"label": "Analytics", "icon": "ti-chart-line"},
    {
        # A "group" item has no url_name of its own -- clicking it
        # expands/collapses its "children" instead of navigating.
        # is_active (and therefore auto-expanded) when ANY child's
        # own path matches, computed below from path_prefix.
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
                "label": "Campaigns",
                "icon": "ti-speakerphone",
                "url_name": "whatsapp-campaign-list",
                "path_prefix": "/dashboard/whatsapp/campaigns/",
            },
        ],
    },
    {
        "label": "Instagram",
        "icon": "ti-brand-instagram",
        # No children built yet -- Instagram has no views/urls of its
        # own in apps.channels yet, so this stays a flat "coming
        # soon" placeholder (no url_name) until that work exists.
    },
    {"label": "Integrations Hub", "icon": "ti-plug-connected"},
    {"label": "Call Scheduler", "icon": "ti-phone-plus"},
    {"label": "Call Tracker", "icon": "ti-phone-check"},
    {"label": "Teams", "icon": "ti-users-group"},
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