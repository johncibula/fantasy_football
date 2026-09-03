# Plan 02 — Live injury status from Sleeper (`src/injuries.py`)

Read `docs/plans/00-overview.md` first.

## Why

The board is a static UDK export. A player who got hurt this week still ranks
at full value. Sleeper's public player endpoint is free, needs no auth, and
carries `injury_status`, `injury_body_part`, `injury_notes`,
`practice_participation`, and `news_updated` for every NFL player.

Endpoint: `GET https://api.sleeper.app/v1/players/nfl` (~5 MB JSON, keyed by
Sleeper player id; each value has `full_name`, `first_name`, `last_name`,
`position`, `team`, `injury_status`, `injury_body_part`, `injury_notes`,
`practice_participation`, `news_updated` (ms epoch), `status` (Active/Inactive),
`fantasy_positions`). Fetch it at most once a day.

## Deliverable

`src/injuries.py`:

```python
CACHE = DATA / "injuries.json"          # DATA from draft_tracker

def refresh(force: bool = False, timeout: float = 20.0) -> dict
    # Fetches Sleeper if cache is missing or older than 12h (or force).
    # Filters to fantasy positions QB/RB/WR/TE/K (DST has no injury status)
    # and to players with a non-empty injury_status OR status != "Active".
    # Writes CACHE as {"fetched_at": iso, "players": {norm_name: {...}}}.
    # On any network/parse failure: keep the existing cache, return it, and
    # print one warning line. Never raise.

def load() -> dict            # cached dict (empty {"players": {}} if none)

def injury_for(name: str) -> dict | None
    # {"status": "Questionable", "chip": "Q", "body_part": "hamstring",
    #  "note": "...", "practice": "DNP", "updated": "2026-09-01",
    #  "penalty": 6.0, "severity": "quest"}
```

Status → chip / penalty (rank-space, positive = worse; lower score is better
in this engine). Scale so a top-40 player with a season-ending designation
drops out of the recommendation list but a Questionable tag only nudges:

| Sleeper status | chip | severity | penalty |
|---|---|---|---|
| IR, PUP, NFI, Out (with `status` Inactive) | IR / PUP / O | out | 60 |
| Out | O | out | 30 |
| Suspended / Sus | SUS | out | 25 |
| Doubtful | D | doubt | 15 |
| Questionable | Q | quest | 6 |
| DNR / other non-empty | ? | quest | 6 |
| empty | — | none | 0 |

Add `injury_penalty(name) -> float` returning 0 when unknown.

Name matching: index by `draft_tracker.norm_name(full_name)`. Also add a
second key of `norm_name(first_name + " " + last_name)` when it differs. The
board uses names like "Kenneth Walker III" and "Marvin Harrison Jr."; the
existing `norm_name` strips suffixes so both sides line up.

Freshness: `injury_for` should include an `updated` date string derived from
`news_updated` when present. Data older than the cache's `fetched_at` by more
than 14 days is still returned (it is what Sleeper says) but flag
`"stale": True`.

## CLI

`./venv/bin/python src/injuries.py [--refresh] [--top 50]`:
- refreshes if asked or stale
- prints how many board players (from `data/board.json`) carry a designation,
  then a table of those players sorted by UDK `overall_rank`: rank, name, pos,
  chip, body part, practice, updated, penalty.

Run it once for real at the end and paste the table into your report (network
is allowed for the CLI, not for tests).

## Tests (`tests/test_injuries.py`)

Monkeypatch the fetch to return a small fake Sleeper payload; point `CACHE` at
`tmp_path`. Cover:

1. A Questionable player maps to chip Q, penalty 6.
2. IR maps to penalty 60 and severity out.
3. A healthy player (`injury_status` empty, status Active) is not in the cache
   and `injury_penalty` returns 0.
4. Name normalisation: payload name "Marvin Harrison Jr." resolves for
   `injury_for("marvin harrison")` and `injury_for("Marvin Harrison Jr")`.
5. Cache freshness: a cache younger than 12h is not refetched (assert the
   patched fetch is not called); `force=True` refetches.
6. Network failure with an existing cache returns the old cache unchanged.

## Do not

- Do not wire this into `draft_live.py` or the dashboard; wave 2 does that
  using `injury_for` / `injury_penalty`.
- Do not commit `data/injuries.json` (data/ is gitignored anyway).
