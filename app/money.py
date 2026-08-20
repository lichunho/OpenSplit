"""
Pure money math: parse, split, balances, simplify.

Imports nothing from the app — no DB, no models, no config. Every function
takes and returns plain ints, dicts, tuples and lists, so tests can hit them
directly. All money is integer cents; there are no floats in this module.

The two parsers live here rather than beside the form that first needed them
because three callers now share them — the expense form, the settle form and
the CSV importer — and a route module is the wrong thing for a DB-free
importer to import.
"""
import heapq
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP

# $1,000,000 sanity ceiling on a single expense — see CLAUDE.md's money
# parsing note. Anything above this is almost certainly a typo (missing a
# decimal point), not a real trip expense.
_MAX_AMOUNT_CENTS = 100_000_000

# Decimal() accepts far more than a money field should: "1e5" is a valid
# Decimal worth $100,000, and "NaN"/"Infinity" parse without raising, so a
# later `cents <= 0` guard never catches them. Gate on plain digits first.
_AMOUNT_RE = re.compile(r"-?\d+(\.\d+)?")


def parse_amount_cents(raw: str) -> int:
    """Decimal string -> integer cents. Never float() anywhere in this app.
    Rejects blank/non-numeric input, zero, negatives, and anything past the
    sanity ceiling — always with a readable ValueError, never a 500."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter an amount.")
    if not _AMOUNT_RE.fullmatch(raw):
        raise ValueError("Enter a valid amount.")
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError):
        raise ValueError("Enter a valid amount.")
    if cents <= 0:
        raise ValueError("Amount must be greater than zero.")
    if cents > _MAX_AMOUNT_CENTS:
        raise ValueError("Amount is too large (over $1,000,000).")
    return cents


def parse_share_cents(raw: str) -> int:
    """Same parsing as parse_amount_cents, but zero (and a blank field) is
    allowed — an exact split can include someone who skipped the appetizer."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    if not _AMOUNT_RE.fullmatch(raw):
        raise ValueError("Enter a valid amount for each participant.")
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError):
        raise ValueError("Enter a valid amount for each participant.")
    if cents < 0:
        raise ValueError("Shares can't be negative.")
    return cents


def split_equal(total_cents, member_ids):
    """Split total_cents equally over member_ids -> {member_id: cents}.

    The shares always sum to exactly total_cents. The leftover cents from the
    division are handed out one each, starting at index `total_cents % n` over
    the members sorted by id, so the extra cent rotates across expenses instead
    of permanently taxing the lowest member id. Fully deterministic: the same
    inputs always produce the same shares.
    """
    ids = sorted(member_ids)
    n = len(ids)
    if n == 0:
        raise ValueError("split_equal needs at least one member")
    if len(set(ids)) != n:
        raise ValueError("split_equal got duplicate member ids")
    if total_cents < 0:
        raise ValueError("split_equal needs a non-negative total")

    base, remainder = divmod(total_cents, n)
    shares = {member_id: base for member_id in ids}
    # `% n` again because the rotation offset must be a valid index.
    start = (total_cents % n) % n
    for step in range(remainder):
        shares[ids[(start + step) % n]] += 1
    return shares


def validate_exact(total_cents, shares):
    """Raise ValueError unless `shares` is a usable exact split of total_cents.

    `shares` is {member_id: cents}. Zero shares are allowed — someone skipped
    the appetizer — but negative shares, an empty participant set, and any sum
    that misses the total are rejected.

    Note this does not check that the payer is in the group: that needs group
    membership, which is route context, and money.py stays free of app state.
    The caller validates the payer.
    """
    if not shares:
        raise ValueError("an exact split needs at least one participant")
    if any(amount < 0 for amount in shares.values()):
        raise ValueError("shares cannot be negative")
    total_shares = sum(shares.values())
    if total_shares != total_cents:
        raise ValueError(
            "shares sum to {} cents, expected {}".format(total_shares, total_cents)
        )


def net_balances(expenses, shares, settlements):
    """Net balance per member -> {member_id: cents}.

    Input shapes are plain tuples, so no model types are imported. The caller
    passes only rows with `deleted_at IS NULL`:

        expenses:    [(payer_id, amount_cents), ...]
        shares:      [(member_id, amount_cents), ...]
        settlements: [(from_member_id, to_member_id, amount_cents), ...]

    balance = paid - owed + sent - received. A positive balance means the group
    owes you; negative means you owe the group. Paying someone moves your
    balance up toward zero and theirs down.

    The result contains only members that appear in the inputs — callers wanting
    a row per member should `.get(member_id, 0)`. Given consistent input (every
    expense's shares sum to its amount) the balances sum to exactly zero.
    """
    balances = {}
    for payer_id, amount_cents in expenses:
        balances[payer_id] = balances.get(payer_id, 0) + amount_cents
    for member_id, amount_cents in shares:
        balances[member_id] = balances.get(member_id, 0) - amount_cents
    for from_member_id, to_member_id, amount_cents in settlements:
        balances[from_member_id] = balances.get(from_member_id, 0) + amount_cents
        balances[to_member_id] = balances.get(to_member_id, 0) - amount_cents
    return balances


def simplify(balances):
    """Suggested transfers -> [(debtor_id, creditor_id, cents), ...].

    Greedy: repeatedly pay the largest debtor's debt to the largest creditor.
    Each transfer zeroes at least one of the two, so with d debtors and c
    creditors there are at most d + c - 1 <= n - 1 transfers. Members with a
    zero balance produce no transfers.

    Tie-break: both heaps are keyed on (signed balance, member_id), so when two
    members owe — or are owed — the same amount, the lower member id is picked
    first. Member ids are unique, so the ordering is total and the output is
    identical on every call regardless of dict iteration order. Without this the
    suggested payee flickers between reloads and reads as a bug.

    Simplification never changes anyone's net balance, only the payment paths.
    Expects balances summing to zero; a non-zero sum leaves the residue unpaid.
    """
    # A debtor's balance is already negative, so a min-heap on it pops the
    # largest debt first; creditors are negated to get the same ordering.
    debtors = [(balance, member_id) for member_id, balance in balances.items() if balance < 0]
    creditors = [(-balance, member_id) for member_id, balance in balances.items() if balance > 0]
    heapq.heapify(debtors)
    heapq.heapify(creditors)

    transfers = []
    while debtors and creditors:
        owed, debtor_id = heapq.heappop(debtors)
        due, creditor_id = heapq.heappop(creditors)
        amount = min(-owed, -due)
        transfers.append((debtor_id, creditor_id, amount))
        if -owed > amount:
            heapq.heappush(debtors, (owed + amount, debtor_id))
        if -due > amount:
            heapq.heappush(creditors, (due + amount, creditor_id))
    return transfers
