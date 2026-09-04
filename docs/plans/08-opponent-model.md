# 08 — Opponent model: managers, replay validation, draft-order tools

## What the bots are now

`draft_sim.ai_pick_fast` draws from the top-14 of the **room's board** (ESPN
order from `market_feed`), weighted by closeness to the top, the bot's own
positional need, and three learned layers:

| layer | source | where |
|---|---|---|
| manager habits | `data/tendencies.json` (2021-2025, recency-weighted) | `league_history.bias_for_slots` → `ai_pick_fast(bias=)` |
| league demand | `data/league_bias.json` (replay-calibrated, 2023-2024) | `draft_sim.league_bias()` |
| position timing | constants in `draft_sim.py` (see below) | `KDST_ROUNDS_WINDOW`, `KDST_RAMP_BASE`, `SECOND_QB_TE_*` |

Managers reach the bots whenever the state carries `team_ids`: the live poller
records them, and mocks borrow a past season's order (`/api/start?slot=N&order=2025`,
`self_mock.py --order 2025`). `&shuffle=1` / `--shuffle` keeps the managers and
randomizes their slots. The dashboard shows who is on the clock and their labels,
and the TEAMS tab shows every manager's labels.

## Replay validation (`src/replay.py`)

`./venv/bin/python src/replay.py --year 2025` freezes the real 2025 draft at each
of our picks, rolls the lookahead to our next pick, and scores its survival
predictions against what the room actually did. Habits are learned from seasons
before the replayed one only. `--calibrate 2023-2024` learns the league demand
multipliers from those seasons (2022's ESPN pool is missing half the drafted
players and is unusable).

Held-out 2025 results (Brier: mean squared error, 0.25 = coin flip):

| bots | Brier |
|---|---|
| before today (K/DST only in the last 6 rounds, QB2 only in the last 5) | 0.0152 |
| windows opened + K/DST ramp | 0.0134 |
| + league demand multipliers | 0.0136 |

Calibration is good in the tails (players predicted 0-10% to survive survived
15%; 80-90% survived 92%). The bots still under-predict this room's appetite
for QBs and TEs in 2025 (25 QBs taken between our picks vs 15 predicted; 22 TEs
vs 14) but matched it in 2023-2024, so it is year-to-year variance, not a
stable multiplier. The QB plan reads the live market for exactly this reason.

## Constants (draft_sim.py)

- `KDST_ROUNDS_WINDOW = 9`: bots consider K/DST once 9 rounds remain (round 7+).
- `KDST_RAMP_BASE = 0.08`: K/DST weight = base × (rounds into window)², vs ~1.0
  for the top market player. Chosen on the 2025 replay grid.
- `SECOND_QB_TE_ROUNDS = 9`, `SECOND_QB_TE_DAMP = 0.5`: a second QB/TE is
  damped ×0.5 before the last 9 rounds, ×0.08 was the old hard rule.

## Engine batch, real managers in 2025 slots (reports/batch15.json)

15 self-play drafts, five random slots per third:

| slots | mean starters proj | mean rank |
|---|---|---|
| 1-6 | 1766 | 1.2 |
| 7-11 | 1709 | 2.6 |
| 12-16 | 1702 | 3.2 |

## Draft night

When ESPN posts the order, the poller records `team_ids` automatically. To
rehearse from the real slot first: `/api/start?slot=<yours>&order=2025` gives
last year's seating; the true seating arrives with the order.
