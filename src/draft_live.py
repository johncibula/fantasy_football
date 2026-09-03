"""Live draft engine: polls ESPN's draft feed and writes reports/live.json
for the auto-refreshing dashboard (reports/draft_dashboard.html).

Modes:
  --poll         real draft day: poll the league's mDraftDetail every 2s
  --once         rebuild live.json from data/draft_state.json (mock/manual mode,
                 where picks are fed in via draft_tracker.py)

The hot path is pure Python — no model calls, nothing waits on chat. `--once`
never touches the network: injuries come from the on-disk cache
(`data/injuries.json`) and league tendencies from `data/tendencies.json`.

Scoring is RANK-SPACE and LOWER = BETTER. Every term that moves a candidate's
score is recorded in that rec's `why` list as {label, delta, detail}; the sum
of the non-None deltas is exactly the score (see tests/test_scoring.py).
"""

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import espn_client
import draft_tracker as dt
import injuries
import league_history
import lookahead
import player_pages
from draft_tracker import (DATA, STATE_PATH, load_board, load_state, save_state,
                           my_pick_numbers, roster_of, positional_need,
                           survival_odds, best_available, norm_name,
                           snake_team_for_pick)

REPORTS = Path(__file__).resolve().parent.parent / "reports"
LIVE_PATH = REPORTS / "live.json"
API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league}"

# Monte-Carlo rollout size for the per-call lookahead. 150 keeps a `--once`
# rebuild on an early-draft state well under the 1.2s budget (see the plan-05
# report); the self-play harness lowers it to FAST_ROLLOUT_N via --fast.
ROLLOUT_N = int(os.environ.get("DRAFT_ROLLOUT_N", "150"))
FAST_ROLLOUT_N = 40
# None = fresh sampling noise every call (the live default). The harness and
# the tests pin it so their output is reproducible.
ROLLOUT_SEED = None

POS6 = ("QB", "RB", "WR", "TE", "DST", "K")

# How the two value axes blend. Both are in rank units (1 = best on the board):
# UDK overall rank, and "where he'd rank if the board were sorted by projected
# points over the 16-team replacement level" (see vbd_calibration). VBD leans
# RB in this league — RB48 projects ~107 pts vs WR48 ~164 in full PPR — so at
# 0.40 the round-1 order tilts RB-first relative to the UDK board (Taylor/Cook
# above Chase/Nacua at pick 1). John's call (2026-09-03): keep the RB lean at
# 0.40; lower toward 0.25 only if he asks to trust the UDK board more.
RANK_WEIGHT = 0.6
VBD_WEIGHT = 0.4

# "Starter-quality" QB in a 16-team league: top-16 at the position. The QB
# plan watches how many of these the rollout expects to survive to our next
# pick — the market's answer to "can I still wait?" — alongside John's tagged
# targets. The old rule only looked at the targets and only from round 6; in
# this room QBs go in rounds 3-6 (league history: median first QB round 5.3,
# ESPN ADP has 12+ gone by pick 90), so the punt was dying before it fired.
STARTABLE_QB = 16


def cookies():
    return {"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["SWID"]}


def fetch_draft(league_id: int, year: int) -> dict:
    r = requests.get(API.format(year=year, league=league_id),
                     params={"view": "mDraftDetail"}, cookies=cookies(), timeout=10)
    r.raise_for_status()
    return r.json()["draftDetail"]


def league_maps(league_id: int, year: int) -> tuple[dict, dict]:
    """(playerId -> name, teamId -> 'Team Name · Owner') built once at startup."""
    from espn_api.football import League
    lg = League(league_id=league_id, year=year,
                espn_s2=os.environ["ESPN_S2"], swid=os.environ["SWID"])
    names = {k: v for k, v in lg.player_map.items() if isinstance(k, int)}
    teams = {}
    for t in lg.teams:
        owner = ""
        if t.owners:
            o = t.owners[0]
            owner = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
        teams[t.team_id] = t.team_name + (f" · {owner}" if owner else "")
    return names, teams


# ---------------------------------------------------------------------------
# VBD: replacement depth, baselines, and the rank-space calibration
# ---------------------------------------------------------------------------

# A FLEX slot is spent on an RB or WR far more often than a TE.
FLEX_SHARE = {"RB": 0.45, "WR": 0.45, "TE": 0.10}
# How a bench slot that IS spent on a startable skill player splits by position.
BENCH_SHARE = {"RB": 0.35, "WR": 0.35, "QB": 0.10, "TE": 0.10}
# ...but most bench slots are NOT startable depth: handcuffs nobody would play,
# rookie stashes, injured bodies, a second K/DST, players cut in week 2.
# Replacement level is the last player you would actually START, so only a
# fraction of the league's 96 bench slots counts toward it. 0.26 is calibrated
# to plan 05's own sanity targets (QB ~18, RB ~48, WR ~48, TE ~20); the literal
# "6 x teams spread by share" reading gives QB 26 / RB 73 / WR 73 / TE 27,
# which is waiver-wire depth, not replacement level.
BENCH_UTILISATION = 0.26

_DEPTHS: dict | None = None
_BASELINES: dict | None = None
_VBD_CAL: dict | None = None


def _compute_depths(cfg: dict) -> dict[str, int]:
    lg = cfg.get("league", {}) or {}
    teams = lg.get("size") or 16
    roster = lg.get("roster", {}) or {}
    flex = roster.get("FLEX", 0) or 0
    bench = roster.get("BENCH", 0) or 0
    out = {}
    for pos in ("QB", "RB", "WR", "TE"):
        starters = (roster.get(pos, 0) or 0) * teams
        flex_depth = flex * teams * FLEX_SHARE.get(pos, 0.0)
        bench_depth = bench * teams * BENCH_SHARE.get(pos, 0.0) * BENCH_UTILISATION
        out[pos] = max(1, int(round(starters + flex_depth + bench_depth)))
    return out


def replacement_depths(cfg: dict | None = None) -> dict[str, int]:
    """How many players at each position are startable league-wide — the
    index of the replacement-level player. Derived from config.yaml's roster
    (teams x starters + flex share + a discounted bench share)."""
    global _DEPTHS
    if cfg is not None:
        return _compute_depths(cfg)
    if _DEPTHS is None:
        _DEPTHS = _compute_depths(espn_client.get_config())
    return _DEPTHS


def replacement_baselines(board: dict) -> dict:
    """Projected points of the replacement-level player per position.
    Cached — board is static."""
    global _BASELINES
    if _BASELINES is None:
        _BASELINES = {}
        for pos, n in replacement_depths().items():
            pts = sorted((p["proj_points"] for p in board.values()
                          if p["pos"] == pos and p.get("proj_points")), reverse=True)
            _BASELINES[pos] = pts[n - 1] if len(pts) >= n else (pts[-1] if pts else 0)
    return _BASELINES


def vbd_of(p: dict, baselines: dict) -> float:
    base = baselines.get(p["pos"])
    if base is None:
        return 0.0
    return max(0.0, (p.get("proj_points") or 0) - base)


def _percentile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    i = (len(sorted_xs) - 1) * q / 100.0
    lo = int(i)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def vbd_calibration(board: dict, top: int = 150) -> dict:
    """Put VBD on the same axis as UDK rank.

    slope = (p90 rank - p10 rank) / |p10 VBD - p90 VBD| over the top-`top`
    board players, i.e. rank points per projected point, so one full spread of
    VBD moves the score as far as one full spread of rank does. (Plan 05
    writes the denominator as p10 - p90, which is negative; we take the
    magnitude so that MORE VBD always LOWERS — improves — the score.)

    `anchor` places the best VBD on the board at rank 1, so
    `anchor - vbd*slope` is "where this player would rank if the board were
    sorted by lineup value" and is directly comparable to `overall_rank`.
    """
    global _VBD_CAL
    if _VBD_CAL is None:
        baselines = replacement_baselines(board)
        seen, rows = set(), []
        for p in board.values():
            if id(p) in seen:
                continue
            seen.add(id(p))
            if p.get("overall_rank"):
                rows.append(p)
        rows.sort(key=lambda p: p["overall_rank"])
        rows = rows[:top]
        ranks = sorted(float(p["overall_rank"]) for p in rows)
        vbds = sorted(vbd_of(p, baselines) for p in rows)
        rank_spread = _percentile(ranks, 90) - _percentile(ranks, 10)
        vbd_spread = abs(_percentile(vbds, 10) - _percentile(vbds, 90))
        slope = (rank_spread / vbd_spread) if vbd_spread else 0.0
        vbd_max = max(vbds) if vbds else 0.0
        _VBD_CAL = {
            "slope": slope,
            "vbd_max": vbd_max,
            "anchor": 1.0 + vbd_max * slope,
            "n": len(rows),
            "rank_p10": _percentile(ranks, 10), "rank_p90": _percentile(ranks, 90),
            "vbd_p10": _percentile(vbds, 10), "vbd_p90": _percentile(vbds, 90),
        }
    return _VBD_CAL


# ---------------------------------------------------------------------------
# Injuries
# ---------------------------------------------------------------------------

# A flat +6 for every "Questionable" is pure noise in preseason (wave 1 found
# 32 of the top-100 carrying one, all Q, most with no body part and no practice
# report at all). Scale those down to a nudge; keep the full penalty when the
# designation actually says something — a named body part, or a DNP/limited
# practice report.
QUESTIONABLE_DISCOUNT = 1.0 / 3.0
_PRACTICE_FLAGS = {"DNP", "DID NOT PARTICIPATE", "LIMITED", "LIMITED PARTICIPATION", "LP"}


_DNP_NEWS = re.compile(r"(no practice|doesn'?t practice|not practic|sidelined|sitting out|stting out|"
                       r"misses practice|did not practice|dnp|out for|ruled out|carted|placed on)", re.I)
_GOOD_NEWS = re.compile(r"(full(y)? practice|returns to practice|back at practice|cleared|good to go|"
                        r"expected to play|optimism|trending toward.*return|no injury designation)", re.I)


def injury_adjust(record: dict | None, profile: dict | None = None) -> tuple[float, str]:
    """(rank-point penalty, detail) for an injuries.load() record, refined by
    the Fantasy Footballers profile when we have one: it names the body part
    and carries dated practice notes, which Sleeper's preseason feed mostly
    lacks. A bare "Questionable" stays discounted; a named body part or a
    no-practice note in the last few days restores the full penalty; a
    "back at practice / cleared" note keeps the discount even with a body part."""
    prof_inj = (profile or {}).get("injury")
    news = (profile or {}).get("news") or []
    if not record and not prof_inj:
        return 0.0, ""
    pen = float((record or {}).get("penalty") or (6.0 if prof_inj else 0.0))
    chip = ((record or {}).get("chip") or ("Q" if prof_inj else "")).strip().upper()
    body = ((record or {}).get("body_part") or "").strip()
    if (not body or body.lower() == "undisclosed") and prof_inj and "(" in prof_inj:
        body = prof_inj[prof_inj.index("(") + 1:].rstrip(")")
    practice = ((record or {}).get("practice") or "").strip().upper()
    status = (record or {}).get("status") or (prof_inj.split(" (")[0] if prof_inj else chip) or "?"
    bits = [status]
    if body and body.lower() != "undisclosed":
        bits.append(body.lower())
    if practice:
        bits.append(f"practice {practice.lower()}")
    if (record or {}).get("updated"):
        bits.append(record["updated"])
    latest = news[0] if news else None
    if latest:
        bits.append(f"FF: {latest['headline']} ({latest['age']})")
    detail = " · ".join(bits)

    dnp = bool(latest and _DNP_NEWS.search(latest["headline"]))
    good = bool(latest and _GOOD_NEWS.search(latest["headline"]))
    informative = (practice in _PRACTICE_FLAGS) or dnp or \
        (bool(body) and body.lower() != "undisclosed" and not good)
    if chip in ("Q", "?") and not informative:
        pen *= QUESTIONABLE_DISCOUNT
        detail += " · no hard evidence — discounted"
    return round(pen, 2), detail


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def survival_window(pick_no: int, next_mine: int, after: int) -> tuple[int, int]:
    """On the clock, the question is "if I pass now, is he there NEXT round"
    (next_mine+1 .. after). Between turns it's "does he reach my upcoming pick
    at all" (pick_no .. next_mine). `lookahead.rollout` uses the same split."""
    if next_mine == pick_no:
        return next_mine + 1, after
    return pick_no, next_mine or pick_no


@dataclass
class ScoreCtx:
    """Everything score_candidate() needs that is constant across candidates."""
    state: dict
    board: dict
    my_counts: dict
    pick_no: int
    next_mine: int
    after: int
    round_next: int
    picks_remaining: int
    surv_start: int
    surv_end: int
    need_dst: bool
    need_k: bool
    tagged_qbs_left: int
    qb_exp_surv: float
    te_exp_surv: float
    look: dict
    baselines: dict
    slope: float
    anchor: float
    inj: dict
    lineup: dict
    bias: dict | None = None
    startable_qbs_left: int = 99      # QBs ranked <= STARTABLE_QB still on the board
    startable_qb_exp: float = 99.0    # ...expected to survive to our next pick
    startable_qb_p_any: float = 1.0   # P(at least one of them survives) — variance-aware
    _surv_cache: dict = field(default_factory=dict)

    def survival(self, p: dict) -> float:
        nm = norm_name(p["name"])
        if nm in self._surv_cache:
            return self._surv_cache[nm]
        s = self.look.get("survival", {}).get(nm)
        if s is None:
            s = survival_odds(self.state, self.board, p, self.surv_start, self.surv_end,
                              bias_by_slot=self.bias)
        self._surv_cache[nm] = s
        return s


def my_lineup(state: dict, board: dict, slot: int) -> dict:
    """Our current would-be starters, for the bye-clash and stack checks."""
    mine = []
    for pk in state["picks"]:
        if pk["team"] == slot:
            bp = board.get(norm_name(pk["name"]))
            if bp:
                mine.append(bp)
    by: dict[str, list] = {}
    for bp in mine:
        by.setdefault(bp["pos"], []).append(bp)
    for v in by.values():
        v.sort(key=lambda b: -(b.get("proj_points") or 0))
    skill = by.get("RB", [])[:2] + by.get("WR", [])[:2] + by.get("TE", [])[:1]
    flex_pool = sorted(by.get("RB", [])[2:] + by.get("WR", [])[2:] + by.get("TE", [])[1:],
                       key=lambda b: -(b.get("proj_points") or 0))
    if flex_pool:
        skill.append(flex_pool[0])
    return {
        "skill": skill,
        "QB": by.get("QB", [])[:1],
        "qb_teams": {b.get("team") for b in by.get("QB", []) if b.get("team")},
        "catcher_teams": {b.get("team") for b in (by.get("WR", []) + by.get("TE", []))
                          if b.get("team")},
        "by": by,
    }


def score_candidate(p: dict, ctx: ScoreCtx) -> tuple[float, list[dict]]:
    """Rank-space score (LOWER = BETTER) plus the factor breakdown that
    produced it. sum(f['delta'] for f in factors if f['delta'] is not None)
    == the returned score, exactly."""
    factors: list[dict] = []
    score = 0.0

    def add(label: str, delta: float | None, detail: str) -> None:
        nonlocal score
        if delta is None:
            factors.append({"label": label, "delta": None, "detail": detail})
            return
        delta = round(float(delta), 4)
        if delta == 0.0:
            return
        score += delta
        factors.append({"label": label, "delta": delta, "detail": detail})

    pos = p["pos"]
    tags = p.get("my_tags", [])
    rank = p.get("overall_rank") or 250
    has_vbd = pos in ctx.baselines
    surv = ctx.survival(p)

    # --- UDK rank (the backbone) ------------------------------------------
    if has_vbd:
        add("UDK rank", RANK_WEIGHT * rank,
            f"UDK #{p.get('overall_rank') or '—'} x {RANK_WEIGHT:.2f} (rank axis)")
    else:
        # K/DST have no projection baseline, so rank carries full weight and
        # the K/DST timing rules below stay calibrated exactly as before.
        add("UDK rank", rank, f"{pos} — no VBD axis, rank carries full weight")

    # --- Tags --------------------------------------------------------------
    tag_bonus = 0.0
    if "bust" in tags:
        tag_bonus += 12
    if "value" in tags or "breakout" in tags:
        tag_bonus -= 4
    if "sleeper" in tags:
        tag_bonus -= 2
    if tag_bonus:
        add("Tags", tag_bonus, "your tags: " + ", ".join(tags))
    elif tags:
        add("Tags", None, "your tags: " + ", ".join(tags))

    # --- Need --------------------------------------------------------------
    # Coefficient recalibrated for the VBD rework below: VBD_WEIGHT*vbd_rank_component
    # now ranges roughly as wide as a full UDK rank (anchor ~244), where the
    # old formula's raw `vbd*0.15` topped out around 40. A -need*6 that was
    # plenty to keep a starter-need honest against the old VBD term is not
    # enough against the new one — self-play regression testing found teams
    # sitting at zero WRs into round 8 because a marginal RB's bigger VBD gap
    # kept outscoring a badly-needed starter WR of similar UDK rank. -need*18
    # keeps a missing-starter need (~1.3-1.6) worth 25-45 rank points, enough
    # to win that comparison, without swamping VBD among comparably-needed
    # players (e.g. early picks, where everyone's need is ~1.3-1.6 anyway).
    need = positional_need(ctx.my_counts, pos)
    add("Need", -need * 18, f"{pos} need {need:.2f} (have {ctx.my_counts.get(pos, 0)})")

    # --- VBD ---------------------------------------------------------------
    vbd = vbd_of(p, ctx.baselines)
    if has_vbd:
        vbd_rank = ctx.anchor - vbd * ctx.slope
        add("VBD", VBD_WEIGHT * vbd_rank,
            f"+{vbd:.0f} pts over {pos}{replacement_depths()[pos]} replacement "
            f"= VBD-rank {vbd_rank:.0f} x {VBD_WEIGHT:.2f}")
    else:
        add("VBD", None, f"no replacement baseline for {pos}")

    # --- VONA --------------------------------------------------------------
    v = lookahead.vona(p, ctx.look)
    if v is not None:
        raw = v * ctx.slope * 0.5  # rank units; >0 = better than the wait
        # next_best is measured at the rollout's next_mine (the pick AFTER the
        # current one when we're on the clock, our upcoming pick otherwise).
        nb_round = ((ctx.look.get("next_mine") or ctx.next_mine) - 1) // ctx.state["teams"] + 1
        if raw > 0:
            # A bonus only to the extent he might NOT last: if he survives, the
            # "next best" at the position is him and the gap is illusory.
            delta = -raw * (1 - surv)
            reason = (f"+{v:.0f} pts over the likely R{nb_round} "
                      f"{pos} ({ctx.look['next_best'][pos]['p50_name']}), "
                      f"x {(1 - surv):.0%} chance he's gone")
        else:
            delta = -raw
            reason = (f"{v:.0f} pts vs the likely R{nb_round} "
                      f"{pos} ({ctx.look['next_best'][pos]['p50_name']}) — waiting is fine")
        delta = max(-12.0, min(12.0, delta))
        add("VONA", delta, reason)

    # --- Market (context only: survival/VONA already price it via the bots) --
    md = p.get("market_delta")
    if md is not None:
        if md >= 15:
            read = "room ranks him well below UDK — he should slide; safe to wait"
        elif md <= -15:
            read = "room ranks him well above UDK — no waiting on him"
        else:
            read = "room and UDK roughly agree"
        add("Market", None,
            f"ESPN #{p.get('espn_rank')} (ADP {p.get('espn_adp') or '—'}) vs UDK "
            f"#{p.get('overall_rank')}: {md:+d} — {read}")

    # --- Survival (context only) -------------------------------------------
    src = "rollout" if norm_name(p["name"]) in ctx.look.get("survival", {}) else "analytic"
    add("Survival", None,
        f"{surv * 100:.0f}% still there at pick {ctx.surv_end} ({src}, "
        f"n={ctx.look.get('n', 0)})")

    # --- Injury -------------------------------------------------------------
    rec = ctx.inj.get(norm_name(p["name"]))
    pen, inj_detail = injury_adjust(rec, player_pages.profile_for(p["name"]))
    if pen:
        add("Injury", pen, inj_detail)
    elif rec or inj_detail:
        add("Injury", None, inj_detail or "listed, no penalty")

    # --- Bye clash ----------------------------------------------------------
    bye = p.get("bye")
    peers = ctx.lineup["QB"] if pos == "QB" else (
        ctx.lineup["skill"] if pos in ("RB", "WR", "TE") else [])
    clash = [b for b in peers if bye and b.get("bye") == bye]
    if len(clash) >= 3:
        add("Bye clash", 6.0, f"week {bye} already off for {', '.join(b['name'] for b in clash)}")
    elif len(clash) == 2:
        add("Bye clash", 3.0, f"week {bye} already off for {', '.join(b['name'] for b in clash)}")

    # --- Stack (never in the early rounds) ----------------------------------
    if ctx.round_next >= 4 and p.get("team"):
        if pos in ("WR", "TE") and p["team"] in ctx.lineup["qb_teams"]:
            qb = ctx.lineup["QB"][0]["name"] if ctx.lineup["QB"] else p["team"]
            add("Stack", -3.0, f"stacks with your QB {qb} ({p['team']})")
        elif pos == "QB" and p["team"] in ctx.lineup["catcher_teams"]:
            mates = [b["name"] for b in ctx.lineup["by"].get("WR", []) + ctx.lineup["by"].get("TE", [])
                     if b.get("team") == p["team"]]
            add("Stack", -3.0, f"stacks with {', '.join(mates)} ({p['team']})")

    # --- K/DST timing -------------------------------------------------------
    # Endgame forcing: with only enough picks left to fill the legally required
    # slots, those slots outrank everything.
    if pos in ("DST", "K"):
        missing_slots = int(ctx.need_dst) + int(ctx.need_k)
        fills_a_hole = (pos == "DST" and ctx.need_dst) or (pos == "K" and ctx.need_k)
        pr = p.get("pos_rank") or 99
        # Force the slot when the draft is about to end. With no slack left
        # (picks == holes) ANY body at the position beats an illegal roster;
        # with one pick of slack, still force unless only scrubs remain.
        must = ctx.picks_remaining <= missing_slots or \
            (ctx.picks_remaining <= missing_slots + 1 and pr <= 16)
        if fills_a_hole and must:
            target = -100 + pr
            add("K/DST timing", target - score,
                f"only {ctx.picks_remaining} picks left and {pos} unfilled — forced")
        elif not fills_a_hole:
            add("K/DST timing", 200.0, f"you already have a {pos}")
        elif ctx.picks_remaining > 4:
            add("K/DST timing", 120.0,
                f"{ctx.picks_remaining} picks left — far too early for a {pos}")

    # --- QB plan ------------------------------------------------------------
    if pos == "QB":
        qb_have = ctx.my_counts.get("QB", 0)
        if qb_have == 0:
            is_target = bool({"value", "breakout"} & set(tags))
            startable = (p.get("pos_rank") or 99) <= STARTABLE_QB
            if ctx.picks_remaining <= 4:
                add("QB plan", -35.0, f"only {ctx.picks_remaining} picks left and no QB")
            elif startable and ctx.round_next >= 4 and \
                    (ctx.startable_qb_exp < 1.5 or ctx.startable_qb_p_any < 0.85):
                # The market is about to take the last real starters. This is
                # the "wait no longer" signal regardless of tags. p_any guards
                # the wheel slots: across a 30-pick gap "2 expected" can easily
                # be 0 realised.
                # Decisive on purpose: QB value-over-replacement is compressed
                # in a 1-QB league, so a 45-point nudge still lost to a WR by 6
                # in self-play (seed 301, slot 1, pick 97 — see tests).
                add("QB plan", -80.0 if is_target else -65.0,
                    f"last starter-quality QBs: ~{ctx.startable_qb_exp:.1f} of "
                    f"{ctx.startable_qbs_left} top-{STARTABLE_QB} QBs survive to your next "
                    f"turn ({ctx.startable_qb_p_any:.0%} chance any does)")
            elif ctx.tagged_qbs_left == 0 and startable and \
                    (ctx.startable_qbs_left <= 3 or ctx.startable_qb_exp <= 2.0):
                # Targets are gone and the market will leave ~2 or fewer real
                # starters for our next turn: take the best one now rather than
                # the worst one later (QB11 now beats QB16 next round).
                add("QB plan", -65.0,
                    f"targets gone; ~{ctx.startable_qb_exp:.1f} of {ctx.startable_qbs_left} "
                    f"starter-quality QBs survive to your next turn")
            elif ctx.tagged_qbs_left == 0:
                add("QB plan", -40.0 if startable else -25.0,
                    "your QB target pool is empty — take the best left")
            elif ctx.qb_exp_surv < 1.5 and ctx.round_next >= 4:
                add("QB plan", -60.0 if is_target else -12.0,
                    f"punt dying: only ~{ctx.qb_exp_surv:.1f} of {ctx.tagged_qbs_left} "
                    f"targets survive to your next turn")
            elif ctx.tagged_qbs_left <= 4 and ctx.round_next >= 6:
                add("QB plan", -15.0 if is_target else -4.0,
                    f"{ctx.tagged_qbs_left} QB targets left in R{ctx.round_next}")
            else:
                add("QB plan", None,
                    f"punt healthy: {ctx.tagged_qbs_left} targets, "
                    f"~{ctx.qb_exp_surv:.1f} survive; ~{ctx.startable_qb_exp:.1f} "
                    f"top-{STARTABLE_QB} QBs survive")
        elif qb_have >= 2:
            add("QB plan", 60.0, "you already have two QBs")
        elif qb_have == 1 and ctx.round_next < 12:
            add("QB plan", 15.0, "QB2 is a luxury in a 1-QB league — wait for R12+")

    # --- RB depth -----------------------------------------------------------
    rb_have = ctx.my_counts.get("RB", 0)
    if pos == "RB" and rb_have < 4 and ctx.round_next >= 5:
        add("RB depth", -8.0 * (4 - rb_have), f"only {rb_have} RBs — depth beats WR hoarding")

    # --- WR surplus ---------------------------------------------------------
    wr_have = ctx.my_counts.get("WR", 0)
    if pos == "WR" and wr_have >= 5:
        add("WR surplus", 12.0 * (wr_have - 4), f"already {wr_have} WRs")

    # --- TE plan ------------------------------------------------------------
    if pos == "TE":
        te_have = ctx.my_counts.get("TE", 0)
        if te_have >= 2:
            add("TE plan", 60.0, "you already have two TEs")
        elif te_have == 0 and ctx.round_next >= 5 and ctx.te_exp_surv < 1.5 \
                and ({"value", "breakout", "sleeper"} & set(tags)):
            add("TE plan", -30.0,
                f"last call on great-or-late: ~{ctx.te_exp_surv:.1f} tagged TEs survive")

    return score, factors


def build_live(state: dict, board: dict) -> dict:
    t0 = time.time()
    pick_no = len(state["picks"]) + 1
    teams, slot, rounds = state["teams"], state["slot"], state["rounds"]
    mine = my_pick_numbers(slot, teams, rounds)
    upcoming = [p for p in mine if p >= pick_no]
    next_mine = upcoming[0] if upcoming else None
    after = upcoming[1] if len(upcoming) > 1 else (next_mine or 0) + 2 * teams
    my_counts = roster_of(state, slot, board)
    round_next = ((next_mine or pick_no) - 1) // teams + 1
    surv_start, surv_end = survival_window(pick_no, next_mine or pick_no, after)

    # League tendencies (from data/tendencies.json — no network). Only the real
    # poller records team_ids, so mocks and self-play get bias=None and behave
    # exactly as before.
    order = state.get("team_ids")
    bias = None
    dna: dict = {}
    if order:
        profiles = league_history.tendencies_by_slot(order)
        bias = {}
        for s, prof in profiles.items():
            if not prof:
                continue
            bias[s] = {pos: league_history.pos_multiplier(prof, pos, round_next)
                       for pos in POS6}
            dna[s] = {"labels": prof.get("labels", []),
                      "team": prof.get("latest_team_name")
                      or (state.get("team_labels", {}) or {}).get(s)
                      or (state.get("team_labels", {}) or {}).get(str(s))
                      or f"slot {s}"}

    # One Monte-Carlo rollout per call feeds survival, VONA and "if you wait".
    look = {"n": 0, "next_mine": next_mine, "after": after, "survival": {},
            "survival_after": {}, "next_best": {}, "elapsed_ms": 0.0}
    if next_mine:
        look = lookahead.rollout(state, board, n=ROLLOUT_N, seed=ROLLOUT_SEED,
                                 pos_bias_by_slot=bias)

    inj = injuries.load().get("players", {})

    recs = []
    tagged_qbs_left = 0
    qb_exp_surv = 0.0
    startable_qbs_left, startable_qb_exp, startable_qb_p_any = 0, 0.0, 0.0
    if next_mine:
        picks_remaining = len(upcoming)
        gone = {norm_name(pk["name"]) for pk in state["picks"]}

        def _surv_of(bp: dict) -> float:
            s = look.get("survival", {}).get(norm_name(bp["name"]))
            if s is None:
                s = survival_odds(state, board, bp, surv_start, surv_end, bias_by_slot=bias)
            return s

        # QB-punt floor: how many of John's tagged QB targets are still on the
        # board, and how many the rollout expects to survive to our next turn.
        tagged_qb_pool = [
            bp for k, bp in board.items()
            if bp["pos"] == "QB" and k not in gone
            and ({"value", "breakout"} & set(bp.get("my_tags", [])))
        ]
        tagged_qbs_left = len(tagged_qb_pool)
        qb_exp_surv = sum(_surv_of(bp) for bp in tagged_qb_pool)
        startable_qb_pool = list({id(bp): bp for k, bp in board.items()
                                  if bp["pos"] == "QB" and k not in gone
                                  and (bp.get("pos_rank") or 99) <= STARTABLE_QB}.values())
        startable_qbs_left = len(startable_qb_pool)
        _ss = [_surv_of(bp) for bp in startable_qb_pool]
        startable_qb_exp = sum(_ss)
        _p_none = 1.0
        for _sv in _ss:
            _p_none *= (1.0 - _sv)
        startable_qb_p_any = (1.0 - _p_none) if _ss else 0.0
        need_dst = my_counts.get("DST", 0) == 0
        need_k = my_counts.get("K", 0) == 0

        # "Great or late" TE plan gets the same survival protection as the QB
        # punt: the late pool is John's tagged TEs (value/breakout/sleeper).
        tagged_te_pool = [
            bp for k, bp in board.items()
            if bp["pos"] == "TE" and k not in gone
            and ({"value", "breakout", "sleeper"} & set(bp.get("my_tags", [])))
        ]
        te_exp_surv = sum(_surv_of(bp) for bp in tagged_te_pool)

        cal = vbd_calibration(board)
        ctx = ScoreCtx(
            state=state, board=board, my_counts=my_counts, pick_no=pick_no,
            next_mine=next_mine, after=after, round_next=round_next,
            picks_remaining=picks_remaining, surv_start=surv_start, surv_end=surv_end,
            need_dst=need_dst, need_k=need_k, tagged_qbs_left=tagged_qbs_left,
            qb_exp_surv=qb_exp_surv, te_exp_surv=te_exp_surv, look=look,
            baselines=replacement_baselines(board), slope=cal["slope"],
            anchor=cal["anchor"], inj=inj,
            lineup=my_lineup(state, board, slot), bias=bias,
            startable_qbs_left=startable_qbs_left, startable_qb_exp=startable_qb_exp,
            startable_qb_p_any=startable_qb_p_any,
        )

        candidates = best_available(state, board, limit=40)
        # K/DST carry no overall rank, so inject the top few when slots must fill.
        # Dedupe by identity and check gone by canonical name — D/ST players are
        # indexed under alias keys too ("texans d/st"), which must not leak.
        if (need_dst or need_k) and picks_remaining <= 4:
            for pos, needed in (("DST", need_dst), ("K", need_k)):
                if needed:
                    seen_ids = set()
                    pool = []
                    for p in board.values():
                        if p["pos"] != pos or id(p) in seen_ids:
                            continue
                        seen_ids.add(id(p))
                        if norm_name(p["name"]) in gone:
                            continue
                        pool.append(p)
                    pool.sort(key=lambda p: p.get("pos_rank") or 99)
                    candidates.extend(pool[:3])

        for p in candidates:
            score, why = score_candidate(p, ctx)
            surv = ctx.survival(p)
            record = inj.get(norm_name(p["name"]))
            nb = look.get("next_best", {}).get(p["pos"])
            recs.append({
                "name": p["name"], "pos": p["pos"], "team": p["team"], "bye": p.get("bye"),
                "tier": p.get("tier"), "rank": p.get("overall_rank"),
                "pos_rank": p.get("pos_rank"), "adp": p.get("adp"),
                "surv": round(surv * 100),
                "need": round(positional_need(my_counts, p["pos"]), 2),
                "tags": p.get("my_tags", []),
                "notes": p.get("my_notes", []), "score": round(score, 1),
                "why": why,
                "vona": round(lookahead.vona(p, look), 1)
                        if lookahead.vona(p, look) is not None else None,
                "injury": ({"chip": record.get("chip"), "status": record.get("status"),
                            "body_part": record.get("body_part"),
                            "updated": record.get("updated")} if record else None),
                "wait": ({"name": nb["p50_name"], "proj": round(nb["mean_proj"], 1)}
                         if nb else None),
                "espn_rank": p.get("espn_rank"), "espn_adp": p.get("espn_adp"),
                "market_delta": p.get("market_delta"),
                "outlook": p.get("outlook"),
                "profile": player_pages.profile_for(p["name"]),
            })
        recs.sort(key=lambda r: r["score"])

    # Position-run detector: last 8 picks by position.
    last8 = state["picks"][-8:]
    runs: dict[str, int] = {}
    for p in last8:
        bp = board.get(norm_name(p["name"]))
        if bp:
            runs[bp["pos"]] = runs.get(bp["pos"], 0) + 1

    rosters = {}
    for t in range(1, teams + 1):
        plist = [{"n": p["name"],
                  "p": board.get(norm_name(p["name"]), {}).get("pos", "?"),
                  "pick": p["pick"]}
                 for p in state["picks"] if p["team"] == t]
        counts = roster_of(state, t, board)
        missing = [pos for pos, n in
                   [("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1)] if counts.get(pos, 0) < n]
        rosters[t] = {"counts": counts, "players": plist, "missing": missing}

    # Market read: how the room is drafting vs. ADP expectation, and what
    # that means for us. Rule-based, recomputed every pick.
    made = len(state["picks"])
    drafted_pos: dict[str, int] = {}
    for pk in state["picks"]:
        bp = board.get(norm_name(pk["name"]))
        if bp:
            drafted_pos[bp["pos"]] = drafted_pos.get(bp["pos"], 0) + 1
    pos_flow = {}
    notes = []
    for pos in ("RB", "WR", "TE", "QB"):
        expected = sum(1 for p in board.values()
                       if p["pos"] == pos and p.get("adp_overall")
                       and p["adp_overall"] * 4 / 3 <= made)
        actual = drafted_pos.get(pos, 0)
        delta = actual - expected
        pos_flow[pos] = {"taken": actual, "expected": expected, "delta": delta}
        if delta >= 3:
            notes.append(f"{pos}s are flying — {actual} gone vs ~{expected} typical by pick {made} "
                         f"(+{delta}). Tiers are thinning ahead of schedule; jump the market if you need one.")
        elif delta <= -3:
            notes.append(f"{pos}s are falling — {-delta} behind the usual pace. "
                         f"Value should slide to your picks; safe to wait.")
    # Tier depletion at the two skill positions.
    for pos in ("RB", "WR"):
        avail_tiers = [p["tier"] for k, p in board.items()
                       if p["pos"] == pos and p.get("tier")
                       and k not in {norm_name(pk["name"]) for pk in state["picks"]}]
        if avail_tiers:
            top = min(avail_tiers)
            cnt = sum(1 for t in avail_tiers if t == top)
            if cnt <= 3:
                notes.append(f"{pos} tier {top} is nearly gone — only {cnt} left. "
                             f"A run here would empty it before your next pick.")
    if my_counts.get("QB", 0) == 0 and next_mine:
        if (startable_qb_exp < 1.5 or startable_qb_p_any < 0.85) and startable_qbs_left:
            notes.append(f"QB: the room is taking the last starter-quality QBs — only "
                         f"~{startable_qb_exp:.1f} of {startable_qbs_left} top-{STARTABLE_QB} "
                         f"QBs expected to survive to your next pick.")
        if tagged_qbs_left == 0:
            notes.append("Your QB target pool is EMPTY — take the best remaining QB soon.")
        elif qb_exp_surv < 1.5:
            notes.append(f"QB punt at risk: only ~{qb_exp_surv:.1f} of your {tagged_qbs_left} "
                         f"targets expected to survive to your next pick.")
        else:
            notes.append(f"QB punt healthy: {tagged_qbs_left} targets left, "
                         f"~{qb_exp_surv:.1f} expected to survive to your next turn.")

    on_clock = snake_team_for_pick(pick_no, teams) if pick_no <= teams * rounds else None
    # Sync integrity: any pick numbers missing below the highest known pick
    # mean a scrape window was skipped — surface them so the feed can backfill.
    have = {p["pick"] for p in state["picks"]}
    gaps = [i for i in range(1, max(have) + 1) if i not in have] if have else []

    return {
        "mock": bool(state.get("mock")),
        "team_labels": state.get("team_labels", {}),
        "gaps": gaps[:40],
        "ts": time.time(),
        "updated": time.strftime("%H:%M:%S"),
        "pick_no": pick_no,
        "round": (pick_no - 1) // teams + 1,
        "on_clock_slot": on_clock,
        "our_slot": slot,
        "next_mine": next_mine,
        "picks_until_ours": (next_mine - pick_no) if next_mine else None,
        "my_roster": [p["name"] for p in state["picks"] if p["team"] == slot],
        "my_counts": my_counts,
        "recs": recs[:14],
        "market": {"pos_flow": pos_flow, "notes": notes[:5]},
        "runs": runs,
        "recent": [{"pick": p["pick"], "name": p["name"], "team": p["team"]} for p in state["picks"][-10:]],
        "rosters": rosters,
        "next_best": look.get("next_best", {}),
        "qb_watch": {"have": my_counts.get("QB", 0), "startable_left": startable_qbs_left,
                     "exp_survive": round(startable_qb_exp, 2), "p_any": round(startable_qb_p_any, 2),
                     "targets_left": tagged_qbs_left, "targets_exp": round(qb_exp_surv, 2)},
        "lookahead_n": look.get("n", 0),
        "lookahead_ms": round(look.get("elapsed_ms", 0.0), 1),
        "dna": dna,
        "injuries_asof": (injuries.load().get("fetched_at") or "")[:10],
        "market_asof": (board.get("__market_asof__") or ""),
        "build_ms": round((time.time() - t0) * 1000, 1),
    }


def write_live(state: dict, board: dict) -> None:
    REPORTS.mkdir(exist_ok=True)
    live = build_live(state, board)
    with open(LIVE_PATH, "w") as f:
        json.dump(live, f)
    return live


def poll_loop(league_id: int, year: int, interval: float) -> None:
    board = load_board()
    names, team_names = league_maps(league_id, year)
    print(f"Player map: {len(names)} ids; teams: {len(team_names)}. Polling every {interval}s…")

    # The only network hit for injuries: once, at startup. Never in the loop.
    cache = injuries.refresh()
    print(f"Injuries: {len(cache.get('players', {}))} designations "
          f"(as of {cache.get('fetched_at')})")

    detail = fetch_draft(league_id, year)
    picks = [p for p in detail["picks"] if p.get("playerId", -1) > 0]
    order = [p["teamId"] for p in detail["picks"][: 16]]
    my_team_id = espn_client.get_config()["team_id"]
    slot = order.index(my_team_id) + 1 if my_team_id in order else None
    labels = {i + 1: team_names.get(tid, f"team {tid}") for i, tid in enumerate(order)}
    print(f"Draft order: {labels}\nOur slot: {slot}")
    state = {"teams": len(set(order)) or 16, "slot": slot or 7,
             "rounds": len(detail["picks"]) // 16, "picks": [],
             "team_labels": labels, "team_ids": order}
    profiles = league_history.tendencies_by_slot(order)
    print("League DNA: " + ", ".join(
        f"{s}:{'/'.join(p.get('labels', [])) or '—'}" for s, p in sorted(profiles.items())))

    known = 0
    while True:
        try:
            detail = fetch_draft(league_id, year)
        except Exception as e:
            print("poll error:", e)
            time.sleep(interval)
            continue
        picks = [p for p in detail["picks"] if p.get("playerId", -1) > 0]
        if len(picks) > known:
            for p in picks[known:]:
                team_slot = order.index(p["teamId"]) + 1 if p["teamId"] in order else 0
                name = names.get(p["playerId"], f"id:{p['playerId']}")
                state["picks"].append({"pick": p["overallPickNumber"], "team": team_slot, "name": name})
                print(f"pick {p['overallPickNumber']}: {name} (slot {team_slot})")
            known = len(picks)
            save_state(state)
            write_live(state, board)
        if detail.get("drafted"):
            print("Draft complete.")
            write_live(state, board)
            break
        time.sleep(interval)


def main() -> None:
    global ROLLOUT_N
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--league", type=int, default=int(os.environ.get("LEAGUE_ID", 0)))
    ap.add_argument("--year", type=int, default=espn_client.get_config()["season"])
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--n", type=int, default=None, help="rollout size (default %d)" % ROLLOUT_N)
    args = ap.parse_args()
    if args.n:
        ROLLOUT_N = args.n
    if args.once:
        t0 = time.time()
        state, board = load_state(), load_board()
        t_load = time.time()
        live = write_live(state, board)
        total = (time.time() - t0) * 1000
        print(f"Wrote {LIVE_PATH}")
        print(f"timing: load {(t_load - t0) * 1000:.0f}ms · "
              f"rollout n={live['lookahead_n']} {live['lookahead_ms']:.0f}ms · "
              f"build {live['build_ms']:.0f}ms · total {total:.0f}ms")
    elif args.poll:
        poll_loop(args.league, args.year, args.interval)
    else:
        ap.error("pass --poll or --once")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import espn_client  # noqa: F401  (loads .env)
    main()
