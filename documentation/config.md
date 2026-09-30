# Configuration

## Where config lives

[`app/config.py`](../app/config.py) is the central config module and owns every `os.environ`
read in the codebase. It also defines the paths: `BASE_DIR` (`app/`), `TEMPLATES_DIR` and
`STATIC_DIR`. Other modules import values from it:

- `db.py` imports `DATABASE_URL` and owns the URL normaliser (`postgres://` and bare
  `postgresql://` → `postgresql+psycopg://`).
- `main.py` imports `SECRET_KEY` and `SESSION_HTTPS_ONLY` for `SessionMiddleware`, and
  `STATIC_DIR`.
- Each module in `routes/` imports `TEMPLATES_DIR`.
- `auth.py` imports nothing from config; it works on the session the middleware signs.

## How it loads

1. At import, `config.py` loads `.env` from the repo root with `python-dotenv`. It **does not
   override** variables already in the environment, so the deployed function's environment
   always beats a stray local file.
2. Each value is read from the environment, with the defaults below.
3. If `DATABASE_URL` starts with `postgres` and `SESSION_HTTPS_ONLY` is not `true`, or
   `SECRET_KEY` is unset or empty, import raises `RuntimeError`.

Values are read once at import. `db.py` builds its engine at import time too, which is why
`tests/conftest.py` sets `DATABASE_URL` (a temp SQLite file), `SECRET_KEY` and
`SESSION_HTTPS_ONLY` *before* importing `app.main`.

## Settings

Copy `.env.example` to `.env` to override defaults locally. Nothing in it is required for
local development.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/app.db` | In production, Neon's **pooled** connection string — the hostname must contain `-pooler` |
| `SECRET_KEY` | generated at startup | Set a fixed value to keep sessions across restarts. Required with Postgres |
| `SESSION_HTTPS_ONLY` | `false` | Must be `true` in production. See below |

**On `SESSION_HTTPS_ONLY`:** a `Secure` cookie is not sent by browsers over plain `http://`,
so setting this to `true` locally would silently break sign-in on `127.0.0.1`. It defaults
to `false` so local development works, and the deploy sets it to `true` through the
function's environment (see `env.json` under [Deploying](deployment.md#deploying)).
Forgetting it on a redeploy would ship the session cookie without the `Secure` flag in an
app that otherwise looks fine, so `config.py` refuses to start when `DATABASE_URL` points at
Postgres and this is not `true`. If a deploy dies at import with that `RuntimeError`, this is
the fix — set the variable rather than removing the check.

**On `SECRET_KEY`:** the generated fallback is per process, so on Lambda every instance
would sign cookies with a different key and users would be signed out at random. For the
same reason, `config.py` refuses to start on a Postgres URL when `SECRET_KEY` is unset or
empty.

Real secrets never belong in the repo. `.env`, `*.db`, `.venv/`, `export.csv` and
`archive/` are all gitignored.

## Settings outside `config.py`

These are deploy-time settings, not app reads, so they live beside the thing they configure:

| Where | Setting | Value |
|---|---|---|
| `Dockerfile` | `AWS_LWA_PORT`, `AWS_LWA_READINESS_CHECK_PATH`, `AWS_LWA_ASYNC_INIT` | `8000`, `/healthz`, `true` — Lambda Web Adapter |
| `Dockerfile` | `PORT` | `8000` unless overridden |
| `docker-compose.yml` | `DATABASE_URL`, `SECRET_KEY`, `SESSION_HTTPS_ONLY` | From the host shell, else `sqlite:///./app.db`, empty (so a generated dev key), `false` |
| `env.json` (never committed) | the three variables above | The Lambda function's environment; see [deployment.md](deployment.md) |

## Known debt

None. No module besides `config.py` reads the environment or hardcodes a path.
