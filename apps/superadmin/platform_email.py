"""Superadmin platform SMTP configuration and signup sender selection."""
from email.utils import formataddr

from django import forms
from django.contrib import messages
from django.core.mail import get_connection
from django.conf import settings
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.debug import sensitive_post_parameters

from apps.integrations.services.email import build_email_backend, test_email_configuration, validate_smtp_host, EmailConfigurationError
from .feature_toggle_views import superuser_required
from .models import PlatformEmailConfiguration, AuditLog


class PlatformEmailForm(forms.ModelForm):
    password = forms.CharField(required=False, label="Password / Gmail App Password", widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = PlatformEmailConfiguration
        fields = ["email_address", "sender_name", "smtp_host", "smtp_port", "smtp_security", "smtp_username"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "w-full border border-gray-200 rounded-xl px-3 py-3"

    def clean_smtp_host(self):
        return validate_smtp_host(self.cleaned_data["smtp_host"])

    def clean_smtp_port(self):
        port = self.cleaned_data["smtp_port"]
        if not 1 <= port <= 65535:
            raise forms.ValidationError("Enter a port between 1 and 65535.")
        return port

    def clean(self):
        data = super().clean()
        if not data.get("password") and not self.instance.encrypted_password:
            self.add_error("password", "Enter your SMTP password or Gmail App Password.")
        return data


@sensitive_post_parameters("password")
@superuser_required
@require_http_methods(["GET", "POST"])
def platform_email_view(request):
    configuration = PlatformEmailConfiguration.objects.filter(pk=1).first()
    form = PlatformEmailForm(instance=configuration)
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"save", "connect", "disconnect"}:
            from django.http import HttpResponseBadRequest
            return HttpResponseBadRequest("Invalid email action.")
        with transaction.atomic():
            configuration = PlatformEmailConfiguration.objects.select_for_update().filter(pk=1).first()
            if configuration is None:
                configuration = PlatformEmailConfiguration(pk=1)
            if action == "disconnect":
                configuration.is_enabled = False
                configuration.save()
                AuditLog.record(actor=request.user, action="platform_email_updated", target=configuration, request=request, operation=action)
                messages.success(request, "Platform email disconnected. Pending verification emails will wait until you reconnect.")
                return redirect("superadmin-platform-email")
            form = PlatformEmailForm(request.POST, instance=configuration)
            if form.is_valid():
                configuration = form.save(commit=False)
                if form.cleaned_data["password"]:
                    configuration.set_password(form.cleaned_data["password"])
                configuration.is_enabled = False
                configuration.last_tested_at = None
                configuration.last_error = ""
                if action == "connect":
                    configuration.last_tested_at = timezone.now()
                    try:
                        test_email_configuration(configuration.as_smtp_configuration())
                    except Exception as exc:
                        configuration.last_error = type(exc).__name__[:100]
                        messages.error(request, "Connection failed. Check the SMTP host, security and App Password, then try again.")
                    else:
                        configuration.is_enabled = True
                        from apps.accounts.models import SignupVerificationDelivery
                        SignupVerificationDelivery.objects.filter(
                            delivered_at__isnull=True, user__is_active=False,
                        ).update(next_attempt_at=timezone.now())
                        messages.success(request, "Email connected. Signup verification will use this sender; pending emails retry automatically.")
                else:
                    messages.success(request, "Settings saved. Select Save & connect to enable verification emails.")
                configuration.save()
                AuditLog.record(actor=request.user, action="platform_email_updated", target=configuration, request=request, operation=action, connected=configuration.is_enabled)
                return redirect("superadmin-platform-email")
    return render(request, "superadmin/platform_email.html", {"form": form, "configuration": configuration})


def platform_verification_mail_options():
    configuration = PlatformEmailConfiguration.objects.filter(pk=1).first()
    if configuration is None:
        # Existing installations retain their environment-configured sender.
        return {"from_email": settings.DEFAULT_FROM_EMAIL, "connection": get_connection(timeout=10)}
    if not configuration.is_enabled:
        raise EmailConfigurationError("Connect the platform email account in Superadmin.")
    return {
        "from_email": formataddr((configuration.sender_name, configuration.email_address)),
        "connection": build_email_backend(configuration.as_smtp_configuration()),
    }
