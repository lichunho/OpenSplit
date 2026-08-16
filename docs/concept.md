# Concept

What this app is, who it's for, and the trades it deliberately makes.

## The problem

Six people go on a trip. Someone buys dinner, someone else pays for the taxi, one person
covers the hotel deposit. By day three nobody remembers who owes what, and the group chat
has become a ledger nobody trusts.

Splitwise solves this. It also asks all six people to create an account, verify an email,
and install an app — for a trip that lasts a weekend. Most groups give up and use a
spreadsheet, or the one person who volunteers to "keep track."

This app is the small version: shared-expense tracking with **no accounts, no email, no
OAuth**. You send your friends a link. They open it, pick their name, and start adding
expenses.

## The access model: the link is the credential

This is the central design decision, and it's borrowed from [when2meet](https://when2meet.com).

A group lives at a secret URL — `/g/{slug}`, where the slug is generated with Python's
`secrets` module and is not guessable. There is no login. You identify yourself by picking
your name from a list. If you want, you can set a password on your own name.

The consequences, stated plainly:

- **Anyone with the link can view the group and add expenses.** That is not a bug. It is
  the same property that makes the app usable without accounts.
- **A per-member password only stops someone acting *as you*.** It is not a barrier to the
  group. If your friend has the link, they can already see everything.
- Because the link is the credential, it is treated like one: slugs come from `secrets`,
  and `robots.txt` disallows all crawling so no group ends up in a search index.

This is a real trade, not an oversight. The threat model is *"stop Alex clicking my name
and recording a settlement I didn't make."* It is not *"resist a targeted attacker."* For a
friend group splitting a dinner bill, the cost of accounts is higher than the risk they
prevent.

If your situation is different — money large enough that someone would bother, or a group
you don't fully trust — this is the wrong tool.

## What it does

- **Equal splits.** $100 dinner across three people, and the leftover cent is handled
  deterministically rather than swallowed.
- **Exact splits.** You had the steak, they had the salad. Amounts must sum to the total or
  the app rejects the expense.
- **Running balances.** Who is up, who is down, always summing to zero.
- **Simplified debts.** Instead of six people paying each other in a circle, the app
  suggests the shortest set of payments that clears everyone.
- **Recorded settlements.** The app records that money moved. It does not move money —
  there are no payment rails, by choice.
- **CSV export.** So the data is yours and the ledger is auditable outside the app.
- **Soft delete with undo.** Nothing is ever really deleted, because a mistyped expense is
  the most common error in an app like this.

## What it deliberately does not do

Real payment rails · multi-currency · expense editing · member rename or delete ·
percentage or share splits · multi-payer expenses · notifications.

Each of these is a reasonable feature. Each also multiplies the number of states the
balance math has to be correct in. The app's value is that its arithmetic is trustworthy;
every excluded feature is one fewer way for that to stop being true.

## Two honest caveats

### Simplified debts are unstable by nature

Simplifying a set of balances never changes anyone's **net** position — it only changes
which payment paths clear it. So "pay Alex $30" can legitimately become "pay Sam $30"
tomorrow, with no bug involved and nobody better or worse off.

This looks like a glitch the first time you see it. Two things make it bearable:

1. **Deterministic tie-breaks**, so the suggestion doesn't flicker between two equally
   valid answers on every page reload.
2. **A visible "someone else" option**, so paying whoever you're actually standing next to
   is a first-class action rather than fighting the app.

A better algorithm is not the fix. The optimal version of this problem is NP-complete, and
"perfectly stable" and "always minimal" are in tension anyway.

### Cold starts are accepted, not engineered around

On free hosting the first request to a sleeping app takes roughly 60–90 seconds. This is
**accepted** as the price of free hosting for a friend-group tool. The app's response is to
say so — in the README, and in a "waking up" indicator — rather than to add a paid
always-on tier or a keepalive pinger. A link that is slow once is fine. A link that looks
broken is not.

## Design philosophy

**All money is integer cents, everywhere.** No floats, ever, at any layer. Floating-point
money is the classic way for an app like this to be quietly wrong.

**The arithmetic is isolated and directly tested.** The split/balance/simplify functions
import nothing from the rest of the app — no database, no models. They take plain integers
and return plain integers. Every real bug in an expense splitter lives in those functions,
so they're testable without spinning up a web server.

**Shares are stored, not recomputed.** When someone joins the group mid-trip, they must not
silently appear in the history of expenses they weren't part of. Recomputing splits on
read would do exactly that.

**Deletes are soft.** Balance queries filter deleted rows; the activity feed still shows
them, struck through, with an undo control.

---

See [implementation.md](implementation.md) for how this is built, and
[instructions.md](instructions.md) for how to run and deploy it.
