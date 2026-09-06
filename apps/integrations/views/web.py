from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.crm.authentication import crm_login_required
from apps.organizations.models import APIKey


CONNECT_HUB_GROUPS = [
    {
        "label": "Shvya Tools",
        "icon": "ti-sparkles",
        "items": [
            {
                "slug": "shvya-api",
                "name": "Shvya API",
                "icon": "ti-api",
                "url_name": "crm-connect-hub-shvya-api",
                "description": "Send and update leads in Shvya from your external systems.",
            },
            {
                "slug": "webhook",
                "name": "Webhook",
                "icon": "ti-webhook",
                "url_name": "crm-connect-hub-webhook",
                "description": "Send lead create and update events to your own endpoint.",
            },
        ],
    },
    {
        "label": "CRM & Data Sync",
        "icon": "ti-database",
        "items": [
            {
                "slug": "google-sheets",
                "name": "Google Sheets",
                "icon": "ti-table",
                "url_name": "crm-connect-hub-google-sheets",
                "description": "Sync lead data between Shvya and your spreadsheets.",
            },
            {
                "slug": "email",
                "name": "Email",
                "icon": "ti-mail",
                "url_name": "crm-connect-hub-email",
                "description": "Connect email workflows for lead communication and follow-up.",
            },
        ],
    },
    {
        "label": "Ads & Conversions",
        "icon": "ti-ad",
        "items": [
            {
                "slug": "meta-conversions-api",
                "name": "Meta Conversions API",
                "icon": "ti-brand-meta",
                "url_name": "crm-connect-hub-meta-conversions-api",
                "description": "Send conversion events to Meta for stronger ad attribution.",
            },
            {
                "slug": "meta-lead-ad-forms",
                "name": "Meta Lead Ad Forms",
                "icon": "ti-brand-meta",
                "url_name": "crm-connect-hub-meta-lead-ad-forms",
                "description": "Bring Meta lead form submissions directly into Shvya.",
            },
        ],
    },
    {
        "label": "Lead Marketplaces & Platforms",
        "icon": "ti-building-store",
        "items": [
            {
                "slug": "justdial",
                "name": "Justdial",
                "icon": "ti-letter-j",
                "url_name": "crm-connect-hub-justdial",
                "description": "Bring Justdial inquiries into Shvya for faster follow-up.",
            },
            {
                "slug": "indiamart",
                "name": "IndiaMART",
                "icon": "ti-building-store",
                "url_name": "crm-connect-hub-indiamart",
                "description": "Import IndiaMART inquiries into Shvya and follow up instantly.",
            },
        ],
    },
    {
        "label": "Payments",
        "icon": "ti-credit-card",
        "items": [
            {
                "slug": "razorpay",
                "name": "RazorPay",
                "icon": "ti-brand-razorpay",
                "url_name": "crm-connect-hub-razorpay",
                "description": "Create and update leads from successful RazorPay payments.",
            },
        ],
    },
]


INTEGRATION_DETAILS = {
    "webhook": {
        "name": "Webhook",
        "icon": "ti-webhook",
        "description": "Send lead events from Shvya to an endpoint in your own system.",
        "accent": "Webhook",
        "requirements": [
            "A publicly reachable HTTPS endpoint",
            "A secret shared only between Shvya and your receiving service",
            "A 2xx response from your endpoint after successful processing",
        ],
        "steps": [
            "Add the receiving URL in your integration configuration.",
            "Set a secret and validate it on every incoming request.",
            "Enable the connection after your endpoint is ready to accept events.",
        ],
        "fields": ["lead_id", "name", "phone", "email", "notes", "stage", "pipeline", "event_type", "custom_attributes"],
        "note": "Webhook delivery configuration is isolated per organization.",
    },
    "google-sheets": {
        "name": "Google Sheets",
        "icon": "ti-table",
        "description": "Use Google Sheets as a lightweight source or destination for lead data.",
        "accent": "Data Sync",
        "requirements": [
            "A Google account with access to the target spreadsheet",
            "The spreadsheet and worksheet you want to use",
            "Lead fields you want to map between Shvya and Sheets",
        ],
        "steps": [
            "Choose the Google account that owns or can edit the sheet.",
            "Select a spreadsheet and worksheet.",
            "Map Shvya lead fields to sheet columns before enabling sync.",
        ],
        "note": "This dedicated URL is the organization workspace for Google Sheets setup.",
    },
    "email": {
        "name": "Email",
        "icon": "ti-mail",
        "description": "Connect email with Shvya lead workflows and follow-up activity.",
        "accent": "Communication",
        "requirements": [
            "An email inbox or provider account your organization controls",
            "Permission to send messages from the connected address",
            "A default sender identity for outbound lead communication",
        ],
        "steps": [
            "Connect the mailbox or provider account.",
            "Confirm the sender identity and reply-to address.",
            "Use the connected email account in lead communication workflows.",
        ],
        "note": "Email credentials should be connected only through the provider setup flow.",
    },
    "meta-conversions-api": {
        "name": "Meta Conversions API",
        "icon": "ti-brand-meta",
        "description": "Send server-side conversion events to Meta from Shvya lead activity.",
        "accent": "Ads & Conversions",
        "requirements": [
            "A Meta Business account",
            "A Pixel or dataset configured for Conversions API",
            "Permission to manage the selected business asset",
        ],
        "steps": [
            "Choose the Meta business asset used for conversion tracking.",
            "Map Shvya lead milestones to Meta event names.",
            "Verify test events before enabling production delivery.",
        ],
        "note": "Keep Meta access credentials organization-scoped and never expose them in the browser after connection.",
    },
    "meta-lead-ad-forms": {
        "name": "Meta Lead Ad Forms",
        "icon": "ti-brand-meta",
        "description": "Bring new Meta lead-form submissions directly into Shvya.",
        "accent": "Lead Capture",
        "requirements": [
            "A Meta Page with active lead ads",
            "Access to the lead forms you want to connect",
            "A Shvya pipeline and stage for new imported leads",
        ],
        "steps": [
            "Choose the Meta Page and lead form.",
            "Select the Shvya pipeline and destination stage.",
            "Map form questions to Shvya fields and activate the connection.",
        ],
        "note": "Each form can be mapped independently so different campaigns can feed different pipelines.",
    },
    "razorpay": {
        "name": "RazorPay",
        "icon": "ti-brand-razorpay",
        "description": "Turn successful RazorPay payment activity into CRM lead updates.",
        "accent": "Payments",
        "requirements": [
            "A RazorPay account",
            "Webhook access for the account",
            "A destination pipeline for payment-driven lead activity",
        ],
        "steps": [
            "Connect the RazorPay account used to receive payments.",
            "Choose which successful payment events should update Shvya.",
            "Map payer details to lead fields and verify an event.",
        ],
        "note": "Payment secrets must remain server-side and should never be placed in client-side code.",
    },
    "justdial": {
        "name": "Justdial",
        "icon": "ti-letter-j",
        "description": "Capture Justdial inquiries in Shvya and route them to your sales pipeline.",
        "accent": "Lead Marketplace",
        "requirements": [
            "An active Justdial business account",
            "Lead/API access available for your Justdial plan",
            "A Shvya pipeline and stage for incoming inquiries",
        ],
        "steps": [
            "Connect the Justdial lead source for your organization.",
            "Choose the destination pipeline and stage.",
            "Map inquiry fields and confirm a sample lead before activation.",
        ],
        "note": "Lead source attribution should remain Justdial so reporting can distinguish marketplace inquiries.",
    },
    "indiamart": {
        "name": "IndiaMART",
        "icon": "ti-building-store",
        "description": "Pull IndiaMART inquiries into Shvya for immediate CRM follow-up.",
        "accent": "Lead Marketplace",
        "requirements": [
            "An IndiaMART seller account with lead access",
            "The IndiaMART CRM/API key provided for your account",
            "A Shvya pipeline and stage for imported inquiries",
        ],
        "steps": [
            "Connect the IndiaMART seller account.",
            "Select where new IndiaMART leads should enter your CRM.",
            "Verify field mapping and activate lead synchronization.",
        ],
        "note": "Imported inquiries should keep IndiaMART as their lead source for accurate reporting.",
    },
}


@crm_login_required
def connect_hub_view(request):
    query = request.GET.get("q", "").strip()

    return render(
        request,
        "integrations/connect_hub.html",
        {
            "groups": CONNECT_HUB_GROUPS,
            "query": query,
        },
    )


@crm_login_required
def shvya_api_view(request):
    user = request.crm_user
    organization = user.organization

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "create_key":
            name = request.POST.get("name", "").strip() or "Connect Hub API Key"
            api_key, raw_key = APIKey.issue(
                organization=organization,
                name=name[:100],
            )
            request.session["connect_hub_raw_api_key"] = raw_key
            request.session.modified = True
            messages.success(request, f"API key '{api_key.name}' created.")
            return redirect("crm-connect-hub-shvya-api")

        if action == "revoke_key":
            api_key = get_object_or_404(
                APIKey,
                id=request.POST.get("api_key_id"),
                organization=organization,
            )
            api_key.is_active = False
            api_key.save(update_fields=["is_active"])
            messages.success(request, f"API key '{api_key.name}' revoked.")
            return redirect("crm-connect-hub-shvya-api")

        messages.error(request, "Unknown API key action.")
        return redirect("crm-connect-hub-shvya-api")

    raw_api_key = request.session.pop("connect_hub_raw_api_key", None)
    active_keys = APIKey.objects.filter(
        organization=organization,
        is_active=True,
    ).order_by("-created_at")

    api_url = request.build_absolute_uri(reverse("lead-upsert"))
    list_api_url = request.build_absolute_uri(reverse("lead-list"))

    return render(
        request,
        "integrations/shvya_api.html",
        {
            "active_keys": active_keys,
            "raw_api_key": raw_api_key,
            "api_url": api_url,
            "list_api_url": list_api_url,
        },
    )


@crm_login_required
def integration_detail_view(request, integration_slug):
    integration = INTEGRATION_DETAILS.get(integration_slug)
    if integration is None:
        return redirect("crm-connect-hub")

    return render(
        request,
        "integrations/detail.html",
        {
            "integration": integration,
            "integration_slug": integration_slug,
        },
    )
