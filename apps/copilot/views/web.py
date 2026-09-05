from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import Stage
from services.copilot_service import (
    FLAG_DEFINITIONS,
    active_flags_for_user,
    ensure_fresh_cache,
    get_copilot_config,
    group_flags_for_dashboard,
    visible_pipelines_for_user,
)


SECTION_META = [
    {
        "severity": "critical",
        "title": "Critical Intervention",
        "subtitle": "Leads that need your immediate action",
        "icon": "ti-alert-circle",
        "border": "border-red-400",
        "icon_color": "text-red-500",
        "badge": "bg-red-50 text-red-600",
    },
    {
        "severity": "high",
        "title": "Needs Attention Today",
        "subtitle": "Leads that need your attention today",
        "icon": "ti-clock-hour-4",
        "border": "border-orange-400",
        "icon_color": "text-orange-500",
        "badge": "bg-orange-50 text-orange-600",
    },
    {
        "severity": "medium",
        "title": "Follow-Up Recommended",
        "subtitle": "Leads that should be followed up",
        "icon": "ti-info-circle",
        "border": "border-amber-400",
        "icon_color": "text-amber-500",
        "badge": "bg-amber-50 text-amber-600",
    },
    {
        "severity": "low",
        "title": "Monitor Leads",
        "subtitle": "Leads to keep an eye on",
        "icon": "ti-eye",
        "border": "border-emerald-500",
        "icon_color": "text-emerald-600",
        "badge": "bg-emerald-50 text-emerald-700",
    },
]


@crm_login_required
@require_GET
def copilot_dashboard(request):
    user = request.crm_user
    organization = user.organization
    config = get_copilot_config(organization)

    cards = []
    if config["copilot_enabled"]:
        ensure_fresh_cache(organization)
        cards = group_flags_for_dashboard(active_flags_for_user(user))

    cards_by_severity = {item["severity"]: [] for item in SECTION_META}
    for card in cards:
        cards_by_severity[card["severity"]].append(card)

    sections = []
    for item in SECTION_META:
        section = dict(item)
        section["cards"] = cards_by_severity[item["severity"]]
        section["count"] = len(section["cards"])
        sections.append(section)

    pipelines = list(visible_pipelines_for_user(user).order_by("name"))
    pipeline_options = []
    for pipeline in pipelines:
        stages = list(
            Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
            ).order_by("display_order", "name")
        )
        pipeline_options.append(
            {
                "id": str(pipeline.id),
                "name": pipeline.name,
                "stages": [
                    {"id": str(stage.id), "name": stage.name}
                    for stage in stages
                ],
            }
        )

    context = {
        "copilot_config": config,
        "copilot_enabled": config["copilot_enabled"],
        "sections": sections,
        "pipelines": pipelines,
        "pipeline_options": pipeline_options,
        "flag_definitions": FLAG_DEFINITIONS,
        "is_copilot_admin": user.role == User.Role.ADMIN,
    }

    response = render(request, "copilot/dashboard.html", context)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response
