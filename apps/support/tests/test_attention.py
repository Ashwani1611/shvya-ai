"""Reply-required state uses real ticket services, permissions and middleware."""
from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import Http404
from django.template import Context, Template
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.organizations.models import Organization
from apps.support import services
from apps.support.attention import attention_count
from apps.support.mail import ingest_verified_email
from apps.support.models import (
    OrganizationSupportPolicy, SupportSettings, TicketCategory, TicketIssue,
    TicketPriority, TicketStatus,
)
from apps.support.templatetags.support_attention import support_attention_state


@override_settings(
    ALLOWED_HOSTS=["testserver"], DEBUG=False, SECURE_SSL_REDIRECT=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class SupportAttentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.org = Organization.objects.create(name="Attention organization")
        cls.other_org = Organization.objects.create(name="Other attention organization")
        cls.owner = users.objects.create_user(
            email="attention-owner@example.test", organization=cls.org, name="Owner", role="admin",
        )
        cls.agent = users.objects.create_user(
            email="attention-agent@example.test", organization=cls.org, name="Agent", role="agent",
        )
        cls.other = users.objects.create_user(
            email="attention-other@example.test", organization=cls.other_org, name="Other", role="admin",
        )
        cls.staff = users.objects.create_superuser(email="attention-ops@example.test", name="Ops")
        cls.category = TicketCategory.objects.create(name="Attention regressions")
        cls.issue = TicketIssue.objects.create(category=cls.category, name="Support reply")
        cls.priority, _ = TicketPriority.objects.get_or_create(key="medium", defaults={"name": "Medium"})
        for key in ("open", "in_progress", "answered", "on_hold", "closed"):
            TicketStatus.objects.get_or_create(
                key=key, defaults={"name": key, "behavior": key, "system": True},
            )

    def setUp(self):
        context = patch("apps.support.notifications.wake_delivery", lambda: None)
        context.start()
        self.addCleanup(context.stop)
        self.client = Client(enforce_csrf_checks=True)
        self.login(self.owner)

    def login(self, user, area="dashboard"):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies[get_session_cookie_name(area)] = session.session_key

    def create_ticket(self, actor=None):
        return services.create_ticket(
            actor=actor or self.owner, category_id=self.category.pk, issue_id=self.issue.pk,
            priority_id=self.priority.pk, subject="Support indicator", body="Issue details",
        )

    def reply(self, ticket, actor=None, **extra):
        return services.reply(ticket_id=ticket.pk, actor=actor or self.staff, body="Reply details", **extra)

    def status(self, ticket, key, actor=None):
        return services.update_ticket(
            ticket_id=ticket.pk, actor=actor or self.staff,
            status_id=TicketStatus.objects.get(key=key).pk,
        )

    def test_new_ticket_does_not_alert_until_public_staff_reply(self):
        ticket = self.create_ticket()
        self.assertEqual(attention_count(self.owner), 0)
        self.reply(ticket, internal=True)
        self.assertEqual(attention_count(self.owner), 0)
        self.reply(ticket)
        self.assertEqual(attention_count(self.owner), 1)
        self.reply(ticket, internal=True)
        self.assertEqual(attention_count(self.owner), 1)

    def test_any_authorized_organization_user_reply_clears_for_the_team(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        self.assertEqual(attention_count(self.owner), 1)
        self.assertEqual(attention_count(self.agent), 1)
        self.reply(ticket, self.agent)
        self.assertEqual(attention_count(self.owner), 0)
        self.assertEqual(attention_count(self.agent), 0)
        self.reply(ticket)
        self.assertEqual(attention_count(self.agent), 1)

    def test_get_list_detail_and_attention_never_acknowledges(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        ticket.refresh_from_db()
        version = ticket.version
        for path in (
            reverse("support-client:list"),
            reverse("support-client:detail", args=[ticket.pk]),
            reverse("support-client:attention"),
        ):
            self.assertEqual(self.client.get(path, secure=True).status_code, 200)
            self.assertEqual(attention_count(self.owner), 1)
        ticket.refresh_from_db()
        self.assertEqual(ticket.version, version)

    def test_multiple_messages_count_as_one_and_all_tickets_must_be_handled(self):
        first, second = self.create_ticket(), self.create_ticket()
        self.reply(first)
        self.reply(first)
        self.reply(second)
        self.assertEqual(attention_count(self.agent), 2)
        self.reply(first, self.agent)
        self.assertEqual(attention_count(self.owner), 1)
        self.status(second, "closed", self.agent)
        self.assertEqual(attention_count(self.owner), 0)

    def test_nonclosing_status_edits_neither_create_nor_dismiss_reply_obligation(self):
        ticket = self.create_ticket()
        self.status(ticket, "answered")
        self.assertEqual(attention_count(self.owner), 0)
        self.reply(ticket, status_id=TicketStatus.objects.get(key="in_progress").pk)
        for key in ("in_progress", "open", "on_hold", "answered"):
            self.status(ticket, key)
            self.assertEqual(attention_count(self.owner), 1)
        custom = TicketStatus.objects.create(name="Waiting for client", key="attention-wait", behavior="on_hold")
        services.update_ticket(ticket_id=ticket.pk, actor=self.staff, status_id=custom.pk)
        self.assertEqual(attention_count(self.owner), 1)

    def test_closed_behavior_is_authoritative_even_for_personalized_labels(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        closed = TicketStatus.objects.create(name="Resolved for client", key="attention-resolved", behavior="closed")
        services.update_ticket(ticket_id=ticket.pk, actor=self.staff, status_id=closed.pk)
        self.assertEqual(attention_count(self.owner), 0)
        self.status(ticket, "open", self.owner)
        self.assertEqual(attention_count(self.owner), 1)
        self.reply(ticket, self.owner)
        self.assertEqual(attention_count(self.owner), 0)

    def test_unrelated_ticket_reply_does_not_clear_pending_ticket(self):
        pending, unrelated = self.create_ticket(), self.create_ticket()
        self.reply(pending)
        self.reply(unrelated, self.agent)
        self.assertEqual(attention_count(self.owner), 1)

    def test_own_contact_policy_is_not_weakened_for_the_badge(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        OrganizationSupportPolicy.objects.create(organization=self.org, own_tickets_only=True)
        self.assertEqual(attention_count(self.owner), 1)
        self.assertEqual(attention_count(self.agent), 0)
        with self.assertRaises(Http404):
            self.reply(ticket, self.agent)
        self.assertEqual(attention_count(self.owner), 1)

    def test_anonymous_shared_reply_is_not_an_organization_acknowledgement(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        _, token = services.issue_share(actor=self.owner, ticket_id=ticket.pk, label="Viewer", can_reply=True)
        grant = services.resolve_grant(token)
        services.reply(ticket_id=ticket.pk, grant=grant, body="Shared viewer response")
        self.assertEqual(attention_count(self.owner), 1)
        self.reply(ticket, self.agent)
        self.assertEqual(attention_count(self.owner), 0)

    def test_verified_email_reply_uses_the_same_rule(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        config = SupportSettings.load()
        config.email_intake_enabled = True
        config.email_replies_only = True
        config.save()
        mail = EmailMessage()
        mail["From"] = self.agent.email
        mail["To"] = "support@example.test"
        mail["Subject"] = f"Re: [{ticket.reference}] Response"
        mail["Message-ID"] = "<attention-ack@example.test>"
        mail.set_content("Acknowledged by an organization teammate.")
        ingest_verified_email(mail.as_bytes(), verified_sender=self.agent.email)
        self.assertEqual(attention_count(self.owner), 0)

    def test_failed_validation_and_csrf_post_never_clear_indicator(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        with self.assertRaises(ValidationError):
            services.reply(ticket_id=ticket.pk, actor=self.owner, body="")
        response = self.client.post(
            reverse("support-client:reply", args=[ticket.pk]), {"body": "Forged request"}, secure=True,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(attention_count(self.owner), 1)

    def test_notification_preferences_do_not_suppress_the_dashboard_indicator(self):
        ticket = self.create_ticket()
        config = SupportSettings.load()
        config.notify_customer = config.notify_staff = False
        config.save()
        OrganizationSupportPolicy.objects.create(organization=self.org, email_notifications=False)
        self.reply(ticket)
        self.assertEqual(attention_count(self.owner), 1)

    def test_merge_counts_only_the_canonical_public_conversation(self):
        first, second = self.create_ticket(), self.create_ticket()
        self.reply(first)
        self.reply(second)
        self.assertEqual(attention_count(self.owner), 2)
        services.merge_tickets(actor=self.staff, primary_id=first.pk, source_ids=[second.pk])
        self.assertEqual(attention_count(self.owner), 1)
        self.reply(first, self.agent)
        self.assertEqual(attention_count(self.owner), 0)

    def test_endpoint_is_tenant_scoped_private_get_only_and_payload_minimal(self):
        self.reply(self.create_ticket())
        self.reply(self.create_ticket(self.other))
        path = reverse("support-client:attention")
        response = self.client.get(path, {"organization_id": str(self.other_org.pk)}, secure=True)
        self.assertEqual(response.json(), {"count": 1})
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertEqual(self.client.put(path, secure=True).status_code, 403)
        self.client = Client()
        self.assertEqual(self.client.get(path, secure=True).status_code, 302)
        self.login(self.staff, "superadmin")
        self.assertNotEqual(self.client.get(path, secure=True).status_code, 200)

    def test_unauthorized_or_inactive_users_have_no_attention_state(self):
        ticket = self.create_ticket()
        self.reply(ticket)
        self.assertEqual(attention_count(self.other), 0)
        self.assertEqual(attention_count(self.staff), 0)
        self.assertEqual(attention_count(AnonymousUser()), 0)
        self.agent.is_active = False
        self.assertEqual(attention_count(self.agent), 0)
        self.owner.organization.is_active = False
        self.assertEqual(attention_count(self.owner), 0)

    def test_bootstrap_is_dashboard_only_and_uses_named_urls(self):
        request = RequestFactory().get("/dashboard/", secure=True)
        request.user = self.owner
        request.crm_user = self.owner
        request.shvya_session_area = "dashboard"
        self.reply(self.create_ticket())
        state = support_attention_state({"request": request})
        self.assertEqual(state, {"count": 1, "endpoint": reverse("support-client:attention"), "portal": reverse("support-client:list")})
        html = Template('{% include "support/partials/attention_assets.html" %}').render(Context({"request": request}))
        self.assertIn('id="shvya-support-attention"', html)
        request.shvya_session_area = "superadmin"
        self.assertIsNone(support_attention_state({"request": request}))
        self.assertIsNone(support_attention_state({}))

    def test_count_does_not_issue_per_ticket_queries_or_load_message_bodies(self):
        for _ in range(3):
            self.reply(self.create_ticket())
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(attention_count(self.owner), 3)
        self.assertLessEqual(len(queries), 3)
        self.assertFalse(any('"body"' in query["sql"] for query in queries))
