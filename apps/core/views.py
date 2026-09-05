from django.views.generic import TemplateView

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
