import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import json

import pytest

import draft_live as dl
import draft_tracker as dt
import injuries
import lookahead

TEAMS = 16
ROUNDS = 15

BOARD = dt.load_board()


def fresh_state(slot: int = 7) -> dict:
    """Nothing drafted yet: pick 1 on the clock, our slot's first turn is
    pick `slot`. The purest "early-draft state" for sanity checks."""
    return {"teams": TEAMS, "slot": slot, "rounds": ROUNDS, "picks": []}


def make_ctx(state: dict, board: dict, inj: dict | None = None,
             rollout_n: int = 60, seed: int = 1) -> dl.ScoreCtx:
    """Build a ScoreCtx the same way build_live does, for tests that want to
    call score_candidate() directly instead of going through the full
    build_live()/recs pipeline (e.g. to inspect a candidate outside the
    top-14 that gets returned)."""
    pick_no = len(state["picks"]) + 1
    teams, slot, rounds = state["teams"], state["slot"], state["rounds"]
    mine = dt.my_pick_numbers(slot, teams, rounds)
    upcoming = [p for p in mine if p >= pick_no]
    next_mine = upcoming[0]
    after = upcoming[1] if len(upcoming) > 1 else next_mine + 2 * teams
    my_counts = dt.roster_of(state, slot, board)
    round_next = (next_mine - 1) // teams + 1
    surv_start, surv_end = dl.survival_window(pick_no, next_mine, after)

    look = lookahead.rollout(state, board, n=rollout_n, seed=seed)
    gone = {dt.norm_name(pk["name"]) for pk in state["picks"]}

    def surv_of(bp):
        s = look.get("survival", {}).get(dt.norm_name(bp["name"]))
        if s is None:
            s = dt.survival_odds(state, board, bp, surv_start, surv_end)
        return s

    tagged_qb_pool = [bp for k, bp in board.items()
                      if bp["pos"] == "QB" and k not in gone
                      and ({"value", "breakout"} & set(bp.get("my_tags", [])))]
    tagged_qbs_left = len(tagged_qb_pool)
    qb_exp_surv = sum(surv_of(bp) for bp in tagged_qb_pool)
    tagged_te_pool = [bp for k, bp in board.items()
                      if bp["pos"] == "TE" and k not in gone
                      and ({"value", "breakout", "sleeper"} & set(bp.get("my_tags", [])))]
    te_exp_surv = sum(surv_of(bp) for bp in tagged_te_pool)

    cal = dl.vbd_calibration(board)
    inj = inj if inj is not None else injuries.load().get("players", {})
    return dl.ScoreCtx(
        state=state, board=board, my_counts=my_counts, pick_no=pick_no,
        next_mine=next_mine, after=after, round_next=round_next,
        picks_remaining=len(upcoming), surv_start=surv_start, surv_end=surv_end,
        need_dst=my_counts.get("DST", 0) == 0, need_k=my_counts.get("K", 0) == 0,
        tagged_qbs_left=tagged_qbs_left, qb_exp_surv=qb_exp_surv, te_exp_surv=te_exp_surv,
        look=look, baselines=dl.replacement_baselines(board), slope=cal["slope"],
        anchor=cal["anchor"], inj=inj, lineup=dl.my_lineup(state, board, slot), bias=None,
    )


# --- (a) factors sum to score, for every rec in a --once-style build --------

def _accumulate(deltas):
    """Left-to-right float accumulation, matching score_candidate's `add()`
    closure exactly (Python 3.12+'s builtin sum() uses a higher-precision
    algorithm for floats that can round a hair differently at the last
    decimal, so a plain running total is what actually mirrors the score)."""
    total = 0.0
    for d in deltas:
        total += d
    return total


def test_factors_sum_to_score_on_disk_state():
    state = dt.load_state()
    board = dt.load_board()
    if len(state["picks"]) >= state["teams"] * state["rounds"]:
        # the saved draft is finished (no picks left to recommend): replay
        # its first eight rounds so the test still runs on a real board
        state = {**state, "picks": state["picks"][: state["teams"] * 8]}
    live = dl.build_live(state, board)
    assert live["recs"], "expected at least one rec from the draft state"
    for rec in live["recs"]:
        deltas = [f["delta"] for f in rec["why"] if f["delta"] is not None]
        total = _accumulate(deltas)
        assert round(total, 1) == rec["score"], (
            f"{rec['name']}: sum(why deltas)={total} != score={rec['score']}"
        )


def test_factors_sum_to_score_fresh_state():
    state = fresh_state()
    live = dl.build_live(state, BOARD)
    assert live["recs"]
    for rec in live["recs"]:
        deltas = [f["delta"] for f in rec["why"] if f["delta"] is not None]
        total = _accumulate(deltas)
        assert round(total, 1) == rec["score"]


# --- (b) no K in the top 5 recs before round 13 in an early-draft state -----

def test_no_kicker_in_top5_early_draft():
    state = fresh_state()
    live = dl.build_live(state, BOARD)
    assert live["round"] < 13
    top5 = live["recs"][:5]
    assert all(r["pos"] != "K" for r in top5), top5


def test_no_kicker_in_top5_mid_draft_before_round13():
    # ~9 rounds in (round 10), still well before the round-13 endgame window.
    import random
    import draft_sim as ds
    rng = random.Random(3)
    state = {"teams": TEAMS, "slot": 99, "rounds": ROUNDS, "picks": []}
    while len(state["picks"]) < 16 * 9:
        pick_no = len(state["picks"]) + 1
        team_slot = dt.snake_team_for_pick(pick_no, TEAMS)
        p = ds.ai_pick(state, BOARD, rng)
        state["picks"].append({"pick": pick_no, "team": team_slot, "name": p["name"]})
    state["slot"] = 7
    live = dl.build_live(state, BOARD)
    assert live["round"] < 13
    top5 = live["recs"][:5]
    assert all(r["pos"] != "K" for r in top5), top5


# --- (c) an IR-tagged player drops at least 30 rank points ------------------

def test_ir_tag_drops_score_by_at_least_30(monkeypatch):
    state = fresh_state()
    target = dt.best_available(state, BOARD, limit=1)[0]
    nm = dt.norm_name(target["name"])

    ctx_clean = make_ctx(state, BOARD, inj={})
    score_clean, why_clean = dl.score_candidate(target, ctx_clean)
    assert not any(f["label"] == "Injury" and f["delta"] for f in why_clean)

    ir_players = {nm: {"chip": "IR", "status": "IR", "body_part": "Knee",
                        "practice": None, "updated": "2026-09-01", "penalty": 60.0}}
    ctx_ir = make_ctx(state, BOARD, inj=ir_players)
    score_ir, why_ir = dl.score_candidate(target, ctx_ir)

    injury_factors = [f for f in why_ir if f["label"] == "Injury"]
    assert injury_factors and injury_factors[0]["delta"] is not None
    assert score_ir - score_clean >= 30, (
        f"expected an IR tag to cost >= 30 rank points, got {score_ir - score_clean}"
    )


# --- (d) on-the-clock survival window semantics unchanged -------------------

def test_survival_window_on_the_clock():
    # "On the clock, the question is: if I pass now, is he there NEXT round"
    # (next_mine+1 .. after) -- matches build_live's pre-existing comment.
    start, end = dl.survival_window(pick_no=5, next_mine=5, after=37)
    assert (start, end) == (6, 37)


def test_survival_window_between_turns():
    # Between turns: "does he reach my upcoming pick at all" (pick_no .. next_mine).
    start, end = dl.survival_window(pick_no=3, next_mine=5, after=37)
    assert (start, end) == (3, 5)


# --- (e) the QB plan must act when the market is taking the last starters ---

def test_qb_plan_never_passes_when_starters_are_running_out():
    """Board-independent invariant, checked over a full self-play draft from
    the wheel (slot 1, seed 301 — 30-pick gaps): whenever we have no QB, it is
    round 4+, a top-16 QB is on the board, and the rollout expects <= 2 of them
    to survive to our next turn (or < 85% chance any does), the top
    recommendation must be a QB. And the draft must end with a top-16 QB."""
    import random
    import draft_sim as ds
    dl.ROLLOUT_N, dl.ROLLOUT_SEED = 40, 301
    board = dt.load_board()
    state = {"teams": TEAMS, "slot": 1, "rounds": ROUNDS, "picks": [], "mock": True}
    rng = random.Random(301)
    qb1 = None
    while len(state["picks"]) < TEAMS * ROUNDS:
        ds.sim_until_my_turn(state, board, rng)
        pick_no = len(state["picks"]) + 1
        if pick_no > TEAMS * ROUNDS or dt.snake_team_for_pick(pick_no, TEAMS) != 1:
            break
        live = dl.build_live(state, board)
        top = live["recs"][0]
        w = live["qb_watch"]
        if w["have"] == 0 and live["round"] >= 4 and w["startable_left"] > 0 \
                and (w["exp_survive"] <= 2.0 or w["p_any"] < 0.85):
            assert top["pos"] == "QB", (pick_no, w, top["name"], top["pos"])
        if top["pos"] == "QB" and qb1 is None:
            qb1 = (pick_no, top["name"], top["pos_rank"])
        state["picks"].append({"pick": pick_no, "team": 1, "name": top["name"]})
    assert qb1 is not None and qb1[2] <= dl.STARTABLE_QB, qb1


# --- (f) the FF profile sharpens a bare "Questionable" -----------------------

def test_injury_adjust_uses_profile_evidence():
    bare = {"chip": "Q", "status": "Questionable", "body_part": "Undisclosed", "practice": None,
            "updated": "2026-09-01", "penalty": 6.0}
    pen0, d0 = dl.injury_adjust(bare, None)
    assert pen0 == 2.0 and "discounted" in d0
    dnp = {"injury": "Questionable (Knee)", "news": [{"headline": "Doesn't practice Wednesday", "age": "19 hours ago"}]}
    pen1, d1 = dl.injury_adjust(bare, dnp)
    assert pen1 == 6.0 and "knee" in d1 and "FF:" in d1
    cleared = {"injury": "Questionable (Groin)", "news": [{"headline": "Back at practice Sunday", "age": "4 days ago"}]}
    pen2, _ = dl.injury_adjust(bare, cleared)
    assert pen2 == 2.0
    pen3, d3 = dl.injury_adjust(None, dnp)          # Sleeper silent, FF says hurt
    assert pen3 == 6.0 and d3.startswith("Questionable")


def test_injury_good_news_word_order() -> None:
    bare = {
        "chip": "Q",
        "status": "Questionable",
        "body_part": "Shoulder",
        "practice": None,
        "updated": "2026-09-02",
        "penalty": 6.0,
    }
    for headline in (
        "Practices fully Tuesday",
        "Full participant Wednesday",
        "Returns to practice",
    ):
        pen, _ = dl.injury_adjust(
            bare,
            {
                "injury": "Questionable (Shoulder)",
                "news": [{"headline": headline, "age": "1 day ago"}],
            },
        )
        assert pen == 2.0, headline


# --- (g) take who won't last, at any gap ------------------------------------


def test_now_or_never_beats_sure_thing_of_equal_value() -> None:
    """A sure thing of equal value must rank below the player who will not last.

    Live mock, slot 10, pick 74 (next pick 87): Lloyd and Stevenson are a coin
    flip on value (UDK 68/69, ~190 proj each) but Lloyd is ~100% there at 87
    and Stevenson ~0%. The board must put Stevenson first so we get both.
    """
    state = json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "state_slot10_pick74.json").read_text()
    )
    dl.ROLLOUT_N, dl.ROLLOUT_SEED = 60, 1
    live = dl.build_live(state, BOARD)
    names = [r["name"] for r in live["recs"]]
    assert names.index("Rhamondre Stevenson") < names.index("MarShawn Lloyd")
    assert live["queue"][0]["name"] == live["recs"][0]["name"]
