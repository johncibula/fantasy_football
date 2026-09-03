# Plan 05 — Integrate everything into the scoring loop and dashboard

Read `docs/plans/00-overview.md` first, then plans 01-04 and the wave-1 report
notes in `docs/plans/wave1-results.md`. Wave 1 has landed: `src/lookahead.py`,
`src/injuries.py`, `src/league_history.py` exist with tests. This plan wires
them in. You own `src/draft_live.py`, `src/draft_tracker.py`,
`reports/draft_dashboard.html`, and `src/self_mock.py`.

Read `src/draft_live.py` `build_live` completely before starting. The score
is rank-space, LOWER = BETTER. Keep it that way.

## A. Refactor the scoring loop into an explainable function

Extract the per-candidate block in `build_live` (from `for p in candidates:`
to `recs.append`) into

```python
def score_candidate(p, ctx) -> tuple[float, list[dict]]
```

where `ctx` is a small dataclass/dict holding everything the loop currently
closes over (my_counts, picks_remaining, round_next, tagged pools, survival
numbers, baselines, lookahead result, injuries, etc.). Every term that moves
the score appends `{"label": str, "delta": float, "detail": str}` to the
factors list. Context-only rows (no score change) use `"delta": None`.
Labels to use, in this order when they fire: `UDK rank`, `Tags`, `Need`,
`VBD`, `VONA`, `Survival`, `Injury`, `Bye clash`, `Stack`, `K/DST timing`,
`QB plan`, `RB depth`, `WR surplus`, `TE plan`. The sum of non-None deltas
must equal the final score (add a test).

Each rec gains `"why": factors` (list) and keeps every existing key so the
dashboard and `self_mock.grade` keep working.

## B. VBD as a first-class axis

Currently `score = overall_rank + tag_bonus - need*6 - vbd*0.15` where VBD is
points over a hardcoded replacement (QB18/RB40/WR44/TE18). Change:

1. Derive replacement depth from `config.yaml` roster: for each position,
   `teams × starters` plus a flex share (`FLEX` counts as RB 0.45 / WR 0.45 /
   TE 0.10) plus a bench share (bench 6 × teams spread RB 0.35 / WR 0.35 /
   QB 0.1 / TE 0.1, rounded). Print the resulting depths from a small helper
   `replacement_depths(cfg)` and sanity-check they land near
   QB ~18, RB ~48, WR ~48, TE ~20 for this league.
2. Convert VBD to rank-space with a calibrated slope instead of 0.15: fit
   `slope = (rank_at_percentile(90) - rank_at_percentile(10)) /
   (vbd_at_percentile(10) - vbd_at_percentile(90))` over the top-150 board
   once (cache it), so that a full standard deviation of VBD moves the score
   roughly as much as a full SD of UDK rank does. Then use
   `score = 0.6*overall_rank_component + 0.4*vbd_rank_component`, where
   `vbd_rank_component = rank_of_best - vbd*slope` is expressed in rank units.
   Report the slope and show, for the top 30 board players, old score vs new
   score, so the sweep can see nothing absurd happened (e.g. a QB jumping to
   #1 because raw points are high — QBs must still land where UDK has them ±10).
3. The `need` term and tag bonuses stay additive in rank units.

## C. Wire the lookahead

In `build_live`, run `lookahead.rollout(state, board, n=N, seed=None,
pos_bias_by_slot=bias)` once per call. Choose N so `--once` stays under
~1.2s total on the early-draft state (measure; expect n≈150-200). Then:

- Replace the per-candidate `survival_odds` call with the rollout's
  `survival[norm_name]` (fall back to `survival_odds` if the name is missing).
  Keep `survival_odds` in `draft_tracker.py` for the tagged-pool expectations
  (`qb_exp_surv`, `te_exp_surv`) but switch those to sum the rollout
  survival too — same numbers, one source.
- Add a `VONA` factor: `lookahead.vona(p, result)`; convert to rank units with
  the same slope as VBD and weight 0.5. When VONA ≤ 0 (he'll be there, or an
  equal player will) the factor is a penalty; when large and survival < 0.5
  it's a bonus. Cap the absolute effect at 12 rank points.
- Emit `"next_best"` and `"lookahead_n"` in live.json so the dashboard can
  show "if you wait: RB → <name> (~x pts)".

## D. Wire injuries

`injuries.load()` once per `build_live`. For each candidate:
`pen = injuries.injury_penalty(name)`; add the factor and `"injury":
{"chip", "status", "body_part", "updated"}` (or None) to the rec. In
`poll_loop` call `injuries.refresh()` once at startup (never in the loop).
`--once` should NOT hit the network.

## E. Wire league tendencies

- `poll_loop` already has `order` (team ids by slot). Store it in state as
  `"team_ids": order` and compute `bias = league_history.tendencies_by_slot(order)`
  reduced to `{slot: {pos: multiplier}}` via `pos_multiplier(profile, pos,
  round_next)` for the six positions. Pass it to the rollout.
- Also feed it into `draft_tracker.positional_need` for the intervening
  teams: multiply need by the same multiplier (add an optional `bias` arg;
  default behaviour unchanged).
- For mock/self_mock states without `team_ids`, bias is None (no change).
- Add to live.json: `"dna": {slot: {"labels": [...], "team": name}}` for the
  TEAMS tab.

## F. Bye clash and QB stack tiebreakers (small)

- Bye clash: if the candidate's bye equals the bye of ≥2 of my current
  starters at RB/WR/TE (or my QB for a QB candidate), +3 rank points; if it
  would make 3+ starters share a bye, +6. Detail string names the week and
  the players.
- Stack: WR/TE candidate whose team matches my QB's team, or QB candidate
  whose team matches one of my WR/TE: −3. Only rounds ≥ 4 (never distort the
  early board).

## G. Dashboard

`reports/draft_dashboard.html` renders `live.json` (see `tick()` and the
`recs` template around line 249). Add, without changing the layout style:

- An injury chip next to the name (`Q`/`D`/`O`/`IR`, coloured like the
  existing `.surv.gone`) with the body part in a `title` tooltip.
- A collapsible "why" line under each rec (click the score or a small ▸)
  listing factors as `label ±delta` with the detail in a tooltip. Show the
  top 4 factors by |delta| inline, rest on expand.
- A "wait for" column: `next_best[pos].p50_name` + mean proj, so the user
  sees what taking someone else now costs.
- TEAMS tab: show `dna[slot].labels` under each team card.

Keep it vanilla JS, no libraries. Open it via `./venv/bin/python
src/mock_server.py 8123` and a mock (`/api/start?slot=7`) to make sure nothing
throws in the console; describe what you checked.

## H. Regression

1. Save the wave-1 baseline: `./venv/bin/python src/self_mock.py --batch
   "1,4,7,10,13,16" --seed 1` output is in
   `docs/plans/wave1-results.md`. After your changes, run it again. Every
   roster must be legal; the mean `proj` across the six slots must not drop by
   more than 1%; QB1 pos_rank must still be ≥ 5 in most slots (the punt
   survives). If the new engine is clearly better on proj, say so with the
   numbers; if worse, investigate before finishing.
2. `self_mock.py` runs `build_live` ~15 times per mock. With the rollout it
   will slow down. Add a `--fast` flag (or env var) that sets rollout n=40 for
   the batch harness, and report timings for both.
3. `tests/test_scoring.py`: (a) factors sum to score for every rec in a
   `--once` build; (b) a K never appears in the top 5 recs before round 13
   in an early-draft state; (c) an IR-tagged player (monkeypatch
   `injuries.load`) drops at least 30 rank points; (d) on-the-clock survival
   window semantics unchanged (compare against the pre-existing comment at
   `build_live` lines 84-90).
4. `./venv/bin/python src/draft_live.py --once` on the existing
   `data/draft_state.json` must run without error and print timing.

## Report

Files changed, the VBD slope and the top-30 before/after table, rollout N and
`--once` timing, self_mock before/after table, test output, and what you
verified in the browser.
