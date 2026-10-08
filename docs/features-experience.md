# Public Features experience

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.


`/features/` renders `templates/features.html` inside the same `marketing/dark_base.html`, header and footer as the current public landing page. Keep this interactive experience when changing the marketing theme; route tests assert its assets and controls so a static catalogue cannot silently replace it again.

The page-specific CSS and JavaScript are scoped to `.premium-features`. The existing sidebar product catalogue is represented by accessible tabs. The customer journey is an illustrative office-space enquiry, not a customer testimonial or a live account.


## Expanded product tour (8 October 2026)

The **01 / Your Entire Sales Day** tour now covers 16 capabilities in four groups:

- **Sales workspace:** CRM, Sales Desk, SHVYA Calendar, Call Intelligence, SHVYA Sales.
- **AI & automation:** AI Playbooks, AI Engagement, Cadence, Workflows, Insights.
- **Channels & integrations:** WhatsApp, Instagram, Connect Hub.
- **Client operations:** SHVYA Vault, Teams, Help & Support.

The tour draws its active sidebar names from `apps/core/context_processors.py` and checks underlying code and product documentation. AI Engagement describes the configured AI runtime rather than promising always-on replies. Vault is deliberately identified as **staff-managed onboarding**, not a dashboard sidebar module. Integration options may require provider setup and permissions; the public tour is illustrative, not evidence of an active customer connection.

**Maintenance contract:** keep `templates/features.html` tab IDs / `data-feature` values in sync with the feature definitions and documentation routes in `static/marketing/premium-features.js`. The tab list must remain keyboard-accessible (arrow keys, Home/End), visible on small screens and scrollable on desktop. For any new module, add a public-safe guide in `apps/core/docs_articles/` first; avoid linking marketing visitors to login-only dashboard URLs.

The test in `apps/core/tests/test_features_page.py` asserts coverage and that historical “upcoming” claims for Touchpoints and the call scheduler are not reintroduced. After deployment, verify the new asset version and that every guide link resolves.

## Mascot and accessibility

The mascot body derives from the user-supplied orb artwork, with independently drawn eyes. Pointer tracking, hover listening, greeting, wink, nod, thinking and celebration are presentation states only. Intersection observation limits pointer updates to visible mascots. Motion pauses when requested, follows the system reduced-motion preference, and stops while the page is hidden. All click interactions also support keyboard activation; both tab groups support arrow keys, Home and End.

## Film

`shvya-cinematic-film.mp4` is a 30-second original motion-design film. Its fictional office establishing frame and laptop close-up were generated for Shvya; they are not footage of actual staff or customers. Cinematic camera movement and product overlays are composed around these still scenes. The page labels them as illustrative AI-generated scenes. It uses the original Shvya narration and matching English VTT captions, and provides a text transcript. No x.ai footage, script or brand assets are used.

The film loads only after a visitor opens the native dialog. Closing it pauses playback and restores keyboard focus to the play button. New filenames prevent caches from retaining the old film.

## Release verification

Check the public URL with and without the trailing slash, shared header links, all feature tabs, the four journey steps, motion pause, mobile overflow, film play/close/Escape and voice/captions. After deployment verify the page includes `premium-features.js` and `shvya-cinematic-film.mp4`, rather than relying only on the service health endpoint.


## Current platform naming

The public and authenticated product surfaces use the current module names **Sales Desk**, **Cadence**, **Playbooks**, **Workflows** and **Insights**. Historical implementation identifiers such as `copilot` and `smart-trigger` may remain in URLs, Python modules or test names and should not be copied into customer-facing navigation.

The public marketing shell supports day/night presentation. The initial theme follows the browser/system color preference unless the visitor has made an explicit theme choice, and the header control lets the visitor switch themes without changing product data or authentication state.

## Current authenticated workspaces

The staging product also exposes dedicated **SHVYA Sales**, **SHVYA Calendar**, and **Call Intelligence** workspaces. These names are customer-facing; internal app/module identifiers such as `sales`, `shvya_calendar` and `telephony` remain implementation details.
