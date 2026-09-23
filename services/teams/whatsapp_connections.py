# ruff: noqa: F401
"""Compatibility facade for the canonical Teams WhatsApp connection service."""

from apps.teams.services.whatsapp_connections import (
    _account_label as _account_label,
    _account_number as _account_number,
    member_whatsapp_connections as member_whatsapp_connections,
)
