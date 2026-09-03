# Sweep — verification of waves 1 and 2

Everything below was re-run independently after the agents finished, not
taken from their reports.

## Verified

- `./venv/bin/python -m pytest tests/ -q` → 36 passed (snake math 7, injuries 6,
  lookahead 9, league history 7, scoring 7).
- `draft_live.py --once` on the on-disk state: ~306 ms total (rollout n=150 ≈ 260 ms).
- Self-play harness, old engine (pre-wave snapshot) vs new, six slots × five seeds:

  | seed | old mean proj | new mean proj | delta |
  |---|---|---|---|
  | 1 | 1686.4 | 1697.4 | +0.65% |
  | 2 | 1684.0 | 1699.5 | +0.92% |
  | 3 | 1695.1 | 1698.1 | +0.18% |
  | 4 | 1707.9 | 1709.3 | +0.08% |
  | 5 | 1704.4 | 1708.2 | +0.22% |
  | all | 1695.6 | 1702.5 | +0.41% |

  All 60 rosters legal. QB1 position rank stays ≥ 7 everywhere (the punt
  survives; the new engine waits slightly longer on QB than the old one).
- A full self-play walk at slot 7 with the `why` breakdown printed at rounds
  1/3/5/7/9/12/14/15: factors sum to the score; K/DST buried until forced;
  QB-punt escalation fires at R7 when the target pool is dying; the lookahead
  correctly prefers Dak (0% survival) over Purdy (71% survival) at that pick;
  Questionable-with-no-details injuries cost 2, not 6; bye clash and stack
  tiebreakers appear where expected.
- Dashboard: script block parses under JavaScriptCore (`osascript -l
  JavaScript`), new `.wait`/`.why-*` rows span columns 3..end of the existing
  six-column `.rec` grid. Not verified in a real browser — open
  `http://localhost:8123/draft_dashboard.html` once before draft night.

## Bugs found and fixed during the sweep

1. `league_history.tendencies_by_slot` mapped a reused team id to whichever
   profile iterated last. Team ids 4, 7, 11, 14 have changed hands; our own
   team id 7 resolved to the previous owner. Now prefers the most recent
   holder (`team_id_latest`). Test added.
2. Tendency labels used absolute rounds ("QB-early" = first QB by round 5),
   but the league-wide median first QB is round 5.3, so half the room was
   tagged. Labels are now relative to the league's own medians; homer needs
   ≥ 5 picks and ≥ 2.5× chance. Test added. Numeric `pos_bias` was already
   relative and is unchanged.
3. `injuries.load()` re-parsed the JSON on every `injury_for` call (40× per
   rebuild). Memoised on file mtime.
4. VONA's "likely R{n}" label was off by one round between turns.
5. The manual-backup rebuild in `mock_server.py` dropped `team_ids`, which
   silently turned off the league-DNA bias if the ESPN feed stalled on draft
   night. It now carries them over from the poller's state.
6. (Integration agent) `global ROLLOUT_N` after use in `main()`;
   `survival_odds`/`positional_need` lacked the `bias` parameters the new
   scoring loop called; the self-play harness was non-deterministic because
   the rollout seed was not pinned; and the first honest regression run was
   −1.66% because Need was scaled for the old VBD term — fixed by raising the
   Need coefficient from 6 to 18.

## Judgment calls John should know about

- **VBD leans RB.** With RB48 projecting ~107 pts vs WR48 ~164 in this
  full-PPR 16-team league, the VBD axis pulls RBs up. At pick 1 the engine
  now has Taylor/Cook/Achane above Chase/Nacua; at pick 7 Chase/Nacua are
  back above Taylor. It wins in self-play, but if you trust the UDK board
  more, lower `VBD_WEIGHT` in `src/draft_live.py` (0.40 → ~0.25).
  **Decision (John, 2026-09-03): keep it leaning RB. VBD_WEIGHT stays 0.40.**
- **Need coefficient 18** is what keeps a missing starter ahead of a
  bigger-VBD bench body. It was tuned on the harness, not on theory.
- **Questionable injuries** with no body part and no practice report count
  for a third of the full penalty (2 rank points). Preseason Sleeper data is
  mostly that.
- **Tendency bias only reaches the engine through the live poller** (which
  records `team_ids`) or the manual-backup path. Mocks run with no bias.

## Draft-night checklist additions

- Run `./venv/bin/python src/injuries.py --refresh` an hour before (the poller
  also does this once at startup).
- Run `./venv/bin/python src/league_history.py pull --seasons 2025 && learn`
  only if the league changed; the model on disk covers 2021-2025.
- `docs/plans/wave1-results.md` and this file explain every number in the
  `why` column.
