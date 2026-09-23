"""Compatibility facade for CRM stage-requirement services."""

from apps.crm.services.stage_requirements import (
    clean_entry_values,
    has_value,
    missing_attributes,
    required_attributes,
)

__all__ = [
    "clean_entry_values",
    "has_value",
    "missing_attributes",
    "required_attributes",
]
