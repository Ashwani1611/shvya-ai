from django import template
from apps.telephony.analytics import format_duration

register = template.Library()
register.filter("call_duration", format_duration)
