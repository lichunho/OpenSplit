# Directory map

One line per entry. Update this file when a path moves.

## Root

| Path | Purpose |
|---|---|
| `app/` | The application package |
| `tests/` | pytest suite |
| `documentation/` | Project docs; start at [README.md](README.md) |
| `README.md` | Public entry point: what the app is, quick start, the two accepted "bugs", the `create_all` decision |
| `CLAUDE.md` | Project guidance for Claude Code |
| `requirements.txt` | Runtime and test dependencies (pins SQLAlchemy ≥ 2.0.33) |
| `Dockerfile` | Python 3.13 slim image plus the Lambda Web Adapter; the deploy artifact |
| `docker-compose.yml` | Runs the same image locally on :8000 |
| `.env.example` | Placeholder config; copy it to `.env` |
| `.gitattributes` | `* text=auto`, which normalises line endings to LF |
| `.dockerignore` | Keeps the database, `.env`, `archive/` and `tests/` out of the image |
| `archive/` | Dated prior generations of the database (gitignored, private) |
| `app.db*` | Local SQLite database (gitignored, private) |

## `app/`

| Path | Purpose |
|---|---|
| `main.py` | Creates the app: middleware, static mount, routers, `create_all` lifespan, `/healthz`, `/robots.txt` |
| `config.py` | Every `os.environ` read, plus the template and static paths |
| `db.py` | Engine, session dependency, URL normaliser, pool config |
| `models.py` | SQLModel tables: `Group`, `Member`, `Expense`, `Share`, `Settlement` |
| `money.py` | Pure, DB-free money functions: parse, split, balances, simplify |
| `auth.py` | Session identity helpers, scrypt hash/verify, `require_member` |
| `queries.py` | Read-side queries and cents formatting shared across routes |
| `csv_import.py` | Pure CSV parser: exported CSV text in, `ImportPlan` out |
| `routes/groups.py` | Landing page, create group, dashboard, identify/switch, add member |
| `routes/expenses.py` | Expense add/edit/delete/restore, CSV export |
| `routes/settlements.py` | Settle-up flow, settlement delete/restore |
| `routes/imports.py` | CSV import form, preview, confirm |
| `templates/` | Jinja2 pages; `base.html` is the layout |
| `static/app.css` | One token-driven stylesheet with dark mode |
| `static/app.js` | Vanilla JS: expense-form toggles, settle hint, copy-link, cold-start indicator |
| `static/robots.txt` | Disallows all crawling |

## `tests/`

| Path | Purpose |
|---|---|
| `conftest.py` | Points `DATABASE_URL` at a temp SQLite file before `app.main` is imported; `client` fixture |
| `test_money.py` | Money core, called directly |
| `test_routes.py` | Routes via `TestClient`; the tests assert stored rows and exact integers |
| `test_csv_import.py` | CSV parser, called directly |
| `test_db.py` | Engine config: branch choice, URL normalisation, `NullPool`, WAL |
| `test_config.py` | Refuses to start on Postgres without `SESSION_HTTPS_ONLY` |
