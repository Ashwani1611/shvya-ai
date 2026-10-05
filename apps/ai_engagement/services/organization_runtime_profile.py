from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.tenant_guard import TenantGuard


RUNTIME_PROFILE_VERSION = "phase4.v2"

MAX_PIPELINES = 50
MAX_STAGES = 250
MAX_ATTRIBUTES = 200
MAX_KNOWLEDGE_SOURCES = 100
MAX_WHATSAPP_ACCOUNTS = 25
MAX_STRING_LENGTH = 12000
MAX_COLLECTION_ITEMS = 200
MAX_MAPPING_KEYS = 200
MAX_NESTING_DEPTH = 6

_SECRET_KEY_PARTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "smtp",
    "authorization",
    "oauth",
)

_BUSINESS_INFORMATION_KEYS = (
    "business_information",
    "products",
    "services",
    "terminology",
)
_AI_INSTRUCTION_KEYS = (
    "response_instructions",
    "tone",
    "communication_style",
    "language_rules",
    "restrictions",
)
_BUSINESS_FACT_KEYS = (
    "pricing",
    "plans",
    "packages",
    "policies",
    "locations",
    "working_hours",
    "hours",
    "service_availability",
    "availability",
)
_BOOKING_HANDOFF_KEYS = (
    "booking",
    "booking_rules",
    "handoff",
    "handoff_rules",
    "scheduling",
    "call_handoff",
)
_AI_CONFIGURATION_KEYS = (
    "ai",
    "ai_config",
    "ai_configuration",
    "runtime_options",
    "ai_objections", "ai_signals", "ai_memory", "ai_response",
    "ai_qualification", "ai_action_permissions",
)


def _secret_key(key: Any) -> bool:
    normalized = str(key or "").strip().casefold().replace("-", "_")
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _bounded_safe_copy(value: Any, *, depth: int = 0):
    """Copy JSON-like organization-authored state while dropping secret-like keys."""

    if depth > MAX_NESTING_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_MAPPING_KEYS:
                break
            if _secret_key(key):
                continue
            output[str(key)[:200]] = _bounded_safe_copy(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _bounded_safe_copy(item, depth=depth + 1)
            for item in list(value)[:MAX_COLLECTION_ITEMS]
        ]
    # Runtime profile is intentionally serializable and never carries arbitrary
    # ORM/provider objects.
    return str(value)[:MAX_STRING_LENGTH]


def _selected_settings(settings: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    selected = {
        key: settings[key]
        for key in keys
        if key in settings and not _secret_key(key)
    }
    return _bounded_safe_copy(selected)


def _freeze(value: Any):
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any):
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


@dataclass(frozen=True)
class OrganizationAIRuntimeProfile:
    """Bounded immutable adapter over canonical organization-owned configuration."""

    organization_id: str
    organization_name: str
    profile_version: str
    revision: str
    brain_bundle_schema_version: int
    brain_bundle_revision: str
    identity: Any
    business_information: Any
    ai_instructions: Any
    qualification: Any
    business_facts: Any
    booking_handoff: Any
    knowledge_sources: Any
    crm_capabilities: Any
    ai_configuration: Any
    channels: Any
    _legacy_organization: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "organization_name": self.organization_name,
            "profile_version": self.profile_version,
            "revision": self.revision,
            "brain_bundle": {
                "schema_version": self.brain_bundle_schema_version,
                "revision": self.brain_bundle_revision,
            },
            "identity": _thaw(self.identity),
            "business_information": _thaw(self.business_information),
            "ai_instructions": _thaw(self.ai_instructions),
            "qualification": _thaw(self.qualification),
            "business_facts": _thaw(self.business_facts),
            "booking_handoff": _thaw(self.booking_handoff),
            "knowledge_sources": _thaw(self.knowledge_sources),
            "crm_capabilities": _thaw(self.crm_capabilities),
            "ai_configuration": _thaw(self.ai_configuration),
            "channels": _thaw(self.channels),
        }

    def legacy_organization_context(self) -> dict[str, Any]:
        """Return the exact Organization context shape used before Phase 4."""
        return _thaw(self._legacy_organization)

    def configured_requirements(self) -> list[dict[str, Any]]:
        qualification = _thaw(self.qualification)
        requirements = qualification.get("requirements") if isinstance(qualification, dict) else []
        return requirements if isinstance(requirements, list) else []


def configured_action_types(settings) -> frozenset[str]:
    """Intersect tenant-authored permissions with the canonical action schema."""
    from apps.ai_engagement.services.crm_actions import ALLOWED_ACTION_TYPES

    settings = settings if isinstance(settings, dict) else {}
    permissions = settings.get("ai_action_permissions", {})
    permissions = permissions if isinstance(permissions, dict) else {}
    requested = permissions.get("allowed_action_types")
    if requested is None:
        return frozenset(ALLOWED_ACTION_TYPES)
    if not isinstance(requested, list):
        return frozenset()
    return frozenset(ALLOWED_ACTION_TYPES).intersection(item for item in requested if isinstance(item, str))


class OrganizationAIRuntimeProfileBuilder:
    """Load only bounded configuration for one deterministically resolved tenant."""

    def build(self, *, organization, lead=None) -> OrganizationAIRuntimeProfile:
        guard = TenantGuard(organization)
        if lead is not None:
            guard.validate_current_lead_context(lead)

        from apps.ai_engagement.services.organization_brain_bundle import (
            get_organization_ai_brain_bundle,
        )
        from apps.channels.models import WhatsAppAccount

        # The downloadable artifact and all channel runtimes share these exact
        # canonical rows. Only this adapter applies prompt/runtime size limits.
        bundle = get_organization_ai_brain_bundle(organization=organization)
        org_info = bundle["ai"]
        identity = bundle["organization"]
        legacy_organization = {
            "id": identity["id"],
            "name": identity["name"],
            "timezone": identity["timezone"],
            "ai_enabled": bool(org_info.get("ai_enabled", False)),
            "about": str(org_info.get("about") or ""),
            "bot_languages": str(org_info.get("bot_languages") or ""),
            "ai_playbook": str(org_info.get("ai_playbook") or ""),
            "qualification_model": str(org_info.get("qualification_model") or ""),
            "sales_support_model": str(org_info.get("sales_support_model") or ""),
            "summary_model": str(org_info.get("summary_model") or ""),
            "bump_up_enabled": bool(org_info.get("bump_up_enabled", False)),
            "bump_up_count": int(org_info.get("bump_up_count") or 0),
        }
        compiled = compile_org_ai_profile_from_context(legacy_organization)

        pipeline_rows = [
            row for row in bundle["crm"]["pipelines"] if row["is_active"]
        ][:MAX_PIPELINES]
        stage_rows = sorted(
            [(pipeline["id"], stage) for pipeline in pipeline_rows
             for stage in pipeline["stages"] if stage["is_active"]],
            key=lambda item: (item[0], item[1]["display_order"], item[1]["id"]),
        )[:MAX_STAGES]
        stages_by_pipeline: dict[str, list[dict[str, Any]]] = {}
        for pipeline_id, row in stage_rows:
            stages_by_pipeline.setdefault(pipeline_id, []).append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "display_order": row["display_order"],
                    "color": row["color"],
                    "ai_on": bool(row["ai_on"]),
                    "config": _bounded_safe_copy(row.get("config") or {}),
                }
            )
        pipelines = [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "country_code": row["country_code"],
                "phone_number": row["phone_number"],
                "ai_enabled": bool(row["ai_enabled"]),
                "stages": stages_by_pipeline.get(row["id"], []),
            }
            for row in pipeline_rows
        ]

        from apps.ai_engagement.services.confidentiality import (
            is_sensitive_attribute_definition,
        )

        attributes = [
            {
                "id": row["id"],
                "name": row["name"],
                "key": row["key"],
                "field_type": row["field_type"],
                "description": row["description"],
                "options": _bounded_safe_copy(row.get("options") or []),
                "display_order": row["display_order"],
            }
            for row in [item for item in bundle["crm"]["attributes"]
                        if item["is_active"]][:MAX_ATTRIBUTES]
            if not is_sensitive_attribute_definition(row)
        ]
        document_rows = sorted(
            [row for row in bundle["knowledge"]["documents"]
             if ((row["is_active"] and row["processing_status"] == "completed")
                 or (row["file_sharing_ready"] and row["has_file"]
                     and str(row["share_instruction"] or "").strip()))],
            key=lambda row: (row["updated_at"] or "", int(row["id"])),
            reverse=True,
        )[:MAX_KNOWLEDGE_SOURCES]
        documents = [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "source_key": row["source_url"] or "document:" + row["id"],
                "source_url": row["source_url"],
                "version": row["version"],
                "processing_status": row["processing_status"],
                "is_active": bool(row["is_active"]),
                "has_file": bool(row["has_file"]),
                "file_sharing_ready": bool(row["file_sharing_ready"]),
                "share_instruction": str(row["share_instruction"] or "")[:MAX_STRING_LENGTH],
            }
            for row in document_rows
        ]

        whatsapp_accounts = [
            {
                "id": str(row["id"]),
                "connection_type": row["connection_type"],
                "business_name": row["business_name"],
                "display_phone_number": row["display_phone_number"],
                "status": row["status"],
                "is_active": bool(row["is_active"]),
                "request_contact_info": bool(row["request_contact_info"]),
            }
            for row in WhatsAppAccount.objects.filter(
                organization_id=organization.id
            )
            .order_by("-connected_at", "-id")
            .values(
                "id",
                "connection_type",
                "business_name",
                "display_phone_number",
                "status",
                "is_active",
                "request_contact_info",
            )[:MAX_WHATSAPP_ACCOUNTS]
        ]

        organization_settings = getattr(organization, "settings", None)
        raw_settings = (
            organization_settings
            if isinstance(organization_settings, dict)
            else {}
        )
        business_information = {
            "about": legacy_organization["about"],
            "configured": _selected_settings(
                raw_settings,
                _BUSINESS_INFORMATION_KEYS,
            ),
        }
        ai_instructions = {
            "ai_playbook": legacy_organization["ai_playbook"],
            "languages": list((compiled.get("communication") or {}).get("languages") or []),
            "configured": _selected_settings(raw_settings, _AI_INSTRUCTION_KEYS),
        }
        qualification = _bounded_safe_copy(compiled.get("qualification") or {})
        business_facts = _selected_settings(raw_settings, _BUSINESS_FACT_KEYS)
        allowed_actions = configured_action_types(raw_settings)
        booking_handoff = {
            "configured": _selected_settings(raw_settings, _BOOKING_HANDOFF_KEYS),
            "capabilities": {
                "call_handoff": "create_reminder" in allowed_actions,
                "booking_executor": False,
            },
        }
        crm_capabilities = {
            "allowed_action_types": sorted(allowed_actions),
            "attributes": attributes,
            "pipelines": pipelines,
        }
        ai_configuration = {
            "ai_enabled": legacy_organization["ai_enabled"],
            "bump_up_enabled": legacy_organization["bump_up_enabled"],
            "bump_up_count": legacy_organization["bump_up_count"],
            "qualification_model": legacy_organization["qualification_model"],
            "sales_support_model": legacy_organization["sales_support_model"],
            "summary_model": legacy_organization["summary_model"],
            "runtime_options": _selected_settings(raw_settings, _AI_CONFIGURATION_KEYS),
        }
        channels = {
            "whatsapp_accounts": whatsapp_accounts,
        }
        identity = {
            "organization_id": str(organization.id),
            "name": legacy_organization["name"],
            "timezone": legacy_organization["timezone"],
        }

        revision_payload = _bounded_safe_copy(
            {
                "identity": identity,
                "business_information": business_information,
                "ai_instructions": ai_instructions,
                "qualification": qualification,
                "business_facts": business_facts,
                "booking_handoff": booking_handoff,
                "knowledge_sources": documents,
                "crm_capabilities": crm_capabilities,
                "ai_configuration": ai_configuration,
                "channels": channels,
            }
        )
        # The complete hash includes FAQ/source changes and edits beyond the
        # bounded adapter. Runtime-only permissions/account data stay covered too.
        revision = _fingerprint({
            "brain_bundle_revision": bundle["revision"],
            "runtime_configuration": revision_payload,
        })

        return OrganizationAIRuntimeProfile(
            organization_id=str(organization.id),
            organization_name=legacy_organization["name"],
            profile_version=RUNTIME_PROFILE_VERSION,
            revision=revision,
            brain_bundle_schema_version=bundle["schema_version"],
            brain_bundle_revision=bundle["revision"],
            identity=_freeze(identity),
            business_information=_freeze(business_information),
            ai_instructions=_freeze(ai_instructions),
            qualification=_freeze(qualification),
            business_facts=_freeze(business_facts),
            booking_handoff=_freeze(booking_handoff),
            knowledge_sources=_freeze(documents),
            crm_capabilities=_freeze(crm_capabilities),
            ai_configuration=_freeze(ai_configuration),
            channels=_freeze(channels),
            _legacy_organization=_freeze(legacy_organization),
        )


def get_organization_ai_runtime_profile(
    *,
    organization,
    lead=None,
    refresh: bool = False,
) -> OrganizationAIRuntimeProfile:
    """Reuse configuration within a turn; context builders refresh new turns.

    This is an in-memory snapshot, never a TTL cache of authoritative settings.
    ``refresh`` ensures a reused Lead object cannot retain an older FAQ/Brain
    revision when a new conversation context is assembled.
    """

    cache_attr = "_shvya_ai_runtime_profile"
    if lead is not None:
        guard = TenantGuard(organization)
        guard.validate_current_lead_context(lead)
        cached = getattr(lead, cache_attr, None)
        if (
            not refresh
            and isinstance(cached, OrganizationAIRuntimeProfile)
            and cached.organization_id == str(organization.id)
        ):
            return cached

    profile = OrganizationAIRuntimeProfileBuilder().build(
        organization=organization,
        lead=lead,
    )
    if lead is not None:
        setattr(lead, cache_attr, profile)
    return profile
