"""AI opponents for local mock drafts.

Each autopick team drafts from a market-rank distribution (UDK rank blended
with 16-team-scaled ADP) weighted by its positional needs, with sampling noise
so every mock unfolds differently — runs and reaches emerge naturally.
"""

import json
import random
from pathlib import Path

import league_history
from draft_tracker import norm_name, positional_need, roster_of, snake_team_for_pick

# Hard roster caps for AI teams (16-team league, 15 rounds).
CAPS = {"QB": 2, "RB": 6, "WR": 7, "TE": 2, "DST": 1, "K": 1}
LEAGUE_BIAS_PATH = Path(__file__).resolve().parent.parent / "data" / "league_bias.json"
_LEAGUE_BIAS: dict = {}
KDST_ROUNDS_WINDOW = 9  # bots consider K/DST only with this many rounds left (F3: first DST ~R10.5)
KDST_RAMP_BASE = 0.08  # K/DST weight = base * (rounds into the window)^2; ~1.0 = top market player
SECOND_QB_TE_ROUNDS = 9  # a 2nd QB/TE is a late-round luxury until this many rounds remain
SECOND_QB_TE_DAMP = 0.5


def league_bias() -> dict[str, float]:
    """Per-position demand multipliers learned from this league's past drafts.

    Written by `src/replay.py --calibrate`: how much more or less often the
    room takes each position than ESPN's national ADP implies. Empty until
    learned; call reset_league_bias() after rewriting the file.
    """
    if "bias" not in _LEAGUE_BIAS:
        try:
            _LEAGUE_BIAS["bias"] = json.loads(LEAGUE_BIAS_PATH.read_text()).get("bias", {})
        except (OSError, ValueError):
            _LEAGUE_BIAS["bias"] = {}
    return _LEAGUE_BIAS["bias"]


def reset_league_bias() -> None:
    """Forget the memoised multipliers so the next call re-reads the file."""
    _LEAGUE_BIAS.clear()


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
        if rounds_left > KDST_ROUNDS_WINDOW and p["pos"] in ("K", "DST"):
            continue
        pool.append(p)
        if len(pool) >= 14:
            break
    if not pool:
        avail_now = [p for p in avail_sorted if norm_name(p["name"]) not in gone]
        return rng.choice(avail_now)

    # K/DST sit past pick 130 on ESPN's board and would never enter the market window on
    # merit, yet the room fills them in rounds 10-13 (F3 history: first DST ~R10.5, first K ~R11.6).
    kdst_weights: dict[int, float] = {}
    if rounds_left <= KDST_ROUNDS_WINDOW:
        for pos in ("DST", "K"):
            if counts.get(pos, 0):
                continue
            best = next(
                (p for p in avail_sorted if p["pos"] == pos and norm_name(p["name"]) not in gone),
                None,
            )
            if best is not None and best not in pool:
                pool.append(best)
                kdst_weights[id(best)] = (
                    KDST_RAMP_BASE * (KDST_ROUNDS_WINDOW + 1 - rounds_left) ** 2
                )

    weights = []
    base = market_rank(pool[0])
    for p in pool:
        if id(p) in kdst_weights:
            weights.append(kdst_weights[id(p)])
            continue
        w = 1.0 / (1.0 + (market_rank(p) - base) / 6.0) ** 2
        w *= 0.5 + positional_need(counts, p["pos"])
        # F3 history 2022-25: 14-18 QBs gone by pick 121 but only 1-3 teams doubled up, so
        # without this damping the bots spread QB2s across rounds 6-9 while others sit on zero.
        if (
            p["pos"] in ("QB", "TE")
            and counts.get(p["pos"], 0) >= 1
            and rounds_left > SECOND_QB_TE_ROUNDS
        ):
            w *= SECOND_QB_TE_DAMP
        w *= league_bias().get(p["pos"], 1.0)
        if bias:
            w *= bias.get(p["pos"], 1.0)
        weights.append(w)
    return rng.choices(pool, weights=weights, k=1)[0]


def _bias_for(state: dict, team_slot: int, round_no: int) -> dict | None:
    """Learned tendency multipliers for the manager in `team_slot`.

    Only when the state carries `team_ids` (the live poller records them; mocks
    can borrow last year's order). None means a generic bot.
    """
    order = state.get("team_ids")
    if not order:
        return None
    return league_history.bias_for_slots(order, round_no).get(team_slot)


def ai_pick(state: dict, board: dict, rng: random.Random) -> dict:
    """Thin wrapper kept for callers that don't need rollout-speed: rebuilds
    the available list and position counts fresh every call. Behaviour is
    identical to the pre-refactor implementation. See `ai_pick_fast` for the
    version a hot loop (e.g. src/lookahead.py) should call directly. If the
    state names the managers (`team_ids`), the pick is biased by that
    manager's learned habits."""
    pick_no = len(state["picks"]) + 1
    teams, rounds = state["teams"], state["rounds"]
    team_slot = snake_team_for_pick(pick_no, teams)
    rounds_left = rounds - (pick_no - 1) // teams
    counts = roster_of(state, team_slot, board)
    avail_sorted = sorted(available(state, board), key=market_rank)
    bias = _bias_for(state, team_slot, (pick_no - 1) // teams + 1)
    return ai_pick_fast(counts, avail_sorted, set(), rounds_left, rng, bias=bias)


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
