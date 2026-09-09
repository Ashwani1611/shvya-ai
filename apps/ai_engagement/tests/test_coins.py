from decimal import Decimal

import pytest

from apps.ai_engagement.coins import (
    AI_CREDITS_PER_COIN,
    coins_to_credits,
    credits_to_coins,
    format_coins,
)


def test_coin_conversion_rate_is_fixed_at_thirty_credits():
    assert AI_CREDITS_PER_COIN == 30
    assert coins_to_credits("1") == 30
    assert coins_to_credits("2") == 60
    assert credits_to_coins(30) == Decimal("1.00")
    assert credits_to_coins(45) == Decimal("1.50")


def test_fractional_coin_admin_amounts_round_to_whole_internal_credits():
    assert coins_to_credits("1.50") == 45
    assert coins_to_credits("0.10") == 3
    assert credits_to_coins(1) == Decimal("0.03")


def test_coin_formatting_is_consistent_for_superadmin_summaries():
    assert format_coins(300) == "10.00"
    assert format_coins(-18, signed=True) == "-0.60"
    assert format_coins(30, signed=True) == "+1.00"


def test_negative_coin_amount_is_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        coins_to_credits("-1")
