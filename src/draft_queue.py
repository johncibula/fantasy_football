"""Plan the sequence of our next few picks, not just the next one.

The board answers "who now?". The queue answers "who now, who at my pick
after that, and who after that?" using the same scoring the board uses,
re-run for each future pick with the roster as it will stand by then and
the lookahead's survival odds at that pick. The rule of thumb it encodes is
the snake-draft one: take the player who will not last, plan the one who
will.

Queue slots are a plan, not a promise: every pick is re-planned from
scratch on each rebuild, so the queue reshuffles as the room drafts.
"""

from __future__ import annotations

from dataclasses import replace

from draft_tracker import my_pick_numbers, norm_name, positional_need

QUEUE_LEN = 6
MIN_SURVIVAL = 0.35  # a plan slot needs at least this chance the player is there
CAN_WAIT = 0.75  # label a slot "could wait" above this (the score already reflects it)
CANDIDATES = 24  # players scored per slot (the best still likely to be there)
ROSTER_CAPS = {"QB": 2, "TE": 2, "K": 1, "DST": 1}


def _ctx_for_pick(
    ctx: object, look: dict, my_counts: dict, pick: int, pick_after: int | None
) -> object:
    """A ScoreCtx for planning `pick`: roster as it will stand, survival to the pick after."""
    st = ctx.state
    teams = st["teams"]
    later = {
        "survival": look["survival_at"].get(pick_after, {}) if pick_after else {},
        "next_best": look["next_best_at"].get(pick_after, {}) if pick_after else {},
        "next_mine": pick_after,
        "n": look["n"],
    }
    remaining = [m for m in my_pick_numbers(st["slot"], teams, st["rounds"]) if m >= pick]
    return replace(
        ctx,
        my_counts=my_counts,
        pick_no=pick,
        next_mine=pick,
        after=pick_after or pick + 2 * teams,
        round_next=(pick - 1) // teams + 1,
        picks_remaining=len(remaining),
        surv_start=pick + 1,
        surv_end=pick_after or pick + 2 * teams,
        need_dst=my_counts.get("DST", 0) == 0,
        need_k=my_counts.get("K", 0) == 0,
        look=later,
        _surv_cache={},
    )


def _best_for_slot(
    pctx: object,
    candidates: list[dict],
    score_fn: object,
    *,
    counts: dict,
    taken: set[str],
    here: dict | None,
    pick: int,
    pick_after: int | None,
) -> dict | None:
    """The best plan for one slot: lowest score among players likely to be there."""
    best = None
    scored = 0
    for p in candidates:
        if scored >= CANDIDATES:
            break
        key = norm_name(p["name"])
        if key in taken or counts.get(p["pos"], 0) >= ROSTER_CAPS.get(p["pos"], 99):
            continue
        surv_here = 1.0 if here is None else here.get(key, 0.0)
        if surv_here < MIN_SURVIVAL:
            continue
        scored += 1
        score, why = score_fn(p, pctx)
        surv_next = pctx.survival(p)
        reasons = []
        if pick_after and surv_next >= CAN_WAIT:
            reasons.append(f"could wait ({surv_next:.0%} there at #{pick_after})")
        elif pick_after and surv_next < MIN_SURVIVAL:
            reasons.append(f"won't last ({surv_next:.0%} there at #{pick_after})")
        if positional_need(counts, p["pos"]) >= 1.0:
            reasons.append(f"fills {p['pos']} starter")
        if best is None or score < best["score"]:
            best = {
                "pick": pick,
                "round": (pick - 1) // pctx.state["teams"] + 1,
                "name": p["name"],
                "pos": p["pos"],
                "team": p.get("team"),
                "rank": p.get("overall_rank"),
                "score": round(score, 1),
                "proj": p.get("proj_points"),
                "surv_here": round(surv_here * 100),
                "surv_next": round(surv_next * 100),
                "reason": "; ".join(reasons) or "best value on the board",
                "why": why,
            }
    return best


def build_queue(
    ctx: object, look: dict, candidates: list[dict], score_fn: object, *, current_pick_is_ours: bool
) -> list[dict]:
    """Plan our next QUEUE_LEN picks.

    `ctx` is the board's ScoreCtx (draft_live.ScoreCtx), `look` the lookahead
    result with `survival_at` / `next_best_at`, `candidates` the best-available
    pool, `score_fn` draft_live.score_candidate. When we are on the clock the
    first slot is the current pick (everyone is available); the rest are the
    lookahead's future picks.
    """
    future = list(look.get("my_future") or [])
    slots = (([ctx.pick_no] if current_pick_is_ours else []) + future)[:QUEUE_LEN]
    counts = dict(ctx.my_counts)
    taken: set[str] = set()
    plan: list[dict] = []
    for i, pick in enumerate(slots):
        pick_after = slots[i + 1] if i + 1 < len(slots) else None
        here = look["survival_at"].get(pick)
        pctx = _ctx_for_pick(ctx, look, counts, pick, pick_after)
        best = _best_for_slot(
            pctx,
            candidates,
            score_fn,
            counts=counts,
            taken=taken,
            here=here,
            pick=pick,
            pick_after=pick_after,
        )
        if best is None:
            break
        plan.append(best)
        taken.add(norm_name(best["name"]))
        counts[best["pos"]] = counts.get(best["pos"], 0) + 1
    return plan
