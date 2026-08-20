# Instructions

Setup, running, testing, deploying, and using the app.

## Prerequisites

- **Python 3.13.** Newer versions may work, but 3.13 is what the app is developed and
  tested against, and it has settled wheels for `psycopg` and `pydantic-core` on Windows.
- **Docker** (optional) — only for `docker compose up`.
- **Node/npm are not required.** There is no frontend build step.

## Local setup

```bash
py -3.13 -m venv .venv && .venv/Scripts/activate    # Windows
# python3.13 -m venv .venv && source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

Run it:

```bash
uvicorn app.main:app --reload        # http://127.0.0.1:8000
```

No configuration is needed to start. `DATABASE_URL` falls back to a local SQLite file at
the project root, and `SECRET_KEY` falls back to a key generated at startup.

> The generated dev key is new on every restart, which invalidates existing sessions — so
> with `--reload` you'll be asked who you are again after a code change. That's expected.
> Set a fixed `SECRET_KEY` in `.env` if it becomes annoying.

## Testing

```bash
pytest                                   # all 84 tests
pytest tests/test_money.py               # the money core only — fast, no DB
pytest tests/test_money.py::test_name    # a single test
pytest -q -W default                     # surface warnings
```

Tests use a temporary database and never touch your local `app.db`.

## Docker

```bash
docker compose up                        # same app on :8000
```

Environment variables pass through from your shell if set, with local defaults otherwise.
No secret is baked into the image.

> **Not yet exercised.** The Docker files are desk-checked and `docker compose config`
> validates, but they have not been run against a live Docker daemon. If `docker compose up`
> misbehaves, that's the likeliest place an error is hiding.

## Configuration

Copy `.env.example` to `.env` to override defaults locally. Nothing in it is required for
local development.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local SQLite file | In production, Neon's **pooled** connection string — the hostname must contain `-pooler` |
| `SECRET_KEY` | generated at startup | Set a fixed value to keep sessions across restarts |
| `SESSION_HTTPS_ONLY` | `false` | Must be `true` in production. See below |

**On `SESSION_HTTPS_ONLY`:** a `Secure` cookie is not sent by browsers over plain `http://`,
so setting this to `true` locally would silently break sign-in on `127.0.0.1`. It defaults
to `false` so local development works, and `render.yaml` forces it to `true` for the
deployed service. If you deploy some other way, **you must set it yourself** or the session
cookie ships without the `Secure` flag.

Real secrets never belong in the repo. `.env`, `*.db`, `.venv/`, `export.csv` and
`archive/` are all gitignored.

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

## Deploying

Two free-tier services: **Render** for the web app, **Neon** for Postgres. Render's own free
Postgres is deleted after 30 days and its disks are ephemeral, which is why the database
lives on Neon.

### 1. Create the Neon database

Create a Neon project, then copy its **pooled** connection string — the hostname contains
`-pooler`. This matters: the app sets `poolclass=NullPool` and lets Neon's PgBouncer own
connection pooling. Using the unpooled host reintroduces the `SSL SYSCALL error: EOF
detected` failure described in [implementation.md](implementation.md#the-three-production-only-traps).

### 2. Deploy to Render

Push the repo to GitHub, then create a Blueprint deploy from `render.yaml`. It defines the
service, the build and start commands, `/healthz` as the health check, and:

- `DATABASE_URL` — marked `sync: false`, so Render prompts you for it rather than the value
  being committed. Paste the Neon **pooled** string.
- `SECRET_KEY` — `generateValue: true`, generated by Render and never committed.
- `SESSION_HTTPS_ONLY` — set to `true`.

The start command binds `0.0.0.0` and Render's injected `$PORT`. A hardcoded port fails the
health check silently.

### 3. Expect a slow first request

**The first request to a sleeping app takes roughly 60–90 seconds.** Render spins the
service down after 15 minutes idle; Neon autosuspends after 5. Waking both is what that
minute is. The app shows a "waking up" indicator rather than appearing broken. This is
accepted free-tier behaviour, not a bug to fix.

## Verifying a deployment

Use **two different browsers** — this is the only way to actually test the identity model,
since one browser holds one identity per group.

1. Create a group; add Chris, Alex and Sam.
2. Browser A → identify as Chris, set a password. Browser B → open the same link, confirm it
   asks who you are.
3. Browser B → identify as Chris with the **wrong** password → readable error, not a 500.
   Identify as Alex instead → allowed.
4. Chris adds "Dinner $100" paid by Chris, split equally three ways → shares 3334/3333/3333,
   summing to 10000.
5. Alex adds "Taxi $45" paid by Alex, exact 20/15/10. A 20/15/9 attempt must be **rejected**.
6. Dashboard: balances sum to zero, at most 2 transfers, and **reloading twice gives an
   identical suggestion** (the tie-break check).
7. Settle as Sam accepting the default → Sam's balance is exactly $0.00 and that transfer
   disappears.
8. Settle as Alex with the amount **edited downward** → remainder correct, balances still
   sum to zero. Overpay on a third settlement → the balance **flips sign** rather than
   erroring.
9. Delete that settlement, then soft-delete and restore the taxi expense → balances return
   exactly.
10. Edit the dinner to $60 → it stays **in the same place in the feed** (editing does not
    touch `created_at`), balances follow, and the expense still has exactly three share
    rows. Soft-delete it → the Edit control disappears until it is restored.
11. `export.csv` opens in a spreadsheet and reconciles with the dashboard.
12. **The test only production can run:** idle for 20 minutes, come back, and confirm the app
    is *slow but does not 500*. That is the Neon-suspend trap proving fixed, and no local
    test can substitute for it.

## Using the app

1. **Create a group** from the landing page.
2. **Share the link.** Use the copy button on the group page. Remember: anyone with the link
   can view the group and add expenses — see [concept.md](concept.md#the-access-model-the-link-is-the-credential).
3. **Everyone picks their name** the first time they open it. Setting a password is optional
   and only stops someone else acting as you.
4. **Add expenses** as they happen. Equal is the default; switch to exact when the split
   isn't even.
5. **Categorise them** with the optional Category dropdown. It lists the categories this
   group already uses — a new group has none — plus **Custom…**, which asks for a name and
   adds it to the list for this group only. Casing is matched for you, so entering "food"
   joins the existing "Food" rather than starting a second one.
6. **Filter the feed** with the tabs above the activity list: one per category in use, plus
   All and Uncategorized, each with a total. The tabs are ordinary links, so they're
   bookmarkable and survive a refresh. **Balances and suggested settlements always show the
   whole group** — filtering never changes what anyone owes.
7. **Settle up** when you're squaring away. The app suggests who to pay, but paying anyone
   directly is fine — balances net out the same either way.
8. **Export to CSV** any time. The Expenses section carries a Category column.

Mistyped something? Everything is soft-deleted, so **delete and restore both work** on
expenses and settlements.

## Troubleshooting

**The link takes a minute to load.** Free-tier cold start. See above.

**Everyone got signed out.** `SECRET_KEY` changed — either it wasn't set and the app
restarted, or it was rotated. Set a fixed value.

**Sign-in silently fails in production.** Check `SESSION_HTTPS_ONLY` is `true` **and** the
site is served over HTTPS. A `Secure` cookie over plain HTTP is dropped by the browser.

**The copy-link button isn't showing.** The Clipboard API requires a secure context, so it's
hidden over plain `http://` on a LAN IP. The link input is still selectable — tap it and
copy manually.

**A page 500s after a schema change.** This is trap #3. `create_all` does not alter existing
tables, so a new column exists in the models and not in the database. For a nullable column,
back up and `ALTER TABLE ... ADD COLUMN` — see [Schema changes](#schema-changes) — which
keeps the existing data. Only a change that can't be expressed that way needs the old
drop-and-recreate route: dump to `archive/YYYY-MM-DD_<description>/`, recreate the Neon
branch, let `create_all` rebuild it. Either way the statement must be run against **Neon as
well as local** before the new code deploys, or every page 500s on the missing column. Once
this happens often enough to be annoying, the answer is Alembic.

---

See [concept.md](concept.md) for the design rationale and
[implementation.md](implementation.md) for how it's built.
