"""Local draft server: serves the dashboard AND runs mock drafts.

  python src/mock_server.py            # http://localhost:8123/draft_dashboard.html

Endpoints (all GET, used by the dashboard's buttons):
  /api/start?slot=7   new mock; AI teams pick until our first turn
                      (&order=2025|latest|none: which season's draft order the
                       bots borrow so they draft like the real managers)
  /api/draft?name=X   we draft X; AI teams pick until our next turn
  /api/undo           rewind to the start of our previous turn
  /api/avail?q=text   search available players (for drafting off-list)

On real draft day this same server just serves files while draft_live.py --poll
writes live.json.
"""

import json
import random
import re
import sys
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import espn_client  # noqa: F401  loads .env
from draft_tracker import (load_board, load_state, save_state, norm_name,
                           snake_team_for_pick)
from draft_live import build_live, write_live, REPORTS
import draft_sim
import league_history

BOARD = load_board()
RNG = random.Random()
LAST_PUSH = {"hash": None}

PANEL_RE = re.compile(r"^(.*?) / [A-Z]{2,4} [A-Za-z/]+\nR(\d+), P(\d+) - (.+)$", re.M)


def rebuild_from_panel(text: str, slot: int, teams: int = 16, rounds: int = 15) -> int:
    """Rebuild draft state from the ESPN draft-room Picks panel innerText.
    Captures team names from round 1 as slot labels. Idempotent."""
    entries = []
    labels = {}
    for m in PANEL_RE.finditer(text):
        name, rnd, p, tname = m.group(1).strip(), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        slot_of = p if rnd % 2 == 1 else teams + 1 - p
        entries.append((((rnd - 1) * teams + p), slot_of, name))
        labels.setdefault(slot_of, tname)
    # Merge with existing state by pick number — a partial panel (virtualized
    # list showing only recent picks) must never erase earlier picks.
    merged = {}
    old_labels = {}
    team_ids = None
    try:
        prev = load_state()
        if prev.get("teams") == teams and not prev.get("mock"):
            for pk in prev["picks"]:
                merged[pk["pick"]] = pk
            old_labels = {int(k): v for k, v in prev.get("team_labels", {}).items()}
            team_ids = prev.get("team_ids")  # keeps league-DNA bias alive on the backup path
    except Exception:
        pass
    for overall, slot_of, name in entries:
        bp = BOARD.get(norm_name(name))
        merged[overall] = {"pick": overall, "team": slot_of,
                           "name": bp["name"] if bp else name}
    old_labels.update(labels)
    state = {"teams": teams, "slot": slot, "rounds": rounds,
             "team_labels": old_labels,
             "picks": [merged[k] for k in sorted(merged)]}
    if team_ids:
        state["team_ids"] = team_ids
    save_state(state)
    write_live(state, BOARD)
    return len(state["picks"])


def json_bytes(obj) -> bytes:
    return json.dumps(obj).encode()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(REPORTS), **kw)

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json_bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/espn_push":
            q = urllib.parse.parse_qs(url.query)
            slot = int(q.get("slot", ["7"])[0])
            length = int(self.headers.get("Content-Length", 0))
            text = self.rfile.read(min(length, 200_000)).decode("utf-8", "replace")
            h = hash(text)
            if h == LAST_PUSH["hash"]:
                return self.send_json({"ok": True, "unchanged": True})
            LAST_PUSH["hash"] = h
            n = rebuild_from_panel(text, slot)
            return self.send_json({"ok": True, "picks": n})
        return self.send_json({"ok": False, "error": "unknown endpoint"}, 404)

    GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00!\xf9\x04"
           b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/api/img_push":
            # CSP-proof feed: the draft-room page loads this as an <img>.
            # d = compact "r,p,Name;r,p,Name;..." (latest picks); merged by pick no.
            slot = int(q.get("slot", ["5"])[0])
            data = q.get("d", [""])[0]
            try:
                state = load_state()
                if state.get("mock") or state.get("slot") != slot:
                    state = {"teams": 16, "slot": slot, "rounds": 16, "picks": []}
                teams = state.get("teams", 16)
                existing = {p["pick"] for p in state["picks"]}
                for part in data.split(";"):
                    bits = part.split(",", 2)
                    if len(bits) != 3:
                        continue
                    rnd, p, name = int(bits[0]), int(bits[1]), bits[2].strip()
                    overall = (rnd - 1) * teams + p
                    if overall in existing or not name:
                        continue
                    slot_of = p if rnd % 2 == 1 else teams + 1 - p
                    bp = BOARD.get(norm_name(name))
                    state["picks"].append({"pick": overall, "team": slot_of,
                                           "name": bp["name"] if bp else name})
                state["picks"].sort(key=lambda x: x["pick"])
                save_state(state)
                write_live(state, BOARD)
            except Exception as e:
                print("img_push error:", e)
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.GIF)
            return
        if url.path == "/api/start":
            slot = int(q.get("slot", ["7"])[0])
            state = {"teams": 16, "slot": slot, "rounds": 15, "picks": [], "mock": True}
            # A past season's draft order makes the bots draft like the real managers
            # (order=none turns it off; order=2025 picks a season).
            want = q.get("order", ["latest"])[0]
            if want != "none":
                order, labels = league_history.mock_order(None if want == "latest" else int(want))
                if order and len(order) == state["teams"]:
                    state["team_ids"] = order
                    state["team_labels"] = labels
            draft_sim.sim_until_my_turn(state, BOARD, RNG)
            save_state(state)
            write_live(state, BOARD)
            return self.send_json({"ok": True})
        if url.path == "/api/draft":
            name = q.get("name", [""])[0]
            state = load_state()
            bp = BOARD.get(norm_name(name))
            if not bp:
                return self.send_json({"ok": False, "error": f"unknown player: {name}"}, 400)
            if norm_name(name) in {norm_name(p["name"]) for p in state["picks"]}:
                return self.send_json({"ok": False, "error": f"{bp['name']} is already drafted"}, 400)
            pick_no = len(state["picks"]) + 1
            state["picks"].append({"pick": pick_no, "team": state["slot"], "name": bp["name"]})
            draft_sim.sim_until_my_turn(state, BOARD, RNG)
            save_state(state)
            write_live(state, BOARD)
            return self.send_json({"ok": True, "drafted": bp["name"], "at": pick_no})
        if url.path == "/api/reconcile":
            # Mid-draft rescue: rebuild complete state from a PARTIAL panel
            # window. Known picks merge normally; pick numbers below the
            # current pick that remain unknown are filled by inference — the
            # best players by market rank that aren't seen anywhere else are
            # assumed drafted, in order. Good enough for opponent modeling.
            slot = int(q.get("slot", ["1"])[0])
            teams = int(q.get("teams", ["16"])[0])
            rounds = int(q.get("rounds", ["16"])[0])
            current_pick = int(q.get("current", ["0"])[0])  # pick now on the clock
            data = q.get("d", [""])[0]
            state = {"teams": teams, "slot": slot, "rounds": rounds, "picks": []}
            known = {}
            for part in data.split(";"):
                bits = part.split(",", 2)
                if len(bits) != 3:
                    continue
                rnd, p, name = int(bits[0]), int(bits[1]), bits[2].strip()
                overall = (rnd - 1) * teams + p
                slot_of = p if rnd % 2 == 1 else teams + 1 - p
                bp = BOARD.get(norm_name(name))
                known[overall] = {"pick": overall, "team": slot_of,
                                  "name": bp["name"] if bp else name}
            taken_names = {norm_name(v["name"]) for v in known.values()}
            missing = [i for i in range(1, max(current_pick, max(known, default=1)))
                       if i not in known]
            if missing:
                # Market-rank order proxy: overall_rank blended with 16-team ADP.
                def mrank(p):
                    adp = p.get("adp_overall")
                    r = p.get("overall_rank") or 300
                    return 0.5 * r + 0.5 * (adp * 4 / 3) if adp else r * 1.05
                pool = sorted((p for k, p in BOARD.items()
                               if k not in taken_names and p.get("overall_rank")),
                              key=mrank)
                seen_objs = set()
                fill = []
                for p in pool:
                    if id(p) in seen_objs:
                        continue
                    seen_objs.add(id(p))
                    fill.append(p)
                    if len(fill) >= len(missing):
                        break
                for overall, p in zip(missing, fill):
                    rnd = (overall - 1) // teams + 1
                    pos_in = (overall - 1) % teams + 1
                    slot_of = pos_in if rnd % 2 == 1 else teams + 1 - pos_in
                    known[overall] = {"pick": overall, "team": slot_of,
                                      "name": p["name"], "inferred": True}
            state["picks"] = [known[k] for k in sorted(known)]
            save_state(state)
            write_live(state, BOARD)
            inferred = sum(1 for p in state["picks"] if p.get("inferred"))
            return self.send_json({"ok": True, "picks": len(state["picks"]),
                                   "inferred": inferred})
        if url.path == "/api/taken":
            # Manual redundancy: record that a player was just drafted by
            # whichever team is on the clock (works without any ESPN feed).
            name = q.get("name", [""])[0]
            state = load_state()
            bp = BOARD.get(norm_name(name))
            if not bp:
                return self.send_json({"ok": False, "error": f"unknown player: {name}"}, 400)
            if norm_name(name) in {norm_name(p["name"]) for p in state["picks"]}:
                return self.send_json({"ok": False, "error": f"{bp['name']} is already drafted"}, 400)
            pick_no = len(state["picks"]) + 1
            if pick_no > state["teams"] * state["rounds"]:
                return self.send_json({"ok": False, "error": "draft is over"}, 400)
            team = snake_team_for_pick(pick_no, state["teams"])
            state["picks"].append({"pick": pick_no, "team": team, "name": bp["name"]})
            save_state(state)
            write_live(state, BOARD)
            mine = " (OUR pick)" if team == state["slot"] else ""
            return self.send_json({"ok": True, "taken": bp["name"], "at": pick_no, "slot": team, "mine": bool(mine)})
        if url.path == "/api/undo_last":
            state = load_state()
            removed = state["picks"].pop() if state["picks"] else None
            save_state(state)
            write_live(state, BOARD)
            return self.send_json({"ok": True, "removed": removed and removed["name"]})
        if url.path == "/api/undo":
            state = load_state()
            # Pop AI picks back through our most recent pick.
            while state["picks"] and state["picks"][-1]["team"] != state["slot"]:
                state["picks"].pop()
            if state["picks"]:
                state["picks"].pop()
            save_state(state)
            write_live(state, BOARD)
            return self.send_json({"ok": True})
        if url.path == "/api/avail":
            text = q.get("q", [""])[0].lower()
            state = load_state()
            avail = draft_sim.available(state, BOARD)
            hits = [p for p in avail if text in p["name"].lower()]
            hits.sort(key=lambda p: p.get("overall_rank") or 300)
            return self.send_json([{"name": p["name"], "pos": p["pos"], "team": p["team"],
                                    "rank": p.get("overall_rank"), "tier": p.get("tier")}
                                   for p in hits[:10]])
        return super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    print(f"Draft server on http://localhost:{port}/draft_dashboard.html")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
