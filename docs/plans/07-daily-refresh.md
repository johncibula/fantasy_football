# 07 — Daily data refresh (UDK, market, injuries, player pages)

Runs every morning at 06:30 via launchd (`~/Library/LaunchAgents/com.f3.daily-refresh.plist`),
executing `src/daily_refresh.py`. If the Mac was asleep, launchd runs it at the
next wake. Logs: `data/logs/refresh.log` (one line per run), `data/logs/launchd.*.log`.

## Steps, in order

| step | module | source | needs |
|---|---|---|---|
| injuries | `injuries.py --refresh` | Sleeper public API | nothing |
| udk | `udk_fetch.py` | FootClan UDK CSV export (Playwright) | UDK login |
| market | `market_feed.py --refresh` | ESPN player universe (rank/ADP/ownership) + FFC ADP | ESPN cookies in .env |
| pages | `player_pages.py --top 200` | thefantasyfootballers.com/fantasy/<slug>/ | UDK login |
| board | `draft_board.py` | rebuild `data/board.json` from CSVs + market | — |
| diff | in `daily_refresh.py` | `reports/udk_changes.md`: movers, tier changes, projection swings, adds/drops | — |

Each step is independent; a failure is logged and the rest still run.

## UDK login (one time)

Add to `.env`:

```
UDK_EMAIL=you@example.com
UDK_PASSWORD=...
```

The fetcher logs in headlessly, saves the session to `data/udk_session.json`,
and re-logs in whenever the session expires. Without credentials, run
`./venv/bin/python src/udk_fetch.py --login` once and log in by hand.

The FootClan login form and the "More → Download CSV" control were written
from the public help article, not from a logged-in DOM. **The first run must
be watched**: `./venv/bin/python src/udk_fetch.py --only rb --headed`. If a
selector misses, a screenshot and HTML land in `data/udk_debug/`.

## Market data

The room drafts off ESPN's board. `market_feed.py` records, per player,
ESPN's draft rank, ADP (None when undrafted in ESPN drafts, which ESPN reports
as ~170), ownership %, and FFC ADP. `espn_order` is the room's board position
(ADP first, then ESPN rank) and is what the engine uses:

- `draft_board.py` merges it as `espn_rank`/`espn_adp`/`market_delta`
  (= room position − UDK rank, only inside the 240-pick window).
- `draft_sim.py` bots draft off the room board, not the UDK blend. This is
  the biggest realism change: opponents now take QBs when ESPN says, not when
  the Ballers say.
- `draft_tracker.survival_odds` (analytic fallback) uses ESPN ADP.
- `draft_live.py` adds a **Market** context row to the why breakdown; the
  dashboard shows a `ROOM ±n` chip (green = the room lets him slide, wait;
  red = the room takes him early).

`./venv/bin/python src/market_feed.py --top 30` prints the biggest gaps.

## Player pages

`player_pages.py` pulls each top-200 player's page with the UDK session, keeps
the readable text and first substantial paragraph as `summary`, and the
dashboard shows it (or the UDK CSV outlook as fallback) under ▸ more with a
"profile ↗" link. Pages older than 72h refetch; ~1s between pages. Like the
CSV export, the extraction is written blind; check `data/players/*.json`
after the first run and tighten `extract()` if the summary is boilerplate.

## Checks

```
./venv/bin/python -m pytest tests/ -q
./venv/bin/python src/daily_refresh.py --no-udk     # what the job does, minus the browser
tail data/logs/refresh.log
launchctl print gui/$(id -u)/com.f3.daily-refresh | head
```

Restart the mock/dashboard server after a refresh — it loads the board once
at startup.
