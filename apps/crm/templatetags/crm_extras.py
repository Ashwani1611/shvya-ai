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

@register.simple_tag
def lead_note_count(lead):
    notes = list(lead.lead_notes.all())
    legacy = (lead.notes or "").strip()
    return len(notes) + int(bool(legacy) and not any((n.note or "").strip() == legacy for n in notes))


@register.simple_tag
def lead_creation_source(activity, lead):
    labels = {"system": "System", "whatsapp_api": "WhatsApp API", "whatsapp": "WhatsApp",
              "instagram": "Instagram", "csv_import": "CSV Import", "meta_ads": "Meta Ads",
              "google_sheets": "Google Sheet", "external_api": "External API"}
    details = activity.details if isinstance(activity.details, dict) else {}
    source = details.get("lead_source") or lead.lead_source
    return labels.get(source, "Unknown")


@register.simple_tag
def lead_avatar(lead):
    """A stable, local portrait. User text never enters the SVG markup."""
    import hashlib
    from django.utils.safestring import mark_safe
    seed = hashlib.sha256(str(lead.pk).encode()).digest()
    backgrounds = ["#bce9ff", "#ffd5e5", "#d7ceff", "#c8f5de", "#ffe2a8", "#bdd9ff"]
    skin = ["#f5cba7", "#d99b72", "#965d45", "#603e31", "#ffe0bd"][seed[1] % 5]
    hair = ["#302a40", "#693c28", "#e5a735", "#7745ae", "#203e59"][seed[2] % 5]
    shirt = ["#6952dd", "#087fa0", "#f07592", "#da8738", "#286e53"][seed[3] % 5]
    styles = [
        '<path d="M16 28Q10 5 33 9Q51 5 49 31L43 22Q28 28 22 19L20 32Z"/>',
        '<path d="M13 49V27Q12 7 32 8Q53 7 51 28V50L42 43V24Q30 23 25 16L21 29V45Z"/>',
        '<path d="M16 27Q8 20 18 14Q15 4 27 8Q34 0 39 9Q54 5 50 20L47 28L40 19L21 23Z"/>',
        '<path d="M16 29Q11 8 31 9Q56 6 48 29L43 22L21 22Z"/><circle cx="39" cy="9" r="8"/>',
    ]
    glasses = '<g fill="none" stroke="#302a40" stroke-width="2"><rect x="21" y="28" width="9" height="7" rx="3"/><rect x="34" y="28" width="9" height="7" rx="3"/><path d="M30 30h4"/></g>' if seed[5] % 3 == 0 else ''
    return mark_safe(f'<svg class="lead-cartoon-avatar" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="44" height="44" aria-hidden="true"><rect width="64" height="64" rx="18" fill="{backgrounds[seed[0]%6]}"/><circle cx="52" cy="12" r="14" fill="white" opacity=".25"/><path d="M8 64Q8 45 32 45Q56 45 56 64" fill="{shirt}"/><g fill="{hair}">{styles[seed[4]%4]}</g><path d="M27 40h10v10q-5 6-10 0" fill="{skin}"/><ellipse cx="32" cy="31" rx="14" ry="17" fill="{skin}"/><g fill="{hair}"><path d="M18 25Q13 9 32 10Q48 8 47 24Q31 19 26 17Q23 25 18 25Z"/></g><g fill="#302a40"><circle cx="26" cy="31" r="1.6"/><circle cx="38" cy="31" r="1.6"/></g><path d="M28 39q4 4 8 0" fill="none" stroke="#874c44" stroke-width="2" stroke-linecap="round"/>{glasses}</svg>')
