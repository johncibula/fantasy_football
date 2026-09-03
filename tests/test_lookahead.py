import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import random
import time

import pytest

import draft_tracker as dt
import lookahead as la

TEAMS = 16
ROUNDS = 15

BOARD = dt.load_board()


def synthetic_state(n_picks: int = 0, slot: int = 7, seed: int = 1) -> dict:
    """A state with `n_picks` picks already made by the bot model, for a
    16-team/15-round draft at our `slot`."""
    import draft_sim as ds
    rng = random.Random(seed)
    # slot 99 never comes on the clock, so we can let the bots draft freely
    # for exactly n_picks picks regardless of whose turn it "really" is.
    state = {"teams": TEAMS, "slot": 99, "rounds": ROUNDS, "picks": []}
    while len(state["picks"]) < n_picks:
        pick_no = len(state["picks"]) + 1
        team_slot = dt.snake_team_for_pick(pick_no, TEAMS)
        p = ds.ai_pick(state, BOARD, rng)
        state["picks"].append({"pick": pick_no, "team": team_slot, "name": p["name"]})
    state["slot"] = slot
    return state


def early_state() -> dict:
    """~23 picks in, our next pick 2 away, the one after 13 more away —
    matches the plan's 'pick ~20, 15 picks to ours' performance scenario."""
    return synthetic_state(n_picks=23, slot=7, seed=1)


def test_survival_probabilities_in_range_and_drafted_players_absent():
    state = early_state()
    result = la.rollout(state, BOARD, n=150, seed=1)
    gone = {dt.norm_name(p["name"]) for p in state["picks"]}
    assert result["survival"], "expected survival entries for an early-draft state"
    for name, p in result["survival"].items():
        assert 0.0 <= p <= 1.0
        assert name not in gone
    for name, p in result["survival_after"].items():
        assert 0.0 <= p <= 1.0
        assert name not in gone


def test_monotonic_sanity_top_player_survives_less_than_deep_player():
    state = synthetic_state(n_picks=0, slot=7)  # nothing drafted yet: pick 1 on the clock
    result = la.rollout(state, BOARD, n=200, seed=1)
    avail = dt.best_available(state, BOARD, limit=100)
    top = dt.norm_name(avail[0]["name"])
    deep = dt.norm_name(avail[59]["name"])
    assert result["survival"][top] < result["survival"][deep]


def test_on_the_clock_semantics_current_pick_not_treated_as_taken():
    # It's our turn right now (pick 1, slot 1). The player we could take now
    # must not be excluded from the survival window, and next_mine must be
    # our pick AFTER the current one (not the current pick itself).
    state = {"teams": TEAMS, "slot": 1, "rounds": ROUNDS, "picks": []}
    result = la.rollout(state, BOARD, n=100, seed=1)
    mine = dt.my_pick_numbers(1, TEAMS, ROUNDS)
    assert result["next_mine"] == mine[1]
    assert result["after"] == mine[2]
    avail = dt.best_available(state, BOARD, limit=5)
    top_name = dt.norm_name(avail[0]["name"])
    # The #1 overall player is what "we" would take at pick 1 in real life;
    # since pick 1 is not simulated at all, he must still appear as a
    # survival candidate (present in the dict, not force-excluded).
    assert top_name in result["survival"]


def test_next_best_has_entries_for_each_skill_position_with_positive_proj():
    state = early_state()
    result = la.rollout(state, BOARD, n=150, seed=1)
    for pos in ("QB", "RB", "WR", "TE"):
        assert pos in result["next_best"], f"missing next_best for {pos}"
        assert result["next_best"][pos]["mean_proj"] > 0
        assert result["next_best"][pos]["p50_name"]


def test_pos_bias_drops_survival_for_biased_position():
    state = early_state()
    unbiased = la.rollout(state, BOARD, n=200, seed=7)

    other_slots = [s for s in range(1, TEAMS + 1) if s != state["slot"]]
    bias = {s: {"QB": 50.0} for s in other_slots}
    biased = la.rollout(state, BOARD, n=200, seed=7, pos_bias_by_slot=bias)

    gone = {dt.norm_name(p["name"]) for p in state["picks"]}
    qb_names = [dt.norm_name(p["name"]) for p in BOARD.values()
                if p["pos"] == "QB" and dt.norm_name(p["name"]) not in gone]
    qb_names = list(dict.fromkeys(qb_names))  # dedupe aliasing

    mean_unbiased = sum(unbiased["survival"].get(n, 0.0) for n in qb_names) / len(qb_names)
    mean_biased = sum(biased["survival"].get(n, 0.0) for n in qb_names) / len(qb_names)
    assert mean_biased < mean_unbiased


def test_reproducible_with_same_seed():
    state = early_state()
    r1 = la.rollout(state, BOARD, n=150, seed=42)
    r2 = la.rollout(state, BOARD, n=150, seed=42)
    assert r1["survival"] == r2["survival"]
    assert r1["survival_after"] == r2["survival_after"]
    assert r1["next_best"] == r2["next_best"]
    assert r1["next_mine"] == r2["next_mine"]
    assert r1["after"] == r2["after"]


@pytest.mark.timeout_gate
def test_rollout_perf_gate_n200_under_1_5s():
    """Perf gate (see docs/plans/01-lookahead.md): rollout(n=200) from an
    early-draft state must finish well under the 1.5s absolute ceiling
    (target <= 1.0s)."""
    state = early_state()
    t0 = time.time()
    result = la.rollout(state, BOARD, n=200, seed=1)
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"rollout(n=200) took {elapsed:.3f}s, over the 1.5s ceiling"
    assert result["elapsed_ms"] < 1500


def test_vona_positive_when_candidate_beats_next_best():
    state = early_state()
    result = la.rollout(state, BOARD, n=150, seed=1)
    nb = result["next_best"].get("RB")
    assert nb is not None
    candidate = {"pos": "RB", "proj_points": nb["mean_proj"] + 50}
    v = la.vona(candidate, result)
    assert v is not None and v == pytest.approx(50, abs=1e-6)


def test_vona_none_without_projection():
    state = early_state()
    result = la.rollout(state, BOARD, n=50, seed=1)
    assert la.vona({"pos": "RB"}, result) is None
