import json
from datetime import timedelta

from django.http import Http404
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import (
    Lead,
    LeadReminder,
    LeadReminderNotificationAck,
    Pipeline,
)
from apps.crm.views.dashboard import (
    global_reminder_snooze,
    reminder_notification_ack,
    reminder_notification_feed,
)
from apps.organizations.models import Organization
from services.crm.reminder_notification_service import (
    acknowledge_reminder_notification,
    due_reminder_notifications,
    reset_reminder_notification_acknowledgements,
)


class ReminderNotificationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.organization = Organization.objects.create(name="Reminder Notifications Org")
        self.pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Leads",
        )
        self.stage = self.pipeline.stages.order_by("display_order").first()
        self.user = User.objects.create_user(
            email="reminder-owner@example.com",
            organization=self.organization,
            password="test-password",
            name="Reminder Owner",
            role=User.Role.ADMIN,
        )
        self.other_user = User.objects.create_user(
            email="reminder-other@example.com",
            organization=self.organization,
            password="test-password",
            name="Other User",
            role=User.Role.AGENT,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Reminder Lead",
            phone="+919000001111",
        )

    def _reminder(self, *, due_at=None, status="pending", title="Follow up"):
        return LeadReminder.objects.create(
            lead=self.lead,
            assigned_to=self.user,
            title=title,
            description="Call the lead about their requirement.",
            due_at=due_at or (timezone.now() - timedelta(minutes=1)),
            status=status,
        )

    def _request(self, method="get"):
        request = getattr(self.factory, method)("/dashboard/reminders/notifications/")
        request.crm_user = self.user
        request.user = self.user
        return request

    def test_feed_returns_only_due_pending_unacknowledged_reminders(self):
        due = self._reminder(title="Due now")
        self._reminder(
            title="Future",
            due_at=timezone.now() + timedelta(hours=1),
        )
        self._reminder(
            title="Completed",
            status="completed",
        )

        reminders = due_reminder_notifications(user=self.user)

        self.assertEqual([item.pk for item in reminders], [due.pk])

    def test_acknowledgement_is_per_user_and_does_not_complete_reminder(self):
        reminder = self._reminder()

        acknowledge_reminder_notification(
            user=self.user,
            reminder=reminder,
        )
        reminder.refresh_from_db()

        self.assertEqual(reminder.status, "pending")
        self.assertFalse(
            any(item.pk == reminder.pk for item in due_reminder_notifications(user=self.user))
        )
        self.assertTrue(
            any(
                item.pk == reminder.pk
                for item in due_reminder_notifications(user=self.other_user)
            )
        )

    def test_reset_acknowledgements_rearms_notification(self):
        reminder = self._reminder()
        acknowledge_reminder_notification(
            user=self.user,
            reminder=reminder,
        )
        self.assertTrue(
            LeadReminderNotificationAck.objects.filter(
                reminder=reminder,
                user=self.user,
            ).exists()
        )

        reset_reminder_notification_acknowledgements(reminder=reminder)

        self.assertFalse(
            LeadReminderNotificationAck.objects.filter(
                reminder=reminder,
                user=self.user,
            ).exists()
        )
        self.assertTrue(
            any(item.pk == reminder.pk for item in due_reminder_notifications(user=self.user))
        )

    def test_notification_feed_returns_existing_reminder_and_ack_url(self):
        reminder = self._reminder(title="Existing website reminder")
        response = reminder_notification_feed.__wrapped__(self._request("get"))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(len(payload["notifications"]), 1)
        item = payload["notifications"][0]
        self.assertEqual(item["id"], str(reminder.pk))
        self.assertEqual(item["title"], "Existing website reminder")
        self.assertEqual(item["lead_name"], self.lead.name)
        self.assertIn(str(reminder.pk), item["ack_url"])
        self.assertEqual(payload["pending_count"], 1)

    def test_notification_ack_is_tenant_scoped_and_keeps_reminder_pending(self):
        reminder = self._reminder()
        request = self._request("post")
        response = reminder_notification_ack.__wrapped__(request, reminder.pk)

        self.assertEqual(response.status_code, 200)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "pending")
        self.assertTrue(
            LeadReminderNotificationAck.objects.filter(
                reminder=reminder,
                user=self.user,
            ).exists()
        )

        other_org = Organization.objects.create(name="Other Reminder Org")
        foreign_user = User.objects.create_user(
            email="foreign-reminder@example.com",
            organization=other_org,
            password="test-password",
            name="Foreign User",
            role=User.Role.ADMIN,
        )
        foreign_request = self.factory.post("/dashboard/reminders/notifications/ack/")
        foreign_request.crm_user = foreign_user
        foreign_request.user = foreign_user
        with self.assertRaises(Http404):
            reminder_notification_ack.__wrapped__(
                foreign_request,
                reminder.pk,
            )

    def test_snooze_clears_ack_and_notification_returns_only_when_due_again(self):
        reminder = self._reminder()
        acknowledge_reminder_notification(
            user=self.user,
            reminder=reminder,
        )

        request = self.factory.post("/dashboard/reminders/snooze/")
        request.crm_user = self.user
        request.user = self.user
        response = global_reminder_snooze.__wrapped__(request, reminder.pk)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            LeadReminderNotificationAck.objects.filter(
                reminder=reminder,
                user=self.user,
            ).exists()
        )
        reminder.refresh_from_db()
        self.assertGreater(reminder.due_at, timezone.now())
        self.assertFalse(
            any(item.pk == reminder.pk for item in due_reminder_notifications(user=self.user))
        )

        reminder.due_at = timezone.now() - timedelta(seconds=1)
        reminder.save(update_fields=["due_at", "updated_at"])
        self.assertTrue(
            any(item.pk == reminder.pk for item in due_reminder_notifications(user=self.user))
        )
