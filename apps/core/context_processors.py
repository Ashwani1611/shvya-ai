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
        "label": "WhatsApp",
        "icon": "ti-brand-whatsapp",
        "url_name": "whatsapp-chats",
        "path_prefix": "/dashboard/whatsapp/",
    },
    {"label": "Instagram", "icon": "ti-brand-instagram"},
    {"label": "Integrations Hub", "icon": "ti-plug-connected"},
    {"label": "Call Scheduler", "icon": "ti-phone-plus"},
    {"label": "Call Tracker", "icon": "ti-phone-check"},
    {"label": "Teams", "icon": "ti-users-group"},
]


def sidebar_nav(request):
    """
    Shared sidebar nav items. Items with a "url_name" render as a
    real clickable link (base.html); items without one stay
    disabled "Coming soon" placeholders.

    "is_active" is computed here (not in the template) since it
    needs request.path, which a context processor has direct
    access to. Two matching modes:

      - "path_exact":  active only when request.path is exactly
                        this value. Used for CRM, since its prefix
                        ("/dashboard/") would otherwise also match
                        every other module's pages.
      - "path_prefix":  active when request.path starts with this
                        value. Used for modules with their own
                        sub-tree, e.g. WhatsApp's chats/templates/
                        campaigns pages all live under
                        /dashboard/whatsapp/.
    """
    nav_items = []

    for item in NAV_ITEMS:
        entry = dict(item)

        path_exact = entry.get("path_exact")
        path_prefix = entry.get("path_prefix")

        if path_exact:
            entry["is_active"] = request.path == path_exact
        else:
            entry["is_active"] = bool(
                path_prefix and request.path.startswith(path_prefix)
            )

        nav_items.append(entry)

    return {
        "nav_items": nav_items,
    }