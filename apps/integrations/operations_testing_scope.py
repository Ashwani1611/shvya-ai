"""Server-owned AI test isolation; never activated by request data or lead source."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.db import models
from django.db.models import Q


@dataclass(frozen=True)
class TestScope:
    run_id: str
    organization_id: str
    lead_id: str
    account_id: str


_CURRENT = ContextVar("shvya_operations_ai_test_scope", default=None)


def current_test_scope():
    return _CURRENT.get()


@contextmanager
def owned_test_scope(run):
    """Only a persisted manifest can expose its own fixtures in this context."""
    if not getattr(run, "pk", None) or not run.fixture_lead_id or not run.fixture_account_id:
        raise ValueError("A persisted test manifest with owned fixtures is required.")
    scope = TestScope(str(run.pk), str(run.organization_id), str(run.fixture_lead_id), str(run.fixture_account_id))
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


class OperationsVisibleLeadManager(models.Manager):
    def get_queryset(self):
        query = super().get_queryset()
        scope = current_test_scope()
        ordinary = Q(is_operations_test=False)
        if scope:
            ordinary |= Q(pk=scope.lead_id, organization_id=scope.organization_id)
        return query.filter(ordinary)


class OperationsVisibleAccountManager(models.Manager):
    def get_queryset(self):
        query = super().get_queryset()
        scope = current_test_scope()
        ordinary = Q(is_operations_test=False)
        if scope:
            ordinary |= Q(pk=scope.account_id, organization_id=scope.organization_id)
        return query.filter(ordinary)


class OperationsVisibleMessageManager(models.Manager):
    def get_queryset(self):
        query = super().get_queryset()
        scope = current_test_scope()
        ordinary = Q(account__is_operations_test=False)
        if scope:
            ordinary |= Q(account_id=scope.account_id, lead_id=scope.lead_id, organization_id=scope.organization_id)
        return query.filter(ordinary)
