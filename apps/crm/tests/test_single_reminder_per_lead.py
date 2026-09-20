from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import Lead, LeadReminder, LeadReminderNotificationAck
from apps.organizations.models import Organization


class SingleLeadReminderTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Single Reminder Org")
        self.user = User.objects.create_user(
            email="single-reminder@example.com",
            password="test-password",
            name="Reminder Owner",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.pipeline = self.organization.pipelines.get(name="Leads")
        self.stage = self.pipeline.stages.order_by("display_order").first()
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="One Reminder Lead",
            phone="+919000009991",
        )

    def _create(self, *, title, status="pending", offset_minutes=30):
        return LeadReminder.objects.create(
            lead=self.lead,
            assigned_to=self.user,
            title=title,
            due_at=timezone.now() + timedelta(minutes=offset_minutes),
            status=status,
        )

    def test_new_reminder_deletes_previous_reminder_for_same_lead(self):
        old = self._create(title="Old reminder")
        LeadReminderNotificationAck.objects.create(
            reminder=old,
            user=self.user,
        )

        new = self._create(title="New reminder", offset_minutes=60)

        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertTrue(LeadReminder.objects.filter(pk=new.pk).exists())
        self.assertFalse(LeadReminder.objects.filter(pk=old.pk).exists())
        self.assertFalse(
            LeadReminderNotificationAck.objects.filter(reminder_id=old.pk).exists()
        )

    def test_new_reminder_replaces_completed_or_cancelled_history_too(self):
        old = self._create(title="Completed reminder", status="completed")
        new = self._create(title="Replacement")

        self.assertFalse(LeadReminder.objects.filter(pk=old.pk).exists())
        self.assertEqual(
            list(LeadReminder.objects.filter(lead=self.lead).values_list("pk", flat=True)),
            [new.pk],
        )

    def test_database_constraint_blocks_direct_duplicate_save(self):
        self._create(title="Existing")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LeadReminder(
                    lead=self.lead,
                    assigned_to=self.user,
                    title="Bypass manager",
                    due_at=timezone.now() + timedelta(hours=1),
                ).save(force_insert=True)

        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
