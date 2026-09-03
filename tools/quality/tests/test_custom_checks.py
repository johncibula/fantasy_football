"""Comment rules over added lines."""

from __future__ import annotations

from pathlib import Path

from custom_checks import AddedLine, CommentRules

FILE = Path("/repo/pkg/mod.py")


def added(*texts: str, start: int = 1) -> list[AddedLine]:
    """Build consecutive added lines for ``texts``."""
    return [AddedLine(FILE, start + i, t) for i, t in enumerate(texts)]


def rules_of(lines: list[AddedLine]) -> list[str]:
    """Rule names that fired, in order."""
    return [f.rule for f in CommentRules().check(lines)]


def test_narrative_comment_is_a_finding() -> None:
    assert rules_of(added("# check if the file exists", "x = 1")) == ["narrative"]


def test_narrative_trailing_comment_is_a_finding() -> None:
    assert rules_of(added("x = 1  # now loop over items")) == ["narrative"]


def test_why_comment_passes() -> None:
    assert rules_of(added("# ESPN caps the feed at 2 requests/s, so back off here", "x = 1")) == []


def test_ticket_key_exempts_a_narrative_line() -> None:
    assert rules_of(added("# check the cache first, see ENG-142")) == []


def test_plan_doc_path_exempts_a_narrative_line() -> None:
    assert rules_of(added("# now loop over picks (docs/plans/01-lookahead.md)")) == []


def test_three_consecutive_comment_lines_are_a_block() -> None:
    assert rules_of(added("# because one", "# because two", "# because three", "x = 1")) == [
        "block"
    ]


def test_two_comment_lines_are_not_a_block() -> None:
    assert rules_of(added("# because one", "# because two", "x = 1")) == []


def test_non_consecutive_comment_lines_are_not_a_block() -> None:
    lines = added("# because one", "# because two") + added("# because three", start=10)
    assert rules_of(lines) == []


def test_banner_is_a_finding() -> None:
    assert rules_of(added("# ==== helpers ====")) == ["banner"]
    assert rules_of(added("# ----------------------------------------")) == ["banner"]


def test_shebang_cookie_noqa_and_todo_are_exempt() -> None:
    lines = added(
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        "x = 1  # noqa: E501 - long url",
        "# TODO: make this a class",
        "# type: ignore[attr-defined]",
    )
    assert rules_of(lines) == []


def test_hash_inside_a_string_is_not_a_comment() -> None:
    assert rules_of(added('url = "https://x.test/#now"')) == []
