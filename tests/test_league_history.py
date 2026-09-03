import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from league_history import learn, pos_multiplier, tendencies_by_slot  # noqa: E402

CURRENT_YEAR = 2026
SEASONS = [2022, 2023, 2024, 2025]

# Manager A: team_id 7 every year, QB-early (QB in rounds 2-4), and repeats
# "Steady Vet" at K for three straight seasons (loyalty check).
A_ROUNDS = {
    2022: ["RB", "QB", "WR", "RB", "WR", "TE", "K", "DST"],
    2023: ["RB", "WR", "QB", "RB", "WR", "TE", "K", "DST"],
    2024: ["WR", "QB", "RB", "WR", "RB", "TE", "K", "DST"],
    2025: ["RB", "WR", "WR", "QB", "RB", "TE", "K", "DST"],
}
A_NAMES = {
    2022: ["RB A1", "QB A", "WR A1", "RB A2", "WR A2", "TE A", "Steady Vet", "DST A"],
    2023: ["RB A1", "WR A1", "QB A", "RB A2", "WR A2", "TE A", "Steady Vet", "DST A"],
    2024: ["WR A1", "QB A", "RB A1", "WR A2", "RB A2", "TE A", "Steady Vet", "DST A"],
    2025: ["RB A1", "WR A1", "WR A2", "QB A", "RB A2", "TE A", "New Kicker", "DST A"],
}

# Managers B, C, D: never touch QB in rounds 1-8, fill out RB/WR/TE/K/DST.
BCD_ROUNDS = ["WR", "RB", "RB", "WR", "WR", "RB", "TE", "K"]


def _bcd_names(letter, season):
    return [f"{pos} {letter}{season}-{i}" for i, pos in enumerate(BCD_ROUNDS)]


def make_drafts(extra_one_season_manager=False):
    drafts = []
    for season in SEASONS:
        picks = []
        overall = 1
        # Round-robin by round so overall pick numbers are sane; not needed
        # for correctness, just realism.
        per_team = {
            7: (A_ROUNDS[season], A_NAMES[season], "owner-A"),
            3: (BCD_ROUNDS, _bcd_names("B", season), "owner-B"),
            9: (BCD_ROUNDS, _bcd_names("C", season), "owner-C"),
            11: (BCD_ROUNDS, _bcd_names("D", season), "owner-D"),
        }
        if extra_one_season_manager and season == 2025:
            # Replace team 11's owner with a one-season manager for this year.
            per_team[11] = (BCD_ROUNDS, _bcd_names("E", season), "owner-E")

        for rnd in range(8):
            for team_id, (rounds, names, owner) in per_team.items():
                picks.append({
                    "overall": overall,
                    "round": rnd + 1,
                    "team_id": team_id,
                    "owner": owner,
                    "name": names[rnd],
                    "pos": rounds[rnd],
                    "nfl_team": None,
                    "auto": False,
                    "keeper": False,
                })
                overall += 1
        teams = {
            7: {"name": f"Team A {season}", "owner": "owner-A"},
            3: {"name": f"Team B {season}", "owner": "owner-B"},
            9: {"name": f"Team C {season}", "owner": "owner-C"},
            11: {"name": (f"Team E {season}" if (extra_one_season_manager and season == 2025)
                          else f"Team D {season}"),
                 "owner": ("owner-E" if (extra_one_season_manager and season == 2025) else "owner-D")},
        }
        drafts.append({
            "season": season,
            "teams": teams,
            "order": [7, 3, 9, 11],
            "team_count": 4,
            "picks": picks,
        })
    return drafts


def test_qb_early_manager_bias_and_multiplier():
    drafts = make_drafts()
    model = learn(drafts, current_year=CURRENT_YEAR)
    profile = model["managers"]["owner-A"]

    assert profile["pos_bias"]["QB"] > 1.3
    assert "QB-early" in profile["labels"]
    assert pos_multiplier(profile, "QB", 1) < 1.0
    assert pos_multiplier(profile, "QB", 3) > 1.0


def test_one_season_manager_bias_shrunk_toward_one():
    drafts = make_drafts(extra_one_season_manager=True)
    model = learn(drafts, current_year=CURRENT_YEAR)
    profile = model["managers"]["owner-E"]

    assert profile["seasons"] == 1
    assert profile["confidence"] == 0.25
    # Confidence-shrunk bias must land closer to 1.0 than a fully-confident
    # manager's would for the same underlying behavior.
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        assert 0.4 <= profile["pos_bias"][pos] <= 2.5


def test_loyalty_counts_repeat_player():
    drafts = make_drafts()
    model = learn(drafts, current_year=CURRENT_YEAR)
    profile = model["managers"]["owner-A"]

    assert profile["loyalty"]["steady vet"] == 3
    assert "new kicker" not in profile["loyalty"]  # only drafted once


def test_tendencies_by_slot_maps_team_ids(tmp_path, monkeypatch):
    import league_history

    drafts = make_drafts()
    model = learn(drafts, current_year=CURRENT_YEAR)

    tendencies_path = tmp_path / "tendencies.json"
    import json
    with open(tendencies_path, "w") as f:
        json.dump(model, f)
    monkeypatch.setattr(league_history, "TENDENCIES_PATH", tendencies_path)

    by_slot = tendencies_by_slot([7, 3, 9, 11])
    assert by_slot[1]["latest_team_name"] == "Team A 2025"
    assert by_slot[2]["latest_team_name"] == "Team B 2025"
    assert "QB-early" in by_slot[1]["labels"]


def test_learn_empty_does_not_raise():
    model = learn([])
    assert model == {"seasons": [], "managers": {}}


def test_tendencies_by_slot_prefers_latest_holder_of_reused_team_id(tmp_path, monkeypatch):
    """Team 11 changes owners in 2025 (owner-D -> owner-E). The slot map must
    hand slot 4 to owner-E, the most recent holder, not whichever profile
    happens to iterate last."""
    import json
    import league_history

    drafts = make_drafts(extra_one_season_manager=True)
    model = learn(drafts, current_year=CURRENT_YEAR)
    assert model["managers"]["owner-D"]["team_id_latest"] == {"11": 2024}
    assert model["managers"]["owner-E"]["team_id_latest"] == {"11": 2025}

    tendencies_path = tmp_path / "tendencies.json"
    with open(tendencies_path, "w") as f:
        json.dump(model, f)
    monkeypatch.setattr(league_history, "TENDENCIES_PATH", tendencies_path)

    by_slot = tendencies_by_slot([7, 3, 9, 11])
    assert by_slot[4]["latest_team_name"] == "Team E 2025"


def test_labels_are_relative_to_league():
    """B, C, D never take a QB in the sampled rounds, so A's round-3 QB is
    'early' relative to the league; but no one should be tagged QB-late/early
    for merely matching the league norm."""
    drafts = make_drafts()
    model = learn(drafts, current_year=CURRENT_YEAR)
    assert model["league_first_pos_round"]["QB"] > 5
    assert "QB-early" in model["managers"]["owner-A"]["labels"]
    for key in ("owner-B", "owner-C", "owner-D"):
        assert "TE-early" not in model["managers"][key]["labels"]
