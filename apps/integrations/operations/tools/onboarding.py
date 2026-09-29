"""Read-only onboarding orchestration and reusable playbook discovery."""

from __future__ import annotations

from apps.ai_engagement.models import FAQ, OrgInfo
from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Pipeline
from apps.followups.models import FollowupSequence
from apps.integrations.operations_tools import ToolExecution, _organization_for, _require_operations_capability
from apps.integrations.operations_policy import CAP_ORGANIZATION_READ
from apps.shvya_calendar.models import CalendarPage
from apps.triggers.models import SmartTrigger


INDUSTRY_PLAYBOOKS = {
    "professional_services": {
        "label": "Professional services",
        "stages": ["New lead", "Discovery", "Proposal", "Qualified", "Won", "Lost"],
        "attributes": ["service_interest", "budget_range", "decision_timeline", "decision_maker"],
        "faq_topics": ["services", "pricing", "process", "availability"],
        "sequence_pattern": ["instant acknowledgement", "discovery reminder", "proposal follow-up"],
        "rules": ["handoff on explicit human request", "suppress after opt-out", "pause after booking"],
        "touchpoints": ["welcome", "discovery", "proposal", "re-engagement"],
    },
    "education": {
        "label": "Education",
        "stages": ["New lead", "Program fit", "Counselling", "Application", "Enrolled", "Lost"],
        "attributes": ["program_interest", "intake", "location", "budget_range"],
        "faq_topics": ["programs", "fees", "eligibility", "admissions"],
        "sequence_pattern": ["course overview", "counselling reminder", "application follow-up"],
        "rules": ["handoff for admissions questions", "suppress after opt-out", "stop on enrolment"],
        "touchpoints": ["welcome", "program fit", "counselling", "application"],
    },
    "real_estate": {
        "label": "Real estate",
        "stages": ["New lead", "Requirement capture", "Viewing", "Negotiation", "Booked", "Lost"],
        "attributes": ["property_type", "location", "budget_range", "move_timeline"],
        "faq_topics": ["inventory", "pricing", "viewings", "documentation"],
        "sequence_pattern": ["requirement acknowledgement", "viewing reminder", "post-viewing follow-up"],
        "rules": ["handoff for negotiation", "suppress after opt-out", "pause after booking"],
        "touchpoints": ["welcome", "requirement capture", "viewing", "negotiation"],
    },
}


def list_industry_playbooks(*, identity, arguments):
    organization = _organization_for(identity, required=False)
    if organization is not None:
        _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    requested = str((arguments or {}).get("industry") or "").strip().lower()
    rows = []
    for key, value in INDUSTRY_PLAYBOOKS.items():
        if requested and requested != key:
            continue
        rows.append({"key": key, **value, "tenant_data": False})
    return ToolExecution(data={"playbooks": rows, "count": len(rows), "voice_agent": "excluded", "vault": "excluded"}, capability=CAP_ORGANIZATION_READ, target_type="platform" if organization is None else "organization", target_id=str(organization.id) if organization else "")


def prepare_account_onboarding(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    info = OrgInfo.objects.filter(organization=organization).first()
    phases = [
        ("intake", bool(info and (info.about or "").strip()), "Capture source-attributed onboarding facts and contradictions."),
        ("ai_brain", bool(info and (info.ai_playbook or "").strip()), "Review AI Brain About, Playbook, language and policy."),
        ("crm", Pipeline.objects.filter(organization=organization, is_active=True).exists() and AttributeDefinition.objects.filter(organization=organization, is_active=True).exists(), "Align pipelines, stages and lead attributes."),
        ("qualification", bool(info and "Qualification Questions" in (info.ai_playbook or "")), "Validate required questions, mappings and completion stage."),
        ("knowledge_faqs", FAQ.objects.filter(organization=organization, is_active=True).exists(), "Publish approved FAQs and knowledge metadata."),
        ("integrations", WhatsAppAccount.objects.filter(organization=organization, is_active=True).exists(), "Connect, validate and route supported providers."),
        ("calendar", CalendarPage.objects.filter(organization=organization).exists(), "Configure booking pages, reminders and provider sync."),
        ("automation", SmartTrigger.objects.filter(organization=organization, is_active=True).exists() and FollowupSequence.objects.filter(organization=organization, is_active=True).exists(), "Validate Workflows, Cadences, suppression and ordering."),
        ("acceptance", False, "Run no-send acceptance coverage before activation."),
    ]
    missing = [name for name, ready, _ in phases if not ready]
    return ToolExecution(data={"organization_id": str(organization.id), "orchestration": [{"phase": name, "status": "ready" if ready else "needs_input", "description": description} for name, ready, description in phases], "missing_phases": missing, "recommended_order": [name for name, _, _ in phases], "writes_performed": 0, "messages_sent": 0, "activation_performed": False, "next_action": "Use existing dry-run configuration tools to assemble an approval plan, then verify each phase in order."}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id))
