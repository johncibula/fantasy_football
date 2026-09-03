# Draft engine improvements — overview

Seven improvements identified by comparing this repo to a friend's draft
assistant (Project Shredder). They are implemented in two waves so agents never
edit the same scoring loop concurrently.

## Ground rules for every agent

- Work only inside this repo (`/Users/johncibula/Desktop/fantasy_football`).
  Use `./venv/bin/python` for everything. `pytest` and `requests` are installed.
- Do NOT commit. Do not touch `.env`, `data/udk/`, `data/notes/`, or
  `data/board.json`. Do not rebuild the board.
- League facts: 16 teams, FULL PPR (1.0/rec), 15 rounds, our ESPN team_id 7,
  roster QB1 RB2 WR2 TE1 FLEX1 DST1 K1 + 6 bench, no FAAB. All in `config.yaml`.
- Scores in this engine are LOWER = BETTER (they are rank-space). Penalties are
  positive numbers added to the score; bonuses are subtracted.
- Player name matching: always go through `draft_tracker.norm_name`. D/ST
  entries on the board are named like "Houston Texans" and aliased under
  "texans", "texans d/st", etc. Several board keys point at the same dict, so
  dedupe by `id(p)` when iterating `board.values()` (see `draft_sim.available`).
- Tests: put them in `tests/test_<module>.py`. Start each test file with

  ```python
  import sys, pathlib
  sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
  ```

  Run with `./venv/bin/python -m pytest tests/ -q`. Tests must not hit the
  network; use fixtures or monkeypatching.
- Before finishing, run the regression harness and confirm it still completes
  with all six rosters legal:

  ```
  ./venv/bin/python src/self_mock.py --batch "1,4,7,10,13,16" --seed 1
  ./venv/bin/python src/draft_live.py --once
  ```
- Your final message must list: files created/changed, how to run it, test
  output, timing numbers where the plan asks for them, and anything you could
  not finish or verify.

## Wave 1 (parallel, disjoint files)

| Plan | New module | Touches existing? |
|---|---|---|
| 01 lookahead | `src/lookahead.py` | `src/draft_sim.py` (perf only, behaviour-preserving) |
| 02 injuries | `src/injuries.py` | nothing |
| 03 league history | `src/league_history.py` | nothing |
| 04 housekeeping | — | `README.md`, `.gitignore`, `src/draft_live.py` (team_id line only), `config.yaml` |

## Wave 2 (single agent, after wave 1)

| Plan | Touches |
|---|---|
| 05 integration | `src/draft_live.py`, `src/draft_tracker.py`, `reports/draft_dashboard.html`, `src/self_mock.py` |

Wave 2 wires lookahead, injuries, tendencies, VBD, bye/stack tiebreakers, and
the per-player "why" breakdown into the scoring loop and dashboard.
