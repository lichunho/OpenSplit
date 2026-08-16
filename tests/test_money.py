"""Tests for the pure money core. All amounts are integer cents — no floats."""
import pytest

from app.money import net_balances, simplify, split_equal, validate_exact

# The verification scenario from the plan: Chris, Alex and Sam on a trip.
CHRIS, ALEX, SAM = 1, 2, 3

DINNER_CENTS = 10000  # $100, paid by Chris, split equally three ways
TAXI_CENTS = 4500  # $45, paid by Alex, exact 20 / 15 / 10
TAXI_SHARES = {CHRIS: 2000, ALEX: 1500, SAM: 1000}


def trip_balances(settlements=()):
    """Net balances for the dinner + taxi trip, plus any settlements."""
    dinner_shares = split_equal(DINNER_CENTS, [CHRIS, ALEX, SAM])
    validate_exact(TAXI_CENTS, TAXI_SHARES)
    expenses = [(CHRIS, DINNER_CENTS), (ALEX, TAXI_CENTS)]
    shares = list(dinner_shares.items()) + list(TAXI_SHARES.items())
    return net_balances(expenses, shares, settlements)


# --- split_equal ------------------------------------------------------------


def test_ten_dollars_three_ways():
    shares = split_equal(1000, [CHRIS, ALEX, SAM])
    assert sum(shares.values()) == 1000
    assert sorted(shares.values()) == [333, 333, 334]
    assert all(isinstance(amount, int) for amount in shares.values())


def test_hundred_dollar_dinner_three_ways():
    shares = split_equal(DINNER_CENTS, [CHRIS, ALEX, SAM])
    assert sum(shares.values()) == 10000
    assert sorted(shares.values()) == [3333, 3333, 3334]


def test_single_member_takes_the_whole_amount():
    assert split_equal(1000, [7]) == {7: 1000}
    assert split_equal(1, [7]) == {7: 1}


def test_zero_total_splits_to_zero():
    assert split_equal(0, [CHRIS, ALEX]) == {CHRIS: 0, ALEX: 0}


def test_shares_always_sum_to_the_total():
    for total_cents in range(0, 200):
        for member_ids in ([1], [1, 2], [1, 2, 3], [4, 7, 11, 12]):
            shares = split_equal(total_cents, member_ids)
            assert sum(shares.values()) == total_cents
            assert set(shares) == set(member_ids)


def test_remainder_rotates_instead_of_always_taxing_the_lowest_id():
    # $10.00 over three: remainder 1 starts at index 1000 % 3 == 1, so the
    # second member by id carries the extra cent, not the first.
    assert split_equal(1000, [CHRIS, ALEX, SAM]) == {CHRIS: 333, ALEX: 334, SAM: 333}
    # $10.01: remainder 2 starts at index 1001 % 3 == 2 and wraps onto index 0.
    assert split_equal(1001, [CHRIS, ALEX, SAM]) == {CHRIS: 334, ALEX: 333, SAM: 334}


def test_every_member_carries_the_extra_cent_sometimes():
    # The point of the rotation: no member is permanently taxed the extra cent.
    for member_ids in ([1, 2, 3], [1, 2, 3, 4]):
        n = len(member_ids)
        taxed = set()
        for total_cents in range(40):
            shares = split_equal(total_cents, member_ids)
            taxed |= {
                member_id
                for member_id, amount in shares.items()
                if amount > total_cents // n
            }
        assert taxed == set(member_ids)


def test_split_equal_is_order_independent_and_repeatable():
    assert split_equal(1000, [SAM, CHRIS, ALEX]) == split_equal(1000, [CHRIS, ALEX, SAM])
    assert split_equal(1000, [CHRIS, ALEX, SAM]) == split_equal(1000, [CHRIS, ALEX, SAM])


def test_split_equal_rejects_bad_input():
    with pytest.raises(ValueError):
        split_equal(1000, [])
    with pytest.raises(ValueError):
        split_equal(1000, [CHRIS, CHRIS, ALEX])
    with pytest.raises(ValueError):
        split_equal(-1, [CHRIS, ALEX])


# --- validate_exact ---------------------------------------------------------


def test_exact_split_that_sums_to_the_total_is_accepted():
    validate_exact(TAXI_CENTS, TAXI_SHARES)  # 20 / 15 / 10 against $45


def test_exact_split_that_misses_the_total_is_rejected():
    with pytest.raises(ValueError):
        validate_exact(TAXI_CENTS, {CHRIS: 2000, ALEX: 1500, SAM: 900})  # 20 / 15 / 9
    with pytest.raises(ValueError):
        validate_exact(TAXI_CENTS, {CHRIS: 2000, ALEX: 1500, SAM: 1100})  # a cent over


def test_zero_share_is_allowed():
    # Sam skipped the appetizer.
    validate_exact(TAXI_CENTS, {CHRIS: 2000, ALEX: 2500, SAM: 0})


def test_negative_share_is_rejected_even_when_the_sum_matches():
    with pytest.raises(ValueError):
        validate_exact(TAXI_CENTS, {CHRIS: 5000, ALEX: -500})


def test_empty_participant_set_is_rejected():
    with pytest.raises(ValueError):
        validate_exact(0, {})


# --- net_balances -----------------------------------------------------------


def test_trip_balances_and_sum_to_zero():
    balances = trip_balances()
    assert balances == {CHRIS: 4667, ALEX: -334, SAM: -4333}
    assert sum(balances.values()) == 0
    assert all(isinstance(amount, int) for amount in balances.values())


def test_everyone_already_square():
    # Each of two members paid $10 and each owes half of both bills.
    balances = net_balances(
        expenses=[(CHRIS, 1000), (ALEX, 1000)],
        shares=[(CHRIS, 500), (ALEX, 500), (CHRIS, 500), (ALEX, 500)],
        settlements=[],
    )
    assert balances == {CHRIS: 0, ALEX: 0}
    assert simplify(balances) == []


def test_settlement_exactly_zeroes_a_pair():
    # Sam pays Chris the suggested amount and settles up completely.
    suggested = [t for t in simplify(trip_balances()) if t[0] == SAM]
    assert suggested == [(SAM, CHRIS, 4333)]

    balances = trip_balances(settlements=[(SAM, CHRIS, 4333)])
    assert balances[SAM] == 0
    assert balances == {CHRIS: 334, ALEX: -334, SAM: 0}
    assert sum(balances.values()) == 0
    # Settled members drop out of the suggestions entirely.
    assert simplify(balances) == [(ALEX, CHRIS, 334)]


def test_overpayment_flips_the_sign():
    # Alex owes $3.34 but hands over $5.00 — the balance flips in Alex's favour.
    balances = trip_balances(settlements=[(SAM, CHRIS, 4333), (ALEX, CHRIS, 500)])
    assert balances == {CHRIS: -166, ALEX: 166, SAM: 0}
    assert sum(balances.values()) == 0
    # Chris now owes Alex the difference.
    assert simplify(balances) == [(CHRIS, ALEX, 166)]


def test_settlement_moves_the_payers_balance_up_and_the_payees_down():
    balances = net_balances([], [], [(ALEX, CHRIS, 2500)])
    assert balances == {ALEX: 2500, CHRIS: -2500}


def test_balances_only_cover_members_who_appear():
    balances = net_balances([(CHRIS, 1000)], [(CHRIS, 1000)], [])
    assert balances == {CHRIS: 0}
    assert balances.get(SAM, 0) == 0


# --- simplify ---------------------------------------------------------------


def test_simplify_settles_every_balance():
    balances = {1: -100, 2: -200, 3: -300, 4: 200, 5: 400}
    transfers = simplify(balances)

    settled = dict(balances)
    for debtor_id, creditor_id, amount in transfers:
        assert amount > 0
        settled[debtor_id] += amount
        settled[creditor_id] -= amount
    assert set(settled.values()) == {0}


def test_simplify_uses_at_most_n_minus_one_transfers():
    assert len(simplify(trip_balances())) <= 3 - 1
    assert len(simplify({1: -100, 2: -200, 3: -300, 4: 200, 5: 400})) <= 5 - 1


def test_simplify_is_identical_across_repeated_calls():
    # The dashboard reloads must not flicker between suggestions. Insertion
    # order differs between the two dicts; the output must not.
    forward = {CHRIS: 4667, ALEX: -334, SAM: -4333}
    reverse = {SAM: -4333, ALEX: -334, CHRIS: 4667}
    assert simplify(forward) == simplify(forward) == simplify(reverse)
    assert simplify(forward) == [(SAM, CHRIS, 4333), (ALEX, CHRIS, 334)]


def test_simplify_breaks_debtor_ties_by_lowest_member_id():
    transfers = simplify({1: -500, 2: -500, 3: 1000})
    assert transfers == [(1, 3, 500), (2, 3, 500)]


def test_simplify_breaks_creditor_ties_by_lowest_member_id():
    transfers = simplify({1: -1000, 2: 500, 3: 500})
    assert transfers == [(1, 2, 500), (1, 3, 500)]


def test_simplify_ignores_zero_balances():
    assert simplify({}) == []
    assert simplify({CHRIS: 0, ALEX: 0, SAM: 0}) == []
    assert simplify({CHRIS: -500, ALEX: 0, SAM: 500}) == [(CHRIS, SAM, 500)]
