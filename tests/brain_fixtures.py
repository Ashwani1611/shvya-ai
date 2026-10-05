"""Canonical profile injection for isolated Sandbox/provider tests."""
from copy import deepcopy

from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.organization_runtime_profile import OrganizationAIRuntimeProfile
from apps.ai_engagement.services.playbook import qualification_questions


def mocked_brain_profile_loader(org_info_service, *, requirements=None):
    """Read the existing authored fixture without invoking the database loader."""

    def load(*, organization):
        org_info = org_info_service.get_or_create(organization=organization)
        legacy = {
            "id": str(organization.id), "name": organization.name,
            "ai_enabled": org_info.ai_enabled, "about": org_info.about,
            "bot_languages": org_info.bot_languages, "ai_playbook": org_info.ai_playbook,
            "qualification_model": str(getattr(org_info, "qualification_model", "") or ""),
            "sales_support_model": str(getattr(org_info, "sales_support_model", "") or ""),
            "summary_model": str(getattr(org_info, "summary_model", "") or ""),
            "bump_up_enabled": org_info.bump_up_enabled, "bump_up_count": org_info.bump_up_count,
        }
        configured = requirements
        if configured is None:
            configured = compile_qualification_requirements(
                qualification_questions(org_info.ai_playbook),
            )["requirements"]
        return OrganizationAIRuntimeProfile(
            organization_id=str(organization.id), organization_name=organization.name,
            profile_version="phase4.v2", revision="a" * 64,
            brain_bundle_schema_version=1, brain_bundle_revision="b" * 64,
            identity={}, business_information={}, ai_instructions={},
            qualification={"requirements": deepcopy(configured)}, business_facts={},
            booking_handoff={}, knowledge_sources=[], crm_capabilities={},
            ai_configuration={}, channels={}, _legacy_organization=deepcopy(legacy),
        )

    return load
