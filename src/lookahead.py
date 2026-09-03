"""Monte Carlo lookahead: roll the bot model (`draft_sim`) forward many times
to estimate, for every available player, the odds they survive to our next
pick(s) — and what the best replacement at each position is likely to look
like when we get there.

`draft_tracker.survival_odds` is an analytic per-pick guess. This module
instead simulates the intervening picks explicitly (many times, with sampling
noise) and counts, which is the only way to answer "what will be left at my
next pick" rather than "roughly how likely is this one player to still be
there".

CLI:
  python src/lookahead.py [--n 200] [--seed 1]
"""

import argparse
import random
import time
from collections import Counter

import draft_sim as ds
import draft_tracker as dt

SKILL_POS = ("QB", "RB", "WR", "TE")

# Sort key mirroring draft_tracker.best_available's ranking (overall_rank,
# with its same quirky flex_rank fallback) — used for our own stand-in pick
# and for "best remaining at this pos" bookkeeping. Precomputed once per
# rollout rather than calling best_available() per simulated pick.
def _rank_key(p: dict) -> float:
    return p.get("overall_rank") or 200 + (p.get("flex_rank") or 999)


def _pos_rank_key(p: dict) -> float:
    return p.get("overall_rank") or p.get("flex_rank") or p.get("pos_rank") or 999


def rollout(state: dict, board: dict, n: int = 200, seed: int | None = None,
            pos_bias_by_slot: dict[int, dict[str, float]] | None = None,
            horizon_picks: int = 2) -> dict:
    t0 = time.time()
    teams, slot, rounds = state["teams"], state["slot"], state["rounds"]
    pick_no = len(state["picks"]) + 1
    total_slots = teams * rounds
    on_clock = dt.snake_team_for_pick(pick_no, teams) == slot if pick_no <= total_slots else False

    mine = dt.my_pick_numbers(slot, teams, rounds)
    upcoming = [p for p in mine if p >= pick_no]

    if on_clock:
        # "If I pass on him now, is he there next round" — our current pick
        # (pick_no) is not simulated at all; next_mine is the FOLLOWING one.
        next_mine = upcoming[1] if len(upcoming) > 1 else None
        bot_start = pick_no + 1
    else:
        next_mine = upcoming[0] if upcoming else None
        bot_start = pick_no

    after = None
    if next_mine is not None and horizon_picks >= 2:
        idx = mine.index(next_mine)
        after = mine[idx + 1] if idx + 1 < len(mine) else None

    empty_result = {
        "n": n, "next_mine": next_mine, "after": after,
        "survival": {}, "survival_after": {}, "next_best": {},
        "elapsed_ms": (time.time() - t0) * 1000,
    }
    if next_mine is None:
        return empty_result

    # Built ONCE for the whole rollout (all n iterations share it): the
    # deduped, market-rank-sorted available list. Per-iteration state only
    # needs a `gone` set of names taken *during that simulation*.
    avail_sorted = sorted(ds.available(state, board), key=ds.market_rank)
    if not avail_sorted:
        return empty_result

    # Precomputed once: overall-rank order for the stand-in pick, and
    # per-position overall-rank order for next_best lookups.
    standin_pool = sorted(
        (p for p in avail_sorted if p["pos"] not in ("K", "DST")
         and (p.get("overall_rank") or p.get("flex_rank"))),
        key=_rank_key,
    )
    by_pos = {
        pos: sorted((p for p in avail_sorted if p["pos"] == pos), key=_pos_rank_key)
        for pos in SKILL_POS
    }

    base_counts = {t: dt.roster_of(state, t, board) for t in range(1, teams + 1)}

    survival_hits: Counter = Counter()
    survival_after_hits: Counter = Counter()
    next_best_acc = {pos: {"proj": 0.0, "rank": 0.0, "n": 0, "names": Counter()}
                     for pos in SKILL_POS}

    rng = random.Random(seed)

    def advance_bots(counts: dict, gone: set, start: int, end: int) -> None:
        """Simulate bot picks for pick numbers [start, end) in place."""
        for cur in range(start, end):
            if cur > total_slots:
                break
            team_slot = dt.snake_team_for_pick(cur, teams)
            rounds_left = rounds - (cur - 1) // teams
            bias = (pos_bias_by_slot or {}).get(team_slot)
            p = ds.ai_pick_fast(counts[team_slot], avail_sorted, gone, rounds_left, rng, bias=bias)
            gone.add(dt.norm_name(p["name"]))
            counts[team_slot][p["pos"]] = counts[team_slot].get(p["pos"], 0) + 1

    for _ in range(n):
        counts = {t: dict(c) for t, c in base_counts.items()}
        gone: set = set()

        advance_bots(counts, gone, bot_start, next_mine)

        for p in avail_sorted:
            if dt.norm_name(p["name"]) not in gone:
                survival_hits[dt.norm_name(p["name"])] += 1

        for pos in SKILL_POS:
            best = next((p for p in by_pos[pos] if dt.norm_name(p["name"]) not in gone), None)
            if best is not None:
                acc = next_best_acc[pos]
                acc["n"] += 1
                acc["proj"] += best.get("proj_points") or 0.0
                acc["rank"] += _pos_rank_key(best)
                acc["names"][best["name"]] += 1

        if after is not None:
            standin = next((p for p in standin_pool if dt.norm_name(p["name"]) not in gone), None)
            if standin is not None:
                gone.add(dt.norm_name(standin["name"]))
                counts[slot][standin["pos"]] = counts[slot].get(standin["pos"], 0) + 1

            advance_bots(counts, gone, next_mine + 1, after)

            for p in avail_sorted:
                if dt.norm_name(p["name"]) not in gone:
                    survival_after_hits[dt.norm_name(p["name"])] += 1

    survival = {dt.norm_name(p["name"]): survival_hits[dt.norm_name(p["name"])] / n
                for p in avail_sorted}
    survival_after = ({dt.norm_name(p["name"]): survival_after_hits[dt.norm_name(p["name"])] / n
                        for p in avail_sorted} if after is not None else {})

    next_best = {}
    for pos, acc in next_best_acc.items():
        if acc["n"]:
            p50_name = acc["names"].most_common(1)[0][0]
            next_best[pos] = {
                "mean_proj": acc["proj"] / acc["n"],
                "mean_rank": acc["rank"] / acc["n"],
                "p50_name": p50_name,
            }

    return {
        "n": n, "next_mine": next_mine, "after": after,
        "survival": survival, "survival_after": survival_after,
        "next_best": next_best,
        "elapsed_ms": (time.time() - t0) * 1000,
    }


def vona(candidate: dict, result: dict) -> float | None:
    """Value Over Next Available: candidate['proj_points'] minus the mean
    projection of the best player likely available at his position at our
    next pick. Positive means taking him now beats waiting. None if we lack
    a projection for the candidate or no next_best data for his position."""
    proj = candidate.get("proj_points")
    nb = result.get("next_best", {}).get(candidate.get("pos"))
    if proj is None or not nb:
        return None
    return proj - nb["mean_proj"]


def _cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    board = dt.load_board()
    state = dt.load_state()
    result = rollout(state, board, n=args.n, seed=args.seed)

    pick_no = len(state["picks"]) + 1
    print(f"Rollout: n={result['n']}  pick_no={pick_no}  "
          f"next_mine={result['next_mine']}  after={result['after']}  "
          f"elapsed={result['elapsed_ms']:.1f}ms")

    avail = dt.best_available(state, board, limit=15)
    print(f"\n{'player':26s} {'pos':5s} {'rank':5s} {'surv@next':10s} {'surv@after':10s}")
    for p in avail:
        nm = dt.norm_name(p["name"])
        surv = result["survival"].get(nm)
        surv_after = result["survival_after"].get(nm)
        surv_s = f"{surv*100:5.1f}%" if surv is not None else "-"
        surv_after_s = f"{surv_after*100:5.1f}%" if surv_after is not None else "-"
        print(f"{p['name']:26s} {p['pos']:5s} {str(p.get('overall_rank','-')):5s} "
              f"{surv_s:10s} {surv_after_s:10s}")

    print(f"\n{'pos':5s} {'mean_proj':10s} {'mean_rank':10s} p50_name")
    for pos, nb in result["next_best"].items():
        print(f"{pos:5s} {nb['mean_proj']:10.1f} {nb['mean_rank']:10.1f} {nb['p50_name']}")


if __name__ == "__main__":
    _cli()
