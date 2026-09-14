# Meta source of truth for Instagram Login

SHVYA uses Meta's **Instagram API with Instagram Login** for professional Business/Creator accounts.

The integration intentionally uses:

- OAuth host: `https://www.instagram.com/oauth/authorize`
- Token exchange: `https://api.instagram.com/oauth/access_token`
- Graph host: `https://graph.instagram.com`
- Permissions: `instagram_business_basic`, `instagram_business_manage_messages`
- Dedicated Instagram App ID / Instagram App Secret from the Instagram API setup

The generic Meta/Facebook App ID used for WhatsApp Embedded Signup is not used as the Instagram Login `client_id` in production.
