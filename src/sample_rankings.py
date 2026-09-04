"""Build samples/sample_rankings.csv from ESPN's public player universe.

The public app ships one rankings file that is nobody's edge: ESPN's default
PPR draft rank, its ADP, and its season projection for the top players, with
bye weeks from ESPN's team schedule. Nothing under data/ and no league cookies
are read. Regenerate before showing the app so the sample is current:

  ./venv/bin/python src/sample_rankings.py [--top 300] [--year 2026]

ESPN's ADP comes from its 10-team drafts, so the app scales it from 10 teams.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

import requests

PLAYERS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
    "/segments/0/leaguedefaults/3?view=kona_player_info"
)
TEAMS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
    "?view=proTeamSchedules_wl"
)
SAMPLE_PATH = Path(__file__).resolve().parent.parent / "samples" / "sample_rankings.csv"
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
ADP_TEAMS = 10
UNDRAFTED_ADP = 169.5
PROJECTION_SOURCE = 1
SEASON_TOTAL_PERIOD = 0
DEFAULT_TOP = 300
DEFAULT_YEAR = 2026
COLUMNS = ("Rank", "Name", "Pos", "Team", "Bye", "Proj", "ADP")
TIMEOUT_S = 30.0

log = logging.getLogger(__name__)


def fetch_teams(year: int) -> dict[int, tuple[str, int | None]]:
    """ESPN pro-team id -> (abbreviation, bye week)."""
    resp = requests.get(TEAMS_URL.format(year=year), timeout=TIMEOUT_S)
    resp.raise_for_status()
    teams = resp.json()["settings"]["proTeams"]
    return {t["id"]: (t["abbrev"], t.get("byeWeek") or None) for t in teams if t.get("abbrev")}


def fetch_players(year: int, limit: int) -> list[dict]:
    """The top `limit` players by ESPN's PPR draft rank, raw."""
    flt = {
        "players": {
            "limit": limit,
            "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "PPR"},
            "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": [f"10{year}"]},
        }
    }
    resp = requests.get(
        PLAYERS_URL.format(year=year),
        headers={"X-Fantasy-Filter": json.dumps(flt)},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return [entry["player"] for entry in resp.json().get("players", []) if entry.get("player")]


def season_projection(player: dict, year: int) -> float | None:
    """ESPN's projected season total for `year`, when the payload carries one."""
    for stat in player.get("stats") or []:
        if (
            stat.get("statSourceId") == PROJECTION_SOURCE
            and stat.get("seasonId") == year
            and stat.get("scoringPeriodId") == SEASON_TOTAL_PERIOD
        ):
            total = stat.get("appliedTotal")
            return round(total, 1) if total else None
    return None


def to_row(player: dict, year: int, teams: dict[int, tuple[str, int | None]]) -> dict | None:
    """One CSV row, or None when the player has no position or PPR rank."""
    pos = POSITIONS.get(player.get("defaultPositionId"))
    rank = ((player.get("draftRanksByRankType") or {}).get("PPR") or {}).get("rank")
    if not pos or not rank:
        return None
    team, bye = teams.get(player.get("proTeamId"), ("", None))
    adp = (player.get("ownership") or {}).get("averageDraftPosition")
    if adp is not None and adp >= UNDRAFTED_ADP:
        adp = None
    return {
        "Rank": rank,
        "Name": player.get("fullName", ""),
        "Pos": pos,
        "Team": team,
        "Bye": bye or "",
        "Proj": season_projection(player, year) or "",
        "ADP": round(adp, 1) if adp is not None else "",
    }


def build(year: int, top: int) -> list[dict]:
    """Fetch and shape the sample rows, sorted by ESPN rank."""
    teams = fetch_teams(year)
    rows = [row for p in fetch_players(year, top) if (row := to_row(p, year, teams))]
    rows.sort(key=lambda r: r["Rank"])
    return rows


def write(rows: list[dict], path: Path = SAMPLE_PATH) -> None:
    """Write the rows as the CSV the importer reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    args = parser.parse_args()
    rows = build(args.year, args.top)
    write(rows)
    with_proj = sum(1 for r in rows if r["Proj"] != "")
    log.info("wrote %s: %d players, %d with projections", SAMPLE_PATH, len(rows), with_proj)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
