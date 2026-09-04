"""Learn F³ manager drafting tendencies from past ESPN drafts.

The survival model in draft_tracker.py assumes every opponent drafts to a
generic 16-team build (TARGET_BUILD). This league has years of ESPN draft
history; some managers have real, persistent habits (QB in round 3 every
year, never a TE before round 10, always a Cowboy). This module pulls that
history, learns a per-manager tendencies profile, and exposes helpers so a
later wave can bias the survival model by who is actually on the clock.

Data flow:
  pull()  -- network. Hits ESPN's v3 API directly for draftDetail/mTeam/
             mSettings (autoDraftTypeId, reservedForKeeper, pickOrder aren't
             exposed by espn_api's Pick object), and uses espn_api's
             League(...).player_info() -- one batched kona_playercard call
             per season -- to resolve playerId -> (name, position, NFL team).
             Positions are cached in data/player_pos_cache.json so re-runs,
             and later seasons with mostly-repeat players, are cheap.
             Raw per-season results are saved to data/history/draft_{year}.json.
  learn() -- offline, pure. Turns the pulled seasons into a tendencies model
             (data/tendencies.json) and a human dossier (reports/league_dna.md).

CLI:
  python src/league_history.py pull --seasons 2021-2025   # network
  python src/league_history.py learn                       # offline
  python src/league_history.py show [--slot-order 3,9,7,...]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

import espn_client
from draft_tracker import norm_name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"
HISTORY_DIR = DATA / "history"
TENDENCIES_PATH = DATA / "tendencies.json"
POS_CACHE_PATH = DATA / "player_pos_cache.json"
BOARD_PATH = DATA / "board.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "league_dna.md"

API = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
       "{year}/segments/0/leagues/{league_id}")

POS_LIST = ["QB", "RB", "WR", "TE", "K", "DST"]
LATE_GUARD_POS = ("QB", "TE", "K", "DST")
RECENCY_DECAY = 0.8
NFL_TEAM_FIXUP = {"WSH": "WAS"}  # match config/board convention


# --------------------------------------------------------------------------
# pull: network
# --------------------------------------------------------------------------

def _cookies() -> dict:
    return {"espn_s2": os.environ.get("ESPN_S2"), "SWID": os.environ.get("SWID")}


def _fetch_raw_season(league_id: int, year: int) -> dict | None:
    """One GET for draftDetail + mTeam + mSettings. None if the league didn't
    exist yet that year (404)."""
    url = API.format(year=year, league_id=league_id)
    r = requests.get(url, params=[("view", "mDraftDetail"), ("view", "mTeam"),
                                   ("view", "mSettings")],
                      cookies=_cookies(), timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _load_pos_cache() -> dict:
    if POS_CACHE_PATH.exists():
        with open(POS_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_pos_cache(cache: dict) -> None:
    POS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(POS_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=1)


def _resolve_positions(year: int, league_id: int, espn_s2: str, swid: str,
                        player_ids: list[int]) -> dict:
    """One batched kona_playercard call (via espn_api's player_info, which
    accepts a list of ids) resolving playerId -> {name, pos, team}."""
    if not player_ids:
        return {}
    from espn_api.football import League

    lg = League(league_id=league_id, year=year, espn_s2=espn_s2, swid=swid)
    try:
        info = lg.player_info(playerId=player_ids)
    except Exception as e:  # ESPN card lookups can 500 on stale/odd ids
        print(f"  warning: player_info lookup failed for {year} ({e})")
        return {}
    if info is None:
        info = []
    elif not isinstance(info, list):
        info = [info]
    out = {}
    for pl in info:
        pos = pl.position or "?"
        if pos == "D/ST":
            pos = "DST"
        team = pl.proTeam
        if team in (None, "None", ""):
            team = None
        else:
            team = NFL_TEAM_FIXUP.get(team, team)
        out[str(pl.playerId)] = {"name": pl.name, "pos": pos, "team": team}
    return out


def pull_drafts(seasons: list[int]) -> list[dict]:
    """Pull and save draft history for each season. Returns the seasons that
    actually loaded (each: season/teams/order/picks). Skips, and reports
    plainly, any season that errors or has no completed draft."""
    league_id = os.environ.get("LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("SWID")
    if not league_id or not espn_s2 or not swid:
        print("Missing ESPN_S2 / SWID / LEAGUE_ID in .env -- cannot pull league "
              "history. Not fabricating data; stopping.")
        return []
    league_id = int(league_id)

    pos_cache = _load_pos_cache()
    results = []
    for year in seasons:
        try:
            raw = _fetch_raw_season(league_id, year)
        except requests.RequestException as e:
            print(f"season {year}: request failed ({e}) -- skipping")
            continue
        if raw is None:
            print(f"season {year}: league not found (404) -- skipping")
            continue

        dd = raw.get("draftDetail", {})
        picks_raw = dd.get("picks", [])
        if not dd.get("drafted") or not picks_raw:
            print(f"season {year}: no completed draft -- skipping")
            continue

        teams_raw = raw.get("teams", [])
        team_info = {}
        for t in teams_raw:
            name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
            owners = t.get("owners") or []
            owner = owners[0] if owners else t.get("primaryOwner")
            team_info[t["id"]] = {"name": name, "owner": owner}

        order = raw.get("settings", {}).get("draftSettings", {}).get("pickOrder", [])

        player_ids = sorted({p["playerId"] for p in picks_raw if p.get("playerId")})
        missing = [pid for pid in player_ids if str(pid) not in pos_cache]
        if missing:
            resolved = _resolve_positions(year, league_id, espn_s2, swid, missing)
            pos_cache.update(resolved)
            _save_pos_cache(pos_cache)

        picks = []
        for p in sorted(picks_raw, key=lambda x: x["overallPickNumber"]):
            pid = p.get("playerId")
            info = pos_cache.get(str(pid), {})
            team_id = p["teamId"]
            picks.append({
                "overall": p["overallPickNumber"],
                "round": p["roundId"],
                "team_id": team_id,
                "owner": team_info.get(team_id, {}).get("owner"),
                "name": info.get("name") or f"player_{pid}",
                "pos": info.get("pos") or "?",
                "nfl_team": info.get("team"),
                "auto": bool(p.get("autoDraftTypeId")),
                "keeper": bool(p.get("reservedForKeeper")),
            })

        season_dict = {
            "season": year,
            "teams": team_info,
            "order": order,
            "team_count": len(team_info) or len(order),
            "picks": picks,
        }
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_DIR / f"draft_{year}.json", "w") as f:
            json.dump(season_dict, f, indent=1)
        results.append(season_dict)
        print(f"season {year}: loaded {len(picks)} picks, {len(team_info)} teams "
              f"({len(missing)} new player lookups)")
    return results


# --------------------------------------------------------------------------
# learn: offline, pure
# --------------------------------------------------------------------------

def _team_lookup(teams: dict, team_id) -> dict:
    return teams.get(team_id) or teams.get(str(team_id)) or {}


def _manager_key(owner, team_id) -> str:
    return owner if owner else f"team_{team_id}"


def _weight(season: int, current_year: int) -> float:
    return RECENCY_DECAY ** (current_year - 1 - season)


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """pairs of (value, weight); None if no weight."""
    wsum = sum(w for _, w in pairs)
    if wsum <= 0:
        return None
    return sum(v * w for v, w in pairs) / wsum


def _load_board_index() -> dict:
    try:
        with open(BOARD_PATH) as f:
            board = json.load(f)
        return {norm_name(p["name"]): p for p in board["players"]}
    except Exception:
        return {}


def _labels(first_pos_round: dict, early_pos_rate: dict, autopick_rate: float,
            favorite_nfl_team: dict | None,
            league_first_pos_round: dict | None = None) -> list[str]:
    """Labels are RELATIVE to this league: "QB-early" means at least 1.5 rounds
    before the league's typical first QB, not an absolute round number (in a
    16-team room the whole league takes QBs earlier than a 12-team one)."""
    labels = []
    lg = league_first_pos_round or {}
    for pos in ("QB", "TE"):
        fpr = first_pos_round.get(pos)
        ref = lg.get(pos)
        if fpr is None:
            continue
        if ref is None:                       # no league context: absolute fallback
            ref = 6.0 if pos == "QB" else 7.0
        if fpr <= ref - 1.5:
            labels.append(f"{pos}-early")
        elif fpr >= ref + 2.5:
            labels.append(f"{pos}-late")
    if early_pos_rate.get("RB", 0.0) >= 0.6:
        labels.append("RB-heavy")
    if early_pos_rate.get("WR", 0.0) >= 0.6:
        labels.append("WR-heavy")
    if early_pos_rate.get("RB", 1.0) <= 0.15:
        labels.append("zero-RB")
    if favorite_nfl_team:
        labels.append(f"homer:{favorite_nfl_team['team']}")
    if autopick_rate >= 0.5:
        labels.append("autopicker")
    if not labels:
        labels.append("ADP-robot")
    return labels


def learn(drafts: list[dict], current_year: int = 2026) -> dict:
    """Pure function: seasons of pulled draft data -> tendencies model."""
    seasons_list = sorted(d["season"] for d in drafts)
    if not drafts:
        return {"seasons": [], "managers": {}}

    # League-wide rounds 1-8 position shares, per season.
    league_r1_8 = {}
    for d in drafts:
        counts, total = {}, 0
        for p in d["picks"]:
            if p["round"] <= 8:
                total += 1
                counts[p["pos"]] = counts.get(p["pos"], 0) + 1
        league_r1_8[d["season"]] = {"total": total, "counts": counts}

    team_count_by_season = {d["season"]: (d.get("team_count") or len(d.get("teams", {}))) for d in drafts}

    # League-wide "first round a team takes each position", per season (median
    # across teams; a team that never took it counts as one round past the
    # end). Labels are judged against this, not against absolute rounds.
    league_fpr_pairs = {pos: [] for pos in POS_LIST}
    for d in drafts:
        w = _weight(d["season"], current_year)
        max_round = max((p["round"] for p in d["picks"]), default=0)
        first_by_team: dict = {}
        for p in d["picks"]:
            fb = first_by_team.setdefault(p["team_id"], {})
            if p["pos"] in POS_LIST and (p["pos"] not in fb or p["round"] < fb[p["pos"]]):
                fb[p["pos"]] = p["round"]
        for pos in POS_LIST:
            vals = sorted(fb.get(pos, max_round + 1) for fb in first_by_team.values())
            if vals:
                league_fpr_pairs[pos].append((vals[len(vals) // 2], w))
    league_first_pos_round = {pos: round(_weighted_mean(v), 2)
                              for pos, v in league_fpr_pairs.items() if v}

    # Group picks by manager (owner SWID if we have it, else team_id).
    managers: dict = {}
    for d in drafts:
        season = d["season"]
        teams = d["teams"]
        for p in d["picks"]:
            key = _manager_key(p.get("owner"), p["team_id"])
            m = managers.setdefault(key, {
                "team_ids": set(), "seasons": set(), "latest_team_name": None,
                "_latest_season": None, "picks_by_season": {},
            })
            m["team_ids"].add(p["team_id"])
            m["seasons"].add(season)
            # latest season this manager held this team id — team ids get
            # reassigned when a manager leaves, so a slot lookup must prefer
            # the most recent holder.
            tl = m.setdefault("team_id_latest", {})
            tl[str(p["team_id"])] = max(tl.get(str(p["team_id"]), 0), season)
            m["picks_by_season"].setdefault(season, []).append(p)
            if m["_latest_season"] is None or season >= m["_latest_season"]:
                m["_latest_season"] = season
                m["latest_team_name"] = _team_lookup(teams, p["team_id"]).get("name")

    board_idx = _load_board_index()
    out_managers = {}
    for key, m in managers.items():
        n_seasons = len(m["seasons"])
        confidence = min(n_seasons / 4.0, 1.0)
        seasons_sorted = sorted(m["picks_by_season"])
        seasons_desc = sorted(m["picks_by_season"], reverse=True)

        ratio_pairs = {pos: [] for pos in POS_LIST}       # (ratio, weight)
        early_rate_pairs = {pos: [] for pos in POS_LIST}  # (share, weight)
        first_pos_pairs = {pos: [] for pos in POS_LIST}   # (round, weight)
        auto_weighted, auto_total_w = 0.0, 0.0
        loyalty: dict = {}
        nfl_counts: dict = {}

        for season in seasons_sorted:
            picks = m["picks_by_season"][season]
            w = _weight(season, current_year)

            r1_8 = [p for p in picks if p["round"] <= 8]
            r1_4 = [p for p in picks if p["round"] <= 4]
            lg = league_r1_8.get(season, {"total": 0, "counts": {}})

            if r1_8 and lg["total"]:
                m_total = len(r1_8)
                for pos in POS_LIST:
                    m_share = sum(1 for p in r1_8 if p["pos"] == pos) / m_total
                    l_share = lg["counts"].get(pos, 0) / lg["total"]
                    if l_share > 0:
                        ratio_pairs[pos].append((m_share / l_share, w))
                    elif m_share == 0:
                        ratio_pairs[pos].append((1.0, w))
                    # if league never takes it there but manager does, leave
                    # undefined for this season rather than inventing a ratio.

            if r1_4:
                m_total4 = len(r1_4)
                for pos in POS_LIST:
                    share = sum(1 for p in r1_4 if p["pos"] == pos) / m_total4
                    early_rate_pairs[pos].append((share, w))

            first_seen: dict = {}
            for p in picks:
                pos = p["pos"]
                if pos in POS_LIST and (pos not in first_seen or p["round"] < first_seen[pos]):
                    first_seen[pos] = p["round"]
            for pos, rnd in first_seen.items():
                first_pos_pairs[pos].append((rnd, w))

            for p in picks:
                auto_total_w += w
                if p.get("auto"):
                    auto_weighted += w
                nm = norm_name(p["name"])
                loyalty[nm] = loyalty.get(nm, 0) + 1
                if p.get("nfl_team"):
                    nfl_counts[p["nfl_team"]] = nfl_counts.get(p["nfl_team"], 0) + 1

        pos_bias = {}
        for pos in POS_LIST:
            raw = _weighted_mean(ratio_pairs[pos])
            raw = 1.0 if raw is None else raw
            biased = 1 + (raw - 1) * confidence
            pos_bias[pos] = max(0.4, min(2.5, biased))

        first_pos_round = {}
        for pos in POS_LIST:
            v = _weighted_mean(first_pos_pairs[pos])
            if v is not None:
                first_pos_round[pos] = v

        early_pos_rate = {}
        for pos in POS_LIST:
            v = _weighted_mean(early_rate_pairs[pos])
            if v is not None:
                early_pos_rate[pos] = v

        autopick_rate = (auto_weighted / auto_total_w) if auto_total_w > 0 else 0.0

        # Homer: only when one NFL team is drafted far above chance. With N
        # picks spread over 32 teams the expected max for a neutral drafter is
        # a handful, so require >= 5 AND >= 2.5x the per-team expectation.
        favorite_nfl_team = None
        if nfl_counts:
            total_picks = sum(len(v) for v in m["picks_by_season"].values())
            team, count = max(nfl_counts.items(), key=lambda kv: kv[1])
            if count >= max(5, 2.5 * total_picks / 32):
                favorite_nfl_team = {"team": team, "count": count}

        # reach_index: most recent season with any current-board name match.
        reach_index = None
        for season in seasons_desc:
            team_count = team_count_by_season.get(season) or 16
            diffs = []
            for p in m["picks_by_season"][season]:
                if p["round"] > 6:
                    continue
                bp = board_idx.get(norm_name(p["name"]))
                if not bp or not bp.get("adp_overall"):
                    continue
                adp_scaled = bp["adp_overall"] * (team_count / 12.0)
                diffs.append(adp_scaled - p["overall"])
            if diffs:
                reach_index = sum(diffs) / len(diffs)
                break

        loyalty_repeat = {n: c for n, c in loyalty.items() if c >= 2}
        labels = _labels(first_pos_round, early_pos_rate, autopick_rate, favorite_nfl_team,
                         league_first_pos_round)

        out_managers[key] = {
            "team_ids": sorted(m["team_ids"]),
            "team_id_latest": m.get("team_id_latest", {}),
            "latest_team_name": m["latest_team_name"],
            "seasons": n_seasons,
            "pos_bias": pos_bias,
            "first_pos_round": first_pos_round,
            "early_pos_rate": early_pos_rate,
            "reach_index": reach_index,
            "autopick_rate": autopick_rate,
            "favorite_nfl_team": favorite_nfl_team,
            "loyalty": loyalty_repeat,
            "confidence": confidence,
            "labels": labels,
        }

    return {"seasons": seasons_list, "managers": out_managers,
            "league_first_pos_round": league_first_pos_round}


def load_tendencies() -> dict:
    if TENDENCIES_PATH.exists():
        with open(TENDENCIES_PATH) as f:
            return json.load(f)
    return {}


def pos_multiplier(profile: dict, pos: str, round_no: int) -> float:
    bias = (profile.get("pos_bias") or {}).get(pos, 1.0)
    if pos in LATE_GUARD_POS:
        fpr = (profile.get("first_pos_round") or {}).get(pos)
        if fpr is not None and round_no < fpr - 1.5:
            return min(bias, 1.0) * 0.6
    return bias


def tendencies_by_slot(order: list[int]) -> dict:
    """Map a draft-order list of team_ids (slot 1..N) to each slot's profile.

    Team ids are reused when a manager leaves the league, so several profiles
    can claim the same id; the one that held it most recently wins."""
    model = load_tendencies()
    managers = model.get("managers", {})
    by_team_id: dict = {}
    for profile in managers.values():
        latest = profile.get("team_id_latest", {})
        for tid in profile.get("team_ids", []):
            season = latest.get(str(tid), 0)
            cur = by_team_id.get(tid)
            if cur is None or season > cur[0]:
                by_team_id[tid] = (season, profile)
    return {slot: (by_team_id.get(team_id, (0, {}))[1])
            for slot, team_id in enumerate(order, start=1)}


def mock_order(season: int | None = None) -> tuple[list[int], dict[int, str]]:
    """A past season's (draft order as team ids, {slot: team name}).

    The stand-in for this year's order until ESPN publishes it. With no season
    given, the latest pulled season is used. ([], {}) if none.
    """
    files = sorted(HISTORY_DIR.glob("draft_*.json"))
    if not files:
        return [], {}
    path = HISTORY_DIR / f"draft_{season}.json" if season else files[-1]
    if not path.exists():
        return [], {}
    h = json.loads(path.read_text())
    order = [int(t) for t in h.get("order", [])]
    teams = h.get("teams", {})
    labels = {
        i + 1: (teams.get(str(t)) or teams.get(t) or {}).get("name", f"team {t}")
        for i, t in enumerate(order)
    }
    return order, labels


def bias_for_slots(
    order: list[int], round_no: int, positions: tuple[str, ...] = tuple(POS_LIST)
) -> dict[int, dict[str, float]]:
    """{slot: {pos: multiplier}} for the managers in `order` at this round.

    What the bots and the survival model use to draft like the real people.
    """
    out: dict[int, dict[str, float]] = {}
    for slot, prof in tendencies_by_slot(order).items():
        if prof:
            out[slot] = {pos: pos_multiplier(prof, pos, round_no) for pos in positions}
    return out


# --------------------------------------------------------------------------
# dossier
# --------------------------------------------------------------------------

def write_dossier(model: dict, drafts: list[dict], path: Path = REPORT_PATH) -> None:
    """Human-readable scouting report. Team names only -- never SWIDs."""
    team_names: dict = {}
    first_round: dict = {}
    for d in drafts:
        season = d["season"]
        teams = d["teams"]
        for p in d["picks"]:
            key = _manager_key(p.get("owner"), p["team_id"])
            tinfo = _team_lookup(teams, p["team_id"])
            team_names.setdefault(key, {})[season] = tinfo.get("name", "?")
            if p["round"] == 1:
                first_round.setdefault(key, {})[season] = f"{p['name']} ({p['pos']})"

    lines = ["# League DNA -- F3 draft tendencies", "",
             f"Seasons analyzed: {', '.join(str(s) for s in model['seasons'])}", ""]
    lfpr = model.get("league_first_pos_round") or {}
    if lfpr:
        lines.append("League-typical first round at position (labels are relative to this): "
                     + ", ".join(f"{p}={v:.1f}" for p, v in lfpr.items()))
        lines.append("")

    def sort_key(kv):
        _, profile = kv
        return profile.get("latest_team_name") or ""

    for key, profile in sorted(model["managers"].items(), key=sort_key):
        display_name = profile.get("latest_team_name") or "(unknown team)"
        lines.append(f"## {display_name}")
        lines.append("")
        tn = team_names.get(key, {})
        if tn:
            lines.append("Team names by season: " + ", ".join(f"{s}: {n}" for s, n in sorted(tn.items())))
        lines.append(f"Seasons of data: {profile['seasons']} (confidence {profile['confidence']:.2f})")
        lines.append(f"Labels: {', '.join(profile['labels'])}")
        lines.append("Position bias (1.0 = league average): " +
                      ", ".join(f"{p}={v:.2f}" for p, v in profile["pos_bias"].items()))
        if profile.get("first_pos_round"):
            lines.append("First pick at position (avg round): " +
                          ", ".join(f"{p}={v:.1f}" for p, v in profile["first_pos_round"].items()))
        if profile.get("early_pos_rate"):
            lines.append("Rounds 1-4 position share: " +
                          ", ".join(f"{p}={v:.2f}" for p, v in profile["early_pos_rate"].items()))
        lines.append(f"Autopick rate: {profile['autopick_rate']:.2f}")
        if profile.get("reach_index") is not None:
            lines.append(f"Reach index (rounds 1-6, +ve = reaches early): {profile['reach_index']:.1f}")
        else:
            lines.append("Reach index: n/a (no recent pick matched the current board)")
        if profile.get("favorite_nfl_team"):
            ft = profile["favorite_nfl_team"]
            lines.append(f"Favorite NFL team: {ft['team']} ({ft['count']} picks)")
        if profile.get("loyalty"):
            top_loyalty = sorted(profile["loyalty"].items(), key=lambda kv: -kv[1])[:8]
            lines.append("Repeat picks: " + ", ".join(f"{n} x{c}" for n, c in top_loyalty))
        fr = first_round.get(key, {})
        if fr:
            lines.append("First-round picks by year: " + ", ".join(f"{s}: {n}" for s, n in sorted(fr.items())))
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_seasons(spec: str) -> list[int]:
    seasons: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            seasons.extend(range(int(a), int(b) + 1))
        else:
            seasons.append(int(part))
    return seasons


def _load_all_history() -> list[dict]:
    if not HISTORY_DIR.exists():
        return []
    drafts = []
    for path in sorted(HISTORY_DIR.glob("draft_*.json")):
        with open(path) as f:
            drafts.append(json.load(f))
    return drafts


def cmd_pull(args) -> None:
    seasons = _parse_seasons(args.seasons)
    pull_drafts(seasons)


def cmd_learn(args) -> None:
    drafts = _load_all_history()
    if not drafts:
        print("No history in data/history/ -- run `pull` first.")
        return
    current_year = espn_client.get_config().get("season", 2026)
    model = learn(drafts, current_year=current_year)
    TENDENCIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TENDENCIES_PATH, "w") as f:
        json.dump(model, f, indent=1)
    write_dossier(model, drafts)
    print(f"Learned tendencies from seasons: {model['seasons']}")
    for profile in model["managers"].values():
        print(f"  {(profile.get('latest_team_name') or '?'):30s} "
              f"labels={profile['labels']}")
    print(f"\nSaved {TENDENCIES_PATH} and {REPORT_PATH}")


def cmd_show(args) -> None:
    model = load_tendencies()
    if not model:
        print("No tendencies.json -- run `learn` first.")
        return
    if args.slot_order:
        order = [int(x) for x in args.slot_order.split(",")]
        by_slot = tendencies_by_slot(order)
        for slot, profile in by_slot.items():
            print(f"Slot {slot:2d}: {(profile.get('latest_team_name') or '(no history)'):30s} "
                  f"labels={profile.get('labels', [])}")
    else:
        for profile in model.get("managers", {}).values():
            print(f"{(profile.get('latest_team_name') or '?'):30s} labels={profile['labels']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull")
    p.add_argument("--seasons", required=True, help="e.g. 2021-2025 or 2022,2023")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("learn")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("show")
    p.add_argument("--slot-order", help="comma-separated team_ids in draft-slot order")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
