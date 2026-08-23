NAV_ITEMS = [
    {"label": "Co-Pilot", "icon": "ti-user-star"},
    {"label": "Auto Follow-ups", "icon": "ti-camera-plus"},
    {"label": "Knowledge Base", "icon": "ti-clipboard-list"},
    {"label": "Smart Triggers", "icon": "ti-target-arrow"},
    {"label": "Analytics", "icon": "ti-chart-line"},
    {"label": "WhatsApp", "icon": "ti-brand-whatsapp"},
    {"label": "Instagram", "icon": "ti-brand-instagram"},
    {"label": "Integrations Hub", "icon": "ti-plug-connected"},
    {"label": "Call Scheduler", "icon": "ti-phone-plus"},
    {"label": "Call Tracker", "icon": "ti-phone-check"},
    {"label": "Teams", "icon": "ti-users-group"},
]


def sidebar_nav(request):
    return {
        "nav_items": NAV_ITEMS,
    }