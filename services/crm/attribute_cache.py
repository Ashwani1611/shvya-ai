"""Redis-backed cache helpers for CRM attribute definitions."""

from django.core.cache import cache

from apps.crm.models import AttributeDefinition


ATTRIBUTE_DEFINITIONS_CACHE_TTL = 300


def attribute_definitions_cache_key(organization_id):
    return f"crm:org:{organization_id}:attribute-definitions:v1"


def get_cached_attribute_definitions(organization_id):
    """Return the small, read-only attribute payload used by Lead cards."""
    cache_key = attribute_definitions_cache_key(organization_id)
    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    definitions = list(
        AttributeDefinition.objects
        .filter(organization_id=organization_id)
        .order_by("display_order", "created_at")
        .values(
            "id",
            "name",
            "key",
            "field_type",
            "description",
            "options",
            "display_order",
        )
    )

    cache.set(
        cache_key,
        definitions,
        timeout=ATTRIBUTE_DEFINITIONS_CACHE_TTL,
    )
    return definitions


def invalidate_attribute_definitions(organization_id):
    """Invalidate one organization's attribute-definition cache."""
    if organization_id:
        cache.delete(attribute_definitions_cache_key(organization_id))
