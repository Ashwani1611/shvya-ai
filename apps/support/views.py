"""Thin HTTP boundaries. Object visibility is enforced on every path."""
import csv
import uuid
from functools import partial

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import services
from .access import portal_required, staff_users, visible_tickets
from .fields import public_custom
from .forms import (CONFIG_FORMS, CreateTicketForm, FilterForm, PropertiesForm, ReplyForm,
                    ShareForm, WorkItemForm)
from .models import (Attachment, ConfigurationEvent, EmailDelivery, InboundReceipt, SavedReply, SupportSettings,
                     Ticket, TicketCategory, TicketIssue, TicketPriority, TicketStatus)
from .policy import safe_csv


def private(response):
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def root_url(staff):
    return reverse("support-staff:list" if staff else "support-client:list")


def detail_url(ticket, staff=False):
    return reverse("support-staff:detail" if staff else "support-client:detail", args=[ticket.pk])


def error_text(exc):
    return "; ".join(exc.messages) if isinstance(exc, ValidationError) else "The request could not be completed."


def base_context(request, *, staff=False, shared=False):
    return {
        "support_staff": staff, "support_shared": shared,
        "base_template": "support/public_base.html" if shared else ("superadmin/base.html" if staff else "base.html"),
        "portal_title": "Client’s Portal" if staff else "Help & Support",
        "list_url": "" if shared else root_url(staff),
        "config": SupportSettings.load(),
        "crm_user": request.user if not staff and not shared else None,
        "feature_slug": "support-portal",
    }


def filter_tickets(request, *, staff):
    form = FilterForm(request.GET)
    qs = visible_tickets(request.user).filter(merged_into__isnull=True)
    if not form.is_valid():
        return qs.none(), form
    data = form.cleaned_data
    for key in ("status", "category", "priority"):
        if data.get(key):
            qs = qs.filter(**{key+"_id": data[key]})
    if data.get("q"):
        q = data["q"]
        search = Q(subject__icontains=q) | Q(reference__icontains=q)
        if staff:
            search |= Q(organization__name__icontains=q) | Q(requester__email__icontains=q)
        qs = qs.filter(search)
    if staff:
        if data.get("organization"):
            qs = qs.filter(organization_id=data["organization"])
        if data.get("assignee"):
            qs = qs.filter(assignee_id=data["assignee"])
        if data.get("view") == "mine":
            qs = qs.filter(assignee=request.user)
        elif data.get("view") == "unassigned":
            qs = qs.filter(assignee__isnull=True)
        elif data.get("view") == "waiting":
            qs = qs.filter(status__behavior="answered")
    if data.get("date_from"):
        qs = qs.filter(created_at__date__gte=data["date_from"])
    if data.get("date_to"):
        qs = qs.filter(created_at__date__lte=data["date_to"])
    return qs, form


def ticket_list(request, *, staff=False):
    qs, filters = filter_tickets(request, staff=staff)
    page = Paginator(qs.select_related("status", "priority", "category", "organization", "requester", "assignee"), 25).get_page(request.GET.get("page"))
    all_tickets = visible_tickets(request.user).filter(merged_into__isnull=True)
    counts = {r["status__behavior"]: r["total"] for r in all_tickets.values("status__behavior").annotate(total=Count("pk"))}
    context = base_context(request, staff=staff)
    query = request.GET.copy()
    query.pop("page", None)
    context.update(page=page, filters=filters, counts=counts, total=all_tickets.count(),
        statuses=TicketStatus.objects.filter(active=True), categories=TicketCategory.objects.filter(active=True),
        priorities=TicketPriority.objects.filter(active=True), staff_users=staff_users() if staff else [],
        query_string=query.urlencode(), create_form=None)
    for ticket in page:
        ticket.portal_url = detail_url(ticket, staff)
    if staff:
        from apps.organizations.models import Organization
        context["organizations"] = Organization.objects.order_by("name").values("id", "name")
        context["mail_failures"] = EmailDelivery.objects.filter(state="failed").count()
    else:
        from urllib.parse import parse_qs, urlsplit
        try:
            referrer = urlsplit(request.META.get("HTTP_REFERER", ""))
        except ValueError:
            referrer = urlsplit("")
        same_origin = referrer.netloc == request.get_host() and referrer.scheme == request.scheme
        pipeline = request.GET.get("pipeline")
        if not pipeline and same_origin and referrer.path.startswith("/dashboard/"):
            pipeline = parse_qs(referrer.query).get("pipeline", [None])[0]
        source_path = request.GET.get("source_path") or (referrer.path if same_origin else "")
        initial = {"source_path": source_path or "/dashboard/support-portal/"}
        if pipeline:
            try:
                from .access import allowed_pipelines
                chosen = allowed_pipelines(request.user).filter(pk=uuid.UUID(pipeline)).first()
                if chosen:
                    initial["pipeline"] = chosen.pk
            except ValueError:
                pass
        if not pipeline and same_origin and referrer.path == "/dashboard/":
            from .access import allowed_pipelines
            allowed = allowed_pipelines(request.user)
            chosen = (allowed.filter(name="Leads").first() if request.user.role == "admin" else None) or allowed.first()
            if chosen:
                initial["pipeline"] = chosen.pk
        context["create_form"] = CreateTicketForm(actor=request.user, initial=initial)
        context["issue_choices"] = list(TicketIssue.objects.filter(active=True, category__active=True).values("id", "category_id", "name", "guidance"))
    return private(render(request, "support/list.html", context))


customer_list = portal_required()(require_GET(ticket_list))
staff_list = portal_required(staff=True)(require_GET(partial(ticket_list, staff=True)))


@portal_required()
@require_POST
def create(request):
    form = CreateTicketForm(request.POST, request.FILES, actor=request.user)
    if form.is_valid():
        data = form.cleaned_data
        try:
            ticket = services.create_ticket(actor=request.user, category_id=data["category"].pk,
                issue_id=data["issue"].pk, priority_id=data["priority"].pk, subject=data["subject"], body=data["body"],
                pipeline_id=data["pipeline"].pk if data["pipeline"] else None, custom=form.custom_values(),
                files=data["attachments"], source_path=data["source_path"], client_key=data["client_key"])
        except (ValidationError, ValueError) as exc:
            form.add_error(None, error_text(exc))
        else:
            messages.success(request, f"{ticket.reference} created. Shvya-Ops has your request.")
            return redirect(detail_url(ticket))
    context = base_context(request)
    context.update(create_form=form, issue_choices=list(TicketIssue.objects.filter(active=True, category__active=True).values("id", "category_id", "name", "guidance")))
    return private(render(request, "support/create.html", context, status=400))


def detail_context(request, ticket, *, staff=False, grant=None, token=None, reply_form=None):
    context = base_context(request, staff=staff, shared=bool(grant))
    qs = ticket.messages.select_related("author").prefetch_related("attachments").order_by("-created_at", "-id")
    if not staff:
        qs = qs.filter(internal=False)
    page = Paginator(qs, 40).get_page(request.GET.get("messages_page"))
    thread = list(reversed(page.object_list))
    for message in thread:
        for attachment in message.attachments.all():
            attachment.download_url = (reverse("support-shared:attachment", args=[token, attachment.pk]) if grant
                else reverse("support-staff:attachment" if staff else "support-client:attachment", args=[attachment.pk]))
    context.update(ticket=ticket, thread=thread, thread_page=page,
        public_message_count=ticket.messages.filter(internal=False).count(),
        public_fields=public_custom(ticket.custom_values),
        reply_form=reply_form or ReplyForm(staff=staff), share_form=ShareForm(),
        grant=grant, token=token, can_reply=not grant or grant.can_reply,
        can_close=context["config"].allow_customer_close and (not grant or grant.can_close),
        open_status=TicketStatus.objects.get(key="open", system=True),
        closed_status=TicketStatus.objects.get(key="closed", system=True),
        detail_url=reverse("support-shared:ticket", args=[token]) if grant else detail_url(ticket, staff),
        reply_url=reverse("support-shared:reply", args=[token]) if grant else reverse("support-staff:reply" if staff else "support-client:reply", args=[ticket.pk]),
        state_url=reverse("support-shared:state", args=[token]) if grant else reverse("support-staff:state" if staff else "support-client:state", args=[ticket.pk]),
        status_url=reverse("support-shared:update", args=[token]) if grant else reverse("support-staff:update" if staff else "support-client:update", args=[ticket.pk]))
    if ticket.merged_into_id:
        context["state_url"] = ""
        context["detail_url"] += "?archive=1"
    if not grant:
        context["share_url"] = reverse("support-staff:share" if staff else "support-client:share", args=[ticket.pk])
        context["can_share"] = not ticket.merged_into_id and (staff or request.user.role == "admin" or request.user.pk == ticket.requester_id)
        context["share_links"] = ticket.share_links.order_by("-created_at")[:30] if context["can_share"] else []
        for link in context["share_links"]:
            link.revoke_url = reverse("support-staff:revoke" if staff else "support-client:revoke", args=[ticket.pk, link.pk])
    if staff:
        context.update(properties_form=PropertiesForm(ticket=ticket), work_form=WorkItemForm(),
            work_items=ticket.work_items.select_related("assigned_to").order_by("completed_at", "due_at")[:100],
            events=ticket.events.select_related("actor")[:100], saved_replies=SavedReply.objects.filter(active=True),
            merge_options=Ticket.objects.filter(organization=ticket.organization, requester=ticket.requester,
                merged_into__isnull=True).exclude(pk=ticket.pk).order_by("-updated_at")[:100],
            merged_sources=ticket.merged_sources.all(),
            issue_choices=list(TicketIssue.objects.filter(active=True, category__active=True).values("id", "category_id", "name", "guidance")))
    return context


def ticket_detail(request, ticket_id, *, staff=False):
    ticket = (get_object_or_404(visible_tickets(request.user), pk=ticket_id)
              if staff and request.GET.get("archive") == "1"
              else services.canonical(ticket_id, request.user))
    if str(ticket.pk) != str(ticket_id):
        return redirect(detail_url(ticket, staff))
    return private(render(request, "support/detail.html", detail_context(request, ticket, staff=staff)))


customer_detail = portal_required()(require_GET(ticket_detail))
staff_detail = portal_required(staff=True)(require_GET(partial(ticket_detail, staff=True)))


def post_reply(request, ticket_id=None, *, staff=False, token=None):
    grant = services.resolve_grant(token) if token else None
    ticket = grant.ticket if grant else services.canonical(ticket_id, request.user)
    form = ReplyForm(request.POST, request.FILES, staff=staff)
    if form.is_valid():
        data = form.cleaned_data
        try:
            services.reply(ticket_id=ticket.pk, actor=None if grant else request.user, grant=grant,
                body=data["body"], internal=data.get("internal", False), files=data["attachments"],
                status_id=data["status"].pk if data.get("status") else None,
                assign_me=data.get("assign_me", False), client_key=data["client_key"])
        except (ValidationError, ValueError) as exc:
            form.add_error(None, error_text(exc))
        else:
            return redirect(reverse("support-shared:ticket", args=[token]) if grant else detail_url(ticket, staff))
    return private(render(request, "support/detail.html", detail_context(request, ticket, staff=staff,
                                  grant=grant, token=token, reply_form=form), status=400))


customer_reply = portal_required()(require_POST(post_reply))
staff_reply = portal_required(staff=True)(require_POST(partial(post_reply, staff=True)))
shared_reply = require_POST(post_reply)


def post_update(request, ticket_id=None, *, staff=False, token=None):
    grant = services.resolve_grant(token) if token else None
    ticket = grant.ticket if grant else services.canonical(ticket_id, request.user)
    try:
        if staff:
            if request.POST.get("action") == "assign_me":
                services.update_ticket(ticket_id=ticket.pk, actor=request.user,
                    change_assignee=True, assignee_id=request.user.pk)
            else:
                form = PropertiesForm(request.POST, ticket=ticket)
                if not form.is_valid():
                    raise ValidationError([f"{k}: {'; '.join(v)}" for k, v in form.errors.items()])
                data = form.cleaned_data
                services.update_ticket(ticket_id=ticket.pk, actor=request.user,
                    status_id=data["status"].pk, priority_id=data["priority"].pk,
                    change_assignee=True, assignee_id=data["assignee"].pk if data["assignee"] else None,
                    category_id=data["category"].pk, issue_id=data["issue"].pk,
                    custom=form.custom_values(), version=data["version"])
        else:
            services.update_ticket(ticket_id=ticket.pk, actor=None if grant else request.user, grant=grant,
                status_id=int(request.POST.get("status", "0")), version=int(request.POST.get("version", "0")))
    except (ValidationError, ValueError) as exc:
        messages.error(request, error_text(exc))
    else:
        messages.success(request, "Ticket updated.")
    return redirect(reverse("support-shared:ticket", args=[token]) if grant else detail_url(ticket, staff))


customer_update = portal_required()(require_POST(post_update))
staff_update = portal_required(staff=True)(require_POST(partial(post_update, staff=True)))
shared_update = require_POST(post_update)


def post_share(request, ticket_id, *, staff=False):
    ticket = services.canonical(ticket_id, request.user)
    form = ShareForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        grant, raw = services.issue_share(actor=request.user, ticket_id=ticket.pk, label=data["label"], days=data["days"],
            can_reply=data["can_reply"], can_close=data["can_close"])
        from django.conf import settings
        base = getattr(settings, "SUPPORT_PUBLIC_BASE_URL", "").rstrip("/")
        # Authenticated origin fallback displays a one-time link; it is never used for outbound email.
        url = (base or request.build_absolute_uri("/").rstrip("/")) + reverse("support-shared:ticket", args=[raw])
        context = base_context(request, staff=staff)
        context.update(shared_url=url, grant=grant, ticket=ticket, detail_url=detail_url(ticket, staff))
        return private(render(request, "support/share_created.html", context))
    messages.error(request, "Review the link permissions and acknowledge its visibility before sharing.")
    return redirect(detail_url(ticket, staff))


customer_share = portal_required()(require_POST(post_share))
staff_share = portal_required(staff=True)(require_POST(partial(post_share, staff=True)))


def post_revoke(request, ticket_id, grant_id, *, staff=False):
    services.revoke_share(actor=request.user, ticket_id=ticket_id, grant_id=grant_id)
    return redirect(detail_url(services.canonical(ticket_id, request.user), staff))


customer_revoke = portal_required()(require_POST(post_revoke))
staff_revoke = portal_required(staff=True)(require_POST(partial(post_revoke, staff=True)))


@require_GET
@never_cache
def shared_ticket(request, token):
    grant = services.resolve_grant(token)
    return private(render(request, "support/detail.html", detail_context(request, grant.ticket, grant=grant, token=token)))


def download_attachment(request, attachment_id, *, staff=False, token=None):
    qs = Attachment.objects.select_related("message__ticket")
    if token:
        grant = services.resolve_grant(token)
        attachment = get_object_or_404(qs, pk=attachment_id, message__ticket=grant.ticket, message__internal=False)
    else:
        qs = qs.filter(message__ticket__in=visible_tickets(request.user))
        if not staff:
            qs = qs.filter(message__internal=False)
        attachment = get_object_or_404(qs, pk=attachment_id)
    try:
        file = attachment.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404("This attachment is unavailable.") from exc
    response = FileResponse(file, as_attachment=True, filename=attachment.original_name,
                            content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return private(response)


customer_attachment = portal_required()(require_GET(download_attachment))
staff_attachment = portal_required(staff=True)(require_GET(partial(download_attachment, staff=True)))
shared_attachment = require_GET(download_attachment)


def ticket_state(request, ticket_id=None, *, staff=False, token=None):
    grant = services.resolve_grant(token) if token else None
    ticket = grant.ticket if grant else services.canonical(ticket_id, request.user)
    public_qs = ticket.messages.filter(internal=False)
    state = {"public_messages": public_qs.count(), "status": ticket.status.name,
             "status_behavior": ticket.status.behavior}
    if staff:
        state["version"] = ticket.version
        state["typing"] = []
        users = list(staff_users().exclude(pk=request.user.pk).only("id", "name"))
        keys = {f"support:typing:{ticket.organization_id}:{ticket.pk}:{user.pk}": user for user in users}
        present = cache.get_many(keys)
        state["typing"] = [keys[key].name or "Shvya-Ops" for key in present if present[key]]
    return private(JsonResponse(state))


customer_state = portal_required()(require_GET(ticket_state))
staff_state = portal_required(staff=True)(require_GET(partial(ticket_state, staff=True)))
shared_state = require_GET(ticket_state)


@portal_required(staff=True)
@require_POST
def typing(request, ticket_id):
    ticket = services.canonical(ticket_id, request.user)
    cache.set(f"support:typing:{ticket.organization_id}:{ticket.pk}:{request.user.pk}", True, timeout=15)
    return private(JsonResponse({"ok": True}))


@portal_required(staff=True)
@require_POST
def merge(request, ticket_id):
    try:
        ticket = services.merge_tickets(actor=request.user, primary_id=ticket_id,
                                       source_ids=request.POST.getlist("sources"))
    except (ValidationError, ValueError) as exc:
        messages.error(request, error_text(exc))
        return redirect("support-staff:detail", ticket_id=ticket_id)
    messages.success(request, "Conversations merged. Existing shared links were revoked; internal work stays on its original ticket.")
    return redirect(detail_url(ticket, True))


@portal_required(staff=True)
@require_POST
def work(request, ticket_id):
    form = WorkItemForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        services.add_work_item(actor=request.user, ticket_id=ticket_id, kind=data["kind"], title=data["title"],
                               due_at=data["due_at"], assignee_id=data["assigned_to"].pk)
        messages.success(request, "Staff work item added.")
    else:
        messages.error(request, "Add a valid task/reminder title, assignee and due date.")
    return redirect("support-staff:detail", ticket_id=ticket_id)


@portal_required(staff=True)
@require_POST
def work_complete(request, ticket_id, item_id):
    services.complete_work_item(actor=request.user, ticket_id=ticket_id, item_id=item_id)
    item = services.canonical(ticket_id, request.user)
    target = reverse("support-staff:detail", args=[ticket_id])
    return redirect(target + ("?archive=1" if str(item.pk) != str(ticket_id) else ""))


@portal_required(staff=True)
@require_POST
def bulk(request):
    try:
        ids = sorted({uuid.UUID(value) for value in request.POST.getlist("tickets")}, key=str)
        if not 1 <= len(ids) <= 100:
            raise ValidationError("Select 1–100 tickets.")
        action = request.POST.get("action")
        if action not in ("status", "priority", "assign_me", "unassign"):
            raise ValidationError("Unknown bulk action.")
        with transaction.atomic():
            found = list(Ticket.objects.select_for_update().filter(pk__in=ids, merged_into__isnull=True).order_by("id"))
            if len(found) != len(ids):
                raise ValidationError("A selected ticket was moved or removed. Refresh the list.")
            for ticket in found:
                kwargs = {}
                if action == "status":
                    kwargs["status_id"] = int(request.POST.get("status", "0"))
                elif action == "priority":
                    kwargs["priority_id"] = int(request.POST.get("priority", "0"))
                else:
                    kwargs.update(change_assignee=True, assignee_id=request.user.pk if action == "assign_me" else None)
                services.update_ticket(ticket_id=ticket.pk, actor=request.user, **kwargs)
        messages.success(request, f"Updated {len(ids)} tickets.")
    except (ValueError, ValidationError) as exc:
        messages.error(request, error_text(exc))
    return redirect("support-staff:list")


@portal_required(staff=True)
@require_GET
def export(request):
    qs, form = filter_tickets(request, staff=True)
    if not form.is_valid():
        return HttpResponse("Invalid filters", status=400)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="shvya-support-tickets.csv"'
    writer = csv.writer(response)
    writer.writerow(["Reference", "Subject", "Organization", "Requester", "Pipeline", "Status", "Priority", "Assignee", "Created", "Updated"])
    for t in qs.select_related("organization", "requester", "pipeline", "status", "priority", "assignee")[:5000]:
        writer.writerow([safe_csv(value) for value in [t.reference, t.subject, t.organization.name, t.requester.email,
             t.pipeline.name if t.pipeline else "", t.status.name, t.priority.name,
             t.assignee.name if t.assignee else "", t.created_at.isoformat(), t.updated_at.isoformat()]])
    return private(response)


@portal_required(staff=True)
def configuration(request, section="categories", object_id=None):
    if request.method not in ("GET", "POST"):
        return HttpResponse(status=405)
    if section not in CONFIG_FORMS:
        raise Http404()
    model, form_class = CONFIG_FORMS[section]
    instance = SupportSettings.load() if section == "settings" else (get_object_or_404(model, pk=object_id) if object_id else None)
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save()
            ConfigurationEvent.objects.create(actor=request.user, section=section,
                object_key=str(item.pk), changed_fields=list(form.changed_data))
        messages.success(request, f"{section.replace('_', ' ').title()} saved.")
        return redirect("support-staff:configuration", section=section)
    context = base_context(request, staff=True)
    context.update(config_sections=list(CONFIG_FORMS), section=section, option_form=form, editing=bool(instance),
        options=Paginator(model.objects.all(), 30).get_page(request.GET.get("page")),
        deliveries=EmailDelivery.objects.select_related("event__ticket", "recipient").order_by("-available_at")[:30] if section=="settings" else [],
        receipts=InboundReceipt.objects.order_by("-created_at")[:30] if section=="settings" else [])
    return private(render(request, "support/configuration.html", context, status=400 if request.method=="POST" else 200))
