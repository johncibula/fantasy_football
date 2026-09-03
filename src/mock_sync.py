"""Rebuild draft state from a scraped, ordered pick list (mock-draft feed).

Usage: echo one player name per line (overall pick order) | python src/mock_sync.py --slot 7
Idempotent: rebuilds the full state each call, then refreshes live.json.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import espn_client  # noqa: F401  loads .env
from draft_tracker import load_board, save_state, snake_team_for_pick, norm_name
from draft_live import write_live


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--teams", type=int, default=16)
    ap.add_argument("--rounds", type=int, default=15)
    args = ap.parse_args()

    board = load_board()
    names = [ln.strip() for ln in sys.stdin if ln.strip()]
    state = {"teams": args.teams, "slot": args.slot, "rounds": args.rounds, "picks": []}
    for i, raw in enumerate(names, start=1):
        bp = board.get(norm_name(raw))
        state["picks"].append({
            "pick": i,
            "team": snake_team_for_pick(i, args.teams),
            "name": bp["name"] if bp else raw,
        })
    save_state(state)
    write_live(state, board)
    print(f"Synced {len(names)} picks; next pick {len(names)+1}.")


if __name__ == "__main__":
    main()
