# Instagram OAuth production checklist

Before enabling **Continue with Instagram** in production, confirm all of the following in Meta App Dashboard and the SHVYA VPS environment:

- Instagram API with Instagram Login is configured for the Meta app.
- `META_INSTAGRAM_APP_ID` is the Instagram App ID shown in the Instagram API setup.
- `META_INSTAGRAM_APP_SECRET` is the matching Instagram App Secret.
- Valid OAuth Redirect URI is exactly `https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/`.
- Webhook callback is `https://dashboard.shvya-ai.com/webhooks/instagram/`.
- Required scopes include `instagram_business_basic` and `instagram_business_manage_messages`.
- Webhook fields include `messages` and `messaging_postbacks`.
- App access/App Review is sufficient for accounts outside app roles/testers.
- App is Live before customer production use.
