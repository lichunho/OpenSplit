# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OpenSplit: an open-source Splitwise clone with when2meet-style access. There are no accounts. A group is a secret link, and you pick your name from a list, optionally protected by a password you set yourself.

Read [documentation/README.md](documentation/README.md) at session start. Repo conventions for contributors without the global file are in [documentation/conventions.md](documentation/conventions.md).

## Commands

```bash
python3.13 -m venv .venv && source .venv/bin/activate   # Windows: py -3.13 … && .venv/Scripts/activate
pip install -r requirements.txt
pytest                                   # 136 tests; conftest isolates the DB
pytest tests/test_money.py::test_name    # single test
uvicorn app.main:app --reload            # http://127.0.0.1:8000
docker compose up                        # same image on :8000, ephemeral SQLite
```

## Key locations

- Config: [app/config.py](app/config.py) owns every `os.environ` read and the template/static paths. `db.py` imports `DATABASE_URL` and owns the URL normaliser. `main.py` imports `SECRET_KEY` for the session middleware.
- Entry point: [app/main.py](app/main.py), which only wires middleware, routers and the `create_all` lifespan.
- Testable core: [app/money.py](app/money.py) and [app/csv_import.py](app/csv_import.py) are DB-free. `money.py` imports nothing from the app.
- Archive: `archive/YYYY-MM-DD_<change>/`. It holds prior database generations. There is no candidate/current layout, because the live DB is current.
- Private data (never commit): `.env`, `env.json`, `*.db*` (the local `app.db` holds real trip data), `export.csv`, `archive/`, `.neon`.

## Invariants that span files

- **All money is integer cents.** Never use floats. Parse with `Decimal` + `ROUND_HALF_UP` against a plain-digits pattern first (to reject `1e5` and `NaN`), with a $1M ceiling.
- **Shares are stored, not recomputed.** CSV import keeps an `equal` row's cents from the file and does not re-split them.
- **`Settlement` is its own table.** The feed merges expenses and settlements by `created_at` at render time.
- **Soft delete.** Balance queries filter `deleted_at IS NULL`. `Share` has no `deleted_at`; it is gated by a join to `Expense` in `queries.balance_inputs` only.
- **`require_member(slug)`** returns `(group, member)` or a redirect. Every group route checks `is_redirect` first.
- **All POSTs redirect 303.**
- **Deterministic tie-breaks.** `split_equal` rotates the remainder starting at `total_cents % n` over members sorted by id, and `simplify` breaks ties by member id.
- **Markup is load-bearing for tests.** `test_routes.py` string-matches certain dashboard headings and tags. See architecture.md › UI layer.

## Three production-only traps

A green local `pytest` proves nothing about these. Details are in [documentation/architecture.md](documentation/architecture.md#the-three-production-only-traps).

1. **Neon autosuspend and Lambda teardown** cause `SSL SYSCALL error: EOF`. The fix: the pooled `-pooler` host, `NullPool` for Postgres, SQLAlchemy ≥ 2.0.33, a single URL normaliser, and on SQLite `check_same_thread=False` + WAL.
2. **scrypt above N=2¹⁴ raises `ValueError`** against OpenSSL's 32 MiB cap. Keep N=16384, r=8, p=1, a 16-byte salt, `hmac.compare_digest`, and the self-describing `scrypt$N$r$p$salt$hash` format.
3. **`create_all` silently no-ops on schema changes.** Archive the DB first. Add a nullable column in place with `ALTER TABLE` locally **and on Neon** before deploying. Alembic becomes the next milestone once real data exists.

## Project-specific notes

- Node/npm are not installed, so there is no frontend build step. Everything is served from `app/static/`.
- `README.md` must keep stating the cold-start behavior (~2.4s idle, ~13s after a new image) and the `create_all`/no-migrations decision, so both read as choices.
- `SESSION_HTTPS_ONLY=true` and a non-empty `SECRET_KEY` are required with a Postgres URL. `config.py` refuses to start without them, by design.
- Lambda redeploys need a **new image tag**. Reusing `:v1` does not pick up a new image.
- Scope is fixed. Out of scope: payment rails, multi-currency, member rename/delete, percentage splits, multi-payer expenses, notifications. Simplified debts shift between days by nature; don't "fix" the algorithm.
- The design rationale is in `~/.claude/plans/the-goal-of-this-transient-flamingo.md`.
