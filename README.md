# OpenSplit

An open-source clone of Splitwise for splitting shared expenses within a friend group —
equal and exact splits, running balances, simplified debt suggestions, recorded settlements,
and CSV export/import.

There are no accounts, no email, no OAuth. A group lives at a secret link; you identify
yourself by picking your name from a list.

## Documentation

Start at [documentation/README.md](documentation/README.md), the index. The main docs:

| Doc | What's in it |
|---|---|
| [concept.md](documentation/concept.md) | What the app is, the access model, the trades it makes, what's deliberately out of scope |
| [architecture.md](documentation/architecture.md) | Stack, module rules, data model, the core arithmetic, the three production-only traps |
| [commands.md](documentation/commands.md) | Setup, running, testing, Docker |
| [deployment.md](documentation/deployment.md) | Deploying, verification checklist, troubleshooting |
| [conventions.md](documentation/conventions.md) | How to contribute |

## Quick start

```bash
python3.13 -m venv .venv && source .venv/bin/activate   # macOS / Linux
# py -3.13 -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
pytest                                   # 135 tests
uvicorn app.main:app --reload            # http://127.0.0.1:8000
docker compose up                        # same app on :8000
```

No configuration is needed to start — `DATABASE_URL` falls back to a local SQLite file and
`SECRET_KEY` to a generated dev key. Full details in
[documentation/commands.md](documentation/commands.md) and
[documentation/config.md](documentation/config.md).

## The link is the credential

A group lives at `/g/{slug}`, where the slug is an unguessable `secrets`-generated string.
**Anyone with the link can view the group and add expenses.** A per-member password only
stops someone else from acting *as you* — it is not a barrier to the group itself.

This is a deliberate trade, not an oversight: it's the same model when2meet uses, and it's
why the slug is unguessable and [`robots.txt`](app/static/robots.txt) disallows all
crawling. The reasoning is in [documentation/concept.md](documentation/concept.md).

## Two things that look like bugs and aren't

**Cold start: expect a few seconds on the first request after idle.** Neon's free Postgres
autosuspends after 5 minutes with no traffic, and an idle Lambda sandbox is torn down too, so
the request that arrives after a quiet stretch pays to wake both — measured at ~2.4 seconds
with the sandbox and Neon cold, against ~260ms warm. The first request after a new image is
deployed is slower still (~13s), because it also pulls the image and runs `create_all`. If a
friend opens the link and the page pauses, that's this — not the app being broken. It is a
cold start, not an always-on server: accepted as the price of free hosting rather than
engineered around. The deployment is described in
[documentation/deployment.md](documentation/deployment.md#deploying).

**Simplified debt suggestions shift between days.** Simplifying balances never changes
anyone's net position, only which payment paths clear it — so "pay Alex $30" can
legitimately become "pay Sam $30" tomorrow. Deterministic tie-breaks stop it flickering on
reload, and paying whoever you're actually standing next to is a first-class option.

## Schema changes: `create_all`, no migrations yet

v1 creates tables with SQLModel's `create_all` (see the `lifespan` in
[`app/main.py`](app/main.py)), which **silently no-ops on schema changes** — add a column and
every page that touches it 500s, with no error at deploy time.

This is a deliberate choice for now. Before there's real group data worth keeping, a schema
change just means dumping the database to `archive/YYYY-MM-DD_<short-description>/`
(gitignored — it would hold real member names and spending history) and dropping/recreating
the Neon branch; a nullable column can be added in place instead — see
[documentation/data-and-artifacts.md](documentation/data-and-artifacts.md#schema-changes).
**Alembic becomes the next milestone the moment there's data worth
preserving across a schema change.**

## Scope

**In:** equal and exact splits, running balances, simplified debt suggestions, a recorded
settlement flow, expense editing, CSV export and import, soft delete with undo.

**Deliberately out:** real payment rails, multi-currency, member rename/delete, percentage
or share splits, multi-payer expenses, notifications.

Each excluded feature is one fewer way for the balance arithmetic to stop being trustworthy
— see [documentation/concept.md](documentation/concept.md).
