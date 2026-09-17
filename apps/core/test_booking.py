from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.core.booking import BAC_ORGANIZATION_ID, BookingUnavailable, save_booking
from apps.core.forms import MarketingBookingForm
from apps.core.models import MarketingBookingRequest
from apps.crm.models import Lead
from apps.organizations.models import Organization
from apps.superadmin.bac_views import bac_list


class BookingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(id=BAC_ORGANIZATION_ID, name="SHVYA")
        self.other = Organization.objects.create(name="Other business")
        self.data = dict(name="Asha", email="asha@example.com", phone="+919876543210",
                         company="Asha Services", preferred_date=timezone.localdate() + timedelta(days=1),
                         preferred_time="11:00", goal="Recover enquiries", interest="Services", consent=True)

    def test_fixed_tenant_and_new_leads_stage(self):
        booking = save_booking({**self.data, "organization_id": str(self.other.pk)})
        self.assertEqual(str(booking.lead.organization_id), BAC_ORGANIZATION_ID)
        self.assertIn(booking.lead.stage.name.casefold(), ("new lead", "new leads"))
        self.assertEqual(booking.lead.pipeline_id, booking.lead.stage.pipeline_id)
        self.assertFalse(self.other.leads.exists())
        self.assertIn(str(booking.pk), booking.lead.notes)

    def test_repeats_keep_requests_and_reuse_lead(self):
        first = save_booking(self.data)
        second = save_booking(self.data)
        self.assertEqual(first.lead_id, second.lead_id)
        self.assertEqual(MarketingBookingRequest.objects.count(), 2)
        self.assertEqual(Lead.objects.count(), 1)

    def test_booking_failure_rolls_back_lead(self):
        with patch.object(MarketingBookingRequest.objects, "create", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                save_booking(self.data)
        self.assertFalse(Lead.objects.exists())

    def test_missing_destination_does_not_fallback(self):
        self.org.is_active = False
        self.org.save(update_fields=["is_active"])
        with self.assertRaises(BookingUnavailable):
            save_booking(self.data)
        self.assertFalse(Lead.objects.exists())

    def test_missing_stage_does_not_fallback(self):
        self.org.pipelines.update(is_active=False)
        with self.assertRaises(BookingUnavailable):
            save_booking(self.data)
        self.assertFalse(MarketingBookingRequest.objects.exists())

    def test_form_validates_phone_consent_date_and_honeypot(self):
        form = MarketingBookingForm({**self.data, "phone": "98765 43210"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["phone"], "+919876543210")
        for values in ({"phone": "hello"}, {"consent": False}, {"website": "spam"},
                       {"preferred_date": timezone.localdate() - timedelta(days=1)}):
            self.assertFalse(MarketingBookingForm({**self.data, **values}).is_valid())

    def test_bac_only_superuser(self):
        request = RequestFactory().get("/superadmin/bac/")
        for user in (AnonymousUser(), SimpleNamespace(is_authenticated=True, is_superuser=False)):
            request.user = user
            self.assertEqual(bac_list(request).status_code, 302)
        save_booking(self.data)
        request.user = SimpleNamespace(is_authenticated=True, is_superuser=True)
        with patch("apps.superadmin.bac_views.render") as render:
            bac_list(request)
            self.assertEqual(render.call_args.args[2]["page_obj"].paginator.count, 1)

    def test_permanent_lead_deletion_removes_owned_booking_data(self):
        booking = save_booking(self.data)
        booking.lead.delete()
        self.assertFalse(MarketingBookingRequest.objects.exists())
