# Public website analytics and SEO

## Activation

1. In Google Analytics, select or create SHVYA AI's GA4 property. Use India reporting timezone and INR. Add a web stream for `https://shvya-ai.com` and copy its `G-...` Measurement ID.
2. Set `GOOGLE_ANALYTICS_ID` in production environment and restart the Django web service. Never use an API secret. The public marketing base includes the tag; dashboards and private workspaces do not.
3. Disable ALL enhanced measurement (including browser-history pageviews, site search, forms, outbound clicks, downloads and videos) in the GA4 web stream to avoid duplicate custom events and unintended form data collection. Audit any other installed Google tags before activation.
4. In Search Console, verify the `shvya-ai.com` Domain property using Google's DNS TXT record. Alternatively set `GOOGLE_SITE_VERIFICATION` to the URL-prefix property's HTML token. Submit `https://shvya-ai.com/sitemap.xml` after verification. DNS verification and submission require Google/domain access; code cannot do them.
5. Link Search Console to the GA4 web stream in GA4 Admin product links. Configure internal traffic before activating its exclusion filter; use test mode first.
6. Mark `generate_lead` as a key event. Count once per event. It represents a saved demo request, not a confirmed appointment. Never mark CTA clicks or form submits as completed leads.
7. Set appropriate retention and access roles in GA4 Admin. Check the cookie/privacy policies against the activated collection configuration.

## Events

| Event | Meaning |
| --- | --- |
| page_view | Public page visit after analytics permission |
| article_view | Published blog article (.blog-article), excluding generic/legal article elements |
| demo_click | Link to the demo request page |
| demo_form_submit | Form submission attempt; validation may still fail |
| generate_lead | Server-rendered saved request confirmation; deduplicated per reference per browser session |
| whatsapp_click / phone_click / email_click | Contact action, without recipient or message text |
| outbound_click | External HTTP link; no target URL or domain is sent |
| content_scroll | 50% or 90% of scrollable page reached |

No Google script loads before explicit permission. Preferences can be reopened and collection disabled. Query strings, fragments, form values and booking references are not sent by the custom events. Referrers are limited to allowlisted search-engine origins; other referrers are omitted. Query stripping also removes automatic UTM campaign attribution; do not expect campaign-level reporting until a reviewed attribution design is added. Local browser storage keeps preferences and conversion deduplication markers. This does not replace the existing blog view counter.

## SEO coverage

The marketing base supplies a canonical URL from the configured origin, robots directives and social metadata. Blog pages retain their own canonical, Article/Breadcrumb structured data, previews and noindex behavior. The existing sitemap includes core pages and published, indexable articles. Robots excludes private operational areas; robots is not access control. Search Console verification supports both DNS and HTML ownership methods.

## Release verification

- Confirm one canonical per page on home, pricing, features and a blog article.
- Confirm robots and sitemap return 200 and published URLs use the HTTPS canonical host.
- With clean storage, confirm no Google network requests before permission, or after decline.
- Accept analytics and check GA4 Realtime / Tag Assistant for one page_view.
- Submit an invalid demo form: no generate_lead. Save a valid request: one generate_lead. Reload confirmation: no duplicate within the same session.
- Check outgoing payloads contain no names, email addresses, phone numbers, form text, URL query strings or reference IDs.
- Confirm private dashboard pages and draft previews load no marketing tag.
- Inspect key pages in Search Console and review Page indexing, Core Web Vitals and Search performance. Indexing and rankings are not guaranteed by configuration.

## Privacy hardening and signup

`GOOGLE_ANALYTICS_ID` remains blank by default in all environments. The approved SHVYA stream is `G-LE6XQSCB67` (property 514335326, stream 13047785975). Set only the production web environment; do not put an API secret here. Host allowlisting and DEBUG=False are both required. Resolved public route names are allowlisted; authenticated users, dedicated CRM/superadmin cookies and previews are excluded. Stale dedicated cookies also suppress collection conservatively. Default Django anonymous sessions remain eligible so saved demo and signup receipts work.

`sign_up` fires only on the one-use public `/signup/complete/` receipt generated after the account transaction succeeds; it means account created, not email verified. No analytics is installed on login or email-verification pages. Direct visits/reloads without a receipt redirect to login. Mark `sign_up` and `generate_lead` as key events (once per event). Contact/demo enquiries use the existing booking form; there is no separate public contact-submission endpoint. Contact CTA clicks remain engagement, never completed leads.

Privacy signals (DNT/GPC) suppress collection, including previously stored consent. Storage access exceptions do not break the page. Withdrawal expires this deployment's GA cookies and propagates to other open tabs. GA cookies have a 90-day lifetime with renewal disabled. All enhanced measurement must stay off: custom pageviews alone do not disable automatic history/search/form collection. Titles are a fixed public label; use sanitized page paths to identify content. No UTM query attribution is collected.

Search Console domain ownership and sitemap submission were confirmed by the coordinating SEO chat on 2026-10-17. Preserve that DNS verification record and PR #693's documentation/blog canonical overrides. Link the existing domain property in GA4 rather than creating another property.

Regression commands: `node --test tests/js/marketing-analytics.test.cjs`; `pytest tests/test_marketing_analytics.py apps/accounts/tests/test_signup_verification.py apps/accounts/tests/test_free_signup.py`. PostgreSQL-dependent signup tests must run in CI if local PostgreSQL is unavailable. Deployment must pass the repository CI/Security gates. Activation additionally requires the production environment setting and live network/Realtime verification; a successful code deployment alone does not prove collection.
