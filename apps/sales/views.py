# ruff: noqa: F401
"""Compatibility facade for SHVYA Sales views.

Dashboard/document, template, and public document views live in focused modules.
Historical imports and URL callables remain stable.
"""

from . import views_documents as _documents
from . import views_public as _public
from . import views_templates as _templates


_organization = _documents._organization
_document_type = _documents._document_type
_clean_optional_email = _documents._clean_optional_email
_clean_date = _documents._clean_date
_templates_for = _documents._templates_for
validate_brand_asset = _documents.validate_brand_asset

_IMPLEMENTATIONS = (_documents, _templates, _public)
_ENTRYPOINTS = frozenset(
    {
        "sales_dashboard_view",
        "sales_document_list_view",
        "sales_document_create_view",
        "sales_document_edit_view",
        "sales_document_detail_view",
        "sales_document_send_view",
        "sales_template_list_view",
        "sales_template_form_view",
        "public_document_view",
        "public_document_action_view",
    }
)


def _sync_facade_overrides():
    facade = globals()
    for module in _IMPLEMENTATIONS:
        for name in tuple(module.__dict__):
            if name in _ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def sales_dashboard_view(request):
    _sync_facade_overrides()
    return _documents.sales_dashboard_view(request)


def sales_document_list_view(request):
    _sync_facade_overrides()
    return _documents.sales_document_list_view(request)


def sales_document_create_view(request, document_type):
    _sync_facade_overrides()
    return _documents.sales_document_create_view(request, document_type)


def sales_document_edit_view(request, document_id):
    _sync_facade_overrides()
    return _documents.sales_document_edit_view(request, document_id)


def sales_document_detail_view(request, document_id):
    _sync_facade_overrides()
    return _documents.sales_document_detail_view(request, document_id)


def sales_document_send_view(request, document_id):
    _sync_facade_overrides()
    return _documents.sales_document_send_view(request, document_id)


def sales_template_list_view(request):
    _sync_facade_overrides()
    return _templates.sales_template_list_view(request)


def sales_template_form_view(request, template_id=None):
    _sync_facade_overrides()
    return _templates.sales_template_form_view(request, template_id)


def public_document_view(request, token):
    _sync_facade_overrides()
    return _public.public_document_view(request, token)


def public_document_action_view(request, token):
    _sync_facade_overrides()
    return _public.public_document_action_view(request, token)
