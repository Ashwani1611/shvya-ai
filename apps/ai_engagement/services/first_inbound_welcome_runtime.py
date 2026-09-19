"""Deterministic first-inbound greeting for WhatsApp AI conversations.

The engagement model already receives the backend-selected qualification
requirement.  This layer only guarantees that the very first customer-facing
reply starts with the configured SHVYA welcome shape before that requirement.
It deliberately does not choose, reorder, or invent qualification questions.
"""

from __future__ import annotations

from dataclasses import replace
from functools import wraps
import re


_INSTALLED = False
_GENERIC_LEAD_NAMES = {
    "lead",
    "whatsapp lead",
    "whatsapp user",
    "unknown",
    "unknown lead",
}
_GREETING_RE = re.compile(r"^[\s*_]*(?:hi|hello|hey|welcome)\b", re.IGNORECASE)


def _lead_first_name(value: str) -> str:
    """Return a safe first name, or an empty string for placeholders/phones."""
    name = " ".join(str(value or "").strip().split())
    if not name or name.casefold() in _GENERIC_LEAD_NAMES:
        return ""
    if "@" in name:
        return ""

    compact = re.sub(r"[\s()+.\-]", "", name)
    if compact.isdigit():
        return ""

    first = name.split(" ", 1)[0].strip(" ,:;.!?*-_()[]{}")
    if not first or len(first) > 50 or first.casefold() in _GENERIC_LEAD_NAMES:
        return ""
    if re.fullmatch(r"\+?\d+", first):
        return ""
    return first


def _is_first_inbound_turn(lead) -> bool:
    """True only before SHVYA has replied to the lead's first inbound message."""
    manager = getattr(lead, "whatsapp_messages", None)
    if manager is None:
        return False

    try:
        from apps.channels.models import WhatsAppMessage

        inbound = manager.filter(
            organization_id=lead.organization_id,
            direction=WhatsAppMessage.Direction.INBOUND,
        ).order_by("-created_at", "-id")
        latest = inbound.first()
        if latest is None:
            return False
        if inbound.exclude(pk=latest.pk).exists():
            return False
        return not manager.filter(
            organization_id=lead.organization_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            created_at__lte=latest.created_at,
        ).exists()
    except Exception:
        # Greeting enforcement must never make a valid engagement turn fail.
        return False


def apply_first_inbound_welcome(*, decision, organization, lead, first_turn=None):
    """Prepend one welcome while preserving the generated/backend-selected reply."""
    if not getattr(decision, "should_engage", False):
        return decision

    message = str(getattr(decision, "message", "") or "").strip()
    if not message:
        return decision

    if first_turn is None:
        first_turn = _is_first_inbound_turn(lead)
    if not first_turn:
        return decision

    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.playbook import parse_playbook
    from apps.organizations.models import Organization

    # Sandbox/test organization values are in-memory contexts, not ORM keys.
    # Only persisted organization models can authorize a tenant-scoped lookup.
    info = None
    if isinstance(organization, Organization) and not organization._state.adding:
        info = OrgInfo.objects.filter(organization_id=organization.pk).only("ai_playbook").first()
    authored = parse_playbook(info.ai_playbook if info else "")["welcome_message"]
    if authored:
        if authored in message:
            return decision
        if _GREETING_RE.match(message):
            parts = message.split("\n\n", 1)
            message = parts[1] if len(parts) > 1 else message
        return replace(decision, message=f"{authored}\n\n{message}")
    if _GREETING_RE.match(message):
        return decision

    organization_name = " ".join(str(getattr(organization, "name", "") or "").strip().split())
    first_name = _lead_first_name(getattr(lead, "name", ""))

    if first_name and organization_name:
        greeting = f"Hi {first_name}! Thanks for reaching out to {organization_name}."
    elif first_name:
        greeting = f"Hi {first_name}!"
    elif organization_name:
        greeting = f"Hi there! Thanks for reaching out to {organization_name}."
    else:
        greeting = "Hi there!"

    return replace(decision, message=f"{greeting}\n\n{message}")


def install_first_inbound_welcome_runtime() -> None:
    """Wrap the final EngagementService implementation once at app startup."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    original_engage = EngagementService.engage

    @wraps(original_engage)
    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        decision = original_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        return apply_first_inbound_welcome(
            decision=decision,
            organization=organization,
            lead=lead,
        )

    EngagementService.engage = engage
    _INSTALLED = True
