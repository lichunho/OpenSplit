# Implementation

How the app is built: stack, module layout, data model, and the decisions that are
load-bearing.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Web | FastAPI + Jinja2 | Server-rendered HTML, no build step |
| DB | SQLModel — SQLite locally, Postgres in prod | One `DATABASE_URL` switch |
| Driver | psycopg 3 (`postgresql+psycopg://`) | Correct behaviour behind a transaction-mode pooler; Windows wheels, no compiler |
| Sessions | Starlette `SessionMiddleware`, signed cookie | No session store to run |
| Passwords | `hashlib.scrypt`, N=2¹⁴ r=8 p=1 | Stdlib; see trap #2 below |
| Migrations | `create_all` (Alembic deferred) | See trap #3 below |
| Hosting | AWS Lambda behind a Function URL + Neon free Postgres | Lambda runs the container image unmodified via the Web Adapter, inside the permanently free tier at this scale; ~2.4s cold start. Neon's free tier persists rather than expiring after 30 days |
| CSS | One token-driven stylesheet, dark mode via `prefers-color-scheme` | No build step; see the UI layer section |
| JS | ~150 lines of vanilla JS | Expense-form toggle, settle hint, copy-link, cold-start indicator |

The whole stack follows from one environment fact: **Node/npm are not installed** on the
development machine. So: no frontend build step, no bundler, no CDN. Everything is served
from `app/static/` and works with no network beyond the app itself.

## Module map

Each module has one job.

```
app/
  main.py        FastAPI app, middleware, static mount, router registration — no business logic
  config.py      every os.environ read in the codebase lives here
  db.py          engine + session dependency, URL normalisation, pool config
  models.py      SQLModel tables
  money.py       PURE functions: parse_amount_cents, split_equal, validate_exact, net_balances, simplify
  auth.py        session helpers, scrypt hash/verify, require_member
  queries.py     read-side queries + cents formatting shared by more than one route
  csv_import.py  PURE: exported CSV text -> an ImportPlan of rows to create
  routes/        groups.py, expenses.py, settlements.py, imports.py
  templates/     base, index, identify, group, expense_form, settle_pick, settle_confirm,
                 import_form, import_preview
  static/        app.css, app.js, robots.txt
tests/           test_money.py (28), test_routes.py (82), test_csv_import.py (17), test_db.py (6),
                 test_config.py (2)
```

Rules that span files:

- **`config.py` owns every `os.environ` read.** `db.py` imports `DATABASE_URL` from it;
  `auth.py` imports `SECRET_KEY`. Neither touches the environment directly. This keeps
  configuration auditable in one place instead of scattered across modules. Owning both
  values is also what lets it refuse to start on a Postgres `DATABASE_URL` with
  `SESSION_HTTPS_ONLY` unset — a deploy whose session cookie would ship without `Secure`.
  That check has to be loud, because the resulting app looks entirely healthy.
- **`money.py` imports nothing from the app.** No DB, no models, no config — stdlib only
  (`heapq`, `re`, `decimal`). It takes and returns plain ints, dicts and tuples. This is what
  makes it testable without a web server or a database. The decimal-string parsers live here
  rather than beside the expense form that first needed them: three callers now share them —
  the expense form, the settle form and the importer — and a DB-free importer must not have
  to import a route module to reach one.
- **`csv_import.py` imports nothing from the app but `money.py`.** Same reason, same payoff:
  text in, dataclasses out, so `test_csv_import.py` exercises every parse rule with no
  `TestClient` and no `Session`. Resolving names to member ids and writing rows is
  `routes/imports.py`'s job, and the split is what keeps the parser honest.
- **`main.py` is a thin entry point.** Wiring only.
- **`queries.py` exists because three route modules needed the same reads.** Before it,
  `balance_inputs` and cents-formatting had been copy-pasted across modules and had already
  started to drift — one copy of the formatter mishandled negative amounts. Shared reads
  live in one place now.

## Data model

All money is `int` cents. There is no `Float` or `Numeric` column anywhere.

| Table | Fields |
|---|---|
| `Group` | `id`, `slug` (unique, `secrets.token_urlsafe(8)`), `name`, `currency_symbol`, `created_at` |
| `Member` | `id`, `group_id`, `name`, `password_hash` (nullable), `created_at`; unique `(group_id, name)` |
| `Expense` | `id`, `group_id`, `description`, `amount_cents`, `payer_id`, `split_type`, `created_by_id`, `note`, `category` (nullable), `created_at`, `deleted_at` |
| `Share` | `id`, `expense_id`, `member_id`, `amount_cents` |
| `Settlement` | `id`, `group_id`, `from_member_id`, `to_member_id`, `amount_cents`, `created_by_id`, `note`, `created_at`, `deleted_at` |

Four decisions worth knowing:

**Shares are materialised, not recomputed.** A `Share` row per participant is written when
the expense is created. Recomputing on read would mean a member who joins mid-trip silently
appears in the history of expenses they weren't part of.

**`Settlement` is its own table, not an expense variant.** Two parties, no shares. Folding
it into `Expense` would mean nullable columns and `if split_type == "settlement"` branches
everywhere. The activity feed merges the two lists by `created_at` at render time.

**Categories are free text, not a table.** `Expense.category` is a nullable string. There
is no `Category` table, no per-group seeding and no management UI: a category exists the
moment someone names one on the expense form, and stops existing when the last expense using
it stops using it. There is **no built-in starter list** either — `queries.used_categories`
is the whole of it, so a group's categories are exactly the ones that group has used, and a
new group starts with none. `NULL` means uncategorized, which is what every expense predating
the column reads back as.

**The picker is a `<select>` plus a "Custom…" escape hatch.** The dropdown lists
`No category`, the group's existing categories, then `Custom…` (value `__custom__`), which
reveals a text field for a name not on the list. A category's *first* use therefore always
goes through Custom…; after that it is an ordinary option. Because the list is per-group, a
custom name is available to that group and no other — "this trip only" falls out of
`used_categories` rather than needing storage of its own.

> `routes/expenses._chosen_category` reads both fields. The select is validated against the
> known options — a `<select>` constrains the browser, not a hand-written POST, so this is
> the same check `payer_id` already gets against the roster. A custom name is folded against
> the known options case-insensitively, so "food" typed today joins yesterday's "Food" rather
> than opening a second tab beside it; unmatched names are kept exactly as typed, over-long
> ones truncated to 40 characters. The sentinel itself is rejected as a name, or its
> `<option value="__custom__">` would read back as "Custom…" on the next render.

> **JS-off:** the custom field is rendered visible and hidden by a `category-known` class
> that only `app.js` adds — the same arrangement as `.share-input` for exact splits. With
> scripting off, the dropdown and the text box are both on screen and picking `Custom…`
> still works. There is no second server path for the two cases.

**Soft delete.** `Expense` and `Settlement` carry `deleted_at`. Every balance query filters
`deleted_at IS NULL`; the feed still shows deleted rows struck through, with undo.

> A subtlety worth its own note: `Share` has **no** `deleted_at` of its own. Shares are
> gated by joining back to `Expense` and filtering the *expense's* `deleted_at`. Filtering
> only the expense table while still summing every share row would drop an expense's
> paid-side but keep its owed-side, corrupting every balance by that expense's total — and
> the result would still look like a plausible number. This join lives in exactly one
> place, `queries.balance_inputs`, for that reason.

## The core arithmetic (`money.py`)

Four pure functions:

- **`split_equal(total_cents, member_ids)`** — `divmod`, then distribute the remainder cents
  starting at index `total_cents % n` over members sorted by id. The rotating start means
  the extra cent moves between expenses instead of permanently taxing the lowest member id,
  while staying fully deterministic. Rejects duplicate ids: a dict comprehension would
  silently collapse them and produce shares that don't sum to the total.
- **`validate_exact(total_cents, shares)`** — rejects unless shares sum exactly to the
  total; rejects negative shares and an empty participant set. Zero shares are allowed
  (someone skipped the appetiser).
- **`net_balances(expenses, shares, settlements)`** —
  `paid − owed + sent − received`. Paying someone moves your balance **up** toward zero and
  theirs down. Always sums to exactly zero.
- **`simplify(balances)`** — greedy largest-debtor/largest-creditor via two heaps keyed
  `(balance, member_id)`, producing at most `n−1` transfers. The member-id component is what
  makes ties break deterministically; without it the suggested payee visibly flickers
  between reloads and reads as a bug.

Group membership is deliberately **not** checked here — that needs database state, and
keeping this module DB-free is the point. The route layer validates that the payer and
every participant belong to the group.

## Money parsing at the boundary

Decimal strings become cents with `Decimal` + `ROUND_HALF_UP`, never `float()`, with a
$1,000,000 sanity ceiling.

`Decimal` accepts considerably more than a money field should, which is worth stating
explicitly because two cases are genuinely dangerous:

- `"1e5"` is a valid `Decimal` worth 100,000 — scientific notation would book a $100,000
  expense from five characters.
- `"NaN"` and `"Infinity"` parse without raising, and `Decimal("NaN") <= 0` is `False`, so a
  naive "reject non-positive amounts" guard does **not** catch them.

Both are blocked by matching the raw string against a plain-digits pattern *before* handing
it to `Decimal`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Landing + create-group form |
| POST | `/groups` | Create group, redirect to `/g/{slug}` |
| GET | `/g/{slug}` | Dashboard: balances, simplified debts, activity feed; `?category=` filters the feed |
| GET/POST | `/g/{slug}/identify` | when2meet sign-in |
| POST | `/g/{slug}/switch` | Forget identity for this group ("not you?") |
| POST | `/g/{slug}/members` | Add a member by name |
| GET/POST | `/g/{slug}/expenses/new` | Expense form / create |
| GET/POST | `/g/{slug}/expenses/{id}/edit` | Same form, prefilled / update in place |
| POST | `/g/{slug}/expenses/{id}/delete` · `/restore` | Soft delete / undo |
| GET | `/g/{slug}/settle` | Step 1 — who are you paying |
| GET | `/g/{slug}/settle/{member_id}` | Step 2 — confirm amount |
| POST | `/g/{slug}/settle` | Record it |
| POST | `/g/{slug}/settlements/{id}/delete` · `/restore` | Undo a mistaken settlement |
| GET | `/g/{slug}/export.csv` | Expenses + settlements + balances |
| GET | `/g/{slug}/import` | Upload form |
| POST | `/g/{slug}/import/preview` | Parse and show what will be created — writes nothing |
| POST | `/g/{slug}/import/confirm` | Re-parse and write |
| GET | `/healthz` | Uptime check |
| GET | `/robots.txt` | Disallow all |

**All POSTs redirect 303** so a refresh doesn't double-submit.

**CSV import re-reads the export**, which makes the export a contract rather than a
one-way door. Four things about it are load-bearing:

- **Columns are matched by name, never by position.** Each section's header row becomes a
  `{casefolded name: index}` map. The columns the parser needs are looked up in it, missing
  required ones are named in one error, and every other column in the file is ignored. This
  is what lets the export keep growing — Category was added after the first release — without
  stranding files exported by an older version.
- **Every bad row is reported, not just the first.** A row that fails validation is skipped
  with a `Row N:` message and parsing continues, so one typo in row 3 doesn't hide the twelve
  good rows behind it. `N` is the physical line number, which is what a spreadsheet shows.
- **The file crosses the two steps in a hidden field**, and `import/confirm` re-parses it
  from scratch. There is no staging table (that would be a schema change — see the
  `create_all` trap) and no session storage (a signed cookie can't hold a group's history).
  The hidden field is a transport, not a source of truth.
- **Timestamps are reconstructed.** The export writes date only, so rows are written
  oldest-first and each row on a given date gets its own timestamp one second apart.
  Without that, every row on a day would share one midnight and the feed's `created_at desc`
  sort would order them arbitrarily instead of reproducing the file.

Shares come back from the file verbatim — an `equal` row is **not** re-split on import. Re-running
`split_equal` would be free to hand the rounding cent to a different member than the original
expense did, quietly changing two people's balances on a round trip.

**The dashboard's `category` query param has three states**, and needs no sentinel value for
the third — a category can never be the empty string, because `_chosen_category` maps
blank input to `None`:

| URL | Shows |
|---|---|
| `/g/{slug}` | everything |
| `/g/{slug}?category=Food` | that category only |
| `/g/{slug}?category=` | uncategorized expenses only |

The filter narrows **the activity feed and nothing else**. Balances and suggested settlements
are always computed from the unfiltered `queries.balance_inputs`, because what someone owes
must not depend on which tab is open. Settlements carry no category, so they appear under
"All" and under no other tab.

`require_member(slug)` is the dependency guarding every group route. It returns
`(group, member)` or a redirect to the identify page — so every route calls
`is_redirect(result)` before unpacking. Forgetting that check raises a loud `TypeError`
rather than failing open.

## UI layer

One stylesheet, one script, no build step. `app/static/app.css` is a single hand-written
file: **design tokens on `:root`, then components, then two media queries.** Every colour
goes through a `var(--…)`, so the `@media (prefers-color-scheme: dark)` block redefines
tokens only — it contains no layout or component rules, and there is exactly one definition
of each rule in the file.

Layout is mobile-first and grows by two breakpoints. Turn both off and what is left is the
phone layout, which is the one that has to work one-handed at a restaurant table:

| Width | What changes |
|---|---|
| base | Single column. `.wrap` caps at 480px. Sticky `.submit-bar` keeps the primary action reachable on long forms. |
| ≥ 700px | Form pages split into two columns (`.form-cols`, `.hero`, `.two-col`); `.submit-bar` un-sticks into an ordinary right-aligned action. |
| ≥ 960px | The dashboard becomes `minmax(0, 1fr) 22rem` — activity feed in the main column, a sticky rail holding Balances / Suggested settlements / Members / Share link. |

Only `group.html` opts into the full width, via `{% block wrap_class %}wrap--wide{% endblock %}`
on `base.html`'s `<main class="wrap …">`. Pages that split in two take `wrap--medium`;
everything else keeps the 480px reading measure however wide the window is.

Two conventions worth keeping:

- **`:hover` rules live inside `@media (hover: hover) and (pointer: fine)`.** Touch devices
  keep `:active` only, so a tap doesn't leave a row stuck in its hover state.
- **The markup is load-bearing for some tests.** `test_routes.py` parses the dashboard for
  the literal headings `<h2>Balances</h2>` and `<h2>Suggested settlements</h2>`, expects the
  transfer `<li>` tags to carry no attributes and sit on one source line, and string-matches
  the share-link input and the hidden copy button attribute-for-attribute. Restyle those
  through their existing ids and classes rather than rewriting their tags.

## Auth

`hashlib.scrypt` at **N=16384 (2¹⁴), r=8, p=1**, a 16-byte `secrets.token_bytes` salt, and
`hmac.compare_digest` to verify. The stored value is self-describing —
`scrypt$N$r$p$salt$hash` — and verification reads N/r/p back out of the string, so the cost
can be raised later without a migration or invalidating existing passwords.

The session cookie holds a `{group_id: member_id}` map, so one browser can hold identities
in several groups at once. It's capped at 50 entries so it can't outgrow the 4 KB cookie
limit.

## The three production-only traps

These fail **only** on deployed free-tier infrastructure. A green local `pytest` proves
nothing about them, which is why `db.py` and `auth.py` get care disproportionate to their
size.

### 1. Neon autosuspends at 5 minutes; idle Lambda sandboxes are torn down

In that gap a live process holds pooled connections to a sleeping database, and the next
request dies with `SSL SYSCALL error: EOF detected` — a 500, not a slow page, and
unreproducible on local SQLite.

`db.py` must therefore:
- use Neon's **pooled** host (hostname contains `-pooler`);
- set **`poolclass=NullPool`** for Postgres — PgBouncer is already in front, so let Neon own
  pooling. No pooled connection survives to be reused against a sleeping database, which
  deletes the entire bug class. Per-request handshake cost is noise next to a cold start;
- pin **SQLAlchemy ≥ 2.0.33** (earlier versions reuse idle connections regardless of pool
  settings);
- normalise `postgres://` and bare `postgresql://` → `postgresql+psycopg://` in exactly one
  place;
- on the SQLite branch, set `check_same_thread=False` and enable WAL.

`tests/test_db.py` pins the branch selection, the normalisation (including that
`?sslmode=require` survives), `NullPool`, and WAL — all offline, since building an engine
opens no connection.

`NullPool` serves double duty on Lambda: a frozen execution environment must not hold a
pooled socket across invocations either, for the same reason it must not hold one across a
Neon autosuspend. One setting covers both. It would be the wrong setting on RDS or any
always-on Postgres — no autosuspend means the per-request handshake buys nothing and a real
pool with `pool_pre_ping` would be correct.

### 2. `hashlib.scrypt` raises `ValueError` at OWASP's recommended parameters

CPython defaults `maxmem=0`, which OpenSSL caps at 32 MiB. Memory used is `128·N·r`, so
N=2¹⁵ with r=8 hits the cap exactly and throws — code that ships green and dies on the first
real login. 128 MiB per concurrent login would also OOM a 512 MB free instance.

Hence N=2¹⁴ (~16 MiB), under the default cap.

### 3. `create_all` silently no-ops on schema changes

Add a column post-launch and every page touching it 500s, with no error at deploy time.

This is an accepted v1 trade. Before there is real data, a schema change means dumping the
database to `archive/YYYY-MM-DD_<description>/` (gitignored — it holds real names and
spending history) and recreating the Neon branch. **Alembic becomes the next milestone the
moment there is data worth preserving.**

## Testing

135 tests: **28** on the money core, **82** on routes, **17** on the CSV parser, **6** on
engine configuration, and **2** on the refuse-to-start config guard.

The split is deliberate. `test_money.py` hits the pure functions directly — that's where
the real bugs are, and those tests need no database, no HTTP, and no fixtures.
`test_routes.py` drives the app through `TestClient` and asserts **stored rows and exact
integers**, not rendered text, so the templates stay free to change. `tests/conftest.py`
points `DATABASE_URL` at a temporary file *before* `app.main` is imported, because `db.py`
builds its engine at import time — without that, running the suite would write to the
developer's real database.

---

See [concept.md](concept.md) for why the app is shaped this way, and
[instructions.md](instructions.md) for setup and deployment.
