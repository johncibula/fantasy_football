"""Live draft state for the F³ snake draft (and ESPN mock drafts).

Claude feeds picks in as it reads them from the ESPN draft room; the tracker
maintains every team's roster, computes best-available from the UDK board, and
estimates each candidate's odds of surviving to our next pick based on what
the specific teams drafting in between actually need.

State persists to data/draft_state.json so a crashed session can resume.

CLI:
  python src/draft_tracker.py start --teams 16 --slot 7 [--rounds 15]
  python src/draft_tracker.py pick "Player Name" [--team 3]   # team inferred from pick order if omitted
  python src/draft_tracker.py undo
  python src/draft_tracker.py status
  python src/draft_tracker.py advise
"""

import argparse
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA / "draft_state.json"

# Typical 16-team build the survival model assumes opponents roughly follow.
# (QB/TE late-ish, RB/WR early — refined by observed behavior during the draft.)
TARGET_BUILD = {"QB": 1.5, "RB": 5, "WR": 6, "TE": 1.5, "DST": 1, "K": 1}
STARTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1}


def norm_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[.'’-]", "", name)
    name = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", name)
    return re.sub(r"\s+", " ", name)


def load_board() -> dict:
    with open(DATA / "board.json") as f:
        board = json.load(f)
    idx = {norm_name(p["name"]): p for p in board["players"]}
    # ESPN draft rooms name defenses "Texans D/ST" while the board says
    # "Houston Texans" — index D/STs under their nickname variants too.
    for p in board["players"]:
        if p["pos"] == "DST":
            nick = norm_name(p["name"]).split()[-1]
            for alias in (nick, f"{nick} d/st", f"{nick} dst", f"{nick} defense"):
                idx.setdefault(alias, p)
    return idx


def load_state() -> dict:
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1)


def snake_team_for_pick(pick_no: int, teams: int) -> int:
    """1-indexed overall pick -> 1-indexed draft slot."""
    rnd = (pick_no - 1) // teams
    idx = (pick_no - 1) % teams
    return idx + 1 if rnd % 2 == 0 else teams - idx


def my_pick_numbers(slot: int, teams: int, rounds: int) -> list[int]:
    picks = []
    for rnd in range(rounds):
        base = rnd * teams
        picks.append(base + slot if rnd % 2 == 0 else base + (teams - slot + 1))
    return picks


def cmd_start(args) -> None:
    state = {
        "teams": args.teams,
        "slot": args.slot,
        "rounds": args.rounds,
        "picks": [],  # [{pick, team, name}]
    }
    save_state(state)
    mine = my_pick_numbers(args.slot, args.teams, args.rounds)
    print(f"Draft started: {args.teams} teams, our slot {args.slot}, {args.rounds} rounds.")
    print(f"Our picks: {mine}")


def taken_names(state: dict) -> set[str]:
    return {norm_name(p["name"]) for p in state["picks"]}


def roster_of(state: dict, team_slot: int, board: dict) -> dict:
    counts: dict[str, int] = {}
    for p in state["picks"]:
        if p["team"] == team_slot:
            bp = board.get(norm_name(p["name"]))
            pos = bp["pos"] if bp else "?"
            counts[pos] = counts.get(pos, 0) + 1
    return counts


def positional_need(counts: dict, pos: str, bias: float | None = None) -> float:
    """How hungry a team is for this position, 0..1+.

    `bias` is an optional multiplier (e.g. from league_history.pos_multiplier,
    reflecting a specific manager's learned tendency at this position/round)
    applied on top of the generic need. Default None leaves behaviour
    unchanged.
    """
    have = counts.get(pos, 0)
    target = TARGET_BUILD.get(pos, 1)
    starters = STARTER_SLOTS.get(pos, 1)
    if have < starters:
        n = 1.0 + (starters - have) * 0.3  # missing starters = urgent
    elif have < target:
        n = 0.5 * (target - have) / max(target - starters, 0.5)
    else:
        n = 0.1  # stocked; only takes screaming value
    if bias is not None:
        n *= bias
    return n


def survival_odds(state: dict, board: dict, candidate: dict, current_pick: int, next_my_pick: int,
                   bias_by_slot: dict[int, dict[str, float]] | None = None) -> float:
    """Rough P(candidate still available at our next pick).

    For each intervening pick, estimate the chance that team takes this player:
    base rate from ADP pressure scaled by that team's need at the position.
    `bias_by_slot` is an optional `{slot: {pos: multiplier}}` (learned league
    tendencies) that scales each intervening team's need at the candidate's
    position; None (the default) leaves behaviour unchanged.
    """
    teams = state["teams"]
    p_survive = 1.0
    adp = candidate.get("adp_overall")
    overall = candidate.get("overall_rank") or 999
    espn_adp = candidate.get("espn_adp")
    for pick_no in range(current_pick, next_my_pick):
        team_slot = snake_team_for_pick(pick_no, teams)
        if team_slot == state["slot"]:
            continue
        counts = roster_of(state, team_slot, board)
        bias = (bias_by_slot or {}).get(team_slot, {}).get(candidate["pos"])
        need = positional_need(counts, candidate["pos"], bias=bias)
        # ADP pressure: how overdue the player is at this pick (16-team drafts
        # run ~4/3 faster than the 12-team ADP baseline).
        # ESPN's ADP is the room's real market when we have it; else the old
        # UDK-ADP scaling.
        market_pick = min(espn_adp if espn_adp else (adp * 4 / 3 if adp else overall * 1.1), 300)
        overdue = (pick_no - market_pick) / 12
        base = 0.02 + max(0.0, min(0.5, 0.10 + 0.08 * overdue))
        p_take = min(0.85, base * need)
        p_survive *= (1 - p_take)
    return p_survive


def best_available(state: dict, board: dict, limit: int = 24) -> list[dict]:
    gone = taken_names(state)
    avail = [p for k, p in board.items() if k not in gone and (p.get("overall_rank") or p.get("flex_rank"))]
    return sorted(avail, key=lambda p: p.get("overall_rank") or 200 + (p.get("flex_rank") or 999))[:limit]


def cmd_pick(args) -> None:
    state = load_state()
    board = load_board()
    pick_no = len(state["picks"]) + 1
    team = args.team or snake_team_for_pick(pick_no, state["teams"])
    key = norm_name(args.name)
    bp = board.get(key)
    canonical = bp["name"] if bp else args.name
    if bp is None:
        print(f"  (warning: '{args.name}' not on the UDK board — recorded as-is)")
    state["picks"].append({"pick": pick_no, "team": team, "name": canonical})
    save_state(state)
    rnd = (pick_no - 1) // state["teams"] + 1
    print(f"Pick {pick_no} (R{rnd}, slot {team}): {canonical}" + (f" [{bp['pos']}{bp.get('pos_rank','')}]" if bp else ""))


def cmd_undo(args) -> None:
    state = load_state()
    if state["picks"]:
        removed = state["picks"].pop()
        save_state(state)
        print(f"Removed pick {removed['pick']}: {removed['name']}")


def cmd_status(args) -> None:
    state = load_state()
    board = load_board()
    pick_no = len(state["picks"]) + 1
    mine = my_pick_numbers(state["slot"], state["teams"], state["rounds"])
    upcoming = [p for p in mine if p >= pick_no]
    my_counts = roster_of(state, state["slot"], board)
    print(f"On the clock: pick {pick_no} (slot {snake_team_for_pick(pick_no, state['teams'])})")
    print(f"Our next picks: {upcoming[:3]}")
    print(f"Our roster: {my_counts}")
    my_players = [p["name"] for p in state["picks"] if p["team"] == state["slot"]]
    for n in my_players:
        print(f"  - {n}")


def cmd_advise(args) -> None:
    state = load_state()
    board = load_board()
    pick_no = len(state["picks"]) + 1
    mine = my_pick_numbers(state["slot"], state["teams"], state["rounds"])
    upcoming = [p for p in mine if p >= pick_no]
    if not upcoming:
        print("Draft over.")
        return
    next_mine = upcoming[0]
    after = upcoming[1] if len(upcoming) > 1 else next_mine + 2 * state["teams"]
    my_counts = roster_of(state, state["slot"], board)
    print(f"Advising for our pick {next_mine} (current pick {pick_no}); next after that: {after}")
    print(f"Our roster: {my_counts}\n")
    rows = []
    for p in best_available(state, board):
        surv = survival_odds(state, board, p, next_mine + 1, after)
        need = positional_need(my_counts, p["pos"])
        tags = ",".join(p.get("my_tags", []))
        rows.append((p, surv, need, tags))
    print(f"{'player':26s} {'pos':5s} {'tier':4s} {'UDK':4s} {'surv%':6s} {'need':5s} tags")
    for p, surv, need, tags in rows:
        surv_label = f"{surv*100:3.0f}%"
        print(f"{p['name']:26s} {p['pos']:5s} {str(p.get('tier','-')):4s} "
              f"{str(p.get('overall_rank','-')):4s} {surv_label:6s} {need:4.2f}  {tags}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--teams", type=int, default=16)
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--rounds", type=int, default=15)
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("pick")
    p.add_argument("name")
    p.add_argument("--team", type=int)
    p.set_defaults(func=cmd_pick)
    p = sub.add_parser("undo")
    p.set_defaults(func=cmd_undo)
    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("advise")
    p.set_defaults(func=cmd_advise)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
