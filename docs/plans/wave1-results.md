# Wave 1 results (for the integration agent)

All four wave-1 plans landed. Full suite: `27 passed`. Self-play harness output is
byte-identical to the pre-wave baseline (below).

## Baseline: `./venv/bin/python src/self_mock.py --batch "1,4,7,10,13,16" --seed 1`

```
slot  1: rank  1/16  proj 1721.0 (top 1721.0)  QB7 TE21  tags 4 busts 0  val +409  legal Y  {'RB': 4, 'WR': 5, 'QB': 2, 'TE': 2, 'DST': 1, 'K': 1}
slot  4: rank  1/16  proj 1744.6 (top 1744.6)  QB9 TE20  tags 4 busts 0  val +385  legal Y  {'RB': 4, 'WR': 5, 'QB': 2, 'TE': 2, 'DST': 1, 'K': 1}
slot  7: rank  2/16  proj 1689.1 (top 1717.1)  QB9 TE21  tags 4 busts 0  val +372  legal Y  {'WR': 6, 'RB': 4, 'QB': 1, 'TE': 2, 'DST': 1, 'K': 1}
slot 10: rank  1/16  proj 1719.9 (top 1719.9)  QB7 TE2   tags 4 busts 0  val +371  legal Y  {'WR': 6, 'TE': 2, 'RB': 4, 'QB': 1, 'DST': 1, 'K': 1}
slot 13: rank 12/16  proj 1591.9 (top 1733.6)  QB7 TE21  tags 7 busts 0  val +412  legal Y  {'RB': 4, 'WR': 5, 'QB': 2, 'TE': 2, 'DST': 1, 'K': 1}
slot 16: rank  3/16  proj 1652.1 (top 1772.9)  QB13 TE1  tags 4 busts 0  val +374  legal Y  {'RB': 4, 'TE': 2, 'WR': 5, 'QB': 2, 'DST': 1, 'K': 1}
```
Mean proj across the six slots: 1686.4. Wall time ~3.7s for the batch.

## 01 lookahead — `src/lookahead.py`
- `rollout(state, board, n=200, seed=None, pos_bias_by_slot=None, horizon_picks=2)`
  returns `{n, next_mine, after, survival{norm_name:p}, survival_after{...},
  next_best{pos:{mean_proj, mean_rank, p50_name}}, elapsed_ms}`.
- `vona(candidate, result)` -> proj_points - next_best[pos].mean_proj, or None.
- Timing: n=200 ≈ 356-474ms depending on how many picks sit between our turns.
- `draft_sim.ai_pick_fast(counts, avail_sorted, gone, rounds_left, rng, bias=None)`
  is the hot path; `ai_pick` is now a wrapper. `bias` is `{pos: multiplier}`
  for the slot on the clock.
- On-the-clock semantics match `build_live`'s window comment (lines 85-91).
- CLI: `./venv/bin/python src/lookahead.py --n 200 --seed 1`.

## 02 injuries — `src/injuries.py`
- `refresh(force=False, timeout=20)` (network, cache 12h at `data/injuries.json`),
  `load()`, `injury_for(name)` -> dict with `status, chip, body_part, note,
  practice, updated, penalty, severity, stale` or None, `injury_penalty(name)` -> float.
- Penalties: IR/PUP/Out+Inactive 60, Out 30, Sus 25, Doubtful 15, Questionable 6.
- Real pull on 2026-09-03: 32 of the top-100 board players carry a
  designation, all Questionable (preseason noise). A flat +6 for "Q" on 32 of
  100 players is a lot of noise; the integration should apply Questionable
  only when `practice` is DNP/LP or `body_part` is not "Undisclosed", or scale
  it down (e.g. 2 for Q with no practice data). Use judgement and explain.
- `data/injuries.json` exists now, so `--once` can use it offline.

## 03 league history — `src/league_history.py`
- Seasons 2021-2025 pulled to `data/history/draft_*.json`; model at
  `data/tendencies.json`; dossier at `reports/league_dna.md`.
- `load_tendencies()`, `pos_multiplier(profile, pos, round_no)`,
  `tendencies_by_slot(order: list[int]) -> {slot: profile}` (profiles keyed by
  owner SWID internally; `team_ids` on each profile does the mapping).
- CAVEAT for the sweep, not for you: nearly every manager got `QB-early` /
  `TE-early` / `homer:XXX`. Thresholds were tuned for 12-team rounds and this is
  a 16-team league, so labels are probably over-firing. Use `pos_bias` (the
  ratio, shrunk by confidence) rather than the labels for anything numeric.
  Labels are display-only on the TEAMS tab.
- Team id 11 shows an owner change in 2024 and is split into two profiles.

## 04 housekeeping
- README rewritten, `.gitignore` now `data/*` + `!data/notes/`, `reports/*` +
  `!reports/draft_dashboard.html` + `!reports/engine_guide.html`.
- `draft_live.py` now does `import espn_client` at module level and reads
  `team_id` / `season` from config (`espn_client.get_config()`).
- `config.yaml` gained `draft.rounds: 15` and a `sources:` block.
- `tests/test_snake_math.py` (7 tests).

## Sweep fixes applied to wave 1 (before integration finished)
- `league_history.tendencies_by_slot` now prefers the most recent holder of a
  reused team id (ids 4, 7, 11, 14 changed hands; team 7 used to map to the
  previous owner). Profiles carry `team_id_latest: {tid: season}`.
- Labels are relative to the league's own medians (`league_first_pos_round`
  in the model: QB 5.3, TE 5.8, K 11.6, DST 10.5). Homer requires >= 5 picks
  and >= 2.5x the per-team expectation. Two tests added (7 total).
