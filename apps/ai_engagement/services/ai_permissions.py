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

    The checks are deliberately re-read from the database by callers before
    generation and again before delivery. WhatsApp transport ownership is
    conversation-scoped: once a customer is actively talking to an organization
    owned Connect API/Hosted account, a later CRM pipeline move must not strand
    that live conversation merely because the destination pipeline has another
    routing number. The current pipeline/stage/lead switches still govern whether
    AI may continue after that move.
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
                direction="inbound",
            )
            .select_related("account")
            .order_by("-created_at", "-id")
            .first()
        )

    def _conversation_uses_pipeline_number(
        self,
        *,
        organization,
        lead,
        latest_message=None,
    ):
        """Validate the customer-facing WhatsApp transport for this conversation.

        A freshly routed lead normally matches the current pipeline number. Once
        that lead is intentionally moved by CRM automation, however, the same
        WhatsApp thread remains the authoritative transport. We therefore accept
        an organization-owned, active, connected inbound account even if the CRM
        destination pipeline has a different number. This keeps transport routing
        and CRM classification separate while still failing closed on foreign or
        disconnected accounts.
        """
        if latest_message is None:
            latest_message = self._latest_inbound_message(
                organization=organization,
                lead=lead,
            )

        # Permission evaluation is also used before an inbound conversation
        # exists. The engagement worker independently requires an inbound turn.
        if latest_message is None:
            return True, "no_conversation_yet"

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

        # The inbound message itself is a strong conversation binding. Allow it
        # to survive an intentional pipeline transfer inside the same tenant.
        return True, "conversation_whatsapp_account_bound"

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

        # Organization is the top-level customer-facing AI master switch.
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

        # Stage and Lead switches are never bypassed for WhatsApp. This is
        # intentional: an admin turning either switch off must stop both newly
        # queued and already-generated AI replies before delivery.
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

        latest_inbound = self._latest_inbound_message(
            organization=organization,
            lead=lead,
        )
        mapping_allowed, mapping_reason = self._conversation_uses_pipeline_number(
            organization=organization,
            lead=lead,
            latest_message=latest_inbound,
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