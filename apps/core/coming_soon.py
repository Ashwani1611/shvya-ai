from django.shortcuts import render


COMING_SOON_FEATURES = {
    "copilot": {
        "label": "Co-Pilot",
        "icon": "ti-robot",
        "description": "AI-assisted replies and suggestions, right inside your conversations.",
    },
    "auto-follow-ups": {
        "label": "Auto Follow-ups",
        "icon": "ti-repeat",
        "description": "Automatically nudge leads that have gone quiet, on your schedule.",
    },
    "knowledge-base": {
        "label": "Knowledge Base",
        "icon": "ti-book",
        "description": "A searchable library your team (and your AI) can pull answers from.",
    },
    "smart-triggers": {
        "label": "Smart Triggers",
        "icon": "ti-bolt",
        "description": "Fire automations the moment a lead does something worth acting on.",
    },
    "analytics": {
        "label": "Analytics",
        "icon": "ti-chart-bar",
        "description": "See conversion, response time, and pipeline health at a glance.",
    },
    "instagram": {
        "label": "Instagram",
        "icon": "ti-brand-instagram",
        "description": "Manage Instagram DMs and comments from the same inbox as everything else.",
    },
    "integrations-hub": {
        "label": "Integrations Hub",
        "icon": "ti-plug",
        "description": "Connect the tools you already use — no code required.",
    },
    "call-scheduler": {
        "label": "Call Scheduler",
        "icon": "ti-calendar-event",
        "description": "Let leads book time on your calendar without the back-and-forth.",
    },
    "call-tracker": {
        "label": "Call Tracker",
        "icon": "ti-phone",
        "description": "Log, record, and review calls tied directly to each lead.",
    },
    "teams": {
        "label": "Teams",
        "icon": "ti-users-group",
        "description": "Roles, permissions, and shared ownership for growing teams.",
    },
    "auto-follow-ups-sequences": {
        "label": "Sequences",
        "icon": "ti-repeat",
        "description": "Build multi-step follow-up sequences that run automatically.",
    },
    "auto-follow-ups-workflows": {
        "label": "Workflows",
        "icon": "ti-git-branch",
        "description": "Chain conditions and actions together into a full follow-up workflow.",
    },
    "knowledge-base-ai-setup": {
        "label": "AI Setup",
        "icon": "ti-settings",
        "description": "Configure what your AI Co-Pilot can see and how it should respond.",
    },
    "knowledge-base-faq": {
        "label": "FAQ",
        "icon": "ti-help-circle",
        "description": "Maintain a set of question-and-answer pairs your AI can draw from.",
    },
    "instagram-connect": {
        "label": "Connect Instagram",
        "icon": "ti-brand-instagram",
        "description": "Link your Instagram business account to start managing DMs here.",
    },
    "instagram-chats": {
        "label": "Chats",
        "icon": "ti-message-circle",
        "description": "View and reply to Instagram conversations from your inbox.",
    },
        "team-member-settings": {
        "label": "Agent Settings",
        "icon": "ti-settings",
        "description": "Per-agent send limits, AI auto-reply, and follow-up automation controls.",
    },
}


def coming_soon(request, feature):
    feature_info = COMING_SOON_FEATURES.get(
        feature,
        {
            "label": feature.replace("-", " ").title(),
            "icon": "ti-hourglass",
            "description": "This feature isn't available yet.",
        },
    )

    return render(
        request,
        "crm/coming_soon.html",
        {
            "feature_slug": feature,
            "feature_label": feature_info["label"],
            "feature_icon": feature_info["icon"],
            "feature_description": feature_info["description"],
        },
    )
