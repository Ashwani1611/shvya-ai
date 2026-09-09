from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from apps.ai_engagement.coins import (
    AI_CREDITS_PER_COIN,
    coins_to_credits,
    credits_to_coins,
    format_coins,
)
from apps.ai_engagement.models import (
    AICreditTransaction,
    AICreditWallet,
)
from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
)
from apps.organizations.models import Organization

from .views_flat import superuser_required


AI_FEATURE_LABELS = {
    "engagement": "AI Engagement",
    "qualification": "Qualification",
    "internal_summary": "Conversation Summary",
    "knowledge_embedding": "Knowledge Embedding",
    "knowledge_retrieval": "Knowledge Retrieval",
    "playground": "AI Playground",
    "playground_retrieval": "Playground Retrieval",
    "embedding": "Knowledge Embedding",
    "other": "Other AI Usage",
}

USAGE_RANGE_PRESETS = {"today", "7d", "30d", "month", "all", "custom"}


def _feature_label(feature: str) -> str:
    normalized = (feature or "other").strip().lower()
    return AI_FEATURE_LABELS.get(
        normalized,
        normalized.replace("_", " ").strip().title() or "Other AI Usage",
    )


def _resolve_usage_date_filter(request):
    """Resolve quick presets or an explicit inclusive date range."""

    today = timezone.localdate()
    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    preset = (request.GET.get("range") or "30d").strip().lower()
    if preset not in USAGE_RANGE_PRESETS:
        preset = "30d"

    start_date = parse_date(raw_from) if raw_from else None
    end_date = parse_date(raw_to) if raw_to else None

    if raw_from or raw_to:
        preset = "custom"
    elif preset == "today":
        start_date = today
        end_date = today
    elif preset == "7d":
        start_date = today - timedelta(days=6)
        end_date = today
    elif preset == "30d":
        start_date = today - timedelta(days=29)
        end_date = today
    elif preset == "month":
        start_date = today.replace(day=1)
        end_date = today
    elif preset == "all":
        start_date = None
        end_date = None

    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    if start_date and end_date:
        if start_date == end_date:
            label = start_date.strftime("%d %b %Y")
        else:
            label = (
                f"{start_date.strftime('%d %b %Y')} – "
                f"{end_date.strftime('%d %b %Y')}"
            )
    elif start_date:
        label = f"From {start_date.strftime('%d %b %Y')}"
    elif end_date:
        label = f"Through {end_date.strftime('%d %b %Y')}"
    else:
        label = "All time"

    return {
        "preset": preset,
        "start_date": start_date,
        "end_date": end_date,
        "start_value": start_date.isoformat() if start_date else "",
        "end_value": end_date.isoformat() if end_date else "",
        "label": label,
    }


def _filter_transactions_for_dates(queryset, date_filter):
    start_date = date_filter["start_date"]
    end_date = date_filter["end_date"]

    if start_date:
        queryset = queryset.filter(created_at__date__gte=start_date)
    if end_date:
        queryset = queryset.filter(created_at__date__lte=end_date)
    return queryset


def _build_usage_report(transactions):
    """Build coin summaries while preserving exact credit-ledger totals."""

    credits_added = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.MANUAL_CREDIT,
    ).aggregate(total=Sum("amount"))["total"] or 0
    manual_debits = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.MANUAL_DEBIT,
    ).aggregate(total=Sum("amount"))["total"] or 0
    ai_usage_total = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
    ).aggregate(total=Sum("amount"))["total"] or 0
    net_change = transactions.aggregate(total=Sum("amount"))["total"] or 0

    usage_rows = []
    grouped_usage = (
        transactions.filter(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
        )
        .values("feature")
        .annotate(total=Sum("amount"))
        .order_by("feature")
    )
    for row in grouped_usage:
        amount = int(row["total"] or 0)
        if amount == 0:
            continue
        usage_rows.append(
            {
                "feature": row["feature"] or "other",
                "label": _feature_label(row["feature"]),
                "amount": amount,
                "signed_display": format_coins(amount, signed=True),
                "credits_signed_display": f"{amount:+,}",
            }
        )

    usage_rows.sort(key=lambda row: abs(row["amount"]), reverse=True)

    credits_added = int(credits_added)
    manual_debits = int(manual_debits)
    ai_usage_total = int(ai_usage_total)
    net_change = int(net_change)

    return {
        "credits_added": max(credits_added, 0),
        "manual_deducted": max(-manual_debits, 0),
        "ai_used": max(-ai_usage_total, 0),
        "net_change": net_change,
        "credits_added_display": format_coins(max(credits_added, 0)),
        "manual_deducted_display": format_coins(max(-manual_debits, 0)),
        "ai_used_display": format_coins(max(-ai_usage_total, 0)),
        "net_change_display": format_coins(net_change, signed=True),
        "credits_added_credits_display": f"{max(credits_added, 0):,}",
        "manual_deducted_credits_display": f"{max(-manual_debits, 0):,}",
        "ai_used_credits_display": f"{max(-ai_usage_total, 0):,}",
        "net_change_credits_display": f"{net_change:+,}",
        "usage_rows": usage_rows,
    }


def _coin_wallet_context(wallet: AICreditWallet) -> dict:
    """Expose the same wallet health state with values expressed in AI coins."""

    return {
        "available_credits": credits_to_coins(wallet.available_credits),
        "reserved_credits": credits_to_coins(wallet.reserved_credits),
        "lifetime_credits_added": credits_to_coins(wallet.lifetime_credits_added),
        "lifetime_credits_used": credits_to_coins(wallet.lifetime_credits_used),
        "low_credit_threshold": credits_to_coins(wallet.low_credit_threshold),
        "is_blocked": wallet.is_blocked,
        "is_low": wallet.is_low,
    }


def _coin_reservation_context(reservation) -> dict:
    return {
        "created_at": reservation.created_at,
        "feature": reservation.feature,
        "model": reservation.model,
        "reserved_credits": credits_to_coins(reservation.reserved_credits),
        "reference_id": reservation.reference_id,
    }


@superuser_required
@require_http_methods(["GET"])
def ai_credit_overview_view(request):
    """Show one manually-funded AI wallet for every organization."""

    search = (request.GET.get("search") or "").strip()

    organizations = Organization.objects.all()
    if search:
        organizations = organizations.filter(
            Q(name__icontains=search)
            | Q(users__email__icontains=search)
        ).distinct()

    organization_ids = list(organizations.values_list("id", flat=True))
    existing_ids = set(
        AICreditWallet.objects.filter(
            organization_id__in=organization_ids,
        ).values_list("organization_id", flat=True)
    )
    missing = [
        AICreditWallet(organization_id=organization_id, balance=0)
        for organization_id in organization_ids
        if organization_id not in existing_ids
    ]
    if missing:
        AICreditWallet.objects.bulk_create(missing, ignore_conflicts=True)

    organizations = (
        organizations
        .select_related("ai_credit_wallet")
        .order_by("-created_at")
    )

    return render(
        request,
        "superadmin/ai_credit_overview.html",
        {
            "organizations": organizations,
            "search": search,
            "credits_per_coin": AI_CREDITS_PER_COIN,
        },
    )


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_ai_credit_view(request, organization_id):
    """Manage one organization's AI wallet in coins; audit usage in credits."""

    organization = get_object_or_404(Organization, pk=organization_id)
    wallet = AICreditService.ensure_wallet(organization)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "add":
                credits = coins_to_credits(request.POST.get("amount"))
                if credits <= 0:
                    raise ValueError("AI coin amount must fund at least one credit.")
                coins = credits_to_coins(credits)
                reason = request.POST.get("reason") or ""
                AICreditService.add_manual_credits(
                    organization=organization,
                    amount=credits,
                    reason=reason,
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Added {coins:,.2f} AI coins to {organization.name} "
                    f"({credits:,} internal credits).",
                )

            elif action == "deduct":
                credits = coins_to_credits(request.POST.get("amount"))
                if credits <= 0:
                    raise ValueError("AI coin amount must deduct at least one credit.")
                coins = credits_to_coins(credits)
                if credits > wallet.available_credits:
                    raise AICreditError(
                        "Cannot deduct more than the organization's available AI coins."
                    )
                reason = request.POST.get("reason") or ""
                AICreditService.deduct_manual_credits(
                    organization=organization,
                    amount=credits,
                    reason=reason,
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Deducted {coins:,.2f} AI coins from {organization.name} "
                    f"({credits:,} internal credits).",
                )

            elif action == "block":
                blocked = (request.POST.get("blocked") or "") == "1"
                AICreditService.set_blocked(
                    organization=organization,
                    blocked=blocked,
                )
                messages.success(
                    request,
                    (
                        "AI usage blocked for this organization."
                        if blocked
                        else "AI usage unblocked for this organization."
                    ),
                )

            elif action == "threshold":
                threshold_credits = coins_to_credits(request.POST.get("threshold"))
                threshold_coins = credits_to_coins(threshold_credits)
                AICreditService.set_low_credit_threshold(
                    organization=organization,
                    threshold=threshold_credits,
                )
                messages.success(
                    request,
                    f"Low-balance threshold updated to {threshold_coins:,.2f} AI coins.",
                )

            else:
                raise AICreditError("Unknown AI coin action.")

        except (TypeError, ValueError):
            messages.error(request, "Enter a valid AI coin amount.")
        except AICreditError as exc:
            messages.error(request, str(exc))

        return redirect(
            "superadmin-organization-ai-credits",
            organization_id=organization.id,
        )

    wallet.refresh_from_db()
    now = timezone.now()
    usage = wallet.transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
    )
    today_total = usage.filter(
        created_at__date=timezone.localdate(),
    ).aggregate(total=Sum("amount"))["total"] or 0
    month_total = usage.filter(
        created_at__year=now.year,
        created_at__month=now.month,
    ).aggregate(total=Sum("amount"))["total"] or 0

    date_filter = _resolve_usage_date_filter(request)
    filtered_transactions = _filter_transactions_for_dates(
        wallet.transactions.all(),
        date_filter,
    )
    usage_report = _build_usage_report(filtered_transactions)

    transactions = filtered_transactions[:100]
    active_reservations = [
        _coin_reservation_context(reservation)
        for reservation in wallet.reservations.filter(
            status="active",
        ).order_by("-created_at")[:25]
    ]

    return render(
        request,
        "superadmin/ai_credit_detail.html",
        {
            "organization": organization,
            "wallet": _coin_wallet_context(wallet),
            "transactions": transactions,
            "active_reservations": active_reservations,
            "used_today": credits_to_coins(max(-int(today_total), 0)),
            "used_this_month": credits_to_coins(max(-int(month_total), 0)),
            "date_filter": date_filter,
            "usage_report": usage_report,
            "credits_per_coin": AI_CREDITS_PER_COIN,
        },
    )
