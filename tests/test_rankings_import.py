"""Offline tests for rankings_import: any CSV becomes the board the engine scores."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from rankings_import import RankingsFormatError, RankingsImporter, template_csv

MINIMAL = "Name,Pos\nAlpha Back,RB\nBravo Wide,WR\nCharlie Back,RB\nDelta End,TE\n"
FANTASYPROS = (
    "RK,PLAYER NAME,TEAM,POS,BYE WEEK,AVG\n"
    "1,Alpha Back,DET,RB1,6,1.5\n"
    "2,Bravo Wide,CIN,WR1,6,3.0\n"
    "3,Charlie Back,ATL,RB2,11,\n"
)
FULL = (
    "Rank,Name,Pos,Team,Bye,Tier,Proj,ADP,Tags\n"
    "1,Alpha Back,RB,DET,6,1,300,1,\n"
    '2,Bravo Wide,WR,CIN,6,1,290,2,"target, sleeper"\n'
    "3,Echo Arm,QB,BUF,7,1,380,20,breakout\n"
    "4,Texans D/ST,DST,HOU,8,,120,150,\n"
    "5,Foxtrot Leg,PK,BAL,13,,140,160,avoid\n"
)


def test_minimal_csv_ranks_by_row_order_and_assigns_position_ranks() -> None:
    board = RankingsImporter(teams=12).from_text(MINIMAL)
    ranks = [(p["name"], p["overall_rank"], p["pos_rank"]) for p in board.players]
    assert ranks == [
        ("Alpha Back", 1, 1),
        ("Bravo Wide", 2, 1),
        ("Charlie Back", 3, 2),
        ("Delta End", 4, 1),
    ]
    assert [p["flex_rank"] for p in board.players] == [1, 2, 3, 4]
    assert all(p["espn_rank"] == p["overall_rank"] for p in board.players)
    assert board.report.unlocked() == ["rankings", "survival odds", "AI opponents"]


def test_fantasypros_headers_are_recognised_and_adp_is_kept_as_is() -> None:
    board = RankingsImporter(teams=16).from_text(FANTASYPROS)
    alpha, bravo, charlie = board.players
    assert board.columns["adp"] == "AVG"
    assert alpha["pos_rank"] == 1 and charlie["pos_rank"] == 2
    assert alpha["team"] == "DET" and alpha["bye"] == 6
    assert alpha["espn_adp"] == 1.5 and bravo["espn_adp"] == 3.0
    assert charlie["espn_adp"] is None and charlie["espn_rank"] == 3
    assert board.report.has_adp and board.report.has_bye and not board.report.has_projections


def test_defences_and_kickers_keep_only_a_position_rank_and_alias_to_one_record() -> None:
    board = RankingsImporter(teams=12).from_text(FULL)
    texans = board.index["houston texans"]
    assert texans["name"] == "Houston Texans" and texans["team"] == "HOU"
    assert texans["overall_rank"] is None and texans["pos_rank"] == 1
    assert board.index["texans d/st"] is texans and board.index["texans"] is texans
    kicker = board.index["foxtrot leg"]
    assert kicker["pos"] == "K" and kicker["overall_rank"] is None


def test_tags_are_normalised_to_the_engine_vocabulary() -> None:
    board = RankingsImporter(teams=12).from_text(FULL)
    assert board.index["bravo wide"]["my_tags"] == ["value", "sleeper"]
    assert board.index["echo arm"]["my_tags"] == ["breakout"]
    assert board.index["foxtrot leg"]["my_tags"] == ["bust"]
    assert board.report.tagged == 3
    assert board.index["alpha back"]["proj_points"] == 300.0 and board.report.has_tiers


def test_market_delta_compares_room_rank_with_your_rank() -> None:
    board = RankingsImporter(teams=12).from_text(FULL)
    assert board.index["echo arm"]["market_delta"] == 0
    assert board.index["houston texans"]["market_delta"] is None


def test_missing_position_column_is_a_format_error_naming_the_headers() -> None:
    with pytest.raises(RankingsFormatError, match="missing column"):
        RankingsImporter(teams=12).from_text("Player,Team\nAlpha Back,DET\n")


def test_duplicates_and_blank_rows_are_skipped_not_fatal() -> None:
    board = RankingsImporter(teams=12).from_text("Name,Pos\nAlpha Back,RB\n,RB\nAlpha Back,RB\n")
    assert board.report.players == 1 and len(board.report.skipped) == 2


def test_bytes_with_a_bom_import_like_text() -> None:
    board = RankingsImporter(teams=12).from_bytes(("﻿" + MINIMAL).encode("utf-8"))
    assert board.report.players == 4


def test_template_header_lists_every_recognised_column() -> None:
    assert template_csv().strip() == "Rank,Name,Pos,Team,Bye,Tier,Proj,ADP,Tags"
