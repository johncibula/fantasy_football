"""Offline tests for draft_session: mock and assist drafts on a synthetic board."""

import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

import draft_live
import draft_tracker as dt
from draft_session import DraftError, DraftSession, LeagueProfile
from rankings_import import RankingsImporter

POOL = {"QB": 30, "RB": 70, "WR": 90, "TE": 30, "K": 20, "DST": 20}
TOP_PROJ = {"QB": 380.0, "RB": 320.0, "WR": 310.0, "TE": 250.0, "K": 150.0, "DST": 130.0}
STARTERS = (("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1), ("DST", 1), ("K", 1))


def synthetic_csv() -> str:
    rows = ["Name,Pos,Team,Bye,Proj,ADP"]
    order = []
    for pos, count in POOL.items():
        for i in range(1, count + 1):
            proj = TOP_PROJ[pos] - i * (TOP_PROJ[pos] / (count + 5))
            order.append(
                (
                    proj if pos not in ("K", "DST") else proj - 200,
                    f"{pos} Player{i},{pos},TEAM{i % 8},{5 + i % 9},{proj:.1f}",
                )
            )
    order.sort(key=lambda r: -r[0])
    rows += [f"{line},{n}" for n, (_, line) in enumerate(order, start=1)]
    return "\n".join(rows) + "\n"


BOARD = RankingsImporter(teams=10).from_text(synthetic_csv()).index


def legal(session: DraftSession, seat: int) -> bool:
    comp = Counter(session.lookup(p["name"])["pos"] for p in session.picks if p["team"] == seat)
    return all(comp[pos] >= n for pos, n in STARTERS)


def test_mock_start_lets_the_bots_draft_up_to_our_seat() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=4, mock=True, seed=1)
    assert session.pick_no == 4 and session.our_turn
    assert {p["team"] for p in session.picks} == {1, 2, 3}


def test_full_mock_draft_ends_legal_for_every_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(draft_live, "ROLLOUT_N", 30)
    session = DraftSession(BOARD, LeagueProfile(teams=8, rounds=10), slot=8, mock=True, seed=7)
    mine = dt.my_pick_numbers(8, 8, 10)
    while not session.is_over:
        top = session.live()["recs"][0]
        picks_left = sum(1 for n in mine if n >= session.pick_no)
        assert top["pos"] not in ("K", "DST") or picks_left <= 4, (session.pick_no, top["name"])
        session.pick(top["name"])
    assert len(session.picks) == 80
    assert legal(session, 8)
    bots = [
        Counter(session.lookup(p["name"])["pos"] for p in session.picks if p["team"] == seat)
        for seat in range(1, 8)
    ]
    assert all(comp["K"] == 1 and comp["DST"] == 1 for comp in bots)
    recap = session.recap()
    assert [r["rank"] for r in recap] == list(range(1, 9)) and sum(r["mine"] for r in recap) == 1


def test_mock_undo_rewinds_through_the_bots_to_our_previous_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(draft_live, "ROLLOUT_N", 30)
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=4, mock=True, seed=1)
    first_turn = session.pick_no
    session.pick(session.live()["recs"][0]["name"])
    assert session.pick_no == 17
    assert session.undo() is not None
    assert session.pick_no == first_turn and session.our_turn


def test_assist_mode_records_whoever_is_on_the_clock() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=1, mock=False)
    ours = session.pick("RB Player1")
    theirs = session.pick("RB Player2")
    assert ours["mine"] and ours["team"] == 1
    assert not theirs["mine"] and theirs["team"] == 2
    assert session.undo() == "RB Player2" and session.pick_no == 2


def test_bad_picks_raise_messages_fit_for_the_user() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=1, mock=False)
    session.pick("RB Player1")
    with pytest.raises(DraftError, match="already drafted"):
        session.pick("rb player1")
    with pytest.raises(DraftError, match="not on this board"):
        session.pick("Nobody Real")
    with pytest.raises(DraftError, match="slot must be"):
        DraftSession(BOARD, LeagueProfile(teams=10), slot=11, mock=False)


def test_live_payload_is_reused_until_the_state_changes_and_hides_profiles() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=3, mock=False)
    first = session.live()
    assert session.live() is first
    assert all(rec["profile"] is None for rec in first["recs"])
    session.pick("RB Player1")
    assert session.live() is not first


def test_available_search_is_case_insensitive_and_rank_ordered() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=3, mock=False)
    hits = session.available("wr player1", limit=3)
    assert [p["name"] for p in hits] == ["WR Player1", "WR Player10", "WR Player11"]


def test_league_shape_travels_with_the_state_into_replacement_depths() -> None:
    small = LeagueProfile(teams=8).league_config()
    big = LeagueProfile(teams=16).league_config()
    assert draft_live.league_config({"league": small}) == {"league": small}
    assert draft_live.league_config({"teams": 16}) is None
    assert (
        draft_live.replacement_depths({"league": small})["RB"]
        < draft_live.replacement_depths({"league": big})["RB"]
    )


def test_untagged_board_never_fires_the_target_based_qb_rules() -> None:
    session = DraftSession(BOARD, LeagueProfile(teams=10, rounds=12), slot=5, mock=False)
    live = session.live()
    qb_factors = [
        f
        for rec in live["recs"]
        if rec["pos"] == "QB"
        for f in rec["why"]
        if f["label"] == "QB plan"
    ]
    assert qb_factors, "expected a QB among the early recommendations"
    assert all(f["delta"] is None and "no QB targets tagged" in f["detail"] for f in qb_factors)
    assert any("No QB targets tagged" in note for note in live["market"]["notes"])
