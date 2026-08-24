# Local Setup — SHVYA AI

## 1. Prerequisites
- Python 3.13
- PostgreSQL (15+)
- Redis

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

## 6. Run tests
```bash
pytest --cov
```

## 7. Branch workflow
We use feature branches + pull requests. `main` is protected — no direct pushes.

```bash
git checkout main
git pull
git checkout -b feature/<short-description>
# ...work...
git add .
git commit -m "feat: <what you did>"
git push -u origin feature/<short-description>
```
Then open a pull request on GitHub into `main`. Wait for review before merging.

Branch prefixes:
- `feature/...` — new functionality
- `fix/...` — bug fixes
- `chore/...` — tooling, config, cleanup

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
