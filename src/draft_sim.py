"""AI opponents for local mock drafts.

Each autopick team drafts from a market-rank distribution (UDK rank blended
with 16-team-scaled ADP) weighted by its positional needs, with sampling noise
so every mock unfolds differently — runs and reaches emerge naturally.
"""

import random

from draft_tracker import (norm_name, roster_of, positional_need,
                           snake_team_for_pick)

# Hard roster caps for AI teams (16-team league, 15 rounds).
CAPS = {"QB": 2, "RB": 6, "WR": 7, "TE": 2, "DST": 1, "K": 1}
STARTER_ROUNDS_LEFT = {"DST": 2, "K": 1}  # force these when this many rounds remain


def market_rank(p: dict) -> float:
    """Where the ROOM has this player. The other 15 managers draft off ESPN's
    default board (its PPR rank orders the draft app and every autopick) and
    ESPN's ADP is the realised market, so when data/market.json has been
    merged into the board (draft_board.py) the bots use that — not our UDK
    valuation. Fallback: the old UDK-rank / UDK-ADP blend."""
    espn_adp, espn_order = p.get("espn_adp"), p.get("espn_rank")  # espn_rank = room order
    if espn_adp and espn_order:
        return 0.6 * espn_adp + 0.4 * espn_order
    if espn_order:
        return float(espn_order)
    adp = p.get("adp_overall")
    rank = p.get("overall_rank") or 250
    if adp:
        return 0.5 * rank + 0.5 * (adp * 4 / 3)  # scale 12-team ADP to 16 teams
    return rank * 1.05


def available(state: dict, board: dict) -> list[dict]:
    gone = {norm_name(p["name"]) for p in state["picks"]}
    seen = set()
    out = []
    for p in board.values():
        if id(p) in seen:
            continue  # D/ST alias keys point at the same player object
        seen.add(id(p))
        if norm_name(p["name"]) in gone:
            continue
        if p.get("overall_rank") or p.get("flex_rank") or p.get("pos_rank"):
            out.append(p)
    return out


def ai_pick_fast(counts: dict, avail_sorted: list[dict], gone: set[str],
                  rounds_left: int, rng: random.Random,
                  bias: dict[str, float] | None = None) -> dict:
    """Core pick logic, factored out of `ai_pick` so a rollout can call it in
    a tight loop without rebuilding/re-sorting the available list every pick.

    `avail_sorted` must already be deduped and sorted by `market_rank` (build
    it ONCE per rollout with `sorted(available(state, board), key=market_rank)`).
    `gone` holds the norm_names taken so far *within this simulation* (players
    already gone in the real `state` should simply be absent from
    `avail_sorted`, since it was built from `available()` at rollout start).
    `counts` is the on-the-clock team's position counts (caller-maintained
    incrementally instead of calling `roster_of` every pick).
    `bias` is an optional `{pos: multiplier}` applied to a candidate's weight
    in the main weighted pool (hook for learned tendencies).
    """
    # Late-draft necessities: fill missing K/DST when time runs short.
    for pos, when in STARTER_ROUNDS_LEFT.items():
        if rounds_left <= when and counts.get(pos, 0) == 0:
            pool = sorted((p for p in avail_sorted
                           if p["pos"] == pos and norm_name(p["name"]) not in gone),
                          key=lambda p: p.get("pos_rank") or 99)[:5]
            if pool:
                return rng.choice(pool[:3])

    # Candidate pool: best 14 by market rank the team is allowed to draft.
    # avail_sorted is already market-rank sorted, so a filtering walk over it
    # yields the same order as filtering-then-sorting a fresh list.
    pool = []
    for p in avail_sorted:
        if norm_name(p["name"]) in gone:
            continue
        if counts.get(p["pos"], 0) >= CAPS.get(p["pos"], 9):
            continue
        # AI teams don't take K/DST early. The league's own history has the
        # median first DST in round ~10.5 and first K ~11.6 (some as early as
        # round 5), so open the window with 6 rounds left and let market rank
        # (ESPN puts the top D/STs around pick 110-130) place them from there.
        if rounds_left > 6 and p["pos"] in ("K", "DST"):
            continue
        pool.append(p)
        if len(pool) >= 14:
            break
    if not pool:
        avail_now = [p for p in avail_sorted if norm_name(p["name"]) not in gone]
        return rng.choice(avail_now)

    weights = []
    base = market_rank(pool[0])
    for p in pool:
        w = 1.0 / (1.0 + (market_rank(p) - base) / 6.0) ** 2
        w *= 0.5 + positional_need(counts, p["pos"])
        if bias:
            w *= bias.get(p["pos"], 1.0)
        weights.append(w)
    return rng.choices(pool, weights=weights, k=1)[0]


def ai_pick(state: dict, board: dict, rng: random.Random) -> dict:
    """Thin wrapper kept for callers that don't need rollout-speed: rebuilds
    the available list and position counts fresh every call. Behaviour is
    identical to the pre-refactor implementation. See `ai_pick_fast` for the
    version a hot loop (e.g. src/lookahead.py) should call directly."""
    pick_no = len(state["picks"]) + 1
    teams, rounds = state["teams"], state["rounds"]
    team_slot = snake_team_for_pick(pick_no, teams)
    rounds_left = rounds - (pick_no - 1) // teams
    counts = roster_of(state, team_slot, board)
    avail_sorted = sorted(available(state, board), key=market_rank)
    return ai_pick_fast(counts, avail_sorted, set(), rounds_left, rng)


def sim_until_my_turn(state: dict, board: dict, rng: random.Random) -> list[dict]:
    """Advance AI picks until it's our slot's turn (or draft ends).
    Returns the picks made."""
    made = []
    total = state["teams"] * state["rounds"]
    while len(state["picks"]) < total:
        pick_no = len(state["picks"]) + 1
        team_slot = snake_team_for_pick(pick_no, state["teams"])
        if team_slot == state["slot"]:
            break
        p = ai_pick(state, board, rng)
        rec = {"pick": pick_no, "team": team_slot, "name": p["name"]}
        state["picks"].append(rec)
        made.append(rec)
    return made
