"""The multi-pick queue: a plan for our next few picks built from the board's scoring."""

import pathlib
import random
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import draft_live as dl
import draft_queue as dq
import draft_sim as ds
import draft_tracker as dt

BOARD = dt.load_board()


def _live(state: dict) -> dict:
    dl.ROLLOUT_N, dl.ROLLOUT_SEED = 40, 1
    return dl.build_live(state, BOARD)


def test_queue_covers_next_picks_in_order_with_distinct_players() -> None:
    live = _live({"teams": 16, "slot": 7, "rounds": 15, "picks": []})
    q = live["queue"]
    assert 4 <= len(q) <= dq.QUEUE_LEN
    picks = [s["pick"] for s in q]
    assert picks == sorted(picks) and picks[0] == 7
    assert len({s["name"] for s in q}) == len(q)


def test_queue_first_slot_is_the_current_pick_when_on_the_clock() -> None:
    state = {"teams": 16, "slot": 7, "rounds": 15, "picks": []}
    rng = random.Random(1)
    for pick_no in range(1, 7):
        p = ds.ai_pick(state, BOARD, rng)
        state["picks"].append(
            {"pick": pick_no, "team": dt.snake_team_for_pick(pick_no, 16), "name": p["name"]}
        )
    live = _live(state)
    assert live["picks_until_ours"] == 0
    q = live["queue"]
    assert q[0]["pick"] == 7 and q[0]["surv_here"] == 100
    assert q[0]["name"] == live["recs"][0]["name"]
    assert q[1]["pick"] == 26


def test_queue_respects_roster_caps_and_survival_floor() -> None:
    live = _live({"teams": 16, "slot": 7, "rounds": 15, "picks": []})
    q = live["queue"]
    c = Counter(s["pos"] for s in q)
    assert (
        c.get("QB", 0) <= 2 and c.get("TE", 0) <= 2 and c.get("K", 0) <= 1 and c.get("DST", 0) <= 1
    )
    assert all(s["surv_here"] >= round(dq.MIN_SURVIVAL * 100) for s in q)
