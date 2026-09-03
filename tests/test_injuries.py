import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import json
from datetime import datetime, timedelta, timezone

import pytest

import injuries


FAKE_PAYLOAD = {
    "1001": {
        "full_name": "Amon-Ra St. Brown",
        "first_name": "Amon-Ra",
        "last_name": "St. Brown",
        "position": "WR",
        "team": "DET",
        "fantasy_positions": ["WR"],
        "status": "Active",
        "injury_status": "Questionable",
        "injury_body_part": "Ankle",
        "injury_notes": "Limited in practice.",
        "practice_participation": "Limited",
        "news_updated": int(datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp() * 1000),
    },
    "1002": {
        "full_name": "Some IR Guy",
        "first_name": "Some",
        "last_name": "IR Guy",
        "position": "RB",
        "team": "KC",
        "fantasy_positions": ["RB"],
        "status": "Inactive",
        "injury_status": "IR",
        "injury_body_part": "Knee",
        "injury_notes": "Torn ACL, out for season.",
        "practice_participation": None,
        "news_updated": int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1000),
    },
    "1003": {
        "full_name": "Healthy Fella",
        "first_name": "Healthy",
        "last_name": "Fella",
        "position": "WR",
        "team": "SF",
        "fantasy_positions": ["WR"],
        "status": "Active",
        "injury_status": "",
        "injury_body_part": None,
        "injury_notes": None,
        "practice_participation": None,
        "news_updated": None,
    },
    "1004": {
        "full_name": "Marvin Harrison Jr.",
        "first_name": "Marvin",
        "last_name": "Harrison Jr.",
        "position": "WR",
        "team": "ARI",
        "fantasy_positions": ["WR"],
        "status": "Active",
        "injury_status": "Doubtful",
        "injury_body_part": "Hamstring",
        "injury_notes": "Held out of practice.",
        "practice_participation": "DNP",
        "news_updated": int(datetime(2026, 8, 29, tzinfo=timezone.utc).timestamp() * 1000),
    },
    "1005": {
        "full_name": "Some Defense",
        "first_name": "Some",
        "last_name": "Defense",
        "position": "DEF",
        "team": "SEA",
        "fantasy_positions": ["DEF"],
        "status": "Active",
        "injury_status": "Questionable",
        "injury_body_part": None,
        "injury_notes": None,
        "practice_participation": None,
        "news_updated": None,
    },
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(injuries, "CACHE", tmp_path / "injuries.json")
    yield


def fake_fetch_ok(timeout=20.0):
    return FAKE_PAYLOAD


def test_questionable_player_maps_to_chip_q(monkeypatch):
    monkeypatch.setattr(injuries, "_fetch_raw", fake_fetch_ok)
    injuries.refresh(force=True)

    rec = injuries.injury_for("Amon-Ra St. Brown")
    assert rec is not None
    assert rec["chip"] == "Q"
    assert rec["penalty"] == 6.0
    assert rec["severity"] == "quest"
    assert injuries.injury_penalty("Amon-Ra St. Brown") == 6.0


def test_ir_maps_to_penalty_60_severity_out(monkeypatch):
    monkeypatch.setattr(injuries, "_fetch_raw", fake_fetch_ok)
    injuries.refresh(force=True)

    rec = injuries.injury_for("Some IR Guy")
    assert rec is not None
    assert rec["penalty"] == 60.0
    assert rec["severity"] == "out"


def test_healthy_player_not_cached_penalty_zero(monkeypatch):
    monkeypatch.setattr(injuries, "_fetch_raw", fake_fetch_ok)
    injuries.refresh(force=True)

    assert injuries.injury_for("Healthy Fella") is None
    assert injuries.injury_penalty("Healthy Fella") == 0.0
    # And an entirely unknown player also resolves to 0 / None.
    assert injuries.injury_for("Nobody Atall") is None
    assert injuries.injury_penalty("Nobody Atall") == 0.0


def test_name_normalization_matches_suffix_variants(monkeypatch):
    monkeypatch.setattr(injuries, "_fetch_raw", fake_fetch_ok)
    injuries.refresh(force=True)

    rec1 = injuries.injury_for("marvin harrison")
    rec2 = injuries.injury_for("Marvin Harrison Jr")
    assert rec1 is not None
    assert rec2 is not None
    assert rec1["chip"] == rec2["chip"] == "D"
    assert rec1["penalty"] == rec2["penalty"] == 15.0


def test_fresh_cache_not_refetched_force_refetches(monkeypatch):
    calls = {"n": 0}

    def counting_fetch(timeout=20.0):
        calls["n"] += 1
        return FAKE_PAYLOAD

    monkeypatch.setattr(injuries, "_fetch_raw", counting_fetch)

    injuries.refresh(force=True)
    assert calls["n"] == 1

    # Cache is now fresh (just written) -> a plain refresh should not refetch.
    injuries.refresh(force=False)
    assert calls["n"] == 1

    # force=True always refetches regardless of freshness.
    injuries.refresh(force=True)
    assert calls["n"] == 2


def test_network_failure_returns_existing_cache_unchanged(monkeypatch):
    monkeypatch.setattr(injuries, "_fetch_raw", fake_fetch_ok)
    injuries.refresh(force=True)
    before = injuries.load()

    def broken_fetch(timeout=20.0):
        raise ConnectionError("network is down")

    monkeypatch.setattr(injuries, "_fetch_raw", broken_fetch)
    result = injuries.refresh(force=True)

    assert result == before
    assert injuries.load() == before
    # Still resolves fine from the untouched cache.
    rec = injuries.injury_for("Some IR Guy")
    assert rec is not None
    assert rec["penalty"] == 60.0
