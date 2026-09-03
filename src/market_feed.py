"""Market rankings: what the ESPN draft room actually shows the other 15 managers.

The Fantasy Footballers board is OUR valuation. The room drafts off ESPN's
default board (its PPR rank drives the draft-app ordering and every autopick)
and ESPN's ADP is the realised market across ESPN drafts. A big gap between
the two is actionable: UDK #40 / ESPN #75 means the room will let him slide,
so wait; UDK #60 / ESPN #35 means he goes early, take him now or move on.

Source: ESPN's public player universe (kona_player_info) — the same endpoint
the draft room uses. Needs the league cookies from .env. Optional second
source: FantasyFootballCalculator 16-team PPR ADP when it has data.

  ./venv/bin/python src/market_feed.py --refresh [--top 40]

Writes data/market.json: {"fetched_at", "players": {norm_name: {...}}}.
Consumers (draft_board merge, draft_sim bots, survival) read that file.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import espn_client  # noqa: E402,F401  (loads .env)
from draft_tracker import DATA, norm_name  # noqa: E402

MARKET = DATA / "market.json"
ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
            "/segments/0/leaguedefaults/3?view=kona_player_info")
FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams={teams}&year={year}"
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
ESPN_UNDRAFTED_ADP = 169.5   # ESPN reports ~170 for anyone undrafted in its drafts
DRAFTABLE = 240              # 16 teams x 15 rounds: gaps beyond this are noise
# ESPN proTeamId -> abbreviation (only what D/ST matching needs)
PRO = {1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GB",
       10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE",
       18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
       26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}


def _cookies() -> dict:
    return {"espn_s2": os.environ.get("ESPN_S2", ""), "SWID": os.environ.get("SWID", "")}


def fetch_espn(year: int, limit: int = 600, timeout: float = 30.0) -> list[dict]:
    flt = {"players": {"limit": limit,
                       "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "PPR"},
                       "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": [f"00{year}"]}}}
    r = requests.get(ESPN_URL.format(year=year), headers={"X-Fantasy-Filter": json.dumps(flt)},
                     cookies=_cookies(), timeout=timeout)
    r.raise_for_status()
    out = []
    for pe in r.json().get("players", []):
        p = pe.get("player") or {}
        pos = POS.get(p.get("defaultPositionId"))
        if not pos:
            continue
        own = p.get("ownership") or {}
        ranks = p.get("draftRanksByRankType") or {}
        out.append({
            "name": p.get("fullName"), "pos": pos, "team": PRO.get(p.get("proTeamId"), ""),
            "espn_id": p.get("id"),
            "espn_adp": round(own.get("averageDraftPosition") or 0, 1) or None,
            "espn_rank": (ranks.get("PPR") or {}).get("rank"),
            "espn_rank_std": (ranks.get("STANDARD") or {}).get("rank"),
            "pct_owned": round(own.get("percentOwned") or 0, 1),
            "injury_status": p.get("injuryStatus"),
        })
    return out


def fetch_ffc(year: int, teams: int = 16, timeout: float = 20.0) -> tuple[dict[str, float], int]:
    """({norm_name: adp}, teams) from FantasyFootballCalculator — the largest
    league size it has data for (16 is usually empty; 12 has thousands of
    drafts). ADP is an overall pick number, which is what the engine wants."""
    for t in (teams, 14, 12, 10):
        try:
            r = requests.get(FFC_URL.format(teams=t, year=year), timeout=timeout)
            if r.status_code != 200:
                continue
            d = r.json()
            players = d.get("players") or []
            if players:
                return {norm_name(p["name"]): float(p["adp"]) for p in players if p.get("adp")}, t
        except Exception:  # noqa: BLE001
            continue
    return {}, 0


def _keys_for(rec: dict) -> list[str]:
    """Index keys so board names match: skill players by name; D/ST by the
    board's 'City Nickname' form AND the ESPN 'Nickname D/ST' form."""
    keys = [norm_name(rec["name"] or "")]
    if rec["pos"] == "DST" and rec["name"]:
        nick = norm_name(rec["name"]).replace(" d/st", "").replace(" dst", "").strip()
        keys += [nick, f"{nick} d/st", f"{nick} dst"]
    return [k for k in keys if k]


def refresh(year: int | None = None) -> dict:
    year = year or espn_client.get_config()["season"]
    espn = fetch_espn(year)
    ffc, ffc_teams = fetch_ffc(year)
    # ESPN's ADP saturates at ~170 (undrafted in its 10-team drafts) and its
    # rank field runs past 1000, so neither is usable raw. The ROOM's board is
    # ESPN's ordering: real ADP first, then ESPN rank for the undrafted tail.
    # espn_order is that position (1 = first off the board) and is the number
    # the engine compares with UDK rank and feeds to the bots.
    for rec in espn:
        if rec["espn_adp"] is not None and rec["espn_adp"] >= ESPN_UNDRAFTED_ADP:
            rec["espn_adp"] = None
    ordered = sorted(espn, key=lambda r: (0, r["espn_adp"]) if r["espn_adp"] else (1, r["espn_rank"] or 9999))
    for i, rec in enumerate(ordered, start=1):
        rec["espn_order"] = i
    players: dict[str, dict] = {}
    for rec in espn:
        rec["ffc_adp"] = ffc.get(norm_name(rec["name"] or ""))
        for k in _keys_for(rec):
            players.setdefault(k, rec)
    cache = {"fetched_at": datetime.now(timezone.utc).isoformat(), "year": year,
             "sources": {"espn": len(espn), "ffc": len(ffc), "ffc_teams": ffc_teams},
             "players": players}
    MARKET.parent.mkdir(parents=True, exist_ok=True)
    MARKET.write_text(json.dumps(cache, indent=1))
    return cache


_MEMO: dict = {"key": None, "cache": None}


def load() -> dict:
    try:
        st = MARKET.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"players": {}}
    if _MEMO["key"] != key:
        _MEMO["key"], _MEMO["cache"] = key, json.loads(MARKET.read_text())
    return _MEMO["cache"]


def market_for(name: str) -> dict | None:
    return load().get("players", {}).get(norm_name(name))


def _cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--top", type=int, default=30, help="discrepancies to print")
    args = ap.parse_args()
    if args.refresh or not MARKET.exists():
        c = refresh()
        print(f"market: espn {c['sources']['espn']} players, ffc {c['sources']['ffc']} "
              f"({c['sources']['ffc_teams']}-team) — {MARKET}")
    m = load()
    board = json.loads((DATA / "board.json").read_text())["players"]
    rows, unmatched = [], []
    for p in board:
        if not p.get("overall_rank"):
            continue
        r = m["players"].get(norm_name(p["name"]))
        if not r or not r.get("espn_order"):
            unmatched.append(p["name"])
            continue
        if p["overall_rank"] > DRAFTABLE and r["espn_order"] > DRAFTABLE:
            continue
        rows.append((r["espn_order"] - p["overall_rank"], p, r))
    matched = len(rows)
    print(f"board players with UDK rank: {matched + len(unmatched)}, matched to ESPN: {matched}"
          + (f", unmatched: {', '.join(unmatched[:12])}" if unmatched else ""))
    rows.sort(key=lambda x: -abs(x[0]))
    print(f"\n{'Δ':>5} {'player':24s} {'pos':4s} {'UDK':>4} {'ROOM':>5} {'ADP':>6}   read")
    for d, p, r in rows[:args.top]:
        read = "room lets him slide — can wait" if d >= 15 else ("room takes him early — no waiting" if d <= -15 else "")
        print(f"{d:+5d} {p['name']:24s} {p['pos']:4s} {p['overall_rank']:>4} {r['espn_order']:>5} "
              f"{(r['espn_adp'] or 0):6.1f}   {read}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
