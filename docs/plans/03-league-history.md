# Plan 03 — Learn F³ drafting tendencies from past ESPN drafts (`src/league_history.py`)

Read `docs/plans/00-overview.md` first.

## Why

The survival model assumes every opponent drafts to a generic 16-team build
(`TARGET_BUILD` in `draft_tracker.py`). This league has years of history on
ESPN. Some managers take a QB in round 3 every year; some never take a TE
before round 10; some always draft Cowboys. Those habits are the best
predictor of who takes whom between our picks.

## Data source

ESPN v3 API, same cookies as the live poller (`ESPN_S2`, `SWID`, `LEAGUE_ID`
from `.env`; `import espn_client` loads .env). For a past season:

```
GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league_id}
    ?view=mDraftDetail&view=mTeam&view=mSettings
```

`draftDetail.picks[]` has `overallPickNumber`, `roundId`, `roundPickNumber`,
`teamId`, `playerId`, `autoDraftTypeId` (non-zero = autopick/robot),
`reservedForKeeper`. `teams[]` has `id`, `name`/`location`+`nickname`, and
`owners` (SWID strings) — owners are the persistent identity; team ids can be
reassigned when a manager leaves. `settings.draftSettings.pickOrder` is the
draft order for that year.

Player positions: `playerId` → position. The cheapest way is `espn_api`'s
`League(league_id, year, espn_s2, swid).player_map` (id→name) plus a
position lookup via the `kona_player_info` view, OR simply resolve
`playerId` through `league.player_info`. Explore what `espn_api` (already
installed in venv) gives you for a historical year; `League(...).draft` returns
`Pick` objects with `.playerName`, `.round_num`, `.round_pick`, `.team`,
`.playerId`, `.bid_amount`, `.nominatingTeam`, `.auto_draft_type`, which may be
enough. Position may need `league.player_info(playerId=...)`; cache
aggressively because that is one call per player. Prefer a single
`kona_player_info` request per season with a large `X-Fantasy-Filter` if you
can get it working; otherwise fall back to per-player lookups with a cache
file `data/player_pos_cache.json`.

Seasons to pull: try 2021 through 2025. Skip any season that errors or has no
draft picks (the league may not go back that far). Say in the report which
seasons actually loaded.

## Deliverable

`src/league_history.py`:

```python
def pull_drafts(seasons: list[int]) -> list[dict]
    # each: {"season", "teams", "order": [team_id,...], "picks": [
    #   {"overall", "round", "team_id", "owner": swid_or_None, "name", "pos",
    #    "auto": bool}]}
    # Saved raw to data/history/draft_{season}.json so re-runs are offline.

def learn(drafts: list[dict], current_year: int = 2026) -> dict
    # Pure function. Returns the tendencies model, saved to data/tendencies.json.

def load_tendencies() -> dict                     # {} if file missing
def pos_multiplier(profile: dict, pos: str, round_no: int) -> float
def tendencies_by_slot(order: list[int]) -> dict[int, dict]
    # maps a draft-order list of team_ids (slot 1..N) to {slot: profile}
```

`learn` output (keyed by owner SWID when present, else by team_id, with a
`team_ids` list and `latest_team_name` on each profile so wave 2 can map
either way):

```python
{
  "seasons": [2022, 2023, 2024, 2025],
  "managers": {
    "{SWID}": {
      "team_ids": [7], "latest_team_name": "...", "seasons": 4,
      "pos_bias": {"QB": 1.6, "RB": 1.0, "WR": 0.9, "TE": 0.6, "K": 1.0, "DST": 1.0},
      "first_pos_round": {"QB": 3.5, "TE": 9.0, "K": 14.0, "DST": 13.2},   # mean round of FIRST pick at pos
      "early_pos_rate": {"RB": 0.55, "WR": 0.35, ...},                        # share of rounds 1-4 picks
      "reach_index": 0.3,                    # mean (ADP-scaled pick - actual pick) in rounds 1-6, +ve = reaches
      "autopick_rate": 0.1,                  # share of picks flagged auto
      "favorite_nfl_team": {"team": "DAL", "count": 6} | None,
      "loyalty": {"norm player name": 3, ...},   # players drafted 2+ times
      "confidence": 0.8,                     # seasons/4 capped at 1
      "labels": ["QB-early", "TE-late", "homer:DAL"],
    }
  }
}
```

Definitions:
- `pos_bias[pos]` = manager's share of picks at pos in rounds 1-8, divided by
  the league-wide share for the same rounds, shrunk toward 1.0 by
  confidence: `1 + (raw-1)*confidence`. Clamp to [0.4, 2.5]. This is what
  `pos_multiplier` returns, further scaled by round: for QB/TE/K/DST, if
  `round_no < first_pos_round[pos] - 1.5` return `min(bias, 1.0) * 0.6`
  (they historically never take it this early), else `bias`.
- Recency: weight season s by `0.8 ** (current_year-1-s)` when averaging.
- ADP for `reach_index`: use `data/board.json` `adp_overall` × (teams/12)
  only for the current-year board (no historical ADP is available) — so
  compute reach_index only for the most recent season where a name matches
  the current board; otherwise set None. Document that limitation.
- `favorite_nfl_team`: needs a player→NFL team map; the current board gives
  it for names that still exist. Count across seasons; report if count ≥ 4.
- Labels: `QB-early` if first_pos_round[QB] ≤ 5; `TE-early` if ≤ 5;
  `TE-late` if ≥ 10; `RB-heavy`/`WR-heavy` if early_pos_rate ≥ 0.6;
  `zero-RB` if early_pos_rate[RB] ≤ 0.15; `homer:XXX`; `autopicker` if
  autopick_rate ≥ 0.5; `ADP-robot` if nothing else fires.

Also write a human-readable dossier to `reports/league_dna.md`: one section
per manager with team names by season, labels, the numbers above, and their
first-round picks by year. This is the "scouting report" John reads before
the draft.

## CLI

```
./venv/bin/python src/league_history.py pull --seasons 2021-2025   # network
./venv/bin/python src/league_history.py learn                      # offline
./venv/bin/python src/league_history.py show [--slot-order 3,9,7,...]
```

Run `pull` and `learn` for real at the end and paste the dossier summary
(labels per manager) in your report. If credentials fail, say so and stop —
do not fabricate history.

## Tests (`tests/test_league_history.py`)

No network. Build a synthetic 4-season history for 4 managers in code and
assert:

1. A manager who takes QB in round 2 every year gets `pos_bias["QB"] > 1.3`
   and label `QB-early`; `pos_multiplier(profile, "QB", 1)` is below 1.0
   (too early even for them) and `pos_multiplier(profile, "QB", 3)` > 1.
2. A manager with one season gets bias shrunk close to 1.0 (confidence 0.25).
3. Loyalty counts a player drafted in 3 seasons as 3.
4. `tendencies_by_slot([7, 3, ...])` maps team ids to slots using `team_ids`.
5. `learn([])` returns an empty-managers model without raising.

## Do not

- Do not change `draft_tracker.py`, `draft_live.py`, or `draft_sim.py`.
- Do not store SWIDs in the dossier markdown (use team names); the JSON model
  is gitignored under data/ so SWID keys there are fine.
