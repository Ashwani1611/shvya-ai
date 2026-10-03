from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.integrations.services.email import EmailConfigurationError
from apps.organizations.models import Organization
from .cancellation_services import cancel_booking
from .google import GoogleCalendarError
from .google_reconciliation import import_google_booking
from .lead_capture_services import notify_submission
from .models import CalendarBooking, CalendarReminderDelivery, CalendarReminderSequence, CalendarReminderStep
from . import test_workspace as fixtures


class GoogleBookingImportTests(TestCase):
    setUp = fixtures.CalendarWorkspaceTests.setUp
    authenticate = fixtures.CalendarWorkspaceTests.authenticate
    url = fixtures.CalendarWorkspaceTests.url

    def prepare_event(self):
        self.booking.google_event_id = self.booking.id.hex
        self.booking.google_calendar_id = 'host@example.com'
        self.booking.calendar_sync_status = CalendarBooking.SyncStatus.SYNCED
        self.booking.save(update_fields=['google_event_id', 'google_calendar_id', 'calendar_sync_status', 'updated_at'])
        return {
            'id': self.booking.google_event_id, 'status': 'confirmed',
            'start': {'dateTime': '2026-10-06T14:00:00+05:30'},
            'end': {'dateTime': '2026-10-06T14:45:00+05:30'},
            'htmlLink': 'https://calendar.google.com/calendar/event?eid=event1',
        }

    def import_event(self, event, side_effect=None):
        response = SimpleNamespace(ok=True, status_code=200, json=lambda: event)
        with patch('apps.shvya_calendar.google_reconciliation.connection_for_booking', return_value=SimpleNamespace(calendar_id='primary')), patch('apps.shvya_calendar.google_reconciliation._headers', return_value={}), patch('apps.shvya_calendar.google_reconciliation._admit_google_request'), patch('apps.shvya_calendar.google_reconciliation._google_request', side_effect=side_effect, return_value=response) as request:
            result = import_google_booking(self.booking)
        return result, request

    @patch('apps.shvya_calendar.reminder_services.schedule_booking_reminders')
    def test_google_move_updates_booking_booked_at_and_reminders_without_echo(self, reminders):
        event = self.prepare_event()
        old_start = self.booking.start_at
        result, request = self.import_event(event)
        self.assertEqual(result, 'updated')
        self.booking.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(self.booking.start_at, datetime(2026, 10, 6, 8, 30, tzinfo=UTC))
        self.assertEqual(self.booking.end_at - self.booking.start_at, timedelta(minutes=45))
        self.assertEqual(self.booking.previous_start_at, old_start)
        self.assertEqual(self.lead.attributes['booked_at'], '2026-10-06T14:00')
        self.assertEqual(self.booking.calendar_sync_status, 'synced')
        reminders.assert_called_once()
        self.assertEqual(request.call_args.args[0], 'GET')
        self.assertIn('host%40example.com', request.call_args.args[1])
        revision = self.booking.updated_at
        result, _ = self.import_event(event)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.updated_at, revision)
        self.assertEqual(result, 'unchanged')
        reminders.assert_called_once()

    def test_google_cancel_clears_booked_at_and_skips_reminders(self):
        event = self.prepare_event()
        sequence = CalendarReminderSequence.objects.create(page=self.page)
        step = CalendarReminderStep.objects.create(sequence=sequence, name='Reminder', channel='email', body='Hello')
        delivery = CalendarReminderDelivery.objects.create(booking=self.booking, step=step, due_at=timezone.now())
        result, _ = self.import_event({'id': event['id'], 'status': 'cancelled'})
        self.assertEqual(result, 'cancelled')
        self.booking.refresh_from_db()
        self.lead.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(self.booking.status, 'cancelled')
        self.assertEqual(self.lead.attributes['booked_at'], '')
        self.assertEqual(delivery.status, 'skipped')

    def test_pending_local_change_is_not_overwritten(self):
        event = self.prepare_event()
        self.booking.calendar_sync_status = 'pending'
        result, request = self.import_event(event)
        self.assertEqual(result, 'pending_local')
        request.assert_not_called()

    def test_local_reschedule_during_http_is_not_overwritten(self):
        event = self.prepare_event()
        newer = timezone.now() + timedelta(days=7)
        def concurrent_edit(*args, **kwargs):
            CalendarBooking.objects.filter(pk=self.booking.pk).update(start_at=newer, updated_at=timezone.now(), calendar_sync_status='pending')
            return SimpleNamespace(ok=True, json=lambda: event)
        result, _ = self.import_event(event, side_effect=concurrent_edit)
        self.assertEqual(result, 'changed_locally')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.start_at, newer)

    def test_wrong_event_and_invalid_duration_are_rejected(self):
        event = self.prepare_event()
        with self.assertRaises(GoogleCalendarError):
            self.import_event({**event, 'id': 'another-event'})
        with self.assertRaises(GoogleCalendarError):
            self.import_event({**event, 'end': event['start']})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'scheduled')

    @patch('apps.shvya_calendar.tasks.sync_booking_calendar.delay')
    def test_delete_endpoint_clears_booked_at_and_queues_google_delete(self, enqueue):
        self.prepare_event()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url('booking_update'), {'action': 'cancel'})
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(self.booking.status, 'cancelled')
        self.assertEqual(self.booking.calendar_sync_status, 'pending')
        self.assertEqual(self.lead.attributes['booked_at'], '')
        enqueue.assert_called_once_with(str(self.booking.pk))
        with self.captureOnCommitCallbacks(execute=True):
            cancel_booking(self.booking)
        enqueue.assert_called_once()

    @patch('apps.shvya_calendar.google.cancel_booking_event')
    def test_cancellation_task_deletes_google_event(self, delete):
        from .tasks import sync_booking_calendar
        self.prepare_event()
        CalendarBooking.objects.filter(pk=self.booking.pk).update(status='cancelled', calendar_sync_status='pending')
        result = sync_booking_calendar.run(str(self.booking.pk))
        self.assertEqual(result['status'], 'cancelled')
        delete.assert_called_once()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.calendar_sync_status, 'synced')

    def test_public_status_is_token_scoped_and_honors_toggle(self):
        self.prepare_event()
        self.booking.google_event_url = 'https://calendar.google.com/calendar/event?eid=event1'
        self.booking.save(update_fields=['google_event_url'])
        endpoint = reverse('shvya_calendar_public:booking_status', kwargs={'booking_id': self.booking.pk, 'cancel_token': self.booking.cancel_token})
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['event_url'], self.booking.google_event_url)
        self.assertNotIn(self.lead.email, str(response.json()))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.page.show_add_calendar = False
        self.page.save(update_fields=['show_add_calendar'])
        self.assertEqual(self.client.get(endpoint).json()['event_url'], '')
        bad = reverse('shvya_calendar_public:booking_status', kwargs={'booking_id': self.booking.pk, 'cancel_token': 'wrong'})
        self.assertEqual(self.client.get(bad).status_code, 404)

    def test_foreign_user_cannot_delete_or_focus_booking(self):
        foreign = Organization.objects.create(name='Foreign', package='enterprise')
        user = User.objects.create_user(email='foreign@example.com', password='test', organization=foreign, name='Other', role=User.Role.ADMIN)
        self.authenticate(user)
        self.assertEqual(self.client.post(self.url('booking_update'), {'action': 'cancel'}).status_code, 404)
        endpoint = reverse('shvya_calendar:calendar')
        self.assertEqual(self.client.get(endpoint, {'booking': self.booking.pk}).status_code, 404)

    @patch('apps.shvya_calendar.tasks.import_google_booking_task.delay')
    def test_poll_rotates_and_skips_pending_local_updates(self, enqueue):
        from .tasks import import_google_changes
        self.prepare_event()
        self.booking.end_at = timezone.now() + timedelta(days=7)
        self.booking.save(update_fields=['end_at'])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(import_google_changes()['queued'], 1)
        self.assertEqual(import_google_changes()['queued'], 0)
        enqueue.assert_called_once_with(str(self.booking.pk))
        CalendarBooking.objects.filter(pk=self.booking.pk).update(google_checked_at=None, calendar_sync_status='pending')
        self.assertEqual(import_google_changes()['queued'], 0)


class BookingNotificationTests(TestCase):
    setUp = fixtures.CalendarWorkspaceTests.setUp
    authenticate = fixtures.CalendarWorkspaceTests.authenticate

    @patch('apps.shvya_calendar.lead_capture_services.send_organization_email', return_value=1)
    def test_host_and_selected_users_use_connect_hub_mailbox(self, send):
        selected = User.objects.create_user(email='selected@example.com', password='test', organization=self.org, name='Selected', role=User.Role.AGENT)
        foreign = Organization.objects.create(name='Other')
        outsider = User.objects.create_user(email='outsider@example.com', password='test', organization=foreign, name='Other', role=User.Role.AGENT)
        self.page.notify_user_ids = [str(selected.pk), str(outsider.pk)]
        self.page.notify_roles = []
        self.page.save(update_fields=['notify_user_ids', 'notify_roles'])
        notify_submission(self.submission.pk)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['organization'], self.org)
        self.assertEqual(set(kwargs['to']), {self.user.email, selected.email})
        self.assertIn(self.lead.phone, kwargs['text_body'])

    @patch('apps.shvya_calendar.lead_capture_services.send_organization_email', side_effect=EmailConfigurationError('Disconnected'))
    def test_disconnected_mailbox_does_not_break_lead_capture(self, send):
        notify_submission(self.submission.pk)
        send.assert_called_once()
        self.assertTrue(self.submission.__class__.objects.filter(pk=self.submission.pk).exists())
