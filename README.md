# Fantasy Football Toolkit

Tools for winning a 16-team, full-PPR redraft league on ESPN, powered by
Fantasy Footballers UDK rankings.

## Components

| Module | Purpose |
|---|---|
| `src/espn_client.py` | Shared entry point for the ESPN league connection — run directly to verify settings |
| `src/draft_board.py` | UDK CSV exports → unified tiered board (`data/board.json`) |
| `src/cheatsheet.py` | Renders `data/board.json` into the draft-day cheat sheet (`reports/cheatsheet.html`) |
| `src/draft_tracker.py` | Live draft state, best-available, pick-survival predictions (CLI: start/pick/undo/status/advise) |
| `src/draft_live.py` | Polls ESPN's draft feed (or replays mock/manual state) and writes `reports/live.json` for the dashboard |
| `src/draft_sim.py` | AI opponents for local mock drafts |
| `src/self_mock.py` | Self-play mocks — the rec engine drafts our slot, `draft_sim` plays the other 15, grades the roster |
| `src/mock_server.py` | Local server that serves the dashboard and runs mock drafts |
| `src/mock_sync.py` | Rebuilds draft state from a scraped, ordered pick list (mock-draft feed) |
| `src/espn_sync.py` | Syncs ESPN draft-room picks (pasted from stdin) to state + `live.json` |
| `src/lookahead.py` | Monte Carlo rollout of the remaining draft — survival odds and value-over-next-available (see `docs/plans/01-lookahead.md`) |
| `src/injuries.py` | Live injury status from Sleeper's public player API, used to penalize hurt players (see `docs/plans/02-injuries.md`) |
| `src/league_history.py` | Learns F³ managers' drafting tendencies from past ESPN drafts (see `docs/plans/03-league-history.md`) |

Planned, not yet built: `src/waivers.py`, `src/lineup.py`, `src/scout.py`, `src/report.py`.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Fill in `.env` (never committed):

```
LEAGUE_ID=     # from the ESPN league URL (leagueId=...)
ESPN_S2=       # browser cookie (DevTools > Application > Cookies > fantasy.espn.com)
SWID=          # same place, includes the curly braces
UDK_EMAIL=     # FootClan login, so the daily job can export the UDK CSVs
UDK_PASSWORD=  # and pull player pages (see docs/plans/07-daily-refresh.md)
```

Then verify: `./venv/bin/python src/espn_client.py`

Drop the downloaded UDK CSV exports at `data/udk/`: `qb.csv`, `rb.csv`,
`wr.csv`, `te.csv` (tiered position rankings), `flex.csv` (combined RB+WR+TE
rank), `dst.csv`, `k.csv` (simple rankings), `top200.csv` (overall rank plus
Andy/Jason/Mike) — see `src/draft_board.py` for the exact columns expected.

## Draft night runbook (Sun Sep 6, 7:00 PM — 60s per pick)

1. Start the server: preview `draft-server` (or `./venv/bin/python src/mock_server.py 8123`)
2. Start the live feed: `./venv/bin/python src/draft_live.py --poll` (auto-detects draft order,
   team names/owners, and our slot once ESPN generates the order ~1hr before)
3. Open `http://localhost:8123/draft_dashboard.html` beside the ESPN draft room
4. BOARD tab = recommendations + survival; TEAMS tab = all rosters + market read
5. If the feed ever stalls ("feed stale"), use the MANUAL BACKUP bar: click TAKEN
   on players in draft order — the engine keeps working at full strength
6. Dry-run any time: `src/draft_live.py --poll --year 2025` replays last year's real draft

## How the engine scores a pick

Every recommendation carries a `why` list (visible on the dashboard as the
factor chips under each name). Scores are rank-space, lower is better, and the
factors sum exactly to the score:

- **UDK rank × 0.60** and **VBD × 0.40** — the two value axes. VBD is projected
  points over the 16-team replacement level (QB18 / RB48 / WR48 / TE20,
  derived from `config.yaml`), converted to rank units. Weights are
  `RANK_WEIGHT` / `VBD_WEIGHT` in `src/draft_live.py`; VBD leans RB in full PPR,
  lower it toward 0.25 to trust the UDK board more.
- **Need**, **Tags** (your notes), **VONA** (points over the best player the
  Monte-Carlo lookahead expects at that position at your next pick, weighted by
  the chance he is gone), **Injury** (Sleeper designation; a bare
  "Questionable" counts for a third), **Bye clash**, **Stack**, and the plan
  rails: K/DST timing, QB punt, RB depth, WR surplus, TE great-or-late.
- **Survival** is the lookahead's simulated odds he reaches your next pick.
  On draft night the bots are biased by each manager's learned tendencies
  (`data/tendencies.json`, see `reports/league_dna.md`).

Self-play harness: `./venv/bin/python src/self_mock.py --batch "1,4,7,10,13,16" --seed 1`
(`--fast` lowers the rollout for a quicker batch). The sweep that verified all
of this, with old-vs-new numbers, is in `docs/plans/06-sweep.md`.

## Daily refresh (automatic, 06:30)

A launchd job runs `src/daily_refresh.py` every morning: Sleeper injuries, the
UDK CSV export (logged-in browser), ESPN/FFC market rankings, top-200 player
pages, board rebuild, and `reports/udk_changes.md` listing who moved. Check
`data/logs/refresh.log`. Run it by hand with `./venv/bin/python
src/daily_refresh.py` (`--no-udk` skips the browser). Restart the dashboard
server after a refresh. Details: `docs/plans/07-daily-refresh.md`.

The market data is what the other 15 managers see in the ESPN draft app. The
bots in mocks and the survival lookahead draft off it, the `ROOM ±n` chip on
each recommendation shows how far the room is from the UDK rank (green: the
room lets him slide, you can wait; red: the room takes him early), and the QB
plan fires when the room is about to take the last starter-quality QBs.

## Season rhythm

- **Tue AM** — waiver report + week recap (planned: `src/waivers.py`)
- **Thu PM** — lineup check before TNF (planned: `src/lineup.py`)
- **Sun AM** — final start/sit sweep (planned: `src/lineup.py`)
- Weekly roster snapshots feed the League Intel report (planned: `src/scout.py`, `src/report.py`).

## Tests

`./venv/bin/python -m pytest tests/ -q`

## Plans

Ongoing engine work is tracked in `docs/plans/` (start at `00-overview.md`).
