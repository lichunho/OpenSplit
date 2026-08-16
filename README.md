# Splitwise-Clone

An open-source clone of Splitwise for splitting shared expenses within a friend group —
equal and exact splits, running balances, simplified debt suggestions, recorded settlements,
and CSV export.

## Access model: the link is the credential

There are no accounts, no email, no OAuth — when2meet-style access instead. A group lives at
a secret link (`/g/{slug}`, an unguessable `secrets`-generated slug). You identify yourself by
picking your name from a list; a password is optional and set by each member themselves.

**Anyone with the link can view the group and add expenses.** A per-member password only stops
someone else from acting *as you* — it is not a barrier to the group itself. This is a
deliberate trade, not an oversight: it's the same model when2meet uses, and it's why the slug
is unguessable and [`robots.txt`](app/static/robots.txt) disallows all crawling (nothing about
a group should end up in a search index).

## Deploying this yourself

**Cold start: expect 60–90 seconds on the first request after idle.** Render's free web
service spins down after 15 minutes with no traffic, and Neon's free Postgres autosuspends
after 5. When both are asleep, the request that wakes them pays for a Render cold boot plus a
Neon resume before it returns. If a friend opens the link and the page hangs for a minute
before loading, that's this — not the app being broken.

The service is defined in [`render.yaml`](render.yaml) as a Render Blueprint:
1. Create a Neon project and copy its **pooled** connection string (hostname contains
   `-pooler`) — this matters, see below.
2. In the Render dashboard, deploy from this repo's blueprint. It will prompt for `DATABASE_URL`
   (paste the Neon pooled string) since the blueprint marks it `sync: false` rather than
   committing it. `SECRET_KEY` is generated automatically by Render and never committed either.
3. `SESSION_HTTPS_ONLY` is hardcoded to `true` in the blueprint for the deployed service —
   [`app/config.py`](app/config.py) defaults it to `false` so local `http://127.0.0.1` dev
   works, and production must override it or the session cookie ships without the `Secure`
   flag.

### Why `create_all` and no migrations (yet)

v1 creates tables with SQLModel's `create_all` (see the `lifespan` in
[`app/main.py`](app/main.py)), which **silently no-ops on schema changes** — add a column and
every page that touches it 500s with no warning at deploy time. This is a deliberate choice for
now, not an oversight: before there's real group data worth keeping, a schema change just means
dumping the current database to `archive/YYYY-MM-DD_<short-description>/` (gitignored — it
would hold real member names and spending history) and dropping/recreating the Neon branch.
Alembic becomes the next milestone the moment there's data worth preserving across a schema
change instead.

## Local development

Requires Python 3.13 (Node/npm are not used — this is a server-rendered app with no frontend
build step).

```bash
py -3.13 -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
pytest                                   # money math + route happy paths
pytest tests/test_money.py::test_name    # single test
uvicorn app.main:app --reload            # http://127.0.0.1:8000
docker compose up                        # same app on :8000
```

Copy [`.env.example`](.env.example) to `.env` to override defaults locally; neither variable is
required to get started — `DATABASE_URL` falls back to a local SQLite file and `SECRET_KEY`
falls back to a generated dev key (a new one each restart, which invalidates existing sessions —
harmless in dev).

## Architecture

```
app/
  main.py        FastAPI app, middleware, route registration — no business logic
  config.py      every os.environ read in the codebase lives here
  db.py          engine + session dependency, DATABASE_URL normalisation, pool config
  models.py      SQLModel tables: Group, Member, Expense, Share, Settlement
  money.py       pure functions: split_equal, validate_exact, net_balances, simplify
  auth.py        session helpers, scrypt hash/verify, require_member dependency
  queries.py     read-side queries/formatting shared across route modules
  routes/        groups.py, expenses.py, settlements.py
  templates/      base, index, identify, group, expense_new, settle_pick, settle_confirm
  static/        app.css, app.js, robots.txt (disallow all)
tests/           test_money.py, test_routes.py, test_db.py
```

`app/money.py` is deliberately DB-free — it takes and returns plain ints and dicts, imports
nothing else from the app, and is where every real bug in this project tends to live, so it's
tested directly rather than through the routes.

## Scope

**In:** equal and exact splits, running balances, simplified debt suggestions, a recorded
settlement flow, CSV export, soft delete (with undo) for expenses and settlements.

**Deliberately out:** real payment rails, multi-currency, expense editing, member rename/delete,
percentage or share splits, multi-payer expenses, notifications.

Simplified debt suggestions are **unstable by nature** — simplifying a set of balances never
changes anyone's net balance, only which payment paths clear it, so "pay Alex $30" can
legitimately turn into "pay Sam $30" tomorrow with no bug involved. The app makes this bearable
with deterministic tie-breaks (so the suggestion doesn't flicker on every reload) and a visible
manual-payee option; finding a version of the algorithm that's both optimal and stable isn't in
scope (the optimal version of the problem is NP-complete).
