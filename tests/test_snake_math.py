import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from draft_tracker import snake_team_for_pick, my_pick_numbers

TEAMS = 16
ROUNDS = 15


def test_slot_1_pick_sequence():
    assert my_pick_numbers(1, TEAMS, ROUNDS)[:4] == [1, 32, 33, 64]


def test_slot_16_pick_sequence():
    assert my_pick_numbers(16, TEAMS, ROUNDS)[:4] == [16, 17, 48, 49]


def test_my_pick_numbers_matches_snake_team_for_pick():
    for slot in range(1, TEAMS + 1):
        for pick_no in my_pick_numbers(slot, TEAMS, ROUNDS):
            assert snake_team_for_pick(pick_no, TEAMS) == slot


def test_my_pick_numbers_length_is_one_per_round():
    for slot in range(1, TEAMS + 1):
        assert len(my_pick_numbers(slot, TEAMS, ROUNDS)) == ROUNDS


def test_every_pick_maps_to_exactly_one_slot():
    total_picks = TEAMS * ROUNDS
    counts = {slot: 0 for slot in range(1, TEAMS + 1)}
    for pick_no in range(1, total_picks + 1):
        slot = snake_team_for_pick(pick_no, TEAMS)
        assert 1 <= slot <= TEAMS
        counts[slot] += 1
    assert all(c == ROUNDS for c in counts.values())


def test_snake_direction_alternates_by_round():
    # Round 1 (even index 0) ascends 1..16; round 2 descends 16..1.
    round1 = [snake_team_for_pick(p, TEAMS) for p in range(1, TEAMS + 1)]
    round2 = [snake_team_for_pick(p, TEAMS) for p in range(TEAMS + 1, 2 * TEAMS + 1)]
    assert round1 == list(range(1, TEAMS + 1))
    assert round2 == list(range(TEAMS, 0, -1))


def test_every_slot_gets_all_its_picks_across_full_draft():
    for slot in range(1, TEAMS + 1):
        picks = my_pick_numbers(slot, TEAMS, ROUNDS)
        assert len(picks) == ROUNDS
        assert len(set(picks)) == ROUNDS  # no duplicate pick numbers
        assert all(1 <= p <= TEAMS * ROUNDS for p in picks)
