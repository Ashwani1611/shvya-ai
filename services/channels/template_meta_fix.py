"""Meta compatibility fixes for WhatsApp template submit and synchronization.

The primary template service intentionally remains the source of truth for
validation, media uploads, auditing, and remote synchronization. This module
adds two compatibility fixes required by Meta's message-template API:

* BODY variables must include sample values when a template is created.
* Meta may return ``rejected_reason=NONE`` for non-rejected templates; that
  sentinel is not an actual rejection reason and must not be shown to users.
"""

from apps.channels.models import WhatsAppTemplate

from . import template_service as base

TemplateError = base.TemplateError


def _body_example_values(*, template, mapping):
    """Return Meta BODY sample values in numeric placeholder order."""
    if not mapping:
        return []

    examples = {
        item["key"]: str(item.get("example") or f"Example {item['label']}")
        for item in base.available_placeholders(organization=template.organization)
    }
    ordered_numbers = sorted(mapping, key=lambda value: int(value))
    return [
        examples.get(mapping[number], f"Example {number}")
        for number in ordered_numbers
    ]


def _add_body_examples(*, template, payload):
    """Attach BODY variable examples required by Meta's template API."""
    _, mapping = base.build_meta_body(
        organization=template.organization,
        body=template.body,
    )
    values = _body_example_values(template=template, mapping=mapping)
    if not values:
        return payload

    for component in payload.get("components") or []:
        if str(component.get("type") or "").upper() != "BODY":
            continue
        example = dict(component.get("example") or {})
        example["body_text"] = [values]
        component["example"] = example
        break
    return payload


def submit_template(*, template, attachment_file=None, carousel_files=None):
    """Submit a draft using a Meta-valid payload for BODY placeholders."""
    if template.status != WhatsAppTemplate.Status.DRAFT:
        raise TemplateError(f"Template is already {template.get_status_display()}.")

    st = base.state_for(template)
    base.validate_template(
        template=template,
        for_submit=True,
        carousel_config=st.carousel_config,
    )

    header_handle = ""
    carousel_config = None
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        carousel_config = base._upload_carousel_samples(
            template=template,
            config=base._clean_carousel_config(st.carousel_config),
            carousel_files=carousel_files,
        )
    elif template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        if attachment_file is not None:
            handle, mime, size = base._upload_header_sample(
                account=template.account,
                attachment_type=template.attachment_type,
                uploaded_file=attachment_file,
            )
            st.header_sample_handle = handle
            st.header_file_name = getattr(attachment_file, "name", "")[:255]
            st.header_mime_type = mime
            st.header_file_size = size
            st.save(
                update_fields=[
                    "header_sample_handle",
                    "header_file_name",
                    "header_mime_type",
                    "header_file_size",
                    "updated_at",
                ]
            )
        header_handle = st.header_sample_handle
        if not header_handle:
            rule = base.MEDIA_RULES[template.attachment_type]
            raise TemplateError(
                f"Choose a {rule['label'].lower()} sample file before submitting this template."
            )

    payload = base.build_meta_payload(
        template=template,
        header_handle=header_handle,
        carousel_config=carousel_config,
    )
    payload = _add_body_examples(template=template, payload=payload)

    # Persist the exact payload shape that is about to be sent so diagnostics
    # and the editor state match what Meta received.
    st = base.state_for(template)
    st.components = payload.get("components") or []
    st.local_status = base.WhatsAppTemplateMetadata.LocalStatus.SUBMITTING
    st.save(update_fields=["components", "local_status", "updated_at"])

    try:
        client = base.WhatsAppClient(
            template.account.phone_number_id,
            template.account.access_token,
        )
        response = client._post(
            f"{template.account.waba_id}/message_templates",
            payload,
        )
    except base.WhatsAppAPIError as exc:
        err = base._error(exc)
        st.local_status = base.WhatsAppTemplateMetadata.LocalStatus.SYNC_ERROR
        st.meta_error_code = err["code"]
        st.meta_error_subcode = err["subcode"]
        st.meta_error_type = err["type"]
        st.meta_error_message = err["message"]
        st.meta_response = err["payload"]
        st.save()
        template.rejection_reason = err["message"]
        template.save(update_fields=["rejection_reason", "updated_at"])
        base._audit(
            template,
            base.WhatsAppTemplateOperation.Operation.SUBMIT,
            False,
            error=err,
        )
        raise TemplateError(
            err["message"],
            status_code=err["http_status"],
            meta_error_code=err["code"],
        ) from exc

    template.meta_template_id = str(response.get("id") or "")
    template.status = base.normalize_meta_status(response.get("status") or "PENDING")
    template.rejection_reason = ""
    template.save(
        update_fields=[
            "meta_template_id",
            "status",
            "rejection_reason",
            "updated_at",
        ]
    )
    st.local_status = base.WhatsAppTemplateMetadata.LocalStatus.SUBMITTED
    st.submitted_at = base.timezone.now()
    st.meta_response = response
    st.meta_error_code = ""
    st.meta_error_subcode = ""
    st.meta_error_type = ""
    st.meta_error_message = ""
    st.save()
    base._audit(
        template,
        base.WhatsAppTemplateOperation.Operation.SUBMIT,
        True,
        payload=response,
    )
    return template


def sync_templates(*, organization, account):
    """Sync templates and remove Meta's non-error ``NONE`` reason sentinel."""
    summary = base.sync_templates(organization=organization, account=account)
    WhatsAppTemplate.objects.filter(
        organization=organization,
        account=account,
        rejection_reason__iexact="NONE",
    ).update(rejection_reason="")
    return summary
