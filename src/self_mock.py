"""Self-play mocks: the rec engine drafts our slot, draft_sim plays the other 15.

Usage:
  python src/self_mock.py --slot 7 --seed 1          # one mock, print grade
  python src/self_mock.py --batch "1,4,7,10,13,16" --seed 1   # one per slot
  python src/self_mock.py --batch "..." --seed 1 --fast        # lower rollout n
                                                                  for a faster batch

build_live() runs the Monte-Carlo lookahead once per call (~15 calls per mock);
--fast (or env SELF_MOCK_FAST=1) drops the rollout to draft_live.FAST_ROLLOUT_N
for the whole process, trading a little precision for a much faster batch.

Grades each roster on: projected starter points (and league rank), starter
quality at QB/TE, RB/WR depth, tags captured, and legality.
"""

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import espn_client  # noqa: F401
from draft_tracker import load_board, norm_name, snake_team_for_pick
import draft_live
from draft_live import build_live
import draft_sim
import league_history

BOARD = load_board()


ORDER_SEASON: int | None = None  # --order: bots borrow that season's managers
SHUFFLE_ORDER = False  # --shuffle: same managers, random slots (seeded)


def mock_state(slot: int, seed: int) -> dict:
    """A fresh mock state.

    With ORDER_SEASON the bots are the real managers (learned habits) sitting
    in that season's slots, or in random slots when SHUFFLE_ORDER is on.
    """
    state = {"teams": 16, "slot": slot, "rounds": 15, "picks": [], "mock": True}
    if ORDER_SEASON:
        order, labels = league_history.mock_order(ORDER_SEASON)
        if len(order) == state["teams"]:
            if SHUFFLE_ORDER:
                names = {tid: labels[i + 1] for i, tid in enumerate(order)}
                random.Random(seed * 7919).shuffle(order)
                labels = {i + 1: names[tid] for i, tid in enumerate(order)}
            state["team_ids"], state["team_labels"] = order, labels
    return state


def run_mock(slot: int, seed: int) -> dict:
    rng = random.Random(seed)
    # Pin the lookahead's own RNG too. draft_live's live default (ROLLOUT_SEED
    # = None) means fresh sampling noise every poll, which is right for a real
    # draft but would make this harness (and any --once/regression comparison
    # built on it) nondeterministic — every one of the ~15 build_live() calls
    # per mock would roll different noise even for the same --seed.
    draft_live.ROLLOUT_SEED = seed
    state = mock_state(slot, seed)
    total = 16 * 15
    while len(state["picks"]) < total:
        draft_sim.sim_until_my_turn(state, BOARD, rng)
        if len(state["picks"]) >= total:
            break
        pick_no = len(state["picks"]) + 1
        if snake_team_for_pick(pick_no, 16) != slot:
            break  # draft over for us
        recs = build_live(state, BOARD)["recs"]
        choice = recs[0]["name"]
        state["picks"].append({"pick": pick_no, "team": slot, "name": choice})
    return state


def starters_projection(state: dict, team: int) -> float:
    ps = [BOARD.get(norm_name(p["name"])) for p in state["picks"] if p["team"] == team]
    ps = [p for p in ps if p and p.get("proj_points")]
    by = {}
    for p in ps:
        by.setdefault(p["pos"], []).append(p["proj_points"])
    for v in by.values():
        v.sort(reverse=True)
    tot = sum(by.get("QB", [0])[:1]) + sum(by.get("RB", [0, 0])[:2]) + \
          sum(by.get("WR", [0, 0])[:2]) + sum(by.get("TE", [0])[:1])
    flex = sorted(by.get("RB", [])[2:] + by.get("WR", [])[2:] + by.get("TE", [])[1:], reverse=True)
    return tot + (flex[0] if flex else 0)


def grade(state: dict) -> dict:
    slot = state["slot"]
    mine = [p for p in state["picks"] if p["team"] == slot]
    boards = [BOARD.get(norm_name(p["name"]), {}) for p in mine]
    comp = Counter(b.get("pos", "?") for b in boards)
    proj = {t: starters_projection(state, t) for t in range(1, 17)}
    rank = sorted(proj, key=lambda t: -proj[t]).index(slot) + 1
    qbs = sorted((b for b in boards if b.get("pos") == "QB"),
                 key=lambda b: b.get("pos_rank") or 99)
    tes = sorted((b for b in boards if b.get("pos") == "TE"),
                 key=lambda b: b.get("pos_rank") or 99)
    tags = sum(1 for b in boards if b.get("my_tags") and "bust" not in b["my_tags"])
    busts = sum(1 for b in boards if "bust" in (b.get("my_tags") or []))
    value = sum((b["adp_overall"] * 4 / 3) - p["pick"]
                for p, b in zip(mine, boards) if b.get("adp_overall"))
    legal = all(comp.get(pos, 0) >= n for pos, n in
                [("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1), ("DST", 1), ("K", 1)])
    return {
        "slot": slot, "rank": rank, "proj": round(proj[slot], 1),
        "top_proj": round(max(proj.values()), 1),
        "comp": dict(comp), "legal": legal,
        "qb1_posrank": qbs[0].get("pos_rank") if qbs else None,
        "te1_posrank": tes[0].get("pos_rank") if tes else None,
        "tags": tags, "busts": busts, "value": round(value),
        "picks": [(p["pick"], p["name"]) for p in mine],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int)
    ap.add_argument("--batch", type=str)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--order",
        type=int,
        default=None,
        help="borrow this season's draft order so bots use the managers' learned habits",
    )
    ap.add_argument(
        "--shuffle",
        action="store_true",
        help="with --order: same managers, random slots (seeded by --seed)",
    )
    ap.add_argument("--fast", action="store_true",
                     help=f"lower the rollout to n={draft_live.FAST_ROLLOUT_N} "
                          "(build_live runs ~15x per mock; this keeps the batch harness fast)")
    args = ap.parse_args()
    global ORDER_SEASON, SHUFFLE_ORDER
    ORDER_SEASON, SHUFFLE_ORDER = args.order, args.shuffle
    if args.fast or os.environ.get("SELF_MOCK_FAST"):
        draft_live.QUEUE_ENABLED = False
        draft_live.ROLLOUT_N = draft_live.FAST_ROLLOUT_N
    slots = [int(s) for s in args.batch.split(",")] if args.batch else [args.slot]
    results = []
    for slot in slots:
        g = grade(run_mock(slot, args.seed * 100 + slot))
        results.append(g)
        if not args.json:
            print(f"slot {g['slot']:>2}: rank {g['rank']:>2}/16  proj {g['proj']:>6} (top {g['top_proj']})  "
                  f"QB{g['qb1_posrank'] or '-'} TE{g['te1_posrank'] or '-'}  "
                  f"tags {g['tags']} busts {g['busts']}  val {g['value']:+4d}  "
                  f"legal {'Y' if g['legal'] else 'N'}  {g['comp']}")
    if args.json:
        print(json.dumps(results))


if __name__ == "__main__":
    main()
