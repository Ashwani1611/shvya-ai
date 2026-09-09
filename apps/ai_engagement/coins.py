from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


AI_CREDITS_PER_COIN = 30
COIN_DISPLAY_QUANTUM = Decimal("0.01")
CREDIT_QUANTUM = Decimal("1")


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


def coins_to_credits(coins: int | float | str | Decimal | None) -> int:
    """Convert a Superadmin coin amount to the nearest internal whole credit.

    Fractional coins are accepted because live provider usage can leave a wallet
    between whole-coin boundaries. Credits themselves stay indivisible and are
    always written to the ledger as integers.
    """

    try:
        amount = Decimal(str(coins or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Enter a valid AI coin amount.") from exc

    if not amount.is_finite():
        raise ValueError("Enter a valid AI coin amount.")
    if amount < 0:
        raise ValueError("AI coin amount cannot be negative.")

    credits = (amount * Decimal(AI_CREDITS_PER_COIN)).quantize(
        CREDIT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return int(credits)
