"""Draft Command for anyone's rankings: the draft engine as a public Streamlit app.

  ./venv/bin/streamlit run src/draft_app.py

A visitor uploads a rankings CSV (or uses the bundled ESPN sample), picks the
league size, seat and mode, and gets the same recommendations, factor
breakdowns, Monte Carlo survival odds, market read and roster view the owner
uses on draft night. The owner's board, notes and league history never load:
the engine scores whatever rankings the visitor brought. Injury designations
come from Sleeper's public feed, refreshed once per process.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import injuries
from draft_session import DraftError, DraftSession, LeagueProfile
from draft_tracker import norm_name
from rankings_import import ImportedBoard, RankingsFormatError, RankingsImporter, template_csv

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "samples" / "sample_rankings.csv"
SOURCE_SAMPLE = "Sample: ESPN default rankings"
SOURCE_UPLOAD = "Upload my own CSV"
MODE_MOCK = "Mock draft against AI opponents"
MODE_ASSIST = "Assist my live draft"
TEAM_RANGE = (4, 16)
ROUND_RANGE = (8, 20)
DEFAULT_TEAMS = 12
DEFAULT_ROUNDS = 15
SURV_SAFE = 60
SURV_GONE = 25
SOON_PICKS = 3
TOP_FACTORS = 4
HOT_RUN = 4
ROOM_GAP = 15
TEAMS_PER_ROW = 4
LOOKAHEAD_ROWS = 30
SEARCH_LIMIT = 400
INJURY_TTL_S = 6 * 60 * 60

POS_COLORS = {
    "QB": "#C76D7B",
    "RB": "#4FAE85",
    "WR": "#6C9FD8",
    "TE": "#E08A50",
    "K": "#98A4B0",
    "DST": "#98A4B0",
}
TAG_LABELS = {
    "sleeper": "SLPR",
    "bust": "BUST",
    "breakout": "BRKT",
    "value": "TGT",
    "watch": "WATCH",
}
TAG_COLORS = {
    "sleeper": "#6C9FD8",
    "bust": "#E08578",
    "breakout": "#E08A50",
    "value": "#5BC488",
    "watch": "#98A4B0",
}
GOOD, BAD, FLAG, MUTED = "#5BC488", "#E08578", "#D8BC62", "#98A4B0"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&display=swap');
.dc-title{font-family:"Barlow Condensed","Arial Narrow",sans-serif;letter-spacing:2px;font-size:36px;
  margin:0;line-height:1.1}
.dc-sub{color:#98A4B0;margin:2px 0 10px}
.pos{display:inline-block;font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:14px;
  border:1.5px solid;padding:0 5px;border-radius:2px;letter-spacing:.5px}
.nm{font-weight:700;font-size:16px}
.nm small{color:#98A4B0;font-weight:400;margin-left:6px;font-size:12.5px}
.chip{display:inline-block;font-size:10.5px;font-weight:700;padding:0 5px;margin-left:5px;
  border:1.5px solid currentColor;border-radius:2px;vertical-align:middle}
.surv{font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:22px;
  font-variant-numeric:tabular-nums}
.meta{color:#98A4B0;font-size:13px;font-variant-numeric:tabular-nums}
.f{display:inline-block;font-size:12px;color:#98A4B0;margin-right:10px;white-space:nowrap}
.f.good{color:#5BC488}.f.bad{color:#E08578}
.wait{color:#98A4B0;font-size:12px;font-style:italic}
.pill{display:inline-block;font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:15px;
  padding:1px 9px;border:1.5px solid #2B3540;margin:2px 4px 2px 0;border-radius:2px}
.pill.hot{border-color:#D8BC62;color:#D8BC62;background:#322B15}
.roster span{display:inline-block;background:#202A35;border:1px solid #2B3540;padding:2px 8px;
  margin:2px;font-size:13px}
.card h4{margin:0 0 6px;font-family:"Barlow Condensed",sans-serif;letter-spacing:1px;font-size:16px}
.tag{color:#D8BC62;font-size:12px;margin-left:6px}
.missing{color:#E08578;font-size:12px;font-weight:700;margin-bottom:4px}
.card ul{list-style:none;padding:0;margin:0;font-size:12.5px}
.card li{padding:1px 0}
.pk{color:#98A4B0;display:inline-block;width:26px;text-align:right;margin-right:6px;
  font-variant-numeric:tabular-nums}
.note{color:#E8ECEF;font-size:13.5px;line-height:1.5}
</style>
"""

HOW_IT_THINKS = """
### One line
Follow **your** rankings, tilted by what the room is about to do to you: every pick is scored
against what will still be on the board when you pick again.

### The score
Each recommendation carries a score in rank units, **lower is better**, and the factor chips
under a name sum exactly to that score. Hover a chip for the reason.

| Factor | What it does |
|---|---|
| **Your rank** | Your overall rank times 0.6. The backbone. |
| **VBD** | Value over replacement: projected points above the last starter-quality player at the position for this league size, converted to rank units and weighted 0.4. Needs a Proj column. |
| **Need** | How hungry your roster is at the position. Missing starters are urgent. |
| **VONA** | Value over next available: points over the best *other* player at the position the simulation expects at your next pick, counted only to the extent he might be gone. Needs Proj. |
| **Survival** | The simulated chance he is still there at your next pick. Shown, not scored: the other factors already price it. |
| **Injury** | Sleeper's live designation. A bare Questionable counts for a third. |
| **Bye clash / Stack** | Penalises a third starter on the same bye; rewards a pass-catcher with your QB from round 4 on. |
| **Plan rails** | Kicker and defence only when the roster must be filled; a QB timer that fires when the market is about to take the last starter-quality QBs; RB depth from round 5; a WR surplus brake; tagged TEs on a great-or-late plan. |
| **Tags** | Your own scouting from a Tags column: `target`/`value`, `sleeper`, `breakout`, `watch`, `bust`/`avoid`. |

### The room
The other seats are bots drafting off the **market**: your ADP column, taken as the order the
room drafts in, weighted by each team's needs, with sampling noise so runs and reaches happen. Before each of
your picks the engine replays the picks between your turns **over a hundred times** and counts who
survives and who the best leftover at each position tends to be. That is where the survival
odds, the VONA numbers, and the "if he's gone, best next turn" lines come from.

### The plan
The side panel plans your next few picks, not just this one. Each future pick is scored with
the roster as it will stand by then and the odds at that pick, so it names who to take now
because he will not last, and who to wait on because he will.

### Two modes
**Mock draft**: practise against the bots from any seat. **Assist my live draft**: mark players
taken in draft order as your real room picks, and the recommendations track your actual draft on
any platform.
"""

CSV_HELP = f"""
Only **Name** and **Pos** are required; rows are taken in rank order if there is no Rank column.

```
{template_csv().strip()}
```

- **Pos** accepts `RB`, `RB12` (the number becomes the position rank), `DEF`/`D/ST`/`DST`, `PK`.
- **Proj** is projected season points and unlocks value over replacement and value over next available.
- **ADP** is an overall pick number (7.7 means the eighth player off the board). It is used as the room's order in any league size.
- **Tags**: `target`, `sleeper`, `breakout`, `watch`, `bust` (or `avoid`), separated by commas.
- Common FantasyPros headers (RK, PLAYER NAME, TEAM, POS, BYE WEEK, AVG) are recognised.
"""


@st.cache_resource(show_spinner=False, ttl=INJURY_TTL_S)
def load_injuries() -> str:
    """Refresh Sleeper's injury feed once per process; return the fetch date for the footer."""
    cache = injuries.refresh()
    return (cache.get("fetched_at") or "")[:10]


def esc(text: object) -> str:
    """HTML-escape any value for the markdown blocks below."""
    return html.escape("" if text is None else str(text))


def pos_chip(pos: str) -> str:
    """The bordered position badge, coloured by position and always labelled."""
    color = POS_COLORS.get(pos, MUTED)
    return f'<span class="pos" style="color:{color};border-color:{color}">{esc(pos)}</span>'


def factor_span(factor: dict) -> str:
    """One scoring factor as a coloured chip; the reason is the hover title."""
    delta = factor.get("delta")
    if delta is None:
        cls, text = "", esc(factor["label"])
    else:
        cls = "bad" if delta > 0 else "good"
        text = f"{esc(factor['label'])} {delta:+.1f}"
    return f'<span class="f {cls}" title="{esc(factor.get("detail"))}">{text}</span>'


def surv_html(surv: int) -> str:
    """Survival odds coloured safe/gone at the dashboard's thresholds."""
    color = GOOD if surv > SURV_SAFE else BAD if surv < SURV_GONE else "inherit"
    return f'<span class="surv" style="color:{color}">{surv}%</span>'


def rec_name_html(rec: dict) -> str:
    """Name line: injury and room chips plus team, bye, tier and rank."""
    bits = [f'<span class="nm">{esc(rec["name"])}']
    inj = rec.get("injury")
    if inj:
        title = " · ".join(
            str(b) for b in (inj.get("body_part"), inj.get("status"), inj.get("updated")) if b
        )
        bits.append(
            f'<span class="chip" style="color:{BAD}" title="{esc(title)}">{esc(inj.get("chip") or "?")}</span>'
        )
    delta = rec.get("market_delta")
    if delta is not None:
        color = GOOD if delta >= ROOM_GAP else BAD if delta <= -ROOM_GAP else MUTED
        title = f"market rank {rec.get('espn_rank')} vs your rank {rec.get('rank')}"
        bits.append(
            f'<span class="chip" style="color:{color}" title="{esc(title)}">ROOM {delta:+d}</span>'
        )
    for tag in rec.get("tags") or []:
        bits.append(
            f'<span class="chip" style="color:{TAG_COLORS.get(tag, MUTED)}">{TAG_LABELS.get(tag, tag)}</span>'
        )
    meta = f"{rec.get('team') or '—'} · bye {rec.get('bye') or '—'} · tier {rec.get('tier') or '—'} · rank {rec.get('rank') or '—'}"
    bits.append(f"<small>{esc(meta)}</small></span>")
    return "".join(bits)


class DraftApp:
    """Renders the whole page from Streamlit's session state."""

    def __init__(self) -> None:
        """Configure the page once per rerun and seed the session-state slots."""
        st.set_page_config(page_title="Draft Command", page_icon="🏈", layout="wide")
        st.markdown(CSS, unsafe_allow_html=True)
        self.state = st.session_state
        for key in ("session", "imported", "flash"):
            self.state.setdefault(key, None)

    def run(self) -> None:
        """Sidebar, header, then the tabs when a draft is running."""
        self._sidebar()
        st.markdown(
            '<h1 class="dc-title">DRAFT COMMAND</h1>'
            '<p class="dc-sub">Bring your own rankings. The engine does the rest.</p>',
            unsafe_allow_html=True,
        )
        session: DraftSession | None = self.state.session
        if session is None:
            self._welcome()
            return
        self._flash()
        live = session.live()
        self._header(session, live)
        board, teams, look, rankings, guide = st.tabs(
            ["Board", "Teams", "Lookahead", "Rankings", "How it thinks"]
        )
        with board:
            self._board_tab(session, live)
        with teams:
            self._teams_tab(session, live)
        with look:
            self._lookahead_tab(session, live)
        with rankings:
            self._rankings_tab()
        with guide:
            st.markdown(HOW_IT_THINKS)
        st.caption(
            f"Injuries from Sleeper as of {load_injuries() or 'never'} · "
            f"lookahead {live['lookahead_n']} rollouts in {live['lookahead_ms']:.0f} ms · "
            f"build {live['build_ms']:.0f} ms"
        )

    def _sidebar(self) -> None:
        with st.sidebar:
            st.header("Setup")
            source = st.radio("Rankings", [SOURCE_SAMPLE, SOURCE_UPLOAD], key="source")
            if source == SOURCE_UPLOAD:
                st.file_uploader("Rankings CSV", type=["csv"], key="upload")
            st.divider()
            teams = st.number_input("Teams", *TEAM_RANGE, value=DEFAULT_TEAMS, key="teams")
            st.number_input("Rounds", *ROUND_RANGE, value=DEFAULT_ROUNDS, key="rounds")
            st.number_input("Your seat", 1, int(teams), value=min(5, int(teams)), key="slot")
            st.radio("Mode", [MODE_MOCK, MODE_ASSIST], key="mode")
            st.button("Start draft", type="primary", on_click=self._start, width="stretch")
            if self.state.session is not None:
                st.button("Reset", on_click=self._reset, width="stretch")
            st.divider()
            st.download_button(
                "Download CSV template",
                template_csv(),
                "rankings_template.csv",
                "text/csv",
                width="stretch",
            )
            st.caption(
                "Only Name and Pos are required. Proj unlocks value math, ADP the market read, "
                "Tags your own scouting. Details on the Rankings tab."
            )

    def _start(self) -> None:
        try:
            imported = self._import_board()
            profile = LeagueProfile(teams=int(self.state.teams), rounds=int(self.state.rounds))
            session = DraftSession(
                imported.index, profile, int(self.state.slot), mock=self.state.mode == MODE_MOCK
            )
        except (RankingsFormatError, DraftError, ValueError, OSError) as exc:
            self.state.flash = ("error", f"Could not start: {exc}")
            return
        self.state.imported, self.state.session = imported, session
        self.state.flash = (
            "success",
            f"Loaded {imported.report.players} players · " + ", ".join(imported.report.unlocked()),
        )

    def _import_board(self) -> ImportedBoard:
        if self.state.source == SOURCE_UPLOAD:
            upload = self.state.get("upload")
            if upload is None:
                msg = "upload a rankings CSV first, or switch to the sample"
                raise RankingsFormatError(msg)
            data = upload.getvalue()
        else:
            data = SAMPLE_PATH.read_bytes()
        return RankingsImporter(teams=int(self.state.teams)).from_bytes(data)

    def _reset(self) -> None:
        self.state.session = None
        self.state.imported = None
        self.state.flash = None

    def _pick(self, name: str) -> None:
        session: DraftSession = self.state.session
        try:
            result = session.pick(name)
        except DraftError as exc:
            self.state.flash = ("error", str(exc))
            return
        who = "you" if result["mine"] else f"seat {result['team']}"
        self.state.flash = ("success", f"Pick {result['pick']}: {result['name']} to {who}")

    def _undo(self) -> None:
        removed = self.state.session.undo()
        self.state.flash = ("info", f"Removed {removed}" if removed else "Nothing to undo")

    def _flash(self) -> None:
        flash = self.state.flash
        if not flash:
            return
        kind, text = flash
        getattr(st, kind)(text)
        self.state.flash = None

    def _welcome(self) -> None:
        self._flash()
        st.markdown(
            "Pick your rankings source, league size and seat in the sidebar, then **Start draft**. "
            "The sample is ESPN's public default board, so you can try everything before uploading "
            "your own file."
        )
        with st.expander("What the rankings CSV should look like", expanded=True):
            st.markdown(CSV_HELP)
        st.markdown(HOW_IT_THINKS)

    def _header(self, session: DraftSession, live: dict) -> None:
        until = live["picks_until_ours"]
        cols = st.columns(5)
        cols[0].metric("Pick", live["pick_no"] if not session.is_over else "done")
        cols[1].metric("Round", live["round"] if not session.is_over else "—")
        cols[2].metric("Picks until yours", "—" if until is None else until)
        cols[3].metric("Your next pick", live["next_mine"] or "—")
        clock = live["on_clock_slot"]
        cols[4].metric(
            "On the clock",
            "—" if clock is None else ("you" if clock == session.slot else f"seat {clock}"),
        )
        if session.is_over:
            st.info("Draft complete. The recap is on the Board tab.")
        elif until == 0:
            st.success("▶ YOUR PICK — ON THE CLOCK")
        elif until is not None and until <= SOON_PICKS:
            st.warning(
                f"Your turn in {until} pick{'s' if until != 1 else ''} (pick #{live['next_mine']})"
            )

    def _board_tab(self, session: DraftSession, live: dict) -> None:
        if session.is_over:
            self._recap(session)
            return
        main, side = st.columns([2.2, 1])
        with main:
            self._action_bar(session)
            st.subheader("Recommendations")
            need = "  ".join(f"{p}:{c}" for p, c in live["my_counts"].items())
            st.caption(f"Your roster so far: {need or 'empty'}")
            for i, rec in enumerate(live["recs"], start=1):
                self._rec_row(session, i, rec)
        with side:
            self._side_panels(live)

    def _action_bar(self, session: DraftSession) -> None:
        names = [p["name"] for p in session.available("", SEARCH_LIMIT)]
        col_pick, col_btn, col_undo = st.columns([3, 1, 1], vertical_alignment="bottom")
        choice = col_pick.selectbox(
            "Draft or mark any player",
            names,
            index=None,
            placeholder="Search a player…",
            key="search",
        )
        verb = "Draft" if session.our_turn else f"Taken by seat {session.on_clock_slot}"
        col_btn.button(
            verb,
            disabled=choice is None,
            on_click=self._pick,
            args=(choice,),
            key="search-pick",
            width="stretch",
        )
        col_undo.button("Undo", on_click=self._undo, key="undo", width="stretch")

    def _rec_row(self, session: DraftSession, index: int, rec: dict) -> None:
        with st.container(border=True):
            cols = st.columns([0.35, 0.55, 4.2, 1.1, 0.9, 1.3], vertical_alignment="center")
            cols[0].markdown(f'<span class="meta">{index}</span>', unsafe_allow_html=True)
            cols[1].markdown(pos_chip(rec["pos"]), unsafe_allow_html=True)
            cols[2].markdown(rec_name_html(rec), unsafe_allow_html=True)
            cols[3].markdown(
                f'<span class="meta">{"ADP " + esc(rec["adp"]) if rec.get("adp") else ""}</span>',
                unsafe_allow_html=True,
            )
            cols[4].markdown(surv_html(rec["surv"]), unsafe_allow_html=True)
            label = "DRAFT" if session.our_turn else "TAKEN"
            cols[5].button(
                label,
                key=f"pick-{rec['name']}",
                on_click=self._pick,
                args=(rec["name"],),
                type="primary" if index == 1 else "secondary",
                width="stretch",
            )
            self._rec_details(rec)

    @staticmethod
    def _rec_details(rec: dict) -> None:
        why = rec.get("why") or []
        scored = sorted(
            (f for f in why if f.get("delta") is not None), key=lambda f: -abs(f["delta"])
        )[:TOP_FACTORS]
        rest = [f for f in why if f not in scored]
        line = "".join(factor_span(f) for f in scored)
        wait = rec.get("wait")
        if wait:
            line += (
                f'<span class="wait">if he\'s gone, best {esc(rec["pos"])} next turn: '
                f"{esc(wait['name'])} (~{wait['proj']} pts)</span>"
            )
        st.markdown(line, unsafe_allow_html=True)
        if rest:
            with st.expander(f"All {len(why)} factors · score {rec['score']}"):
                st.markdown("".join(factor_span(f) for f in rest), unsafe_allow_html=True)
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"factor": f["label"], "delta": f["delta"], "why": f["detail"]}
                            for f in why
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )

    @staticmethod
    def _side_panels(live: dict) -> None:
        queue = live.get("queue") or []
        if queue:
            st.subheader("Plan for your next picks")
            for slot in queue:
                st.markdown(
                    f'<span class="meta">#{slot["pick"]} (R{slot["round"]})</span> '
                    f"{pos_chip(slot['pos'])} <b>{esc(slot['name'])}</b> "
                    f'<span class="meta">· {esc(slot["reason"])}</span>',
                    unsafe_allow_html=True,
                )
        st.subheader("My roster")
        roster = "".join(f"<span>{esc(n)}</span>" for n in live["my_roster"]) or "<span>—</span>"
        st.markdown(f'<div class="roster">{roster}</div>', unsafe_allow_html=True)
        st.subheader("Position run (last 8)")
        runs = sorted(live["runs"].items(), key=lambda kv: -kv[1])
        pills = (
            "".join(
                f'<span class="pill {"hot" if c >= HOT_RUN else ""}">{esc(p)} &times; {c}</span>'
                for p, c in runs
            )
            or "—"
        )
        st.markdown(pills, unsafe_allow_html=True)
        st.subheader("Recent picks")
        for p in reversed(live["recent"]):
            st.markdown(
                f'<span class="meta">#{p["pick"]}</span> {esc(p["name"])} '
                f'<span class="meta">· seat {p["team"]}</span>',
                unsafe_allow_html=True,
            )

    def _teams_tab(self, session: DraftSession, live: dict) -> None:
        market = live.get("market") or {"pos_flow": {}, "notes": []}
        flow = []
        for pos, f in market["pos_flow"].items():
            delta = f["delta"]
            color = BAD if delta >= SOON_PICKS else GOOD if delta <= -SOON_PICKS else FLAG
            trend = f"+{delta} hot" if delta > 0 else f"{delta} cold" if delta < 0 else "on pace"
            flow.append(
                f'<span class="pill" style="color:{color};border-color:{color}">'
                f"{esc(pos)} {f['taken']} taken · {trend}</span>"
            )
        st.subheader(f"Market read · pick {live['pick_no']}")
        st.markdown("".join(flow), unsafe_allow_html=True)
        for note in market["notes"]:
            st.markdown(f'<div class="note">▸ {esc(note)}</div>', unsafe_allow_html=True)
        st.subheader("Rosters")
        seats = sorted(live["rosters"], key=int)
        for start in range(0, len(seats), TEAMS_PER_ROW):
            cols = st.columns(TEAMS_PER_ROW)
            for col, seat in zip(cols, seats[start : start + TEAMS_PER_ROW], strict=False):
                with col.container(border=True):
                    self._team_card(session, live, int(seat))

    @staticmethod
    def _team_card(session: DraftSession, live: dict, seat: int) -> None:
        roster = live["rosters"][seat]
        tag = "YOU" if seat == session.slot else "ON CLOCK" if seat == live["on_clock_slot"] else ""
        counts = " ".join(
            f'<span class="pill">{esc(p)} {c}</span>' for p, c in roster["counts"].items()
        )
        missing = (
            f'<div class="missing">needs: {esc(", ".join(roster["missing"]))}</div>'
            if roster["missing"]
            else ""
        )
        players = "".join(
            f'<li><span class="pk">{pl["pick"]}</span>{pos_chip(pl["p"])} {esc(pl["n"])}</li>'
            for pl in roster["players"]
        )
        st.markdown(
            f'<div class="card"><h4>SEAT {seat}<span class="tag">{tag}</span></h4>'
            f"{counts}{missing}<ul>{players}</ul></div>",
            unsafe_allow_html=True,
        )

    def _lookahead_tab(self, session: DraftSession, live: dict) -> None:
        if session.is_over:
            st.info("The draft is over; nothing left to look ahead to.")
            return
        look = session.lookahead()
        nxt, after = look["next_mine"], look["after"]
        st.markdown(
            f"The engine replayed the picks between now and your turn **{look['n']} times** "
            f"in {look['elapsed_ms']:.0f} ms. Your next pick is **#{nxt}**"
            + (f", then **#{after}**." if after else ".")
        )
        rows = []
        for rec in live["recs"][:LOOKAHEAD_ROWS]:
            surv = look["survival"].get(norm_name(rec["name"]), rec["surv"] / 100)
            surv_after = look["survival_after"].get(norm_name(rec["name"]))
            rows.append(
                {
                    "Player": rec["name"],
                    "Pos": rec["pos"],
                    "Rank": rec["rank"],
                    f"Still there at #{nxt}": round(surv * 100),
                    f"Still there at #{after}": None
                    if surv_after is None
                    else round(surv_after * 100),
                }
            )
        frame = pd.DataFrame(rows)
        progress = {
            c: st.column_config.ProgressColumn(c, min_value=0, max_value=100, format="%d%%")
            for c in frame.columns
            if c.startswith("Still there")
        }
        st.dataframe(frame, hide_index=True, width="stretch", column_config=progress)
        st.subheader("Expected best available at your next pick")
        best = [
            {
                "Position": pos,
                "Most likely": nb["p50_name"],
                "Mean projection": round(nb["mean_proj"], 1),
                "Mean rank": round(nb["mean_rank"], 1),
            }
            for pos, nb in look["next_best"].items()
        ]
        if best:
            st.dataframe(pd.DataFrame(best), hide_index=True, width="stretch")

    def _rankings_tab(self) -> None:
        imported: ImportedBoard | None = self.state.imported
        if imported is None:
            return
        report = imported.report
        st.markdown(
            f"**{report.players} players** · unlocked: {', '.join(report.unlocked())}"
            + (f" · skipped {len(report.skipped)} rows" if report.skipped else "")
        )
        rows = [
            {
                "Rank": p["overall_rank"],
                "Player": p["name"],
                "Pos": p["pos"],
                "Pos rank": p["pos_rank"],
                "Team": p["team"],
                "Bye": p["bye"],
                "Tier": p["tier"],
                "Proj": p["proj_points"],
                "Market ADP": p["espn_adp"],
                "Market rank": p["espn_rank"],
                "Market vs you": p["market_delta"],
                "Tags": ", ".join(p["my_tags"]),
            }
            for p in imported.players
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=600)
        with st.expander("CSV format"):
            st.markdown(CSV_HELP)
        if report.skipped:
            with st.expander("Skipped rows"):
                st.write(report.skipped)

    @staticmethod
    def _recap(session: DraftSession) -> None:
        st.subheader("Draft recap · projected starting lineups")
        rows = [
            {
                "Rank": r["rank"],
                "Seat": f"{r['slot']}{' (you)' if r['mine'] else ''}",
                "Starters proj": r["proj"],
                "Starters": " · ".join(r["starters"]),
            }
            for r in session.recap()
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        mine = [p for p in session.picks if p["team"] == session.slot]
        st.subheader("Your picks")
        for p in mine:
            player = session.lookup(p["name"]) or {}
            st.markdown(
                f'<span class="pk">{p["pick"]}</span>{pos_chip(player.get("pos", "?"))} '
                f'{esc(p["name"])} <span class="meta">· {player.get("team") or ""} · '
                f"proj {player.get('proj_points') or '—'}</span>",
                unsafe_allow_html=True,
            )


if __name__ == "__main__":
    DraftApp().run()
