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
NEXT_BEST_DEPTH = 2  # best and runner-up per position, so a candidate is compared with the best OTHER player

# Sort key mirroring draft_tracker.best_available's ranking (overall_rank,
# with its same quirky flex_rank fallback) — used for our own stand-in pick
# and for "best remaining at this pos" bookkeeping. Precomputed once per
# rollout rather than calling best_available() per simulated pick.
def _rank_key(p: dict) -> float:
    return p.get("overall_rank") or 200 + (p.get("flex_rank") or 999)


def _pos_rank_key(p: dict) -> float:
    return p.get("overall_rank") or p.get("flex_rank") or p.get("pos_rank") or 999


def _future_picks(state: dict, horizon_picks: int) -> tuple[list[int], int]:
    """Our next `horizon_picks` picks to simulate to, and the pick the bots start at.

    On the clock, the current pick is ours to make and is not simulated: the
    horizon starts at the FOLLOWING pick ("if I pass on him now, is he there
    next round"). Between turns it starts at our upcoming pick.
    """
    teams, slot, rounds = state["teams"], state["slot"], state["rounds"]
    pick_no = len(state["picks"]) + 1
    total = teams * rounds
    on_clock = pick_no <= total and dt.snake_team_for_pick(pick_no, teams) == slot
    upcoming = [p for p in dt.my_pick_numbers(slot, teams, rounds) if p >= pick_no]
    future = upcoming[1:] if on_clock else upcoming
    return future[:horizon_picks], (pick_no + 1 if on_clock else pick_no)


def _standin_pick(standin_pool: list[dict], gone: set, counts: dict) -> dict | None:
    """Our own stand-in pick inside a simulation.

    Best available by rank that doesn't stack a 2nd QB/TE (K/DST are excluded
    from the pool already).
    """
    for p in standin_pool:
        if dt.norm_name(p["name"]) in gone:
            continue
        if p["pos"] in ("QB", "TE") and counts.get(p["pos"], 0) >= 1:
            continue
        return p
    return None


def rollout(state: dict, board: dict, n: int = 200, seed: int | None = None,
            pos_bias_by_slot: dict[int, dict[str, float]] | None = None,
            horizon_picks: int = 2) -> dict:
    """Simulate the room to each of our next `horizon_picks` picks, n times.

    Returns survival odds and the likely best player per position at EACH of
    those picks (`survival_at` / `next_best_at`, keyed by pick number), plus
    the two-pick fields older callers use: `survival` (first future pick),
    `survival_after` (second), `next_best`, `next_mine`, `after`.
    """
    t0 = time.time()
    teams, slot, rounds = state["teams"], state["slot"], state["rounds"]
    total_slots = teams * rounds
    future, bot_start = _future_picks(state, horizon_picks)
    next_mine = future[0] if future else None
    after = future[1] if len(future) > 1 else None

    result = {
        "n": n,
        "next_mine": next_mine,
        "after": after,
        "my_future": future,
        "survival": {},
        "survival_after": {},
        "next_best": {},
        "survival_at": {},
        "next_best_at": {},
        "elapsed_ms": 0.0,
    }
    avail_sorted = sorted(ds.available(state, board), key=ds.market_rank) if future else []
    if not avail_sorted:
        result["elapsed_ms"] = (time.time() - t0) * 1000
        return result

    standin_pool = sorted(
        (p for p in avail_sorted if p["pos"] not in ("K", "DST")
         and (p.get("overall_rank") or p.get("flex_rank"))),
        key=_rank_key,
    )
    by_pos = {
        pos: sorted((p for p in avail_sorted if p["pos"] == pos), key=_pos_rank_key)
        for pos in SKILL_POS
    }
    keys = [dt.norm_name(p["name"]) for p in avail_sorted]
    base_counts = {t: dt.roster_of(state, t, board) for t in range(1, teams + 1)}
    hits = {m: Counter() for m in future}
    acc = {
        m: {
            pos: {"proj": 0.0, "rank": 0.0, "n": 0, "names": Counter(), "runs": []}
            for pos in SKILL_POS
        }
        for m in future
    }
    rng = random.Random(seed)

    def advance_bots(counts: dict, gone: set, start: int, end: int) -> None:
        for cur in range(start, min(end, total_slots + 1)):
            team_slot = dt.snake_team_for_pick(cur, teams)
            rounds_left = rounds - (cur - 1) // teams
            bias = (pos_bias_by_slot or {}).get(team_slot)
            p = ds.ai_pick_fast(counts[team_slot], avail_sorted, gone, rounds_left, rng, bias=bias)
            gone.add(dt.norm_name(p["name"]))
            counts[team_slot][p["pos"]] = counts[team_slot].get(p["pos"], 0) + 1

    def record(m: int, gone: set) -> None:
        h = hits[m]
        for k in keys:
            if k not in gone:
                h[k] += 1
        for pos in SKILL_POS:
            top2 = []
            for p in by_pos[pos]:
                if dt.norm_name(p["name"]) not in gone:
                    top2.append(p)
                    if len(top2) == NEXT_BEST_DEPTH:
                        break
            if not top2:
                continue
            best = top2[0]
            second = top2[1] if len(top2) > 1 else None
            a = acc[m][pos]
            a["n"] += 1
            a["proj"] += best.get("proj_points") or 0.0
            a["rank"] += _pos_rank_key(best)
            a["names"][best["name"]] += 1
            a["runs"].append(
                (
                    best["name"],
                    best.get("proj_points") or 0.0,
                    second["name"] if second else None,
                    (second.get("proj_points") or 0.0) if second else 0.0,
                )
            )

    for _ in range(n):
        counts = {t: dict(c) for t, c in base_counts.items()}
        gone: set = set()
        cur = bot_start
        for m in future:
            advance_bots(counts, gone, cur, m)
            record(m, gone)
            standin = _standin_pick(standin_pool, gone, counts[slot])
            if standin is not None:
                gone.add(dt.norm_name(standin["name"]))
                counts[slot][standin["pos"]] = counts[slot].get(standin["pos"], 0) + 1
            cur = m + 1

    for m in future:
        result["survival_at"][m] = {k: hits[m][k] / n for k in keys}
        nb = {}
        for pos, a in acc[m].items():
            if a["n"]:
                nb[pos] = {
                    "mean_proj": a["proj"] / a["n"],
                    "mean_rank": a["rank"] / a["n"],
                    "p50_name": a["names"].most_common(1)[0][0],
                    "runs": a["runs"],
                }
        result["next_best_at"][m] = nb
    result["survival"] = result["survival_at"].get(next_mine, {})
    result["survival_after"] = result["survival_at"].get(after, {}) if after else {}
    result["next_best"] = result["next_best_at"].get(next_mine, {})
    result["elapsed_ms"] = (time.time() - t0) * 1000
    return result


def _excluding(candidate: dict, nb: dict) -> tuple[float, Counter] | None:
    """Mean projection and name counts of the best OTHER player at the candidate's position.

    Measured at our next pick and excluding the candidate himself; without
    this, a player who usually survives is compared with... himself.
    """
    runs = nb.get("runs") or []
    if not runs:
        return None
    me = candidate.get("name")
    tot, names = 0.0, Counter()
    for best, best_proj, second, second_proj in runs:
        if best == me:
            tot += second_proj
            if second:
                names[second] += 1
        else:
            tot += best_proj
            names[best] += 1
    return tot / len(runs), names


def vona(candidate: dict, result: dict) -> float | None:
    """Value Over Next Available, in projected points.

    The candidate's projection minus the mean projection of the best OTHER
    player likely available at his position at our next pick. Positive means
    taking him now beats waiting. None without a projection or next_best data.
    """
    proj = candidate.get("proj_points")
    nb = result.get("next_best", {}).get(candidate.get("pos"))
    if proj is None or not nb:
        return None
    ex = _excluding(candidate, nb)
    base = ex[0] if ex else nb["mean_proj"]
    return proj - base


def wait_for(candidate: dict, result: dict) -> dict | None:
    """Who you'd most likely get at this position next turn if the candidate is gone.

    Returns {"name", "mean_proj"} or None without next_best data.
    """
    nb = result.get("next_best", {}).get(candidate.get("pos"))
    if not nb:
        return None
    ex = _excluding(candidate, nb)
    if not ex or not ex[1]:
        return {"name": nb["p50_name"], "mean_proj": nb["mean_proj"]}
    return {"name": ex[1].most_common(1)[0][0], "mean_proj": ex[0]}


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
