"""Phone normalization for Free organisation accounts."""
import re

from django.core.exceptions import ValidationError


def normalize_free_phone(value):
    """Return +91 followed by exactly ten ASCII digits."""
    phone = str(value or "").strip()
    if not re.fullmatch(r"(?:\+91)?[0-9]{10}", phone):
        raise ValidationError("Enter a 10-digit Indian phone number, optionally prefixed with +91.")
    return phone if phone.startswith("+91") else f"+91{phone}"
