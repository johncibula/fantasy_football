# Plan 04 — Housekeeping

Read `docs/plans/00-overview.md` first. Small, precise edits only.

## 1. README.md is wrong in several places

- Says "half-PPR". The league is FULL PPR (verified in ESPN settings; see
  `config.yaml` `scoring: full_ppr`). Fix the first paragraph.
- The Components table lists `src/waivers.py`, `src/lineup.py`,
  `src/scout.py`, `src/report.py`, which do not exist. Replace the table with
  the modules that DO exist, one line each, accurate to their docstrings:
  `espn_client.py`, `draft_board.py`, `cheatsheet.py`, `draft_tracker.py`,
  `draft_live.py`, `draft_sim.py`, `self_mock.py`, `mock_server.py`,
  `mock_sync.py`, `espn_sync.py`, plus the wave-1 modules that will exist by
  the time you finish: `lookahead.py`, `injuries.py`, `league_history.py`
  (describe them from `docs/plans/01..03`). Keep the "Season rhythm" section
  but mark the waiver/lineup/scout tools as "planned".
- Mention `data/udk/*.csv` (the actual input) rather than `data/udk.xlsx`.
  Check `src/draft_board.py` for the real filenames.
- Add a "Tests" line: `./venv/bin/python -m pytest tests/ -q`.
- Add a "Plans" line pointing at `docs/plans/`.
- Keep the draft-night runbook; verify each command in it actually exists
  (`draft_live.py --poll`, `mock_server.py 8123`, `--year 2025` replay) by
  reading the argparse in those files. Fix anything stale.

## 2. Hardcoded team id in the live poller

`src/draft_live.py` `poll_loop` has `my_team_id = 7  # Bob's got a hog`.
Read it from config instead: `espn_client.get_config()["team_id"]`
(`espn_client` is already imported in `__main__`; import it at module level
inside `poll_loop` or at the top — but note `draft_live.py` is imported by
`self_mock.py` and `mock_server.py` which already insert `src` on sys.path and
import `espn_client` first, so a top-level `import espn_client` is safe).
Also make `--year` default to `get_config()["season"]` rather than the literal
2026. Touch nothing else in that file.

## 3. `.gitignore` hides source

`reports/` is ignored wholesale, but `reports/draft_dashboard.html` and
`reports/engine_guide.html` are hand-written source, not generated output.
Change the rule to `reports/*` and add negations for those two files so they
can be committed. Same for `data/`: keep it ignored but negate
`data/notes/` (John's scouting notes are source) — check with `git status`
that the negations take effect and that `.env` stays ignored.

## 4. config.yaml

- Add `draft.rounds: 15` next to `slot`, and a comment that `draft.slot` is
  filled in automatically by the poller once ESPN publishes the order.
- Add a `sources:` block documenting where the board comes from (UDK CSV
  export, full-PPR) so nobody re-exports in the wrong format.

## 5. Tests scaffold

Create `tests/__init__.py` (empty) and `tests/test_snake_math.py` covering
`draft_tracker.snake_team_for_pick` and `my_pick_numbers` for 16 teams
(slot 1 → picks 1, 32, 33, 64…; slot 16 → 16, 17, 48, 49…; every pick number
1..240 maps to exactly one slot, each slot gets 15 picks). Run
`./venv/bin/python -m pytest tests/ -q`.

## Report

List every line you changed. Paste `git status --short` before and after the
.gitignore change.
