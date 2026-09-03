"""Live injury status from Sleeper's public player endpoint.

Sleeper's `/v1/players/nfl` dump is ~5 MB of every NFL player keyed by
Sleeper player id. We fetch it at most once a day, filter down to the handful
of players carrying an active injury designation, and cache that slice under
`data/injuries.json`. Everything is keyed by `draft_tracker.norm_name` so it
lines up with the UDK board.

Scores in the draft engine are rank-space: lower is better, so an injury
"penalty" is a positive number meant to be added to a player's score.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from draft_tracker import DATA, norm_name

SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
CACHE = DATA / "injuries.json"

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K"}
REFRESH_INTERVAL = timedelta(hours=12)
STALE_AFTER = timedelta(days=14)

# Sleeper `injury_status` (and, for the first row, overall `status`) -> our
# chip / severity / penalty. Order matters: checked top to bottom.
STATUS_TABLE = [
    # handled specially in _classify() because it depends on both
    # injury_status and the overall `status` field:
    #   IR / PUP / NFI, or Out while status == Inactive -> ("IR"/"PUP"/"O", "out", 60)
    ("OUT", ("O", "out", 30)),
    ("SUSPENDED", ("SUS", "out", 25)),
    ("SUS", ("SUS", "out", 25)),
    ("DOUBTFUL", ("D", "doubt", 15)),
    ("QUESTIONABLE", ("Q", "quest", 6)),
]

# Designations that count as season/long-term ending regardless of the
# `status` field.
IR_LIKE = {"IR", "PUP", "NFI"}


def _classify(injury_status: str, player_status: str) -> tuple[str, str, float]:
    """Map Sleeper's injury_status/status fields to (chip, severity, penalty)."""
    inj = (injury_status or "").strip().upper()
    stat = (player_status or "").strip().upper()

    if not inj:
        return ("", "none", 0.0)

    if inj in IR_LIKE:
        return (inj, "out", 60.0)
    if inj == "OUT" and stat == "INACTIVE":
        return ("O", "out", 60.0)

    for key, val in STATUS_TABLE:
        if inj == key:
            return val

    # DNR or any other non-empty, unrecognized designation.
    return ("?", "quest", 6.0)


def _fetch_raw(timeout: float = 20.0) -> dict:
    """Hit the Sleeper endpoint. Raises on any network/parse failure."""
    import requests

    resp = requests.get(SLEEPER_URL, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _iso_date(ms_epoch) -> str | None:
    if not ms_epoch:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms_epoch) / 1000.0, tz=timezone.utc)
        return dt.date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _build_cache(raw: dict, fetched_at: datetime) -> dict:
    players: dict[str, dict] = {}
    for _pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        fantasy_positions = p.get("fantasy_positions") or []
        pos = p.get("position")
        if pos not in FANTASY_POSITIONS and not (set(fantasy_positions) & FANTASY_POSITIONS):
            continue

        injury_status = p.get("injury_status") or ""
        player_status = p.get("status") or ""
        if not injury_status and player_status == "Active":
            continue  # healthy — nothing worth caching

        chip, severity, penalty = _classify(injury_status, player_status)
        if severity == "none":
            continue

        full_name = p.get("full_name") or " ".join(
            filter(None, [p.get("first_name"), p.get("last_name")])
        )
        if not full_name:
            continue

        record = {
            "name": full_name,
            "pos": pos,
            "team": p.get("team"),
            "status": injury_status or player_status,
            "chip": chip,
            "severity": severity,
            "penalty": penalty,
            "body_part": p.get("injury_body_part"),
            "note": p.get("injury_notes"),
            "practice": p.get("practice_participation"),
            "updated": _iso_date(p.get("news_updated")),
        }

        keys = {norm_name(full_name)}
        alt = norm_name(
            " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))
        )
        if alt:
            keys.add(alt)

        for key in keys:
            if key:
                players[key] = record

    return {"fetched_at": fetched_at.isoformat(), "players": players}


def _read_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def _is_fresh(cache: dict | None) -> bool:
    if not cache or "fetched_at" not in cache:
        return False
    try:
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
    except ValueError:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched_at < REFRESH_INTERVAL


def refresh(force: bool = False, timeout: float = 20.0) -> dict:
    """Fetch Sleeper's player dump if the cache is missing/stale, else reuse it.

    Never raises: on any network/parse failure the existing cache (or an
    empty one) is returned and a single warning line is printed.
    """
    existing = _read_cache()
    if not force and _is_fresh(existing):
        return existing

    try:
        raw = _fetch_raw(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, never raise
        print(f"warning: injuries.refresh failed ({exc}); using cached data")
        return existing or {"fetched_at": None, "players": {}}

    try:
        cache = _build_cache(raw, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: injuries.refresh failed to parse Sleeper payload ({exc}); using cached data")
        return existing or {"fetched_at": None, "players": {}}

    _write_cache(cache)
    return cache


_LOAD_MEMO: dict = {"key": None, "cache": None}


def load() -> dict:
    """Return the cached dict, or an empty shell if there is no cache yet.

    Memoised on the cache file's (mtime, size) so the scoring loop can call
    `injury_for` per candidate without re-parsing the JSON every time."""
    try:
        st = CACHE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _LOAD_MEMO["key"] == key and _LOAD_MEMO["cache"] is not None:
        return _LOAD_MEMO["cache"]
    cache = _read_cache()
    if cache is None:
        cache = {"fetched_at": None, "players": {}}
    _LOAD_MEMO["key"], _LOAD_MEMO["cache"] = key, cache
    return cache


def injury_for(name: str) -> dict | None:
    """Look up an injury record for `name` (any casing/suffix norm_name handles).

    Returns None when the player is not in the cache (i.e. healthy/unknown).
    """
    cache = load()
    players = cache.get("players", {})
    record = players.get(norm_name(name))
    if record is None:
        return None

    result = {
        "status": record.get("status"),
        "chip": record.get("chip"),
        "body_part": record.get("body_part"),
        "note": record.get("note"),
        "practice": record.get("practice"),
        "updated": record.get("updated"),
        "penalty": record.get("penalty", 0.0),
        "severity": record.get("severity"),
    }

    fetched_at = cache.get("fetched_at")
    if fetched_at and record.get("updated"):
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
            updated_dt = datetime.fromisoformat(record["updated"])
            if fetched_dt.tzinfo is not None:
                updated_dt = updated_dt.replace(tzinfo=fetched_dt.tzinfo)
            if fetched_dt - updated_dt > STALE_AFTER:
                result["stale"] = True
        except ValueError:
            pass

    return result


def injury_penalty(name: str) -> float:
    """Convenience wrapper: 0.0 when the player is unknown/healthy."""
    record = injury_for(name)
    if record is None:
        return 0.0
    return record.get("penalty", 0.0)


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live injury status from Sleeper")
    parser.add_argument("--refresh", action="store_true", help="force a refetch")
    parser.add_argument("--top", type=int, default=50, help="board rows to scan")
    args = parser.parse_args(argv)

    cache = refresh(force=args.refresh)
    players = cache.get("players", {})

    try:
        with open(DATA / "board.json") as f:
            board = json.load(f)["players"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"warning: could not load board.json ({exc})")
        board = []

    board = [p for p in board if p.get("overall_rank")]
    board.sort(key=lambda p: p["overall_rank"])
    board = board[: args.top]

    rows = []
    for p in board:
        rec = injury_for(p["name"])
        if rec is None:
            continue
        rows.append((p, rec))

    print(f"{len(rows)} of top {len(board)} board players carry an injury designation\n")

    header = f"{'rank':>4}  {'name':<24} {'pos':<4} {'chip':<5} {'body part':<14} {'practice':<10} {'updated':<10} penalty"
    print(header)
    print("-" * len(header))
    for p, rec in rows:
        print(
            f"{p['overall_rank']:>4}  {p['name']:<24} {p['pos']:<4} "
            f"{(rec['chip'] or ''):<5} {(rec['body_part'] or ''):<14} "
            f"{(rec['practice'] or ''):<10} {(rec['updated'] or ''):<10} {rec['penalty']:.1f}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
