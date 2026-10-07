import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.generic import FormView, TemplateView

from apps.core.docs_portal import docs_context
from apps.core.forms import MarketingBookingForm
from apps.core.booking import BookingUnavailable, save_booking
from apps.core.ratelimit import ratelimit
from apps.crm.authentication import get_crm_authenticated_user


class CRMUserContextMixin:
    """Expose the dedicated CRM user to public marketing templates."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["crm_user"] = get_crm_authenticated_user(self.request)
        return context


class HomeView(CRMUserContextMixin, TemplateView):
    template_name = "home.html"


class PricingView(CRMUserContextMixin, TemplateView):
    template_name = "pricing.html"


class FeaturesView(CRMUserContextMixin, TemplateView):
    template_name = "features.html"


class DocumentationView(CRMUserContextMixin, TemplateView):
    template_name = "docs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            docs_context(
                category=self.kwargs.get("category"),
                slug=self.kwargs.get("slug"),
                legacy_topic=self.request.GET.get("topic"),
                query=self.request.GET.get("q", ""),
            )
        )
        return context


class BookCallView(CRMUserContextMixin, FormView):
    template_name = "book_call.html"
    form_class = MarketingBookingForm
    success_url = "/book-a-call/?submitted=1"

    @method_decorator(ratelimit(limit=8, window=3600))
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        interest = self.request.GET.get("plan") or self.request.GET.get("interest") or ""
        initial["interest"] = interest[:140]
        if interest:
            initial["goal"] = f"I would like to discuss {interest} for my sales process."
        return initial

    def form_valid(self, form):
        try:
            booking = save_booking(form.cleaned_data)
        except BookingUnavailable:
            logging.getLogger(__name__).exception("BAC destination unavailable")
            form.add_error(None, "We could not save your request. Please try again shortly.")
            return self.form_invalid(form)
        reference = booking.pk
        self.request.session["bac_reference"] = str(reference)
        messages.success(self.request, f"Your request is saved. Reference: {reference}")
        return redirect(f"{self.success_url}&ref={reference}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        reference = self.request.session.get("bac_reference")
        context["booking_reference"] = reference if reference == self.request.GET.get("ref") else None
        return context
