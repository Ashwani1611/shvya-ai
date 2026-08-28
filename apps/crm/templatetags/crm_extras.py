from django import template

register = template.Library()


@register.filter
def split(value, sep):
    return value.split(sep)


@register.filter
def format_duration(value):
    """
    Convert seconds into M:SS min format.

    Examples:
        0   -> 0:00 min
        5   -> 0:05 min
        65  -> 1:05 min
        151 -> 2:31 min
    """

    try:
        total_seconds = int(value or 0)
    except (TypeError, ValueError):
        return "0:00 min"

    if total_seconds < 0:
        total_seconds = 0

    minutes = total_seconds // 60
    seconds = total_seconds % 60

    return f"{minutes}:{seconds:02d} min"

@register.filter
def get_item(value, key):
    """
    Return a dictionary value by key.

    Safely returns an empty string when the value is not
    a dictionary or the key does not exist.
    """

    if not isinstance(value, dict):
        return ""

    return value.get(
        key,
        "",
    )    