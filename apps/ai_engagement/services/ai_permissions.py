from __future__ import annotations

from dataclasses import dataclass

from apps.ai_engagement.services.org_info import OrgInfoService


class AIPermissionError(Exception):
    """
    Raised when AI permission state cannot be evaluated safely.
    """


@dataclass(frozen=True)
class AIPermissionDecision:
    """
    Normalized technical AI permission result.

    This answers one question only:

        Is SHVYA AI technically allowed to operate for this Lead?

    It does not decide what the AI should do, generate a response,
    mutate CRM records, or send a message.
    """

    allowed: bool
    reason: str
    organization_id: str | None
    lead_id: str | None
    stage_id: str | None = None


class AIPermissionService:
    """
    Central evaluator for the SHVYA AI control hierarchy.

    Permission hierarchy:

        Organization AI
            ↓
        Stage AI
            ↓
        Lead AI

    These are technical permission controls only. They must not be
    used as business rules for deciding engagement behavior.
    """

    def __init__(
        self,
        *,
        org_info_service: OrgInfoService | None = None,
    ) -> None:
        self.org_info_service = (
            org_info_service
            or OrgInfoService()
        )

    def evaluate(
        self,
        *,
        organization,
        lead,
    ) -> AIPermissionDecision:
        """
        Evaluate whether AI may operate for the supplied Lead.
        """

        if organization is None:
            raise AIPermissionError(
                "Organization is required."
            )

        if lead is None:
            raise AIPermissionError(
                "Lead is required."
            )

        organization_id = str(
            organization.id
        )
        lead_id = str(
            lead.id
        )
        stage_id = (
            str(lead.stage_id)
            if lead.stage_id
            else None
        )

        if lead.organization_id != organization.id:
            return AIPermissionDecision(
                allowed=False,
                reason="organization_mismatch",
                organization_id=organization_id,
                lead_id=lead_id,
                stage_id=stage_id,
            )

        org_info = (
            self.org_info_service.get_or_create(
                organization=organization,
            )
        )

        if not org_info.ai_enabled:
            return AIPermissionDecision(
                allowed=False,
                reason="organization_ai_disabled",
                organization_id=organization_id,
                lead_id=lead_id,
                stage_id=stage_id,
            )

        if lead.stage_id and not lead.stage.ai_on:
            return AIPermissionDecision(
                allowed=False,
                reason="stage_ai_disabled",
                organization_id=organization_id,
                lead_id=lead_id,
                stage_id=stage_id,
            )

        if not lead.ai_enabled:
            return AIPermissionDecision(
                allowed=False,
                reason="lead_ai_disabled",
                organization_id=organization_id,
                lead_id=lead_id,
                stage_id=stage_id,
            )

        return AIPermissionDecision(
            allowed=True,
            reason="allowed",
            organization_id=organization_id,
            lead_id=lead_id,
            stage_id=stage_id,
        )
