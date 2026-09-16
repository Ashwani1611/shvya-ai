import uuid

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.generic import FormView, TemplateView

from apps.core.docs_content import DOC_TOPICS, docs_index
from apps.core.forms import MarketingBookingForm
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
        topic_slug = self.request.GET.get("topic") or "overview"
        query = (self.request.GET.get("q") or "").strip().lower()
        if topic_slug not in DOC_TOPICS:
            topic_slug = "overview"

        topics = DOC_TOPICS
        if query:
            topics = {
                slug: topic
                for slug, topic in DOC_TOPICS.items()
                if query in " ".join(
                    [topic["title"], topic["intro"], *[item for _, texts in topic["sections"] for item in texts]]
                ).lower()
            }

        context.update(
            {
                "topic_slug": topic_slug,
                "topic": DOC_TOPICS[topic_slug],
                "topics": docs_index(),
                "query": query,
                "filtered_topics": [(slug, topic["title"], topic["intro"]) for slug, topic in topics.items()],
            }
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
        reference = uuid.uuid4()
        messages.success(self.request, f"Your request is saved. Reference: {reference}")
        return redirect(f"{self.success_url}&ref={reference}")
