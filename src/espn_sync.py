"""Sync ESPN draft-room picks (stdin) to state + live.json.
Accepts panel innerText OR compact 'r,p,name;r,p,name;...' format."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import espn_client  # noqa
from draft_tracker import load_board, save_state, norm_name
from draft_live import write_live

slot = int(sys.argv[1]) if len(sys.argv) > 1 else 7
text = sys.stdin.read().strip()
entries = []
if ";" in text and re.match(r"^\d+,\d+,", text):
    for part in text.split(";"):
        rnd, p, name = part.split(",", 2)
        entries.append(((int(rnd) - 1) * 16 + int(p), name.strip(), int(rnd), int(p)))
else:
    pat = re.compile(r"^(.*?) / [A-Z]{2,4} [A-Za-z/]+\nR(\d+), P(\d+) - ", re.M)
    for m in pat.finditer(text):
        entries.append((((int(m.group(2)) - 1) * 16 + int(m.group(3))), m.group(1).strip(),
                        int(m.group(2)), int(m.group(3))))
entries.sort()
board = load_board()
state = {"teams": 16, "slot": slot, "rounds": 15, "picks": []}
for overall, name, rnd, p in entries:
    slot_of = p if rnd % 2 == 1 else 17 - p
    bp = board.get(norm_name(name))
    state["picks"].append({"pick": overall, "team": slot_of, "name": bp["name"] if bp else name})
save_state(state)
write_live(state, board)
print(f"synced {len(entries)} picks; next pick {len(entries)+1}")
