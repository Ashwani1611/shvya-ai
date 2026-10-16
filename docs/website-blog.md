# Website blog

The company blog is a platform-owned Django feature under `apps/blog/`. It is not a customer CRM module. Articles are database-backed and can be published without a code deployment after the initial release and migration.

## Routes and navigation

- `/blog/`: searchable, paginated article archive with featured posts and categories.
- `/blog/<slug>/`: published article, related articles and contextual demo link.
- `/blog/category/<slug>/`: category archive derived from published content only.
- `/superadmin/blog/`: Blog Manager, also linked from the Superadmin sidebar.
- `/superadmin/blog/login/`: dedicated entry for authorised editorial accounts.
- `/sitemap.xml`: marketing pages and indexable published articles; split into sitemap pages when necessary.
- `/robots.txt`: public crawl policy and sitemap location.

## Writing and publication

Create an article in Blog Manager, add its title and body, upload a cover and inline images, and review the content/SEO settings. The visual editor supports headings, lists, links, tables, quotations and image captions. Preview uses the public article layout but requires editorial authentication.

The available workflow states are Draft, In Review, Scheduled, Published and Archived. Saving a published article changes its draft only. Publish explicitly replaces the live snapshot. This applies to all fields, including title, body, category, URL, author, images and SEO metadata. Restoring a revision restores a draft; it never publishes automatically.

The editor uses optimistic version checks. A stale browser tab must reload before overwriting newer content. Autosave and manual publication are coordinated by the editor so a delayed save cannot overwrite the just-published version.

Save, publish and restore requests keep the editor open when validation, authentication, network or server errors occur. Explicit actions temporarily lock editing while their snapshot is submitted. Requests have time limits and do not automatically retry publication. After signing in again in another tab, the editor uses the refreshed CSRF token for the next attempt.

Each new editor page carries a stable creation identifier. Retrying an identical initial draft save after a lost response returns the existing article instead of creating a duplicate. If content has changed or an action may already have completed, the editor preserves local edits and offers a draft backup and a link to the saved version for reconciliation.

Scheduling uses a frozen content snapshot and a timezone-labelled IST input. Saving changed content cancels its existing schedule; the editor explains that the revised article must be scheduled again. Celery Beat dispatches `apps.blog.tasks.publish_scheduled_posts` every minute on the general worker. Due posts are published transactionally and duplicate executions are safe. Authorisation is checked again at execution. Publication errors remain visible in Blog Manager; the job does not silently mark a failed publication as live. The scheduler catches up after a worker interruption, so publication may occur after the requested time during an outage.

Unpublishing removes the live snapshot from public pages, search, related content, sitemap and image-publication references. A changed published slug receives a permanent redirect. Old slugs remain reserved for their original article to prevent unrelated content taking over an existing link.

## Editorial authority

Active platform superusers can edit and publish. Restricted editorial accounts must be organization-free users with role `superadmin`, plus Django's `blog.change_post` permission. Publishing, scheduling and unpublishing additionally require `blog.publish_post`. A platform superuser can create the account under technical administration → Users, leave its organization blank and superuser checkbox unchecked, then edit its groups/user permissions. Restricted editors use the dedicated Blog login rather than technical administration.

Changing a name, email or active flag in Account Information preserves an existing account's authority. It no longer silently promotes legacy role-only accounts. To deliberately grant full platform authority, a superuser must set that flag explicitly in Users. Creating a new full Superadmin through Account Information retains the existing creation behavior.

The dedicated Superadmin session is reused. The middleware exception for restricted editors applies only to `/superadmin/blog/`; it grants no access to other Superadmin features. Customer organization users are rejected even if somebody assigns them a blog permission. Normal CRM login does not authenticate the Blog Manager.

## Images and HTML

Uploads accept still JPEG, PNG and WebP images up to 10 MiB and 25 megapixels. Empty, corrupt, animated and unsupported files are rejected with an explanation. Images are decoded, their orientation corrected, and re-encoded as WebP while preserving transparency, with a smaller responsive variant. The upload dialog shows progress and retains the selected file after a failed attempt so the editor can retry. Browser checks improve feedback; server-side validation remains authoritative.

Expected storage failures produce a safe temporary-unavailability response instead of exposing provider errors. Failed uploads clean up completed objects on a best-effort basis, with cleanup failures logged separately. Public and private media routes handle missing or corrupt objects consistently, and a missing thumbnail falls back to the main image. Deployment upload limits must remain larger than the 10 MiB image limit plus multipart overhead; the current 20 MiB Nginx limit meets that requirement.

Raw HTML is validated through a server-side allowlist; scripts, event handlers, arbitrary embeds and non-blog image URLs are excluded. Do not relax this boundary merely to support a pasted third-party widget.

Images use the configured default storage backend, including S3 in production. Stored objects contain authenticated ciphertext. Browser-visible URLs are stable application routes under `/blog/media/<image-id>/`; they are not expiring S3 signed URLs. Only images referenced by a currently published snapshot are publicly served. Private previews use `/superadmin/blog/media/<image-id>/`.

Back up database, media objects and the encryption key together. Preserve `SECRET_KEY_FALLBACKS` during key rotation while older encrypted media remains in use. Do not publish the raw storage objects or change the access policy of customer/Vault media.

## Search and sharing

Each article has an editable SEO title, meta description, clean slug, category/tags, optional canonical URL and indexing control, plus social-sharing title, description and image. Focus phrases are editorial guidance; the obsolete meta-keywords tag is not generated.

The complete article, metadata, canonical and `BlogPosting`/breadcrumb JSON-LD are server-rendered. JSON-LD values are escaped for their HTML context. Dates describe actual publication and live updates. Internal search/filter variants are `noindex`; paginated archive pages have their own canonical URLs. Draft previews are authenticated and `noindex`, and never enter the sitemap.

`BLOG_PUBLIC_ORIGIN` defaults to `https://shvya-ai.com`. Set it to the intended public website origin in a separate non-production deployment. Canonicals and sitemap locations never derive from a browser-supplied Host header. The existing site's default description remains intact for other marketing pages.

Search previews and completeness checks are advisory, not a promise of ranking. Google may select different titles/descriptions and decides whether/when to index a page.

## Launch and measurement

1. Release through the normal PR, CI/security and validated-main deployment path. Apply the blog migration before starting web and worker code for the release; run `collectstatic`.
2. Confirm web, general worker and Beat all run the same release, and confirm the configured storage is persistent.
3. Create a real article as a draft. Check desktop/mobile preview, images, headings, links, author information and SEO settings.
4. Publish and verify the public article, image URLs, canonical, source HTML and sitemap. Check unpublish and a scheduled test with a disposable draft before relying on scheduling.
5. In the site's verified Google Search Console property, submit `/sitemap.xml` and inspect a representative article URL. Validate structured data using Google's Rich Results Test and assess loading with PageSpeed Insights.
6. Monitor impressions, clicks, search queries and demo enquiries. Article demo links prefill the existing booking `interest` field with the originating blog title for attribution.

Search Console ownership/connection and third-party analytics require the site's account access and are not provisioned by this code migration. No third-party tracking script or fabricated traffic metrics are added.

## Validation

Run the focused regression suite with the repository test settings and PostgreSQL, together with Django checks and migration drift checks:

```bash
pytest apps/blog/tests
python manage.py check --settings=config.settings.testing
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
ruff check apps/blog apps/accounts/middleware.py config/urls.py config/celery.py
```

The focused tests cover the live/draft boundary, schedules, optimistic concurrency, creation retry recovery, tenant/editor permissions, HTML/upload validation, storage outages and cleanup failures, image visibility and thumbnail fallback, redirects, sitemap and SEO output. The wider CI gate remains authoritative for shared middleware/settings changes.
