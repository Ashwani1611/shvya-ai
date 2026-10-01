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
    """Central evaluator for the SHVYA AI control hierarchy.

    Customer-facing AI is allowed only when every configured control permits it:

        Organization AI -> Pipeline AI -> Stage AI -> Lead AI

    Every WhatsApp send remains bound to the number configured for the lead's
    current pipeline. Historical messages never authorize a different sender.
    """

    WHATSAPP_AUTOMATION_CONNECTION_TYPES = {"api", "hosted"}

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

    def _latest_inbound_message(self, *, organization, lead):
        return (
            lead.whatsapp_messages.filter(
                organization=organization,
                account__organization=organization,
                direction="inbound",
            )
            .select_related("account")
            .defer("account__access_token")
            .order_by("-created_at", "-id")
            .first()
        )

    def _conversation_uses_pipeline_number(
        self,
        *,
        organization,
        lead,
        latest_message=None,
        account=None,
    ):
        """Validate the customer-facing WhatsApp transport for this conversation."""
        if latest_message is None and account is None:
            latest_message = self._latest_inbound_message(
                organization=organization,
                lead=lead,
            )

        if latest_message is None and account is None:
            return True, "no_conversation_yet"

        if account is None:
            account = getattr(latest_message, "account", None)
        if account is None or account.organization_id != organization.id:
            return False, "whatsapp_account_organization_mismatch"
        if not getattr(account, "is_active", False):
            return False, "whatsapp_account_inactive"
        if str(getattr(account, "status", "") or "").strip().casefold() != "connected":
            return False, "whatsapp_account_not_connected"

        inbound_connection_type = str(
            getattr(account, "connection_type", "") or ""
        ).strip().casefold()
        if inbound_connection_type not in self.WHATSAPP_AUTOMATION_CONNECTION_TYPES:
            return False, "unsupported_whatsapp_connection_type"

        actual_number = _normalize_whatsapp_number(
            phone_number=getattr(account, "display_phone_number", ""),
        )
        if not actual_number:
            return False, "whatsapp_account_number_missing"

        pipeline = getattr(lead, "pipeline", None)
        expected_number = _normalize_whatsapp_number(
            country_code=getattr(pipeline, "country_code", ""),
            phone_number=getattr(pipeline, "phone_number", ""),
        )
        if expected_number and actual_number == expected_number:
            return True, "pipeline_whatsapp_account_match"

        if not expected_number:
            return False, "pipeline_whatsapp_number_missing"
        return False, "pipeline_whatsapp_account_mismatch"

    def evaluate(
        self,
        *,
        organization,
        lead,
        latest_inbound=None,
        account=None,
        channel="whatsapp",
    ) -> AIPermissionDecision:
        """Evaluate current, non-cached AI permission state for one Lead.

        ``latest_inbound`` lets durable provider-specific jobs bind permission
        evaluation to the exact authenticated message they own rather than a
        newer message on a different connected number for the same Lead.
        ``account`` binds proactive welcomes and final sends without requiring
        an inbound message to exist.
        """

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

        try:
            org_info = self.org_info_service.get_or_create(
                organization=organization,
            )
        except Exception as exc:
            raise AIPermissionError(
                "Organization AI configuration could not be loaded."
            ) from exc

        if not org_info.ai_enabled:
            return self._decision(
                allowed=False,
                reason="organization_ai_disabled",
                organization=organization,
                lead=lead,
            )

        if not getattr(lead.pipeline, "ai_enabled", True):
            return self._decision(
                allowed=False,
                reason="pipeline_ai_disabled",
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

        normalized_channel = str(channel or "whatsapp").strip().casefold()
        if normalized_channel == "instagram":
            # Instagram conversation identity is the Meta participant ID, not a
            # phone number or a pipeline-bound WhatsApp sender. The same org,
            # pipeline, stage, and lead AI toggles still apply above.
            return self._decision(
                allowed=True,
                reason="allowed",
                organization=organization,
                lead=lead,
            )
        if normalized_channel != "whatsapp":
            return self._decision(
                allowed=False,
                reason="unsupported_ai_channel",
                organization=organization,
                lead=lead,
            )

        if latest_inbound is None and account is None:
            latest_inbound = self._latest_inbound_message(
                organization=organization,
                lead=lead,
            )
        mapping_allowed, mapping_reason = self._conversation_uses_pipeline_number(
            organization=organization,
            lead=lead,
            latest_message=latest_inbound,
            account=account,
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
