# Working in this repo

Narrative and runbooks for agents. The enforced gate is in `CLAUDE.md`; code
style is in `.claude/skills/shared-refs/code-style.md`. This file holds neither.

## What this is

Tools for winning a 16-team, full-PPR redraft league on ESPN, built on the
Fantasy Footballers UDK rankings. `README.md` has the module-by-module table
and is the place to look first; the short version is three groups of scripts:

- **Board building** — `src/draft_board.py` turns the UDK exports into the
  tiered board, `src/cheatsheet.py` renders it, `src/udk_fetch.py` and
  `src/market_feed.py` pull the source data.
- **Draft night** — `src/draft_live.py` polls the ESPN feed and writes the
  dashboard payload, `src/draft_tracker.py` holds the state and the
  recommendations, `src/lookahead.py` runs the survival rollouts,
  `src/injuries.py` and `src/league_history.py` feed the scoring.
- **Practice and support** — `src/draft_sim.py` and `src/self_mock.py` for
  mock drafts, `src/mock_server.py` for the local dashboard,
  `src/espn_sync.py` and `src/mock_sync.py` for recovering state by hand.
- **Public app** — `src/draft_app.py` (Streamlit) runs the same engine on a
  visitor's own rankings: `src/rankings_import.py` fills the board schema
  from any CSV, `src/draft_session.py` holds one draft in memory for any
  league size, `src/sample_rankings.py` regenerates the bundled ESPN sample.
  It must never read the owner's board, notes, player pages or tendencies;
  the "Public app" section of `README.md` has the deploy checklist.

The draft-night runbook, the setup steps, and the breakdown of how a pick is
scored are all in `README.md`. Ongoing engine work is planned in `docs/plans/`,
starting at `docs/plans/00-overview.md`.

## Ground rules

These come from the "Ground rules for every agent" section of
`docs/plans/00-overview.md`. Read that section before starting engine work;
what follows is the short form.

- Use `venv/bin/python` for everything. Do not install into the system Python.
- Leave these alone: `.env`, `data/udk/`, `data/notes/`, and `data/board.json`.
  Do not rebuild the board. The notes and the exports are the owner's, and
  the board is derived from them.
- Do not commit unless you were asked to. The plan documents assume the owner
  commits.
- League facts live in `config.yaml`: 16 teams, full PPR, 15 rounds, our ESPN
  team is number 7.
- **Scores are rank-space and lower is better.** Penalties add to a score and
  bonuses subtract from it. This trips people up constantly, so check the sign
  of anything you add to the scoring loop.
- **Match player names through the tracker's normaliser**, never by raw string
  comparison. Team defences carry several aliases pointing at one entry, so
  iterate by object identity when walking the board rather than by key.

## Tests

`venv/bin/python -m pytest tests/ -q` runs the engine tests. Put new ones in
`tests/` named after the module they cover, and open each file with the search
path line the other tests use.

**Tests must not reach the network.** Use recorded data or replace the network
call in the test.

`make test` runs the engine tests together with the quality tooling's own
tests, and is what the shared build runs.

## Before you finish engine work

Run the regression harness and confirm it still completes with every roster
legal:

```
venv/bin/python src/self_mock.py --batch "1,4,7,10,13,16" --seed 1
venv/bin/python src/draft_live.py --once
```

Then say what you changed, how to run it, and anything you could not verify.

## Code style

Style rules are in `.claude/skills/shared-refs/code-style.md`. Read it before
writing code. It is the only place style rules live: this file has none, and
neither does any other document here. If you find a style rule written down
somewhere else, that is a defect — delete it and point at the style doc.
