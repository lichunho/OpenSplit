# Data and artifacts

## Inputs

- **User input through the web UI**: groups, members, expenses, shares, settlements, and
  optional per-member password hashes.
- **CSV import**: a file shaped like the group's own `export.csv`, uploaded at
  `/g/{slug}/import`. It is parsed by `app/csv_import.py`, previewed, and then appended. Nothing
  is read from disk.

There are no datasets, models or caches. The app ships no seed data.

## The generation: schema + contents

The only generated artifact that has to be versioned is **the database, meaning its schema
and its contents together**. The schema comes from `app/models.py` through `create_all` at
startup (the `lifespan` in `app/main.py`). `create_all` creates missing tables but never
alters existing ones, so a model change and the database it runs against must move as a
unit. See trap #3 in [architecture.md](architecture.md#the-three-production-only-traps).

| Environment | Database | Location |
|---|---|---|
| Local dev | SQLite with WAL | `app.db`, `app.db-wal` and `app.db-shm` at the repo root. **Holds real trip data.** |
| Tests | SQLite | A temp dir created by `tests/conftest.py` on each run |
| `docker compose` | SQLite inside the container | Lost when the container is removed |
| Production | Neon Postgres, pooled host | The Neon project; see [deployment.md](deployment.md) |

This repo has no `candidate/` or `current/` layout. The live database *is* current, and
`archive/` holds prior generations. There is no fingerprint or manifest yet. Alembic's
revision table becomes that record when migrations arrive.

The deployed Lambda image is a second artifact. Each deploy needs a **new image tag**,
because Lambda pins a tag to its digest when it is deployed.

## Archive

Before any irreversible change to the database, copy the old generation to
`archive/YYYY-MM-DD_<short-description>/`. Nothing in config references `archive/`. Current
entries:

| Folder | Contents |
|---|---|
| `archive/2026-08-19_expense-category/` | SQLite `.backup` of `app.db` taken before the `category` column was added |
| `archive/2026-08-20_delete-getting-lost-in-la/` | JSON dump and per-table CSVs taken before a group was deleted |

## Schema changes

`create_all` builds missing tables but never alters existing ones (trap #3), so a new column
has to be added by hand until Alembic arrives. For a **nullable** column this is one
statement, identical on SQLite and Postgres, and it keeps every existing row in place:

```bash
# Back up first — WAL-aware, so it's consistent even mid-write.
mkdir -p archive/$(date +%F)_<short-description>
sqlite3 app.db ".backup 'archive/$(date +%F)_<short-description>/app.db'"

# Apply. Goes through app.db's engine, so it uses the same normalised
# DATABASE_URL the app does — SQLite locally, Postgres in prod.
python -c "
from sqlalchemy import text
from app.db import engine
with engine.begin() as conn:
    conn.execute(text('ALTER TABLE expense ADD COLUMN category VARCHAR'))
"
```

Re-running raises "duplicate column name" — SQLite has no `ADD COLUMN IF NOT EXISTS`, so
that error means it's already applied, not that something is broken. Postgres does support
`IF NOT EXISTS`, which makes the same statement safely repeatable there.

Tests need nothing: `tests/conftest.py` points `DATABASE_URL` at a throwaway SQLite file that
`create_all` builds from scratch on every run.


## Private data

The following files contain real people's names and spending, or credentials. They must never be committed. `.gitignore` covers all of them, and
`.dockerignore` keeps the database files, `.env` and `archive/` out of the image.

| What | Where | Ignored by |
|---|---|---|
| Local database | `*.db`, `*.db-wal`, `*.db-shm` | `.gitignore`, `.dockerignore` |
| Secrets (`SECRET_KEY`, Neon `DATABASE_URL` with its password) | `.env`, and `env.json` during a deploy | `.env` in both; `env.json` in `.gitignore`. Still delete it after deploying |
| Exported group data | `export.csv` | `.gitignore` |
| Archived generations | `archive/` | `.gitignore`, `.dockerignore` |
| Neon CLI state | `.neon` | `.gitignore` |
| IAM deploy policy (account ID, no secrets) | `policy.json` | `.gitignore` |
