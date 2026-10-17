"""Administrative adapters to the canonical tenant-owned AI services."""
from functools import partial

from django.core import signing
from django.db import transaction

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceService
from apps.ai_engagement.tasks import ingest_and_index_document, ingest_and_index_url_source
from apps.organizations.models import Organization
from .models import AuditLog

AI_FIELDS = ("about", "bot_languages", "ai_playbook", "ai_enabled", "bump_up_enabled", "bump_up_count")
REVISION_SALT = "superadmin-ai-brain-v1"


class StaleAIConfiguration(ValueError):
    pass


def revision_token(organization, instance=None, kind="brain"):
    return signing.dumps({"organization": str(organization.pk), "kind": kind,
        "pk": str(instance.pk) if instance else None,
        "updated": instance.updated_at.isoformat() if instance else None}, salt=REVISION_SALT)


def check_revision(token, organization, instance=None, kind="brain"):
    try:
        supplied = signing.loads(token, salt=REVISION_SALT, max_age=86400)
        current = signing.loads(revision_token(organization, instance, kind), salt=REVISION_SALT)
    except signing.BadSignature as exc:
        raise StaleAIConfiguration("This editor has expired. Open a fresh page before saving.") from exc
    if supplied != current:
        raise StaleAIConfiguration("This configuration changed after you opened it. Your edits have not been saved. Open the current configuration in another tab and compare before saving again.")


def audit(request, organization, operation, **metadata):
    AuditLog.record(actor=request.user, action=AuditLog.Action.ORGANIZATION_UPDATED,
                    target=organization, request=request, operation=operation, **metadata)


@transaction.atomic
def save_brain(request, organization, data):
    Organization.objects.select_for_update().get(pk=organization.pk)
    info = OrgInfo.objects.select_for_update().filter(organization=organization).first()
    check_revision(data["revision"], organization, info)
    changes = {key: data[key] for key in AI_FIELDS}
    result = OrgInfoService().update(organization=organization, data=changes)
    # Never copy the Playbook, business description or customer content into logs.
    audit(request, organization, "ai_brain_update", fields=list(changes))
    return result


@transaction.atomic
def add_url(request, organization, data):
    Organization.objects.select_for_update().get(pk=organization.pk)
    service = KnowledgeSourceService()
    source = service.create_url_source(organization=organization, **data)
    audit(request, organization, "knowledge_url_added", source_id=source.pk)
    transaction.on_commit(partial(ingest_and_index_url_source.delay, source_id=source.pk, organization_id=organization.pk))
    return source


@transaction.atomic
def add_file(request, organization, data):
    source, document = KnowledgeSourceService().create_file_source(
        organization=organization, uploaded_file=data["file"], name=data["name"],
        share_instruction=data["share_instruction"],
    )
    audit(request, organization, "knowledge_file_added", document_id=document.pk, source_id=source.pk)
    transaction.on_commit(partial(ingest_and_index_document.delay, document_id=document.pk, organization_id=organization.pk))
    return document


def brain_summary(organization):
    from apps.ai_engagement.models import AICreditWallet, Document, FAQ
    from apps.ai_engagement.coins import credits_to_coins
    from apps.followups.models import FollowupSequence
    wallet = AICreditWallet.objects.filter(organization=organization).first()
    return {
        "wallet": wallet,
        "coins_used": credits_to_coins(wallet.lifetime_credits_used if wallet else 0),
        "coins_available": credits_to_coins(wallet.available_credits if wallet else 0),
        "coins_added": credits_to_coins(wallet.lifetime_credits_added if wallet else 0),
        "coins_reserved": credits_to_coins(wallet.reserved_credits if wallet else 0),
        "coins_threshold": credits_to_coins(wallet.low_credit_threshold if wallet else 0),
        "documents": Document.objects.filter(organization=organization).count(),
        "faqs": FAQ.objects.filter(organization=organization).count(),
        "sequences": FollowupSequence.objects.filter(organization=organization).count(),
    }
