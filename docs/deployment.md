# SHVYA deployment runbook

> **Implementation snapshot:** refreshed on 2026-10-13 against `main` commit `bac4ebf2fcb1` and the workflows under `.github/workflows/`. Git, migrations, tests and the workflow files remain the executable source of truth.

This is the concise release runbook. Environment isolation details are in [`deployment_environments.md`](./deployment_environments.md).

## Current release model

`main` is the production release branch. The current CI, Security, Hosted regression and Android workflow triggers target `main`. A successful `main` CI run is the source event for the production deployment workflows.

```text
feature / fix / docs branch
          ↓
       PR to main
          ↓
   CI + Security gates
          ↓
         main
          ↓
 production deployment of validated SHA
```

The repository retains `docker-compose.staging.yml` for an isolated non-production stack, but the current `main` tree does **not** contain a `deploy-staging.yml` workflow. Do not document a staging branch as a required promotion step unless such a workflow is intentionally reintroduced.

## Automated production paths

- `.github/workflows/deploy.yml` deploys the validated `main` SHA to the primary production target.
- `.github/workflows/deploy-main-secondary.yml` can deploy the same validated `main` SHA to the configured secondary target.
- `.github/workflows/android-apk.yml` builds the Android app on its configured `main` paths or manual dispatch.
- Hosted delivery/inbox regression workflows run on their configured `main` triggers.

A deployment must not substitute "whatever is currently checked out on the server" for the SHA that passed CI.

## Required validation

Run the relevant checks for the changed surface. The full local equivalent remains:

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest --cov

docker compose --env-file .env.example -f docker-compose.yml config --quiet
docker compose --env-file .env.staging.example -f docker-compose.staging.yml config --quiet
```

Provider, browser, Android and gateway changes also require their focused tests/builds; CI decides between targeted and full jobs based on changed files.

## Deployment checks

1. Confirm the intended `main` commit SHA and the CI/Security result associated with it.
2. Confirm migrations are reviewed and compatible with the deployed application/worker set.
3. Verify `/health/live/` and `/health/ready/`.
4. Verify Celery workers/Beat and Daphne/WebSocket service when those paths changed.
5. Verify provider callbacks/integrations only with the correct environment credentials and safe test identities.
6. Verify durable post-write state or provider receipt; a successful HTTP request alone is not business-outcome proof.
7. Check structured logs without exposing secrets, raw OAuth tokens, private webhook URLs or customer payloads.

## Rollback principle

Rollback must preserve data integrity. Revert through Git to a previously verified commit only when its schema remains compatible. Prefer expand/contract migrations. Do not manually fake migrations, edit production containers as the permanent fix, or point a test stack at production data to make a rollback appear successful.

## Documentation-only changes

Documentation-only PRs target `main` like other changes. They should not alter runtime code, Compose, infrastructure or provider behavior unless that is explicitly part of the task. Docs describing a tool catalog, route, release, API or deployment rule must be rechecked against the current code/workflow before merge.
