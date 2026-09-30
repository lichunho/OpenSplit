# OpenSplit documentation

OpenSplit is an open-source Splitwise clone for splitting shared expenses within a friend
group. It has no accounts: a group lives at a secret link, and you pick your name from a list.
It is built with FastAPI and Jinja2 on SQLite locally and Neon Postgres in production, and is
deployed to AWS Lambda behind a Function URL. v1 is feature-complete, with 136 tests.

## Docs

| Doc | What's in it |
|---|---|
| [concept.md](concept.md) | What the app is and why: the access model, the trades it makes, scope, and a user guide |
| [architecture.md](architecture.md) | Stack, module rules, data model, core arithmetic, routes, UI, auth, the three production-only traps, testing design |
| [directory-map.md](directory-map.md) | Every meaningful path, one line each |
| [config.md](config.md) | `app/config.py`, load order, environment variables, deploy-time settings |
| [data-and-artifacts.md](data-and-artifacts.md) | The database as the versioned artifact, archive layout, schema changes, private data |
| [commands.md](commands.md) | Prerequisites, local setup, running, testing, Docker |
| [deployment.md](deployment.md) | Deploying to Lambda + Neon, the verification checklist, troubleshooting |
| [conventions.md](conventions.md) | How work is done here: privacy, code quality, archiving, docs, git |

## Reading order

1. [concept.md](concept.md): why the app is shaped this way. The link-is-the-credential
   model explains most of the other decisions.
2. [architecture.md](architecture.md): especially *Rules that span files* and *The three
   production-only traps*.
3. [conventions.md](conventions.md) before changing anything.
4. [commands.md](commands.md) to run it. The other docs are for reference as needed.

## Plans

Dated implementation plans from the agent pipeline go in `plans/`. The original v1 plan is
at `~/.claude/plans/the-goal-of-this-transient-flamingo.md`, outside the repo.
