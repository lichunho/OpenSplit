# Commands

Setup, running and testing locally. Deploying is in [deployment.md](deployment.md).

## Prerequisites

- **Python 3.13.** Newer versions may work, but 3.13 is what the app is developed and
  tested against, and it has settled wheels for `psycopg` and `pydantic-core` on macOS and
  Windows.
- **Docker** (optional) — only for `docker compose up`.
- **Node/npm are not required.** There is no frontend build step.

## Local setup

```bash
python3.13 -m venv .venv && source .venv/bin/activate   # macOS / Linux
# py -3.13 -m venv .venv && .venv/Scripts/activate      # Windows

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
pytest                                   # all 136 tests
pytest tests/test_money.py               # the money core only — fast, no DB
pytest tests/test_money.py::test_name    # a single test
pytest -q -W default                     # surface warnings
```

Tests use a temporary database and never touch your local `app.db`. Configuration variables
are in [config.md](config.md).

## Docker

```bash
docker compose up                        # same app on :8000
```

Environment variables pass through from your shell if set, with local defaults otherwise.
No secret is baked into the image.

> **The `Dockerfile` is exercised; this compose path is not.** The deployed Lambda image is
> built from that same `Dockerfile` (see [deployment.md](deployment.md)), so the image itself is
> known good. What hasn't been run against a live daemon is `docker compose up` specifically
> — `docker compose config` validates and no further. If it misbehaves, that's the likeliest
> place an error is hiding.

The compose default `DATABASE_URL` is `sqlite:///./app.db` *inside the container*, and
`.dockerignore` excludes `*.db`, so the container starts with an empty database that is lost
when the container is removed. Point `DATABASE_URL` at a real database to keep data.
