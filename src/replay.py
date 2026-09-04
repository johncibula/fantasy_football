"""Replay a past F3 draft against the opponent model.

Last year's players cannot be re-scored with this year's rankings, so this is
not a "would the engine have drafted better" test. It is the test that matters
for draft night: does the bot model (market board plus learned manager habits)
predict what the other 15 people actually do between our picks?

    ./venv/bin/python src/replay.py --year 2025 [--n 200]

For each of our picks, with the real draft frozen there, the lookahead is
rolled to our next pick twice, with the managers' learned habits and without,
and its survival predictions are compared with what really happened. Habits
are learned from the seasons before the replayed one only.

Writes reports/replay_<year>.md: Brier score and calibration (habits vs
generic), positional flow between our picks (predicted vs actual), and our own
picks that year against the room's board (reach or value).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draft_sim
import espn_client
import league_history
import lookahead
import market_feed
from draft_tracker import DATA, my_pick_numbers, norm_name

REPORTS = Path(__file__).resolve().parent.parent / "reports"
HISTORY = DATA / "history"
POS_ORDER = ("QB", "RB", "WR", "TE", "K", "DST")
MODES = ("habits", "generic")
CALIBRATION_BUCKETS = 10
SURVIVAL_CERTAIN = 0.999
DEFAULT_YEAR = 2025
DEFAULT_ROLLOUTS = 200
COIN_FLIP_BRIER = 0.25


def espn_board(year: int) -> dict[str, dict]:
    """The room's board for `year` from ESPN's player universe, shaped like data/board.json entries."""
    espn = market_feed.fetch_espn(year)
    for rec in espn:
        if rec["espn_adp"] is not None and rec["espn_adp"] >= market_feed.ESPN_UNDRAFTED_ADP:
            rec["espn_adp"] = None
    ordered = sorted(
        espn, key=lambda r: (0, r["espn_adp"]) if r["espn_adp"] else (1, r["espn_rank"] or 9999)
    )
    board: dict[str, dict] = {}
    pos_seen: Counter[str] = Counter()
    for i, rec in enumerate(ordered, start=1):
        pos_seen[rec["pos"]] += 1
        entry = {
            "name": rec["name"],
            "pos": rec["pos"],
            "team": rec["team"],
            "overall_rank": i,
            "pos_rank": pos_seen[rec["pos"]],
            "espn_rank": i,
            "espn_adp": rec["espn_adp"],
            "proj_points": None,
            "tier": None,
            "adp_overall": None,
        }
        for key in market_feed._keys_for(rec):
            board.setdefault(key, entry)
    return board


def load_draft(year: int) -> dict:
    """The pulled draft for `year` (see league_history.pull_drafts)."""
    return json.loads((HISTORY / f"draft_{year}.json").read_text())


def add_missing_picks(board: dict[str, dict], picks: list[dict]) -> int:
    """Put real draft picks absent from ESPN's pool on the board at the pick they went.

    ESPN's universe is capped and names drift year to year; a drafted player
    the pool lacks would otherwise vanish from the "actual" side while the
    bots still spend that pick on someone. His real pick number stands in
    for the room's rank. Returns how many were added.
    """
    pos_seen: Counter[str] = Counter(e["pos"] for e in {id(e): e for e in board.values()}.values())
    added = 0
    for p in sorted(picks, key=lambda x: x["overall"]):
        key = norm_name(p["name"])
        if key in board:
            continue
        pos_seen[p["pos"]] += 1
        board[key] = {
            "name": p["name"],
            "pos": p["pos"],
            "team": p.get("nfl_team") or "",
            "overall_rank": p["overall"],
            "pos_rank": pos_seen[p["pos"]],
            "espn_rank": p["overall"],
            "espn_adp": None,
            "proj_points": None,
            "tier": None,
            "adp_overall": None,
        }
        added += 1
    return added


def habits_before(year: int) -> dict:
    """Tendencies learned only from seasons before `year`, so the replay never peeks."""
    drafts = [
        d
        for d in (json.loads(f.read_text()) for f in sorted(HISTORY.glob("draft_*.json")))
        if d["season"] < year
    ]
    return league_history.learn(drafts, current_year=year) if drafts else {"managers": {}}


def bias_from_model(model: dict, order: list[int], round_no: int) -> dict[int, dict[str, float]]:
    """{slot: {pos: multiplier}} for `order` from an in-memory tendencies model."""
    by_tid: dict[int, tuple[int, dict]] = {}
    for prof in model.get("managers", {}).values():
        latest = prof.get("team_id_latest", {})
        for tid in prof.get("team_ids", []):
            season = latest.get(str(tid), 0)
            if tid not in by_tid or season > by_tid[tid][0]:
                by_tid[tid] = (season, prof)
    out: dict[int, dict[str, float]] = {}
    for slot, tid in enumerate(order, start=1):
        prof = by_tid.get(tid, (0, {}))[1]
        if prof:
            out[slot] = {
                pos: league_history.pos_multiplier(prof, pos, round_no) for pos in POS_ORDER
            }
    return out


class Tally:
    """Accumulates prediction quality for one mode (habits or generic)."""

    def __init__(self) -> None:
        """Start empty."""
        self.sq_errors: list[float] = []
        self.calib: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        self.flow: Counter[str] = Counter()

    def add(self, survival: float, *, gone: bool, pos: str) -> None:
        """Record one player's predicted survival against what happened."""
        self.sq_errors.append((survival - (0.0 if gone else 1.0)) ** 2)
        bucket = min(int(survival * CALIBRATION_BUCKETS), CALIBRATION_BUCKETS - 1)
        self.calib[bucket][0] += 0 if gone else 1
        self.calib[bucket][1] += 1
        if gone or survival < SURVIVAL_CERTAIN:
            self.flow[pos] += 1 - survival

    def brier(self) -> float:
        """Mean squared error of the survival predictions (0.25 is a coin flip)."""
        return sum(self.sq_errors) / max(1, len(self.sq_errors))

    def calibration(self) -> dict[str, tuple[float | None, int]]:
        """{predicted-range: (actual survival rate, n)} per decile."""
        width = 1 / CALIBRATION_BUCKETS
        return {
            f"{b * width:.0%}-{(b + 1) * width:.0%}": ((v[0] / v[1]) if v[1] else None, v[1])
            for b, v in sorted(self.calib.items())
        }


def _window_state(picks: list[dict], order: list[int], slot: int, rounds: int, upto: int) -> dict:
    """The draft frozen just after overall pick `upto`, as engine state."""
    before = [
        {"pick": p["overall"], "team": order.index(p["team_id"]) + 1, "name": p["name"]}
        for p in picks
        if p["overall"] <= upto
    ]
    return {"teams": len(order), "slot": slot, "rounds": rounds, "picks": before, "team_ids": order}


def replay(year: int, n: int = DEFAULT_ROLLOUTS, seed: int = 1) -> dict:
    """Score the opponent model against the real draft of `year`."""
    draft = load_draft(year)
    order = [int(t) for t in draft["order"]]
    my_tid = espn_client.get_config()["team_id"]
    if my_tid not in order:
        msg = f"team_id {my_tid} did not draft in {year}"
        raise SystemExit(msg)
    my_slot = order.index(my_tid) + 1
    picks = sorted(draft["picks"], key=lambda p: p["overall"])
    rounds = max(p["round"] for p in picks)
    board = espn_board(year)
    added = add_missing_picks(board, picks)
    model = habits_before(year)
    mine = my_pick_numbers(my_slot, len(order), rounds)

    tallies = {mode: Tally() for mode in MODES}
    actual_flow: Counter[str] = Counter()
    for pick, nxt in pairwise(mine):
        state = _window_state(picks, order, my_slot, rounds, pick)
        taken = {norm_name(p["name"]) for p in picks if pick < p["overall"] < nxt}
        round_no = (pick - 1) // len(order) + 1
        for mode in MODES:
            bias = bias_from_model(model, order, round_no) if mode == "habits" else None
            look = lookahead.rollout(
                state, board, n=n, seed=seed, pos_bias_by_slot=bias, horizon_picks=1
            )
            for key, survival in look["survival"].items():
                tallies[mode].add(survival, gone=key in taken, pos=board[key]["pos"])
        for key in taken:
            actual_flow[board[key]["pos"] if key in board else "?"] += 1

    my_picks = []
    for p in picks:
        if p["team_id"] != my_tid:
            continue
        entry = board.get(norm_name(p["name"]))
        my_picks.append(
            {
                "pick": p["overall"],
                "round": p["round"],
                "name": p["name"],
                "pos": p["pos"],
                "room_rank": entry["overall_rank"] if entry else None,
                "delta": (entry["overall_rank"] - p["overall"]) if entry else None,
            }
        )
    return {
        "year": year,
        "slot": my_slot,
        "teams": len(order),
        "rounds": rounds,
        "n": n,
        "unmatched": [],
        "added_to_pool": added,
        "brier": {mode: t.brier() for mode, t in tallies.items()},
        "calib": {mode: t.calibration() for mode, t in tallies.items()},
        "flow": {
            **{
                mode: {pos: round(t.flow[pos], 1) for pos in POS_ORDER}
                for mode, t in tallies.items()
            },
            "actual": {pos: actual_flow[pos] for pos in POS_ORDER},
        },
        "my_picks": my_picks,
    }


LEAGUE_BIAS_PATH = DATA / "league_bias.json"
CALIBRATION_ROUNDS = 3
CALIBRATION_DAMPING = 0.7
BIAS_FLOOR, BIAS_CEIL = 0.4, 4.0


def _flow_for_year(year: int, n: int, seed: int) -> tuple[Counter[str], Counter[str]]:
    """(actual, predicted-generic) players taken per position between our picks in `year`."""
    result = replay(year, n=n, seed=seed)
    return (Counter(result["flow"]["actual"]), Counter(result["flow"]["generic"]))


def calibrate(years: list[int], n: int, seed: int) -> dict[str, float]:
    """Learn league-level positional demand multipliers from replays of `years`.

    Iterates: predict with the current multipliers, compare with what the room
    actually took, nudge each position by the (damped) ratio. Writes
    data/league_bias.json, which draft_sim.league_bias() reads.
    """
    bias = {pos: 1.0 for pos in POS_ORDER}
    for _ in range(CALIBRATION_ROUNDS):
        LEAGUE_BIAS_PATH.write_text(json.dumps({"years": years, "bias": bias}, indent=1))
        draft_sim.reset_league_bias()
        actual: Counter[str] = Counter()
        predicted: Counter[str] = Counter()
        for year in years:
            a, p = _flow_for_year(year, n, seed)
            actual.update(a)
            predicted.update(p)
        for pos in POS_ORDER:
            if predicted[pos] > 0 and actual[pos] > 0:
                ratio = (actual[pos] / predicted[pos]) ** CALIBRATION_DAMPING
                bias[pos] = min(BIAS_CEIL, max(BIAS_FLOOR, bias[pos] * ratio))
        geo_mean = math.prod(bias.values()) ** (1 / len(bias))
        bias = {pos: v / geo_mean for pos, v in bias.items()}
        sys.stdout.write(
            f"calibration pass: actual {dict(actual)} predicted "
            f"{ {k: round(v, 1) for k, v in predicted.items()} } -> bias "
            f"{ {k: round(v, 2) for k, v in bias.items()} }\n"
        )
    LEAGUE_BIAS_PATH.write_text(json.dumps({"years": years, "bias": bias}, indent=1))
    draft_sim.reset_league_bias()
    return bias


def _fmt_cell(cell: tuple[float | None, int] | None) -> str:
    return f"{cell[0]:.0%} (n={cell[1]})" if cell and cell[0] is not None else "—"


def write_report(result: dict) -> Path:
    """Render the replay result as markdown under reports/."""
    year = result["year"]
    lines = [
        f"# {year} draft replay — does the opponent model predict the room?",
        "",
        f"Our slot {result['slot']} of {result['teams']}, {result['rounds']} rounds, "
        f"{result['n']} rollouts per window. Habits learned from seasons before {year} only.",
    ]
    if result["added_to_pool"]:
        lines.append(
            f"{result['added_to_pool']} drafted players were missing from ESPN's pool that year "
            "and were placed on the board at the pick they actually went."
        )
    lines += [
        "",
        f"## Survival prediction quality (Brier, lower is better; {COIN_FLIP_BRIER} = coin flip)",
        "",
        "| model | Brier |",
        "|---|---|",
    ]
    lines += [f"| {mode} | {result['brier'][mode]:.4f} |" for mode in MODES]
    lines += [
        "",
        "## Calibration (predicted survival → actual survival rate)",
        "",
        "| predicted | habits | generic |",
        "|---|---|---|",
    ]
    keys = sorted(set(result["calib"]["habits"]) | set(result["calib"]["generic"]))
    lines += [
        f"| {k} | {_fmt_cell(result['calib']['habits'].get(k))} | {_fmt_cell(result['calib']['generic'].get(k))} |"
        for k in keys
    ]
    lines += [
        "",
        "## Players taken between our picks, by position (whole draft)",
        "",
        "| pos | actual | predicted (habits) | predicted (generic) |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {pos} | {result['flow']['actual'][pos]} | {result['flow']['habits'][pos]} | "
        f"{result['flow']['generic'][pos]} |"
        for pos in POS_ORDER
    ]
    lines += [
        "",
        f"## Our {year} picks vs the room's board",
        "",
        "| pick | rd | player | pos | room rank | delta |",
        "|---|---|---|---|---|---|",
    ]
    for p in result["my_picks"]:
        delta = f"{p['delta']:+d}" if p["delta"] is not None else "—"
        lines.append(
            f"| {p['pick']} | {p['round']} | {p['name']} | {p['pos']} | {p['room_rank'] or '—'} | {delta} |"
        )
    deltas = [p["delta"] for p in result["my_picks"] if p["delta"] is not None]
    if deltas:
        lines += [
            "",
            f"Mean delta {sum(deltas) / len(deltas):+.1f} (positive = taken later than the room "
            "ranked him, value; negative = reach).",
        ]
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"replay_{year}.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    """CLI entry: replay a season and write the report."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=DEFAULT_YEAR)
    ap.add_argument("--n", type=int, default=DEFAULT_ROLLOUTS)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--calibrate",
        type=str,
        default=None,
        help="learn league demand multipliers from these seasons, e.g. 2022-2024",
    )
    args = ap.parse_args()
    if args.calibrate:
        lo, _, hi = args.calibrate.partition("-")
        years = list(range(int(lo), int(hi or lo) + 1))
        bias = calibrate(years, n=args.n, seed=args.seed)
        sys.stdout.write(f"written {LEAGUE_BIAS_PATH}: {bias}\n")
        return 0
    out = write_report(replay(args.year, n=args.n, seed=args.seed))
    sys.stdout.write(out.read_text())
    sys.stdout.write(f"written {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
