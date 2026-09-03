# Plan 01 — Monte Carlo lookahead (`src/lookahead.py`)

Read `docs/plans/00-overview.md` first.

## Why

Today `draft_tracker.survival_odds` is an analytic guess: per intervening pick,
take-probability = ADP pressure × that team's positional need. It never
accounts for the fact that a team takes exactly one player, and it cannot say
what will be left at our next pick. The real decision each turn is:

    take X now   vs.   take Y now and get the best remaining at X's position next turn

That needs an expectation over the other 15 teams' picks. We already have a
bot model in `src/draft_sim.py` (`ai_pick`, `sim_until_my_turn`). Roll it
forward many times and count.

## Deliverable

`src/lookahead.py` exposing:

```python
def rollout(state: dict, board: dict, n: int = 200, seed: int | None = None,
            pos_bias_by_slot: dict[int, dict[str, float]] | None = None,
            horizon_picks: int = 2) -> dict
```

Returns:

```python
{
  "n": n,
  "next_mine": <our next pick number or None>,
  "after": <our pick after that or None>,
  "survival": {norm_name: p_available_at_next_mine},        # for every available player
  "survival_after": {norm_name: p_available_at_after},      # same, for the pick after
  "next_best": {pos: {"mean_proj": float, "mean_rank": float,
                      "p50_name": str}},                    # best available at that pos at next_mine
  "elapsed_ms": float,
}
```

Semantics:
- If it is currently OUR pick (`snake_team_for_pick(pick_no) == slot`), the
  rollout starts by having the bots take picks `pick_no+1 .. next_mine-1`
  where `next_mine` is our pick AFTER the current one, i.e. answers "if I pass
  on him now, is he there next round". Match the window logic already in
  `draft_live.build_live` (lines ~84-90).
- If it is not our pick, roll from `pick_no` to our upcoming pick.
- `horizon_picks=2` means also record survival at the pick after next
  (needed for QB/TE punt logic). Rolling to the second pick requires a stand-in
  for OUR pick in between: use `best_available(...)[0]` by overall rank,
  excluding K/DST. Do not use `build_live` for that (circular + slow).
- `next_best[pos]`: at `next_mine` in each rollout, the best available player
  at `pos` by `overall_rank` (fallback `flex_rank`, then `pos_rank`). Report
  mean `proj_points`, mean `overall_rank`, and the most frequent name.
- `pos_bias_by_slot`: optional `{slot: {pos: multiplier}}`. When present,
  multiply the bot's weight for a candidate by `bias[pos]` for the slot on the
  clock. This is the hook for learned tendencies (plan 03). Default 1.0.

Also expose a convenience:

```python
def vona(candidate: dict, result: dict) -> float | None
```

Value Over Next Available: `candidate["proj_points"] - next_best[pos]["mean_proj"]`.
Positive means taking him now beats waiting. None if no projection.

## Performance budget (hard requirement)

`draft_live.py --poll` rebuilds live.json every 2s during the real draft, and
`self_mock.py` runs ~15 build_live calls per mock. Target on this machine:

- `rollout(n=200)` from an early-draft state (pick ~20, 15 picks to ours)
  **must finish in ≤ 1.0s**, and ≤ 1.5s is the absolute ceiling. Report the
  number.
- `draft_sim.ai_pick` is the hot path. It currently rebuilds `available()` by
  scanning the whole board for every pick and sorts ~300 players each call.
  Fix it without changing behaviour:
  - Build the deduped, market-ranked available list ONCE per rollout and pass
    it in; maintain a `gone` set; slice the top window from the sorted list.
  - Add a fast entry point (e.g. `ai_pick_fast(counts, avail_sorted, gone,
    rounds_left, rng, bias=None)`) and keep the old `ai_pick` signature working
    as a thin wrapper so `self_mock.py` and `mock_server.py` are untouched.
  - `roster_of` in `draft_tracker` rescans all picks; inside the rollout keep
    per-slot position counts incrementally instead.
- Use `random.Random(seed)` so results are reproducible in tests.

## Tests (`tests/test_lookahead.py`)

Use `data/board.json` via `draft_tracker.load_board()` (it exists and is
static) and a synthetic state (`{"teams":16,"slot":7,"rounds":15,"picks":[...]}`).

1. Survival probabilities are in [0,1]; a player already drafted is absent.
2. Monotonic sanity: the #1 overall available player has lower survival than
   the #60 available player from an early state.
3. On-the-clock semantics: when it is our pick, the survival window starts at
   the pick after ours (the player we could take now is not "taken" by us).
4. `next_best` has entries for QB/RB/WR/TE with `mean_proj > 0`.
5. `pos_bias_by_slot` with `{s: {"QB": 50.0}}` for every other slot makes QB
   survival drop versus the unbiased run.
6. Reproducibility: same seed, same output.
7. A timing test asserting `rollout(n=200)` < 1.5s (mark it so it is obvious
   it is a perf gate).

## CLI

`./venv/bin/python src/lookahead.py [--n 200] [--seed 1]` loads
`data/draft_state.json` + board, runs a rollout, prints elapsed time, the top
15 available players with survival at next and after, and the `next_best`
table. Handy for the sweep.

## Do not

- Do not modify `draft_live.py` or `draft_tracker.py` (wave 2 wires this in).
- Do not change what `ai_pick` picks for a given RNG state, other than through
  the new `bias` argument. Run `self_mock.py --batch "1,4,7,10,13,16" --seed 1`
  before and after and confirm the printed lines are identical.
