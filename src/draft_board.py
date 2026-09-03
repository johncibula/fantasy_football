"""Build the unified draft board from the UDK CSV exports.

Inputs (data/udk/): qb/rb/wr/te (tiered position rankings), flex (combined
RB+WR+TE rank), dst/k (simple rankings), top200 (overall with Andy/Jason/Mike).

Output: data/board.json — one record per player with every signal the live
draft tracker and cheat sheet need.
"""

import csv
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
UDK = DATA / "udk"
NOTES = DATA / "notes"

# John's own scouting notes: data/notes/<tag>.md, one "- Player Name — note" per line.
NOTE_FILES = {"sleeper": "sleepers.md", "bust": "busts.md", "value": "values.md",
              "breakout": "breakouts.md", "watch": "watchlist.md"}

# UDK ADP strings like "1.02" are round.pick in a 12-team format.
ADP_LEAGUE_SIZE = 12


def norm_name(name: str) -> str:
    """Normalize a player name for cross-file joining."""
    name = name.lower().strip()
    name = re.sub(r"[.'’-]", "", name)
    name = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", name)
    return re.sub(r"\s+", " ", name)


def adp_to_overall(adp: str) -> float | None:
    """'3.07' (round 3 pick 7, 12-team) -> overall pick 31."""
    if not adp:
        return None
    try:
        rnd, pick = adp.split(".")
        return (int(rnd) - 1) * ADP_LEAGUE_SIZE + int(pick)
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def build_board() -> dict:
    players: dict[str, dict] = {}

    # Tiered position files are the core record.
    for pos, fname in [("QB", "qb.csv"), ("RB", "rb.csv"), ("WR", "wr.csv"), ("TE", "te.csv")]:
        for row in read_csv(UDK / fname):
            key = norm_name(row["Name"])
            players[key] = {
                "name": row["Name"],
                "pos": pos,
                "team": row["Team"],
                "bye": int(row["Bye Week"]) if row["Bye Week"].isdigit() else None,
                "pos_rank": int(row["Rank"]),
                "tier": int(row["Tier"]) if row.get("Tier", "").isdigit() else None,
                "proj_points": float(row["Points"]) if row.get("Points") else None,
                "risk": float(row["Risk"]) if row.get("Risk") else None,
                "upside": float(row["Upside"]) if row.get("Upside") else None,
                "adp": row.get("ADP") or None,
                "adp_overall": adp_to_overall(row.get("ADP", "")),
                "outlook": row.get("Outlook") or None,
            }

    # D/ST and K: no tiers/projections, keep it light.
    for pos, fname in [("DST", "dst.csv"), ("K", "k.csv")]:
        for row in read_csv(UDK / fname):
            key = norm_name(row["Name"])
            players[key] = {
                "name": row["Name"],
                "pos": pos,
                "team": row["Team"],
                "bye": int(row["Bye Week"]) if row["Bye Week"].isdigit() else None,
                "pos_rank": int(row["Rank"]),
                "tier": None,
                "proj_points": None,
                "risk": None,
                "upside": None,
                "adp": None,
                "adp_overall": None,
                "outlook": None,
            }

    # Flex rank (RB/WR/TE combined) — the cross-position value ordering.
    for row in read_csv(UDK / "flex.csv"):
        p = players.get(norm_name(row["Name"]))
        if p:
            p["flex_rank"] = int(row["Rank"])

    # Top 200 overall + the three hosts' individual ranks.
    for row in read_csv(UDK / "top200.csv"):
        p = players.get(norm_name(row["Name"]))
        if p is None:
            # Player in top200 but missing from position files — record anyway.
            key = norm_name(row["Name"])
            p = players[key] = {
                "name": row["Name"],
                "pos": row["Pos"],
                "team": row["Team"],
                "bye": int(row["Bye"]) if row["Bye"].isdigit() else None,
                "pos_rank": None, "tier": None, "proj_points": None,
                "risk": None, "upside": None, "adp": None,
                "adp_overall": None, "outlook": None,
            }
        p["overall_rank"] = int(row["Rank"])
        p["andy"] = int(row["Andy"]) if row.get("Andy", "").isdigit() else None
        p["jason"] = int(row["Jason"]) if row.get("Jason", "").isdigit() else None
        p["mike"] = int(row["Mike"]) if row.get("Mike", "").isdigit() else None
        ranks = [r for r in (p["andy"], p["jason"], p["mike"]) if r]
        # Host disagreement = the board is split on this player; flag for judgment.
        p["host_spread"] = max(ranks) - min(ranks) if len(ranks) >= 2 else None

    # Merge John's note files as tags.
    unmatched = []
    for tag, fname in NOTE_FILES.items():
        path = NOTES / fname
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.lstrip("-* ").strip()
            name, _, note = [s.strip() for s in line.partition("—")]
            if not note:  # allow plain "-" separators too
                name, _, note = [s.strip() for s in line.partition(" - ")]
            p = players.get(norm_name(name))
            if p is None:
                unmatched.append((tag, name))
                continue
            p.setdefault("my_tags", []).append(tag)
            if note:
                p.setdefault("my_notes", []).append(f"[{tag}] {note}")
    for tag, name in unmatched:
        print(f"  ! note not matched to a player: {name} ({tag}) — check spelling")

    # Market (ESPN draft-room rank/ADP + FFC ADP) from data/market.json when
    # present. market_delta = ESPN rank - UDK rank: +ve means the room ranks
    # him LATER than the Ballers do (he'll slide; you can wait), -ve means the
    # room takes him earlier than we'd pay (no waiting on him).
    market = {}
    mpath = DATA / "market.json"
    if mpath.exists():
        try:
            market = json.load(open(mpath)).get("players", {})
        except (OSError, ValueError):
            market = {}
    by_dst_team = {r["team"]: r for r in market.values() if r.get("pos") == "DST" and r.get("team")}
    matched = 0
    for key, p in players.items():
        r = market.get(key)
        if r is None and p["pos"] == "DST":
            r = by_dst_team.get(p["team"])
        if r is None:
            p["espn_rank"] = p["espn_adp"] = p["ffc_adp"] = p["pct_owned"] = p["market_delta"] = None
            continue
        matched += 1
        p["espn_rank"] = r.get("espn_order")      # the room's board position (see market_feed)
        p["espn_adp"] = r.get("espn_adp")         # None when undrafted in ESPN drafts
        p["ffc_adp"] = r.get("ffc_adp")
        p["pct_owned"] = r.get("pct_owned")
        ur, er = p.get("overall_rank"), r.get("espn_order")
        # Only inside the draftable window (240 picks) is a gap meaningful.
        p["market_delta"] = (er - ur) if (ur and er and (ur <= 240 or er <= 240)) else None
    if market:
        print(f"  market: {matched}/{len(players)} board players matched to ESPN")

    # Value vs ADP: positive = UDK likes them more than the market (target),
    # negative = market reaches for them beyond UDK's rank (avoid paying).
    for p in players.values():
        if p.get("overall_rank") and p.get("adp_overall"):
            p["value_vs_adp"] = round(p["adp_overall"] - p["overall_rank"], 1)
        else:
            p["value_vs_adp"] = None

    board = {
        "meta": {
            "source": "Fantasy Footballers UDK CSV export",
            "adp_league_size": ADP_LEAGUE_SIZE,
            "players": len(players),
        },
        "players": sorted(
            players.values(),
            key=lambda p: p.get("overall_rank") or (200 + (p.get("flex_rank") or p.get("pos_rank") or 999)),
        ),
    }
    return board


def main() -> None:
    board = build_board()
    out = DATA / "board.json"
    with open(out, "w") as f:
        json.dump(board, f, indent=1)
    counts: dict[str, int] = {}
    tiered = 0
    for p in board["players"]:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
        tiered += p.get("tier") is not None
    print(f"Wrote {out} — {board['meta']['players']} players ({counts}), {tiered} with tiers")
    top = [p for p in board["players"] if p.get("overall_rank")][:10]
    print("\nTop 10 overall:")
    for p in top:
        print(f"  {p['overall_rank']:3d}. {p['name']:26s} {p['pos']:3s} tier {p['tier']} "
              f"ADP {p['adp'] or '-':5s} value {p['value_vs_adp']}")
    print("\nBiggest values in top 100 (UDK rank vs ADP):")
    vals = sorted((p for p in board["players"] if p.get("overall_rank") and p["overall_rank"] <= 100 and p["value_vs_adp"] is not None),
                  key=lambda p: -p["value_vs_adp"])[:8]
    for p in vals:
        print(f"  +{p['value_vs_adp']:4.0f}  {p['name']:26s} {p['pos']:3s} UDK {p['overall_rank']} vs ADP {p['adp']}")


if __name__ == "__main__":
    main()
