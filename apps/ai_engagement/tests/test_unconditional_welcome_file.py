from apps.ai_engagement.services.file_sharing import (
    unconditional_welcome_document, reconcile_welcome_document,
)


def candidate(instruction, **extra):
    return {"document_id": 18, "name": "product brochure", "share_instruction": instruction,
            "already_shared": False, **extra}


def test_authored_welcome_or_request_is_not_lost_on_german_greeting():
    files = [candidate("send this product brochure along with welcome message or when ever lead ask product brochure.")]
    assert unconditional_welcome_document(files, welcome_due=True) == 18
    assert unconditional_welcome_document(files, welcome_due=False) is None


def test_restrictions_and_ambiguous_candidates_still_require_review():
    for instruction in (
        "send product brochure with welcome only if qualified",
        "send product brochure with welcome message or when lead ask product brochure after qualification",
        "Do not send product brochure with welcome",
        "send product brochure with welcome message unless Instagram",
        "send another document with welcome message",
        "send product brochure only when lead asks",
    ):
        assert unconditional_welcome_document([candidate(instruction)], welcome_due=True) is None
    valid = candidate("send product brochure with welcome message")
    assert unconditional_welcome_document([valid, {**valid, "document_id": 19}], welcome_due=True) is None
    assert unconditional_welcome_document([{**valid, "already_shared": True}], welcome_due=True) is None
    assert unconditional_welcome_document([{**valid, "document_id": True}], welcome_due=True) is None


def test_final_pricing_reply_cannot_keep_draft_welcome_only_attachment():
    files = [candidate("send product brochure with welcome message or when lead ask product brochure")]
    assert reconcile_welcome_document(18, files, welcome_due=False, explicit_request=False) is None
    assert reconcile_welcome_document(18, files, welcome_due=True, explicit_request=False) == 18
    assert reconcile_welcome_document(18, files, welcome_due=False, explicit_request=True) == 18
    conditional = [candidate("send product brochure when lead has completed qualification")]
    assert reconcile_welcome_document(18, conditional, welcome_due=False, explicit_request=False) == 18
