# Instagram connection setup

SHVYA's Instagram inbox uses Meta's Instagram API with Instagram Login for professional accounts.

## Meta app setup

Use the same Meta Business app credentials already configured for WhatsApp when that app also has the Instagram product enabled, or configure the deployment's `META_APP_ID` and `META_APP_SECRET` with the Meta app that owns the Instagram integration.

In Meta App Dashboard:

1. Add/configure Instagram API with Instagram Login.
2. Add this exact OAuth redirect URL for production:
   `https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/`
3. Request these scopes for the Instagram login configuration:
   - `instagram_business_basic`
   - `instagram_business_manage_messages`
4. Complete the Meta access level / App Review requirements before connecting customer accounts outside your app roles.

## Runtime behavior

- Instagram credentials are organization-scoped and the access token is encrypted before it is saved in `Organization.settings`.
- The Chats page loads conversations from the connected Instagram professional account through Meta's API.
- A reply is sent only to the Instagram-scoped participant returned by that conversation.
- Meta requires the Instagram user to have initiated the conversation before the professional account can reply through the Send API.
- No database migration is required for this first Instagram release.
