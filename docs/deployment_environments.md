# SHVYA AI deployment environments

SHVYA AI uses two long-lived environments and short-lived feature/fix branches.

## Branches and environments

| Branch | Environment | URL | Server directory |
| --- | --- | --- | --- |
| `main` | Production | `https://dashboard.shvya-ai.com` | `/opt/shvya-ai` |
| `staging` | Staging / pre-production | `https://staging.shvya-ai.com` | `/opt/shvya-ai-staging` |
| `feature/*`, `fix/*` | Development | local / PR CI | developer machine |

Production and staging run separate PostgreSQL, Redis, Celery, media, static,
and WhatsApp session storage. They share only the VPS and the edge Nginx
container. Nginx is attached to the staging Docker network only for reverse
proxy traffic.

## Normal release flow

1. Create `feature/<name>` or `fix/<name>` from `staging`.
2. Open a pull request into `staging`.
3. CI must pass.
4. Merge to `staging`.
5. `Deploy Staging` automatically deploys `/opt/shvya-ai-staging`.
6. Verify the feature at `staging.shvya-ai.com` using staging/test credentials.
7. Open a pull request from `staging` into `main`.
8. CI must pass again.
9. Merge to `main`.
10. `Deploy` backs up PostgreSQL, migrates, updates services, performs readiness
    checks, and deploys production.

Do not merge feature branches directly to `main` except for a documented
emergency hotfix.

## Environment files

Production keeps `/opt/shvya-ai/.env` and staging keeps
`/opt/shvya-ai-staging/.env.staging`. Both files are ignored by Git.

The staging deploy creates `.env.staging` automatically on first deploy from
`.env.staging.example` and generates independent Django, database, webhook, and
gateway secrets. Meta, OpenAI, SMTP, payment, and other third-party credentials
must use sandbox/test accounts in staging when those integrations are tested.

Never point staging at the production PostgreSQL database, production Redis, or
production WhatsApp/Instagram credentials.

## Database safety

Production deployment creates a timestamped `pg_dump` before migrations and
keeps 14 days of deployment backups in `/opt/shvya-ai/backups`.

Staging migrations only run against the isolated `shvya-staging` Compose
project database volume.

If production data is ever copied to staging, anonymize personal/customer data
before it becomes available to staging users.

## Health checks

- `/health/live/` checks that the Django process can respond.
- `/health/ready/` checks both PostgreSQL and Redis.

Deployments use readiness checks before reporting success.

## DNS and TLS

Create an `A` record for `staging.shvya-ai.com` pointing to the same VPS as the
production dashboard. The staging deploy writes an isolated Nginx server block,
adds `X-Robots-Tag: noindex, nofollow, noarchive`, and attempts to provision a
Let's Encrypt certificate automatically. If DNS is not ready, the Docker stack
still deploys but TLS provisioning waits until a later staging deploy.

## GitHub repository settings

Recommended repository controls:

- Require pull requests for `main` and `staging`.
- Require the `CI` checks before merge.
- Block force pushes and branch deletion for both long-lived branches.
- Configure the GitHub `production` Environment with required approval.
- `staging` may deploy automatically after CI.
- Keep VPS credentials in GitHub Actions secrets, never in repository files.

The workflows reference GitHub Environments named `staging` and `production`.
Environment protection rules are configured in GitHub repository settings.

## Rollback

Application rollback:

1. Revert the bad PR or reset `staging` to a known good commit for staging.
2. For production, create a revert PR into `main` and deploy it through CI.
3. If a migration is not backward compatible, restore the matching pre-deploy
   database backup before bringing the previous application version back.

Prefer backward-compatible, expand/contract database migrations so ordinary
application rollbacks do not require a database restore.
