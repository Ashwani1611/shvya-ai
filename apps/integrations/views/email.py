from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.crm.authentication import crm_login_required
from apps.integrations.models import EmailConfiguration
from apps.integrations.services.email import (
    EmailConfigurationError,
    test_email_configuration,
    validate_smtp_host,
)


PROVIDER_PRESETS = {
    EmailConfiguration.Provider.GMAIL: {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_security": EmailConfiguration.Security.STARTTLS,
    },
    EmailConfiguration.Provider.MICROSOFT: {
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_security": EmailConfiguration.Security.STARTTLS,
    },
    EmailConfiguration.Provider.ZOHO: {
        "smtp_host": "smtp.zoho.com",
        "smtp_port": 587,
        "smtp_security": EmailConfiguration.Security.STARTTLS,
    },
}


def _form_data(configuration=None, post_data=None):
    if post_data is not None:
        return {
            "provider": post_data.get(
                "provider",
                EmailConfiguration.Provider.GMAIL,
            ),
            "email_address": post_data.get("email_address", ""),
            "sender_name": post_data.get("sender_name", ""),
            "reply_to_email": post_data.get("reply_to_email", ""),
            "smtp_host": post_data.get("smtp_host", ""),
            "smtp_port": post_data.get("smtp_port", "587"),
            "smtp_security": post_data.get(
                "smtp_security",
                EmailConfiguration.Security.STARTTLS,
            ),
            "smtp_username": post_data.get("smtp_username", ""),
            "is_enabled": post_data.get("is_enabled") == "on",
        }

    if configuration is None:
        preset = PROVIDER_PRESETS[EmailConfiguration.Provider.GMAIL]
        return {
            "provider": EmailConfiguration.Provider.GMAIL,
            "email_address": "",
            "sender_name": "",
            "reply_to_email": "",
            "smtp_host": preset["smtp_host"],
            "smtp_port": preset["smtp_port"],
            "smtp_security": preset["smtp_security"],
            "smtp_username": "",
            "is_enabled": False,
        }

    return {
        "provider": configuration.provider,
        "email_address": configuration.email_address,
        "sender_name": configuration.sender_name,
        "reply_to_email": configuration.reply_to_email,
        "smtp_host": configuration.smtp_host,
        "smtp_port": configuration.smtp_port,
        "smtp_security": configuration.smtp_security,
        "smtp_username": configuration.smtp_username,
        "is_enabled": configuration.is_enabled,
    }


def _clean_form(post_data, configuration):
    errors = []
    provider = post_data.get("provider", "").strip()
    email_address = post_data.get("email_address", "").strip()
    sender_name = post_data.get("sender_name", "").strip()
    reply_to_email = post_data.get("reply_to_email", "").strip()
    smtp_host = post_data.get("smtp_host", "").strip()
    smtp_security = post_data.get("smtp_security", "").strip()
    smtp_username = post_data.get("smtp_username", "").strip() or email_address
    password = post_data.get("password", "")
    requested_enabled = post_data.get("is_enabled") == "on"

    valid_providers = {value for value, _ in EmailConfiguration.Provider.choices}
    if provider not in valid_providers:
        errors.append("Choose a supported email provider.")

    if not email_address:
        errors.append("Email address is required.")
    else:
        try:
            validate_email(email_address)
        except ValidationError:
            errors.append("Enter a valid email address.")

    if reply_to_email:
        try:
            validate_email(reply_to_email)
        except ValidationError:
            errors.append("Enter a valid reply-to email address.")

    try:
        smtp_host = validate_smtp_host(smtp_host)
    except ValidationError as exc:
        errors.extend(exc.messages)

    try:
        smtp_port = int(post_data.get("smtp_port", ""))
        if not 1 <= smtp_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        smtp_port = 0
        errors.append("SMTP port must be between 1 and 65535.")

    valid_security = {
        value for value, _ in EmailConfiguration.Security.choices
    }
    if smtp_security not in valid_security:
        errors.append("Choose a valid SMTP security option.")

    if not smtp_username:
        errors.append("SMTP username is required.")

    has_existing_password = bool(configuration and configuration.has_password)
    if not password and not has_existing_password:
        errors.append("Password or app password is required.")

    if len(sender_name) > 120:
        errors.append("Sender name must be 120 characters or fewer.")

    if len(smtp_username) > 254:
        errors.append("SMTP username must be 254 characters or fewer.")

    return (
        {
            "provider": provider,
            "email_address": email_address,
            "sender_name": sender_name,
            "reply_to_email": reply_to_email,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_security": smtp_security,
            "smtp_username": smtp_username,
            "password": password,
            "requested_enabled": requested_enabled,
        },
        errors,
    )


def _connection_changed(configuration, values):
    if configuration is None:
        return True

    current = (
        configuration.provider,
        configuration.email_address,
        configuration.smtp_host,
        configuration.smtp_port,
        configuration.smtp_security,
        configuration.smtp_username,
    )
    incoming = (
        values["provider"],
        values["email_address"],
        values["smtp_host"],
        values["smtp_port"],
        values["smtp_security"],
        values["smtp_username"],
    )
    return current != incoming or bool(values["password"])


@crm_login_required
def email_configuration_view(request):
    organization = request.crm_user.organization
    configuration = EmailConfiguration.objects.filter(
        organization=organization,
    ).first()

    if request.method == "POST":
        action = request.POST.get("action", "save").strip()

        if action == "disconnect":
            if configuration is not None:
                configuration.delete()
                messages.success(
                    request,
                    "Email account disconnected. Stored credentials were removed.",
                )
            return redirect("crm-connect-hub-email")

        values, errors = _clean_form(request.POST, configuration)

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            changed = _connection_changed(configuration, values)
            if configuration is None:
                configuration = EmailConfiguration(
                    organization=organization,
                )

            configuration.provider = values["provider"]
            configuration.email_address = values["email_address"]
            configuration.sender_name = values["sender_name"]
            configuration.reply_to_email = values["reply_to_email"]
            configuration.smtp_host = values["smtp_host"]
            configuration.smtp_port = values["smtp_port"]
            configuration.smtp_security = values["smtp_security"]
            configuration.smtp_username = values["smtp_username"]

            if values["password"]:
                configuration.set_password(values["password"])

            if changed:
                configuration.last_test_status = (
                    EmailConfiguration.TestStatus.NOT_TESTED
                )
                configuration.last_tested_at = None
                configuration.last_error = ""
                configuration.is_enabled = False
            else:
                configuration.is_enabled = (
                    values["requested_enabled"]
                    and configuration.last_test_status
                    == EmailConfiguration.TestStatus.SUCCESS
                )

            configuration.save()

            if action == "test":
                try:
                    test_email_configuration(configuration)
                except EmailConfigurationError as exc:
                    configuration.last_test_status = (
                        EmailConfiguration.TestStatus.FAILED
                    )
                    configuration.last_tested_at = timezone.now()
                    configuration.last_error = str(exc)[:1000]
                    configuration.is_enabled = False
                    configuration.save(
                        update_fields=[
                            "last_test_status",
                            "last_tested_at",
                            "last_error",
                            "is_enabled",
                            "updated_at",
                        ]
                    )
                    messages.error(request, str(exc))
                else:
                    configuration.last_test_status = (
                        EmailConfiguration.TestStatus.SUCCESS
                    )
                    configuration.last_tested_at = timezone.now()
                    configuration.last_error = ""
                    configuration.is_enabled = True
                    configuration.save(
                        update_fields=[
                            "last_test_status",
                            "last_tested_at",
                            "last_error",
                            "is_enabled",
                            "updated_at",
                        ]
                    )
                    messages.success(
                        request,
                        "Email connected successfully and is ready for automations.",
                    )
                return redirect("crm-connect-hub-email")

            if values["requested_enabled"] and not configuration.is_enabled:
                messages.warning(
                    request,
                    "Configuration saved. Test the SMTP connection before enabling it for automations.",
                )
            else:
                messages.success(
                    request,
                    "Email configuration saved successfully.",
                )
            return redirect("crm-connect-hub-email")

    return render(
        request,
        "integrations/email.html",
        {
            "configuration": configuration,
            "form_data": _form_data(
                configuration,
                request.POST if request.method == "POST" else None,
            ),
            "provider_choices": EmailConfiguration.Provider.choices,
            "security_choices": EmailConfiguration.Security.choices,
        },
    )
