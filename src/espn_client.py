"""Shared entry point for the ESPN league connection.

Credentials come from .env (ESPN_S2, SWID, LEAGUE_ID); season and team
settings from config.yaml. Every other module gets its League object here.
"""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from espn_api.football import League

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


@lru_cache(maxsize=1)
def get_config() -> dict:
    with open(PROJECT_ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def get_league() -> League:
    league_id = os.environ.get("LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("SWID")
    if not league_id:
        raise RuntimeError("LEAGUE_ID missing — add it to .env")
    return League(
        league_id=int(league_id),
        year=get_config()["season"],
        espn_s2=espn_s2,
        swid=swid,
    )


def my_team(league: League | None = None):
    league = league or get_league()
    team_id = get_config().get("team_id")
    if team_id is None:
        raise RuntimeError("team_id missing — set it in config.yaml")
    for team in league.teams:
        if team.team_id == team_id:
            return team
    raise RuntimeError(f"team_id {team_id} not found in league")


def verify_connection() -> None:
    """Connect and print league settings so downstream logic can be checked
    against reality (team count, scoring, roster slots, waiver system)."""
    league = get_league()
    s = league.settings
    print(f"League: {s.name}")
    print(f"Teams: {s.team_count}")
    print(f"Playoff teams: {s.playoff_team_count}, reg season ends wk {s.reg_season_count}")
    print(f"Scoring type: {getattr(s, 'scoring_type', 'unknown')}")
    for item in getattr(s, "scoring_format", []):
        if item.get("abbr") == "REC":
            print(f"Points per reception: {item.get('points')}")
    print(f"FAAB: {getattr(s, 'faab', 'unknown')}")
    print("Roster slots:", getattr(s, "position_slot_counts", "unknown"))
    print("\nTeams:")
    for team in league.teams:
        owners = ", ".join(o.get("firstName", "?") + " " + o.get("lastName", "") for o in team.owners) if team.owners else "?"
        print(f"  [{team.team_id:2d}] {team.team_name}  ({owners.strip()})")


if __name__ == "__main__":
    verify_connection()
