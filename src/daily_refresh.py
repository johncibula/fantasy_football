"""Daily data refresh: injuries (Sleeper), UDK rankings (FootClan export),
market rankings (ESPN draft-room rank/ADP + FFC ADP), board rebuild, and a
"what moved" report.

  ./venv/bin/python src/daily_refresh.py            # everything
  ./venv/bin/python src/daily_refresh.py --no-udk   # skip the browser export
  ./venv/bin/python src/daily_refresh.py --diff-only

Runs from launchd every morning (see docs/plans/07-daily-refresh.md). Each
step is independent: if the UDK export fails (session expired, layout change)
the injuries still refresh and the board is rebuilt from the last good CSVs.
Writes reports/udk_changes.md and appends one line to data/logs/refresh.log.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

import espn_client  # noqa: E402,F401  (loads .env)
from draft_tracker import DATA, norm_name  # noqa: E402

REPORTS = ROOT / "reports"
LOG_DIR = DATA / "logs"
BOARD = DATA / "board.json"
BOARD_PREV = DATA / "board_prev.json"
CHANGES = REPORTS / "udk_changes.md"
PY = sys.executable


def _players(board: dict) -> dict[str, dict]:
    return {norm_name(p["name"]): p for p in board.get("players", [])}


def board_diff(old: dict, new: dict, top: int = 120) -> dict:
    """Rank movers, tier changes, projection swings, adds/drops between two
    board.json payloads. Pure; used by the report and the tests."""
    o, n = _players(old), _players(new)
    movers, tiers, proj, added, dropped = [], [], [], [], []
    for k, p in n.items():
        q = o.get(k)
        nr = p.get("overall_rank")
        if q is None:
            if nr and nr <= top + 40:
                added.append(p)
            continue
        orr = q.get("overall_rank")
        if nr and orr and (nr <= top or orr <= top) and nr != orr:
            movers.append((orr - nr, p, orr, nr))  # +ve = moved up
        if p.get("tier") and q.get("tier") and p["tier"] != q["tier"] and (nr or 999) <= top + 40:
            tiers.append((p, q["tier"], p["tier"]))
        pp, qp = p.get("proj_points"), q.get("proj_points")
        if pp and qp and abs(pp - qp) >= 8 and (nr or 999) <= top + 40:
            proj.append((pp - qp, p, qp, pp))
    for k, q in o.items():
        if k not in n and (q.get("overall_rank") or 999) <= top + 40:
            dropped.append(q)
    movers.sort(key=lambda m: -abs(m[0]))
    proj.sort(key=lambda m: -abs(m[0]))
    return {"movers": movers, "tiers": tiers, "proj": proj, "added": added, "dropped": dropped,
            "n_old": len(o), "n_new": len(n)}


def write_changes(diff: dict, when: str) -> str:
    lines = [f"# UDK changes — {when}", ""]
    lines.append(f"Players on board: {diff['n_old']} → {diff['n_new']}.")
    lines.append("")
    if not any(diff[k] for k in ("movers", "tiers", "proj", "added", "dropped")):
        lines.append("No ranking changes since the last refresh.")
    if diff["movers"]:
        lines += ["## Rank movers (top 120)", "", "| Δ | player | pos | was | now |", "|---|---|---|---|---|"]
        for d, p, orr, nr in diff["movers"][:30]:
            lines.append(f"| {d:+d} | {p['name']} | {p['pos']} | {orr} | {nr} |")
        lines.append("")
    if diff["tiers"]:
        lines += ["## Tier changes", ""]
        for p, a, b in diff["tiers"]:
            lines.append(f"- {p['name']} ({p['pos']}): tier {a} → {b}")
        lines.append("")
    if diff["proj"]:
        lines += ["## Projection swings (≥ 8 pts)", ""]
        for d, p, a, b in diff["proj"][:20]:
            lines.append(f"- {p['name']} ({p['pos']}): {a:.0f} → {b:.0f} ({d:+.0f})")
        lines.append("")
    if diff["added"]:
        lines += ["## New on the board", ""] + [f"- {p['name']} ({p['pos']}, #{p.get('overall_rank')})" for p in diff["added"]] + [""]
    if diff["dropped"]:
        lines += ["## Gone from the board", ""] + [f"- {p['name']} ({p['pos']}, was #{p.get('overall_rank')})" for p in diff["dropped"]] + [""]
    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(exist_ok=True)
    CHANGES.write_text(text)
    return text


def step(label: str, argv: list[str], timeout: int = 600) -> tuple[bool, str]:
    t0 = time.time()
    try:
        r = subprocess.run([PY] + argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        ok = r.returncode == 0
        tail = out.splitlines()[-1] if out else ""
        print(f"[{label}] {'ok' if ok else f'exit {r.returncode}'} in {time.time() - t0:.0f}s — {tail}")
        return ok, out
    except subprocess.TimeoutExpired:
        print(f"[{label}] timed out after {timeout}s")
        return False, "timeout"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-udk", action="store_true")
    ap.add_argument("--no-injuries", action="store_true")
    ap.add_argument("--diff-only", action="store_true", help="just rebuild the board and diff")
    args = ap.parse_args()
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    results = {}

    if not args.diff_only and not args.no_injuries:
        results["injuries"] = step("injuries", ["src/injuries.py", "--refresh", "--top", "0"])[0]
    if not args.diff_only and not args.no_udk:
        ok, out = step("udk", ["src/udk_fetch.py"], timeout=900)
        results["udk"] = ok
        if "Session expired" in out or "No saved session" in out:
            results["udk_session"] = False

    if not args.diff_only:
        results["market"] = step("market", ["src/market_feed.py", "--refresh", "--top", "0"])[0]
    if not args.diff_only and not args.no_udk and results.get("udk_session", True):
        results["pages"] = step("pages", ["src/player_pages.py", "--top", "200"], timeout=1800)[0]

    if BOARD.exists():
        shutil.copy2(BOARD, BOARD_PREV)
    results["board"] = step("board", ["src/draft_board.py"])[0]

    diff_summary = "no previous board"
    if BOARD.exists() and BOARD_PREV.exists():
        old = json.loads(BOARD_PREV.read_text())
        new = json.loads(BOARD.read_text())
        diff = board_diff(old, new)
        write_changes(diff, when)
        diff_summary = (f"{len(diff['movers'])} movers, {len(diff['tiers'])} tier changes, "
                        f"{len(diff['added'])} added, {len(diff['dropped'])} dropped")
        print(f"[diff] {diff_summary} → {CHANGES}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "refresh.log", "a") as f:
        f.write(f"{when}  " + "  ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in results.items())
                + f"  {diff_summary}\n")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
