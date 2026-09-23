# ruff: noqa: F401,F403
"""Compatibility facade for SHVYA Sales services.

Document/template logic and delivery/provider logic live in focused modules.
Historical imports from apps.sales.services remain supported.
"""

from .document_services import *
from .document_services import (
    _decimal as _decimal,
    _items_table as _items_table,
    _money_text as _money_text,
    _sanitize_style as _sanitize_style,
)
from .delivery_services import *
from .delivery_services import (
    _attachment_payloads as _attachment_payloads,
    _delivery_row as _delivery_row,
    _mark_document_sent as _mark_document_sent,
    _record_failure as _record_failure,
    _sales_template_message as _sales_template_message,
    _sales_whatsapp_template_values as _sales_whatsapp_template_values,
)
