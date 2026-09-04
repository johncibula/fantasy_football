"""Turn any rankings CSV into the board the draft engine scores.

The engine was built on one board schema (see draft_board.py). This module
fills that schema from a visitor's CSV so the public app never needs the
owner's rankings, tiers, or notes: a name, a position and a rank order are
enough; bye, tier, projected points, ADP and tags each unlock more of the
engine (bye clashes, tier notes, VBD and VONA, survival odds, the QB plan).

Kickers and defences keep only their position rank, as on the owner's board:
the engine injects them itself when the roster must be filled, and a ranked
defence would otherwise re-enter the candidate list under its aliases.

ADP in the CSV is an overall pick number from an `adp_teams` draft. It is
rescaled to the league being drafted and stored where the bots and the
survival model already read the market (`espn_adp`, `espn_rank`), so the room
in a mock drafts off the visitor's market, not the owner's.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from draft_tracker import index_board, norm_name

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "player", "player name", "playername"),
    "pos": ("pos", "position"),
    "team": ("team", "tm", "nfl team"),
    "rank": ("rank", "rk", "overall", "ovr", "overall rank", "#"),
    "bye": ("bye", "bye week", "byeweek"),
    "tier": ("tier", "tiers"),
    "proj": ("proj", "projection", "projected points", "proj pts", "points", "fpts", "pts"),
    "adp": ("adp", "avg", "avg pick", "average draft position"),
    "tags": ("tags", "tag", "notes", "note"),
}
REQUIRED_COLUMNS = ("name", "pos")
POSITION_ALIASES = {
    "QB": "QB",
    "RB": "RB",
    "WR": "WR",
    "TE": "TE",
    "K": "K",
    "PK": "K",
    "DST": "DST",
    "D/ST": "DST",
    "DEF": "DST",
    "D": "DST",
}
TAG_ALIASES = {
    "sleeper": "sleeper",
    "bust": "bust",
    "avoid": "bust",
    "value": "value",
    "target": "value",
    "breakout": "breakout",
    "watch": "watch",
    "watchlist": "watch",
}
FLEX_POSITIONS = ("RB", "WR", "TE")
UNRANKED_POSITIONS = ("K", "DST")
DRAFTABLE_WINDOW = 240
DEFAULT_ADP_TEAMS = 12
TEMPLATE_HEADER = "Rank,Name,Pos,Team,Bye,Tier,Proj,ADP,Tags"

NFL_TEAMS = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LV": "Las Vegas Raiders",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers",
    "SEA": "Seattle Seahawks",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}
TEAM_CODE_ALIASES = {
    "WSH": "WAS",
    "JAC": "JAX",
    "GNB": "GB",
    "KAN": "KC",
    "NWE": "NE",
    "NOR": "NO",
    "SFO": "SF",
    "TAM": "TB",
    "LVR": "LV",
    "OAK": "LV",
}
_NICKNAMES = {full.split()[-1].lower(): full for full in NFL_TEAMS.values()}
_POS_WITH_RANK = re.compile(r"^([A-Za-z/]+?)(\d*)$")
_TAG_SPLIT = re.compile(r"[,\s;/|]+")


class RankingsFormatError(ValueError):
    """The CSV cannot be read as rankings; the message says what is missing."""


@dataclass
class ImportReport:
    """What a CSV import produced, for the app's status line."""

    players: int = 0
    has_projections: bool = False
    has_tiers: bool = False
    has_adp: bool = False
    has_bye: bool = False
    tagged: int = 0
    skipped: list[str] = field(default_factory=list)

    def unlocked(self) -> list[str]:
        """The engine features this file switches on, in plain words."""
        out = ["rankings", "survival odds", "AI opponents"]
        if self.has_adp:
            out.append("market read (ADP)")
        if self.has_projections:
            out += ["value over replacement", "value over next available"]
        if self.has_tiers:
            out.append("tier alerts")
        if self.has_bye:
            out.append("bye clashes")
        if self.tagged:
            out.append("your tags")
        return out


@dataclass
class ImportedBoard:
    """A board the engine can score plus the story of how it was read."""

    index: dict
    players: list[dict]
    report: ImportReport
    columns: dict[str, str]


def template_csv() -> str:
    """The header line a visitor's CSV should carry; only Name and Pos are required."""
    return TEMPLATE_HEADER + "\n"


def _clean_header(header: str) -> str:
    return re.sub(r"[^a-z0-9#/ ]", "", header.strip().lower().replace("_", " "))


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = text.strip().replace(",", "")
    if not cleaned or cleaned in {"-", "--", "n/a", "na"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    value = _num(text)
    return int(value) if value is not None else None


def _team_code(text: str | None) -> str | None:
    code = (text or "").strip().upper()
    code = TEAM_CODE_ALIASES.get(code, code)
    return code if code in NFL_TEAMS else None


def _dst_name(name: str, team: str | None) -> str:
    if team:
        return NFL_TEAMS[team]
    for word in reversed(name.lower().replace("/", " ").split()):
        if word in _NICKNAMES:
            return _NICKNAMES[word]
    return name


def _dst_team(name: str) -> str | None:
    full = _dst_name(name, None)
    for code, team_name in NFL_TEAMS.items():
        if team_name == full:
            return code
    return None


def _parse_position(text: str | None) -> tuple[str | None, int | None]:
    match = _POS_WITH_RANK.match((text or "").strip().upper())
    if not match:
        return None, None
    pos = POSITION_ALIASES.get(match.group(1))
    pos_rank = int(match.group(2)) if match.group(2) else None
    return pos, pos_rank


def _parse_tags(text: str | None) -> list[str]:
    tags: list[str] = []
    for raw in _TAG_SPLIT.split((text or "").lower()):
        tag = TAG_ALIASES.get(raw.strip("[]()"))
        if tag and tag not in tags:
            tags.append(tag)
    return tags


class RankingsImporter:
    """Read a rankings CSV into the engine's board for one league size."""

    def __init__(self, teams: int, adp_teams: int = DEFAULT_ADP_TEAMS) -> None:
        """Remember the league size and the size of the draft the ADP column came from."""
        self.teams = teams
        self.adp_teams = adp_teams

    def from_bytes(self, data: bytes) -> ImportedBoard:
        """Decode an uploaded file (UTF-8 with or without a BOM, else Latin-1) and import it."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return self.from_text(text)

    def from_text(self, text: str) -> ImportedBoard:
        """Import CSV text; raises RankingsFormatError when Name or Pos is missing."""
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        columns = self._map_columns(reader.fieldnames or [])
        report = ImportReport()
        players: list[dict] = []
        seen: set[str] = set()
        for row_no, row in enumerate(reader, start=2):
            player = self._row_to_player(row, columns, len(players) + 1)
            if player is None:
                report.skipped.append(f"row {row_no}: no usable name and position")
                continue
            key = norm_name(player["name"])
            if key in seen:
                report.skipped.append(f"row {row_no}: duplicate of {player['name']}")
                continue
            seen.add(key)
            players.append(player)
        if not players:
            msg = "no player rows found under the header " + ", ".join(reader.fieldnames or [])
            raise RankingsFormatError(msg)
        self._finish(players, report)
        return ImportedBoard(index_board(players), players, report, columns)

    def _map_columns(self, fieldnames: list[str]) -> dict[str, str]:
        cleaned = {_clean_header(name): name for name in fieldnames if name}
        columns: dict[str, str] = {}
        for canonical, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in cleaned:
                    columns[canonical] = cleaned[alias]
                    break
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            msg = (
                f"missing column(s) {', '.join(missing)}; found: "
                f"{', '.join(fieldnames) or 'nothing'}. Expected something like {TEMPLATE_HEADER}"
            )
            raise RankingsFormatError(msg)
        return columns

    def _row_to_player(self, row: dict, columns: dict[str, str], order: int) -> dict | None:
        def cell(canonical: str) -> str | None:
            header = columns.get(canonical)
            return row.get(header) if header else None

        name = (cell("name") or "").strip()
        pos, pos_rank_in_cell = _parse_position(cell("pos"))
        if not name or pos is None:
            return None
        team = _team_code(cell("team"))
        if pos == "DST":
            name = _dst_name(name, team)
            team = team or _dst_team(name)
        adp = _num(cell("adp"))
        return {
            "name": name,
            "pos": pos,
            "team": team or "",
            "bye": _int(cell("bye")),
            "pos_rank": pos_rank_in_cell,
            "tier": _int(cell("tier")),
            "proj_points": _num(cell("proj")),
            "risk": None,
            "upside": None,
            "adp": f"{adp:g}" if adp is not None else None,
            "adp_overall": None,
            "outlook": None,
            "overall_rank": None if pos in UNRANKED_POSITIONS else (_int(cell("rank")) or order),
            "andy": None,
            "jason": None,
            "mike": None,
            "host_spread": None,
            "espn_rank": None,
            "espn_adp": adp,
            "ffc_adp": None,
            "pct_owned": None,
            "market_delta": None,
            "value_vs_adp": None,
            "my_tags": _parse_tags(cell("tags")),
            "my_notes": [],
        }

    def _finish(self, players: list[dict], report: ImportReport) -> None:
        players.sort(key=lambda p: p["overall_rank"] or DRAFTABLE_WINDOW + (p["espn_adp"] or 0))
        self._assign_position_ranks(players)
        self._assign_market(players)
        report.players = len(players)
        report.has_projections = any(p["proj_points"] for p in players)
        report.has_tiers = any(p["tier"] for p in players)
        report.has_adp = any(p["espn_adp"] for p in players)
        report.has_bye = any(p["bye"] for p in players)
        report.tagged = sum(1 for p in players if p["my_tags"])

    @staticmethod
    def _assign_position_ranks(players: list[dict]) -> None:
        counts: dict[str, int] = {}
        flex = 0
        for p in players:
            counts[p["pos"]] = counts.get(p["pos"], 0) + 1
            if p["pos_rank"] is None:
                p["pos_rank"] = counts[p["pos"]]
            if p["pos"] in FLEX_POSITIONS:
                flex += 1
                p["flex_rank"] = flex

    def _assign_market(self, players: list[dict]) -> None:
        scale = self.teams / self.adp_teams
        by_market = sorted(
            players,
            key=lambda p: (p["espn_adp"] is None, p["espn_adp"] or 0, p["overall_rank"] or 0),
        )
        for room_rank, p in enumerate(by_market, start=1):
            p["espn_rank"] = room_rank
            if p["espn_adp"] is not None:
                p["espn_adp"] = round(p["espn_adp"] * scale, 1)
            rank = p["overall_rank"]
            in_window = rank is not None and (
                rank <= DRAFTABLE_WINDOW or room_rank <= DRAFTABLE_WINDOW
            )
            p["market_delta"] = room_rank - rank if in_window else None
