from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.tenant_guard import TenantGuard


RUNTIME_PROFILE_VERSION = "phase4.v1"

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


def _safe_source_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    # Query strings/fragments can carry signed credentials. Source metadata needs
    # identity, not authorization material.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[
        :MAX_STRING_LENGTH
    ]


def _safe_source_key(value: str) -> str:
    raw = str(value or "").strip()
    if raw.casefold().startswith(("http://", "https://")):
        return _safe_source_url(raw)
    return raw[:500]


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

        from apps.ai_engagement.models import Document, OrgInfo
        from apps.channels.models import WhatsAppAccount
        from apps.crm.models import AttributeDefinition, Pipeline, Stage

        org_info = (
            OrgInfo.objects.filter(organization_id=organization.id)
            .values(
                "about",
                "bot_languages",
                "qualification_requirements",
                "engagement_instructions",
                "ai_enabled",
                "bump_up_enabled",
                "bump_up_count",
            )
            .first()
        ) or {}

        legacy_organization = {
            "id": str(organization.id),
            "name": str(organization.name or ""),
            "ai_enabled": bool(org_info.get("ai_enabled", False)),
            "about": str(org_info.get("about") or ""),
            "bot_languages": str(org_info.get("bot_languages") or ""),
            "qualification_requirements": str(
                org_info.get("qualification_requirements") or ""
            ),
            "engagement_instructions": str(
                org_info.get("engagement_instructions") or ""
            ),
            "bump_up_enabled": bool(org_info.get("bump_up_enabled", False)),
            "bump_up_count": int(org_info.get("bump_up_count") or 0),
        }
        compiled = compile_org_ai_profile_from_context(legacy_organization)

        pipeline_rows = list(
            Pipeline.objects.filter(
                organization_id=organization.id,
                is_active=True,
            )
            .order_by("name", "id")
            .values(
                "id",
                "name",
                "description",
                "country_code",
                "phone_number",
                "ai_enabled",
            )[:MAX_PIPELINES]
        )
        pipeline_ids = [row["id"] for row in pipeline_rows]
        stage_rows = list(
            Stage.objects.filter(
                pipeline_id__in=pipeline_ids,
                pipeline__organization_id=organization.id,
                is_active=True,
            )
            .order_by("pipeline_id", "display_order", "name", "id")
            .values(
                "id",
                "pipeline_id",
                "name",
                "description",
                "display_order",
                "color",
                "ai_on",
                "config",
            )[:MAX_STAGES]
        )
        stages_by_pipeline: dict[str, list[dict[str, Any]]] = {}
        for row in stage_rows:
            stages_by_pipeline.setdefault(str(row["pipeline_id"]), []).append(
                {
                    "id": str(row["id"]),
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
                "id": str(row["id"]),
                "name": row["name"],
                "description": row["description"],
                "country_code": row["country_code"],
                "phone_number": row["phone_number"],
                "ai_enabled": bool(row["ai_enabled"]),
                "stages": stages_by_pipeline.get(str(row["id"]), []),
            }
            for row in pipeline_rows
        ]

        attributes = [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "key": row["key"],
                "field_type": row["field_type"],
                "description": row["description"],
                "options": _bounded_safe_copy(row.get("options") or []),
                "display_order": row["display_order"],
            }
            for row in AttributeDefinition.objects.filter(
                organization_id=organization.id
            )
            .order_by("display_order", "id")
            .values(
                "id",
                "name",
                "key",
                "field_type",
                "description",
                "options",
                "display_order",
            )[:MAX_ATTRIBUTES]
        ]

        documents = [
            {
                "id": row["id"],
                "name": row["name"],
                "source_key": _safe_source_key(row["source_key"]),
                "source_url": _safe_source_url(row["source_url"]),
                "version": row["version"],
                "processing_status": row["processing_status"],
                "is_active": bool(row["is_active"]),
                "has_file": bool(row["file"]),
                "share_instruction": str(row["share_instruction"] or "")[
                    :MAX_STRING_LENGTH
                ],
            }
            for row in Document.objects.filter(
                organization_id=organization.id,
                is_active=True,
                processing_status=Document.ProcessingStatus.COMPLETED,
            )
            .order_by("-updated_at", "-id")
            .values(
                "id",
                "name",
                "source_key",
                "source_url",
                "version",
                "processing_status",
                "is_active",
                "file",
                "share_instruction",
            )[:MAX_KNOWLEDGE_SOURCES]
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
            "engagement_instructions": legacy_organization["engagement_instructions"],
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
            "runtime_options": _selected_settings(raw_settings, _AI_CONFIGURATION_KEYS),
        }
        channels = {
            "whatsapp_accounts": whatsapp_accounts,
        }
        identity = {
            "organization_id": str(organization.id),
            "name": str(organization.name or ""),
            "timezone": str(getattr(organization, "timezone", "") or ""),
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
        revision = _fingerprint(revision_payload)

        return OrganizationAIRuntimeProfile(
            organization_id=str(organization.id),
            organization_name=str(organization.name or ""),
            profile_version=RUNTIME_PROFILE_VERSION,
            revision=revision,
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
) -> OrganizationAIRuntimeProfile:
    """Return one per-Lead-object snapshot without creating persistent duplicate state."""

    cache_attr = "_shvya_ai_runtime_profile"
    if lead is not None:
        guard = TenantGuard(organization)
        guard.validate_current_lead_context(lead)
        cached = getattr(lead, cache_attr, None)
        if (
            isinstance(cached, OrganizationAIRuntimeProfile)
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
