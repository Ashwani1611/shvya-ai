from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


AI_CREDITS_PER_COIN = 30
COIN_DISPLAY_QUANTUM = Decimal("0.01")


def credits_to_coins(credits: int | None) -> Decimal:
    """Convert internal AI credits to the user-facing AI coin unit.

    Credits remain the source-of-truth accounting unit because provider usage can
    consume a single credit. The coin value is presentation/billing-facing only:
    30 internal credits = 1 AI coin.
    """

    raw_credits = int(credits or 0)
    return (
        Decimal(raw_credits) / Decimal(AI_CREDITS_PER_COIN)
    ).quantize(COIN_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)


def format_coins(credits: int | None, *, signed: bool = False) -> str:
    value = credits_to_coins(credits)
    if signed:
        return f"{value:+,.2f}"
    return f"{value:,.2f}"


def coins_to_credits(coins: int | str | None) -> int:
    """Convert a whole-number Superadmin coin amount to internal credits."""

    try:
        amount = int(coins or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid whole-number AI coin amount.") from exc

    if amount < 0:
        raise ValueError("AI coin amount cannot be negative.")
    return amount * AI_CREDITS_PER_COIN
