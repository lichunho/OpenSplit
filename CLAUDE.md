# CLAUDE.md — OpenSplit

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data Privacy
Never commit secrets, credentials, or private data. In this repo that means: `.env` (only `.env.example` is committed, with placeholder values), the live `SECRET_KEY` and `DATABASE_URL` — including the Neon pooled connection string, which embeds a password — local SQLite database files (`*.db` and their `-wal`/`-shm` siblings), `.venv/`, any `export.csv` downloaded from a real group, since it contains member names and spending history, and `archive/`, whose pre-migration database dumps hold that same data. `.gitignore` covers all of these. When in doubt about whether a file is safe to commit, ask.

## Working style
1. **Ask clarifying questions until you are absolutely sure how something should be implemented.** Do not guess on ambiguous requirements.
2. **Fact-check the user.** When the user states something verifiable (a filename, a count, an API), verify it against the code before acting; surface any discrepancy.

## Code Quality
- **KISS and SOLID.** Prefer the simplest design that meets the requirement; resist over-engineering. 3 explicit lines beat 1 clever abstraction. No helpers used once. No params for hypothetical futures. No error handling for impossible conditions. If a focused task runs past ~50 lines, reconsider.
- **Preserve existing comments.** Do not remove comments already present unless explicitly told to. For new code, comment only when WHY is non-obvious.
- **All paths from a central config.** Never hardcode paths in modules. `app/config.py` owns every `os.environ` read and the template/static directories. `db.py` imports `DATABASE_URL` from it and owns the URL normaliser; `auth.py` imports `SECRET_KEY`. Neither reads `os.environ` directly.
- **Thin entry points.** Scripts and CLIs delegate; they don't implement. `app/main.py` wires middleware and registers routers — no business logic.
- **One job per module.** Each module does one thing.
- **Don't reach through modules.** Call the public interface; don't import a sibling's internals. In particular `app/money.py` imports nothing from the app — no DB, no models.
- **Add behavior via new flags or new modules** — don't branch inside an existing core loop.

## Versioning / archive-before-destructive-change
Before any irreversible regeneration — schema migrations, retraining, mass rewrites, regenerating derived artifacts — move the old generation to a dated archive sibling (`archive/YYYY-MM-DD_<short-description>/`) that is never referenced by config, then replace the originals. Stale artifacts that load silently and produce wrong results with no error are the hazard this guards against.

A "generation" here is **the database schema and its contents**. v1 uses `create_all`, which silently no-ops on schema changes, so a pre-launch schema change means dropping and recreating the Neon branch — dump the old data to `archive/YYYY-MM-DD_<change>/` first if it is worth anything. Once real group data exists, Alembic stops being deferred and becomes the next milestone.

## Documentation
Read the project's docs at session start. Update the relevant doc when a feature is added/changed or a file path moves. Skip doc updates for bug fixes and refactors that don't move files.

[README.md](README.md) is the docs entry point and must keep stating the cold-start behaviour (60–90s on a free-tier link) and the `create_all`/no-migrations decision, so both read as choices rather than bugs. It links to three docs, each with one job — match the change to the doc:

- [docs/concept.md](docs/concept.md) — what the app is and why; the access model, the trades, scope in/out. Update when a *decision* changes.
- [docs/implementation.md](docs/implementation.md) — stack, module map, data model, core arithmetic, the three traps. Update when *structure* changes.
- [docs/instructions.md](docs/instructions.md) — setup, testing, config vars, deploy, verification checklist, troubleshooting. Update when a *command, variable or step* changes.

## Git
- **Never push** — the user does that manually. After meaningful changes, suggest a commit message.
- Commit messages must be a single one-liner (no body, no trailing summary).
- Do not add Claude as a co-author on commits (no `Co-Authored-By` trailer).

---

## Project

An open-source clone of Splitwise: shared-expense tracking for a friend group, with **when2meet-style access** — no accounts, no email, no OAuth. A group is reachable by secret link; you identify yourself by picking your name from a list, optionally protected by a password you set yourself.

**The link is the credential.** Anyone with the URL can view the group and add expenses; per-member passwords only stop someone acting *as you*. Hence `secrets`-generated slugs and a disallow-all `robots.txt`. This is a deliberate trade, not an oversight.

Full design rationale lives in the implementation plan at `~/.claude/plans/the-goal-of-this-transient-flamingo.md`.

## Current state

**All nine milestones of the implementation plan are built and committed** on the `build/v1` branch, with 84 tests passing. Not yet done: the plan's *deployed* verification — pushing to GitHub, wiring Render + Neon, and the 20-minute-idle test that is the only real proof trap #1 is fixed. `docker compose up` is also written but has never been executed (no Docker daemon was running).

## Stack

Node/npm are **not installed** on this machine; Python 3 and Docker are. The stack follows from that: server-rendered, no frontend build step.

| Concern | Choice |
|---|---|
| Web | FastAPI + Jinja2 templates |
| DB | SQLModel — SQLite locally, Neon Postgres in prod, one `DATABASE_URL` switch |
| Driver | psycopg 3 (`postgresql+psycopg://`) |
| Sessions | Starlette `SessionMiddleware`, signed cookie |
| Passwords | `hashlib.scrypt`, N=2¹⁴ r=8 p=1 |
| Migrations | `create_all` for v1, Alembic deferred |
| Hosting | Render free web service + Neon free Postgres |
| JS | ~40 lines of vanilla JS on the expense form |

## Commands

```bash
py -3.13 -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
pytest                                   # money math + route happy paths
pytest tests/test_money.py::test_name    # single test
uvicorn app.main:app --reload            # http://127.0.0.1:8000
docker compose up                        # same app on :8000
```

## Architecture

```
app/
  main.py       app, middleware, router registration
  config.py     env + paths (see the Code Quality note above)
  db.py         engine, session dependency, URL normalisation, pool config
  models.py     SQLModel tables
  money.py      PURE functions: split, balances, simplify  <- the testable core
  auth.py       session helpers, scrypt hash/verify, require_member
  queries.py    read-side queries/formatting shared by more than one route
  routes/       groups.py, expenses.py, settlements.py
  templates/    base, index, identify, group, expense_form, settle_pick, settle_confirm
  static/       app.css, app.js, robots.txt
tests/          test_money.py, test_routes.py
```

Invariants that span files:

- **All money is integer cents.** Never floats, anywhere. Parse decimal input with `Decimal` + `ROUND_HALF_UP`, never `float()`, with a sanity ceiling.
- **`money.py` is DB-free by design** so it can be tested directly. Every real bug in this app lives in its three functions.
- **Shares are stored, not recomputed.** A member joining mid-trip must not silently rewrite the history of expenses they weren't part of.
- **`Settlement` is its own table**, not an expense variant — two parties, no shares. The activity feed merges expenses and settlements by `created_at` at render time.
- **Soft delete**: expenses and settlements carry `deleted_at`; every balance query filters `deleted_at IS NULL`.
- **`require_member(slug)`** returns `(group, member)` or redirects to identify. Every mutating route depends on it.
- **All POSTs redirect 303** so refresh doesn't double-submit.
- **Deterministic tie-breaks.** `split_equal` rotates the remainder cent starting at index `total_cents % n` over members sorted by id; `simplify` breaks ties by member id. Without these the suggested payee flickers between reloads, which reads as a bug.

## Three production-only traps

These fail *only* on the deployed free tier — a green local `pytest` proves nothing about them. They are why `db.py` and `auth.py` deserve care disproportionate to their size.

1. **Neon autosuspends at 5 min, Render spins down at 15.** In that gap a live process holds pooled connections to a sleeping database and the next request dies with `SSL SYSCALL error: EOF detected` — a 500, unreproducible on local SQLite. `db.py` must use Neon's **pooled** host (`-pooler`), set **`poolclass=NullPool`** for Postgres, pin **SQLAlchemy ≥ 2.0.33**, normalise `postgres://` → `postgresql+psycopg://` in exactly one place, and on the SQLite branch set `check_same_thread=False` plus WAL.
2. **`hashlib.scrypt` raises `ValueError` at OWASP's recommended parameters.** CPython defaults `maxmem=0`, which OpenSSL caps at 32 MiB; `128·N·r` means N=2¹⁵ r=8 hits the cap and throws — ships green, dies on first login. Use **N=16384 (2¹⁴), r=8, p=1**, a 16-byte `secrets.token_bytes` salt, `hmac.compare_digest` to verify, and store a self-describing `scrypt$N$r$p$salt$hash` so the cost can be raised later without a migration.
3. **`create_all` silently no-ops on schema changes** — add a column post-launch and every page 500s. See the versioning section above.

## Scope

In: equal and exact splits, balances, simplified debts, recorded settlements, expense editing, free-text expense categories with a per-category feed filter, CSV export, soft delete.

Deliberately out: real payment rails, multi-currency, member rename/delete, percentage or share splits, multi-payer expenses, notifications. Free-tier cold start is accepted, not engineered around.

Simplified debt suggestions are **unstable by nature** — simplification never changes anyone's net balance, only payment paths, so "pay Alex $30" can legitimately become "pay Sam $30" tomorrow. Deterministic tie-breaks and a visible manual-payee option are the fix; a better algorithm is not (optimal is NP-complete).

## Conventions

[.gitattributes](.gitattributes) sets `* text=auto`, so line endings normalize to LF in the repository. Development is on Windows; avoid committing CRLF-dependent fixtures.
