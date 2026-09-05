from django.views.generic import TemplateView

from apps.crm.authentication import get_crm_authenticated_user


class HomeView(TemplateView):
    template_name = "home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["crm_user"] = get_crm_authenticated_user(self.request)
        return context
