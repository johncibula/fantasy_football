"""One draft, any league, in memory: the engine without disk state or ESPN.

`DraftSession` wraps the same state dict `draft_live.build_live` scores on
draft night. In a mock it drives `draft_sim`'s bots for every other slot; in
assist mode it records a real room's picks in draft order. Either way it
hands back the dashboard payload and the Monte Carlo lookahead. Nothing here
reads data/draft_state.json or the owner's board, so the public app can run
it for whatever rankings a visitor uploads.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import draft_live
import draft_sim
import lookahead
from draft_tracker import norm_name, snake_team_for_pick

STANDARD_ROSTER: Mapping[str, int] = MappingProxyType(
    {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1, "BENCH": 6}
)
DEFAULT_TEAMS = 12
DEFAULT_ROUNDS = 15
MIN_TEAMS = 4
LOOKAHEAD_N = 150
STARTER_ORDER = ("QB", "RB", "WR", "TE", "FLEX")


class DraftError(ValueError):
    """A pick the draft cannot accept; the message is fit to show the user."""


@dataclass(frozen=True)
class LeagueProfile:
    """The shape of the league being drafted: size, length, and lineup."""

    teams: int = DEFAULT_TEAMS
    rounds: int = DEFAULT_ROUNDS
    roster: Mapping[str, int] = STANDARD_ROSTER

    def __post_init__(self) -> None:
        """Reject sizes the snake math cannot draft."""
        if self.teams < MIN_TEAMS or self.rounds < 1:
            msg = f"a league needs at least {MIN_TEAMS} teams and one round"
            raise ValueError(msg)

    @property
    def total_picks(self) -> int:
        """Picks in the whole draft."""
        return self.teams * self.rounds

    def league_config(self) -> dict:
        """The shape `draft_live.league_config` reads off the draft state."""
        return {"size": self.teams, "roster": dict(self.roster)}


class DraftSession:
    """A running draft for one board, one league shape, and one seat."""

    def __init__(
        self, board: dict, profile: LeagueProfile, slot: int, *, mock: bool, seed: int | None = None
    ) -> None:
        """Start an empty draft; a mock immediately lets the bots pick up to our seat."""
        if not 1 <= slot <= profile.teams:
            msg = f"slot must be between 1 and {profile.teams}"
            raise DraftError(msg)
        self._board = board
        self._profile = profile
        self._rng = random.Random(seed)
        self._state: dict = {
            "teams": profile.teams,
            "slot": slot,
            "rounds": profile.rounds,
            "picks": [],
            "mock": mock,
            "league": profile.league_config(),
        }
        self._live: tuple[int, dict] | None = None
        self._look: tuple[int, dict] | None = None
        if mock:
            self._advance_bots()

    @property
    def profile(self) -> LeagueProfile:
        """The league shape this draft runs under."""
        return self._profile

    @property
    def slot(self) -> int:
        """Our draft seat, 1-based."""
        return self._state["slot"]

    @property
    def mock(self) -> bool:
        """True when bots fill the other seats."""
        return bool(self._state["mock"])

    @property
    def picks(self) -> list[dict]:
        """Every pick so far, in draft order (copies)."""
        return [dict(p) for p in self._state["picks"]]

    @property
    def pick_no(self) -> int:
        """The pick currently on the clock, 1-based."""
        return len(self._state["picks"]) + 1

    @property
    def is_over(self) -> bool:
        """True once every roster slot is filled."""
        return len(self._state["picks"]) >= self._profile.total_picks

    @property
    def on_clock_slot(self) -> int | None:
        """The seat picking now, or None when the draft is over."""
        if self.is_over:
            return None
        return snake_team_for_pick(self.pick_no, self._profile.teams)

    @property
    def our_turn(self) -> bool:
        """True when our seat is on the clock."""
        return self.on_clock_slot == self.slot

    def lookup(self, name: str) -> dict | None:
        """The board record for `name`, matched through the tracker's normaliser."""
        return self._board.get(norm_name(name))

    def available(self, query: str = "", limit: int = 10) -> list[dict]:
        """Undrafted players whose name contains `query`, best rank first."""
        text = query.strip().lower()
        hits = [
            p for p in draft_sim.available(self._state, self._board) if text in p["name"].lower()
        ]
        hits.sort(key=lambda p: p.get("overall_rank") or DRAFT_END_RANK)
        return hits[:limit]

    def pick(self, name: str) -> dict:
        """Record `name` for the seat on the clock; in a mock the bots then draft up to us.

        Returns {"name", "pick", "team", "mine"}. Raises DraftError for an
        unknown or already-drafted player, or once the draft is over.
        """
        if self.is_over:
            msg = "the draft is over"
            raise DraftError(msg)
        player = self.lookup(name)
        if player is None:
            msg = f"{name} is not on this board"
            raise DraftError(msg)
        if norm_name(player["name"]) in self._taken():
            msg = f"{player['name']} is already drafted"
            raise DraftError(msg)
        team = self.on_clock_slot
        record = {"pick": self.pick_no, "team": team, "name": player["name"]}
        self._state["picks"].append(record)
        if self.mock:
            self._advance_bots()
        return {**record, "mine": team == self.slot}

    def undo(self) -> str | None:
        """Take back the last pick; a mock rewinds through the bots to our previous turn."""
        picks = self._state["picks"]
        if self.mock:
            while picks and picks[-1]["team"] != self.slot:
                picks.pop()
        removed = picks.pop() if picks else None
        return removed["name"] if removed else None

    def live(self) -> dict:
        """The dashboard payload for the current state, computed once per state."""
        key = self._state_key()
        if self._live is None or self._live[0] != key:
            payload = draft_live.build_live(self._state, self._board)
            for rec in payload["recs"]:
                rec["profile"] = None
            self._live = (key, payload)
        return self._live[1]

    def lookahead(self, n: int = LOOKAHEAD_N) -> dict:
        """The Monte Carlo rollout behind the survival odds, computed once per state."""
        key = self._state_key()
        if self._look is None or self._look[0] != key:
            seed = self._rng.randrange(1 << 30)
            self._look = (key, lookahead.rollout(self._state, self._board, n=n, seed=seed))
        return self._look[1]

    def recap(self) -> list[dict]:
        """Every roster's projected starting lineup, best first, for the end of a draft."""
        rows = []
        for team in range(1, self._profile.teams + 1):
            lineup = draft_live.my_lineup(self._state, self._board, team)
            starters = lineup["QB"] + lineup["skill"]
            rows.append(
                {
                    "slot": team,
                    "mine": team == self.slot,
                    "proj": round(sum(p.get("proj_points") or 0.0 for p in starters), 1),
                    "starters": [f"{p['pos']} {p['name']}" for p in starters],
                }
            )
        rows.sort(key=lambda r: -r["proj"])
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows

    def _taken(self) -> set[str]:
        return {norm_name(p["name"]) for p in self._state["picks"]}

    def _state_key(self) -> int:
        return hash(tuple(p["name"] for p in self._state["picks"]))

    def _advance_bots(self) -> None:
        draft_sim.sim_until_my_turn(self._state, self._board, self._rng)


DRAFT_END_RANK = 10_000
