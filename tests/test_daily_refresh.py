import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import daily_refresh as dr  # noqa: E402
import udk_fetch as uf  # noqa: E402


def _board(players):
    return {"players": players}


def _p(name, pos, rank, tier=1, proj=200.0):
    return {"name": name, "pos": pos, "overall_rank": rank, "tier": tier, "proj_points": proj}


def test_board_diff_detects_movers_tiers_proj_adds_drops():
    old = _board([_p("A One", "RB", 1), _p("B Two", "WR", 2, tier=1, proj=300),
                  _p("C Three", "TE", 3), _p("D Gone", "QB", 4)])
    new = _board([_p("A One", "RB", 2), _p("B Two", "WR", 1, tier=2, proj=280),
                  _p("C Three", "TE", 3), _p("E New", "RB", 4)])
    d = dr.board_diff(old, new)
    moves = {p["name"]: delta for delta, p, _, _ in d["movers"]}
    assert moves == {"A One": -1, "B Two": 1}
    assert [(p["name"], a, b) for p, a, b in d["tiers"]] == [("B Two", 1, 2)]
    assert [(p["name"], round(delta)) for delta, p, _, _ in d["proj"]] == [("B Two", -20)]
    assert [p["name"] for p in d["added"]] == ["E New"]
    assert [p["name"] for p in d["dropped"]] == ["D Gone"]


def test_board_diff_identical_boards_is_quiet():
    b = _board([_p("A One", "RB", 1), _p("B Two", "WR", 2)])
    d = dr.board_diff(b, b)
    assert not any(d[k] for k in ("movers", "tiers", "proj", "added", "dropped"))


def test_write_changes_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "REPORTS", tmp_path)
    monkeypatch.setattr(dr, "CHANGES", tmp_path / "udk_changes.md")
    old = _board([_p("A One", "RB", 1)])
    new = _board([_p("A One", "RB", 3)])
    text = dr.write_changes(dr.board_diff(old, new), "2026-09-04 06:30")
    assert "Rank movers" in text and "| -2 | A One | RB | 1 | 3 |" in text
    assert (tmp_path / "udk_changes.md").exists()


def test_validate_csv_requires_header_and_rows():
    good = "Name,Position,Team,Bye Week,Rank,Points,Tier\n" + \
        "\n".join(f"P{i},RB,DET,6,{i},{300 - i},1" for i in range(1, 15))
    ok, why = uf.validate_csv(good, {"Name", "Rank", "Points", "Tier"})
    assert ok and "14 rows" in why
    ok, why = uf.validate_csv("Name,Rank\nA,1\n", {"Name", "Rank", "Points"})
    assert not ok and "missing columns" in why
    ok, why = uf.validate_csv("<html>login</html>", {"Name"})
    assert not ok


def test_pages_cover_every_board_input():
    # draft_board.py reads exactly these stems from data/udk/
    assert set(uf.PAGES) == {"qb", "rb", "wr", "te", "dst", "k", "flex", "top200"}
