from __future__ import annotations

from dataclasses import dataclass

from apps.ai_engagement.services.org_info import OrgInfoService


class AIPermissionError(Exception):
    """Raised when AI permission state cannot be evaluated safely."""


@dataclass(frozen=True)
class AIPermissionDecision:
    """Normalized technical AI permission result."""

    allowed: bool
    reason: str
    organization_id: str | None
    lead_id: str | None
    stage_id: str | None = None


def _normalize_whatsapp_number(*, country_code="", phone_number="") -> str:
    raw_phone = str(phone_number or "").strip()
    raw_country = str(country_code or "").strip()
    phone_digits = "".join(ch for ch in raw_phone if ch.isdigit())
    country_digits = "".join(ch for ch in raw_country if ch.isdigit())

    if not phone_digits:
        return ""

    if raw_phone.startswith("+"):
        combined = phone_digits
    elif country_digits and phone_digits.startswith(country_digits):
        combined = phone_digits
    else:
        combined = f"{country_digits}{phone_digits}"

    if len(combined) < 8 or len(combined) > 15:
        return ""
    return f"+{combined}"


class AIPermissionService:
    """
    Central evaluator for the SHVYA AI control hierarchy.

    AI may operate only when organization, current stage, and lead switches are
    enabled. For an existing WhatsApp conversation, the account carrying that
    conversation must also be the number linked to the lead's current pipeline.
    """

    def __init__(
        self,
        *,
        org_info_service: OrgInfoService | None = None,
    ) -> None:
        self.org_info_service = org_info_service or OrgInfoService()

    def _decision(self, *, allowed, reason, organization, lead):
        return AIPermissionDecision(
            allowed=allowed,
            reason=reason,
            organization_id=str(organization.id),
            lead_id=str(lead.id),
            stage_id=(str(lead.stage_id) if lead.stage_id else None),
        )

    def _conversation_uses_pipeline_number(self, *, organization, lead):
        """Fail closed when a WhatsApp conversation is on the wrong number."""
        latest_message = (
            lead.whatsapp_messages.filter(organization=organization)
            .select_related("account")
            .order_by("-created_at", "-id")
            .first()
        )

        # Permission evaluation is also used before a conversation exists. The
        # AI engagement worker separately requires an inbound message before it
        # can generate or send a customer-facing reply.
        if latest_message is None:
            return True, "no_conversation_yet"

        pipeline = getattr(lead, "pipeline", None)
        expected_number = _normalize_whatsapp_number(
            country_code=getattr(pipeline, "country_code", ""),
            phone_number=getattr(pipeline, "phone_number", ""),
        )
        if not expected_number:
            return False, "pipeline_whatsapp_number_missing"

        account = getattr(latest_message, "account", None)
        if account is None or account.organization_id != organization.id:
            return False, "whatsapp_account_organization_mismatch"

        actual_number = _normalize_whatsapp_number(
            phone_number=getattr(account, "display_phone_number", ""),
        )
        if not actual_number:
            return False, "whatsapp_account_number_missing"

        if actual_number != expected_number:
            return False, "pipeline_whatsapp_account_mismatch"

        return True, "pipeline_whatsapp_account_match"

    def evaluate(
        self,
        *,
        organization,
        lead,
    ) -> AIPermissionDecision:
        """Evaluate current, non-cached AI permission state for one Lead."""

        if organization is None:
            raise AIPermissionError("Organization is required.")
        if lead is None:
            raise AIPermissionError("Lead is required.")

        if lead.organization_id != organization.id:
            return self._decision(
                allowed=False,
                reason="organization_mismatch",
                organization=organization,
                lead=lead,
            )

        org_info = self.org_info_service.get_or_create(
            organization=organization,
        )
        if not org_info.ai_enabled:
            return self._decision(
                allowed=False,
                reason="organization_ai_disabled",
                organization=organization,
                lead=lead,
            )

        if lead.stage_id and not lead.stage.ai_on:
            return self._decision(
                allowed=False,
                reason="stage_ai_disabled",
                organization=organization,
                lead=lead,
            )

        if not lead.ai_enabled:
            return self._decision(
                allowed=False,
                reason="lead_ai_disabled",
                organization=organization,
                lead=lead,
            )

        mapping_allowed, mapping_reason = self._conversation_uses_pipeline_number(
            organization=organization,
            lead=lead,
        )
        if not mapping_allowed:
            return self._decision(
                allowed=False,
                reason=mapping_reason,
                organization=organization,
                lead=lead,
            )

        return self._decision(
            allowed=True,
            reason="allowed",
            organization=organization,
            lead=lead,
        )
