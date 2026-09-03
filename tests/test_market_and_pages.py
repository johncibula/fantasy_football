import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import market_feed as mf  # noqa: E402
import player_pages as pp  # noqa: E402
import draft_sim  # noqa: E402
import draft_tracker as dt  # noqa: E402


def test_room_order_uses_adp_then_rank_and_drops_saturated_adp(monkeypatch, tmp_path):
    espn = [
        {"name": "A", "pos": "RB", "team": "DET", "espn_adp": 3.0, "espn_rank": 5},
        {"name": "B", "pos": "WR", "team": "CIN", "espn_adp": 1.5, "espn_rank": 1},
        {"name": "C", "pos": "WR", "team": "LAR", "espn_adp": 170.0, "espn_rank": 900},   # undrafted on ESPN
        {"name": "D", "pos": "TE", "team": "SF", "espn_adp": 169.9, "espn_rank": 300},    # undrafted, better rank
        {"name": "Texans D/ST", "pos": "DST", "team": "HOU", "espn_adp": 120.0, "espn_rank": 200},
    ]
    monkeypatch.setattr(mf, "fetch_espn", lambda year, **kw: espn)
    monkeypatch.setattr(mf, "fetch_ffc", lambda year, **kw: ({"a": 2.7}, 12))
    monkeypatch.setattr(mf, "MARKET", tmp_path / "market.json")
    cache = mf.refresh(year=2026)
    players = cache["players"]
    assert players["b"]["espn_order"] == 1
    assert players["a"]["espn_order"] == 2
    assert players["texans d/st"]["espn_order"] == 3
    assert players["d"]["espn_order"] == 4 and players["d"]["espn_adp"] is None
    assert players["c"]["espn_order"] == 5 and players["c"]["espn_adp"] is None
    assert players["a"]["ffc_adp"] == 2.7
    # D/ST is reachable under the board's nickname aliases too
    assert players["texans"]["name"] == "Texans D/ST"


def test_bots_prefer_the_room_board_when_present():
    with_market = {"overall_rank": 60, "adp_overall": 50, "espn_adp": 30.0, "espn_rank": 28}
    without = {"overall_rank": 60, "adp_overall": 50}
    assert draft_sim.market_rank(with_market) == 0.6 * 30.0 + 0.4 * 28
    assert draft_sim.market_rank({"overall_rank": 60, "espn_rank": 300, "espn_adp": None}) == 300.0
    assert draft_sim.market_rank(without) == 0.5 * 60 + 0.5 * (50 * 4 / 3)


def test_analytic_survival_uses_espn_adp_when_present():
    state = {"teams": 16, "slot": 7, "rounds": 15, "picks": []}
    board = {}
    early = {"name": "X", "pos": "RB", "overall_rank": 80, "adp_overall": 80, "espn_adp": 20.0}
    late = {"name": "Y", "pos": "RB", "overall_rank": 80, "adp_overall": 80, "espn_adp": 150.0}
    s_early = dt.survival_odds(state, board, early, 8, 26)
    s_late = dt.survival_odds(state, board, late, 8, 26)
    assert s_early < s_late


def test_player_page_slug_and_key():
    assert pp.slug("Josh Allen") == "josh-allen"
    assert pp.slug("Ja'Marr Chase") == "jamarr-chase"
    assert pp.slug("Marvin Harrison Jr.") == "marvin-harrison-jr"
    assert pp.slug("De'Von Achane") == "devon-achane"
    assert pp.key_for("Ja'Marr Chase") == "jamarr_chase"


def test_profile_for_reads_cached_page(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(pp, "PLAYERS_DIR", tmp_path)
    (tmp_path / "josh_allen.json").write_text(json.dumps({
        "url": "https://www.thefantasyfootballers.com/fantasy/josh-allen/",
        "summary": "Allen remains the QB1.", "fetched_at": "2026-09-03T10:00:00+00:00",
        "headings": ["Josh Allen", "Outlook"]}))
    prof = pp.profile_for("Josh Allen")
    assert prof["summary"] == "Allen remains the QB1."
    assert prof["fetched"] == "2026-09-03"
    assert pp.profile_for("Nobody Here") is None


def test_parse_profile_pulls_structured_fields():
    raw = "\n".join([
        "PUKA NACUA", "LAR", "#12", "Aug 28, 2026", "Injured: Questionable with Groin", "Week 1",
        "HT/WT 6' 2\", 216 lbs", "AGE 25.2", "DRAFTED Rd 5 (#177) - 2023", "EXPERIENCE 3 years",
        "2026 DRAFT RANKING", "CONSENSUS", "ADP", "1.07", "ANDY #3", "JASON #2", "MIKE #2",
        "PUKA NACUA OUTLOOK", "Puka was dominant in 2025.", "He is unstoppable.", "PODCASTS", "junk",
    ])
    heads = ["PUKA NACUA", "Working off to side Monday 3 DAYS AGO", "Back at practice Sunday 4 DAYS AGO",
             "Registrations are now open!", "PODCASTS"]
    f = pp.parse_profile("Puka Nacua", raw, heads)
    assert f["injury"] == "Questionable (Groin)" and f["injury_date"] == "Aug 28, 2026"
    assert f["adp"] == "1.07" and f["hosts"] == {"Andy": 3, "Jason": 2, "Mike": 2}
    assert f["age"] == 25.2 and f["experience"] == "3 years"
    assert [n["headline"] for n in f["news"]] == ["Working off to side Monday", "Back at practice Sunday"]
    assert f["news"][0]["age"] == "3 days ago"
    assert f["outlook"] == "Puka was dominant in 2025. He is unstoppable."


def test_slug_candidates_drop_suffix_first_then_variants():
    c = pp.slug_candidates("James Cook III")
    assert c[:2] == ["james-cook", "james-cook-iii"] and "james-cook-jr" in c
    assert pp.slug_candidates("Josh Allen")[0] == "josh-allen"
    assert "chigoziem-okonkwo" in pp.slug_candidates("Chig Okonkwo")
    assert "brian-robinson-jr" in pp.slug_candidates("Brian Robinson")
    assert "cameron-ward" in pp.slug_candidates("Cam Ward", espn_name="Cameron Ward")
