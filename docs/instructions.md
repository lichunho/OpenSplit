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
to `false` so local development works, and the deploy sets it to `true` through the
function's environment (see `env.json` under [Deploying](#deploying)). Forgetting it on a
redeploy would ship the session cookie without the `Secure` flag in an app that otherwise
looks fine, so `config.py` refuses to start when `DATABASE_URL` points at Postgres and this
is not `true`. If a deploy dies at import with that `RuntimeError`, this is the fix — set
the variable rather than removing the check.

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

The container image runs on **AWS Lambda** behind a Function URL, with **Neon** for
Postgres. At this scale Lambda sits inside its permanently free tier, so the only real cost
is ECR image storage (under $1 for two months). Postgres lives on Neon rather than anywhere
attached to the compute because Neon's free tier persists and survives the function being
replaced.

### 1. Create the Neon database

Create a Neon project, then copy its **pooled** connection string — the hostname contains
`-pooler`. This matters: the app sets `poolclass=NullPool` and lets Neon's PgBouncer own
connection pooling. Using the unpooled host reintroduces the `SSL SYSCALL error: EOF
detected` failure described in [implementation.md](implementation.md#the-three-production-only-traps).

### 2. Deploy to Lambda

The [AWS Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter) is baked into the
`Dockerfile`; it speaks the Lambda Runtime API on one side and plain HTTP to uvicorn on the
other, so no application code is Lambda-specific and the same image still runs under
`docker compose up`. The free tier covers 1M requests and 400,000 GB-seconds per month.

Prerequisites: `brew install awscli`, Docker Desktop running, then `aws configure` with
region `us-east-1`.

Don't deploy as the account root. Create an IAM user scoped to just this project and use a
named profile, so a leaked key can touch this one function and nothing else — not billing,
not other services:

```bash
aws iam create-user --user-name splitwise-deploy
aws iam put-user-policy --user-name splitwise-deploy \
  --policy-name splitwise-deploy-scoped --policy-document file://policy.json
aws iam create-access-key --user-name splitwise-deploy   # write into ~/.aws/credentials
```

The policy grants ECR push/pull on the `splitwise-clone` repository, Lambda management on
the `splitwise-clone` function, `iam:PassRole` on `splitwise-lambda-role` only, and read
access to that function's logs. Note that `logs:DescribeLogGroups` has no resource-level
support and must be granted on `"Resource": "*"` — scoping it to the log-group ARN denies it.

Append `--profile splitwise` to the commands below, or `export AWS_PROFILE=splitwise`.

```bash
# 1. ECR repository, and log in to it
aws ecr create-repository --repository-name splitwise-clone --region us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin $REGISTRY

# 2. Build and push. arm64 is native on Apple silicon — no emulation, and no
#    "exec format error" from an arch mismatch with the function below.
#    --provenance/--sbom must be off: buildx otherwise pushes an OCI *image index*
#    bundling the image with an attestation manifest, and Lambda accepts only a
#    single image manifest. It fails with "The image manifest, config or layer
#    media type for the source image is not supported".
docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
  -t $REGISTRY/splitwise-clone:v1 --push .

# 3. Execution role. The app calls no AWS services, so basic logging is all it needs.
aws iam create-role --role-name splitwise-lambda-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name splitwise-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# 4. The function. Timeout must exceed 3s (the default) or a cold start that also
#    waits on a Neon resume will be killed mid-boot. Memory buys CPU, which buys
#    a shorter cold start, and 1024 MB is still a rounding error against the free tier.
aws lambda create-function --function-name splitwise-clone \
  --package-type Image --code ImageUri=$REGISTRY/splitwise-clone:v1 \
  --role arn:aws:iam::$ACCOUNT:role/splitwise-lambda-role \
  --architectures arm64 --memory-size 1024 --timeout 30 \
  --environment file://env.json

# 5. Public Function URL. No API Gateway: it adds per-request cost after 12 months
#    and buys nothing here.
aws lambda create-function-url-config --function-name splitwise-clone --auth-type NONE

#    BOTH permissions are required. Since October 2025 a function URL needs
#    lambda:InvokeFunction in addition to lambda:InvokeFunctionUrl, and they must be
#    added as separate statements. With only the first, every request returns a bare
#    403 "Forbidden" even though AuthType is NONE and the policy looks correct —
#    there is nothing in the policy output to suggest what is missing.
aws lambda add-permission --function-name splitwise-clone \
  --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl \
  --principal '*' --function-url-auth-type NONE
aws lambda add-permission --function-name splitwise-clone \
  --statement-id UrlPolicyInvokeFunction --action lambda:InvokeFunction \
  --principal '*' --invoked-via-function-url
```

Step 4 reads `env.json` from a file rather than taking `--environment` inline, because the
value embeds the Neon password and an inline flag would land in shell history. Write it,
use it, delete it — and never commit it:

```json
{"Variables":{"DATABASE_URL":"postgresql://...-pooler.../db?sslmode=require",
              "SECRET_KEY":"<python3 -c 'import secrets;print(secrets.token_urlsafe(32))'>",
              "SESSION_HTTPS_ONLY":"true"}}
```

Public access with `--auth-type NONE` is correct here rather than an oversight: the link is
the credential by design, and `robots.txt` disallows all crawling.

**Redeploying after a code change:** rebuild and push the image, then
`aws lambda update-function-code --function-name splitwise-clone --image-uri $REGISTRY/splitwise-clone:v2`.
Lambda resolves image tags to a digest at deploy time, so reusing the `:v1` tag will *not*
pick up a new image — bump the tag.

**If the URL 403s,** check the permissions above before anything else. To tell a broken
function apart from a blocked URL, invoke it directly — this bypasses URL authorization
entirely, so a 200 here plus a 403 through the URL means the problem is permissions, not
your code:

```bash
aws lambda invoke --function-name splitwise-clone --region us-east-1 \
  --payload '{"version":"2.0","rawPath":"/healthz","requestContext":{"http":{"method":"GET","path":"/healthz"}},"headers":{},"isBase64Encoded":false}' \
  /tmp/out.json && cat /tmp/out.json
```

**Expect a short cold start.** Measured at ~2.4s with both the Lambda sandbox and Neon cold,
against ~260ms warm. The first request after deploying a new image is much slower (~13s
observed: 9.8s init to pull the image and run `create_all`, plus 2.9s handler) — that init
sits close to Lambda's 10s ceiling, which is why `AWS_LWA_ASYNC_INIT=true` is set in the
Dockerfile. If steady-state cold starts exceed 10s, raise `--memory-size` to 1769 (a full
vCPU) before changing anything else.

Measure it properly from Lambda's own records rather than by timing `curl`, which cannot
tell a cold invocation from a warm one:

```bash
aws logs filter-log-events --log-group-name /aws/lambda/splitwise-clone \
  --filter-pattern "Init Duration" --query 'events[].message' --output text
```

**Teardown.** Only ECR accrues charges while idle, but delete all of it:

```bash
aws lambda delete-function --function-name splitwise-clone
aws ecr delete-repository --repository-name splitwise-clone --force
aws logs delete-log-group --log-group-name /aws/lambda/splitwise-clone
aws iam detach-role-policy --role-name splitwise-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name splitwise-lambda-role
```

If the group holds real data by then, export it first — see [Schema changes](#schema-changes)
and the archive rule in [CLAUDE.md](../CLAUDE.md).

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
12. **Round-trip it:** create a second, empty group, import that same `export.csv`, and
    confirm the two dashboards show **identical balances** and the same roster. Then move a
    column in the file, add a junk column, and import again into a third group — it must
    still work, which is the whole point of matching columns by name.
13. **The test only production can run:** idle for 20 minutes — past both Neon's 5-minute
    autosuspend and the teardown of an idle Lambda sandbox — come back, and confirm the app
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
9. **Import a CSV** from the same link on the group page. Upload a file shaped like the
   export — an **Expenses** section and, optionally, a **Settlements** one, each with its own
   header row. Columns are matched **by name**, so their order doesn't matter and extra
   columns are ignored; a file exported by an older version still imports. Anyone named in
   the file who isn't in the group yet is added to the roster. You get a preview of
   everything that will be created, including new members, before anything is saved.
   Importing **adds** — it never changes or removes what's already there, so importing the
   same file twice gives you two copies of it.

Mistyped something? Everything is soft-deleted, so **delete and restore both work** on
expenses and settlements.

## Troubleshooting

**The link is slow on the first load.** Free-tier cold start — a few seconds normally,
longer on the first request after a fresh deploy. See above.

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
