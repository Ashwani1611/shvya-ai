# Instagram OAuth incident: Instagram 404 before login

## Symptom

Clicking **Continue with Instagram** opened Instagram and immediately rendered:

> Sorry, this page isn't available.

The failure occurred before Instagram redirected to SHVYA, so no OAuth callback or token exchange could run.

## Root cause

The production Instagram integration was allowed to fall back to SHVYA's generic Meta App ID / App Secret, which are also used by WhatsApp Embedded Signup. Meta's **Instagram API with Instagram Login** expects the dedicated **Instagram App ID** and **Instagram App Secret** from the Instagram API setup. Sending the wrong OAuth `client_id` can make `instagram.com/oauth/authorize` fail before authorization.

## Fix

- Production requires `META_INSTAGRAM_APP_ID` and `META_INSTAGRAM_APP_SECRET`.
- Instagram Login no longer falls back to the generic WhatsApp/Facebook Meta credentials in production.
- The authorize URL uses `force_reauth=true` and `enable_fb_login=0` for the direct Instagram professional-account login flow.
- Regression tests ensure production cannot silently reuse `META_APP_ID` / `META_APP_SECRET`.

## Required production values

```text
META_INSTAGRAM_APP_ID=<Instagram App ID from Meta Instagram API setup>
META_INSTAGRAM_APP_SECRET=<Instagram App Secret from the same setup>
```

The Meta App Dashboard must also contain the exact redirect URI:

```text
https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/
```
