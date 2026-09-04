"""Headless run of the Streamlit page through Streamlit's own AppTest harness."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest
from streamlit.testing.v1 import AppTest

import draft_live
import injuries

APP = pathlib.Path(__file__).resolve().parents[1] / "src" / "draft_app.py"
NO_INJURIES = {"fetched_at": None, "players": {}}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setattr(injuries, "refresh", lambda **_: NO_INJURIES)
    monkeypatch.setattr(draft_live, "ROLLOUT_N", 30)
    return AppTest.from_file(str(APP), default_timeout=60)


def test_welcome_page_renders_without_a_draft(app: AppTest) -> None:
    app.run()
    assert not app.exception
    assert app.sidebar.button[0].label == "Start draft"
    assert any("Start draft" in md.value for md in app.markdown)


def test_start_draft_on_the_sample_shows_recommendations(app: AppTest) -> None:
    app.run()
    app.sidebar.button[0].click().run()
    assert not app.exception
    assert app.session_state["session"] is not None
    labels = [b.label for b in app.button]
    assert "DRAFT" in labels and "Undo" in labels
    assert any("Loaded" in s.value for s in app.success)


def test_drafting_the_top_recommendation_advances_the_mock(app: AppTest) -> None:
    app.run()
    app.sidebar.button[0].click().run()
    before = app.session_state["session"].pick_no
    draft_buttons = [b for b in app.button if b.label == "DRAFT"]
    draft_buttons[0].click().run()
    assert not app.exception
    assert app.session_state["session"].pick_no > before
    assert any("Pick" in s.value for s in app.success)
