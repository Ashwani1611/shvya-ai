from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.ai_engagement.models import AICreditWallet
from apps.core.context_processors import (
    AI_CREDIT_ALERT_THRESHOLD,
    _ai_credit_context,
)
from apps.organizations.models import Organization


pytestmark = pytest.mark.django_db


def _request_for(organization=None):
    if organization is None:
        return SimpleNamespace(crm_user=None)
    return SimpleNamespace(
        crm_user=SimpleNamespace(
            organization_id=organization.id,
            organization=organization,
        )
    )


def test_ai_credit_context_is_hidden_without_an_organization():
    context = _ai_credit_context(_request_for())

    assert context["ai_credit_balance"] is None
    assert context["ai_credit_is_low"] is False
    assert context["ai_credit_alert_threshold"] == AI_CREDIT_ALERT_THRESHOLD
    assert context["ai_coin_balance"] is None
    assert context["ai_coin_total"] is None
    assert context["ai_coin_alert_threshold"] == Decimal("16.67")


def test_ai_coin_context_shows_zero_for_organization_without_wallet():
    organization = Organization.objects.create(name="No Wallet Organization")

    context = _ai_credit_context(_request_for(organization))

    assert context["ai_credit_balance"] == 0
    assert context["ai_coin_balance"] == Decimal("0.00")
    assert context["ai_coin_total"] == Decimal("0.00")
    assert context["ai_coin_is_low"] is True


def test_ai_coin_context_uses_spendable_balance_after_reservations():
    organization = Organization.objects.create(name="Reserved Credits Organization")
    AICreditWallet.objects.create(
        organization=organization,
        balance=1000,
        reserved_credits=200,
        lifetime_credits_added=1200,
    )

    context = _ai_credit_context(_request_for(organization))

    assert context["ai_credit_balance"] == 800
    assert context["ai_coin_balance"] == Decimal("26.67")
    assert context["ai_coin_total"] == Decimal("40.00")
    assert context["ai_coin_is_low"] is False


def test_ai_coin_context_alerts_at_exactly_500_available_credits():
    organization = Organization.objects.create(name="Threshold Organization")
    AICreditWallet.objects.create(
        organization=organization,
        balance=650,
        reserved_credits=150,
    )

    context = _ai_credit_context(_request_for(organization))

    assert context["ai_credit_balance"] == 500
    assert context["ai_coin_balance"] == Decimal("16.67")
    assert context["ai_coin_is_low"] is True


def test_ai_coin_context_does_not_alert_above_threshold():
    organization = Organization.objects.create(name="Healthy Credit Organization")
    AICreditWallet.objects.create(
        organization=organization,
        balance=501,
        reserved_credits=0,
    )

    context = _ai_credit_context(_request_for(organization))

    assert context["ai_credit_balance"] == 501
    assert context["ai_coin_balance"] == Decimal("16.70")
    assert context["ai_coin_is_low"] is False
