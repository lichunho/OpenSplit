# Conventions

How work is done in this repo. This applies to human contributors and AI agents alike.

## Data privacy

Never commit secrets, credentials or private data. [data-and-artifacts.md](data-and-artifacts.md#private-data)
lists what that means here: `.env`, the live `SECRET_KEY` and `DATABASE_URL`, local `*.db`
files, `export.csv`, `archive/`, and `env.json` during a deploy. Only `.env.example`, with
placeholder values, is committed. When in doubt about a file, ask.

## Working style

1. **Ask clarifying questions until you are sure how something should be implemented.** Don't
   guess at ambiguous requirements.
2. **Fact-check claims.** When a request states something verifiable, such as a filename, a count
   or an API, check it against the code before acting and point out any discrepancy.

## Code quality

- **KISS and SOLID.** Use the simplest design that meets the requirement. Three explicit lines
  beat one clever abstraction. Don't write a helper that is used only once, a parameter for a
  hypothetical future, or error handling for an impossible condition. If a focused task runs
  past ~50 lines, reconsider the design.
- **Preserve existing comments.** Don't remove comments already present unless asked. In new
  code, comment only when the *why* is non-obvious.
- **All config and paths come from [`app/config.py`](../app/config.py).** No other module reads
  `os.environ` or hardcodes a path. See [config.md](config.md).
- **Thin entry points.** `app/main.py` wires middleware and registers routers, with no
  business logic.
- **One job per module.** See [architecture.md](architecture.md#module-map).
- **Don't reach through modules.** Call the public interface and don't import a sibling's
  internals. `app/money.py` imports nothing from the app, and `app/csv_import.py` imports
  only `money.py`.
- **Add behavior with new flags or new modules**, not branches inside an existing core loop.
- **Preserve sensible defaults.** An override flag keeps the existing default behavior.

## Archive before a destructive change

Before any irreversible change to the database schema or contents, copy the old generation
to `archive/YYYY-MM-DD_<short-description>/`. Nothing in config ever references that folder.
Prefer an in-place `ALTER TABLE` that keeps the data over dropping and recreating. The
procedure is in [data-and-artifacts.md](data-and-artifacts.md#schema-changes).

## Documentation

Read [README.md](README.md) at session start. Update the doc that matches the change:

| Change | Doc |
|---|---|
| A design decision, the access model, or scope | [concept.md](concept.md) |
| Structure: modules, data model, arithmetic, routes | [architecture.md](architecture.md) |
| A file path moves | [directory-map.md](directory-map.md) |
| A config variable | [config.md](config.md) |
| The database, archive, or private data | [data-and-artifacts.md](data-and-artifacts.md) |
| A local command or step | [commands.md](commands.md) |
| A deploy step or troubleshooting entry | [deployment.md](deployment.md) |

Skip doc updates for bug fixes and refactors that don't move files. The root `README.md`
must keep stating the cold-start behavior and the `create_all`/no-migrations decision, so
that both read as choices rather than bugs.

## Git

- Never push. The maintainer pushes manually.
- Make small commits as work lands.
- Commit messages are a single line, with no body and no trailing summary.
- No AI co-author trailers.
- Line endings normalize to LF (`.gitattributes`). Don't commit fixtures that depend on CRLF.
