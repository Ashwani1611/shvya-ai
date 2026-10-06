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


def test_this_refers_to_the_attached_file_without_losing_welcome():
    files = [candidate("send this with welcome messgae, also when ever user ask for brochure send this.", name="brochure")]
    assert unconditional_welcome_document(files, welcome_due=True) == 18
    assert unconditional_welcome_document(files, welcome_due=False) is None
    assert unconditional_welcome_document([candidate("send this with welcome message")], welcome_due=True) == 18
    assert reconcile_welcome_document(18, files, welcome_due=False, explicit_request=False) is None
    assert reconcile_welcome_document(18, files, welcome_due=False, explicit_request=True) == 18


def test_pronoun_welcome_does_not_ignore_restrictions_or_wrong_file_names():
    for instruction in (
        "send this with welcome message only if qualified",
        "send this with welcome message, also when user asks for another guide send this.",
        "send this with welcome message unless Instagram",
        "Do not send this with welcome message",
    ):
        assert unconditional_welcome_document([candidate(instruction)], welcome_due=True) is None
    file = candidate("send this with welcome message")
    assert unconditional_welcome_document([file, {**file, "document_id": 19}], welcome_due=True) is None
    assert unconditional_welcome_document([{**file, "already_shared": True}], welcome_due=True) is None

def test_explicit_attachment_refusals_override_welcome_selection():
    from apps.ai_engagement.services.file_sharing import declined_file_request
    for body in ("Hi. Please do not send me any brochure or file.",
                 "Don't send the product brochure.", "Never share any files."):
        assert declined_file_request(body, candidate(""))
    assert not declined_file_request("Send the product brochure.", candidate(""))
    assert not declined_file_request("Don't send the price list.", candidate(""))
    assert not declined_file_request("Do not book a trial. Send the brochure.", candidate(""))

def test_explicit_resend_respects_exact_authored_request_permission():
    from apps.ai_engagement.services.file_sharing import unrestricted_requested_document
    files = [candidate("send this product brochure along with welcome message or when ever lead ask product brochure.", already_shared=True)]
    assert unrestricted_requested_document(files, text="Please resend the same brochure now.") == 18
    assert unrestricted_requested_document(files, text="Thanks, I have it.") is None
    assert unrestricted_requested_document(files, text="Don't send the product brochure.") is None
    assert unrestricted_requested_document([candidate("send product brochure with welcome message", already_shared=True)], text="Please resend it.") is None
    assert unrestricted_requested_document([candidate("send product brochure with welcome message or when lead ask product brochure after qualification", already_shared=True)], text="Please resend it.") is None


def test_hinglish_attachment_refusals_override_welcome_selection():
    from apps.ai_engagement.services.file_sharing import declined_file_request

    file = candidate("")
    for body in (
        "Hinglish mein reply karo. Brochure ya koi file mat bhejna. Gym kitne baje khulta hai?",
        "Product brochure mat bhejna.",
        "Koi PDF nahi bhejna.",
        "Documents bhejna mat.",
        "Koi file nahin share.",
    ):
        assert declined_file_request(body, file), body
    for body in (
        "Product brochure bhejo.",
        "Price list mat bhejna. Product brochure bhejo.",
        "Trial book mat karo. Product brochure bhejo.",
        "Mat bolo ki file bhejna possible nahi hai.",
    ):
        assert not declined_file_request(body, file), body

def test_attachment_refusal_survives_content_questions_and_assistant_offers():
    from apps.ai_engagement.services.file_sharing import declined_in_conversation

    file = candidate("send product brochure with welcome message")
    def inbound(text):
        return {"direction": "inbound", "body": text}
    history = [inbound("Do not send files."),
               {"direction": "outbound", "body": "Please send the product brochure."},
               inbound("What does the brochure say?"),
               inbound("Is that brochure offer active today?")]
    assert declined_in_conversation(history, file) is True
    assert declined_in_conversation(history + [inbound("Please send the price list.")], file) is True
    for request in ("Please send the product brochure.", "Could you share the brochure?",
                    "mujhe brochure bhejo please", "Send me the file now."):
        assert declined_in_conversation(history + [inbound(request)], file) is False
    assert declined_in_conversation(
        [inbound("Please send the brochure."), inbound("Koi file mat bhejna."),
         inbound("Gym kitne baje khulta hai?")], file,
    ) is True
    assert declined_in_conversation(
        [inbound("Do not send product brochure."), inbound("Send me the brochure please.")], file,
    ) is False


def test_file_candidates_filter_prior_refusal_before_selection():
    from types import SimpleNamespace
    from unittest.mock import patch
    from apps.ai_engagement.services.file_sharing import FileSharingService

    from dataclasses import replace
    from apps.ai_engagement.tests.test_organization_information_alignment import build_context

    base_context = build_context()
    context = replace(
        base_context,
        lead={**base_context.lead, "shared_document_ids": []},
        knowledge=[],
        conversation={"message_count": 2, "messages": [
            {"direction": "inbound", "body": "Do not send product brochure."},
            {"direction": "inbound", "body": "What does that brochure offer mean?"},
        ]},
    )
    documents = [
        SimpleNamespace(id=18, name="product brochure", version=1, source_url="",
                        share_instruction="send product brochure with welcome message"),
        SimpleNamespace(id=19, name="price list", version=1, source_url="",
                        share_instruction="send price list only when requested"),
    ]
    service = FileSharingService()
    with patch.object(service, "get_eligible_documents", return_value=documents):
        files = service.build_file_candidates(organization=SimpleNamespace(), context=context)
    assert [item["document_id"] for item in files] == [19]


def test_omitted_file_review_does_not_call_provider_after_prior_refusal():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from apps.ai_engagement.services.file_sharing import FileSharingService

    context = SimpleNamespace(as_dict=lambda: {"conversation": {"messages": [
        {"direction": "inbound", "body": "Do not send files."},
        {"direction": "inbound", "body": "What does the brochure say?"},
    ]}})
    generate = Mock()
    selected = FileSharingService().review_requested_file(
        organization=SimpleNamespace(id="org"), lead=SimpleNamespace(id="lead"),
        context=context, candidates=[candidate("send product brochure with welcome message")],
        provider=Mock(), generate=generate, welcome_due=True,
    )
    assert selected is None
    generate.assert_not_called()
