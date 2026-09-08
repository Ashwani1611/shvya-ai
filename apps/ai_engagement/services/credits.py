from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.models.credit import (
    AICreditReservation,
    AICreditTransaction,
    AICreditWallet,
)
from apps.organizations.models import Organization


class AICreditError(Exception):
    """Base exception for AI-credit failures."""


class AICreditUnavailableError(AICreditError):
    """Raised before an AI request when usable credits are unavailable."""


class AICreditSettlementError(AICreditError):
    """Raised when a completed provider request cannot be settled safely."""


@dataclass(frozen=True)
class AICreditStatus:
    balance: int
    reserved: int
    available: int
    blocked: bool
    low_credit_threshold: int

    @property
    def can_use_ai(self) -> bool:
        return not self.blocked and self.available > 0


class AICreditService:
    """Central manual organization AI-credit wallet service.

    Design rules:
      * every organization starts with zero AI credits
      * only explicit Superadmin actions add credits
      * no package/plan/monthly allocation exists here
      * provider calls reserve credits first to prevent concurrent overspend
      * successful calls settle against actual provider token usage when present
      * failed provider calls release their reservation
      * balance and generic Organization.credits_* fields are independent
    """

    DEFAULT_TEXT_RATE = {
        "input_per_1k": 1,
        "output_per_1k": 4,
    }
    DEFAULT_EMBEDDING_RATE = {
        "input_per_1k": 1,
        "output_per_1k": 0,
    }
    DEFAULT_RESERVED_OUTPUT_TOKENS = 1000

    @classmethod
    def _positive_int(cls, value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default

    @classmethod
    def _configured_rates(cls) -> dict[str, dict[str, int]]:
        """Read optional model-specific internal credit rates from env.

        Example:
        AI_CREDIT_MODEL_RATES_JSON={"gpt-4.1-nano":{"input_per_1k":1,"output_per_1k":4}}
        """

        raw = (os.getenv("AI_CREDIT_MODEL_RATES_JSON", "") or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}

        normalized: dict[str, dict[str, int]] = {}
        for model, rates in payload.items():
            if not isinstance(model, str) or not isinstance(rates, dict):
                continue
            normalized[model] = {
                "input_per_1k": cls._positive_int(rates.get("input_per_1k"), 0),
                "output_per_1k": cls._positive_int(rates.get("output_per_1k"), 0),
            }
        return normalized

    @classmethod
    def rate_for(cls, *, model: str, embedding: bool = False) -> dict[str, int]:
        configured = cls._configured_rates().get(model)
        if configured is not None:
            return configured
        return dict(cls.DEFAULT_EMBEDDING_RATE if embedding else cls.DEFAULT_TEXT_RATE)

    @classmethod
    def reserved_output_tokens(cls) -> int:
        return max(
            cls._positive_int(
                os.getenv("AI_CREDIT_RESERVED_OUTPUT_TOKENS"),
                cls.DEFAULT_RESERVED_OUTPUT_TOKENS,
            ),
            1,
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Conservative provider-independent preflight estimate.

        Exact settlement uses provider-reported usage. The estimate is only used
        to reserve enough credits before the network request.
        """

        normalized = text or ""
        if not normalized:
            return 1
        return max(1, math.ceil(len(normalized) / 4))

    @staticmethod
    def _bucket_charge(tokens: int, credits_per_1k: int) -> int:
        if tokens <= 0 or credits_per_1k <= 0:
            return 0
        return math.ceil(tokens / 1000) * credits_per_1k

    @classmethod
    def calculate_charge(
        cls,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int = 0,
        embedding: bool = False,
    ) -> int:
        rates = cls.rate_for(model=model, embedding=embedding)
        charge = cls._bucket_charge(input_tokens, rates["input_per_1k"])
        charge += cls._bucket_charge(output_tokens, rates["output_per_1k"])
        # Every successful metered provider call consumes at least one credit.
        return max(charge, 1)

    @staticmethod
    def feature_from_metadata(metadata: dict[str, str] | None) -> str:
        metadata = metadata or {}
        raw = (
            metadata.get("task")
            or metadata.get("purpose")
            or metadata.get("feature")
            or "other"
        )
        value = str(raw).strip().lower().replace(" ", "_")
        aliases = {
            "lead_qualification_summary": "qualification",
            "internal_conversation_summary": "internal_summary",
            "bump_up": "engagement",
        }
        return aliases.get(value, value[:64] or "other")

    @staticmethod
    def reference_from_metadata(metadata: dict[str, str] | None) -> str:
        metadata = metadata or {}
        value = metadata.get("lead_id") or metadata.get("document_id") or ""
        return str(value)[:150]

    @staticmethod
    def _actor_fields(actor) -> tuple[str, str]:
        if actor is None:
            return "", ""
        actor_id = str(getattr(actor, "id", "") or "")[:64]
        label = (
            getattr(actor, "email", "")
            or getattr(actor, "username", "")
            or str(actor)
        )
        return actor_id, str(label)[:255]

    @classmethod
    def ensure_wallet(cls, organization: Organization | str) -> AICreditWallet:
        organization_id = getattr(organization, "id", organization)
        wallet, _ = AICreditWallet.objects.get_or_create(
            organization_id=organization_id,
            defaults={"balance": 0},
        )
        return wallet

    @classmethod
    def status(cls, organization: Organization | str) -> AICreditStatus:
        wallet = cls.ensure_wallet(organization)
        return AICreditStatus(
            balance=int(wallet.balance),
            reserved=int(wallet.reserved_credits),
            available=wallet.available_credits,
            blocked=bool(wallet.is_blocked),
            low_credit_threshold=int(wallet.low_credit_threshold),
        )

    @classmethod
    def assert_available(cls, organization: Organization | str) -> None:
        status = cls.status(organization)
        if status.blocked:
            raise AICreditUnavailableError(
                "AI usage is blocked for this organization by Superadmin."
            )
        if status.available <= 0:
            raise AICreditUnavailableError(
                "This organization has no available AI credits."
            )

    @classmethod
    @transaction.atomic
    def add_manual_credits(
        cls,
        *,
        organization: Organization,
        amount: int,
        reason: str,
        actor=None,
    ) -> AICreditWallet:
        amount = int(amount)
        if amount <= 0:
            raise AICreditError("Credit amount must be greater than zero.")
        reason = (reason or "").strip()
        if not reason:
            raise AICreditError("A reason is required when adding AI credits.")

        wallet = cls.ensure_wallet(organization)
        wallet = AICreditWallet.objects.select_for_update().get(pk=wallet.pk)
        wallet.balance += amount
        wallet.lifetime_credits_added += amount
        wallet.save(
            update_fields=[
                "balance",
                "lifetime_credits_added",
                "updated_at",
            ]
        )
        actor_id, actor_label = cls._actor_fields(actor)
        AICreditTransaction.objects.create(
            organization=organization,
            wallet=wallet,
            transaction_type=AICreditTransaction.TransactionType.MANUAL_CREDIT,
            amount=amount,
            balance_after=wallet.balance,
            description=reason[:500],
            actor_id=actor_id,
            actor_label=actor_label,
        )
        return wallet

    @classmethod
    @transaction.atomic
    def deduct_manual_credits(
        cls,
        *,
        organization: Organization,
        amount: int,
        reason: str,
        actor=None,
    ) -> AICreditWallet:
        amount = int(amount)
        if amount <= 0:
            raise AICreditError("Debit amount must be greater than zero.")
        reason = (reason or "").strip()
        if not reason:
            raise AICreditError("A reason is required when deducting AI credits.")

        wallet = cls.ensure_wallet(organization)
        wallet = AICreditWallet.objects.select_for_update().get(pk=wallet.pk)
        if amount > wallet.available_credits:
            raise AICreditError(
                "Cannot deduct more than the organization's available AI credits."
            )
        wallet.balance -= amount
        wallet.save(update_fields=["balance", "updated_at"])
        actor_id, actor_label = cls._actor_fields(actor)
        AICreditTransaction.objects.create(
            organization=organization,
            wallet=wallet,
            transaction_type=AICreditTransaction.TransactionType.MANUAL_DEBIT,
            amount=-amount,
            balance_after=wallet.balance,
            description=reason[:500],
            actor_id=actor_id,
            actor_label=actor_label,
        )
        return wallet

    @classmethod
    @transaction.atomic
    def set_blocked(
        cls,
        *,
        organization: Organization,
        blocked: bool,
    ) -> AICreditWallet:
        wallet = cls.ensure_wallet(organization)
        wallet = AICreditWallet.objects.select_for_update().get(pk=wallet.pk)
        wallet.is_blocked = bool(blocked)
        wallet.save(update_fields=["is_blocked", "updated_at"])
        return wallet

    @classmethod
    @transaction.atomic
    def set_low_credit_threshold(
        cls,
        *,
        organization: Organization,
        threshold: int,
    ) -> AICreditWallet:
        threshold = int(threshold)
        if threshold < 0:
            raise AICreditError("Low-credit threshold cannot be negative.")
        wallet = cls.ensure_wallet(organization)
        wallet = AICreditWallet.objects.select_for_update().get(pk=wallet.pk)
        wallet.low_credit_threshold = threshold
        wallet.save(update_fields=["low_credit_threshold", "updated_at"])
        return wallet

    @classmethod
    def reserve_text(
        cls,
        *,
        organization_id,
        model: str,
        instructions: str,
        input_text: str,
        feature: str,
        reference_id: str = "",
    ) -> AICreditReservation:
        estimated_input = cls.estimate_tokens(
            f"{instructions or ''}\n{input_text or ''}"
        )
        estimated_output = cls.reserved_output_tokens()
        credits = cls.calculate_charge(
            model=model,
            input_tokens=estimated_input,
            output_tokens=estimated_output,
            embedding=False,
        )
        return cls._reserve(
            organization_id=organization_id,
            model=model,
            feature=feature,
            reference_id=reference_id,
            credits=credits,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        )

    @classmethod
    def reserve_embedding(
        cls,
        *,
        organization_id,
        model: str,
        texts: list[str],
        feature: str = "knowledge_embedding",
        reference_id: str = "",
    ) -> AICreditReservation:
        estimated_input = sum(cls.estimate_tokens(text) for text in texts)
        credits = cls.calculate_charge(
            model=model,
            input_tokens=estimated_input,
            output_tokens=0,
            embedding=True,
        )
        return cls._reserve(
            organization_id=organization_id,
            model=model,
            feature=feature,
            reference_id=reference_id,
            credits=credits,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=0,
        )

    @classmethod
    @transaction.atomic
    def _reserve(
        cls,
        *,
        organization_id,
        model: str,
        feature: str,
        reference_id: str,
        credits: int,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
    ) -> AICreditReservation:
        wallet = cls.ensure_wallet(organization_id)
        wallet = AICreditWallet.objects.select_for_update().get(pk=wallet.pk)
        if wallet.is_blocked:
            raise AICreditUnavailableError(
                "AI usage is blocked for this organization by Superadmin."
            )
        if wallet.available_credits <= 0:
            raise AICreditUnavailableError(
                "This organization has no available AI credits."
            )
        if credits > wallet.available_credits:
            raise AICreditUnavailableError(
                "This organization does not have enough available AI credits "
                "for the requested AI operation."
            )

        wallet.reserved_credits += credits
        wallet.save(update_fields=["reserved_credits", "updated_at"])
        return AICreditReservation.objects.create(
            organization_id=organization_id,
            wallet=wallet,
            feature=(feature or "other")[:64],
            model=(model or "")[:150],
            reference_id=(reference_id or "")[:150],
            reserved_credits=credits,
            estimated_input_tokens=max(int(estimated_input_tokens), 0),
            estimated_output_tokens=max(int(estimated_output_tokens), 0),
        )

    @classmethod
    @transaction.atomic
    def settle(
        cls,
        *,
        reservation: AICreditReservation | str,
        input_tokens: int | None,
        output_tokens: int | None = 0,
        embedding: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> AICreditReservation:
        reservation_id = getattr(reservation, "id", reservation)
        locked = (
            AICreditReservation.objects.select_for_update()
            .select_related("wallet", "organization")
            .get(pk=reservation_id)
        )
        if locked.status != AICreditReservation.Status.ACTIVE:
            return locked

        wallet = AICreditWallet.objects.select_for_update().get(pk=locked.wallet_id)
        actual_input = (
            max(int(input_tokens), 0)
            if input_tokens is not None
            else int(locked.estimated_input_tokens)
        )
        actual_output = (
            max(int(output_tokens), 0)
            if output_tokens is not None
            else int(locked.estimated_output_tokens)
        )

        if input_tokens is None:
            actual_charge = int(locked.reserved_credits)
        else:
            actual_charge = cls.calculate_charge(
                model=locked.model,
                input_tokens=actual_input,
                output_tokens=actual_output,
                embedding=embedding,
            )

        wallet.reserved_credits = max(
            int(wallet.reserved_credits) - int(locked.reserved_credits),
            0,
        )
        # Provider usage has already happened. If actual usage exceeded the
        # conservative reservation, record the overage rather than losing the
        # charge. A non-positive balance then prevents the next AI call.
        wallet.balance -= actual_charge
        wallet.lifetime_credits_used += actual_charge
        wallet.save(
            update_fields=[
                "reserved_credits",
                "balance",
                "lifetime_credits_used",
                "updated_at",
            ]
        )

        locked.actual_credits = actual_charge
        locked.actual_input_tokens = actual_input
        locked.actual_output_tokens = actual_output
        locked.status = AICreditReservation.Status.SETTLED
        locked.settled_at = timezone.now()
        locked.save(
            update_fields=[
                "actual_credits",
                "actual_input_tokens",
                "actual_output_tokens",
                "status",
                "settled_at",
            ]
        )

        AICreditTransaction.objects.create(
            organization=locked.organization,
            wallet=wallet,
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-actual_charge,
            balance_after=wallet.balance,
            feature=locked.feature,
            model=locked.model,
            input_tokens=actual_input,
            output_tokens=actual_output,
            reservation_id=locked.id,
            reference_id=locked.reference_id,
            description=f"AI usage: {locked.feature}"[:500],
            metadata=metadata or {},
        )
        return locked

    @classmethod
    @transaction.atomic
    def release(cls, reservation: AICreditReservation | str | None) -> None:
        if reservation is None:
            return
        reservation_id = getattr(reservation, "id", reservation)
        locked = AICreditReservation.objects.select_for_update().get(pk=reservation_id)
        if locked.status != AICreditReservation.Status.ACTIVE:
            return
        wallet = AICreditWallet.objects.select_for_update().get(pk=locked.wallet_id)
        wallet.reserved_credits = max(
            int(wallet.reserved_credits) - int(locked.reserved_credits),
            0,
        )
        wallet.save(update_fields=["reserved_credits", "updated_at"])
        locked.status = AICreditReservation.Status.RELEASED
        locked.settled_at = timezone.now()
        locked.save(update_fields=["status", "settled_at"])

    @staticmethod
    def extract_usage(response) -> tuple[int | None, int | None]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None, None
        if isinstance(usage, dict):
            return usage.get("input_tokens"), usage.get("output_tokens")
        return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
