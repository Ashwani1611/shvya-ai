"""Durable Hosted WhatsApp shard selection and callback fencing."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from decouple import config
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime


DEFAULT_SHARD = "primary"


def configured_gateways():
    """Return a shard -> URL map while preserving the legacy single URL."""
    raw = str(config("WHATSAPP_WEB_GATEWAYS", default="") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = dict(
                item.split("=", 1)
                for item in raw.split(",")
                if "=" in item
            )
        gateways = {
            str(shard).strip(): str(url).strip().rstrip("/")
            for shard, url in dict(parsed).items()
            if str(shard).strip() and str(url).strip()
        }
        if gateways:
            return gateways
    return {
        DEFAULT_SHARD: str(
            config(
                "WHATSAPP_WEB_GATEWAY_URL",
                default="http://whatsapp-web-gateway:3000",
            )
        ).rstrip("/")
    }


def _rendezvous_shard(account_id, shards):
    return max(
        sorted(shards),
        key=lambda shard: hashlib.sha256(
            f"{account_id}:{shard}".encode("utf-8")
        ).digest(),
    )


def gateway_shard_for_account(account, *, persist=True):
    gateways = configured_gateways()
    if account.hosted_gateway_shard in gateways:
        return account.hosted_gateway_shard
    if account.hosted_gateway_shard:
        raise ValueError(
            "The account is assigned to a Hosted gateway shard that is not configured. "
            "Use the controlled rebalance operation after its lease expires."
        )
    selected = _rendezvous_shard(account.id, gateways.keys())
    if persist:
        from apps.channels.models import WhatsAppAccount

        WhatsAppAccount.objects.filter(
            pk=account.pk,
            organization_id=account.organization_id,
            connection_type="hosted",
            hosted_gateway_shard="",
        ).update(hosted_gateway_shard=selected)
        account.refresh_from_db(fields=["hosted_gateway_shard"])
        if account.hosted_gateway_shard in gateways:
            return account.hosted_gateway_shard
    return selected


def gateway_client_for_account(account, *, client_class=None):
    if client_class is None:
        from apps.channels.providers.whatsapp_web import WhatsAppWebClient

        client_class = WhatsAppWebClient

    return client_class(shard=gateway_shard_for_account(account))


@transaction.atomic
def move_hosted_account(*, account, target_shard):
    """Controlled rebalance; an unexpired foreign owner is never moved."""
    from apps.channels.models import WhatsAppAccount

    if target_shard not in configured_gateways():
        raise ValueError("Unknown Hosted gateway shard.")
    locked = WhatsAppAccount.objects.select_for_update().get(
        pk=account.pk,
        organization_id=account.organization_id,
        connection_type="hosted",
    )
    if locked.hosted_lease_expires_at and locked.hosted_lease_expires_at > timezone.now():
        raise ValueError("The Hosted session still has an active gateway lease.")
    locked.hosted_gateway_shard = target_shard
    locked.hosted_lease_owner = ""
    locked.hosted_lease_expires_at = None
    locked.hosted_session_state = "pending"
    locked.status = WhatsAppAccount.Status.PENDING
    locked.save(
        update_fields=[
            "hosted_gateway_shard",
            "hosted_lease_owner",
            "hosted_lease_expires_at",
            "hosted_session_state",
            "status",
            "updated_at",
        ]
    )
    return locked


@transaction.atomic
def record_gateway_presence(*, account, payload, event):
    """Persist shard/lease health and reject callbacks from the wrong shard."""
    from apps.channels.models import WhatsAppAccount

    locked = WhatsAppAccount.objects.select_for_update().get(
        pk=account.pk,
        organization_id=account.organization_id,
        connection_type="hosted",
    )
    expected = gateway_shard_for_account(locked)
    shard = str(payload.get("gatewayShard") or expected).strip()
    owner = str(payload.get("gatewayOwner") or "").strip()[:128]
    if shard != expected:
        return False
    now = timezone.now()
    if (
        owner
        and locked.hosted_lease_owner
        and owner != locked.hosted_lease_owner
        and locked.hosted_lease_expires_at
        and locked.hosted_lease_expires_at > now
    ):
        # A late heartbeat/disconnect from a fenced process must never replace
        # or clear the new owner's durable lease.
        return False
    terminal = str(event or "").strip().lower() in {
        "disconnected",
        "logout",
        "auth_failure",
        "failed",
        "lease_lost",
    }
    if terminal:
        owner = ""
        expires = None
    else:
        expires = parse_datetime(str(payload.get("leaseExpiresAt") or ""))
        if expires is None and owner:
            expires = timezone.now() + timedelta(seconds=90)
    WhatsAppAccount.objects.filter(
        pk=account.pk,
        organization_id=account.organization_id,
        connection_type="hosted",
        hosted_gateway_shard=expected,
    ).update(
        hosted_lease_owner=owner,
        hosted_lease_expires_at=expires,
        hosted_gateway_heartbeat_at=now,
        hosted_session_state=str(event or "")[:32],
        updated_at=now,
    )
    account.hosted_lease_owner = owner
    account.hosted_lease_expires_at = expires
    account.hosted_gateway_heartbeat_at = now
    account.hosted_session_state = str(event or "")[:32]
    return True
