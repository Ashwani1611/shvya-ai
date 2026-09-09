from __future__ import annotations

import uuid

from django.db import models

from apps.ai_engagement.coins import credits_to_coins


class AICreditWallet(models.Model):
    """Organization-scoped SHVYA AI credit wallet.

    AI credits are intentionally separate from Organization.credits_*.
    New wallets start at zero and are funded only by an explicit manual
    Superadmin action.

    Credits remain the accounting source of truth. User-facing wallet screens
    expose AI coins at a fixed conversion of 30 credits = 1 coin.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_credit_wallet",
    )
    balance = models.BigIntegerField(default=0)
    reserved_credits = models.PositiveBigIntegerField(default=0)
    lifetime_credits_added = models.PositiveBigIntegerField(default=0)
    lifetime_credits_used = models.PositiveBigIntegerField(default=0)
    is_blocked = models.BooleanField(default=False)
    low_credit_threshold = models.PositiveBigIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__name"]
        verbose_name = "AI Credit Wallet"
        verbose_name_plural = "AI Credit Wallets"

    @property
    def available_credits(self) -> int:
        return max(int(self.balance) - int(self.reserved_credits), 0)

    @property
    def available_coins(self):
        return credits_to_coins(self.available_credits)

    @property
    def reserved_coins(self):
        return credits_to_coins(self.reserved_credits)

    @property
    def lifetime_coins_added(self):
        return credits_to_coins(self.lifetime_credits_added)

    @property
    def lifetime_coins_used(self):
        return credits_to_coins(self.lifetime_credits_used)

    @property
    def low_coin_threshold(self):
        return credits_to_coins(self.low_credit_threshold)

    @property
    def is_low(self) -> bool:
        return 0 < self.available_credits <= int(self.low_credit_threshold)

    @property
    def can_use_ai(self) -> bool:
        return not self.is_blocked and self.available_credits > 0

    def __str__(self) -> str:
        return f"{self.organization.name}: {self.available_coins} AI coins"


class AICreditReservation(models.Model):
    """Concurrency-safe reservation made immediately before an AI call."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SETTLED = "settled", "Settled"
        RELEASED = "released", "Released"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_credit_reservations",
    )
    wallet = models.ForeignKey(
        AICreditWallet,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    feature = models.CharField(max_length=64, default="other")
    model = models.CharField(max_length=150, blank=True)
    reference_id = models.CharField(max_length=150, blank=True)
    reserved_credits = models.PositiveBigIntegerField()
    estimated_input_tokens = models.PositiveBigIntegerField(default=0)
    estimated_output_tokens = models.PositiveBigIntegerField(default=0)
    actual_credits = models.PositiveBigIntegerField(default=0)
    actual_input_tokens = models.PositiveBigIntegerField(default=0)
    actual_output_tokens = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "status", "created_at"],
                name="ai_engageme_organiz_4489d8_idx",
            ),
        ]


class AICreditTransaction(models.Model):
    """Immutable audit ledger for every settled AI-credit balance change."""

    class TransactionType(models.TextChoices):
        MANUAL_CREDIT = "manual_credit", "Manual credit"
        MANUAL_DEBIT = "manual_debit", "Manual debit"
        AI_USAGE = "ai_usage", "AI usage"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_credit_transactions",
    )
    wallet = models.ForeignKey(
        AICreditWallet,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    transaction_type = models.CharField(
        max_length=30,
        choices=TransactionType.choices,
    )
    amount = models.BigIntegerField(
        help_text="Signed credits. Positive adds balance; negative consumes it.",
    )
    balance_after = models.BigIntegerField()
    feature = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=150, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    reservation_id = models.UUIDField(null=True, blank=True)
    reference_id = models.CharField(max_length=150, blank=True)
    description = models.CharField(max_length=500, blank=True)
    actor_id = models.CharField(max_length=64, blank=True)
    actor_label = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="ai_engageme_organiz_08a8ae_idx",
            ),
            models.Index(
                fields=["organization", "transaction_type", "created_at"],
                name="ai_engageme_organiz_8cb254_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.name}: {self.amount:+d} AI credits"
