"""Render data/board.json into the draft-day cheat sheet (reports/cheatsheet.html)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = r"""<meta charset="utf-8">
<title>F³ Draft Board</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Source+Sans+3:ital,wght@0,400;0,600;0,700;1,400&display=swap">
<style>
:root {
  --ground: #F6F7F4; --panel: #FFFFFF; --ink: #182028; --ink-2: #5A6572;
  --line: #DDE1DA; --band: #ECEFE9;
  --rb: #2E7D5B; --wr: #2B62A8; --te: #C05A21; --qb: #8E3B46; --util: #5A6572;
  --good: #1E7A46; --good-bg: #E2F1E7; --bad: #A93226; --bad-bg: #F6E4E1;
  --flag: #8A6D1D; --flag-bg: #F3ECD4;
  --accent: #2E7D5B;
}
:root:not([data-theme="light"]) { }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #141A21; --panel: #1C242E; --ink: #E8ECEF; --ink-2: #98A4B0;
    --line: #2B3540; --band: #202A35;
    --rb: #4FAE85; --wr: #6C9FD8; --te: #E08A50; --qb: #C76D7B; --util: #98A4B0;
    --good: #5BC488; --good-bg: #1C3327; --bad: #E08578; --bad-bg: #3A2320;
    --flag: #D8BC62; --flag-bg: #322B15;
  }
}
:root[data-theme="dark"] {
  --ground: #141A21; --panel: #1C242E; --ink: #E8ECEF; --ink-2: #98A4B0;
  --line: #2B3540; --band: #202A35;
  --rb: #4FAE85; --wr: #6C9FD8; --te: #E08A50; --qb: #C76D7B; --util: #98A4B0;
  --good: #5BC488; --good-bg: #1C3327; --bad: #E08578; --bad-bg: #3A2320;
  --flag: #D8BC62; --flag-bg: #322B15;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: "Source Sans 3", "Helvetica Neue", Arial, sans-serif;
  font-size: 15px; line-height: 1.35;
}
header {
  position: sticky; top: 0; z-index: 10; background: var(--panel);
  border-bottom: 2px solid var(--line); padding: 10px 16px;
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px;
}
header h1 {
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  font-size: 26px; font-weight: 700; margin: 0; letter-spacing: .5px;
}
header .facts { color: var(--ink-2); font-size: 13px; }
header .facts b { color: var(--ink); }
nav { display: flex; gap: 2px; margin-left: auto; flex-wrap: wrap; }
nav button {
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  font-size: 16px; font-weight: 600; letter-spacing: .5px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink-2);
  padding: 4px 12px; cursor: pointer;
}
nav button:focus-visible, .row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
nav button.on { background: var(--ink); color: var(--ground); border-color: var(--ink); }
.tools { display: flex; gap: 10px; align-items: center; padding: 10px 16px; }
.tools input[type=search] {
  flex: 0 1 340px; padding: 6px 10px; font: inherit;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink);
}
.tools label { color: var(--ink-2); font-size: 13px; display: flex; gap: 5px; align-items: center; }
main { padding: 0 16px 60px; max-width: 1100px; margin: 0 auto; }
.tierband { margin: 18px 0 4px; }
.tierhead {
  display: flex; align-items: baseline; gap: 10px;
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  font-size: 18px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase;
  padding: 5px 10px; background: var(--band); border-left: 5px solid var(--accent);
}
.tierhead .left { color: var(--ink-2); font-size: 13px; font-family: "Source Sans 3", sans-serif; font-weight: 600; letter-spacing: 0; text-transform: none; }
.row {
  display: grid; grid-template-columns: 34px 40px 1fr 96px 60px 64px 70px 28px;
  gap: 8px; align-items: center; padding: 5px 10px;
  border-bottom: 1px solid var(--line); background: var(--panel); cursor: pointer;
}
.row:hover { background: var(--band); }
.row .num { font-variant-numeric: tabular-nums; color: var(--ink-2); text-align: right; }
.row .pos {
  font-family: "Barlow Condensed", sans-serif; font-weight: 700; font-size: 14px;
  letter-spacing: .5px; text-align: center; border: 1.5px solid; padding: 0 3px;
}
.pos.RB { color: var(--rb); border-color: var(--rb); }
.pos.WR { color: var(--wr); border-color: var(--wr); }
.pos.TE { color: var(--te); border-color: var(--te); }
.pos.QB { color: var(--qb); border-color: var(--qb); }
.pos.DST, .pos.K { color: var(--util); border-color: var(--util); }
.row .name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .name small { color: var(--ink-2); font-weight: 400; margin-left: 6px; }
.row .meta { color: var(--ink-2); font-size: 13px; font-variant-numeric: tabular-nums; }
.chip {
  font-size: 12px; font-weight: 700; padding: 1px 7px; border-radius: 2px;
  font-variant-numeric: tabular-nums; text-align: center;
}
.chip.good { color: var(--good); background: var(--good-bg); }
.chip.bad { color: var(--bad); background: var(--bad-bg); }
.chip.flag { color: var(--flag); background: var(--flag-bg); }
.chip.mine { border: 1.5px solid currentColor; background: transparent; }
.chip.sleeper { color: var(--wr); }
.chip.bust { color: var(--bad); }
.chip.value { color: var(--good); }
.chip.breakout { color: var(--te); }
.chip.watch { color: var(--ink-2); }
.outlook .mynote { display: block; margin-top: 6px; color: var(--ink); font-weight: 600; }
.row.gone .name, .row.gone .meta, .row.gone .num { text-decoration: line-through; opacity: .38; }
.row.gone .chip, .row.gone .pos { opacity: .25; }
.outlook {
  grid-column: 1 / -1; display: none; color: var(--ink-2); font-size: 14px;
  padding: 4px 6px 8px 82px; max-width: 72ch;
}
.row.open .outlook { display: block; }
.gonebox { justify-self: center; width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }
.empty { color: var(--ink-2); padding: 30px 0; text-align: center; }
@media (max-width: 700px) {
  .row { grid-template-columns: 30px 36px 1fr 60px 28px; }
  .row .adp, .row .hosts { display: none; }
  .outlook { padding-left: 10px; }
}
@media (prefers-reduced-motion: no-preference) {
  .row { transition: background .12s; }
}
</style>
<header>
  <h1>F³ DRAFT BOARD</h1>
  <span class="facts"><b>16-team</b> · <b>Full PPR</b> · 4pt pass TD · 1QB 2RB 2WR 1TE 1FLX · waiver priority · UDK tiers</span>
  <nav id="tabs"></nav>
</header>
<div class="tools">
  <input id="q" type="search" placeholder="Search player or team…" aria-label="Search players">
  <label><input id="hidegone" type="checkbox"> hide drafted</label>
  <label id="gonecount"></label>
</div>
<main id="list"></main>
<script>
const BOARD = /*BOARD_JSON*/;
const TABS = ["TOP 200","RB","WR","TE","QB","DST","K"];
let tab = "TOP 200", query = "", hideGone = false;
let gone = {};
try { gone = JSON.parse(localStorage.getItem("f3-gone") || "{}"); } catch (e) {}
function saveGone() { try { localStorage.setItem("f3-gone", JSON.stringify(gone)); } catch (e) {} }
const key = p => p.name + "|" + p.pos;

function playersFor(t) {
  if (t === "TOP 200") return BOARD.players.filter(p => p.overall_rank).sort((a,b) => a.overall_rank - b.overall_rank);
  return BOARD.players.filter(p => p.pos === t && p.pos_rank).sort((a,b) => a.pos_rank - b.pos_rank);
}
function tierOf(p, t) {
  if (t === "TOP 200") return p.tier != null ? null : null; // top-200 view: band by round-of-16 instead
  return p.tier;
}
function bands(t) {
  const ps = playersFor(t), out = [];
  if (t === "TOP 200") {
    // Band the overall list into 16-pick rounds — matches this league's draft rhythm.
    ps.forEach(p => {
      const r = Math.floor((p.overall_rank - 1) / 16) + 1;
      if (!out.length || out[out.length-1].label !== "ROUND " + r) out.push({ label: "ROUND " + r, players: [] });
      out[out.length-1].players.push(p);
    });
  } else if (t === "DST" || t === "K") {
    out.push({ label: t === "DST" ? "D/ST — stream, draft last" : "KICKERS — draft last", players: ps });
  } else {
    ps.forEach(p => {
      const label = "TIER " + (p.tier ?? "—");
      if (!out.length || out[out.length-1].label !== label) out.push({ label, players: [] });
      out[out.length-1].players.push(p);
    });
  }
  return out;
}
function match(p) {
  if (hideGone && gone[key(p)]) return false;
  if (!query) return true;
  return (p.name + " " + p.team).toLowerCase().includes(query);
}
function chipVal(p) {
  if (p.value_vs_adp == null) return "";
  if (p.value_vs_adp >= 4) return '<span class="chip good">+' + p.value_vs_adp + ' val</span>';
  if (p.value_vs_adp <= -4) return '<span class="chip bad">' + p.value_vs_adp + ' rch</span>';
  return "";
}
function chipMine(p) {
  return (p.my_tags || []).map(t =>
    '<span class="chip mine ' + t + '">' + ({sleeper:"SLPR",bust:"BUST",breakout:"BRKT",value:"TGT",watch:"WATCH"}[t] || t.toUpperCase()) + "</span>").join(" ");
}
function chipHosts(p) {
  return (p.host_spread != null && p.host_spread >= 15)
    ? '<span class="chip flag" title="Andy/Jason/Mike disagree: ' + [p.andy,p.jason,p.mike].join("/") + '">split</span>' : "";
}
function render() {
  document.getElementById("tabs").innerHTML = TABS.map(t =>
    '<button class="' + (t===tab?"on":"") + '" data-t="' + t + '">' + t + "</button>").join("");
  const list = document.getElementById("list");
  let html = "", shown = 0;
  for (const band of bands(tab)) {
    const vis = band.players.filter(match);
    if (!vis.length) continue;
    shown += vis.length;
    html += '<section class="tierband"><div class="tierhead">' + band.label +
            ' <span class="left">' + vis.filter(p => !gone[key(p)]).length + " left</span></div>";
    for (const p of vis) {
      const g = gone[key(p)] ? " gone" : "";
      const rank = tab === "TOP 200" ? p.overall_rank : p.pos_rank;
      html += '<div class="row' + g + '" tabindex="0" data-k="' + key(p).replace(/"/g,"&quot;") + '">' +
        '<span class="num">' + rank + "</span>" +
        '<span class="pos ' + p.pos + '">' + (p.pos === "DST" ? "D" : p.pos) + "</span>" +
        '<span class="name">' + p.name + "<small>" + p.team + " · bye " + (p.bye ?? "—") + "</small></span>" +
        '<span class="meta adp">' + (p.adp ? "ADP " + p.adp : "") + "</span>" +
        '<span>' + (chipMine(p) || chipVal(p)) + "</span>" +
        '<span class="hosts">' + chipHosts(p) + "</span>" +
        '<span class="meta">' + (p.proj_points ? p.proj_points + " pt" : "") + "</span>" +
        '<input type="checkbox" class="gonebox" aria-label="mark drafted"' + (gone[key(p)] ? " checked" : "") + ">" +
        ((p.outlook || p.my_notes) ? '<div class="outlook">' + (p.outlook || "") +
          (p.my_notes || []).map(n => '<span class="mynote">✎ ' + n + "</span>").join("") + "</div>" : "") +
        "</div>";
    }
    html += "</section>";
  }
  list.innerHTML = html || '<div class="empty">No players match.</div>';
  const n = Object.values(gone).filter(Boolean).length;
  document.getElementById("gonecount").textContent = n ? n + " marked drafted" : "";
}
document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return; tab = b.dataset.t; render();
});
document.getElementById("q").addEventListener("input", e => { query = e.target.value.toLowerCase(); render(); });
document.getElementById("hidegone").addEventListener("change", e => { hideGone = e.target.checked; render(); });
document.getElementById("list").addEventListener("click", e => {
  const row = e.target.closest(".row"); if (!row) return;
  if (e.target.classList.contains("gonebox")) {
    gone[row.dataset.k] = e.target.checked; saveGone(); render(); return;
  }
  row.classList.toggle("open");
});
document.getElementById("list").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.classList.contains("row")) e.target.classList.toggle("open");
});
render();
</script>
"""


def main() -> None:
    with open(ROOT / "data" / "board.json") as f:
        board = json.load(f)
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    html = TEMPLATE.replace("/*BOARD_JSON*/", json.dumps(board))
    out = out_dir / "cheatsheet.html"
    out.write_text(html)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
