# Local Setup — SHVYA AI

> **Implementation baseline:** refreshed on 2026-10-12 against `main` commit `24f2a1d5f75d`. Source code, migrations, tests and environment examples remain authoritative.

## 1. Prerequisites
- Python 3.13
- PostgreSQL 17+ with the `vector`/pgvector extension available
- Redis
- Node.js 22.12+ only when running the Hosted WhatsApp gateway locally

macOS:
```bash
brew install postgresql@18 redis
brew services start postgresql@18
brew services start redis
```

## 2. Clone and set up the virtualenv
```bash
git clone https://github.com/Ashwani1611/shvya-ai.git
cd shvya-ai
python -m venv venv
source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
```

## 3. Environment variables
```bash
cp .env.example .env
```
Then edit `.env` and fill in:
- `SECRET_KEY` — any random string for local dev
- `DB_NAME`, `DB_USER`, `DB_PASSWORD` — match your local Postgres user
- `DB_HOST=127.0.0.1`, `DB_PORT=5432`

**Important:** if your shell already has `DB_*` environment variables set
(e.g. from a previous `export`), they will silently override `.env`.
Check with `env | grep DB_` and `unset` them if so.

**Important:** if your password contains a `#`, wrap it in quotes in `.env`,
e.g. `DB_PASSWORD='mypass#123'`.

## 4. Create the database
```bash
psql -U postgres -c "CREATE DATABASE shvya_ai;"
```
(Adjust the user/db name to match what you put in `.env`.)

## 5. Run migrations and start the server
```bash
python manage.py migrate
python manage.py runserver
```
Visit `http://127.0.0.1:8000/dashboard/login/`.

## 6. Run tests and workers

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest --cov
```

For message/AI/automation development, also run the required processes in separate shells:

```bash
celery -A config worker -l info
celery -A config worker -l info -Q ai_realtime --concurrency=2 --prefetch-multiplier=1
celery -A config worker -l info -Q hosted_ai --concurrency=1 --prefetch-multiplier=1
celery -A config beat -l info
```

Run the Node gateway only when testing Hosted WhatsApp. Never use production provider credentials in local development.

## 7. Branch workflow
Normal development starts from the current `main` release line.

```bash
git checkout main
git pull origin main
git checkout -b feature/<short-description>
# ...work...
git add .
git commit -m "feat: <what you did>"
git push -u origin feature/<short-description>
```

Open the focused pull request into `main`. Require CI/Security to pass before merge. If an isolated non-production staging stack is intentionally used, treat it as additional validation rather than a required Git branch promotion path.

Branch prefixes:
- `feature/...` — new functionality
- `fix/...` — bug fixes
- `chore/...` — tooling, config, cleanup
- `docs/...` — documentation-only changes

## 8. Before committing
A pre-commit hook runs `ruff` automatically to catch undefined names and
unused imports. If it's not installed yet:
```bash
pre-commit install
```

## Troubleshooting
- **`psql: FATAL: password authentication failed`** — check `.env` matches
  your actual local Postgres password, and check for stale shell `DB_*`
  env vars overriding it (see step 3).
- **Redis `Connection refused`** — make sure `redis-server` is running:
  `redis-cli ping` should return `PONG`.
- **`NameError` / `AttributeError` in the browser** — run
  `ruff check apps --select F821` to catch undefined names before it
  reaches the browser.
